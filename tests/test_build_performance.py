"""Build-time behaviour that keeps long models fast (global-changes.md, G3) and memory signals."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp.automation import session_identity
from solidworks_mcp.automation.shape_parametric import ParametricCompiler, write_equation
from solidworks_mcp.shape_equations import emit_length, plan_recipes
from solidworks_mcp.shape_program import ProgramError, run_program

MODULE = 'solidworks_mcp.automation.shape_parametric.'


class FakeManager:
    def __init__(self):
        self.equations = []

    def Add2(self, index, text, solve):
        self.equations.append((text, solve))
        return len(self.equations) - 1

    def Equation(self, index):
        return self.equations[index][0]

    def SetEquation(self, index, text):
        self.equations[index] = (text, True)


class EquationTests(unittest.TestCase):
    def compiler(self):
        self.manager = FakeManager()
        compiler = ParametricCompiler(Mock(), SimpleNamespace())
        patcher = patch(MODULE + 'typed', return_value=self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch(MODULE + 'com_get', return_value=self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        return compiler

    def test_dimension_equations_wait_for_the_end_of_the_build(self):
        compiler = self.compiler()
        compiler.add_equation('D1@ShapeParameters', '"H"', immediate=True)
        compiler.add_equation('D1@Slot', '(0.1*"H")')
        compiler.add_equation('D3@Slot', '(0.05*"H")')
        # Only the parameter binding reached the manager while features are being created.
        self.assertEqual(len(self.manager.equations), 1)
        self.assertEqual(self.manager.equations[0], ('"D1@ShapeParameters" = "H"', True))
        self.assertEqual(compiler.dimension_equations['D1@Slot'], {'index': -1, 'expression': '(0.1*"H")'})
        compiler.flush_equations()
        self.assertEqual([text for text, _ in self.manager.equations[1:]],
                         ['"D1@Slot" = (0.1*"H")', '"D3@Slot" = (0.05*"H")'])
        self.assertFalse(any(solve for _, solve in self.manager.equations[1:]))
        self.assertEqual(compiler.dimension_equations['D3@Slot']['index'], 2)
        compiler.flush_equations()
        self.assertEqual(len(self.manager.equations), 3)

    def test_rejected_equation_is_an_explicit_failure(self):
        compiler = self.compiler()
        self.manager.Add2 = lambda index, text, solve: -1
        compiler.add_equation('D1@Slot', 'bad')
        with self.assertRaises(ProgramError) as raised:
            compiler.flush_equations()
        self.assertEqual(raised.exception.code, 'NATIVE_EQUATION_FAILED')

    def test_unchanged_equation_is_not_rewritten(self):
        manager = FakeManager()
        manager.Add2(-1, '"D1@Slot" = (0.1*"H")', False)
        manager.SetEquation = Mock()
        self.assertFalse(write_equation(manager, 0, '"D1@Slot"=(0.1*"H")'))
        manager.SetEquation.assert_not_called()
        self.assertTrue(write_equation(manager, 0, '"D1@Slot" = (0.2*"H")'))
        manager.SetEquation.assert_called_once_with(0, '"D1@Slot" = (0.2*"H")')


class RecipeCostTests(unittest.TestCase):
    """Straight tubes must not add entries to the equation manager."""

    def test_straight_tube_has_no_driven_dimensions(self):
        program = run_program('H = model.parameter("H", 150)\nmodel.tube("Pin", [(0, 0, 0), (0.1*H, 0.2*H, 0)], 0.03*H)')
        spec = plan_recipes(program, {'H': 'D1@ShapeParameters'})[0]
        self.assertEqual(spec['variant'], 'tube_cylinder_v3')
        self.assertNotIn('dimensions', spec)
        self.assertNotIn('extrusion', spec)
        section = spec['sketches'][-1]
        self.assertTrue(section['circle_through_tip'])
        self.assertEqual(len(section['curves']), 1)
        self.assertIn('D1@ShapeParameters', section['curves'][0]['xyz'][0])

    def test_saved_tubes_keep_their_recipe(self):
        code = 'model.tube("Pin", [(0, 0, 0), (0, 0, 20)], 5)'
        legacy = run_program(code, recipe_versions={'Pin': 'tube_sweep_v1'})
        self.assertNotIn('recipe', legacy['nodes'][0]['geometry'])
        self.assertNotIn('variant', plan_recipes(legacy, {})[0])
        second = run_program(code, recipe_versions={'Pin': 'tube_cylinder_v2'})
        self.assertEqual(plan_recipes(second, {})[0]['variant'], 'tube_cylinder_v2')
        self.assertEqual(run_program(code)['nodes'][0]['geometry']['recipe'], 'tube_cylinder_v3')

    def test_length_expression_is_well_conditioned(self):
        program = run_program('H = model.parameter("H", 150)\nClr = model.parameter("Clr", 0.25)\n'
                              'model.tube("S", [(0.3*H, 0.2*H - Clr, 0), (0.4*H + Clr, 0.3*H, 0.05*H)], 2)',
                              recipe_versions={'S': 'tube_cylinder_v2'})
        length = plan_recipes(program, {'H': 'D1@P', 'Clr': 'D2@P'})[0]['dimensions']['length']
        self.assertTrue(length.startswith('sqr('))
        # No expanded polynomial with 16-digit integer coefficients, which the equation manager rejects.
        self.assertNotRegex(length, r'\d{9,}')
        from solidworks_mcp.shape_program import Expr
        h = Expr('parameter', ('H',))
        self.assertEqual(emit_length([h * 0, h * 0.1 - 20, h * 0], {'H': 150}, {'H': 'H'}), '(20+(-0.1*"H"))')


class MemoryTests(unittest.TestCase):
    def test_own_process_memory_is_readable(self):
        private = session_identity.process_private_bytes(os.getpid())
        self.assertGreater(private, 1_000_000)
        self.assertIsNone(session_identity.process_private_bytes(999999))
        available, limit = session_identity.commit_memory()
        self.assertGreater(limit, available)

    def test_memory_warning_thresholds(self):
        gigabyte = 2**30
        with patch.object(session_identity, 'solidworks_processes', return_value=[{'pid': 1, 'created_at': 1.}]):
            with patch.object(session_identity, 'process_private_bytes', return_value=3*gigabyte), \
                 patch.object(session_identity, 'commit_memory', return_value=(40*gigabyte, 64*gigabyte)):
                calm = session_identity.solidworks_memory()
            self.assertNotIn('warning', calm)
            self.assertEqual(calm['solidworks_private_gb'], 3.0)
            with patch.object(session_identity, 'process_private_bytes', return_value=47*gigabyte), \
                 patch.object(session_identity, 'commit_memory', return_value=(3.6*gigabyte, 69.8*gigabyte)):
                tight = session_identity.solidworks_memory()
            self.assertEqual(tight['warning'], 'SOLIDWORKS_MEMORY_HIGH')
            self.assertIn('restart_solidworks', tight['action'])
        with patch.object(session_identity, 'solidworks_processes', return_value=[]):
            self.assertIsNone(session_identity.solidworks_memory())


if __name__ == '__main__':
    unittest.main()
