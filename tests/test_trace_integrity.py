import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from solidworks_mcp import __version__
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.transactions import MCP_VERSION


class TraceIntegrityTests(unittest.TestCase):
    def test_shared_native_endpoint_is_satisfied_without_self_relation(self):
        a = SolidWorksAutomation()
        point = SimpleNamespace()
        doc = SimpleNamespace(Extension=SimpleNamespace(
            GetPersistReference3=Mock(return_value=b'native-point')),
            ClearSelection2=Mock(), SketchAddConstraints=Mock())
        records = {'a': {'points': {'end': point}},
                   'b': {'points': {'start': point}}}
        result = a._apply_constraint(doc, records,
            {'type': 'coincident', 'entities': ['a.end', 'b.start']})
        self.assertEqual(result['status'], 'SHARED_NATIVE_POINT')
        self.assertFalse(result['created'])
        doc.ClearSelection2.assert_not_called()
        doc.SketchAddConstraints.assert_not_called()

    def test_matching_coordinates_or_missing_ids_do_not_prove_shared_point(self):
        a = SolidWorksAutomation()
        records = {name: {'points': {'start': SimpleNamespace(X=0, Y=0, Z=0)}}
                   for name in ('a', 'b')}
        for ids in ([b'a', b'b'], [None, None]):
            doc = SimpleNamespace(Extension=SimpleNamespace(
                GetPersistReference3=Mock(side_effect=ids)),
                ClearSelection2=Mock(), SketchAddConstraints=Mock())
            a._select_reference = Mock(return_value=True)
            a._active_relation_count = Mock(side_effect=[0, 0])
            with self.assertRaisesRegex(RuntimeError, 'rejected'):
                a._apply_constraint(doc, records,
                    {'type': 'coincident', 'entities': ['a.start', 'b.start']})
            doc.SketchAddConstraints.assert_called_once()

    def test_arc_fit_preserves_seams_and_equal_endpoint_radii(self):
        a = SolidWorksAutomation()
        angles = np.linspace(.2, 1.8, 100)
        radii = 10 + .04 * np.sin(np.linspace(0, 8, 100))
        points = np.column_stack([radii*np.cos(angles), radii*np.sin(angles)])
        result = a._circle_fit(points, preserve_endpoints=True)
        self.assertIsNotNone(result)
        self.assertEqual(result['start'], points[0].tolist())
        self.assertEqual(result['end'], points[-1].tolist())
        self.assertAlmostEqual(math.dist(result['center'], result['start']),
                               math.dist(result['center'], result['end']), places=10)
        errors = abs(np.linalg.norm(points-np.array(result['center']), axis=1)-result['radius'])
        self.assertAlmostEqual(result['max_error'], max(errors))
        self.assertLess(result['max_error'], .15)

    def test_open_spline_fits_fixed_seams_within_original_error(self):
        a = SolidWorksAutomation()
        x = np.linspace(0, 10, 80)
        points = np.column_stack([x, .3*np.sin(x)])
        result, _ = a._fit_open_bspline(points, .1, 16)
        self.assertIsNotNone(result)
        self.assertEqual(result['control_points'][0], points[0].tolist())
        self.assertEqual(result['control_points'][-1], points[-1].tolist())
        self.assertLessEqual(result['fit_error_mm'], .1)

    def test_topology_includes_nonperiodic_spline_seams(self):
        a = SolidWorksAutomation()
        loops = [{'entities': [{'id': 'a', 'type': 'b_spline'}, {'id': 'b', 'type': 'arc'}]}]
        self.assertEqual(len(a._topology_constraints(loops, 'minimal_parametric')), 2)
        self.assertEqual(a._topology_constraints(loops, 'construction_reference'), [])
        self.assertEqual(a._topology_constraints([{'entities': [{'id': 'p', 'type': 'b_spline', 'periodic': True}]}], 'locked_trace'), [])

    def test_bad_arc_does_not_reach_native_creation(self):
        a = SolidWorksAutomation()
        manager = SimpleNamespace(CreateArc=Mock())
        doc = SimpleNamespace(SketchManager=manager)
        with self.assertRaisesRegex(ValueError, 'same circle'):
            a._create_entity(doc, {'type': 'arc', 'center': [0, 0],
                                  'start': [10, 0], 'end': [0, 11]}, 'mm')
        manager.CreateArc.assert_not_called()

    def test_vector_checkpoint_restores_bytes_after_post_commit_failure(self):
        from test_native_rollback import NativeRollbackTests
        fixture = NativeRollbackTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        a = fixture.sw
        a.get_active_doc = Mock(return_value=(fixture.doc, None))
        def operation():
            fixture.source.write_bytes(b'changed')
            return a._error('IMAGE_LOW_CONFIDENCE', 'Native comparison failed')
        result = a._checkpointed_vector_commit('Trace', operation, True)
        self.assertFalse(result['success'])
        self.assertTrue(result['data']['error']['document_restored'])
        self.assertEqual(fixture.source.read_bytes(), b'original native dimensions')

    def test_vector_checkpoint_rejects_dirty_and_failed_checkpoint_before_geometry(self):
        a = SolidWorksAutomation()
        doc = SimpleNamespace(GetSaveFlag=True)
        a.get_active_doc = Mock(return_value=(doc, None))
        operation = Mock()
        with tempfile.NamedTemporaryFile() as file:
            a._get_doc_path = Mock(return_value=file.name)
            result = a._checkpointed_vector_commit('Trace', operation, True)
            self.assertFalse(result['success'])
            operation.assert_not_called()
            doc.GetSaveFlag = False
            a._transaction_snapshot = Mock(return_value={})
            a._create_checkpoint = Mock(return_value={'success': False})
            result = a._checkpointed_vector_commit('Trace', operation, True)
            self.assertFalse(result['success'])
            operation.assert_not_called()

    def test_vector_checkpoint_catches_exception_and_keeps_restore_failure_false(self):
        a = SolidWorksAutomation()
        doc = SimpleNamespace(GetSaveFlag=False)
        a.get_active_doc = Mock(return_value=(doc, None))
        with tempfile.NamedTemporaryFile() as file:
            a._get_doc_path = Mock(return_value=file.name)
            a._transaction_snapshot = Mock(return_value={})
            a._create_checkpoint = Mock(return_value={'success': True, 'path': file.name})
            a._rollback_transaction = Mock(return_value={'document_restored': False})
            result = a._checkpointed_vector_commit('Trace', Mock(side_effect=RuntimeError('failure')), True)
            self.assertFalse(result['success'])
            self.assertFalse(result['data']['error']['document_restored'])

    def test_version_source_is_shared(self):
        self.assertEqual(MCP_VERSION, __version__)


if __name__ == '__main__':
    unittest.main()
