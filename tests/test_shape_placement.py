"""Frame algebra, native planning and dependency contracts."""
import math
import unittest
import numpy as np
from solidworks_mcp.shape_program import ShapeProgram, ProgramError, resolve, run_program
from solidworks_mcp.shape_equations import plan_recipes, topology
from solidworks_mcp.shape_reconcile import reconcile_plan


def plans(code,values=None):
    p=run_program(code,values)
    return plan_recipes(p,{k:f'D{i+1}@ShapeParameters' for i,k in enumerate(p['parameters'])})


class PlacementTests(unittest.TestCase):
    def test_frame_order_against_independent_matrix(self):
        m=ShapeProgram();a=m.parameter('A',.3,unit='ratio')
        f=m.frame(origin=(4,5,6),rotation=(a,-.7,1.2))
        rx=np.array([[1,0,0],[0,math.cos(.3),-math.sin(.3)],[0,math.sin(.3),math.cos(.3)]])
        ry=np.array([[math.cos(-.7),0,math.sin(-.7)],[0,1,0],[-math.sin(-.7),0,math.cos(-.7)]])
        rz=np.array([[math.cos(1.2),-math.sin(1.2),0],[math.sin(1.2),math.cos(1.2),0],[0,0,1]])
        expected=rz@ry@rx@np.array([3,7,-2])
        np.testing.assert_allclose(resolve(f.direction((3,7,-2)),m.values),expected,atol=1e-12)
        np.testing.assert_allclose(resolve(f.point((3,7,-2)),m.values),expected+[4,5,6],atol=1e-12)

    def test_cardinal_rotation_exact(self):
        f=ShapeProgram().frame(rotation=(0,math.pi/2,0))
        self.assertEqual(resolve(f.point((1,2,3)),{}),[3,2,-1])

    def test_native_axes_and_signs_and_parameter_edits(self):
        code="A=model.parameter('A',.5,unit='ratio')\nH=model.parameter('H',40)\nb=model.dome('Seed',5,3)\nmodel.place('Placed',b,frame=model.frame(origin=(-H,H/2,0),rotation=(A,0,-A/2)))"
        a,b=plans(code),plans(code,{'A':.6,'H':44})
        self.assertEqual(topology(a),topology(b))
        steps=a[-1]['transform']
        self.assertEqual([(x['kind'],x.get('axis'),x.get('direction')) for x in steps],[('rotate','X',1),('rotate','Z',-1),('translate',None,None)])
        self.assertIn('"A"',steps[0]['expression'])
        self.assertTrue(steps[0]['expression'].endswith(' * 1deg'))
        self.assertEqual(steps[-1]['axes']['X']['direction'],-1)
        self.assertEqual(a[-1]['consumes'],['Seed'])

    def test_domain_and_identity_fail_before_com(self):
        for frame in ['model.frame()', 'model.frame(rotation=(2*pi,0,0))',"model.frame(rotation=(A-A+1e-10,0,0))"]:
            with self.assertRaises(ProgramError):
                plans("A=model.parameter('A',1,unit='ratio')\nb=model.dome('Seed',5,3)\nmodel.place('Placed',b,frame="+frame+")")

    def test_axis_topology_rebuilds_dependents_only(self):
        code="b=model.dome('Seed',5,3)\np=model.place('Placed',b,frame=model.frame(rotation=(.4,0,0)))\nmodel.mirror('Pair',[p])\nmodel.dome('Other',3,2,center=(20,0,0))"
        plan=reconcile_plan(plans(code),plans(code.replace('(.4,0,0)','(.4,.2,0)')))
        self.assertEqual(plan['build'],['Placed','Pair'])
        self.assertEqual(plan['preserve'],['Seed','Other'])

    def test_consumed_source_cannot_be_reused(self):
        with self.assertRaises(ProgramError) as error:
            run_program("b=model.dome('Seed',5,3)\nmodel.place('Placed',b,frame=model.frame(origin=(3,0,0)))\nmodel.mirror('Invalid',[b])")
        self.assertEqual(error.exception.code,'COMPONENT_CONSUMED')

    def test_support_name_collision(self):
        with self.assertRaises(ProgramError) as error:
            plans("b=model.dome('Placed_Axes',5,3)\nmodel.place('Placed',b,frame=model.frame(rotation=(.3,0,0)))")
        self.assertEqual(error.exception.code,'NATIVE_NAME_COLLISION')

    def test_multi_body_placement_preserves_count_for_boolean_validation(self):
        with self.assertRaises(ProgramError) as error:
            run_program("a=model.dome('A',5,3)\nb=model.dome('B',5,3,center=(15,0,0))\np=model.place('P',[a,b],frame=model.frame(origin=(0,10,0)))\nc=model.dome('C',3,2)\nmodel.combine('Cut',[p,c],operation='difference')")
        self.assertEqual(error.exception.code,'INVALID_BOOLEAN_TARGET')

    def test_surface_anchor_round_and_elliptical(self):
        m=ShapeProgram();h=m.parameter('H',40)
        b=m.ellipsoid('E',(h/4,h/2,h/5),center=(3,4,5))
        np.testing.assert_allclose(resolve(m.surface_point(b,fraction=.75,angle=math.pi/2),m.values),[3+10*math.sqrt(.75),14,5])
        d=m.dome('D',10,5)
        p=resolve(m.surface_point(d,fraction=.5),m.values)
        self.assertAlmostEqual(p[2]**2+(p[1]+7.5)**2,12.5**2)
        self.assertEqual(resolve(m.surface_point(d,fraction=1),m.values),[0,5,0])

    def test_placed_anchor_and_path_endpoints_follow_frame(self):
        m=ShapeProgram();b=m.tube('Tube',[(1,2,3),(4,7,9),(9,8,11)],1)
        f=m.frame(origin=(10,20,30),rotation=(0,math.pi/2,0));p=m.place('Placed',b,frame=f)
        self.assertEqual(resolve(m.path_point(p,segment=0,fraction=0),{}),resolve(p.start,{}))
        self.assertEqual(resolve(m.path_point(p,segment=1,fraction=1),{}),resolve(p.end,{}))
        left=resolve(m.path_tangent(p,segment=0,fraction=1),{})
        right=resolve(m.path_tangent(p,segment=1,fraction=0),{})
        np.testing.assert_allclose(left,right,atol=1e-12)
        self.assertAlmostEqual(sum(v*v for v in left),1)

    def test_path_anchor_is_hermite_not_polygon(self):
        m=ShapeProgram();p=m.tube('T',[(0,0,0),(10,10,0),(20,0,0)],1)
        self.assertEqual(resolve(m.path_point(p,fraction=.5),{}),[5,6.25,0])
        with self.assertRaises(ProgramError):m.path_point(p,segment=2)

    def test_placed_surface_anchor_tracks_parameter(self):
        m=ShapeProgram();a=m.parameter('A',.4,unit='ratio')
        e=m.ellipsoid('E',(5,10,6));f=m.frame(rotation=(a,0,0),origin=(1,2,3))
        p=m.place('P',e,frame=f)
        np.testing.assert_allclose(resolve(m.surface_point(p),m.values),[1,2-6*math.sin(.4),3+6*math.cos(.4)])


if __name__=='__main__':unittest.main()
