"""Surface dependencies, symbolic anchors and retained native recipe versions."""
import math,unittest
from solidworks_mcp.shape_program import run_program,ProgramError
from solidworks_mcp.shape_equations import plan_recipes,topology
from solidworks_mcp.shape_reconcile import reconcile_plan

BASE='''
H=model.parameter('H',100)
Gap=model.parameter('Gap',.25)
host=model.sectioned_solid('Host',[(0,.2*H,.15*H),(.5*H,.24*H,.18*H)])
p=model.surface_point(host,fraction=.5,angle=pi/2)
mask=model.rounded_prism('Mask',H/10,H/10,H/10,H/100,center=p)
'''
TAIL="model.surface_patch('Patch',host,mask,2,offset=Gap)"
def plan(code,values=None):
    p=run_program(code,values)
    return plan_recipes(p,{k:f'D{i+1}@ShapeParameters' for i,k in enumerate(p['parameters'])})

class SurfacePlanningTests(unittest.TestCase):
    def test_reserved_native_parameter_is_mapped_without_changing_public_name(self):
        code=BASE+"\nWall=model.parameter('Thickness',2)\nmodel.surface_patch('Patch',host,mask,Wall,offset=Gap)"
        self.assertEqual(plan(code)[-1]['surface']['thickness_expression'],'"ShapeParameter_Thickness"')
        with self.assertRaises(ProgramError) as error:
            plan(code+"\nmodel.parameter('ShapeParameter_Thickness',3)")
        self.assertEqual(error.exception.code,'NATIVE_PARAMETER_COLLISION')

    def test_anchor_scales_natively(self):
        for h in [100,110]:
            p=run_program(BASE,{'H':h});point=p['nodes'][1]['geometry']['center']
            self.assertAlmostEqual(point[0],.22*h)
            self.assertAlmostEqual(point[1],.25*h)
            self.assertAlmostEqual(point[2],0,places=9)
        a,b=plan(BASE),plan(BASE,{'H':110})
        self.assertEqual(a[1]['sketches'],b[1]['sketches'])

    def test_patch_depends_on_host_but_consumes_only_mask(self):
        patch=plan(BASE+TAIL)[-1]
        self.assertEqual(patch['seeds'],['Host','Mask'])
        self.assertEqual(patch['consumes'],['Mask'])
        run_program(BASE+TAIL+"\nmodel.mirror('HostPair',[host])")
        with self.assertRaises(ProgramError) as err:
            run_program(BASE+TAIL+"\nmodel.mirror('MaskPair',[mask])")
        self.assertEqual(err.exception.code,'COMPONENT_CONSUMED')

    def test_host_topology_change_rebuilds_patch_and_keeps_witness(self):
        base=BASE+TAIL+"\nmodel.dome('Witness',4,2)"
        other=base.replace("p=model.surface_point(host,fraction=.5,angle=pi/2)","p=(22,25,0)")
        other=other.replace("[(0,.2*H,.15*H),(.5*H,.24*H,.18*H)]",
                            "[(0,.2*H,.15*H),(.2*H,.22*H,.16*H),(.5*H,.24*H,.18*H)]")
        diff=reconcile_plan(plan(base),plan(other))
        self.assertEqual(diff['build'],['Host','Patch'])
        self.assertIn('Witness',diff['preserve'])

    def test_gap_remains_a_native_independent_equation(self):
        a,b=plan(BASE+TAIL),plan(BASE+TAIL,{'H':110})
        self.assertEqual(a[-1]['surface']['offset_expression'],'"Gap"')
        self.assertEqual(topology(a),topology(b))

    def test_offset_sign_and_thicken_direction_are_topology(self):
        positive=plan(BASE+TAIL)
        negative=plan(BASE+TAIL.replace('offset=Gap','offset=-Gap,inward=True'))
        self.assertNotEqual(topology(positive),topology(negative))
        self.assertEqual(negative[-1]['surface']['offset'],.25)

    def test_invalid_patch_rejected_without_com(self):
        for tail,code in [
            ("model.surface_patch('Patch',host,mask,0)",'INVALID_THICKNESS'),
            ("model.surface_patch('Patch',mask,host,1)",'UNSUPPORTED_SURFACE_HOST'),
            ("model.surface_point(host,fraction=2)",'INVALID_SURFACE_ANCHOR'),
            ("model.surface_point(mask)",'UNSUPPORTED_SURFACE_ANCHOR')]:
            with self.subTest(code=code),self.assertRaises(ProgramError) as error:run_program(BASE+tail)
            self.assertEqual(error.exception.code,code)

    def test_new_profiles_close_at_exact_quarter_endpoints(self):
        p=plan(BASE)[0]
        for sketch in p['sketches']:
            endpoints=[]
            for curve in sketch['curves']:
                def point(t):
                    return [eval(x.replace('^','**').replace('"D1@ShapeParameters"','100'),{'t':t}) for x in curve['xyz']]
                endpoints.append((point(0),point(1)))
            self.assertEqual(len(endpoints),4)
            for i,(_,end) in enumerate(endpoints):self.assertEqual(end,endpoints[(i+1)%4][0])

    def test_legacy_section_recipe_is_retained_for_existing_native_ids(self):
        p=run_program(BASE,recipe_versions={'Host':'trig_halves'})
        node=plan_recipes(p,{'H':'D1@ShapeParameters','Gap':'D2@ShapeParameters'})[0]
        self.assertTrue(all(len(s['curves'])==2 for s in node['sketches']))

if __name__=='__main__':unittest.main()
