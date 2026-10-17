"""Regression evidence for the Bender post-retest polish; no native COM."""
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np

from solidworks_mcp.shape_program import Expr, ProgramError, run_program
from solidworks_mcp.shape_equations import emit, emit_dimension, plan_recipes
from solidworks_mcp.mesh_cleanup import bounded_cleanup, mesh_diagnostics, write_binary_stl
from solidworks_mcp.mesh_acceptance import inspect_mesh
from solidworks_mcp.automation.high_level import HighLevelOperations
from solidworks_mcp.automation.shape_edit_compatibility import edit_compatibility

V = np.array([[0.,0,0], [1.,0,0], [0,1.,0], [0,0,1.]])
F = np.array([[0,2,1], [0,1,3], [0,3,2], [1,2,3]])
TETRA = V[F]
BOX = {'min':[0,0,0], 'max':[1,1,1]}


def evaluate(expression, h, t=0, gap=.25):
    return eval(expression.replace('^','**').replace('"D1@ShapeParameters"',str(h))
                .replace('"H"',str(h)).replace('"Gap"',str(gap)), {'__builtins__':{}}, {'t':t})


class DimensionPolishTests(unittest.TestCase):
    def test_affine_fixed_gap_and_named_gap_at_three_heights(self):
        h = Expr('parameter',('H',));gap=Expr('parameter',('Gap',))
        for g in (.25, gap):
            expression = emit_dimension(118*h/150+g/.939372712847379, {'H':'H','Gap':'Gap'})
            self.assertLess(len(expression),100)
            self.assertNotIn('000000000000000', expression)
            for height in (135,150,165):
                self.assertAlmostEqual(evaluate(expression,height),118*height/150+.25/.939372712847379, places=10)

    def test_non_affine_and_curve_emission_remain_legacy(self):
        h = Expr('parameter',('H',))
        for expression in (h*h,1/h):
            self.assertEqual(emit_dimension(expression,{'H':'H'}),emit(expression,{'H':'H'}))
        self.assertEqual(emit_dimension(.000002,{}),'0.000002')

    def test_shared_radius_joins_all_quarters_exactly(self):
        code='H=model.parameter("H",150)\nS=H/150\nmodel.sectioned_solid("Torso",[(46.5*S,14.7*S,13*S),(88.5*S,19*S,15.5*S)],center_xz=(.1*H,-.2*H))'
        program=run_program(code)
        self.assertEqual(program['source_graph']['nodes'][0]['geometry']['profile_representation'],'rational_quarters_v2')
        recipe=plan_recipes(program,{'H':'D1@ShapeParameters'})[0]
        for height in (135,150,165):
            for section in recipe['sketches']:
                curves=section['curves']
                for a,b in zip(curves,curves[1:]+curves[:1]):
                    self.assertEqual([evaluate(v,height,1) for v in a['xyz']],
                                     [evaluate(v,height,0) for v in b['xyz']])

    def test_stored_legacy_quarters_and_trig_remain_unchanged(self):
        code='H=model.parameter("H",150)\nmodel.sectioned_solid("Torso",[(0,H/10,H/10),(H/2,H/8,H/9)])'
        for version, count in [('rational_quarters',4),('trig_halves',2)]:
            p=run_program(code, recipe_versions={'Torso':version})
            s=plan_recipes(p,{'H':'D1@ShapeParameters'})[0]['sketches'][0]
            self.assertEqual(len(s['curves']),count)
            self.assertEqual(p['source_graph']['nodes'][0]['geometry']['profile_representation'],version)


class CleanupTests(unittest.TestCase):
    def cleanup(self, tri, volume=1/6, box=BOX):
        return bounded_cleanup(tri,volume,box,.02)

    def test_small_winding_error_is_repaired_and_roundtrips(self):
        tri=TETRA.copy();tri[2]=tri[2,::-1]
        output,audit=self.cleanup(tri)
        self.assertEqual(audit['reoriented_facet_indices'],[2])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'out.stl';write_binary_stl(path,output)
            actual=HighLevelOperations._load_stl_triangles(str(path),100)
            self.assertTrue(inspect_mesh(actual,1/6,BOX,.02)['passed'])

    def test_local_seam_and_one_collapsed_facet(self):
        tri=TETRA.copy();tri[0,0,0]=1e-8
        tri=np.concatenate([tri, np.array([[[0,0,0],[1e-8,0,0],[0,1,0]]])])
        output,audit=self.cleanup(tri)
        self.assertEqual(audit['removed_collapsed_facet_indices'],[4])
        self.assertLessEqual(audit['maximum_vertex_displacement_mm'],.00006)
        self.assertTrue(inspect_mesh(output,1/6,BOX,.02)['passed'])

    def test_holes_duplicates_nonmanifold_and_inward_shell_rejected(self):
        bad=[TETRA[:-1],np.concatenate([TETRA,TETRA[:1]]),TETRA[:,::-1],
             np.concatenate([TETRA,-TETRA[:,::-1]])]
        for tri in bad:
            with self.subTest(facets=len(tri)),self.assertRaises(ProgramError):self.cleanup(tri)

    def test_multiple_and_inner_shells_are_never_globally_reoriented(self):
        for tri,volume in [(np.concatenate([TETRA,TETRA+2]),1/3),
                           (np.concatenate([TETRA,(TETRA*.1+.1)[:,::-1]]),(1-.001)/6)]:
            with self.assertRaises(ProgramError):self.cleanup(tri,volume)
        hollow=np.concatenate([TETRA,(TETRA*.1+.1)[:,::-1]])
        self.assertTrue(inspect_mesh(hollow,(1-.001)/6,BOX,.02)['passed'])

    def test_limit_rejects_coarse_welding_far_from_origin(self):
        tri=TETRA+1000;tri[0,0,0]+=.00008
        with self.assertRaises(ProgramError) as cm:self.cleanup(tri)
        self.assertIn('displacement',str(cm.exception))

    def test_nonfinite_and_excess_degenerate_facets_rejected(self):
        tri=TETRA.copy();tri[0,0,0]=math.nan
        for bad in (tri,np.concatenate([TETRA,np.zeros((2,3,3))])):
            with self.assertRaises(ProgramError):self.cleanup(bad)

    def test_diagnostic_samples_are_bounded_and_identify_raw_facets(self):
        d=mesh_diagnostics(TETRA[:-1],limit=2)
        self.assertEqual(len(d['edge_samples']),2)
        self.assertTrue(all(e['incident_count']==1 for e in d['edge_samples']))
        self.assertTrue(all(0<=i<3 for e in d['edge_samples'] for i in e['facet_indices']))


class EditCompatibilityTests(unittest.TestCase):
    def test_fast_context_performs_no_inventory_walk(self):
        record={'native':{'ownership':{'A':{}},'feature_inventory':{'A':'Revolve'}}}
        with patch('solidworks_mcp.automation.shape_edit_compatibility.feature_inventory',side_effect=AssertionError):
            result=edit_compatibility(None,None,record)
        self.assertEqual(result['status'],'NOT_CHECKED')
        self.assertEqual(result['topology_edit'],'CONDITIONAL')

    def test_added_fillet_blocks_topology_without_adopting_it(self):
        record={'native':{'ownership':{'A':{}},'feature_inventory':{'A':'Revolve'}}}
        with patch('solidworks_mcp.automation.shape_edit_compatibility.feature_inventory',
                   return_value={'A':'Revolve','VisorLipFillet':'Fillet'}):
            result=edit_compatibility(None,None,record,True)
        self.assertEqual(result['topology_edit'],'BLOCKED')
        self.assertEqual(result['added'],{'VisorLipFillet':'Fillet'})
        self.assertEqual(record['native']['feature_inventory'],{'A':'Revolve'})


if __name__=='__main__':unittest.main()
