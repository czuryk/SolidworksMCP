"""The forced rebuild of an edit is proven on curves the edit must have moved.

Seen once in a long session: ForceRebuild3 answered True, the dimensions held the new values
and every equation curve kept its old points; the edit saved the part and reported success.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp.automation import shape_parametric
from solidworks_mcp.automation.shape_parametric import curves_current, forced_rebuild, witness_curves
from solidworks_mcp.error_actions import recovery_actions
from solidworks_mcp.shape_program import ProgramError

H = 'D1@ShapeParameters'
R = 'D2@ShapeParameters'


def curve(sketch, expression=f'(0.1*"{H}")*t', mode='3d'):
    return {'sketch': sketch, 'index': 0, 'mode': mode, 'xyz': [expression, 't', '0'], 'range': [0., 1.]}


class WitnessTests(unittest.TestCase):
    def setUp(self):
        self.native = {'curves': [curve(f'S{i}') for i in range(9)] + [curve('Fixed', '5*t')]}

    def test_first_middle_and_last_of_the_curves_that_depend_on_a_rewritten_dimension(self):
        picked = witness_curves(self.native, {H}, [])
        self.assertEqual([c['sketch'] for c in picked], ['S0', 'S4', 'S8'])

    def test_curves_of_other_dimensions_are_not_witnesses(self):
        self.assertEqual(witness_curves(self.native, {R}, []), [])
        self.assertEqual(witness_curves(self.native, set(), []), [])

    def test_a_rewritten_curve_is_a_witness_whatever_it_depends_on(self):
        fixed = self.native['curves'][-1]
        self.assertEqual(witness_curves(self.native, set(), [fixed]), [fixed])
        both = witness_curves(self.native, {H}, [fixed])
        self.assertEqual([c['sketch'] for c in both], ['S0', 'S4', 'Fixed'])

    def test_few_candidates_are_all_checked(self):
        native = {'curves': [curve('A'), curve('B')]}
        self.assertEqual(len(witness_curves(native, {H}, [])), 2)


class CurvesCurrentTests(unittest.TestCase):
    """The proof reads only the sketches of the curves it is given."""

    def document(self, value_mm, ends):
        point = lambda x: SimpleNamespace(X=x / 1000, Y=0., Z=0.)
        features = {name: SimpleNamespace(GetSpecificFeature2=SimpleNamespace(
            GetSketchPoints2=[point(0.), SimpleNamespace(X=end / 1000, Y=.001, Z=0.)])) for name, end in ends.items()}
        doc = SimpleNamespace(Parameter=lambda name: SimpleNamespace(SystemValue=value_mm / 1000))
        automation = SimpleNamespace(_find_feature=Mock(side_effect=lambda _, name: features.get(name)))
        return automation, doc

    def test_a_subset_is_proven_without_reading_the_rest(self):
        native = {'curves': [curve('Fresh'), curve('Stale'), curve('Other')]}
        # H is 75 mm: the end point of each curve is at x = 7.5 mm; one sketch still holds 15 mm.
        automation, doc = self.document(75., {'Fresh': 7.5, 'Stale': 15., 'Other': 7.5})
        self.assertTrue(curves_current(automation, doc, native, curves=[native['curves'][0]]))
        self.assertEqual(automation._find_feature.call_count, 1)
        self.assertFalse(curves_current(automation, doc, native, curves=native['curves'][:2]))
        self.assertFalse(curves_current(automation, doc, native))
        self.assertTrue(curves_current(automation, doc, native, curves=[]))


class ForcedRebuildTests(unittest.TestCase):
    def run_rebuild(self, answers, current, witnesses=({'sketch': 'W'},)):
        doc = Mock()
        doc.ForceRebuild3.side_effect = answers
        equations = Mock()
        native = {'ownership': {}}
        with patch.object(shape_parametric, 'curves_current', side_effect=current) as proof, \
             patch.object(shape_parametric, 'rebuild_error', return_value=ProgramError('NATIVE_REBUILD_FAILED', 'no')):
            try:
                mode = forced_rebuild(Mock(), doc, equations, native, list(witnesses))
            except ProgramError as exc:
                mode = exc
        return mode, doc, equations, proof

    def test_a_rebuild_that_moved_the_curves_is_one_rebuild(self):
        mode, doc, equations, proof = self.run_rebuild([True], [True])
        self.assertEqual(mode, 'forced')
        self.assertEqual(doc.ForceRebuild3.call_count, 1)
        equations.EvaluateAll.assert_not_called()

    def test_an_edit_without_witnesses_is_not_checked(self):
        mode, doc, _, proof = self.run_rebuild([True], [], witnesses=())
        self.assertEqual(mode, 'forced')
        proof.assert_not_called()

    def test_stale_curves_earn_one_more_evaluation_and_rebuild(self):
        mode, doc, equations, proof = self.run_rebuild([True, True], [False, True])
        self.assertEqual(mode, 'forced_twice')
        self.assertEqual(doc.ForceRebuild3.call_count, 2)
        self.assertEqual(proof.call_count, 2)

    def test_curves_that_stay_stale_fail_the_edit(self):
        mode, doc, _, _ = self.run_rebuild([True, True], [False, False], witnesses=({'sketch': 'Leg_Path1'},))
        self.assertIsInstance(mode, ProgramError)
        self.assertEqual(mode.code, 'NATIVE_REBUILD_STALE')
        self.assertEqual(mode.details, {'sketches': ['Leg_Path1'], 'rebuilds': 2})
        self.assertEqual(doc.ForceRebuild3.call_count, 2)

    def test_a_rebuild_that_reports_failure_is_a_rebuild_error(self):
        first, doc, _, proof = self.run_rebuild([False], [])
        self.assertEqual(first.code, 'NATIVE_REBUILD_FAILED')
        proof.assert_not_called()
        second, _, _, _ = self.run_rebuild([True, False], [False])
        self.assertEqual(second.code, 'NATIVE_REBUILD_FAILED')

    def test_the_failure_tells_how_to_repeat_the_edit(self):
        arguments = {'mode': 'edit', 'document_path': 'D:\\\\work\\\\part.SLDPRT', 'expected_revision': 'r1',
                     'parameters': {'H': 75}}
        action = recovery_actions('NATIVE_REBUILD_STALE', 'execute_model_code', arguments,
                                  {'sketches': ['Leg_Path1'], 'rebuilds': 2})[0]
        self.assertEqual(action['tool'], 'execute_model_code')
        self.assertTrue(action['arguments']['idempotency_key'].startswith('retry-'))
        self.assertEqual(action['arguments']['expected_revision'], 'r1')
        self.assertIn('restart_solidworks', action['why'])


if __name__ == '__main__':
    unittest.main()
