import asyncio
import json
import tempfile
import unittest
import threading
import time
from pathlib import Path
from unittest.mock import patch, Mock

from solidworks_mcp.automation.request_journal import RequestJournal, RequestConflict
from solidworks_mcp.automation.request_execution import execute_request, request_spec
from solidworks_mcp.automation.sta_executor import StaExecutor
from solidworks_mcp import server


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'part.SLDPRT'
        self.path.write_bytes(b'baseline')
        self.journal = RequestJournal(Path(self.temp.name)/'requests.sqlite3')
        self.args = {'document_path':str(self.path), 'expected_revision':'rev1',
                     'idempotency_key':'key', 'mode':'edit', 'parameters':{'H':44}, 'observe':False}
        self.state = ('rev1',{'file':'baseline'})
        self.calls = 0
        self.patch = patch('solidworks_mcp.automation.request_execution.native_state',side_effect=lambda *_:self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def mutate(self):
        self.calls += 1
        self.state = ('rev2',{'file':'after'})
        return {'success':True, 'message':'saved', 'data':{'revision':'rev2','images':[{'path':'old.png'}]}}

    def run_request(self, args=None, callback=None):
        return execute_request(None, 'execute_model_code', args or self.args, callback or self.mutate, self.journal)

    def test_identical_retry_replays_once_across_journal_instances(self):
        first = self.run_request()
        self.journal = RequestJournal(self.journal.path)
        second = self.run_request()
        self.assertTrue(first['success'] and second['success'])
        self.assertTrue(second['data']['request_receipt']['replayed'])
        self.assertNotIn('images',second['data'])
        self.assertEqual(self.calls,1)

    def test_changed_payload_document_and_revision_are_conflicts(self):
        self.run_request()
        for delta in [{'parameters':{'H':45}}, {'expected_revision':'rev2'},
                      {'document_path':str(self.path.with_name('other.SLDPRT'))}]:
            with self.subTest(delta=delta):
                result = self.run_request({**self.args,**delta})
                self.assertEqual(result['error_name'],'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.calls,1)

    def test_changed_post_state_does_not_reexecute_or_return_old_success(self):
        self.run_request()
        self.state = ('rev3',{'file':'manual-edit'})
        result = self.run_request()
        self.assertEqual(result['error_name'],'IDEMPOTENCY_STATE_CHANGED')
        self.state = ('rev3',None)
        self.assertEqual(self.run_request()['error_name'],'IDEMPOTENCY_STATE_CHANGED')
        self.assertEqual(self.calls,1)

    def test_stale_and_wrong_active_document_do_not_reserve_receipts(self):
        self.state = ('rev3',{'file':'manual-edit'})
        self.assertEqual(self.run_request()['error_name'],'STALE_REVISION')
        self.assertIsNone(self.journal.get('key'))
        with patch('solidworks_mcp.automation.request_execution.native_state',
                   side_effect=RequestConflict('DOCUMENT_MISMATCH','wrong document')):
            self.assertEqual(self.run_request()['error_name'],'DOCUMENT_MISMATCH')
        self.assertEqual(self.calls,0)

    def test_crash_receipt_remains_unresolved_and_never_replays(self):
        def crash():
            self.calls += 1
            raise RuntimeError('Process failed before receipt completion')
        self.assertEqual(self.run_request(callback=crash)['error_name'],'REQUEST_OUTCOME_UNKNOWN')
        self.assertEqual(self.journal.get('key')['status'],'RUNNING')
        self.assertEqual(self.run_request()['error_name'],'REQUEST_OUTCOME_UNKNOWN')
        self.assertEqual(self.calls,1)

    def test_failed_partial_result_never_becomes_successful_retry(self):
        result = self.run_request(callback=lambda:{'success':False,'message':'failed','data':{}})
        replay = self.run_request()
        self.assertFalse(result['success'] or replay['success'])
        self.assertEqual(self.calls,0)
        self.assertTrue(replay['data']['request_receipt']['replayed'])

    def test_preflight_failure_reports_no_native_mutation_with_verified_preservation(self):
        result=self.run_request(callback=lambda:{'success':False,'data':{'error':{
            'details':{'native_mutation_started':False,'source_preserved':True}}}})
        receipt=result['data']['request_receipt']
        self.assertTrue(receipt['callback_invoked'])
        self.assertFalse(receipt['native_mutation_started'])
        self.assertTrue(receipt['source_preserved'])
        self.assertEqual(self.journal.get('key')['status'],'FAILED')

    def test_legacy_unknown_mutation_is_not_reported_as_false(self):
        result=self.run_request()
        self.assertIsNone(result['data']['request_receipt']['native_mutation_started'])
        self.assertFalse(result['data']['request_receipt']['source_preserved'])

    def test_lost_receipt_completion_does_not_claim_safe_failure(self):
        with patch.object(self.journal,'finish',side_effect=OSError('disk failure')):
            result = self.run_request()
        self.assertEqual(result['error_name'],'REQUEST_OUTCOME_UNKNOWN')
        self.assertTrue(result['data']['original_result']['success'])
        self.assertEqual(self.run_request()['error_name'],'REQUEST_OUTCOME_UNKNOWN')
        self.assertEqual(self.calls,1)

    def test_auto_shape_key_is_deterministic_and_observation_is_not_mutation(self):
        args = {k:v for k,v in self.args.items() if k!='idempotency_key'}
        first = request_spec('execute_model_code',args)
        second = request_spec('execute_model_code',{**args,'observe':True})
        self.assertEqual(first,second)
        self.assertTrue(first['key'].startswith('auto:'))
        self.assertNotEqual(first['key'],request_spec('execute_model_code',{**args,'parameters':{'H':55}})['key'])

    def test_create_replay_does_not_create_another_document(self):
        target = self.path.with_name('new.SLDPRT')
        args = {'mode':'create','code':"model.dome('Dome',10,5)",'output_path':str(target)}
        def create():
            target.write_bytes(b'part')
            return self.mutate()
        first = self.run_request(args, create)
        second = self.run_request(args, create)
        self.assertTrue(first['success'] and second['success'])
        self.assertTrue(second['data']['request_receipt']['replayed'])
        self.assertEqual(self.calls,1)
        # The saved part was deleted: nothing is left to duplicate, the same call builds it again.
        target.unlink()
        third = self.run_request(args, create)
        self.assertTrue(third['success'] and target.exists())
        self.assertEqual((self.calls, third['data']['request_receipt']['attempt']), (2, 2))

    def test_request_runs_again_after_its_outcome_was_undone(self):
        first = self.run_request()
        self.assertEqual(self.state, ('rev2',{'file':'after'}))
        # mode="revert" restores the saved bytes the request started from.
        self.state = ('rev1',{'file':'baseline'})
        second = self.run_request()
        self.assertTrue(first['success'] and second['success'])
        self.assertFalse(second['data']['request_receipt']['replayed'])
        self.assertEqual((self.calls, second['data']['request_receipt']['attempt']), (2, 2))
        # Its outcome is in place again: now the repeat is a replay.
        third = self.run_request()
        self.assertTrue(third['data']['request_receipt']['replayed'])
        self.assertEqual(self.calls,2)

    def test_a_failed_request_is_not_run_again_from_its_restored_state(self):
        failed = self.run_request(callback=lambda:{'success':False,'message':'failed','data':{}})
        self.assertFalse(failed['success'])
        # The rollback left the starting state: still a replayed failure, never a silent second attempt.
        replay = self.run_request()
        self.assertTrue(replay['data']['request_receipt']['replayed'])
        self.assertEqual(self.calls,0)

    def failing_create(self, target):
        """A create that fails before saving: no document exists to read a post-state from."""
        args = {'mode':'create','code':"model.dome('Dome',10,5)",'output_path':str(target)}
        def state(_, document):
            if not Path(document).exists():
                raise RequestConflict('DOCUMENT_NOT_FOUND','The requested document does not exist')
            return ('rev1',{'file':Path(document).read_bytes().decode()})
        calls = []
        def fail():
            calls.append('fail')
            return {'success':False,'message':'path failed','data':{'error':{'details':{'native_mutation_started':True}}}}
        def create():
            calls.append('create')
            target.write_bytes(b'part')
            return {'success':True,'message':'created','data':{'revision':'rev1'}}
        return args, state, calls, fail, create

    def test_failed_create_that_left_no_file_runs_again(self):
        target = self.path.with_name('new.SLDPRT')
        args, state, calls, fail, create = self.failing_create(target)
        with patch('solidworks_mcp.automation.request_execution.native_state',side_effect=state):
            first = self.run_request(args, fail)
            self.assertEqual(first['data']['request_receipt']['status'],'FAILED')
            self.assertFalse(first['data']['request_receipt']['post_state_verified'])
            second = self.run_request(args, create)
            self.assertTrue(second['success'])
            self.assertEqual(second['data']['request_receipt']['attempt'],2)
            self.assertFalse(second['data']['request_receipt']['replayed'])
            # The part that now exists is replayed, never built twice.
            third = self.run_request(args, create)
            self.assertTrue(third['data']['request_receipt']['replayed'])
        self.assertEqual(calls,['fail','create'])

    def test_failed_create_is_not_repeated_over_a_file_that_appeared(self):
        target = self.path.with_name('new.SLDPRT')
        args, state, calls, fail, create = self.failing_create(target)
        with patch('solidworks_mcp.automation.request_execution.native_state',side_effect=state):
            self.run_request(args, fail)
            target.write_bytes(b'made by someone else')
            self.assertEqual(self.run_request(args, create)['error_name'],'IDEMPOTENCY_STATE_CHANGED')
        self.assertEqual(calls,['fail'])
        self.assertEqual(target.read_bytes(),b'made by someone else')

    def test_replayed_failure_says_it_was_not_executed_again(self):
        self.run_request(callback=lambda:{'success':False,'message':'Edited document did not rebuild','data':{}})
        replay = self.run_request()
        self.assertTrue(replay['message'].startswith('Not executed again'))
        self.assertIn('idempotency_key',replay['message'])
        self.assertTrue(replay['message'].endswith('Edited document did not rebuild'))
        # A new key is a new attempt.
        again = self.run_request({**self.args,'idempotency_key':'second'})
        self.assertTrue(again['success'])
        self.assertEqual(self.calls,1)

    def test_replayed_success_keeps_its_message(self):
        self.run_request()
        self.assertEqual(self.run_request()['message'],'saved')

    def test_legacy_key_requires_explicit_document_and_revision(self):
        for args in [{'idempotency_key':'k'}, {'idempotency_key':'k','document_path':str(self.path)}]:
            with self.assertRaises(RequestConflict):
                request_spec('run_transaction',args)
        with self.assertRaises(RequestConflict):
            request_spec('execute_cad_plan',{'plan_id':'plan','operations':[]})

    def test_unkeyed_legacy_path_is_not_falsely_cached(self):
        callback = Mock(return_value={'success':True})
        for _ in range(2):
            execute_request(None,'run_transaction',{'name':'test'},callback,self.journal)
        self.assertEqual(callback.call_count,2)

    def test_unknown_native_result_keeps_running_receipt(self):
        result = self.run_request(callback=lambda:{'success':False,'data':{'native_outcome_unknown':True}})
        self.assertFalse(result['success'])
        self.assertEqual(self.journal.get('key')['status'],'RUNNING')

    def test_transport_key_is_not_passed_to_strict_shape_compiler(self):
        with patch.object(server,'execute_request',side_effect=lambda a,n,args,callback:callback()), \
             patch.object(server,'_call_tool_direct',return_value={'success':True}) as direct:
            server._execute_cad_request('execute_model_code',self.args)
        self.assertNotIn('idempotency_key',direct.call_args.args[1])

    def test_journal_corruption_fails_closed(self):
        self.journal.path.write_bytes(b'not sqlite')
        self.assertEqual(self.run_request()['error_name'],'REQUEST_JOURNAL_FAILED')
        self.assertEqual(self.calls,0)

    def test_long_mutation_receipt_replays_without_reconciliation(self):
        executor=StaExecutor(Path(self.temp.name)/'session.lock',identity_provider=lambda:[],
            initialize=lambda:None,finalize=lambda:None,pump=lambda:None)
        release=threading.Event();entered=threading.Event()
        def delayed():
            entered.set();release.wait(2)
            return self.mutate()
        try:
            op=executor.submit('edit',self.args,lambda:self.run_request(callback=delayed))
            self.assertTrue(entered.wait(1))
            # The caller stops waiting; the mutation keeps running and stays collectable.
            self.assertEqual(asyncio.run(executor.wait(op,.08))['data']['status'],'RUNNING')
            release.set()
            self.assertTrue(op.response.result(1)['success'])
            deadline=time.monotonic()+1
            while executor.status()['worker_busy'] and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(executor.status()['journal']['status'],'COMPLETED')
            self.assertEqual(self.journal.get('key')['status'],'COMPLETED')
            replay=executor.submit('retry',self.args,lambda:self.run_request()).response.result(1)
            self.assertTrue(replay['data']['request_receipt']['replayed'])
            self.assertEqual(self.calls,1)
        finally:
            release.set();executor.close()

    def test_empty_or_nonstring_keys_cannot_bypass_validation(self):
        for key in ['',0,[],{},' '*3]:
            with self.assertRaises(RequestConflict):request_spec('run_transaction',{'idempotency_key':key})


if __name__=='__main__': unittest.main()
