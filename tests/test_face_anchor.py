"""Deferred B-rep anchors, native dependencies and ambiguous ray contracts."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from solidworks_mcp.shape_program import run_program, ProgramError
from solidworks_mcp.shape_equations import plan_recipes, topology
from solidworks_mcp.shape_reconcile import reconcile_plan
from solidworks_mcp.automation.shape_anchor import first_hit, validate_anchor_links
from solidworks_mcp.automation.shape_service import ShapeProgramOperations

SOURCE = '''R=model.parameter('R',10)
X=model.parameter('X',2)
host=model.ellipsoid('Host',(R,2*R,.6*R))
anchor=model.face_anchor(host,origin=(X,3,0),direction=(0,0,1))
model.anchored_extrude('Boss',anchor,{'circle':R/5},R/4)
'''


def plan(code=SOURCE, overrides=None):
    p=run_program(code,overrides)
    return plan_recipes(p,{k:f'D{i+1}@ShapeParameters' for i,k in enumerate(p['parameters'])})


class FaceAnchorTests(unittest.TestCase):
    def test_deferred_attachment_has_no_sampled_coordinates(self):
        a,b=plan(),plan(overrides={'R':12,'X':3})
        self.assertEqual(a[1]['sketches'],b[1]['sketches'])
        self.assertEqual(topology(a),topology(b))
        self.assertEqual(a[1]['seeds'],['Host'])
        self.assertEqual(a[1]['sketches'][1]['mode'],'anchor')
        self.assertNotIn('consumes',a[1])
        self.assertEqual(b[1]['anchor']['origin'],[3,3,0])

    def test_anchor_is_lazy_and_cannot_be_a_body_or_xyz(self):
        self.assertEqual(len(plan(SOURCE.split('model.anchored_extrude')[0])),1)
        for expression in ["model.place('B',host,frame=anchor)","model.dome('B',2,1,center=anchor)"]:
            with self.assertRaises(ProgramError):plan(SOURCE+expression)

    def test_direction_and_depth_sign_change_rebuild_only_dependents(self):
        for source in [SOURCE.replace('(0,0,1)','(1,0,1)'),SOURCE.replace('R/4)','-R/4)')]:
            result=reconcile_plan(plan(),plan(source))
            self.assertEqual(result['preserve'],['Host'])
            self.assertEqual(result['build'],['Boss'])

    def test_host_topology_change_rebuilds_attachment(self):
        new=SOURCE.replace("model.ellipsoid('Host',(R,2*R,.6*R))", "model.dome('Host',R,R)")
        self.assertEqual(reconcile_plan(plan(),plan(new))['build'],['Host','Boss'])

    def test_invalid_inputs_are_rejected_before_com(self):
        for source in [SOURCE.replace('(0,0,1)','(0,0,0)'), SOURCE.replace('(0,0,1)','(0,0,R)'),
                       SOURCE.replace("{'circle':R/5}","{'circle':-1}"), SOURCE.replace('R/4)','0)'),
                       SOURCE.replace("{'circle':R/5}","{'rounded_rectangle':(2,2,1)}")]:
            with self.assertRaises(ProgramError):plan(source)

    def test_consumed_anchor_host_rejected(self):
        source=SOURCE.replace("model.anchored_extrude", "other=model.dome('Other',4,2)\nmodel.combine('Union',[host,other])\nmodel.anchored_extrude")
        with self.assertRaises(ProgramError) as error:plan(source)
        self.assertEqual(error.exception.code,'COMPONENT_CONSUMED')

    def test_native_support_names_reserved(self):
        for suffix in ['_Ray','_Axis','_Anchor','_Frame','_Profile']:
            with self.assertRaises(ProgramError) as error:plan(SOURCE+f"model.dome('Boss{suffix}',2,1)")
            self.assertEqual(error.exception.code,'NATIVE_NAME_COLLISION')

    def test_profile_equations_stay_symbolic(self):
        for profile,count in [("{'ellipse':(R/5,R/6)}",4),("{'rounded_rectangle':(R,R/2,R/10)}",8)]:
            item=plan(SOURCE.replace("{'circle':R/5}",profile))[1]
            curves=item['sketches'][1]['curves']
            self.assertEqual(len(curves),count)
            self.assertTrue(any('ShapeParameters' in e for c in curves for e in c['xyz']))

    def test_first_hit_is_body_scoped_forward_and_unambiguous(self):
        row=[0,0,2,0,0,.01,0,0,1]
        self.assertEqual(first_hit(row,1,[0,0,0],[0,0,1],1,2),[0,0,10])
        for rows,count in [(row+row,2),([0,0,3]+row[3:],1),([0,0,2,0,0,.01,1,0,0],1),([],0)]:
            with self.assertRaises(ProgramError):first_hit(rows,count,[0,0,0],[0,0,1],1,2)

    def test_public_metadata_does_not_leak_native_selection_ids(self):
        native={'components':{'A':{'bodies':['A'],'native_anchor':{'refs':['secret']},
                                  'native_profile':{'relations':['secret']},'attachment':'NATIVE_FACE_INTERSECTION'}}}
        public=ShapeProgramOperations._public_shape_native(native)
        self.assertNotIn('secret',str(public))
        self.assertIn('native_anchor',native['components']['A'])

    def test_native_links_cannot_be_replaced_by_coincident_frozen_geometry(self):
        constants={'swAxisTypeTwoPoints':2,'swRefPointIntersection':6,
                   'swRefPlaneReferenceConstraint_Perpendicular':2,'swRefPlaneReferenceConstraint_Coincident':4}
        binding={'ray':'Boss_Ray','point':'Boss_Anchor','frame':'Boss_Frame',
                 'point_parents':['Axis','Host'],
                 'refs':{'Boss_Ray':'Ray','Boss_Axis':'Axis','Boss_Anchor':'Point','Boss_Frame':'Frame'}}
        for tamper in [None,'axis_parent','axis_type','point_type','point_host','plane_constraint','plane_reference']:
            constraints=[2,4,0];references=['Axis','Point',None]
            axis=SimpleNamespace(Type=1 if tamper=='axis_type' else 2)
            point=SimpleNamespace(Type=7 if tamper=='point_type' else 6,Selections=['Axis','Face'])
            frame=SimpleNamespace(Constraint=lambda i:constraints[i],Reference=lambda i:references[i])
            if tamper=='plane_constraint':constraints[1]=8
            if tamper=='plane_reference':references[1]='FrozenPoint'
            features={'Boss_Axis':SimpleNamespace(GetParents=['FrozenRay'] if tamper=='axis_parent' else ['Ray'],GetDefinition=axis),
                      'Boss_Anchor':SimpleNamespace(GetParents=['Axis','OtherHost'] if tamper=='point_host' else ['Axis','Host'],GetDefinition=point),
                      'Boss_Frame':SimpleNamespace(GetDefinition=frame)}
            automation=SimpleNamespace(_find_feature=lambda doc,name:features[name])
            with self.subTest(tamper=tamper), patch('solidworks_mcp.automation.shape_anchor.typed',side_effect=lambda obj,interface:obj), \
                 patch('solidworks_mcp.automation.shape_anchor.persist',side_effect=lambda doc,obj:obj), \
                 patch('solidworks_mcp.automation.shape_anchor.resolve_solidworks_constant',side_effect=constants.__getitem__):
                if tamper:
                    with self.assertRaises(ProgramError) as error:validate_anchor_links(automation,object(),'Boss',binding)
                    self.assertEqual(error.exception.code,'NATIVE_MAPPING_STALE')
                else:validate_anchor_links(automation,object(),'Boss',binding)


if __name__=='__main__':unittest.main()
