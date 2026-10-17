"""Dependency closure, semantic identity and ownership guards."""
import copy,unittest
from solidworks_mcp.shape_program import run_program,ProgramError
from solidworks_mcp.shape_equations import plan_recipes
from solidworks_mcp.shape_reconcile import reconcile_plan
from solidworks_mcp.automation.shape_reconcile import prepare_reconcile

BASE='a=model.dome("A",4,2)\nb=model.dome("B",4,2)\nw=model.dome("Witness",2,1)\n'
TAIL='c=model.combine("C",[a,b])\nmodel.mirror("M",[c])'
def specs(code):return plan_recipes(run_program(code),{})

class ReconcileTests(unittest.TestCase):
    def test_append_preserves_existing_components(self):
        p=reconcile_plan(specs(BASE),specs(BASE+TAIL))
        self.assertEqual(p['remove'],[])
        self.assertEqual(p['build'],['C','M'])
        self.assertEqual(p['preserve'],['A','B','Witness'])

    def test_change_family_rebuilds_only_dependent_closure(self):
        changed=(BASE+TAIL).replace('model.dome("A",4,2)','model.ellipsoid("A",(4,2,3))')
        p=reconcile_plan(specs(BASE+TAIL),specs(changed))
        self.assertEqual(p['remove'],['M','C','A'])
        self.assertEqual(p['build'],['A','C','M'])
        self.assertEqual(p['preserve'],['B','Witness'])

    def test_definition_change_does_not_recreate_native_features(self):
        p=reconcile_plan(specs(BASE+TAIL),specs((BASE+TAIL).replace('4,2','5,2')))
        self.assertEqual(p['remove'],[])
        self.assertEqual(p['build'],[])

    def test_delete_dependents_restores_inputs(self):
        p=reconcile_plan(specs(BASE+TAIL),specs(BASE))
        self.assertEqual(p['remove'],['M','C'])
        self.assertEqual(p['build'],[])
        self.assertEqual(p['deleted'],['C','M'])

    def test_new_node_on_host_consumed_by_saved_combine_rebuilds_that_combine(self):
        head='H = model.parameter("H", 150)\nu = H / 150\n'
        v1=head+('body = model.sectioned_solid("Body", [(0, 10*u, 10*u), (30*u, 10*u, 10*u)])\n'
                 'cap = model.dome("Cap", radius=10*u, height=10*u, center=(0, 30*u, 0))\n'
                 'shell = model.combine("Shell", [body, cap])\n')
        v2=head+('body = model.sectioned_solid("Body", [(0, 10*u, 10*u), (30*u, 10*u, 10*u)])\n'
                 'mask = model.rounded_prism("Mask", 12*u, 6*u, 16*u, 2*u, center=(0, 12*u, 8*u))\n'
                 'pocket = model.surface_patch("Pocket", body, mask, 2*u, inward=True)\n'
                 'cap = model.dome("Cap", radius=10*u, height=10*u, center=(0, 30*u, 0))\n'
                 'shell = model.combine("Shell", [body, cap])\n'
                 'model.combine("Carved", [shell, pocket], operation="difference")\n')
        dims={'H':'D1@ShapeParameters'}
        p=reconcile_plan(plan_recipes(run_program(v1),dims),plan_recipes(run_program(v2),dims))
        self.assertEqual(p['remove'],['Shell'])
        self.assertEqual(p['build'],['Mask','Pocket','Shell','Carved'])
        self.assertEqual(p['preserve'],['Body','Cap'])
        self.assertEqual(p['added'],['Mask','Pocket','Carved'])

    def test_host_consumers_propagate_to_their_dependents(self):
        base=('b=model.sectioned_solid("Body",[(0,10,10),(30,10,10)])\n'
              'c=model.dome("Cap",10,10,center=(0,30,0))\n')
        tail='s=model.combine("Shell",[b,c])\nmodel.mirror("Twin",[s])\n'
        patch=('m=model.rounded_prism("Mask",12,6,16,2,center=(0,12,8))\n'
               'model.surface_patch("Skin",b,m,1,offset=1)\n')
        p=reconcile_plan(specs(base+tail),specs(base+patch+tail))
        self.assertEqual(p['remove'],['Twin','Shell'])
        self.assertEqual(p['build'],['Mask','Skin','Shell','Twin'])
        self.assertEqual(p['preserve'],['Body','Cap'])

    def test_live_host_needs_no_extra_rebuild(self):
        base=('b=model.sectioned_solid("Body",[(0,10,10),(30,10,10)])\n'
              'w=model.dome("Witness",2,1,center=(40,0,0))\n')
        patch=('m=model.rounded_prism("Mask",12,6,16,2,center=(0,12,8))\n'
               'model.surface_patch("Skin",b,m,1,offset=1)\n')
        p=reconcile_plan(specs(base),specs(base+patch))
        self.assertEqual(p['remove'],[])
        self.assertEqual(p['build'],['Mask','Skin'])

    def test_path_point_count_is_a_topology_change(self):
        old=specs('model.tube("A",[(0,0,0),(0,10,0)],1)')
        new=specs('model.tube("A",[(0,0,0),(1,5,0),(0,10,0)],1)')
        self.assertEqual(reconcile_plan(old,new)['build'],['A'])

    def test_native_inventory_is_required_for_destructive_reconcile(self):
        old=run_program(BASE);new=run_program(BASE+TAIL)
        record={'program':old,'native':{'parameter_bindings':{}}}
        with self.assertRaises(ProgramError) as error:prepare_reconcile(record,new)
        self.assertEqual(error.exception.code,'TOPOLOGY_MAPPING_MISSING')

    def test_old_records_allow_same_topology_edits(self):
        record={'program':run_program(BASE),'native':{'parameter_bindings':{}}}
        p,_,_=prepare_reconcile(record,run_program(BASE.replace('4,2','5,2')))
        self.assertEqual(p['build'],[])

    def test_boolean_retarget_invalidates_descendants(self):
        old=specs(BASE+TAIL);new=copy.deepcopy(old)
        new[3]['seeds']=['A','Witness']
        p=reconcile_plan(old,new)
        self.assertEqual(p['build'],['C','M'])

    def test_reordering_independent_nodes_preserves_identities(self):
        old=specs(BASE);new=[old[2],old[0],old[1]]
        self.assertEqual(reconcile_plan(old,new)['build'],[])

    def test_new_parameters_get_separate_driver_block(self):
        record={'program':run_program('model.dome("A",4,2)'),
                'native':{'parameter_bindings':{},'ownership':{'A':{}},'feature_inventory':{'A':'Revolve'}}}
        program=run_program('H=model.parameter("H",100)\nGap=model.parameter("Gap",.25)\nmodel.dome("A",H/25+Gap,2)')
        p,_,after=prepare_reconcile(record,program)
        self.assertEqual(p['added_parameters'],['H','Gap'])
        self.assertEqual(p['parameter_block'],'ShapeParameters_H')
        self.assertIn('@ShapeParameters_H',str(after))

    def test_removed_parameter_is_rejected(self):
        record={'program':run_program('H=model.parameter("H",100)\nmodel.dome("A",4,2)'),
                'native':{'parameter_bindings':{'H':{'dimension':'D1@ShapeParameters','unit':'mm'}}}}
        with self.assertRaises(ProgramError) as error:prepare_reconcile(record,run_program('model.dome("A",4,2)'))
        self.assertEqual(error.exception.code,'PARAMETER_SCHEMA_CHANGE')

    def test_added_parameter_block_cannot_shadow_a_component(self):
        record={'program':run_program('model.dome("A",4,2)'),
                'native':{'parameter_bindings':{},'ownership':{'A':{}},'feature_inventory':{'A':'Revolve'}}}
        program=run_program('H=model.parameter("H",100)\nmodel.dome("A",4,2)\nmodel.dome("ShapeParameters_H",4,2)')
        with self.assertRaises(ProgramError) as error:prepare_reconcile(record,program)
        self.assertEqual(error.exception.code,'NATIVE_NAME_COLLISION')

if __name__=='__main__':unittest.main()
