"""Issue #1: localized standard planes must not depend on SelectByID2 names."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import pythoncom

from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.runtime import enrich_legacy_error


class StandardPlaneSketchTests(unittest.TestCase):
    def setUp(self):
        self.planes = [self.feature(name) for name in (
            'Płaszczyzna przednia', 'Płaszczyzna górna', 'Płaszczyzna prawa')]
        self.doc = SimpleNamespace(
            ClearSelection2=Mock(), InsertSketch2=Mock(),
            Extension=SimpleNamespace(SelectByID2=Mock(return_value=False)))
        self.sw = SolidWorksAutomation()
        self.sw.get_active_doc = Mock(return_value=(self.doc, None))
        self.sw._walk_features = Mock(side_effect=lambda *a, **k: iter([
            self.feature('Origin', 'OriginProfileFeature'), *self.planes,
            self.feature('Custom Plane')]))
        self.sw._find_feature = Mock(return_value=None)
        self.sw._auto_normal_to = Mock(return_value={
            'success': True, 'data': {'normal_to_verified': True}})

    @staticmethod
    def feature(name, kind='RefPlane'):
        return SimpleNamespace(Name=name, GetTypeName2=kind,
                               Select2=Mock(return_value=True))

    def test_all_three_localized_planes_select_the_feature_before_insert(self):
        for index, plane in enumerate(('Front', 'Top', 'Right')):
            with self.subTest(plane=plane):
                events = []
                feature = self.planes[index]
                self.doc.ClearSelection2.side_effect = lambda *_: events.append('clear')
                feature.Select2.side_effect = lambda *_: events.append('select') or True
                self.doc.InsertSketch2.side_effect = lambda *_: events.append('insert')
                result = self.sw.create_sketch(plane)
                self.assertTrue(result['success'], result)
                self.assertEqual(events, ['clear', 'select', 'insert'])
                self.assertEqual(result['data']['plane'], feature.Name)
                feature.Select2.assert_called_once_with(False, 0)
                self.sw._walk_features.assert_called_with(self.doc, include_sub=False)
        self.doc.Extension.SelectByID2.assert_not_called()
        self.sw._find_feature.assert_not_called()

    def test_english_names_do_not_require_extension_selection(self):
        for feature, name in zip(self.planes, ('Front Plane', 'Top Plane', 'Right Plane')):
            feature.Name = name
        self.doc.Extension.SelectByID2.side_effect = RuntimeError('Member not found')
        self.assertTrue(self.sw.create_sketch()['success'])
        self.doc.Extension.SelectByID2.assert_not_called()

    def assert_selection_failure(self, result, code):
        self.assertFalse(result['success'], result)
        self.assertEqual(result['error_code'], 104)
        self.assertFalse(result['data']['sketch_created'])
        self.assertEqual(result['data']['error']['code'], code)
        self.doc.InsertSketch2.assert_not_called()
        self.sw._auto_normal_to.assert_not_called()

    def test_false_selection_has_actionable_diagnostics_and_no_sketch(self):
        self.planes[1].Select2.return_value = False
        result = self.sw.create_sketch('Top')
        self.assert_selection_failure(result, 'PLANE_SELECTION_FAILED')
        selection = result['data']['selection']
        self.assertEqual(selection['requested_plane'], 'Top')
        self.assertEqual(selection['resolved_name'], self.planes[1].Name)
        self.assertEqual(selection['method'], 'IFeature.Select2')
        self.assertIsNone(result['data']['error']['com_hresult'])
        self.assertEqual(enrich_legacy_error(result)['data']['error']['code'],
                         'PLANE_SELECTION_FAILED')

    def test_incomplete_tree_does_not_guess_a_localized_plane(self):
        self.planes.pop()
        self.sw._walk_features.side_effect = lambda *a, **k: iter(self.planes)
        self.assert_selection_failure(self.sw.create_sketch(), 'STANDARD_PLANE_NOT_FOUND')
        self.planes[0].Select2.assert_not_called()

    def test_english_fallback_selects_a_refplane_feature(self):
        self.sw._walk_features.side_effect = lambda *a, **k: iter([])
        fallback = self.feature('Front Plane')
        self.sw._find_feature.return_value = fallback
        result = self.sw.create_sketch()
        self.assertTrue(result['success'], result)
        fallback.Select2.assert_called_once_with(False, 0)
        self.assertEqual(result['data']['selection']['lookup'], 'english_name_fallback')

    def test_english_fallback_must_not_select_a_sketch_named_front_plane(self):
        self.sw._walk_features.side_effect = lambda *a, **k: iter([])
        fallback = self.feature('Front Plane', 'ProfileFeature')
        self.sw._find_feature.return_value = fallback
        self.assert_selection_failure(self.sw.create_sketch(), 'STANDARD_PLANE_NOT_FOUND')
        fallback.Select2.assert_not_called()

    def test_com_selection_failure_preserves_hresult(self):
        self.planes[0].Select2.side_effect = pythoncom.com_error(
            -2147352573, 'Member not found', None, None)
        result = self.sw.create_sketch()
        self.assert_selection_failure(result, 'PLANE_SELECTION_FAILED')
        self.assertEqual(result['data']['error']['com_hresult'], -2147352573)

    def test_rpc_disconnect_is_not_relabelled_as_a_selection_failure(self):
        self.planes[0].Select2.side_effect = pythoncom.com_error(
            -2147023174, 'RPC server unavailable', None, None)
        with self.assertRaises(pythoncom.com_error):
            self.sw.create_sketch()
        self.doc.InsertSketch2.assert_not_called()

    def test_failed_view_reports_that_sketch_was_already_created(self):
        self.sw._auto_normal_to.return_value = {'success': False, 'data': {}}
        result = self.sw.create_sketch()
        self.assertFalse(result['success'])
        self.assertTrue(result['data']['sketch_created'])
        self.doc.InsertSketch2.assert_called_once_with(True)

    def test_document_error_is_returned_unchanged(self):
        error = {'success': False, 'message': 'No active document'}
        self.sw.get_active_doc.return_value = (None, error)
        self.assertIs(self.sw.create_sketch(), error)
        self.sw._walk_features.assert_not_called()


if __name__ == '__main__':
    unittest.main()
