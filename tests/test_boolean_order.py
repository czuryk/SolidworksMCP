"""Union ordering needs native copy evidence and a clean failed feature attempt."""

import itertools
import copy
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp.automation import boolean_order as order
from solidworks_mcp.automation.shape_parametric import ParametricCompiler
from solidworks_mcp.shape_program import ProgramError


class Body:
    def __init__(self,name,box):self.Name=name;self.box=box
    def GetBodyBox(self):return self.box
    def GetMassProperties(self,density):return [0.,0.,0.,1.]
    def GetFaceCount(self):return 6
    def Copy2(self,ignored):return Body(self.Name,list(self.box))
    def Select2(self,*args):return True


def get(obj,key,*args):
    value=getattr(obj,key)
    return value(*args) if callable(value) else value


class UnionOrderTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(order,'typed',side_effect=lambda value,_:value))
        self.enterContext(patch.object(order,'com_get',side_effect=get))
        self.bodies={'Left':Body('Left',[-4,0,-1,-2,10,1]),
                     'Right':Body('Right',[2,0,-1,4,10,1]),
                     'Platform':Body('Platform',[-5,9,-2,5,11,2])}

    def merged(self,a,b):
        self.assertNotIn(a,list(self.bodies.values()))
        box=[min(a.box[i],b.box[i]) for i in range(3)]+[max(a.box[i],b.box[i]) for i in range(3,6)]
        return Body('aggregate',box),{'relation':'VALID_SINGLE_SOLID_UNION'}

    def test_all_permutations_choose_connector_on_copies(self):
        with patch.object(order,'union_copy',side_effect=self.merged):
            for names in itertools.permutations(self.bodies):
                result=order.plan_union(None,{n:self.bodies[n] for n in names})
                self.assertEqual(result['connectivity'],'CONNECTED')
                self.assertEqual(result['resolved_order'],['Platform','Left','Right'])
                self.assertEqual(result['probe_count'],2)

    def test_disconnected_bounds_need_no_com_booleans(self):
        with patch.object(order,'union_copy',side_effect=AssertionError('Unnecessary native probe')):
            result=order.plan_union(None,{n:self.bodies[n] for n in ('Left','Right')})
        self.assertEqual(result['connectivity'],'DISCONNECTED')
        self.assertEqual(result['disconnected_groups'],[['Left'],['Right']])

    def test_overlapping_boxes_and_zero_gap_never_prove_solid(self):
        with patch.object(order,'union_copy',return_value=(None,{'native_error':1})), \
             patch.object(order,'measure_pair',return_value={'relation':'CONTACT_OR_NEAR','clearance_mm':0}):
            result=order.plan_union(None,self.bodies)
        self.assertEqual(result['connectivity'],'UNKNOWN')
        self.assertEqual(len(result['unresolved_bodies']),2)

    def compiler(self,stack,partial=False):
        module='solidworks_mcp.automation.shape_parametric.'
        feature=SimpleNamespace(Name='')
        manager=SimpleNamespace(InsertCombineFeature=Mock(side_effect=[None,feature]))
        doc=SimpleNamespace(ClearSelection2=Mock(),FeatureManager=manager,GetFeatureCount=lambda:3)
        automation=SimpleNamespace(_find_body=lambda doc,name:self.bodies[name],_walk_features=lambda doc:[])
        compiler=ParametricCompiler(automation,doc);compiler.component='Frame'
        compiler.components={n:{'bodies':[n]} for n in self.bodies}
        compiler.body_names=lambda:set(self.bodies)
        compiler.bodies=lambda:list(self.bodies.values()) if manager.InsertCombineFeature.call_count<2 else [Body('result',[-5,0,-2,5,11,2])]
        stack.enter_context(patch(module+'typed',side_effect=lambda value,_:value))
        stack.enter_context(patch(module+'com_get',side_effect=get))
        stack.enter_context(patch(module+'create_select_data',return_value=None))
        stack.enter_context(patch(module+'resolve_solidworks_constant',return_value=15902))
        stack.enter_context(patch(module+'persist',side_effect=lambda doc,b:b.Name))
        stack.enter_context(patch(module+'feature_inventory',side_effect=lambda *args:
                                 {'partial':'Combine'} if partial and manager.InsertCombineFeature.call_count else {}))
        return compiler,manager

    def test_only_one_retry_after_unchanged_state_and_confirmed_order(self):
        with ExitStack() as stack:
            compiler,manager=self.compiler(stack)
            stack.enter_context(patch.object(order,'union_copy',side_effect=self.merged))
            compiler.combine({'id':'Frame','seeds':['Left','Right','Platform'],'operation':'union','expected_bodies':1})
            diagnostics=compiler.components['Frame']['boolean_diagnostics']
            self.assertEqual(manager.InsertCombineFeature.call_count,2)
            self.assertEqual(diagnostics['resolved_order'],['Platform','Left','Right'])
            self.assertEqual(diagnostics['retry_count'],1)
            self.assertEqual(compiler.components['Left']['consumed_by'],'Frame')

    def test_partial_feature_prevents_retry_and_preserves_failure_context(self):
        with ExitStack() as stack:
            compiler,manager=self.compiler(stack,partial=True)
            stack.enter_context(patch.object(order,'plan_union',side_effect=AssertionError('Unsafe retry')))
            with self.assertRaises(ProgramError) as caught:
                compiler.combine({'id':'Frame','seeds':['Left','Right','Platform'],'operation':'union','expected_bodies':1})
            self.assertEqual(manager.InsertCombineFeature.call_count,1)
            self.assertFalse(caught.exception.details['failed_attempt_state_unchanged'])
            self.assertEqual(caught.exception.details['input_order'],['Left','Right','Platform'])

    def test_native_mass_roundoff_does_not_disable_safe_retry(self):
        state={'features':{'A':'Boss'},'feature_count':1,'bodies':{'A':{
            'reference':'persistent','face_count':6,'box':[-.001]*3+[.001]*3,
            'mass':[0.,0.,0.,8e-9,2.4e-5,8e-6]+[5.333333333333332e-12]*3+[0.]*3}}}
        after=copy.deepcopy(state)
        after['bodies']['A']['mass'][8]=5.333333333333333e-12
        after['bodies']['A']['mass'][9]=1.0770580892617548e-28
        self.assertEqual(order.native_state_changes(state,after),{})
        for field,index in [('mass',3),('box',0)]:
            changed=copy.deepcopy(state)
            changed['bodies']['A'][field][index]+=1e-8*abs(changed['bodies']['A'][field][index])
            self.assertIn(field,order.native_state_changes(state,changed)['body_measurements']['A'])
        for field,value in [('reference','replacement'),('face_count',7)]:
            changed=copy.deepcopy(state);changed['bodies']['A'][field]=value
            self.assertIn(field,order.native_state_changes(state,changed)['body_measurements']['A'])


if __name__=='__main__':unittest.main()
