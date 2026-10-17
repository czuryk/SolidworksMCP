"""Document-bound idempotency at the public MCP mutation boundary."""

import copy
import hashlib
import os
import time
from pathlib import Path

from .com_utils import com_get
from .request_journal import RequestConflict, RequestJournal, digest
from .shape_observation import (canonical_path, geometry_state, document_revision,
                                activate_target)
from ..shape_program import ProgramError
from .shape_parametric import read_record
from .sta_executor import failure


def request_spec(name, arguments):
    args = copy.deepcopy(arguments)
    key = args.pop('idempotency_key', None)
    if name == 'export_model' and key is not None:
        raise RequestConflict('INVALID_ARGUMENT','export_model protects a new directory; native-only receipt replay is not valid for external files')
    shape = name == 'execute_model_code' and args.get('mode', 'analyze') in {'create','edit','revert'}
    if name == 'execute_cad_plan':
        key = key or args.get('plan_id')
    if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > 256):
        raise RequestConflict('INVALID_ARGUMENT', 'idempotency_key must contain 1–256 characters')
    if key is None and not shape:
        return None
    creating = shape and args.get('mode') == 'create'
    target = args.get('output_path') if creating else args.get('document_path')
    if not target or not Path(target).is_absolute():
        raise RequestConflict('DOCUMENT_TARGET_REQUIRED', 'Idempotent mutations require an absolute document_path (output_path for create)')
    if not creating and not args.get('expected_revision'):
        raise RequestConflict('REVISION_REQUIRED', 'Idempotent mutations require expected_revision from get_model_context')
    document = canonical_path(target)
    args['output_path' if creating else 'document_path'] = document
    # Observation is a response preference. It must not change mutation identity.
    args.pop('observe', None)
    request = {'tool':name, 'arguments':args, 'document':document}
    # document identifies the target in keys and receipts; target is the spelling a document is opened under.
    return {'key':key or 'auto:'+digest(request), 'request':request,
            'document':document, 'target':os.path.abspath(target), 'creating':creating}


def create_left_nothing(prior, spec):
    """A finished create whose output file does not exist: its repeat is a new attempt, not a duplicate write.

    Either it failed before saving, or the part it saved has been deleted since.
    """
    return spec['creating'] and prior['status'] != 'RUNNING' and not Path(spec['document']).exists()


def outcome_undone(prior, current):
    """The document is back at the exact saved state a completed request started from.

    That is what mode="revert" leaves behind. The effect of the request is gone, so the
    same request is a new one: replaying its receipt would refuse a legitimate edit.
    """
    return (prior['status'] == 'COMPLETED' and current is not None and prior.get('before') is not None
            and current == prior['before'] and current != prior.get('after'))


def native_state(automation, target):
    """Revision and saved-state fingerprint of the target, opened under the spelling the caller used.

    Opening it under the normalised lower-case path would make SolidWorks report
    the document under that spelling for the rest of the session.
    """
    try:
        activate_target(automation, target)
    except ProgramError as exc:
        raise RequestConflict(exc.code, str(exc)) from exc
    doc, error = automation.get_active_doc()
    if error:
        raise RequestConflict('NO_ACTIVE_DOCUMENT', error.get('message','No active document'))
    if canonical_path(automation._get_doc_path(doc)) != canonical_path(target):
        raise RequestConflict('DOCUMENT_MISMATCH', 'The active document is not the requested target')
    if int(com_get(doc, 'GetType')) != 1:
        raise RequestConflict('PART_REQUIRED', 'Durable mutation receipts currently support saved native parts')
    path = Path(target)
    if not path.is_file():
        raise RequestConflict('DOCUMENT_UNSAVED', 'Durable mutation receipts require a saved part')
    record = read_record(doc)
    state = geometry_state(doc)
    revision = document_revision(doc, record)
    dirty = bool(com_get(doc, 'GetSaveFlag'))
    # Persisted bytes + clean native state survive SW/MCP restart. A dirty part
    # cannot prove that the saved bytes represent its live geometry.
    fingerprint = None if dirty else {
        'document':canonical_path(target), 'configuration':state['configuration'],
        'model_id':record.get('model_id') if record else None,
        'saved_file_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return revision, fingerprint


def execute_request(automation, name, arguments, callback, journal=None):
    receipt = None
    result = None
    started = time.perf_counter()
    try:
        spec = request_spec(name, arguments)
        if spec is None:
            return callback()
        # A receipt miss/replay may need native reads. Check UI before those reads.
        blocked = automation.ui_preflight(name, arguments, mutating=True) if automation else None
        if blocked:
            return blocked
        journal = journal or RequestJournal()
        prior = journal.lookup(spec['key'], spec['request'])
        retried = state = None
        if prior and create_left_nothing(prior, spec):
            retried, prior = prior, None
        if prior:
            if prior['status'] == 'RUNNING':
                return journal.replay(prior, None)
            state = native_state(automation, spec['target'])
            if not outcome_undone(prior, state[1]):
                result = journal.replay(prior, state[1])
                return result
            retried, prior = prior, None
        if spec['creating']:
            if Path(spec['document']).exists():
                raise RequestConflict('OUTPUT_EXISTS', 'Create requires a new path without a previous matching receipt')
            before = None
        else:
            revision, before = state or native_state(automation, spec['target'])
            if revision != arguments.get('expected_revision'):
                raise RequestConflict('STALE_REVISION', 'The requested starting revision changed')
            if before is None:
                raise RequestConflict('DOCUMENT_DIRTY', 'Save native manual changes before an idempotent mutation')
        receipt = journal.begin(spec['key'], spec['request'], spec['document'], before, previous=retried)
        receipt_sec = time.perf_counter() - started
        result = callback()
        if (result.get('data') or {}).get('native_outcome_unknown'):
            return result
        started = time.perf_counter()
        try:
            _, after = native_state(automation, spec['target'])
        except Exception:
            after = None
        payload = result.get('data') or {}
        details = (payload.get('error') or {}).get('details') or {}
        mutation_started = payload.get('native_mutation_started', details.get('native_mutation_started'))
        source_preserved = (before == after) if before is not None and after is not None else None
        journal.finish(receipt, result, after)
        timings = (result.get('data') or {}).get('timings_ms')
        if isinstance(timings, dict):
            # Document-bound receipts hash the saved file before and after the callback.
            spent = round((receipt_sec + time.perf_counter() - started) * 1000)
            timings['receipt'] = spent
            timings['total'] = timings.get('total', 0) + spent
        result.setdefault('data', {})['request_receipt'] = {
            'key':spec['key'], 'replayed':False,
            # cad_executed is retained as the legacy callback-entry flag.
            'cad_executed':True, 'callback_invoked':True,
            'native_mutation_started':mutation_started, 'source_preserved':source_preserved,
            'status':'COMPLETED' if result.get('success') else 'FAILED',
            'post_state_verified':after is not None,
            **({'attempt':receipt['attempt']} if receipt.get('attempt') else {})}
        return result
    except RequestConflict as exc:
        return failure(exc.code, str(exc), **exc.details)
    except Exception as exc:
        # If execution has begun, never turn a journal write failure into a
        # safe-to-retry ordinary error. The RUNNING receipt remains durable.
        return failure('REQUEST_OUTCOME_UNKNOWN' if receipt else 'REQUEST_JOURNAL_FAILED',
                       str(exc), original_result=result,
                       request_key=receipt['key'] if receipt else None,
                       retry_allowed=False)
