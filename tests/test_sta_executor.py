import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from solidworks_mcp.automation.sta_executor import StaExecutor
from solidworks_mcp.automation.session_lease import SessionLease, SessionBusy, SessionQuarantined
from solidworks_mcp.automation.runtime import atomic_json_write
from solidworks_mcp.automation.session_identity import process_created_at
from solidworks_mcp import server


def ok(**data):
    return {'success': True, 'message': 'test', 'error_code': 0,
            'error_name': 'swSuccess', 'data': data}


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'session.lock'
        self.identities = [{'pid': 123, 'created_at': time.time()-100}]
        self.executors = []
        self.release = threading.Event()
        self.entered = threading.Event()
        self.executor = self.make_executor()

    def make_executor(self):
        executor = StaExecutor(self.path, identity_provider=lambda: self.identities,
                               initialize=lambda: None, finalize=lambda: None, pump=lambda: None)
        self.executors.append(executor)
        return executor

    def tearDown(self):
        self.release.set()
        for executor in self.executors:
            executor.close()
        self.temp.cleanup()

    def blocking(self):
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError('Test did not release the fake native call')
        return ok(document='fixture.SLDPRT')

    def wait_idle(self):
        until = time.monotonic()+2
        while self.executor.status()['worker_busy'] and time.monotonic() < until:
            time.sleep(.005)
        self.assertFalse(self.executor.status()['worker_busy'])

    def test_serial_same_owner_and_reentrant_lease(self):
        calls = []
        def callback():
            with SessionLease(self.path):
                calls.append(threading.get_ident())
            return ok()
        ops = [self.executor.submit('read', {}, callback) for _ in range(5)]
        for op in ops:
            self.assertTrue(op.response.result(2)['success'])
        self.assertEqual(len(set(calls)), 1)
        self.assertNotEqual(calls[0], threading.get_ident())
        self.assertEqual(self.executor.status()['journal']['status'], 'COMPLETED')
        with SessionLease(self.path):
            pass

    def test_slow_operation_reports_running_then_completes_without_quarantine(self):
        op = self.executor.submit('mutate', {}, self.blocking)
        self.assertTrue(self.entered.wait(1))
        reply = asyncio.run(self.executor.wait(op, .07))
        # The wait expired, the operation did not: this is not an error.
        self.assertTrue(reply['success'])
        self.assertEqual(reply['data']['status'], 'RUNNING')
        self.assertEqual(reply['data']['operation_id'], op.id)
        self.assertEqual(reply['data']['next'], 'wait_operation')
        status = self.executor.status(op.id)
        self.assertEqual(status['journal']['status'], 'RUNNING')
        self.assertNotIn('reason', status['journal'])
        self.assertTrue(status['busy'])
        self.assertFalse(status['quarantined'])
        self.assertTrue(status['owner_alive'])
        self.assertFalse(status['operation']['result_available'])
        with self.assertRaises(SessionBusy):
            SessionLease(self.path).acquire()
        # A detached running operation answers busy at once, with its identity.
        with self.assertRaises(SessionBusy) as raised:
            self.executor.submit('read', {}, lambda: self.fail('Ran beside a running operation'))
        self.assertNotIsInstance(raised.exception, SessionQuarantined)
        self.assertEqual(raised.exception.details['operation_id'], op.id)
        self.release.set()
        collected = asyncio.run(self.executor.wait(op, 1, withdraw_queued=False))
        self.assertTrue(collected['success'])
        self.assertEqual(collected['data']['document'], 'fixture.SLDPRT')
        self.wait_idle()
        status = self.executor.status(op.id)
        self.assertEqual(status['journal']['status'], 'COMPLETED')
        self.assertEqual(status['operation']['status'], 'COMPLETED')
        self.assertFalse(status['quarantined'])
        self.assertNotIn('result', status['operation'])
        self.assertTrue(status['operation']['result_summary']['success'])
        # No reconciliation: the next request simply runs.
        with SessionLease(self.path):
            pass
        self.assertTrue(self.executor.submit('new', {}, lambda: ok()).response.result(1)['success'])

    def test_queued_wait_expiry_never_runs_callback_or_disturbs_active_request(self):
        first = self.executor.submit('first', {}, self.blocking)
        self.assertTrue(self.entered.wait(1))
        second = self.executor.submit('second', {}, lambda: self.fail('Queued callback executed'))
        reply = asyncio.run(self.executor.wait(second, .05))
        self.assertEqual(reply['error_name'], 'CAD_SESSION_BUSY')
        self.assertEqual(reply['data']['operation_id'], first.id)
        self.assertFalse(reply['data']['request_executed'])
        self.assertEqual(self.executor.status(second.id)['operation']['status'], 'NOT_EXECUTED')
        self.assertEqual(self.executor.status()['journal']['operation_id'], first.id)
        self.release.set()
        self.assertTrue(first.response.result(1)['success'])
        self.wait_idle()
        self.assertEqual(self.executor.status()['journal']['status'], 'COMPLETED')

    def test_cancelled_wait_leaves_operation_running_and_collectable(self):
        async def scenario():
            op = self.executor.submit('cancel', {}, self.blocking)
            task = asyncio.create_task(self.executor.wait(op))
            await asyncio.to_thread(self.entered.wait, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            status = self.executor.status(op.id)
            self.assertEqual(status['journal']['status'], 'RUNNING')
            self.assertNotIn('reason', status['journal'])
            self.assertEqual(status['operation']['status'], 'RUNNING')
            self.assertTrue(status['worker_busy'])
            self.assertFalse(status['quarantined'])
            self.release.set()
            return op, await self.executor.wait(op, 1, withdraw_queued=False)
        op, collected = asyncio.run(scenario())
        self.assertTrue(collected['success'])
        self.wait_idle()
        self.assertEqual(self.executor.status()['journal']['status'], 'COMPLETED')
        self.assertTrue(self.executor.submit('new', {}, lambda: ok()).response.result(1)['success'])

    def test_abandoned_running_operation_still_requires_reconciliation(self):
        # A UI watchdog (or shutdown) gives up on a running COM call: unknown outcome.
        op = self.executor.submit('mutate', {}, self.blocking)
        self.assertTrue(self.entered.wait(1))
        self.executor.abandon(op.id, 'WATCHDOG_UI_BLOCKED')
        self.assertEqual(op.response.result(1)['error_name'], 'OUTCOME_UNKNOWN')
        self.assertTrue(self.executor.status()['quarantined'])
        with self.assertRaises(SessionQuarantined):
            self.executor.submit('read', {}, lambda: ok())
        self.release.set()
        self.wait_idle()
        self.assertEqual(self.executor.status(op.id)['operation']['status'], 'RETURNED_LATE')
        with self.assertRaises(SessionQuarantined):
            SessionLease(self.path).acquire()
        seen = []
        recovered = self.executor.submit('reconcile_session', {},
            lambda restarted: (seen.append(restarted) or ok()), recovery_id=op.id)
        self.assertTrue(recovered.response.result(1)['success'])
        self.assertEqual(seen, [False])
        self.assertEqual(self.executor.status()['journal']['status'], 'RECONCILED')
        self.assertTrue(self.executor.submit('new', {}, lambda: ok()).response.result(1)['success'])

    def test_failed_mutation_quarantines_only_without_confirmed_rollback(self):
        def failed(restored):
            return {'success': False, 'message': 'edit failed', 'error_name': 'NATIVE_EDIT_FAILED',
                    'data': {'error': {'document_restored': restored,
                                       'details': {'native_mutation_started': True}}}}
        confirmed = self.executor.submit('edit', {}, lambda: failed(True)).response.result(1)
        self.assertFalse(confirmed['success'])
        self.wait_idle()
        self.assertEqual(self.executor.status()['journal']['status'], 'COMPLETED')
        self.assertFalse(self.executor.status()['quarantined'])
        op = self.executor.submit('edit', {}, lambda: failed(False))
        unconfirmed = op.response.result(1)
        self.assertEqual(unconfirmed['data']['recovery']['tool'], 'reconcile_session')
        self.assertEqual(unconfirmed['data']['recovery']['arguments'], {'operation_id': op.id})
        self.wait_idle()
        self.assertEqual(self.executor.status()['journal']['status'], 'ROLLBACK_UNCONFIRMED')
        self.assertTrue(self.executor.status()['quarantined'])
        blocked = self.executor.submit('edit', {}, lambda: self.fail('Unconfirmed rollback bypass'))
        self.assertEqual(blocked.response.result(1)['error_name'], 'SESSION_QUARANTINED')
        recovered = self.executor.submit('reconcile_session', {},
            lambda restarted: ok(restarted=restarted), recovery_id=op.id).response.result(1)
        self.assertFalse(recovered['data']['restarted'])
        self.assertEqual(self.executor.status()['journal']['status'], 'RECONCILED')

    def test_status_separates_busy_owner_from_orphan(self):
        owner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            record = {'status': 'RUNNING', 'operation_id': 'foreign', 'started_at': time.time(),
                      'owner_pid': owner.pid, 'owner_started_at': process_created_at(owner.pid),
                      'solidworks_processes': list(self.identities)}
            atomic_json_write(self.executor.state_path, record)
            status = self.executor.status('foreign')
            self.assertTrue(status['busy'])
            self.assertTrue(status['owner_alive'])
            self.assertFalse(status['quarantined'])
            # The same PID with another start time is a different process.
            atomic_json_write(self.executor.state_path, {**record, 'owner_started_at': 1.0})
            self.assertFalse(self.executor.status()['owner_alive'])
            atomic_json_write(self.executor.state_path, record)
        finally:
            owner.kill()
            owner.wait(5)
        status = self.executor.status('foreign')
        self.assertFalse(status['busy'])
        self.assertFalse(status['owner_alive'])
        self.assertTrue(status['quarantined'])
        self.assertEqual(status['cause'], 'MCP_OWNER_PROCESS_EXITED')

    def test_orphaned_journal_requires_sw_restart_not_mcp_restart(self):
        record = {'status':'RUNNING', 'operation_id':'orphan', 'started_at':time.time(),
                  'solidworks_processes':list(self.identities), 'owner_pid':999999}
        atomic_json_write(self.executor.state_path, record)
        op = self.executor.submit('write', {}, lambda: self.fail('Orphan bypass'))
        self.assertEqual(op.response.result(1)['error_name'], 'SESSION_QUARANTINED')
        recovery = self.executor.submit('reconcile', {}, lambda _: self.fail('Unsafe recovery'), recovery_id='orphan')
        self.assertEqual(recovery.response.result(1)['error_name'], 'SESSION_QUARANTINED')
        self.identities = [{'pid':124, 'created_at':time.time()+1}]
        recovery = self.executor.submit('reconcile', {}, lambda restarted: ok(restarted=restarted), recovery_id='orphan')
        self.assertTrue(recovery.response.result(1)['data']['restarted'])

    def test_late_return_survives_executor_restart_without_replaying(self):
        atomic_json_write(self.executor.state_path, {'status':'RETURNED_LATE', 'operation_id':'late',
            'started_at':time.time(), 'solidworks_processes':list(self.identities)})
        executor = self.make_executor()
        result = executor.submit('reconcile', {}, lambda restarted: ok(restarted=restarted), recovery_id='late').response.result(1)
        self.assertTrue(result['success'])
        self.assertFalse(result['data']['restarted'])

    def test_process_launched_by_unknown_call_is_not_a_verified_restart(self):
        atomic_json_write(self.executor.state_path, {'status':'RUNNING', 'operation_id':'launch',
            'started_at':time.time()-5, 'solidworks_processes':[]})
        self.identities = [{'pid':124, 'created_at':time.time()-1}]
        op = self.executor.submit('reconcile', {}, lambda _: self.fail('Launch mistaken for restart'), recovery_id='launch')
        self.assertEqual(op.response.result(1)['error_name'], 'SESSION_QUARANTINED')
        boundary = self.executor.status()['journal']['quarantined_at']
        self.identities = [{'pid':125, 'created_at':boundary+1}]
        result = self.executor.submit('reconcile', {}, lambda restarted: ok(restarted=restarted), recovery_id='launch').response.result(1)
        self.assertTrue(result['data']['restarted'])

    def test_failed_reconciliation_does_not_release_quarantine(self):
        atomic_json_write(self.executor.state_path, {'status':'RETURNED_LATE', 'operation_id':'late'})
        result = self.executor.submit('reconcile', {}, lambda _: {'success':False,'message':'native read failed'}, recovery_id='late').response.result(1)
        self.assertFalse(result['success'])
        self.assertTrue(self.executor.status()['quarantined'])
        op = self.executor.submit('write', {}, lambda: self.fail('Failed reconciliation cleared quarantine'))
        self.assertEqual(op.response.result(1)['error_name'], 'SESSION_QUARANTINED')

    def test_killed_native_child_does_not_become_safe_when_parent_returns(self):
        op = self.executor.submit('capture', {}, lambda: {
            'success':False, 'message':'Killed native client', 'data':{'native_outcome_unknown':True}})
        op.response.result(1)
        self.wait_idle()
        journal = self.executor.status()['journal']
        self.assertEqual(journal['status'], 'OUTCOME_UNKNOWN')
        recovery = self.executor.submit('reconcile', {}, lambda _: self.fail('Native child is unresolved'), recovery_id=op.id)
        self.assertEqual(recovery.response.result(1)['error_name'], 'SESSION_QUARANTINED')

    def test_corrupt_journal_and_wrong_recovery_identity_fail_closed(self):
        self.executor.state_path.write_text('{bad', encoding='utf-8')
        self.assertTrue(self.executor.status()['quarantined'])
        op = self.executor.submit('read', {}, lambda: self.fail('Corrupt journal bypass'))
        self.assertEqual(op.response.result(1)['error_name'], 'SESSION_QUARANTINED')
        atomic_json_write(self.executor.state_path, {'status':'RETURNED_LATE', 'operation_id':'late'})
        op = self.executor.submit('reconcile', {}, lambda _: self.fail('Wrong identity bypass'), recovery_id='wrong')
        self.assertEqual(op.response.result(1)['error_name'], 'SESSION_QUARANTINED')

    def test_other_thread_cannot_reenter_lease(self):
        with SessionLease(self.path):
            op = self.executor.submit('read', {}, lambda: self.fail('Lease bypass'))
            self.assertEqual(op.response.result(1)['error_name'], 'CAD_SESSION_BUSY')

    def test_mcp_status_and_analysis_remain_available_during_background_job(self):
        async def scenario():
            submitted = await server.call_tool('execute_python_async', {'code':'result=1'})
            job_id = submitted.structuredContent['data']['job_id']
            await asyncio.to_thread(self.entered.wait, 1)
            status = await server.call_tool('get_executor_status', {})
            self.assertTrue(status.structuredContent['data']['worker_busy'])
            analysis = await server.call_tool('execute_model_code', {'code':"model.dome('Cap', 10, 5)"})
            self.assertTrue(analysis.structuredContent['success'])
            queued = await server.call_tool('get_document_info', {'execution_timeout_sec':.03})
            self.assertEqual(queued.structuredContent['error_name'], 'CAD_SESSION_BUSY')
            self.assertEqual(queued.structuredContent['data']['operation_id'], job_id)
            waiting = await server.call_tool('wait_operation', {'operation_id':job_id, 'wait_sec':.03})
            self.assertFalse(waiting.isError)
            self.assertEqual(waiting.structuredContent['data']['status'], 'RUNNING')
            self.release.set()
            polled = await server.call_tool('get_job_result', {'job_id':job_id, 'timeout':1})
            self.assertTrue(polled.structuredContent['success'])
            self.assertEqual(polled.structuredContent['data']['operation']['status'], 'COMPLETED')
            self.assertTrue(polled.structuredContent['data']['operation']['result']['success'])
        with patch.object(server, 'cad_executor', self.executor), \
             patch.object(server, '_call_tool_direct', side_effect=lambda *_:self.blocking()):
            asyncio.run(scenario())

    def test_long_tool_call_returns_running_and_wait_operation_delivers_the_result(self):
        async def scenario():
            first = await server.call_tool('get_model_context', {'execution_timeout_sec':.05})
            self.assertFalse(first.isError)
            running = first.structuredContent['data']
            self.assertEqual(running['status'], 'RUNNING')
            self.assertEqual(running['tool'], 'get_model_context')
            self.assertEqual(running['next'], 'wait_operation')
            self.assertEqual(self.executor.status()['journal']['status'], 'RUNNING')
            # CAD tools answer busy with the operation identity; COM-free tools keep working.
            blocked = await server.call_tool('get_model_context', {})
            self.assertEqual(blocked.structuredContent['error_name'], 'CAD_SESSION_BUSY')
            self.assertEqual(blocked.structuredContent['data']['operation_id'], running['operation_id'])
            self.assertEqual(blocked.structuredContent['data']['next'], 'wait_operation')
            lookup = await server.call_tool('model_api_lookup', {'topic':'index'})
            self.assertFalse(lookup.isError)
            still = await server.call_tool('wait_operation', {'operation_id':running['operation_id'], 'wait_sec':.05})
            self.assertEqual(still.structuredContent['data']['status'], 'RUNNING')
            self.release.set()
            done = await server.call_tool('wait_operation', {'operation_id':running['operation_id']})
            self.assertFalse(done.isError)
            self.assertEqual(done.structuredContent['data']['document'], 'fixture.SLDPRT')
            self.assertEqual(done.structuredContent['data']['executor']['status'], 'COMPLETED')
            status = await server.call_tool('get_executor_status', {'operation_id':running['operation_id']})
            self.assertFalse(status.structuredContent['data']['quarantined'])
            self.assertNotIn('result', status.structuredContent['data']['operation'])
            missing = await server.call_tool('wait_operation', {'operation_id':'missing'})
            self.assertEqual(missing.structuredContent['error_name'], 'OPERATION_NOT_FOUND')
        with patch.object(server, 'cad_executor', self.executor), \
             patch.object(server, '_call_tool_direct', side_effect=lambda *_:self.blocking()):
            asyncio.run(scenario())
            # No reconcile_session: the next call with a fresh state just runs.
            follow = asyncio.run(server.call_tool('get_model_context', {}))
        self.assertFalse(follow.isError)
        self.assertNotIn('OUTCOME_UNKNOWN', json.dumps(self.executor.status()))

    def test_no_call_waits_longer_than_the_sync_limit(self):
        self.assertEqual(server._sync_wait(None), server.SYNC_WAIT_SEC)
        self.assertEqual(server._sync_wait(900), server.SYNC_WAIT_SEC)
        self.assertEqual(server._sync_wait(5), 5)
        with self.assertRaises(ValueError):
            server._sync_wait(0)
        self.assertLessEqual(server.SYNC_WAIT_SEC, 55)
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertIn('wait_operation', tools)
        limit = tools['execute_model_code'].inputSchema['properties']['execution_timeout_sec']
        self.assertEqual(limit['maximum'], server.SYNC_WAIT_SEC)
        self.assertEqual(tools['wait_operation'].inputSchema['properties']['wait_sec']['maximum'],
                         server.SYNC_WAIT_SEC)
        self.assertNotIn('execution_timeout_sec', tools['wait_operation'].inputSchema['properties'])

    def test_stage_is_published_for_running_answers(self):
        def staged():
            server.sw_automation._runtime.record_progress('native_update')
            return self.blocking()
        with patch.object(server, 'cad_executor', self.executor):
            op = self.executor.submit('edit', {}, staged)
            self.assertTrue(self.entered.wait(1))
            reply = asyncio.run(self.executor.wait(op, .02))
        self.assertEqual(reply['data']['stage'], 'native_update')

    def test_cancel_queued_request_does_not_run(self):
        first = self.executor.submit('first', {}, self.blocking, 2)
        self.assertTrue(self.entered.wait(1))
        op = self.executor.submit('queued', {}, lambda: self.fail('Cancelled queued request ran'))
        self.executor.abandon(op.id, 'CLIENT_CANCELLED')
        self.assertEqual(op.response.result(1)['error_name'], 'NOT_EXECUTED')
        self.release.set()
        first.response.result(1)

    def test_timeout_argument_removed_before_strict_dispatch(self):
        with patch.object(server, 'cad_executor', self.executor), \
             patch.object(server, '_call_tool_direct', return_value=ok()) as direct:
            result = asyncio.run(server.call_tool('get_model_context', {'execution_timeout_sec':5}))
        self.assertFalse(result.isError)
        direct.assert_called_once_with('get_model_context', {})

    def test_native_child_unknown_skips_postflight_and_com_snapshots(self):
        native_error = server.sw_automation._error('BUDGET_EXCEEDED', 'Native child killed',
            details={'native_outcome_unknown':True})
        with patch.object(server, '_dispatch_two_phase_body_comparison', return_value=native_error), \
             patch.object(server.sw_automation, 'ui_preflight', return_value=None), \
             patch.object(server.sw_automation, '_document_snapshot', side_effect=AssertionError('COM read after kill')), \
             patch.object(server.sw_automation, 'ui_postflight', side_effect=AssertionError('Postflight after kill')):
            result = server._call_tool_direct('compare_body_silhouette_to_image', {})
        self.assertTrue(result['data']['native_outcome_unknown'])

    def test_incomplete_raw_watchdog_identity_is_rejected(self):
        with patch.object(server, 'cad_executor', self.executor):
            result = asyncio.run(server.call_tool('execute_python_async', {
                'code':'result=1', 'ui_guard':{'auto_resolve_known':['dimension_modify']}}))
        self.assertEqual(result.structuredContent['error_name'], 'INVALID_ARGUMENT')
        self.assertEqual(self.executor.list_jobs(), [])

    def test_modal_blocks_before_any_document_snapshot(self):
        blocked = {'success':False,'message':'modal','error_name':'MODAL_DIALOG_BLOCKING'}
        with patch.object(server.sw_automation, 'ui_preflight', return_value=blocked), \
             patch.object(server.sw_automation, '_document_snapshot', side_effect=AssertionError('Snapshot before UI guard')):
            self.assertEqual(server._call_tool_direct('get_model_context',{}),blocked)

    def test_read_only_context_avoids_redundant_document_snapshots(self):
        with patch.object(server.sw_automation, 'ui_preflight', return_value=None), \
             patch.object(server.sw_automation, '_document_snapshot', side_effect=AssertionError('Redundant COM snapshot')), \
             patch.object(server, 'dispatch_new_tool', return_value=ok()):
            self.assertTrue(server._call_tool_direct('get_model_context',{})['success'])


if __name__ == '__main__':
    unittest.main()
