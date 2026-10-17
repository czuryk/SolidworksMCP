"""One STA owner, bounded waiting, and durable unknown-outcome quarantine.

A caller waits for a bounded time. Expiry or cancellation of that wait never
cancels a COM invocation and never changes its outcome: the operation stays
RUNNING and its result is collected later by operation_id. The owner retains
the OS lease until the callback actually returns. The journal blocks the
session only for a really unknown outcome: the owner process died, a native
client was killed, a UI watchdog gave up, or a failed mutation could not
confirm its rollback.
"""

import asyncio
import copy
import hashlib
import json
import math
import os
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field

from .runtime import atomic_json_write
from .progress import update_progress, progress_snapshot
from .operation_results import OperationResults
from .session_identity import OWN_PROCESS, process_alive, solidworks_processes
from .session_lease import (SessionLease, SessionBusy, SessionQuarantined,
                            UNRESOLVED, read_session_state)


DEFAULT_SYNC_WAIT_SEC = 45.0


def sync_wait_limit():
    """Longest time one tool call may block; MCP clients cut a request at 60 s."""
    try:
        value = float(os.environ.get('SOLIDWORKS_MCP_SYNC_WAIT_SEC', DEFAULT_SYNC_WAIT_SEC))
    except ValueError:
        return DEFAULT_SYNC_WAIT_SEC
    return value if math.isfinite(value) and value > 0 else DEFAULT_SYNC_WAIT_SEC


def failure(code, message, **data):
    return {'success': False, 'message': message, 'error_code': -1,
            'error_name': code, 'data': data}


def summary(result):
    data = result.get('data') or {}
    return {'success': bool(result.get('success')), 'error_name': (data.get('error') or {}).get('code') or result.get('error_name'),
            'message': str(result.get('message', ''))[:300],
            'document': data.get('document'), 'revision': data.get('revision'),
            'status': data.get('status'), 'output_path': data.get('output_path')}


def rollback_unconfirmed(result):
    """A failed mutation that started native work and could not prove its rollback."""
    if result.get('success'):
        return False
    error = (result.get('data') or {}).get('error') or {}
    started = (error.get('details') or {}).get('native_mutation_started')
    return error.get('document_restored') is False and started is True


def snapshot(op):
    return {'status': op.status, 'operation_id': op.id, 'tool': op.name,
            'elapsed_sec': round(time.time() - op.created_at, 1), 'stage': op.stage,
            'progress': progress_snapshot(op.progress), 'next': 'wait_operation'}


def running(op):
    """Not an error: the caller stopped waiting, the operation did not stop."""
    return {'success': True, 'error_code': 0, 'error_name': 'swSuccess',
            'message': f'{op.name} is still running; call wait_operation with this operation_id',
            'data': snapshot(op)}


def busy(message, op=None):
    error = SessionBusy(message)
    error.details = {**snapshot(op), 'request_executed': False} if op else {}
    return error


@dataclass
class Operation:
    name: str
    arguments: dict
    callback: object
    deadline: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = 'QUEUED'
    created_at: float = field(default_factory=time.time)
    response: Future = field(default_factory=Future)
    result: object = None
    record: dict = field(default_factory=dict)
    recovery_id: str = ''
    stop_event: object = field(default_factory=threading.Event)
    stage: str = 'queued'
    progress: dict = field(default_factory=dict)
    # No caller is waiting any more; the result is collected by operation_id.
    detached: bool = False
    # Recovery tooling: may run in a quarantined session and never replaces its journal.
    maintenance: bool = False


class StaExecutor:
    def __init__(self, lease_path=None, *, identity_provider=solidworks_processes,
                 initialize=None, finalize=None, pump=None, max_pending=8):
        self.lease_path = SessionLease(lease_path).path
        self.state_path = SessionLease(lease_path).state_path
        self.results = OperationResults(self.lease_path.parent / 'request-receipts.sqlite3')
        self.identity_provider = identity_provider
        self.initialize = initialize or self._initialize_com
        self.finalize = finalize or self._finalize_com
        self.pump = pump or self._pump_com
        self.max_pending = max_pending
        self._queue = queue.Queue()
        self._lock = threading.RLock()
        self._jobs = {}
        self._active = None
        self._thread = None
        self._startup_error = None
        self._closed = False

    @staticmethod
    def _initialize_com():
        import pythoncom
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)

    @staticmethod
    def _finalize_com():
        import pythoncom
        pythoncom.CoUninitialize()

    @staticmethod
    def _pump_com():
        import pythoncom
        pythoncom.PumpWaitingMessages()

    def _read(self):
        return read_session_state(self.state_path)

    def _write(self, record):
        atomic_json_write(self.state_path, record)

    def submit(self, name, arguments, callback, deadline=1800, *, recovery_id='', detached=False,
               maintenance=False):
        """Queue one operation. deadline is a runtime hint for watchdogs, not a cancellation."""
        deadline = float(deadline)
        if not 0 < deadline <= 1800:
            raise ValueError('The operation runtime hint must be in (0, 1800]')
        with self._lock:
            if self._closed:
                raise RuntimeError('Executor is closed')
            if self._active and self._active.status == 'OUTCOME_UNKNOWN':
                raise SessionQuarantined('An abandoned operation has not returned')
            if self._active and self._active.detached:
                raise busy('Another operation is still running; collect it with wait_operation '
                           'before the next CAD call', self._active)
            if sum(j.status == 'QUEUED' for j in self._jobs.values()) >= self.max_pending:
                raise SessionBusy('CAD queue is full')
            op = Operation(name, copy.deepcopy(arguments), callback, deadline,
                           recovery_id=recovery_id, detached=detached, maintenance=maintenance)
            # Retain bounded recent results, never discard pending work.
            for key in list(self._jobs):
                if len(self._jobs) < 64:
                    break
                if self._jobs[key].status not in {'QUEUED', 'RUNNING', 'OUTCOME_UNKNOWN'}:
                    del self._jobs[key]
            self._jobs[op.id] = op
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='SolidWorks-STA', daemon=True)
                self._thread.start()
            self._queue.put(op)
            return op

    async def wait(self, op, wait_sec=None, *, withdraw_queued=True):
        """Wait a bounded time for the result; a started operation is never affected."""
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()

        def notify(_):
            try:
                loop.call_soon_threadsafe(finished.set)
            except RuntimeError:
                pass  # The waiting loop is already closed.
        op.response.add_done_callback(notify)
        try:
            await asyncio.wait_for(finished.wait(), wait_sec)
        except asyncio.TimeoutError:
            return self.release_waiter(op, withdraw_queued)
        except asyncio.CancelledError:
            self.release_waiter(op, withdraw_queued)
            raise
        return op.response.result()

    def release_waiter(self, op, withdraw_queued=True):
        """The caller stops waiting: withdraw a request that never started, keep a started one."""
        with self._lock:
            if op.response.done():
                return op.response.result()
            if op.status == 'QUEUED' and withdraw_queued:
                op.status = 'NOT_EXECUTED'
                op.stop_event.set()
                blocker = self._active if self._active is not op else None
                op.result = failure(
                    'CAD_SESSION_BUSY', 'Request was not executed: another operation is still running. '
                    'Collect it with wait_operation, then repeat this call',
                    **({**snapshot(blocker), 'request_executed': False} if blocker
                       else {'request_executed': False}))
                op.response.set_result(op.result)
                return op.result
            op.detached = True
            return running(op)

    def find(self, operation_id):
        with self._lock:
            return self._jobs.get(operation_id)

    def report_stage(self, stage):
        """Publish the current phase of the running operation; safe from any thread."""
        with self._lock:
            if self._active is not None:
                event = stage if isinstance(stage, dict) else {'stage': str(stage)}
                self._active.progress = update_progress(self._active.progress, event)
                self._active.stage = self._active.progress['phase'][:80]

    def operation_snapshot(self, op):
        with self._lock:
            return snapshot(op)

    def annotate(self, **facts):
        """Add recovery facts of the running operation to the durable journal."""
        with self._lock:
            op = self._active
            if op is None or op.status != 'RUNNING' or not op.record:
                return False
            op.record.update(facts)
            self._write(op.record)
            return True

    def abandon(self, operation_id, reason):
        with self._lock:
            op = self._jobs[operation_id]
            if op.status not in {'QUEUED', 'RUNNING'}:
                return
            op.stop_event.set()
            if op.status == 'QUEUED':
                op.status = 'NOT_EXECUTED'
                result = failure('NOT_EXECUTED', 'Request stopped before CAD execution',
                                 operation_id=op.id, reason=reason)
            else:
                op.status = 'OUTCOME_UNKNOWN'
                op.record.update(status=op.status, reason=reason, quarantined_at=time.time())
                try:
                    self._write(op.record)
                except Exception as exc:
                    op.record['journal_error'] = str(exc)
                result = failure('OUTCOME_UNKNOWN', 'COM may still be running; do not retry',
                                 operation_id=op.id, reason=reason, worker_still_running=True,
                                 recovery='get_executor_status, then reconcile_session after return')
                op.result = result
            if not op.response.done():
                op.response.set_result(result)

    def current_operation(self):
        """Only the owning STA may bind a native watchdog to its operation."""
        with self._lock:
            if self._thread is None or threading.get_ident() != self._thread.ident:
                raise RuntimeError('Current operation is accessible only on the STA owner')
            return self._active

    def _owner_alive(self, journal):
        """Whether the process that journalled a RUNNING operation still owns it."""
        pid, created = journal.get('owner_pid'), journal.get('owner_started_at')
        if pid == OWN_PROCESS['pid'] and created == OWN_PROCESS['created_at']:
            return bool(self._active and self._active.status == 'RUNNING' and
                        journal.get('operation_id') == self._active.id)
        # Records without a start time cannot rule out a reused PID.
        return process_alive(pid, created)

    def status(self, operation_id=None, *, include_result=False):
        with self._lock:
            try:
                journal = self._read()
            except SessionQuarantined as exc:
                journal = {'status': 'JOURNAL_UNREADABLE', 'error': str(exc)}
            state = journal.get('status')
            owner_alive = self._owner_alive(journal) if state == 'RUNNING' else None
            # RUNNING with a live owner is ordinary work; with a dead owner it is an orphan.
            working = bool(state == 'RUNNING' and owner_alive)
            result = {'journal': journal, 'worker_busy': self._active is not None,
                      'worker_scope': 'this_MCP_process',
                      'sta_thread_id': self._thread.ident if self._thread else None,
                      'pending_count': sum(j.status == 'QUEUED' for j in self._jobs.values()),
                      'busy': working,
                      'quarantined': state in UNRESOLVED | {'JOURNAL_UNREADABLE'} and not working}
            if state == 'RUNNING':
                result['owner_alive'] = owner_alive
                if not owner_alive:
                    result['cause'] = 'MCP_OWNER_PROCESS_EXITED'
                    result['recovery'] = {
                        'tool': 'restart_solidworks', 'via': 'expert_tools',
                        'why': 'The MCP process that owned the COM call exited before it returned',
                        'then': {'tool': 'reconcile_session',
                                 'arguments': {'operation_id': journal.get('operation_id')}}}
            if self._active is not None:
                result['active'] = snapshot(self._active)
            if operation_id:
                op = self._jobs.get(operation_id)
                if op:
                    result['operation'] = {**snapshot(op), 'result_available': op.result is not None,
                                           'result_summary': summary(op.result) if op.result else None}
                    if op.result is not None:
                        del result['operation']['next']
                        if journal.get('operation_id') == op.id:
                            # Same operation: state its summary once.
                            result['journal'] = {k: v for k, v in journal.items() if k != 'result_summary'}
                    if include_result:
                        result['operation']['result'] = op.result
                elif journal.get('operation_id') == operation_id:
                    result['operation'] = {**journal, 'result_available': False}
                else:
                    result['operation'] = None
            return copy.deepcopy(result)

    def list_jobs(self):
        with self._lock:
            return [{'job_id': j.id, 'tool': j.name, 'status': j.status}
                    for j in self._jobs.values()]

    def _recovery_allowed(self, previous, op):
        if previous.get('operation_id') != op.recovery_id:
            raise SessionQuarantined('Recovery operation_id does not match the session journal')
        if (previous.get('status') in {'RETURNED_LATE', 'ROLLBACK_UNCONFIRMED'} and
                not previous.get('native_outcome_unknown')):
            # The callback returned in a live owner: inspecting native state is enough.
            return False
        if previous.get('status') not in UNRESOLVED:
            raise SessionQuarantined('This session does not require reconciliation')
        current = self.identity_provider()
        before = previous.get('solidworks_processes')
        boundary = previous.get('quarantined_at', previous.get('started_at', time.time()))
        if not before and not previous.get('quarantined_at'):
            # The original call may itself have launched the current SW process.
            previous.update(quarantined_at=time.time(), native_outcome_unknown=True)
            self._write(previous)
            raise SessionQuarantined('No pre-call SW identity; restart SolidWorks after this recovery check')
        # A restarted MCP is not evidence that the out-of-process COM server stopped.
        if (before is None or not current or
                any(p in current for p in before) or
                any(p['created_at'] <= boundary for p in current)):
            raise SessionQuarantined(
                'The COM call of this operation was never seen to return, so only the end of that SolidWorks '
                'process proves it stopped: call expert_tools restart_solidworks, then reconcile_session again')
        return True

    def _execute(self, op):
        with SessionLease(self.lease_path, recovery=bool(op.recovery_id) or op.maintenance):
            with self._lock:
                if op.status != 'QUEUED':
                    return
                previous = self._read()
                restarted = self._recovery_allowed(previous, op) if op.recovery_id else False
                # The unresolved record must survive maintenance: reconcile_session verifies against it.
                keep_journal = op.maintenance and previous.get('status') in UNRESOLVED
                identity = self.identity_provider()
                op.status = 'RUNNING'
                op.stage = 'running'
                self._active = op
                op.record = {'operation_id': op.id, 'tool': op.name, 'status': 'RUNNING',
                             'owner_pid': OWN_PROCESS['pid'],
                             'owner_started_at': OWN_PROCESS['created_at'],
                             'started_at': time.time(),
                             'solidworks_processes': identity,
                             'arguments_sha256': hashlib.sha256(json.dumps(
                                 op.arguments, sort_keys=True, default=str).encode()).hexdigest(),
                             'document_hint': op.arguments.get('document_path') or
                                              op.arguments.get('output_path') or op.arguments.get('filepath')}
                if op.recovery_id:
                    op.record['reconciles'] = op.recovery_id
                if keep_journal:
                    op.record = {}
                else:
                    self._write(op.record)
            try:
                result = op.callback(restarted) if op.recovery_id else op.callback()
                if not isinstance(result, dict):
                    raise TypeError('Executor callbacks must return a plain result dictionary')
            except BaseException as exc:
                result = failure('EXECUTOR_CALLBACK_FAILED', str(exc))
            with self._lock:
                unknown_native = bool((result.get('data') or {}).get('native_outcome_unknown'))
                if unknown_native:
                    op.status = 'OUTCOME_UNKNOWN'
                    op.record.update(native_outcome_unknown=True, quarantined_at=time.time())
                    result = failure('OUTCOME_UNKNOWN', 'Native client ended without proving the COM call stopped',
                                     native_outcome_unknown=True, original_result=result,
                                     recovery='A verified SolidWorks restart is required')
                elif op.status == 'OUTCOME_UNKNOWN' or (op.recovery_id and not result.get('success')):
                    op.status = 'RETURNED_LATE'
                elif rollback_unconfirmed(result):
                    op.status = 'ROLLBACK_UNCONFIRMED'
                    op.record.update(quarantined_at=time.time())
                    result.setdefault('data', {})['recovery'] = {
                        'tool': 'reconcile_session', 'arguments': {'operation_id': op.id},
                        'reason': 'The failed mutation did not confirm its rollback'}
                else:
                    # A returned callback is a known outcome, however long it took.
                    op.status = 'RECONCILED' if op.recovery_id else 'COMPLETED'
                op.stage = 'finished'
                if not keep_journal:
                    op.record.update(status=op.status, returned_at=time.time(), result_summary=summary(result))
                    self._write(op.record)
                op.result = result
                result.setdefault('data', {})['executor'] = {
                    'operation_id': op.id, 'status': op.status,
                    'progress': progress_snapshot(op.progress),
                    'sta_thread_id': threading.get_ident()}
                if not result.get('success'):
                    result['data'].setdefault('operation_id', op.id)
                    result['data'].setdefault('stage', op.progress.get('phase', 'running'))
            # Disk I/O and asset copies must not hold the status/progress lock.
            persistence_error = None
            try:
                self.results.save(op, result)
            except Exception as exc:
                persistence_error = {
                    'saved': False, 'error_name': getattr(exc, 'code', 'OPERATION_RESULT_NOT_PERSISTED'),
                    'message': str(exc)}
            with self._lock:
                if persistence_error:
                    result['data']['result_persistence'] = {
                        **persistence_error}
                if not op.response.done():
                    op.response.set_result(result)

    def _run(self):
        initialized = False
        try:
            try:
                self.initialize()
                initialized = True
            except Exception as exc:
                self._startup_error = str(exc)
            while True:
                try:
                    op = self._queue.get(timeout=.05)
                except queue.Empty:
                    if initialized:
                        self.pump()
                    continue
                if op is None:
                    break
                try:
                    if self._startup_error:
                        raise RuntimeError(self._startup_error)
                    self._execute(op)
                except Exception as exc:
                    with self._lock:
                        code = ('SESSION_QUARANTINED' if isinstance(exc, SessionQuarantined)
                                else 'CAD_SESSION_BUSY' if isinstance(exc, SessionBusy)
                                else 'EXECUTOR_FAILED')
                        started = op.status in {'RUNNING', 'OUTCOME_UNKNOWN'}
                        op.status = 'OUTCOME_UNKNOWN' if started else 'NOT_EXECUTED'
                        if started:
                            # Never leave RUNNING behind: a live owner would read as busy forever.
                            op.record.update(status=op.status, reason='EXECUTOR_FAILED',
                                             quarantined_at=time.time())
                            try:
                                self._write(op.record)
                            except Exception:
                                pass
                        op.result = failure(code, str(exc), **{
                            **getattr(exc, 'details', {}), 'operation_id': op.id, 'status': op.status})
                        if not op.response.done():
                            op.response.set_result(op.result)
                finally:
                    with self._lock:
                        if self._active is op:
                            self._active = None
                    self._queue.task_done()
                if initialized:
                    self.pump()
        finally:
            if initialized:
                self.finalize()

    def close(self):
        with self._lock:
            self._closed = True
            for op in self._jobs.values():
                self.abandon(op.id, 'EXECUTOR_SHUTDOWN')
            self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=1)
