"""Historical completion survives a fresh process without changing retry/quarantine authority."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from solidworks_mcp import server
from solidworks_mcp.automation.operation_results import OperationResults, OperationResultError, RETENTION_SEC
from solidworks_mcp.automation.sta_executor import StaExecutor


class ResultHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.executor=StaExecutor(self.root/'session.lock',identity_provider=lambda:[],
                                  initialize=lambda:None,finalize=lambda:None,pump=lambda:None)

    def tearDown(self):
        self.executor.close(); self.directory.cleanup()

    def complete(self,result):
        op=self.executor.submit('export_model',{'document_path':'C:/fixture.SLDPRT'},lambda:result)
        op.response.result(3)
        return op

    def test_success_and_rpc_failure_survive_fresh_process(self):
        for success in (True,False):
            original={'success':success,'error_name':'swSuccess' if success else 'RPC_CONNECTION_LOST',
                      'message':'original detailed message'*40,'data':{'revision':'r1','warnings':[{'code':'TEST'}],
                      'timings_ms':{'total':1234},'error':{'com_hresult':-2147023174}}}
            op=self.complete(original)
            code=('import json,sys; from solidworks_mcp.automation.operation_results import OperationResults; '
                  'print(json.dumps(OperationResults(sys.argv[1]).get(sys.argv[2])))')
            raw=subprocess.check_output([sys.executable,'-c',code,str(self.executor.results.journal.path),op.id],
                                        cwd=Path(__file__).resolve().parents[1],text=True)
            record=json.loads(raw)
            for key in ('success','error_name','message'):
                self.assertEqual(record['result'][key],original[key])
            self.assertEqual(record['result']['data']['revision'],'r1')
            self.assertTrue(record['result']['data']['result_history']['from_journal'])
            self.assertFalse(record['result']['data']['result_history']['cad_executed'])

    def test_wait_after_reconnect_uses_history_for_older_operation(self):
        op=self.complete({'success':False,'error_name':'RPC_CONNECTION_LOST','message':'RPC failed','data':{}})
        self.complete({'success':True,'data':{}})
        fresh=StaExecutor(self.root/'session.lock',initialize=lambda:self.fail('COM initialized'))
        with patch.object(server,'cad_executor',fresh):
            name,result=asyncio.run(server._wait_operation(op.id,0))
            self.assertEqual(name,'export_model')
            self.assertEqual(result['error_name'],'RPC_CONNECTION_LOST')
            self.assertEqual(result['data']['result_history']['status'],'COMPLETED')
            self.assertEqual(asyncio.run(server._wait_operation(op.id,0))[1],result)
        fresh.close()

    def test_unknown_expired_and_corrupt_have_distinct_codes(self):
        store=self.executor.results
        self.assertIsNone(store.get('unknown'))
        op=self.complete({'success':True,'data':{}})
        with store.journal._connect() as db:
            db.execute('UPDATE operation_results SET payload=? WHERE id=?',('broken',op.id))
        with self.assertRaises(OperationResultError) as caught:store.get(op.id)
        self.assertEqual(caught.exception.code,'OPERATION_RESULT_CORRUPT')
        old=self.complete({'success':True,'data':{}})
        with store.journal._connect() as db:
            db.execute('UPDATE operation_results SET finished=? WHERE id=?',(time.time()-RETENTION_SEC-1,old.id))
        with self.assertRaises(OperationResultError) as caught:store.get(old.id)
        self.assertEqual(caught.exception.code,'OPERATION_RESULT_EXPIRED')

    def test_images_are_managed_and_missing_asset_does_not_mask_result(self):
        source=self.root/'view.png'; source.write_bytes(b'fixture pixels')
        op=self.complete({'success':True,'data':{'images':[{'view':'front','path':str(source)}]}})
        source.unlink()
        historical=self.executor.results.get(op.id)['result']
        managed=Path(historical['data']['images'][0]['path'])
        self.assertEqual(managed.read_bytes(),b'fixture pixels')
        managed.unlink()
        historical=self.executor.results.get(op.id)['result']
        self.assertTrue(historical['success'])
        self.assertEqual(historical['data']['result_history']['artifacts'][0]['code'],'OPERATION_ASSET_MISSING')
        self.assertNotIn('path',historical['data']['images'][0])

    def test_unserializable_result_never_repeats_or_quarantines_known_outcome(self):
        op=self.complete({'success':True,'data':{'unsupported':object()}})
        self.assertEqual(op.status,'COMPLETED')
        self.assertFalse(op.result['data']['result_persistence']['saved'])
        self.assertFalse(self.executor.status()['quarantined'])

    def test_retention_preserves_unresolved_and_request_receipts(self):
        op=self.complete({'success':True,'data':{}})
        store=self.executor.results
        with store.journal._connect() as db:
            db.execute('UPDATE operation_results SET status=?,finished=? WHERE id=?',
                       ('OUTCOME_UNKNOWN',time.time()-RETENTION_SEC-1,op.id))
            db.execute('INSERT INTO receipts VALUES (?,?)',('receipt','preserve'))
        store.prune()
        with store.journal._connect() as db:
            self.assertIsNotNone(db.execute('SELECT payload FROM operation_results WHERE id=?',(op.id,)).fetchone()[0])
            self.assertEqual(db.execute('SELECT payload FROM receipts WHERE key_hash=?',('receipt',)).fetchone()[0],'preserve')


if __name__=='__main__':unittest.main()
