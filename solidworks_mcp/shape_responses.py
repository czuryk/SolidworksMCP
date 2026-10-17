"""Compact public answers for the shape tools; pure and COM-free.

The services keep producing their complete results. This module selects what
the caller needs for the next step: revision, parameters, bodies, what was
rebuilt and the warnings. verbose=True at the MCP boundary skips it.
"""

import collections
import math

# Component records are attached to a mutation answer only for a small change.
MAX_CHANGED_RECORDS = 12
PAIR_SHARED = ('volume_method', 'distance_method', 'intersection_method', 'coverage',
               'contact_threshold_mm')


def rounded(value, digits=6):
    """Round every float in a JSON-like value; 1e-6 mm is below native tolerance."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return value
        result = round(value, digits)
        return int(result) if result == int(result) and abs(result) < 1e15 else result
    if isinstance(value, dict):
        return {key: rounded(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [rounded(item, digits) for item in value]
    return value


def compact_bodies(body_state):
    bodies = []
    for body in (body_state or {}).get('bodies', []):
        item = {'name': body.get('name'), 'face_count': body.get('face_count'),
                'bbox': rounded(body.get('bbox'), 4)}
        if body.get('visible') is False:
            item['visible'] = False
        bodies.append(item)
    return bodies


def _bbox_method(body_state):
    return {'bbox_method': body_state['bbox_method']} if (body_state or {}).get('bbox_method') else {}


def compact_context(data, components=None):
    """Summary of get_model_context; the component map is opt-in."""
    keep = ('mcp_version', 'document', 'revision', 'parameters', 'native_rebuild_state',
            'document_editable', 'document_edit_scope', 'activated', 'source',
            'source_parameters', 'reconciliation')
    result = {key: data[key] for key in keep if key in data}
    if (data.get('memory') or {}).get('warning'):
        result['memory'] = data['memory']
    if (data.get('server') or {}).get('code_changed_since_start'):
        # Worth the space only when it matters: the running process is older than its source.
        result['server'] = data['server']
    result['bodies'] = compact_bodies(data.get('body_state'))
    result.update(_bbox_method(data.get('body_state')))
    mapping = data.get('components')
    if mapping is not None:
        result['component_count'] = len(mapping)
        if components:
            result['components'] = {name: mapping[name] for name in components if name in mapping}
            unknown = [name for name in components if name not in mapping]
            if unknown:
                result['unknown_components'] = unknown
    compatibility = data.get('edit_compatibility')
    if compatibility is not None:
        checked = compatibility.get('inventory_checked')
        # An explicit inventory check is a requested answer; keep it whole.
        result['edit_compatibility'] = compatibility if checked else {'status': compatibility.get('status')}
    result['detail'] = 'summary'
    return result


def compact_images(images):
    return [{'view': item.get('view'), 'path': item.get('path')} for item in images or []]


def compact_working_view(view):
    if not isinstance(view, dict):
        return view
    return {'verified': True} if view.get('verified') else view


def compact_mutation(data):
    """Answer of create/edit: what changed and what the next call needs."""
    mapping = data.get('components') or {}
    keep = ('status', 'document', 'committed_document', 'revision', 'program_hash',
            'edit_strategy', 'updated_curve_count', 'changed_source_lines', 'patch_applied', 'history_depth',
            'rebuild_mode',
            'acceptance', 'timings_ms', 'elapsed_sec', 'activated', 'native_mutation_started')
    result = {key: data[key] for key in keep if key in data}
    parameters = data.get('parameters')
    if parameters is None and data.get('program_parameters'):
        parameters = {name: item['value'] for name, item in data['program_parameters'].items()}
    result['parameters'] = parameters
    result['bodies'] = compact_bodies(data.get('body_state'))
    result.update(_bbox_method(data.get('body_state')))
    created = data.get('status') == 'NATIVE_GEOMETRY_CREATED'
    rebuilt = list(data.get('rebuilt_components') or [])
    added = list(mapping) if created else list(data.get('added_components') or [])
    result.update(rebuilt_components=[] if created else rebuilt, added_components=added,
                  removed_components=list(data.get('removed_components') or []),
                  preserved_count=(0 if created else
                                   len(data['preserved_components']) if 'preserved_components' in data
                                   else len(mapping)),
                  component_count=len(mapping))
    if data.get('added_parameters'):
        result['added_parameters'] = data['added_parameters']
    if data.get('obsolete_features_removed'):
        result['obsolete_features_removed_count'] = len(data['obsolete_features_removed'])
    changed = [name for name in dict.fromkeys(rebuilt + ([] if created else added)) if name in mapping]
    if changed and len(changed) <= MAX_CHANGED_RECORDS:
        result['components'] = {name: mapping[name] for name in changed}
    result['warnings'] = data.get('warnings') or []
    result['images'] = compact_images(data.get('images'))
    result['working_view'] = compact_working_view(data.get('working_view'))
    return result


def compact_analysis(data):
    nodes = data.get('nodes') or []
    counts = collections.Counter(node['kind'] for node in nodes)
    result = {'status': data.get('status'),
              'parameters': {name: item['value'] for name, item in (data.get('parameters') or {}).items()},
              'node_count': len(nodes), 'nodes_by_kind': dict(counts.most_common()),
              'components': [node['id'] for node in nodes],
              'program_hash': data.get('program_hash'), 'stdout': data.get('stdout', '')}
    for key in ('plan', 'changed_source_lines', 'patch_applied', 'document', 'draft',
                'component_count', 'max_components', 'remaining_components', 'expanded_complexity'):
        if key in data:
            result[key] = data[key]
    if 'expanded_complexity' in result:
        from .shape_limits import EXPANSION_WARNING
        estimate = result.pop('expanded_complexity')['estimated_created_body_instances']
        if estimate > EXPANSION_WARNING:
            result['expanded_complexity'] = {'estimated_created_body_instances': estimate}
    if data.get('warnings'):
        result['warnings'] = data['warnings']
    if data.get('images'):
        result['images'] = [{key: item[key] for key in ('view', 'path', 'px_per_mm', 'origin_px') if key in item}
                            for item in data['images']]
    return result


def compact_inspection(data):
    result = dict(data)
    pairs = [dict(pair) for pair in data.get('body_pairs') or []]
    shared = {}
    for key in PAIR_SHARED:
        values = [pair[key] for pair in pairs if key in pair]
        # State a method once only when every pair that reports it agrees.
        if values and all(value == values[0] for value in values):
            shared[key] = values[0]
            for pair in pairs:
                pair.pop(key, None)
    slack = [pair['clearance_check'].get('numeric_slack_mm') for pair in pairs if pair.get('clearance_check')]
    if slack and all(value == slack[0] for value in slack):
        shared['numeric_slack_mm'] = slack[0]
        for pair in pairs:
            if pair.get('clearance_check'):
                pair['clearance_check'] = {k: v for k, v in pair['clearance_check'].items()
                                           if k != 'numeric_slack_mm'}
    for pair in pairs:
        if pair.get('errors') == []:
            del pair['errors']
    if pairs:
        result['body_pairs'] = pairs
        result['body_pair_methods'] = shared
    return rounded(result)


def compact_export(data):
    """Delivery answer: where the files are and what was accepted; protocols stay in the manifest."""
    keep = ('status', 'directory', 'manifest', 'manifest_sha256', 'acceptance', 'document_path', 'revision',
            'source_preserved', 'replaced_previous_delivery', 'timings_ms', 'activated')
    result = {key: data[key] for key in keep if key in data}
    files = []
    for item in data.get('files') or []:
        entry = {key: item[key] for key in ('path', 'kind', 'body', 'size_bytes') if key in item}
        triangles = (item.get('inspection') or {}).get('triangles')
        if triangles is not None:
            entry['triangles'] = triangles
        if item.get('cleanup'):
            entry['mesh_cleanup'] = bool(item['cleanup'].get('repair_performed', True))
        files.append(entry)
    result['files'] = files
    # Not "details": the server reads data.details as the dictionary of a failed call.
    result['note'] = 'manifest.json holds every inspection protocol; verbose=true returns them here'
    return result


def compact_observation(data):
    """Observation answer: the images and the few body facts worth reading beside them."""
    keep = ('document', 'revision', 'directory', 'components', 'bbox_method', 'acceptance', 'activated')
    result = {key: data[key] for key in keep if key in data}
    result['bbox_mm'] = rounded(data.get('bbox_mm'), 1)
    result['bodies'] = [{'name': body.get('name'), 'face_count': body.get('face_count'),
                         'bbox': rounded(body.get('bbox'), 1),
                         **({'surface_types': body['surface_types']} if body.get('surface_types') else {})}
                        for body in data.get('bodies') or []]
    result['images'] = [{key: item[key] for key in ('view', 'path', 'sha256', 'direction', 'up') if key in item}
                        for item in data.get('images') or []]
    if data.get('sections'):
        result['sections'] = rounded(data['sections'], 4)
    return result


def compact_response(name, arguments, result):
    """Apply the compact form of one shape tool to a complete service result."""
    data = result.get('data')
    if not isinstance(data, dict):
        return result
    arguments = arguments or {}
    if name == 'get_model_context' and result.get('success'):
        if arguments.get('detail', 'summary') == 'full':
            return result
        compact = compact_context(data, arguments.get('components'))
    elif name == 'execute_model_code' and result.get('success'):
        if data.get('status') == 'PROGRAM_VALIDATED':
            compact = compact_analysis(data)
        elif data.get('status') in {'NATIVE_GEOMETRY_CREATED', 'NATIVE_GEOMETRY_UPDATED',
                                    'NATIVE_GEOMETRY_REVERTED'}:
            compact = compact_mutation(data)
        else:
            return result
    elif name == 'inspect_model' and result.get('success'):
        compact = compact_inspection(data)
    elif name == 'export_model' and result.get('success'):
        compact = compact_export(data)
    elif name == 'observe_model' and result.get('success'):
        compact = compact_observation(data)
    else:
        return result
    return {**result, 'data': compact}
