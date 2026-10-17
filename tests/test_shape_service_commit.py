"""Saved edits remain successful when optional post-commit reads fail."""
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.shape_program import run_program
from solidworks_mcp.shape_program import ProgramError
from solidworks_mcp.automation.com_utils import command_scope


MODULE = 'solidworks_mcp.automation.shape_service.'


class ShapeServiceCommitTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(MODULE+'command_scope', side_effect=lambda app:nullcontext())
        patcher.start()
        self.addCleanup(patcher.stop)
        view = patch(MODULE+'focus_bodies', return_value={'verified':True})
        view.start()
        self.addCleanup(view.stop)

    def test_source_is_read_from_native_record_only_when_requested(self):
        sw=SolidWorksAutomation();doc=SimpleNamespace(GetSaveFlag=False,GetType=1)
        sw._connected=True;sw._sw_app=SimpleNamespace(RevisionNumber='34.2.1',ActiveDoc=doc)
        record={'source':'model.dome("Cap",4,2)','program':{'parameters':{}},
                'model_id':'fixture','native':{'components':{},'ownership':{'Cap':{}}}}
        with patch.object(sw,'_get_doc_title',return_value='part'), \
                patch.object(sw,'_get_doc_path',return_value='C:/part.SLDPRT'), \
                patch.object(sw,'list_bodies',return_value={'data':{}}), \
                patch(MODULE+'read_record',return_value=record), \
                patch(MODULE+'observed_revision',return_value='revision'), \
                patch(MODULE+'global_values',return_value={}):
            self.assertNotIn('source',sw.get_model_context()['data'])
            self.assertEqual(sw.get_model_context(include_source=True)['data']['source'],record['source'])

    def test_context_without_active_document(self):
        sw = SolidWorksAutomation()
        sw._connected = True
        sw._sw_app = SimpleNamespace(RevisionNumber='34.2.1', ActiveDoc=None)
        with patch(MODULE+'read_record', side_effect=AssertionError('No document')):
            result = sw.get_model_context()
        self.assertTrue(result['success'])
        self.assertIsNone(result['data']['document'])

    def test_program_preflight_failure_preserves_saved_document_without_checkpoint(self):
        sw=SolidWorksAutomation();doc=SimpleNamespace(GetSaveFlag=False)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path=Path(directory)/'part.SLDPRT';path.write_bytes(b'unchanged')
            code='model.dome("Cap",4,2)'
            record={'source':code,'program':run_program(code),'native':{}}
            stack.enter_context(patch.object(sw,'get_active_doc',return_value=(doc,None)))
            stack.enter_context(patch.object(sw,'_get_doc_path',return_value=str(path)))
            stack.enter_context(patch(MODULE+'read_record',return_value=record))
            stack.enter_context(patch(MODULE+'observed_revision',return_value='r1'))
            stack.enter_context(patch(MODULE+'run_program',side_effect=ProgramError('UNSUPPORTED_SURFACE_HOST','Unsupported host')))
            copier=stack.enter_context(patch(MODULE+'shutil.copyfile'))
            updater=stack.enter_context(patch(MODULE+'update_native'))
            result=sw.execute_model_code(code,mode='edit',document_path=str(path),expected_revision='r1')
            error=result['data']['error'];details=error['details']
            self.assertEqual(error['code'],'UNSUPPORTED_SURFACE_HOST')
            self.assertEqual(details['phase'],'preflight')
            self.assertFalse(details['native_mutation_started'])
            self.assertTrue(details['native_file_preserved'])
            copier.assert_not_called();updater.assert_not_called()
            self.assertEqual(path.read_bytes(),b'unchanged')

    def test_committed_edit_survives_revision_read_failure(self):
        self._check_commit_failure('revision')

    def test_committed_edit_survives_parameter_read_failure(self):
        self._check_commit_failure('parameters')

    def test_mutation_scope_restores_owner_before_save_or_rollback(self):
        for previous in (False,True):
            for failed in (False,True):
                with self.subTest(previous=previous,failed=failed), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    path=Path(directory)/'part.SLDPRT';path.write_bytes(b'original')
                    sw=SolidWorksAutomation();doc=SimpleNamespace(GetSaveFlag=False)
                    app=SimpleNamespace(ActiveDoc=doc,CommandInProgress=previous,CloseDoc=Mock())
                    sw._sw_app=app
                    code='model.dome("Cap",4,2)'
                    record={'source':code,'program':run_program(code),'native':{'document':str(path)}}
                    stack.enter_context(patch(MODULE+'command_scope',command_scope))
                    stack.enter_context(patch('solidworks_mcp.automation.com_utils.typed',side_effect=lambda obj,interface:obj))
                    for name,value in [('get_active_doc',(doc,None)),('_get_doc_path',str(path)),('_get_doc_title','part'),('ui_preflight',None)]:
                        stack.enter_context(patch.object(sw,name,return_value=value))
                    for name,value in [('read_record',record),('observed_revision','revision'),('global_values',{}),
                                       ('prepare_reconcile',({'remove':[],'build':[],'added_parameters':[]},[],[]))]:
                        stack.enter_context(patch(MODULE+name,return_value=value))
                    def mutate(*args, **kwargs):
                        self.assertTrue(app.CommandInProgress)
                        if failed:raise ProgramError('TEST_MUTATION_FAILED','Expected failure')
                        return {}
                    stack.enter_context(patch(MODULE+'update_native',side_effect=mutate))
                    stack.enter_context(patch(MODULE+'write_record',side_effect=lambda *args:self.assertTrue(app.CommandInProgress)))
                    stack.enter_context(patch(MODULE+'atomic_json_write'))
                    def save(*args):
                        self.assertEqual(app.CommandInProgress,previous)
                        path.write_bytes(b'committed');return {'success':True}
                    def reopen(*args):
                        self.assertEqual(app.CommandInProgress,previous)
                        return {'success':True}
                    stack.enter_context(patch.object(sw,'save_document',side_effect=save))
                    stack.enter_context(patch.object(sw,'open_document',side_effect=reopen))
                    result=sw.execute_model_code(code,mode='edit',document_path=str(path),expected_revision='revision',observe=False)
                    self.assertEqual(result['success'],not failed,result)
                    self.assertEqual(app.CommandInProgress,previous)
                    self.assertEqual(path.read_bytes(),b'original' if failed else b'committed')

    def _check_commit_failure(self, failure):
        code = 'model.dome("Cap", 4, 2)'
        sw = SolidWorksAutomation()
        doc = SimpleNamespace(GetSaveFlag=False)
        sw._sw_app = SimpleNamespace(ActiveDoc=doc, CloseDoc=Mock())
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory)/'part.SLDPRT'
            path.write_bytes(b'old-native-part')
            record = {'source': code, 'program': run_program(code),
                      'native': {'document': str(path)}}
            stack.enter_context(patch.object(sw, 'get_active_doc', return_value=(doc, None)))
            stack.enter_context(patch.object(sw, '_get_doc_path', return_value=str(path)))
            stack.enter_context(patch.object(sw, 'ui_preflight', return_value=None))
            stack.enter_context(patch(MODULE+'read_record', return_value=record))
            revisions = ['before', RuntimeError('Read-back failed') if failure == 'revision' else 'after']
            stack.enter_context(patch(MODULE+'observed_revision', side_effect=revisions))
            stack.enter_context(patch(MODULE+'global_values', side_effect=RuntimeError('Read-back failed')))
            stack.enter_context(patch(MODULE+'prepare_reconcile', return_value=(
                {'remove': [], 'build': [], 'added_parameters': []}, [], [])))
            stack.enter_context(patch(MODULE+'update_native', return_value={'edit_strategy': 'in_place'}))
            stack.enter_context(patch(MODULE+'write_record'))
            stack.enter_context(patch(MODULE+'atomic_json_write'))
            def save(*args):
                path.write_bytes(b'committed-native-part')
                return {'success': True}
            saver = stack.enter_context(patch.object(sw, 'save_document', side_effect=save))
            result = sw.execute_model_code(code, mode='edit', document_path=str(path),
                                           expected_revision='before', observe=False)
            self.assertTrue(result['success'], result)
            self.assertEqual(path.read_bytes(), b'committed-native-part')
            self.assertEqual(result['data']['committed_document'], str(path))
            self.assertTrue(result['data']['native_file_preserved'])
            self.assertIsNone(result['data']['parameters'])
            # A memory warning of the running SolidWorks may precede it.
            self.assertIn('POST_COMMIT_READ_FAILED', [w['code'] for w in result['data']['warnings']])
            saver.assert_called_once()
            sw._sw_app.CloseDoc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
