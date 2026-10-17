"""Regression contracts for equation planning and edit dispatch."""
import math
import unittest
from unittest.mock import patch
from solidworks_mcp.shape_program import Expr,ProgramError,run_program
from solidworks_mcp.shape_equations import emit,plan_recipes,topology,orient_path_curve,T,symbolic
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.server import _is_effectively_mutating


class NativePlanningTests(unittest.TestCase):
    def plans(self,code,values=None):
        p=run_program(code,values)
        return plan_recipes(p,{k:f'D{i+1}@ShapeParameters' for i,k in enumerate(p['parameters'])})

    def test_constant_folding_and_scientific_notation(self):
        self.assertEqual(emit(Expr('literal',(0.000002,)),{}),'0.000002')
        self.assertEqual(emit(Expr('sqrt',(Expr('literal',(4,)),)),{}),'2')

    def test_reciprocal_has_no_bare_negative_exponent(self):
        h=Expr('parameter',('H',))
        expr=emit(1/(1+h*h),{'H':'D1@ShapeParameters'})
        self.assertNotIn('^-',expr)
        self.assertIn('(1/',expr)

    def test_normal_section_avoids_expanding_frame_algebra(self):
        code='H=model.parameter("H",150)\nmodel.tube("Leg",[(.055*H,.32*H,0),(.085*H,.19*H,-.025*H),(.08*H,.055*H,0)],.021*H)'
        node=self.plans(code)[0]
        profile=node['sketches'][-1]
        self.assertEqual(profile['mode'],'normal')
        self.assertEqual(profile['path'],'Leg_Path1')
        self.assertEqual(profile['origin'],[8.25,48,0])
        self.assertTrue(all(len(e)<100 for c in profile['curves'] for e in c['xyz']))

    def test_scale_does_not_change_curve_definitions_or_topology(self):
        code='H=model.parameter("H",150)\nmodel.dome("Cap",.1*H,.08*H,center=(.2*H,0,0))'
        a,b=self.plans(code),self.plans(code,{'H':165})
        self.assertEqual(a[0]['sketches'],b[0]['sketches'])
        self.assertEqual(topology(a),topology(b))
        self.assertNotEqual(a[0]['placement']['X']['value'],b[0]['placement']['X']['value'])

    def test_constant_clearance_is_not_scaled(self):
        code='H=model.parameter("H",150)\ng=model.parameter("Gap",.25)\nmodel.sectioned_solid("Housing",[(0,H/10,H/10),(H/2,H/10,H/10)],center_xz=(H/5+g,0))'
        node=self.plans(code,{'H':165})[0]
        x=node['sketches'][0]['curves'][0]['xyz'][0]
        self.assertIn('"D2@ShapeParameters"',x)
        actual=eval(x.replace('^','**').replace('"D1@ShapeParameters"','165').replace('"D2@ShapeParameters"','.25'),{'t':0,'cos':math.cos})
        self.assertAlmostEqual(actual,49.75)

    def test_invalid_native_domains_fail_without_connecting(self):
        for name,value in [('H',0),('H',-1),('t',1)]:
            sw=SolidWorksAutomation()
            with patch.object(sw,'connect',side_effect=AssertionError('COM accessed')):
                result=sw.execute_model_code(f'{name}=model.parameter("{name}",{value})\nmodel.dome("D",4,2)',mode='create',output_path='C:/never.SLDPRT')
            self.assertEqual(result['data']['error']['code'],'NATIVE_PARAMETER_DOMAIN')

    def test_expression_limit_is_pre_com(self):
        with patch('solidworks_mcp.shape_equations.emit',return_value='x'*2001),self.assertRaises(ProgramError) as error:
            self.plans('model.sectioned_solid("D",[(0,4,4),(10,4,4)])')
        self.assertEqual(error.exception.code,'NATIVE_EXPRESSION_LIMIT')

    def test_component_count_changes_topology(self):
        a=self.plans('model.dome("A",4,2)')
        b=self.plans('model.dome("A",4,2)\nmodel.dome("B",4,2)')
        self.assertNotEqual(topology(a),topology(b))

    def test_edit_is_a_writer_and_analyze_is_not(self):
        self.assertTrue(_is_effectively_mutating('execute_model_code',{'mode':'edit'}))
        self.assertFalse(_is_effectively_mutating('execute_model_code',{'mode':'analyze'}))

    def test_native_parameter_cannot_silently_become_a_literal(self):
        with self.assertRaises(ProgramError) as error:
            self.plans('H=model.parameter("H",100)\nmodel.dome("Cap",model.value(H)/10,5)')
        self.assertEqual(error.exception.code,'NATIVE_PARAMETER_ESCAPE')

    def test_path_orientation_preserves_the_curve(self):
        import sympy as sp
        curve=([T,T*T,0],0,1)
        oriented=orient_path_curve(curve,{})
        t=sp.Symbol('t',real=True)
        for value in [0,.25,.5,1]:
            expected=[1-value,(1-value)**2,0]
            actual=[float(symbolic(v).subs(t,value)) for v in oriented[0]]
            for a,b in zip(actual,expected):self.assertAlmostEqual(a,b)

    def test_negative_placement_uses_positive_dimension_and_explicit_direction(self):
        p=self.plans('H=model.parameter("H",100)\nmodel.dome("Cap",10,5,center=(-H/5,0,0))')[0]['placement']['X']
        self.assertEqual(p['direction'],-1)
        self.assertEqual(p['value'],-20)
        self.assertAlmostEqual(eval(p['expression'].replace('"H"','100')),20)

    def test_generated_native_names_cannot_shadow_components(self):
        for code in ['model.dome("A",4,2)\nmodel.dome("A_Profile",4,2)',
                     'model.dome("A",4,2)\nmodel.dome("a",4,2)']:
            with self.subTest(code=code),self.assertRaises(ProgramError) as error:self.plans(code)
            self.assertEqual(error.exception.code,'NATIVE_NAME_COLLISION')


if __name__=='__main__':unittest.main()
