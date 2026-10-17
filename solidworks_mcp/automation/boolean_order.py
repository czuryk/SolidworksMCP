"""Bounded union planning on temporary bodies after a proven unchanged failure."""

import math

from ..shape_program import ProgramError
from .body_relations import _valid, _volume, measure_pair
from .com_utils import com_get, typed, resolve_solidworks_constant, is_rpc_disconnect

MAX_UNION_PROBES = 4096


def native_state_changes(before, after):
    """Compare identity/topology exactly and native measurements at machine precision.

    GetMassProperties can vary by a few ULPs even without a geometry change.
    This allowance is floating-point roundoff, not a CAD or manufacturing tolerance.
    """
    changes = {}
    if before['features'] != after['features']:
        old, new = before['features'], after['features']
        changes['features'] = {
            'added': sorted(new.keys()-old.keys())[:32],
            'removed': sorted(old.keys()-new.keys())[:32],
            'changed': sorted(n for n in old.keys() & new.keys() if old[n] != new[n])[:32]}
    if before['feature_count'] != after['feature_count']:
        changes['feature_count'] = {'before': before['feature_count'], 'after': after['feature_count']}
    old, new = before['bodies'], after['bodies']
    if old.keys() != new.keys():
        changes['body_names'] = {'added': sorted(new.keys()-old.keys())[:32],
                                 'removed': sorted(old.keys()-new.keys())[:32]}
    changed_bodies = {}
    for name in sorted(old.keys() & new.keys()):
        a, b = old[name], new[name]
        fields = [key for key in ('reference', 'face_count') if a.get(key) != b.get(key)]
        for key in ('box', 'mass'):
            left, right = a[key], b[key]
            if len(left) != len(right):
                fields.append(key)
                continue
            for i, (x, y) in enumerate(zip(left, right)):
                if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y)):
                    fields.append(key)
                    break
                if key == 'box' or i < 3:
                    scale = max(abs(v) for v in a['box'] + b['box'])
                elif i < 6:
                    scale = max(abs(x), abs(y))
                else:
                    # Off-diagonal moments near zero use the diagonal inertia scale.
                    scale = max(abs(v) for v in left[6:9] + right[6:9])
                if abs(x-y) > 64 * math.ulp(scale):
                    fields.append(key)
                    break
        if fields:
            changed_bodies[name] = fields
    if changed_bodies:
        changes['body_measurements'] = dict(list(changed_bodies.items())[:32])
        changes['changed_body_count'] = len(changed_bodies)
    return changes


def bounds(body):
    value = list(com_get(typed(body, 'IBody2'), 'GetBodyBox') or [])
    if len(value) != 6 or not all(math.isfinite(x) for x in value):
        raise ProgramError('BODY_BOUNDS_UNAVAILABLE', 'Union planning requires finite native bounds')
    return value


def overlaps(a, b):
    # Boxes only exclude separated candidates; touching boxes never prove a solid union.
    return all(a[i] <= b[i + 3] and b[i] <= a[i + 3] for i in range(3))


def union_copy(a, b):
    ca, cb = typed(a, 'IBody2').Copy2(False), typed(b, 'IBody2').Copy2(False)
    if ca is None or cb is None:
        raise ProgramError('BODY_COPY_FAILED', 'Cannot copy union inputs')
    answer = typed(ca, 'IBody2').Operations2(resolve_solidworks_constant('SWBODYADD'), cb, 0)
    if not isinstance(answer, tuple) or len(answer) != 2:
        raise ProgramError('BODY_BOOLEAN_UNKNOWN', 'Unexpected temporary union result')
    bodies, error = answer
    if error or len(bodies or []) != 1:
        return None, {'native_error': error, 'result_body_count': len(bodies or []),
                      'relation': 'NOT_PROVEN_JOINABLE'}
    body = bodies[0]
    _valid(body)
    _volume(body)
    return body, {'native_error': 0, 'result_body_count': 1, 'relation': 'VALID_SINGLE_SOLID_UNION'}


def plan_union(doc, bodies):
    """Pick a hub, then attach candidates to the aggregate; never mutate document bodies."""
    names = sorted(bodies)
    boxes = {name: bounds(bodies[name]) for name in names}
    neighbours = {name: {other for other in names if other != name and overlaps(boxes[name], boxes[other])}
                  for name in names}
    broad_groups, remaining = [], set(names)
    while remaining:
        pending, group = [min(remaining)], set()
        while pending:
            name = pending.pop()
            if name in group:
                continue
            group.add(name)
            pending.extend(neighbours[name] - group)
        remaining -= group
        broad_groups.append(sorted(group))
    diagnostics = {'strategy': 'aabb_pruned_temporary_union', 'probe_count': 0, 'relations': [],
                   'resolved_order': [], 'connectivity': 'UNKNOWN'}
    if len(broad_groups) != 1:
        return {**diagnostics, 'connectivity': 'DISCONNECTED', 'disconnected_groups': broad_groups,
                'evidence': 'Positive separation between native bounding boxes'}
    first = min(names, key=lambda n: (-len(neighbours[n]), n))
    aggregate = typed(bodies[first], 'IBody2').Copy2(False)
    if aggregate is None:
        raise ProgramError('BODY_COPY_FAILED', 'Cannot copy union seed')
    order, pending = [first], set(names) - {first}
    while pending:
        aggregate_box = bounds(aggregate)
        candidates = sorted((n for n in pending if overlaps(aggregate_box, boxes[n])),
                            key=lambda n: (-len(neighbours[n]), n))
        advanced = False
        for name in candidates:
            if diagnostics['probe_count'] >= MAX_UNION_PROBES:
                return {**diagnostics, 'resolved_order': order, 'unresolved_bodies': sorted(pending),
                        'reason': 'UNION_PROBE_BUDGET_EXCEEDED', 'max_probes': MAX_UNION_PROBES}
            diagnostics['probe_count'] += 1
            try:
                merged, evidence = union_copy(aggregate, bodies[name])
            except Exception as exc:
                if is_rpc_disconnect(exc):
                    raise
                merged, evidence = None, {'relation': 'UNKNOWN', 'message': str(exc),
                                          'code': getattr(exc, 'code', 'BODY_BOOLEAN_UNKNOWN')}
            if len(diagnostics['relations']) < 32:
                diagnostics['relations'].append({'aggregate': list(order), 'body': name, **evidence})
            if merged is not None:
                aggregate = merged
                order.append(name)
                pending.remove(name)
                advanced = True
                break
        if not advanced:
            # Targeted evidence distinguishes a gap/overlap from ambiguous point/edge contact.
            diagnostics['unresolved_relations'] = [
                {'body': name, **measure_pair(doc, aggregate, bodies[name])} for name in sorted(pending)[:8]]
            return {**diagnostics, 'resolved_order': order, 'unresolved_bodies': sorted(pending)}
    return {**diagnostics, 'resolved_order': order, 'connectivity': 'CONNECTED'}
