"""Optional read-only inventory evidence; no automatic ownership migration."""
from .shape_parametric import feature_inventory


def edit_compatibility(automation, doc, record, check=False):
    native = record['native']
    topology_available = bool(native.get('ownership') and native.get('feature_inventory'))
    result = {'status':'NOT_CHECKED', 'inventory_checked':False,
              'definition_edit':'CONDITIONAL',
              'topology_edit':'CONDITIONAL' if topology_available else 'UNSUPPORTED',
              'scope':'feature_names_and_types',
              'note':'Execution still validates references, definitions, rebuild and revision.'}
    if not check:
        return result
    expected = native.get('feature_inventory')
    if expected is None:
        result.update(status='MAPPING_UNAVAILABLE')
        return result
    try:
        actual = feature_inventory(automation, doc)
        added = {k:v for k,v in actual.items() if k not in expected}
        removed = {k:v for k,v in expected.items() if k not in actual}
        changed = {k:{'expected':expected[k], 'actual':actual[k]}
                   for k in expected.keys() & actual.keys() if expected[k] != actual[k]}
        matching = not (added or removed or changed)
        result.update(status='INVENTORY_MATCH' if matching else 'EXTERNAL_FEATURE_CHANGE',
                      inventory_checked=True, added=added, removed=removed, type_changes=changed)
        if not matching:
            result['topology_edit'] = 'BLOCKED'
    except Exception as exc:
        result.update(status='UNKNOWN', error=str(exc))
    return result
