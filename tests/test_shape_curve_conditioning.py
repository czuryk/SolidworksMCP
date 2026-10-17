"""Native equation conditioning must preserve the intended parametric curve."""
import math
import re
import unittest
import sympy as sp
from solidworks_mcp.shape_program import run_program
from solidworks_mcp.shape_equations import plan_recipes, symbolic, scalar
from solidworks_mcp.shape_equations import reverse_native_curve, retained_native_curve
from unittest.mock import Mock, patch
from solidworks_mcp.automation.shape_parametric import recover_path_parameterization
from solidworks_mcp.shape_program import ProgramError

PROGRAM = """H=model.parameter('H',150)
S=H/2.398
def point(t):
    return ((.119+.113*sin(t*pi*.72))*S,(.77-.622*t)*S,-(.023-.065*sin(pi*t))*S)
model.tube('Leg',[point(i/8) for i in range(9)],.053*S)
"""

class CurveConditioningTests(unittest.TestCase):
    def test_binary_noise_does_not_make_linear_height_cubic(self):
        recipes=plan_recipes(run_program(PROGRAM), {'H':'H'})
        for sketch in recipes[0]['sketches']:
            if sketch.get('role') != 'path': continue
            expression=sketch['curves'][0]['xyz'][1]
            value=sp.sympify(expression.replace('"H"','H').replace('^','**'))
            self.assertEqual(sp.degree(value,sp.Symbol('t')),1,expression)
            self.assertLess(max(abs(float(x)) for x in re.findall(r'\d+(?:\.\d+)?',expression)),2)

    def test_emitted_path_matches_hermite_at_multiple_native_scales(self):
        recipes=plan_recipes(run_program(PROGRAM), {'H':'H'})
        def point(t,H):
            return [( .119+.113*math.sin(t*math.pi*.72))*H/2.398,
                    (.77-.622*t)*H/2.398,-(.023-.065*math.sin(math.pi*t))*H/2.398]
        for H in (15,150,165,15000):
            points=[point(i/8,H) for i in range(9)]
            tangents=[[b-a for a,b in zip(points[0],points[1])]]
            tangents += [[(b-a)/2 for a,b in zip(points[i-1],points[i+1])] for i in range(1,8)]
            tangents += [[b-a for a,b in zip(points[-2],points[-1])]]
            for i,sketch in enumerate(recipes[0]['sketches'][:8]):
                funcs=[sp.lambdify(sp.Symbol('t'),sp.sympify(s.replace('"H"',str(H)).replace('^','**')),'math')
                       for s in sketch['curves'][0]['xyz']]
                reverse=math.dist([f(0) for f in funcs],points[i+1])<1e-7
                for t in (0,.125,.5,.875,1):
                    expected=[(2*t**3-3*t*t+1)*a+(-2*t**3+3*t*t)*b+
                              (t**3-2*t*t+t)*u+(t**3-t*t)*v
                              for a,b,u,v in zip(points[i],points[i+1],tangents[i],tangents[i+1])]
                    self.assertLess(math.dist([f(1-t if reverse else t) for f in funcs],expected),1e-7)

    def test_small_intentional_value_is_not_rounded_to_zero(self):
        self.assertEqual(symbolic(scalar(1e-18)),sp.Rational(1,10**18))
        self.assertEqual(symbolic(scalar(.14800000000000002)),sp.Rational(148,1000))

    def test_constructor_lookup_accepts_the_discovered_name(self):
        from solidworks_mcp.automation import SolidWorksAutomation
        sw=SolidWorksAutomation()
        for topic in ('sectioned_solid','model.sectioned_solid'):
            result=sw.model_api_lookup(topic)
            self.assertTrue(result['success'])
            self.assertIn('model.sectioned_solid',result['data'])

    def test_reverse_preserves_locus_and_quoted_dimension_names(self):
        curve={'xyz':['"t@Height"*t^3','t*t','2-t'],'range':[0.,1.]}
        reverse=reverse_native_curve(curve)
        self.assertIn('"t@Height"',reverse['xyz'][0])
        self.assertEqual(retained_native_curve(curve,reverse),reverse)
        self.assertIs(retained_native_curve(curve,{}),curve)
        for old,new in zip(curve['xyz'],reverse['xyz']):
            a=sp.sympify(old.replace('"t@Height"','150').replace('^','**'))
            b=sp.sympify(new.replace('"t@Height"','150').replace('^','**'))
            self.assertEqual(sp.simplify(a.subs('t',1-sp.Symbol('t'))-b),0)

    def test_only_path_warning_gets_one_retry_and_failure_is_not_accepted(self):
        def spec(role='path'):
            return {'role':role,'mode':'3d','name':'Curve','curves':[{'xyz':['t','t*t','1'],'range':[0.,1.]}]}
        with patch('solidworks_mcp.automation.shape_parametric.typed',side_effect=lambda obj,_:obj):
            doc=Mock();feature=Mock();spline=Mock()
            for health in ((0,False),(1,False),(2,True)):
                feature.GetErrorCode2.return_value=health
                self.assertFalse(recover_path_parameterization(doc,feature,[spline],spec()))
            spline.SetEquationParameters2.assert_not_called()
            feature.GetErrorCode2.side_effect=[(1,True),(0,False)]
            good=spec()
            self.assertTrue(recover_path_parameterization(doc,feature,[spline],good))
            self.assertTrue(good['curves'][0]['parameter_reversed'])
            feature.GetErrorCode2.side_effect=[(1,True),(1,True)]
            with self.assertRaises(ProgramError):recover_path_parameterization(doc,feature,[spline],spec())
            self.assertEqual(spline.SetEquationParameters2.call_count,2)

if __name__=='__main__': unittest.main()
