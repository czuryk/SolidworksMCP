"""Edit history with revert, and language-independent standard planes."""
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.shape_native import NativeCompiler
from solidworks_mcp.automation.shape_service import (HISTORY_DEPTH, history_directory, history_entries,
                                                     keep_history)
from solidworks_mcp.shape_program import run_program
from solidworks_mcp.shape_responses import compact_response

MODULE = 'solidworks_mcp.automation.shape_service.'
CODE = 'model.dome("Cap", 4, 2)'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'part.SLDPRT'

    def keep(self, content, revision):
        checkpoint = Path(self.temp.name) / f'checkpoint-{revision}.SLDPRT'
        checkpoint.write_bytes(content)
        return keep_history(self.path, checkpoint, {'revision': revision, 'source': CODE}, sha(content))

    def test_history_keeps_the_last_states_only(self):
        for index in range(HISTORY_DEPTH + 2):
            self.keep(f'state{index}'.encode(), f'r{index}')
        entries = history_entries(self.path)
        self.assertEqual(len(entries), HISTORY_DEPTH)
        self.assertEqual(entries[-1].read_bytes(), f'state{HISTORY_DEPTH + 1}'.encode())
        record = json.loads(entries[-1].with_suffix('.shape.json').read_text(encoding='utf-8'))
        self.assertEqual(record['native_file_sha256'], sha(entries[-1].read_bytes()))
        self.assertFalse(list(Path(self.temp.name).glob('checkpoint-*')))

    def test_foreign_files_in_the_history_folder_are_left_alone(self):
        folder = history_directory(self.path)
        folder.mkdir(parents=True)
        (folder / 'notes.SLDPRT').write_bytes(b'user file')
        (folder / '123-orphan.SLDPRT').write_bytes(b'no record beside it')
        for index in range(HISTORY_DEPTH + 1):
            self.keep(f'state{index}'.encode(), f'r{index}')
        self.assertEqual(len(history_entries(self.path)), HISTORY_DEPTH)
        self.assertEqual((folder / 'notes.SLDPRT').read_bytes(), b'user file')
        self.assertTrue((folder / '123-orphan.SLDPRT').exists())


class RevertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'part.SLDPRT'
        self.path.write_bytes(b'current state')
        self.sidecar = self.path.with_suffix('.shape.json')
        self.sidecar.write_text(json.dumps({'revision': 'r2'}), encoding='utf-8')
        self.previous = {'revision': 'r1', 'source': CODE, 'program': run_program(CODE),
                         'native': {'components': {'Cap': {'feature': 'Cap', 'bodies': ['Cap']}},
                                    'status': 'NATIVE_GEOMETRY_CREATED'}}
        self.current = {**self.previous, 'revision': 'r2'}
        self.sw = SolidWorksAutomation()
        self.doc = SimpleNamespace(GetSaveFlag=False)
        self.sw._sw_app = SimpleNamespace(ActiveDoc=self.doc, CloseDoc=Mock())
        self.records = [self.current, self.previous]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (('get_active_doc', (self.doc, None)), ('_get_doc_path', str(self.path)),
                            ('_get_doc_title', 'part'), ('ui_preflight', None),
                            ('_shape_body_state', {'bodies': [], 'count': 0})):
            self.stack.enter_context(patch.object(self.sw, name, return_value=value))
        self.opened = self.stack.enter_context(patch.object(self.sw, 'open_document', return_value={'success': True}))
        self.stack.enter_context(patch(MODULE + 'read_record', side_effect=lambda doc: self.records.pop(0)))
        self.stack.enter_context(patch(MODULE + 'observed_revision', side_effect=lambda doc, record: 'rev-' + record['revision']))
        self.stack.enter_context(patch(MODULE + 'global_values', return_value={'H': 150.0}))
        self.stack.enter_context(patch(MODULE + 'focus_bodies', return_value={'verified': True}))

    def keep_previous(self, content=b'previous state'):
        checkpoint = Path(self.temp.name) / 'checkpoint.SLDPRT'
        checkpoint.write_bytes(content)
        return keep_history(self.path, checkpoint, self.previous, sha(content))

    def revert(self, revision='rev-r2'):
        return self.sw.execute_model_code(mode='revert', document_path=str(self.path),
                                          expected_revision=revision, observe=False)

    def test_revert_restores_file_and_sidecar_and_consumes_the_entry(self):
        self.keep_previous()
        result = self.revert()
        self.assertTrue(result['success'], result)
        self.assertEqual(self.path.read_bytes(), b'previous state')
        self.assertEqual(json.loads(self.sidecar.read_text(encoding='utf-8'))['revision'], 'r1')
        data = result['data']
        self.assertEqual(data['status'], 'NATIVE_GEOMETRY_REVERTED')
        self.assertEqual(data['revision'], 'rev-r1')
        self.assertEqual(data['history_depth'], 0)
        self.assertEqual(history_entries(self.path), [])
        self.sw._sw_app.CloseDoc.assert_called_once_with('part')
        self.opened.assert_called_once_with(str(self.path))
        compact = compact_response('execute_model_code', {}, result)['data']
        self.assertEqual(compact['edit_strategy'], 'restore_saved_state')
        self.assertEqual(compact['rebuilt_components'], [])
        self.assertEqual(compact['preserved_count'], 1)

    def test_revert_without_history_or_with_a_stale_revision_changes_nothing(self):
        missing = self.revert()
        self.assertEqual(missing['data']['error']['code'], 'NO_HISTORY')
        self.records = [self.current, self.previous]
        self.keep_previous()
        stale = self.revert('rev-old')
        self.assertEqual(stale['data']['error']['code'], 'STALE_REVISION')
        self.assertEqual(self.path.read_bytes(), b'current state')
        self.assertEqual(len(history_entries(self.path)), 1)
        self.sw._sw_app.CloseDoc.assert_not_called()

    def test_corrupt_history_entry_is_refused(self):
        entry = self.keep_previous()
        entry.write_bytes(b'tampered')
        result = self.revert()
        self.assertEqual(result['data']['error']['code'], 'HISTORY_CORRUPT')
        self.assertEqual(self.path.read_bytes(), b'current state')

    def test_failed_restore_returns_to_the_current_state(self):
        self.keep_previous()
        self.opened.side_effect = [{'success': False, 'message': 'cannot open'}, {'success': True}]
        result = self.revert()
        error = result['data']['error']
        self.assertEqual(error['code'], 'REVERT_FAILED')
        self.assertTrue(error['document_restored'])
        self.assertEqual(self.path.read_bytes(), b'current state')
        self.assertEqual(json.loads(self.sidecar.read_text(encoding='utf-8'))['revision'], 'r2')
        self.assertEqual(len(history_entries(self.path)), 1)

    def test_revert_rejects_edit_arguments(self):
        result = self.sw.execute_model_code(CODE, mode='revert', document_path=str(self.path), expected_revision='x')
        self.assertEqual(result['data']['error']['code'], 'INVALID_ARGUMENT')


class StandardPlaneTests(unittest.TestCase):
    def feature(self, name, kind):
        return SimpleNamespace(Name=name, GetTypeName2=kind, Select2=Mock(return_value=True))

    def compiler(self, features):
        automation = Mock()
        automation._walk_features.side_effect = lambda doc, include_sub=True: iter(features)
        automation._find_feature.return_value = None
        return NativeCompiler(automation, SimpleNamespace(ClearSelection2=Mock())), automation

    def test_localized_planes_are_found_by_position(self):
        tree = [self.feature('Anmerkungen', 'DetailCabinet'), self.feature('Vorne', 'RefPlane'),
                self.feature('Oben', 'RefPlane'), self.feature('Rechts', 'RefPlane'),
                self.feature('Ursprung', 'OriginProfileFeature'), self.feature('Ebene1', 'RefPlane')]
        compiler, automation = self.compiler(tree)
        self.assertEqual(compiler.standard_plane('Front').Name, 'Vorne')
        self.assertEqual(compiler.standard_plane('Top').Name, 'Oben')
        self.assertEqual(compiler.standard_plane('Right').Name, 'Rechts')
        compiler.select_standard_plane('Top')
        tree[2].Select2.assert_called_once_with(False, 0)
        # The tree is read once per compiler.
        self.assertEqual(automation._walk_features.call_count, 1)

    def test_missing_planes_fall_back_to_the_english_name(self):
        compiler, automation = self.compiler([self.feature('Origin', 'OriginProfileFeature')])
        english = self.feature('Front Plane', 'RefPlane')
        automation._find_feature.return_value = english
        self.assertIs(compiler.standard_plane('Front'), english)
        automation._find_feature.assert_called_with(compiler.doc, 'Front Plane')


if __name__ == '__main__':
    unittest.main()
