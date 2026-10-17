"""Offline checks of what the verification of 6.28.0 through the MCP client turned up."""
import unittest
from unittest.mock import Mock, PropertyMock, patch

from solidworks_mcp.automation.shape_parametric import (PICK_VIEWS, ParametricCompiler, pick_view,
                                                        recover_path_parameterization)
from solidworks_mcp.automation.shape_service import failure_causes
from solidworks_mcp.shape_program import ProgramError, run_program

# One tube that bends too tightly next to a block with a fillet.
PROGRAM = ('H = model.parameter("H", 150)\nR = model.parameter("R", 2)\nu = H / 150\n'
           'model.tube("Leg", [(8.6*u, 48.2*u - 0.25, 2.2*u), (8.6*u, 45.6*u, 2.2*u), (10.2*u, 41.5*u, 2.6*u), '
           '(11.55*u, 36.8*u, 3.2*u), (12.7*u, 29.6*u, 3.7*u)], 3.8*u)\n'
           'block = model.rounded_prism("Block", 10, 10, 10, 0, center=(-40, 5, 5))\n'
           'model.fillet("Rim", block, R, edges={"planar_loop": {"axis": "Z", "side": "max"}})\n')


class FailureCauseTests(unittest.TestCase):
    def setUp(self):
        self.program = run_program(PROGRAM)

    def failed(self, **details):
        return ProgramError('NATIVE_REBUILD_FAILED', 'Edited document did not rebuild', **details)

    def test_a_warning_about_another_component_is_not_a_cause(self):
        fillet = self.failed(component='Rim', features=[{'feature': 'Rim', 'type': 'Fillet', 'component': 'Rim'}])
        predicted, causes = failure_causes(fillet, self.program)
        self.assertEqual([w['code'] for w in predicted], ['TUBE_BEND_TOO_TIGHT'])
        self.assertEqual(causes, [])

    def test_a_warning_about_the_failed_component_is(self):
        for details in ({'component': 'Leg'}, {'features': [{'feature': 'Leg', 'type': 'Sweep', 'component': 'Leg'}]}):
            with self.subTest(details=details):
                _, causes = failure_causes(self.failed(**details), self.program)
                self.assertEqual(len(causes), 1)
                self.assertTrue(causes[0].startswith('Leg: TUBE_BEND_TOO_TIGHT'))

    def test_a_failure_before_the_program_exists_has_no_causes(self):
        self.assertEqual(failure_causes(ValueError('syntax'), None), ([], []))
        self.assertEqual(failure_causes(self.failed(), self.program)[1], [])


class PathRecoveryTests(unittest.TestCase):
    def spec(self):
        return {'role': 'path', 'mode': '3d', 'name': 'Leg_Path5', 'curves': [{'xyz': ['t', 't*t', '1'], 'range': [0., 1.]}]}

    def recover(self, accepted, rebuilt, health):
        doc, feature, spline = Mock(), Mock(), Mock()
        spline.SetEquationParameters2.return_value = accepted
        doc.ForceRebuild3.return_value = rebuilt
        feature.GetErrorCode2.side_effect = [(1, True)] + list(health)
        with patch('solidworks_mcp.automation.shape_parametric.typed', side_effect=lambda obj, _: obj):
            with self.assertRaises(ProgramError) as raised:
                recover_path_parameterization(doc, feature, [spline], self.spec())
        return raised.exception, doc

    def test_the_error_says_which_step_failed(self):
        refused, doc = self.recover(False, True, [])
        self.assertEqual(refused.code, 'NATIVE_PATH_FAILED')
        self.assertEqual(refused.details, {'feature': 'Leg_Path5', 'component': 'Leg', 'attempts': 2,
                                           'reversed_curve_accepted': False, 'rebuilt': None, 'error_code': None})
        doc.ForceRebuild3.assert_not_called()
        stuck, _ = self.recover(True, False, [])
        self.assertEqual((stuck.details['reversed_curve_accepted'], stuck.details['rebuilt'], stuck.details['error_code']),
                         (True, False, None))
        warned, _ = self.recover(True, True, [(1, True)])
        self.assertEqual((warned.details['rebuilt'], warned.details['error_code']), (True, 1))


class PickViewTests(unittest.TestCase):
    """Loft connectors are picked on screen: the camera must not look along two vertices of the sections."""

    def test_hose_stations_are_not_picked_from_the_front(self):
        # Stations of a variable tube running up Y: two vertices each, in front of each other along Z.
        sections = [[[.040, y, .003], [.040, y, -.003]] for y in (0., .010, .020)]
        picks = [points[0] for points in sections]
        direction, clear = pick_view(sections, picks)
        self.assertNotEqual(direction, (0, 0, -1))
        self.assertGreater(clear, .5)

    def test_equal_rings_are_not_picked_from_above(self):
        # A straight stack along Y: seen from above the pick points of all sections coincide.
        ring = lambda y: [[.01, y, 0.], [0., y, .01], [-.01, y, 0.], [0., y, -.01]]
        sections = [ring(0.), ring(.02), ring(.04)]
        picks = [points[0] for points in sections]
        direction, clear = pick_view(sections, picks)
        self.assertNotEqual(direction, (0, -1, 0))
        self.assertGreater(clear, .5)

    def test_the_best_of_the_candidates_is_returned_with_its_separation(self):
        sections = [[[.01, 0., 0.], [-.01, 0., 0.]], [[.01, .02, 0.], [-.01, .02, 0.]]]
        picks = [points[0] for points in sections]
        direction, clear = pick_view(sections, picks)
        self.assertIn(direction, PICK_VIEWS)
        self.assertAlmostEqual(clear, 1.)            # From the front every vertex is seen square on.
        self.assertEqual(direction, (0, 0, -1))

    def test_the_camera_is_put_back_after_a_loft(self):
        compiler = ParametricCompiler.__new__(ParametricCompiler)
        view = Mock()
        compiler.doc = Mock(ActiveView=view)
        compiler._loft_camera = ('orientation', 'translation', 2.5)
        compiler.restore_camera()
        self.assertEqual((view.Orientation3, view.Scale2, view.Translation3), ('orientation', 2.5, 'translation'))
        self.assertIsNone(compiler._loft_camera)
        compiler.restore_camera()                    # Nothing saved: nothing to do.

    def test_a_camera_failure_does_not_fail_the_loft(self):
        compiler = ParametricCompiler.__new__(ParametricCompiler)
        compiler.component = 'Hose'
        compiler.doc = Mock()
        type(compiler.doc).ActiveView = PropertyMock(side_effect=RuntimeError('no view'))
        with self.assertLogs('solidworks_mcp.automation.shape_parametric', level='WARNING') as logs:
            compiler.show_picks([], [])
        self.assertIn('Hose', logs.output[0])


if __name__ == '__main__':
    unittest.main()
