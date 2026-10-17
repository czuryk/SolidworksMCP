"""Reject stale body transforms and bound the native settling pass."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from solidworks_mcp.shape_program import ProgramError
from solidworks_mcp.automation.shape_placement import pending_placements,require_placement_ready,settle_placements

MODULE='solidworks_mcp.automation.shape_placement.'


class PlacementStateTests(unittest.TestCase):
    def setUp(self):
        self.native={'P':{'steps':[{'feature':'P','targets':{'angle':'Angle@P'}}]}}
        self.definition=SimpleNamespace(TransformX=.4)
        self.feature=SimpleNamespace(GetDefinition=self.definition)
        self.dimension=SimpleNamespace(SystemValue=.7)
        self.doc=SimpleNamespace(FeatureByName=Mock(return_value=self.feature),
            Parameter=Mock(return_value=self.dimension),ForceRebuild3=Mock(return_value=True))
        patcher=patch(MODULE+'typed',side_effect=lambda obj,name:obj);patcher.start();self.addCleanup(patcher.stop)

    def test_dimension_success_is_not_native_geometry_success(self):
        with self.assertRaises(ProgramError) as error:require_placement_ready(self.doc,self.native)
        self.assertEqual(error.exception.code,'NATIVE_REBUILD_PENDING')
        self.doc.ForceRebuild3.assert_not_called()

    def test_ready_read_does_not_rebuild(self):
        self.definition.TransformX=.7
        self.assertEqual(pending_placements(self.doc,self.native),[])
        self.assertEqual(settle_placements(self.doc,self.native),0)
        self.doc.ForceRebuild3.assert_not_called()

    def test_second_pass_is_bounded_and_verified(self):
        self.doc.GetEquationMgr=SimpleNamespace(EvaluateAll=Mock())
        self.doc.ForceRebuild3.side_effect=lambda _:setattr(self.definition,'TransformX',.7) or True
        self.assertEqual(settle_placements(self.doc,self.native),1)
        self.doc.ForceRebuild3.assert_called_once_with(False)

    def test_still_stale_after_second_pass_is_error(self):
        self.doc.GetEquationMgr=SimpleNamespace(EvaluateAll=Mock())
        with self.assertRaises(ProgramError):settle_placements(self.doc,self.native)
        self.doc.ForceRebuild3.assert_called_once_with(False)


if __name__=='__main__':unittest.main()
