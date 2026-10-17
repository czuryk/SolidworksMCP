"""Read-only native body-pair evidence; unavailable results never certify a gap."""
import math

from ..shape_program import ProgramError
from .com_utils import com_get, typed, resolve_solidworks_constant

CONTACT_MM = 1e-5
NUMERIC_MM = 1e-7


def validate_pairs(queries):
    if queries is None:
        return []
    if not isinstance(queries, list) or not 1 <= len(queries) <= 8:
        raise ProgramError('INVALID_BODY_PAIRS', 'Supply one to eight targeted body pairs')
    ids = set()
    for q in queries:
        allowed = {'id', 'a', 'b', 'min_clearance_mm', 'max_clearance_mm'}
        if not isinstance(q, dict) or set(q)-allowed or not {'id', 'a', 'b'} <= set(q):
            raise ProgramError('INVALID_BODY_PAIRS', 'Each pair needs id, a and b')
        if not isinstance(q['id'], str) or not 1 <= len(q['id']) <= 80 or q['id'] in ids:
            raise ProgramError('INVALID_BODY_PAIRS', 'Pair ids must be unique nonempty strings')
        ids.add(q['id'])
        for side in ('a', 'b'):
            ref = q[side]
            if (not isinstance(ref, dict) or len(ref) != 1 or
                    not set(ref) <= {'body', 'component'} or
                    not isinstance(next(iter(ref.values())), str) or not next(iter(ref.values()))):
                raise ProgramError('INVALID_BODY_PAIRS', 'Use exactly one body or component name per endpoint')
        for key in ('min_clearance_mm', 'max_clearance_mm'):
            if key in q and (isinstance(q[key], bool) or not isinstance(q[key], (int, float)) or
                             not math.isfinite(q[key]) or q[key] < 0):
                raise ProgramError('INVALID_BODY_PAIRS', 'Clearance limits must be finite nonnegative mm')
        if q.get('min_clearance_mm', 0) > q.get('max_clearance_mm', math.inf):
            raise ProgramError('INVALID_BODY_PAIRS', 'Minimum clearance exceeds maximum')
    return queries


def _volume(body):
    props = com_get(typed(body, 'IBody2'), 'GetMassProperties', 1000.)
    if props is None or len(props) < 4:
        raise ProgramError('BODY_VOLUME_UNAVAILABLE', 'Native mass properties unavailable')
    value = float(props[3])*1e9
    if not math.isfinite(value) or value <= 0:
        raise ProgramError('BODY_VOLUME_UNAVAILABLE', 'Invalid native solid volume')
    return value


def _valid(body):
    report = com_get(typed(body, 'IBody2'), 'Check3')
    if report is None or int(com_get(typed(report, 'IFaultEntity'), 'Count')) != 0:
        raise ProgramError('BODY_KERNEL_INVALID', 'Body kernel check unavailable or failed')


def _operation(a, b, operation):
    # Operations2 consumes only fresh temporary copies, never document bodies.
    ca, cb = typed(a, 'IBody2').Copy2(False), typed(b, 'IBody2').Copy2(False)
    if ca is None or cb is None:
        raise ProgramError('BODY_COPY_FAILED', 'Cannot copy both bodies for a native Boolean check')
    result = typed(ca, 'IBody2').Operations2(resolve_solidworks_constant(operation), cb, 0)
    if not isinstance(result, tuple) or len(result) != 2:
        raise ProgramError('BODY_BOOLEAN_UNKNOWN', 'Unexpected native Boolean output')
    bodies, error = result
    if error != 0:
        raise ProgramError('BODY_BOOLEAN_FAILED', 'Native Boolean did not complete successfully',
                           operation=operation, native_error=error)
    values = []
    for body in bodies or []:
        _valid(body)
        values.append(_volume(body))
    return values


def native_intersection(a, b, boundary_distance_mm=None):
    _valid(a)
    _valid(b)
    va, vb = _volume(a), _volume(b)
    volumes = _operation(a, b, 'SWBODYINTERSECT')
    volume = math.fsum(volumes)
    method = 'IBody2.Operations2_INTERSECT_on_copies'
    if not volumes and (boundary_distance_mm is None or boundary_distance_mm <= CONTACT_MM):
        # Some API versions return no intersection body for coincident geometry.
        # A zero-error cut distinguishes that case from a genuine empty overlap.
        cut = _operation(a, b, 'SWBODYCUT')
        if not cut:
            volume = va
            method += '_CUT_containment_check'
        elif not math.isclose(math.fsum(cut), va, rel_tol=1e-8, abs_tol=1e-9):
            raise ProgramError('BODY_BOOLEAN_INCONSISTENT', 'Empty intersection changed the cut volume')
        else:
            method += '_CUT_unchanged_check'
    if volume > min(va, vb)*(1+1e-6)+1e-9:
        raise ProgramError('BODY_BOOLEAN_INCONSISTENT', 'Intersection exceeds the smaller source body')
    return {'volume_mm3': volume, 'method': method, 'native_error': 0}


def _distance(doc, a, b):
    answer = typed(doc, 'IModelDoc2').ClosestDistance(a, b)
    if not isinstance(answer, tuple) or len(answer) != 3:
        raise ProgramError('BODY_DISTANCE_UNAVAILABLE', 'Unexpected ClosestDistance result')
    distance, p, q = answer
    if not math.isfinite(distance) or distance < 0:
        raise ProgramError('BODY_DISTANCE_UNAVAILABLE', 'ClosestDistance returned no solution')
    if any(v is None or len(v) != 3 or not all(math.isfinite(x) for x in v) for v in (p, q)):
        raise ProgramError('BODY_DISTANCE_UNAVAILABLE', 'Native closest points unavailable')
    if not math.isclose(math.dist(p, q), distance, rel_tol=1e-7, abs_tol=NUMERIC_MM/1000):
        raise ProgramError('BODY_DISTANCE_INCONSISTENT', 'Closest points disagree with native distance')
    return {'distance_mm': float(distance)*1000,
            'points_mm': [[float(x)*1000 for x in v] for v in (p, q)],
            'method': 'IModelDoc2.ClosestDistance_solid_bodies'}


def _failure(phase, exc):
    return {'phase': phase, 'code': getattr(exc, 'code', 'NATIVE_MEASUREMENT_FAILED'),
            'message': str(exc), 'details': getattr(exc, 'details', {}),
            'hresult': getattr(exc, 'hresult', None)}


def measure_pair(doc, a, b, query=None):
    evidence = {'relation': 'UNKNOWN', 'boundary_distance_mm': None, 'clearance_mm': None,
                'intersection_volume_mm3': None, 'interference': None, 'errors': [],
                'volume_method': 'IBody2.GetMassProperties_numerical_integration',
                'contact_threshold_mm': CONTACT_MM,
                'coverage': 'Global body-pair minimum; not uniform local mating clearance, wall thickness or print readiness'}
    distance = None
    try:
        result = _distance(doc, a, b)
        distance = result['distance_mm']
        evidence.update(boundary_distance_mm=distance, closest_points_mm=result['points_mm'],
                        distance_method=result['method'])
    except Exception as exc:
        evidence['errors'].append(_failure('distance', exc))
    try:
        intersection = native_intersection(a, b, distance)
        volume = intersection['volume_mm3']
        evidence.update(intersection_volume_mm3=volume, interference=volume > 0,
                        intersection_method=intersection['method'])
        if volume > 0:
            evidence.update(relation='OVERLAP', clearance_mm=0.)
        elif distance is not None:
            evidence.update(relation='SEPARATED' if distance > CONTACT_MM else 'CONTACT_OR_NEAR',
                            clearance_mm=distance)
    except Exception as exc:
        evidence['errors'].append(_failure('intersection', exc))
    query = query or {}
    if 'min_clearance_mm' in query or 'max_clearance_mm' in query:
        passed = None
        if evidence['interference'] is True:
            passed = False
        elif evidence['relation'] != 'UNKNOWN':
            passed = (distance + NUMERIC_MM >= query.get('min_clearance_mm', 0) and
                      distance - NUMERIC_MM <= query.get('max_clearance_mm', math.inf))
        evidence['clearance_check'] = {
            **{k: query[k] for k in ('min_clearance_mm', 'max_clearance_mm') if k in query},
            'passed': passed, 'numeric_slack_mm': NUMERIC_MM}
    return evidence


def measure_pairs(automation, doc, record, queries):
    if not queries:
        return []
    bodies = {str(com_get(b, 'Name')): b for b in automation._get_solid_bodies(doc)}
    mapping = record['native']['components'] if record else {}

    def resolve(ref):
        if 'body' in ref:
            name = ref['body']
        else:
            item = mapping.get(ref['component'])
            if item is None:
                raise ProgramError('UNKNOWN_COMPONENT', ref['component'])
            if item.get('consumed_by'):
                raise ProgramError('COMPONENT_CONSUMED',
                    f"{ref['component']} is consumed by {item['consumed_by']}; address that resulting component",
                    component=ref['component'], consumed_by=item['consumed_by'])
            if len(item.get('bodies', [])) != 1:
                raise ProgramError('BODY_PAIR_SCOPE', 'Component must own one body; use explicit body names')
            name = item['bodies'][0]
        if name not in bodies:
            raise ProgramError('BODY_NOT_FOUND', name)
        return name

    resolved = [(q, resolve(q['a']), resolve(q['b'])) for q in queries]
    if any(a == b for _, a, b in resolved):
        raise ProgramError('BODY_PAIR_SCOPE', 'Choose two distinct bodies')
    return [{'id': q['id'], 'a': q['a'], 'b': q['b'], 'body_a': a, 'body_b': b,
             **measure_pair(doc, bodies[a], bodies[b], q)} for q, a, b in resolved]
