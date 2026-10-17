"""Actionable preflight and Boolean diagnostics without native COM."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp.automation.shape_parametric import ParametricCompiler
from solidworks_mcp.shape_program import ProgramError, run_program


BASE = 'seed=model.tube("Seed",[(0,0,0),(0,10,0)],1)\n'


class SeedDiagnosticsTests(unittest.TestCase):
    def test_invalid_containers_and_items_get_domain_errors(self):
        for operation in ('model.pattern("Copies",{value},count=2,step=(5,0,0))',
                          'model.mirror("Other",{value})',
                          'model.combine("Combined",{value})'):
            for value in ('seed', '"Seed"', 'None', '7', '{}', '[{}]', '[1]', '[None]'):
                with self.subTest(operation=operation, value=value), self.assertRaises(ProgramError) as raised:
                    run_program(BASE + operation.format(value=value))
                self.assertEqual(raised.exception.code, 'INVALID_SEEDS')
                self.assertEqual(raised.exception.details['argument'], 'components')
                self.assertIn('components', str(raised.exception))

    def test_sequences_and_single_placement_keep_working(self):
        for value in ('[seed]', '["Seed"]', '(seed,)', '("Seed",)'):
            for operation in ('model.pattern("Copies",{value},count=2,step=(5,0,0))',
                              'model.mirror("Other",{value})',
                              'model.place("Placed",{value},frame=model.frame(origin=(5,0,0)))'):
                with self.subTest(operation=operation, value=value):
                    run_program(BASE + operation.format(value=value))
        for value in ('seed', '"Seed"'):
            run_program(BASE + f'model.place("Placed",{value},frame=model.frame(origin=(5,0,0)))')
        run_program(BASE + 'other=model.dome("Other",2,2)\nmodel.combine("Both",(seed,other))')

    def test_duplicate_unknown_and_consumed_inputs_keep_their_codes(self):
        for code, expected in [
            ('model.mirror("Other",[seed,seed])', 'INVALID_SEEDS'),
            ('model.mirror("Other",["Missing"])', 'UNKNOWN_COMPONENT'),
            ('model.place("Placed",seed,frame=model.frame(origin=(5,0,0)))\nmodel.mirror("Other",[seed])', 'COMPONENT_CONSUMED')]:
            with self.subTest(code=expected), self.assertRaises(ProgramError) as raised:
                run_program(BASE + code)
            self.assertEqual(raised.exception.code, expected)


class BooleanDiagnosticsTests(unittest.TestCase):
    def test_actual_boolean_count_is_reported_with_scope_and_operation(self):
        inputs = {name: SimpleNamespace(Name=name, Select2=Mock(return_value=True)) for name in ('A', 'B')}
        automation = SimpleNamespace(_find_body=lambda doc, name: inputs[name])
        feature = SimpleNamespace(Name='')
        doc = SimpleNamespace(ClearSelection2=Mock(), FeatureManager=SimpleNamespace(
            InsertCombineFeature=Mock(return_value=feature)))
        compiler = ParametricCompiler(automation, doc)
        compiler.component = 'Teeth'
        compiler.components = {name: {'bodies': [name]} for name in inputs}
        compiler.body_names = lambda: {'A', 'B', 'Witness'}
        compiler.bodies = lambda: [SimpleNamespace(Name=name) for name in ('Witness', 'Result1', 'Result2')]
        module = 'solidworks_mcp.automation.shape_parametric.'
        with patch(module+'typed', side_effect=lambda obj, interface: obj), \
                patch(module+'com_get', side_effect=lambda obj, name: getattr(obj, name)), \
                patch(module+'create_select_data', return_value=None), \
                patch(module+'resolve_solidworks_constant', return_value=15902), \
                self.assertRaises(ProgramError) as raised:
            compiler.combine({'id': 'Teeth', 'seeds': ['A', 'B'],
                              'operation': 'difference', 'expected_bodies': 18})
        self.assertEqual(raised.exception.code, 'NATIVE_RECIPE_FAILED')
        self.assertEqual(raised.exception.details, {'component': 'Teeth', 'stage': 'parametric_boolean',
            'expected_bodies': 18, 'actual_bodies': 2, 'operation': 'difference'})
        self.assertEqual(compiler.components['A']['bodies'], ['A'])


if __name__ == '__main__':
    unittest.main()
