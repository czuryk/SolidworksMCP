"""Public approximation, native planning and profile ownership contracts."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.shape_program import ProgramError, run_program
from solidworks_mcp.shape_equations import plan_recipes, topology
from solidworks_mcp.automation.shape_ellipsoid import validate_guided_profiles

SOURCE = "Rx=model.parameter('Rx',10)\nRy=model.parameter('Ry',20)\nRz=model.parameter('Rz',6)\ne=model.ellipsoid('Oval',(Rx,Ry,Rz),approximate=True)"


def plan(code=SOURCE, overrides=None):
    p = run_program(code, overrides)
    return plan_recipes(p, {k:f'D{i+1}@ShapeParameters' for i,k in enumerate(p['parameters'])})


class GuidedEllipsoidTests(unittest.TestCase):
    def test_independent_axes_preserve_symbolic_guides_and_topology(self):
        a, b = plan(), plan(overrides={'Rx':8,'Ry':17,'Rz':12})
        self.assertEqual(a[0]['sketches'], b[0]['sketches'])
        self.assertEqual(topology(a), topology(b))
        self.assertEqual(len(a[0]['sketches']),5)
        self.assertEqual(b[0]['guided_ellipsoid']['radii'], [8,17,12])
        self.assertNotIn('scale_z',a[0])

    def test_four_meridians_have_signed_exact_endpoints(self):
        node=plan("model.ellipsoid('Oval',(10,20,6),approximate=True)")[0]
        for sketch, start in zip(node['sketches'][1:], [(10,0,0),(0,0,6),(-10,0,0),(0,0,-6)]):
            curves=sketch['curves'][0]['xyz']
            for t, expected in [(0,start),(1,(0,20,0))]:
                actual=[eval(v.replace('^','**'), {'t':t}) for v in curves]
                self.assertEqual(actual,list(expected))

    def test_exact_legacy_source_and_topology_remain_unchanged(self):
        p=run_program("model.ellipsoid('E',(10,20,6))")
        self.assertNotIn('approximate',p['source_graph']['nodes'][0]['geometry'])
        node=plan("model.ellipsoid('E',(10,20,6))")[0]
        self.assertEqual(node['scale_z'],.6)
        self.assertNotIn('ellipsoid_recipe',topology([node])[0])

    def test_opt_in_and_invalid_values_are_explicit(self):
        with self.assertRaises(ProgramError) as e:plan(SOURCE.replace(',approximate=True',''))
        self.assertEqual(e.exception.code,'ELLIPSOID_ASPECT_PARAMETER')
        with self.assertRaises(ProgramError) as e:plan(SOURCE.replace('approximate=True','approximate=1'))
        self.assertEqual(e.exception.code,'INVALID_APPROXIMATION')

    def test_analytic_anchors_cannot_claim_approximate_surface(self):
        for suffix in ['p=model.surface_point(e)',
                       "f=model.place('Moved',e,frame=model.frame(origin=(30,0,0)))\np=model.surface_point(f)"]:
            with self.assertRaises(ProgramError) as e:plan(SOURCE+'\n'+suffix)
            self.assertEqual(e.exception.code,'APPROXIMATE_SURFACE_ANCHOR')

    def test_all_center_axes_drive_native_placement(self):
        node=plan(SOURCE.replace('approximate=True','approximate=True,center=(Rx,-Ry,Rz)'))[0]
        self.assertEqual(set(node['placement']),set('XYZ'))
        self.assertEqual(node['placement']['Y']['direction'],-1)
        self.assertIn('"Ry"',node['placement']['Y']['expression'])

    def test_internal_names_are_owned_and_reserved(self):
        for suffix in ['_Axis','_Meridian4','_Equator','_Half','_OtherHalf']:
            with self.assertRaises(ProgramError) as e:plan(SOURCE+f"\nmodel.dome('Oval{suffix}',2,1)")
            self.assertEqual(e.exception.code,'NATIVE_NAME_COLLISION')

    def test_relation_tamper_fails_before_native_edit(self):
        components={'Oval':{'native_profile':{'feature':'Oval_Equator','relations':[[9,['p','q']]]}}}
        automation=SimpleNamespace(_find_feature=lambda doc,name:object())
        with patch('solidworks_mcp.automation.shape_ellipsoid.com_get',return_value=object()), \
             patch('solidworks_mcp.automation.shape_ellipsoid.relation_signature',return_value=[]):
            with self.assertRaises(ProgramError) as e:validate_guided_profiles(automation,object(),components)
        self.assertEqual(e.exception.code,'NATIVE_MAPPING_STALE')

    def test_native_surface_patch_remains_an_actual_face_dependency(self):
        nodes=plan(SOURCE+"\nm=model.rounded_prism('Mask',8,8,12,1,center=(0,0,8))\nmodel.surface_patch('Skin',e,m,1,offset=.25)")
        self.assertEqual(nodes[-1]['seeds'],['Oval','Mask'])
        self.assertEqual(nodes[-1]['surface']['offset'],.25)


if __name__=='__main__':unittest.main()
