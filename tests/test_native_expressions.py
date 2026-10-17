"""Evaluating native curve expressions to prove a rebuild is current (global-changes.md, G11)."""
import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp.automation import shape_parametric
from solidworks_mcp.native_expressions import curve_endpoints, evaluate, referenced
from solidworks_mcp.shape_equations import plan_recipes
from solidworks_mcp.shape_program import run_program

FIXTURE = (Path(__file__).parent / 'fixtures' / 'bender_program.py').read_text(encoding='utf-8')
DIMENSIONS = {'H': 'D1@ShapeParameters', 'Clr': 'D2@ShapeParameters'}


class EvaluateTests(unittest.TestCase):
    def test_native_syntax(self):
        values = {'D1@P': 150.0}
        self.assertAlmostEqual(evaluate('(0+(0.1*"D1@P")*((1-t^2)/(1+t^2)))', 0, values), 15.0)
        self.assertAlmostEqual(evaluate('(0+(0.1*"D1@P")*((1-t^2)/(1+t^2)))', 1, values), 0.0)
        self.assertAlmostEqual(evaluate('sqr((0.02*"D1@P")^2+(4)^2)', 0, values), 5.0)
        self.assertAlmostEqual(evaluate('(10*cos(t))', math.pi, {}), -10.0)
        self.assertAlmostEqual(evaluate('((0.5*"D1@P")*t+-2.5)', 1, values), 72.5)
        self.assertEqual(evaluate('0', .3, {}), 0.0)
        self.assertEqual(referenced('("D1@P"*t)+"D2@P"'), {'D1@P', 'D2@P'})

    def test_negative_dimension_values_and_unknown_text(self):
        self.assertAlmostEqual(evaluate('(2*"D1@P")', 0, {'D1@P': -1.5}), -3.0)
        with self.assertRaises(KeyError):
            evaluate('"D9@P"', 0, {})
        for text in ('__import__("os")', 'open(1)', 't.real', 'x+1'):
            with self.assertRaises((ValueError, KeyError)):
                evaluate(text, 0, {})

    def test_every_reference_curve_evaluates_and_scales_with_the_parameter(self):
        specs = plan_recipes(run_program(FIXTURE), DIMENSIONS)
        curves = [dict(curve, mode=sketch['mode'], sketch=sketch['name'])
                  for spec in specs for sketch in spec['sketches'] for curve in sketch['curves']]
        self.assertGreater(len(curves), 200)
        full = {'D1@ShapeParameters': 150.0, 'D2@ShapeParameters': 0.25}
        small = {'D1@ShapeParameters': 120.0, 'D2@ShapeParameters': 0.25}
        moved = 0
        for curve in curves:
            a, b = curve_endpoints(curve, full), curve_endpoints(curve, small)
            self.assertTrue(all(math.isfinite(v) for point in a + b for v in point))
            moved += a != b
        # Nearly every curve depends on H: a stale sketch has other end points.
        self.assertGreater(moved, len(curves) * .9)
        head = next(c for c in curves if c['sketch'] == 'HeadCylinder_Section1')
        self.assertAlmostEqual(curve_endpoints(head, full)[0][0], 10.7, places=6)
        self.assertAlmostEqual(curve_endpoints(head, small)[0][0], 8.56, places=6)


class CurvesCurrentTests(unittest.TestCase):
    """curves_current accepts fresh sketches and rejects one that kept its old points."""

    def setUp(self):
        self.curves = [{'sketch': 'A_Section1', 'index': 0, 'mode': '3d', 'range': [0., 1.],
                        'xyz': ['(0.1*"D1@P")*t', '(0.2*"D1@P")', '0']},
                       {'sketch': 'B_Profile', 'index': 0, 'mode': '2d', 'range': [0., 1.],
                        'xyz': ['(0.05*"D1@P")', '(0.3*"D1@P")*t', '']}]
        self.native = {'curves': self.curves}
        self.points = {'A_Section1': [(0, 30, 0), (15, 30, 0)], 'B_Profile': [(7.5, 0, 0), (7.5, 45, 0)]}
        self.doc = SimpleNamespace(Parameter=lambda name: SimpleNamespace(SystemValue=.15))
        self.automation = Mock()
        self.automation._find_feature.side_effect = lambda doc, name: SimpleNamespace(
            GetSpecificFeature2=SimpleNamespace(GetSketchPoints2=[
                SimpleNamespace(X=x / 1000, Y=y / 1000, Z=z / 1000) for x, y, z in self.points[name]]))

    def test_fresh_and_stale(self):
        self.assertTrue(shape_parametric.curves_current(self.automation, self.doc, self.native))
        # The 2D sketch still shows the geometry of H=120.
        self.points['B_Profile'] = [(6, 0, 0), (6, 36, 0)]
        self.assertFalse(shape_parametric.curves_current(self.automation, self.doc, self.native))

    def test_missing_sketch_or_dimension_is_not_current(self):
        self.automation._find_feature.side_effect = lambda doc, name: None
        self.assertFalse(shape_parametric.curves_current(self.automation, self.doc, self.native))
        self.doc.Parameter = lambda name: None
        self.assertFalse(shape_parametric.curves_current(self.automation, self.doc, self.native))


if __name__ == '__main__':
    unittest.main()
