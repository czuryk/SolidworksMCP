"""Discovery-only lifecycle metadata; deprecated calls remain dispatch-compatible."""

DEPRECATION_META_KEY = 'solidworks-mcp/deprecation'
DEPRECATED_TOOLS = {
    'extrude_sketch': {
        'replacements': ['advanced_extrude'],
        'reason': 'Legacy extrusion lacks the guarded feature verification of advanced_extrude.',
        'migration': 'Map the legacy Blind/MidPlane arguments to advanced_extrude; describe its schema first.',
    },
    'cut_extrude': {
        'replacements': ['advanced_cut'],
        'reason': 'Legacy cut lacks explicit multibody scope and guarded verification.',
        'migration': 'Use advanced_cut with explicit scope_bodies for multibody parts; describe its schema first.',
    },
    'compare_sketch_to_image': {
        'replacements': ['compare_sketch_to_reference'],
        'reason': 'Duplicate alias of compare_sketch_to_reference.',
        'migration': 'Same comparison arguments; use compare_sketch_to_reference.',
    },
    'get_job_result': {
        'since': '6.29.4',
        'replacements': ['wait_operation'],
        'reason': 'Alias kept from before every long call answered RUNNING.',
        'migration': 'wait_operation(operation_id=<job_id>, wait_sec=<timeout>) returns the same result.',
    },
    'execute_python_async': {
        'since': '6.29.4',
        'replacements': ['execute_python', 'wait_operation'],
        'reason': 'Every call now answers RUNNING after the synchronous wait, so a separate asynchronous entry is redundant.',
        'migration': 'Call execute_python with the same code; when it answers status RUNNING collect the result '
                     'with wait_operation(operation_id).',
    },
    'list_jobs': {
        'since': '6.29.4',
        'replacements': ['get_executor_status', 'get_session_report'],
        'reason': 'Lists only the jobs of execute_python_async.',
        'migration': 'get_executor_status(operation_id=...) reports one operation; get_session_report summarises the session.',
    },
    'create_semantic_primitive': {
        'replacements': ['create_revolved_body', 'create_swept_member',
                         'create_multibody_insert', 'shell', 'mirror_feature',
                         'linear_pattern', 'circular_pattern',
                         'create_parametric_sketch', 'execute_cad_plan'],
        'reason': 'Generic dispatcher hides the argument contract of existing concrete tools.',
        'migration': {
            'revolved_shell': 'create_revolved_body',
            'tubular_member': 'create_swept_member',
            'clearance_insert|clearance_pocket|cap_and_opening': 'create_multibody_insert',
            'uniform_shell': 'shell',
            'symmetric_pair': 'mirror_feature',
            'linear_feature_array': 'linear_pattern',
            'circular_feature_array': 'circular_pattern',
            'hole_or_rib_array': 'linear_pattern or circular_pattern; remove the dispatcher pattern argument',
            'capsule_profile': 'create_parametric_sketch with two semicircles and two tangent lines; not a drop-in argument rename',
            'printable_hinge': 'execute_cad_plan with the existing explicit operations/invariants and checkpoint+rollback transaction',
        },
    },
}


def annotate_lifecycle(tools):
    """Copy catalog entries so repeat discovery cannot stack prefixes or metadata."""
    result = []
    for tool in tools:
        tool = tool.model_copy(deep=True)
        entry = DEPRECATED_TOOLS.get(tool.name)
        if entry:
            replacements = ', '.join(entry['replacements'])
            tool.description = f'DEPRECATED: use {replacements}. {entry["reason"]} Compatibility calls remain available; describe for migration details.'
            entry = dict(entry)
            tool.meta = {**(tool.meta or {}), DEPRECATION_META_KEY: {
                'deprecated': True, 'catalog_since': entry.pop('since', '6.24.0'),
                'callable': True, **entry}}
        result.append(tool)
    return result


def catalog_summary(tool):
    result = {'name': tool.name, 'description': tool.description.split('.')[0]}
    entry = (tool.meta or {}).get(DEPRECATION_META_KEY)
    if entry:
        result.update(deprecated=True, replacements=entry['replacements'])
    return result
