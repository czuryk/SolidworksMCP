"""One saved part has one revision, whatever spelling of its path it was opened under.

Found through the MCP client: an edit addressed to a part that was not open answered
STALE_REVISION, because the request layer opened it under the lower-case form of its path
and the revision hashed the path as SolidWorks reported it.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.automation import request_execution
from solidworks_mcp.automation.request_execution import execute_request, native_state, request_spec
from solidworks_mcp.automation.request_journal import RequestJournal
from solidworks_mcp.automation.shape_observation import canonical_path, geometry_state, true_case
from solidworks_mcp.automation.shape_parametric import observed_revision


def document(path, stamp=7):
    configuration = SimpleNamespace(Name='Default')
    return SimpleNamespace(GetPathName=str(path), GetUpdateStamp=stamp, GetFeatureCount=12,
                           ConfigurationManager=SimpleNamespace(ActiveConfiguration=configuration))


class SpellingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name) / 'Parts'
        folder.mkdir()
        self.path = folder / 'Bracket.SLDPRT'
        self.path.write_bytes(b'saved part')
        self.lower = str(self.path).lower()
        self.assertNotEqual(self.lower, str(self.path))

    def test_true_case_is_the_spelling_on_disk(self):
        stored = os.path.realpath(self.path)
        self.assertEqual(true_case(self.lower), stored)
        self.assertEqual(true_case(str(self.path).upper()), stored)
        self.assertEqual(true_case(stored), stored)
        self.assertEqual(true_case(''), '')

    def test_a_path_that_differs_in_more_than_case_is_kept(self):
        elsewhere = str(Path(self.temp.name) / 'Other' / 'Bracket.SLDPRT')
        with patch('os.path.realpath', return_value=elsewhere):
            self.assertEqual(true_case(self.lower), self.lower)
        with patch('os.path.realpath', side_effect=OSError('offline share')):
            self.assertEqual(true_case(self.lower), self.lower)

    def test_the_revision_does_not_depend_on_the_spelling(self):
        record = {'revision': 'r1'}
        with patch('solidworks_mcp.automation.shape_parametric.global_values', return_value={'H': 150.0}):
            proper = observed_revision(document(self.path), record)
            lowered = observed_revision(document(self.lower), record)
            rebuilt = observed_revision(document(self.path, stamp=8), record)
        self.assertEqual(proper, lowered)
        self.assertNotEqual(proper, rebuilt)
        self.assertEqual(geometry_state(document(self.lower))['path'], os.path.realpath(self.path))

    def test_the_request_layer_opens_a_part_under_the_callers_spelling(self):
        arguments = {'mode': 'edit', 'document_path': str(self.path), 'expected_revision': 'rev1',
                     'parameters': {'H': 120}}
        spec = request_spec('execute_model_code', arguments)
        self.assertEqual(spec['target'], os.path.abspath(self.path))
        self.assertEqual(spec['document'], canonical_path(self.path))
        seen = []

        def state(_, target):
            seen.append(target)
            return 'rev1', {'file': 'baseline'}

        journal = RequestJournal(Path(self.temp.name) / 'requests.sqlite3')
        with patch.object(request_execution, 'native_state', side_effect=state):
            result = execute_request(None, 'execute_model_code', arguments,
                                     lambda: {'success': True, 'data': {}}, journal)
            execute_request(None, 'execute_model_code', arguments, lambda: {'success': True, 'data': {}}, journal)
        self.assertTrue(result['success'])
        self.assertEqual(set(seen), {os.path.abspath(self.path)})
        self.assertEqual(len(seen), 3)              # Before, after, and the replay check.

    def test_native_state_activates_the_spelling_it_is_given(self):
        opened = []

        class Automation:
            active = 'C:\\elsewhere\\Other.SLDPRT'

            def get_active_doc(self):
                return SimpleNamespace(GetType=1, GetSaveFlag=False), None

            def _get_doc_path(self, doc):
                return self.active

            def open_document(self, path):
                opened.append(path)
                self.active = path
                return {'success': True}

        with patch.object(request_execution, 'read_record', return_value={'model_id': 'm1'}), \
             patch.object(request_execution, 'geometry_state', return_value={'configuration': 'Default'}), \
             patch.object(request_execution, 'document_revision', return_value='rev1'):
            revision, fingerprint = native_state(Automation(), str(self.path))
        self.assertEqual(opened, [str(self.path)])
        self.assertEqual(revision, 'rev1')
        # The identity in the receipt stays the normalised form, as in receipts written before.
        self.assertEqual(fingerprint['document'], canonical_path(self.path))


if __name__ == '__main__':
    unittest.main()
