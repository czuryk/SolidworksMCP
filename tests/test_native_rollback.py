import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp.automation import SolidWorksAutomation


class NativeRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'source.SLDPRT';self.source.write_bytes(b'original native dimensions')
        self.sw=SolidWorksAutomation()
        self.doc=SimpleNamespace(GetSaveFlag=False)
        self.sw._sw_app=SimpleNamespace(ActiveDoc=self.doc,CloseDoc=Mock())
        self.sw._get_doc_path=lambda doc:str(self.source) if doc is self.doc else str(self.root/'other.SLDPRT')
        self.sw._get_doc_title=lambda doc:'source.SLDPRT'
        self.sw.open_document=Mock(return_value={'success':True})
        self.before={'path':str(self.source),'features':[{'name':'Body'}],
                     'bodies':[{'name':'Main'}],'solid_body_count':1}
        self.sw._transaction_snapshot=Mock(return_value=self.before)

    def checkpoint(self):
        return self.sw._create_checkpoint(self.doc,'test',{'path':str(self.root/'checkpoint.SLDPRT')},self.before)

    def test_changed_dimensions_with_same_names_restore_original_bytes(self):
        checkpoint=self.checkpoint()
        self.source.write_bytes(b'changed dimensions, same feature names')
        result=self.sw._rollback_transaction(self.doc,self.before,checkpoint['path'])
        self.assertTrue(result['document_restored'])
        self.assertEqual(self.source.read_bytes(),b'original native dimensions')
        self.sw.open_document.assert_called_once_with(str(self.source))
        self.sw._sw_app.CloseDoc.assert_called_once()

    def test_no_checkpoint_cannot_claim_rollback_from_matching_names(self):
        result=self.sw._rollback_transaction(self.doc,self.before,None)
        self.assertFalse(result['document_restored'])
        self.sw._sw_app.CloseDoc.assert_not_called()

    def test_wrong_active_document_and_tampered_checkpoint_are_preserved(self):
        checkpoint=self.checkpoint()
        self.sw._sw_app.ActiveDoc=object()
        result=self.sw._rollback_transaction(self.doc,self.before,checkpoint['path'])
        self.assertFalse(result['document_restored'])
        self.sw._sw_app.CloseDoc.assert_not_called()
        self.sw._sw_app.ActiveDoc=self.doc
        Path(checkpoint['path']).write_bytes(b'bad checkpoint')
        result=self.sw._rollback_transaction(self.doc,self.before,checkpoint['path'])
        self.assertFalse(result['document_restored'])
        self.sw._sw_app.CloseDoc.assert_not_called()

    def test_retention_keeps_unowned_cad_files_and_existing_path_is_rejected(self):
        unrelated=self.root/'unrelated.SLDPRT';unrelated.write_bytes(b'user model')
        result=self.sw._create_checkpoint(self.doc,'test',{'path':str(self.root/'one.SLDPRT'),'keep_last_n':1},self.before)
        self.assertTrue(result['success'])
        result2=self.sw._create_checkpoint(self.doc,'test',{'path':str(self.root/'two.SLDPRT'),'keep_last_n':1},self.before)
        self.assertTrue(result2['success'])
        self.assertTrue(unrelated.exists() and self.source.exists())
        self.assertFalse(Path(result['path']).exists())
        rejected=self.sw._create_checkpoint(self.doc,'test',{'path':str(unrelated)},self.before)
        self.assertFalse(rejected['success'])
        self.assertEqual(unrelated.read_bytes(),b'user model')

    def test_transaction_does_not_claim_commit_when_native_save_fails(self):
        self.before['feature_count']=1
        self.sw.get_active_doc=Mock(return_value=(self.doc,None))
        self.sw._create_checkpoint=Mock(return_value={'success':True,'path':None})
        self.sw._validate_invariants=Mock(return_value=[])
        self.sw.save_document=Mock(return_value={'success':False,'message':'disk failure'})
        result=self.sw.run_transaction('SaveFailure',[])
        self.assertFalse(result['success'])
        error=result['data']['error']
        self.assertEqual(error['code'],'TRANSACTION_SAVE_FAILED')
        self.assertFalse(error['details']['committed'])

    def test_transaction_switch_stops_before_next_operation(self):
        self.before['feature_count']=1
        self.sw.get_active_doc=Mock(return_value=(self.doc,None))
        self.sw._create_checkpoint=Mock(return_value={'success':True,'path':None})
        self.sw._validate_invariants=Mock(return_value=[])
        def change_active(*args):
            self.sw._sw_app.ActiveDoc=object()
            return {'success':True,'message':'first'}
        self.sw._call_operation=Mock(side_effect=change_active)
        result=self.sw.run_transaction('Switch',[{'op':'hide_body'},{'op':'delete_feature'}])
        self.assertFalse(result['success'])
        self.assertEqual(self.sw._call_operation.call_count,1)
        self.sw._sw_app.CloseDoc.assert_not_called()


if __name__=='__main__':unittest.main()
