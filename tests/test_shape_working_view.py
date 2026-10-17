"""Working-view errors are bounded and cannot break a geometry operation."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp.automation.shape_working_view import focus_bodies, frame_bounds, changed_body_names

MODULE = 'solidworks_mcp.automation.shape_working_view.'


class WorkingViewTests(unittest.TestCase):
    def test_changed_inputs_follow_consumption_and_mirror_dependencies(self):
        before = {'nodes': [
            {'id':'Left','kind':'dome','geometry':{'height':2}},
            {'id':'Moved','kind':'place','geometry':{'seeds':['Left']}},
            {'id':'Right','kind':'mirror','geometry':{'seeds':['Moved']}}]}
        after = deepcopy(before)
        after['nodes'][0]['geometry']['height'] = 3
        components = {'Left': {'bodies':[], 'consumed_by':'Moved'},
                      'Moved': {'bodies':['MovedBody']}, 'Right': {'bodies':['MirroredBody']}}
        self.assertEqual(changed_body_names(before,after,components), ['MirroredBody','MovedBody'])
        self.assertEqual(changed_body_names(before,before,components), [])

    def test_target_scope_ignores_hidden_and_unrelated_bodies(self):
        bodies = [SimpleNamespace(Name='Target',Visible=True,GetBodyBox=[1,2,3,4,5,6]),
                  SimpleNamespace(Name='Hidden',Visible=False,GetBodyBox=[-99]*6),
                  SimpleNamespace(Name='Other',Visible=True,GetBodyBox=[-20]*6)]
        sw=Mock(); sw._get_solid_bodies.return_value=bodies
        with patch(MODULE+'frame_bounds', return_value={'verified':True}) as frame:
            result=focus_bodies(sw,object(),['Target','Hidden'])
        self.assertEqual(result['bodies'],['Target'])
        self.assertEqual(frame.call_args.args[-2:],([1,2,3],[4,5,6]))

    def test_camera_exception_is_reported_without_escaping(self):
        sw=Mock(); sw._get_solid_bodies.side_effect=RuntimeError('View unavailable')
        result=focus_bodies(sw,object())
        self.assertFalse(result['verified'])
        self.assertEqual(result['status'],'WORKING_VIEW_FAILED')

    def test_scale_side_effect_is_recentered_with_custom_orientation(self):
        class View:
            Orientation3 = 'custom'
            Translation3 = SimpleNamespace(ArrayData=[99,88,77])
            HasPerspective = False
            _scale = 4.
            @property
            def Scale2(self): return self._scale
            @Scale2.setter
            def Scale2(self,value):
                self._scale=value
                self.Translation3=SimpleNamespace(ArrayData=[-100,-100,-100])
        view=View(); doc=SimpleNamespace(ActiveView=view,GraphicsRedraw2=Mock())
        sw=Mock(); sw.app=SimpleNamespace(GetMathUtility=object())
        def orient(*args,**kwargs):
            view.Orientation3='front'
            return {'success':True}
        sw.set_view_orientation.side_effect=orient
        def zoom(*args,**kwargs):
            view.Scale2=2.
            view.Translation3=SimpleNamespace(ArrayData=[-5.9,-11.8,-17.7])
        sw._apply_zoom_bbox_m.side_effect=zoom
        sw._fit_measurement.side_effect=[{'verified':False,'dominant_fill_ratio':1.1},
                                        {'verified':True,'dominant_fill_ratio':.55}]
        def point(data):
            xyz=list(data.value)
            return SimpleNamespace(MultiplyTransform=lambda orientation:SimpleNamespace(
                ArrayData=xyz if orientation=='front' else [xyz[1],-xyz[0],xyz[2]]))
        utility=SimpleNamespace(CreatePoint=point,
            CreateVector=lambda data:SimpleNamespace(ArrayData=list(data.value)))
        with patch(MODULE+'typed',return_value=utility):
            result=frame_bounds(sw,doc,[2,5,8],[4,7,10])
        self.assertEqual(result['attempts'],2)
        self.assertTrue(result['verified'])
        self.assertEqual(view.Orientation3,'custom')
        for got,want in zip(view.Translation3.ArrayData,[-5.9,3.2,-8.7]):
            self.assertAlmostEqual(got,want)
        self.assertEqual(view.Scale2,1.)
        self.assertEqual(sw.set_view_orientation.call_count,1)
        self.assertEqual(sw._fit_measurement.call_count,2)

    def test_perspective_is_reported_without_camera_mutation(self):
        view=SimpleNamespace(Orientation3=object(),Translation3=object(),Scale2=2.,HasPerspective=True)
        sw=Mock()
        result=frame_bounds(sw,SimpleNamespace(ActiveView=view),[0,0,0],[1,1,1])
        self.assertFalse(result['verified'])
        sw.set_view_orientation.assert_not_called()


if __name__=='__main__': unittest.main()
