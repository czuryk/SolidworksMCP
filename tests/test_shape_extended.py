"""Dependency and native representation contracts for extended shape families."""
import unittest
from unittest.mock import patch,Mock
from types import SimpleNamespace
from solidworks_mcp.shape_program import ProgramError,run_program
from solidworks_mcp.shape_equations import plan_recipes,topology
from solidworks_mcp.automation import SolidWorksAutomation

def plan(code,overrides=None):
    program=run_program(code,overrides)
    return plan_recipes(program,{k:f'D{i+1}@ShapeParameters' for i,k in enumerate(program['parameters'])})

class ExtendedShapeTests(unittest.TestCase):
    def test_ellipsoid_retains_native_scale_dependencies(self):
        code='H=model.parameter("H",100)\nmodel.ellipsoid("Oval",(H/10,H/8,H/15))'
        a,b=plan(code),plan(code,{'H':110})
        self.assertEqual(a,b)
        self.assertAlmostEqual(a[0]['scale_z'],2/3)
        self.assertEqual(len(a[0]['sketches'][0]['curves']),3)

    def test_unbound_ellipsoid_aspect_is_rejected_before_com(self):
        sw=SolidWorksAutomation()
        with patch.object(sw,'connect',side_effect=AssertionError('COM accessed')):
            result=sw.execute_model_code('H=model.parameter("H",100)\nmodel.ellipsoid("A",(H/10,5,4))',mode='create',output_path='C:/never.SLDPRT')
        self.assertFalse(result['success'])
        self.assertEqual(result['data']['error']['code'],'ELLIPSOID_ASPECT_PARAMETER')

    def test_invalid_ellipsoid_radii(self):
        for radii in ['(0,1,1)','(1,-1,1)']:
            with self.subTest(radii=radii),self.assertRaises(ProgramError):plan(f'model.ellipsoid("A",{radii})')

    def test_variable_stations_follow_path_endpoints(self):
        spec=plan('model.variable_tube("A",[(0,0,0),(2,10,0),(0,20,0)],[2,3,1])')[0]
        sections=[s for s in spec['sketches'] if s.get('role')=='section']
        self.assertEqual(len(sections),3)
        self.assertTrue(all(s['mode']=='3d' for s in sections))
        # Opposite section points average to the requested station center.
        import math
        for section,center in zip(sections,[[0,0,0],[2,10,0],[0,20,0]]):
            curves=section['curves'][0]['xyz']
            a=[eval(e.replace('^','**'),{'t':0,'cos':math.cos,'sin':math.sin,'sqr':math.sqrt}) for e in curves]
            b=[eval(e.replace('^','**'),{'t':math.pi,'cos':math.cos,'sin':math.sin,'sqr':math.sqrt}) for e in curves]
            for actual,expected in zip([(x+y)/2 for x,y in zip(a,b)],center):self.assertAlmostEqual(actual,expected)

    def test_variable_station_validation(self):
        for points,radii in [('[(0,0,0),(0,10,0)]','[2]'),('[(0,0,0),(0,10,0)]','[2,-1]'),('[(0,0,0),(0,10,0),(0,0,0)]','[2,2,2]')]:
            with self.subTest(points=points,radii=radii),self.assertRaises(ProgramError):plan(f'model.variable_tube("A",{points},{radii})')

    def test_boolean_records_explicit_ordered_dependencies(self):
        specs=plan('a=model.dome("A",4,2)\nb=model.dome("B",3,2)\nmodel.combine("Result",[a,b],operation="difference")')
        self.assertEqual(specs[-1]['seeds'],['A','B'])
        self.assertEqual(topology(specs)[-1]['operation'],'difference')

    def test_consumed_bodies_cannot_be_reused(self):
        prefix='a=model.dome("A",4,2)\nb=model.dome("B",3,2)\nr=model.combine("Result",[a,b])\n'
        for suffix in ['model.mirror("M",[a])','model.combine("C",[a,r])']:
            with self.subTest(suffix=suffix),self.assertRaises(ProgramError) as error:plan(prefix+suffix)
            self.assertEqual(error.exception.code,'COMPONENT_CONSUMED')

    def test_boolean_result_is_available_to_mirror(self):
        specs=plan('a=model.dome("A",4,2)\nb=model.dome("B",3,2)\nr=model.combine("Result",[a,b])\nmodel.mirror("M",[r])')
        self.assertEqual(specs[-1]['seeds'],['Result'])

    def test_boolean_scope_errors_are_pre_com(self):
        for suffix in ['model.combine("C",[a])','model.combine("C",[a,a])','model.combine("C",[a,"Missing"])','model.combine("C",[a,b],expected_bodies=0)']:
            with self.subTest(suffix=suffix),self.assertRaises(ProgramError):
                plan('a=model.dome("A",4,2)\nb=model.dome("B",3,2)\n'+suffix)

    def test_dependency_direction_is_part_of_edit_topology(self):
        prefix='a=model.dome("A",4,2)\nb=model.dome("B",3,2)\n'
        a=plan(prefix+'model.combine("C",[a,b],operation="difference")')
        b=plan(prefix+'model.combine("C",[b,a],operation="difference")')
        self.assertNotEqual(topology(a),topology(b))

    def test_rounded_prism_has_eight_clean_edges_and_native_depth(self):
        code='H=model.parameter("H",100)\nmodel.rounded_prism("Box",H/10,H/8,H/20,H/100,center=(H/4,0,H/8))'
        swept=plan(code)[0]
        self.assertEqual(swept['variant'],'sweep')
        self.assertEqual([len(s['curves']) for s in swept['sketches']],[1,8])
        self.assertEqual(swept['placement'],{})
        self.assertNotIn('extrusion',swept)
        # Saved parts keep the extrusion with its native depth and placement.
        program=run_program(code,recipe_versions={'Box':'prism_extrude_v1'})
        spec=plan_recipes(program,{'H':'D1@ShapeParameters'})[0]
        self.assertEqual(len(spec['sketches'][0]['curves']),8)
        self.assertEqual(spec['extrusion']['value'],5)
        self.assertIn('"H"',spec['extrusion']['expression'])
        self.assertEqual(set(spec['placement']),{'Z'})

    def test_rounded_prism_degeneracy_is_pre_com(self):
        for args in ['10,8,6,-1','10,8,6,4.01','10,8,-6,1']:
            with self.subTest(args=args),self.assertRaises(ProgramError):plan(f'model.rounded_prism("Box",{args})')

    def test_full_round_prism_is_a_native_slot_or_circle(self):
        slot=plan('model.rounded_prism("Slot",28,12,10,6,center=(0,0,5))')[0]
        self.assertEqual(slot['variant'],'slot_horizontal')
        self.assertEqual(slot['sketches'][0]['curves'],[])
        self.assertEqual(slot['sketches'][0]['slot'],{'half_length':8.0,'radius':6.0,'vertical':False})
        self.assertEqual(set(slot['dimensions']),{'slot_length','diameter'})
        self.assertEqual(set(slot['placement']),{'Z'})
        tall=plan('model.rounded_prism("Slot",12,28,10,6,center=(3,-4,5))')[0]
        self.assertEqual(tall['variant'],'slot_vertical')
        self.assertEqual({k:v['direction'] for k,v in tall['placement'].items()},{'X':1,'Y':-1,'Z':1})
        disc=plan('model.rounded_prism("Disc",12,12,10,6)')[0]
        self.assertEqual(disc['variant'],'circle')
        self.assertEqual(disc['sketches'][0]['circle']['radius'],6.0)
        # The equality must hold for every parameter value, not by coincidence at this one.
        symbolic='H=model.parameter("H",150)\nu=H/150\nmodel.rounded_prism("Slot",28*u,12*u,10*u,6*u,center=(0,0,5*u))'
        driven=plan(symbolic)[0]
        self.assertEqual(driven['variant'],'slot_horizontal')
        self.assertIn('"H"',driven['dimensions']['slot_length'])
        self.assertIn('"H"',driven['dimensions']['diameter'])
        with self.assertRaises(ProgramError) as raised:
            plan('H=model.parameter("H",150)\nmodel.rounded_prism("Slot",28,12,10,H/25)')
        self.assertEqual(raised.exception.code,'INVALID_ROUNDED_PRISM')
        # An ordinary rounded rectangle keeps its eight equation curves.
        self.assertEqual(len(plan('model.rounded_prism("Box",28,12,10,5.9)')[0]['sketches'][-1]['curves']),8)
        self.assertEqual(plan('model.rounded_prism("Box",28,12,10,5.9)')[0]['variant'],'sweep')

    def test_multibody_difference_target_is_pre_com(self):
        code='a=model.dome("A",4,2)\nb=model.dome("B",3,2)\nm=model.mirror("M",[a,b])\nmodel.combine("C",[m,a],operation="difference")'
        with self.assertRaises(ProgramError) as error:plan(code)
        self.assertEqual(error.exception.code,'INVALID_BOOLEAN_TARGET')

    def test_native_feature_lookup_does_not_scan_the_tree(self):
        sw=SolidWorksAutomation();feature=SimpleNamespace(Name='Target')
        part=SimpleNamespace(FeatureByName=Mock(return_value=feature))
        with patch('solidworks_mcp.automation.advanced_features.typed',return_value=part),patch.object(sw,'_walk_features',side_effect=AssertionError('Full scan')):
            self.assertIs(sw._find_feature(SimpleNamespace(GetType=1),'Target'),feature)

    def test_feature_lookup_preserves_exact_name_semantics(self):
        sw=SolidWorksAutomation();wrong=SimpleNamespace(Name='TARGET');correct=SimpleNamespace(Name='Target')
        part=SimpleNamespace(FeatureByName=lambda name:wrong)
        with patch('solidworks_mcp.automation.advanced_features.typed',return_value=part),patch.object(sw,'_walk_features',return_value=iter([wrong,correct])):
            self.assertIs(sw._find_feature(SimpleNamespace(GetType=1),'Target'),correct)

if __name__=='__main__':unittest.main()
