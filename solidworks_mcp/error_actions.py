"""Concrete next step for frequent failures: a tool and its arguments.

Pure and COM-free. Every action names a real tool; expert operations are
reached through expert_tools, so the hint can be executed as written.
"""

import uuid

SURFACE_CODES = {'UNSUPPORTED_SURFACE_HOST', 'INVALID_SURFACE_SCOPE', 'INVALID_THICKNESS',
                 'APPROXIMATE_SURFACE_ANCHOR', 'UNSUPPORTED_SURFACE_ANCHOR'}
PATH_CODES = {'DOCUMENT_MISMATCH', 'DOCUMENT_NOT_FOUND', 'DOCUMENT_ACTIVATION_FAILED',
              'NO_ACTIVE_DOCUMENT'}


def action(tool, why, **arguments):
    return {'tool': tool, 'arguments': arguments, 'why': why}


def expert(name, why, **arguments):
    return action('expert_tools', why, action='call', name=name, arguments=arguments)


def recovery_actions(code, tool=None, arguments=None, details=None):
    """Ordered actions for one error code; an empty list when there is no specific advice."""
    arguments, details = arguments or {}, details or {}
    document = arguments.get('document_path')
    # A quarantine error names the journalled operation, not the request it rejected.
    operation = (details.get('unresolved_operation_id') or details.get('operation_id') or
                 (details.get('journal') or {}).get('operation_id'))
    if code in {'PROGRAM_LIMIT', 'DRAFT_COMPLEXITY_LIMIT'}:
        return [action('model_api_lookup', 'Read the active budget. For a component limit, consolidate repetitions '
                       'with pattern or split genuinely independent parts. For draft expansion, reduce nested '
                       'patterns. Do not repeat an unchanged over-budget program.', topic='limits')]
    if code in {'NATIVE_BOOLEAN_FAILED', 'NATIVE_BOOLEAN_DISCONNECTED'} or (
            code == 'NATIVE_RECIPE_FAILED' and details.get('stage') == 'parametric_boolean'):
        return [action('model_api_lookup', 'Inspect input_order, resolved_order and connectivity diagnostics; '
                       'join disconnected groups with real geometry. Put the connecting platform first for '
                       'separate supports; do not change tolerances to hide a gap.', topic='combine'),
                action('get_model_context', 'Read the restored component definitions before correcting the program',
                       detail='full')]
    if code == 'RPC_CONNECTION_LOST':
        return [action('get_executor_status', 'Read process identity and quarantine state before any further CAD call'),
                expert('restart_solidworks', 'After the operation has returned, save recoverable work and restart '
                       'SolidWorks if its RPC connection is unavailable; never retry the failed mutation automatically')]
    if code == 'STALE_REVISION':
        return [action('get_model_context', 'Read data.revision and repeat this call with it as expected_revision')]
    if code == 'IDEMPOTENCY_STATE_CHANGED':
        return [action('get_model_context', 'This exact request ran before and the document has changed since. '
                       'data.revision is the current state; to apply the request to it pass a new idempotency_key')]
    if code in PATH_CODES:
        actions = [action('get_model_context', 'data.document.path is the active document; '
                          'document_path must be an existing saved part')]
        if document:
            actions.append(expert('open_document', 'Opens or activates the part explicitly', filepath=document))
        return actions
    if code == 'OUTPUT_EXISTS':
        if tool == 'export_model':
            return [action('export_model', 'replace=true republishes over an earlier export_model delivery; '
                           'for any other existing directory choose a new directory',
                           **{**{k: arguments[k] for k in ('document_path', 'expected_revision', 'directory')
                                 if k in arguments}, 'replace': True})]
        return [action('execute_model_code', 'create needs an output_path that does not exist yet; '
                       'to change the existing part use mode=edit with its document_path')]
    if code == 'MODAL_DIALOG_BLOCKING':
        return [expert('get_environment_status', 'modal_dialog.window_details lists the blocking dialog and its buttons'),
                expert('recover_environment', 'Resolves a recognised dialog; an unknown dialog must be closed in SolidWorks')]
    if code == 'CAD_SESSION_BUSY' and operation:
        return [action('wait_operation', 'Another operation is running; collect it, then repeat this call',
                       operation_id=operation)]
    if code in {'SESSION_QUARANTINED', 'OUTCOME_UNKNOWN'}:
        actions = [action('get_executor_status', 'journal.operation_id and cause identify the unresolved operation')]
        if details.get('cause') == 'MCP_OWNER_PROCESS_EXITED' or details.get('restart_required'):
            actions.append(expert('restart_solidworks', 'The MCP process that owned the COM call exited: '
                                  'only a verified SolidWorks restart proves that call has stopped'))
        actions.append(action('reconcile_session', 'Inspects native state and lifts the quarantine; '
                              'never replays the operation',
                              **({'operation_id': operation} if operation else {})))
        return actions
    if code == 'NATIVE_REBUILD_FAILED':
        component = details.get('component') or next(
            (f.get('component') for f in details.get('features') or [] if isinstance(f, dict) and f.get('component')), None)
        actions = []
        if component:
            actions.append(action('get_model_context', 'details.features[0] is the feature that failed first; '
                                  'this shows the definition of its component', components=[component]))
        if document:
            actions.append(action('execute_model_code', 'Preview the corrected program without touching the part: '
                                  'it returns the edit plan, the draft and the program warnings',
                                  mode='analyze', document_path=document))
        return actions
    if code == 'NATIVE_REBUILD_STALE':
        # Seen once as a transient SolidWorks state; an identical failed edit is replayed unless the key is new.
        repeat = {k: arguments[k] for k in ('mode', 'document_path', 'expected_revision') if k in arguments}
        repeat['idempotency_key'] = 'retry-' + uuid.uuid4().hex[:12]
        return [action('execute_model_code', 'SolidWorks answered two rebuilds without regenerating the curves of '
                       'this part; the saved part was restored. Repeat the edit once with this new key; if it '
                       'fails again run expert_tools restart_solidworks and repeat', **repeat)]
    if code == 'NATIVE_PATH_FAILED':
        # Seen once as a transient SolidWorks state: the same request built on the next call.
        repeat = {k: arguments[k] for k in ('mode', 'output_path', 'document_path', 'expected_revision')
                  if k in arguments}
        if arguments.get('mode') != 'create':
            # An identical failed edit is replayed from its receipt; a new key makes it a new attempt.
            repeat['idempotency_key'] = 'retry-' + uuid.uuid4().hex[:12]
        where = (' of ' + details['component']) if isinstance(details.get('component'), str) else ''
        return [action('execute_model_code', 'SolidWorks rejected a path segment' + where + ' in both parameter '
                       'directions. Repeat the call once with the same code; if it fails again, move one point '
                       'of that tube by 0.01 mm', **repeat)]
    if code == 'NATIVE_BODY_INVALID':
        faults = details.get('faults') or {}
        names = [n for n in faults.get('components') or [] if isinstance(n, str)]
        return [action('get_model_context', 'These components own the faulty faces; rebuild them differently '
                       '(a loft, or a Boolean with nearly coincident faces) or revert the last edit',
                       **({'components': names} if names else {}))]
    if code == 'COMPONENT_CONSUMED':
        names = [n for n in (details.get('component'), details.get('consumed_by')) if isinstance(n, str)]
        why = ('A consumed component has no body; address the component that consumed it'
               + (f": {details['consumed_by']}" if details.get('consumed_by') else ' (consumed_by in its record)'))
        return [action('get_model_context', why, **({'components': names} if names else {'detail': 'full'}))]
    if code in SURFACE_CODES or details.get('stage') == 'native_surface_patch':
        return [action('model_api_lookup', 'Host, mask, offset sign and the one-connected-patch rule, with a worked example',
                       topic='surface')]
    if code in {'PATCH_NOT_FOUND', 'PATCH_NOT_UNIQUE'}:
        return [action('get_model_context', 'data.source is the embedded program the patch must match exactly',
                       include_source=True)]
    if code in {'PLAN_SOURCE_UNAVAILABLE', 'PLAN_SOURCE_STALE'}:
        return [action('execute_model_code', 'The edit itself reports rebuilt/added/removed components',
                       mode='edit')]
    return []


def attach_recovery(tool, arguments, result):
    """Put the concrete actions first in a failed result; successful results are untouched."""
    if not isinstance(result, dict) or result.get('success'):
        return result
    data = result.get('data')
    if not isinstance(data, dict):
        data = result['data'] = {}
    error = data.get('error') if isinstance(data.get('error'), dict) else None
    code = (error or {}).get('code') or result.get('error_name')
    details = {**data, **((error or {}).get('details') or {})}
    actions = recovery_actions(code, tool, arguments, details)
    if not actions:
        return result
    target = error if error is not None else data
    previous = [item for item in target.get('recommended_actions') or [] if isinstance(item, dict)]
    target['recommended_actions'] = actions + [item for item in previous if item not in actions]
    return result
