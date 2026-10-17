"""Observation must not deliver pixels from the wrong or a changing model."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import ExitStack, nullcontext

from PIL import Image
from solidworks_mcp.automation.shape_observation import observe, collect_facts, document_revision
from solidworks_mcp.automation.shape_parametric import observed_revision
from solidworks_mcp.shape_program import ProgramError

MODULE = 'solidworks_mcp.automation.shape_observation.'


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'part.SLDPRT'; self.path.write_bytes(b'fixture')
        self.view = SimpleNamespace(Orientation3=object(), Translation3=SimpleNamespace(ArrayData=(1,2,3)),
                                    Scale2=2., HasPerspective=False, RemovePerspective=Mock())
        self.hidden = False
        def set_hidden(e, o, value): self.hidden = value
        self.doc = SimpleNamespace(GetPathName=str(self.path), GetType=1, GetUpdateStamp=1,
            ConfigurationManager=SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name='Default')),
            ActiveView=self.view, ClearSelection2=Mock(), GraphicsRedraw2=Mock(),
            Extension=SimpleNamespace(GetUserPreferenceToggle=lambda e,o:self.hidden,
                                      SetUserPreferenceToggle=set_hidden))
        self.sw = Mock(); self.sw.app = SimpleNamespace(ActiveDoc=self.doc, GetMathUtility=object())
        self.sw.get_active_doc.return_value = (self.doc,None)
        self.sw._get_doc_path.side_effect = lambda d:d.GetPathName
        self.sw.set_view_orientation.return_value = {'success':True}
        self.sw._get_solid_bodies.return_value = []
        def capture(path, **kwargs):
            Image.new('RGB',(400,300),'gray').save(path)
            return {'success':True,'data':{}}
        self.sw.take_screenshot.side_effect = capture
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch(MODULE+'command_scope',side_effect=lambda app:nullcontext()))
        self.stack.enter_context(patch(MODULE+'read_record',return_value=None))
        self.stack.enter_context(patch(MODULE+'require_full_model_view'))
        self.stack.enter_context(patch(MODULE+'document_revision',side_effect=lambda doc,record=None:str(doc.GetUpdateStamp)))
        self.stack.enter_context(patch(MODULE+'collect_facts',return_value=([],{'min':[0,0,0],'max':[10,20,5]},[])))
        self.stack.enter_context(patch(MODULE+'resolve_solidworks_constant',return_value=1))
        self.stack.enter_context(patch(MODULE+'typed',return_value=SimpleNamespace(
            CreateVector=lambda data:SimpleNamespace(ArrayData=data.value),
            CreatePoint=lambda data:SimpleNamespace(MultiplyTransform=lambda orientation:SimpleNamespace(ArrayData=data.value)))))

    def run_observe(self, **kwargs):
        args = dict(directory=self.temp.name,document_path=str(self.path),expected_revision='1',views=['front','right'])
        args.update(kwargs)
        return observe(self.sw,**args)

    def assert_code(self, code, **kwargs):
        with self.assertRaises(ProgramError) as raised: self.run_observe(**kwargs)
        self.assertEqual(raised.exception.code,code)

    def test_missing_wrong_and_stale_target_no_capture(self):
        self.assert_code('OBSERVATION_TARGET_REQUIRED',document_path=None)
        # A path that is not the active document is activated or opened; a missing file is named.
        self.assert_code('DOCUMENT_NOT_FOUND',document_path=str(Path(self.temp.name)/'other.SLDPRT'))
        self.assert_code('STALE_REVISION',expected_revision='old')
        self.sw.take_screenshot.assert_not_called()

    def test_unique_artifacts_and_camera_restore(self):
        original = (self.view.Orientation3,self.view.Translation3,self.view.Scale2)
        a=self.run_observe(); b=self.run_observe()
        self.assertNotEqual(a['images'][0]['path'],b['images'][0]['path'])
        self.assertTrue(Path(a['images'][0]['path']).is_file())
        self.assertEqual((self.view.Orientation3,self.view.Translation3,self.view.Scale2),original)
        self.assertFalse(self.hidden)
        self.assertEqual({f['revision'] for f in a['images']},{'1'})
        self.assertEqual(len({f['native_scale'] for f in a['images']}),1)

    def test_native_change_mid_capture_discards_all_frames(self):
        original = self.sw.take_screenshot.side_effect
        def capture(*args,**kwargs):
            result=original(*args,**kwargs);self.doc.GetUpdateStamp+=1;return result
        self.sw.take_screenshot.side_effect=capture
        self.assert_code('OBSERVATION_STATE_CHANGED')
        self.assertFalse(list(Path(self.temp.name).rglob('*.png')))
        self.assertFalse(self.hidden)

    def test_active_document_switch_is_rejected(self):
        original=self.sw.take_screenshot.side_effect
        def capture(*args,**kwargs):
            result=original(*args,**kwargs)
            self.sw.app.ActiveDoc=SimpleNamespace(GetPathName='other',GetUpdateStamp=1,
                                                   ConfigurationManager=self.doc.ConfigurationManager)
            return result
        self.sw.take_screenshot.side_effect=capture
        self.assert_code('OBSERVATION_STATE_CHANGED')
        self.assertFalse(list(Path(self.temp.name).rglob('*.png')))

    def test_failed_or_unreadable_capture_not_delivered(self):
        for result in ({'success':False},{'success':True,'data':{'frame_unreadable':True}}):
            self.sw.take_screenshot.side_effect=lambda path,**kw:result
            self.assert_code('FRAME_INVALID')

    def test_camera_failure_is_not_silently_ignored(self):
        self.sw.set_view_orientation.return_value={'success':False}
        self.assert_code('CAMERA_FAILED')
        self.sw.take_screenshot.assert_not_called()

    def test_real_command_scope_restores_state_on_capture_failure(self):
        from solidworks_mcp.automation.com_utils import command_scope
        app = SimpleNamespace(CommandInProgress=False)
        def capture(*args, **kwargs):
            self.assertTrue(app.CommandInProgress)
            return {'success':False}
        self.sw.take_screenshot.side_effect = capture
        with patch(MODULE+'command_scope', command_scope), patch(
                'solidworks_mcp.automation.com_utils.typed', return_value=app):
            self.assert_code('FRAME_INVALID')
        self.assertFalse(app.CommandInProgress)
        self.assertFalse(self.hidden)

    def test_consumed_component_does_not_zoom_wrong_body(self):
        record={'native':{'components':{'Mask':{'bodies':[],'consumed_by':'Patch'}}}}
        with self.assertRaises(ProgramError) as raised: collect_facts(self.sw,self.doc,record,['Mask'])
        self.assertEqual(raised.exception.code,'COMPONENT_CONSUMED')

    def test_approximation_is_visible_in_native_inspection_facts(self):
        self.sw._get_solid_bodies.return_value=[SimpleNamespace(Name='Oval',Visible=True,GetFaces=[])]
        self.sw._body_info.return_value={'name':'Oval','bbox':{'min':[-1,-2,-3],'max':[1,2,3]}}
        self.sw._body_mass_props.return_value={'volume':25.}
        self.sw._find_feature.return_value=SimpleNamespace(GetTypeName2='Combine')
        component={'feature':'Oval','bodies':['Oval'],'kind':'ellipsoid',
                   'geometry_fidelity':'GUIDED_APPROXIMATION','continuous_error_bound_mm':None,
                   'analytic_surface_anchor':False}
        record={'model_id':'fixture','native':{'components':{'Oval':component}}}
        facts,_,_=collect_facts(self.sw,self.doc,record)
        owner=facts[0]['components'][0]
        self.assertEqual(owner['geometry_fidelity'],'GUIDED_APPROXIMATION')
        self.assertIsNone(owner['continuous_error_bound_mm'])
        self.assertFalse(owner['analytic_surface_anchor'])

    def test_revision_detects_unsaved_native_geometry_and_configuration(self):
        self.doc.GetFeatureCount=10
        record={'revision':'same','native':{'parameter_bindings':{}}}
        with patch('solidworks_mcp.automation.shape_parametric.global_values',return_value={}):
            first=observed_revision(self.doc,record)
            self.doc.GetUpdateStamp+=1
            second=observed_revision(self.doc,record)
            self.assertNotEqual(first,second)
            self.doc.ConfigurationManager.ActiveConfiguration.Name='Other'
            self.assertNotEqual(second,observed_revision(self.doc,record))

    def test_changed_image_is_not_delivered_as_native_evidence(self):
        from solidworks_mcp.server import tool_result_content
        path=Path(self.temp.name)/'frame.png'
        Image.new('RGB',(400,300),'gray').save(path)
        result=tool_result_content('observe_model',{'success':True,'message':'frame',
            'data':{'images':[{'path':str(path),'sha256':'not-the-captured-file'}]}})
        self.assertTrue(result.isError)
        self.assertFalse(any(c.type=='image' for c in result.content))
        self.assertNotIn('images',result.structuredContent['data'])

    def test_delivered_pixels_have_matching_metadata_after_resize(self):
        import base64,hashlib,io
        from solidworks_mcp.server import tool_result_content
        path=Path(self.temp.name)/'frame.png'
        Image.new('RGB',(2000,1500),'gray').save(path)
        result=tool_result_content('observe_model',{'success':True,'message':'frame',
            'data':{'images':[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}})
        frame=next(c for c in result.content if c.type=='image')
        data=base64.b64decode(frame.data);info=result.structuredContent['data']['images'][0]
        self.assertEqual(hashlib.sha256(data).hexdigest(),info['delivered_sha256'])
        with Image.open(io.BytesIO(data)) as img:self.assertEqual(list(img.size),info['delivered_size_px'])


    def test_custom_camera_is_captured_beside_standard_views(self):
        custom = {'name': 'three_quarter', 'direction': [0.574, -0.14, -0.819], 'up': [0, 1, 0]}
        data = self.run_observe(views=['front', custom])
        self.assertEqual([image['view'] for image in data['images']], ['front', 'three_quarter'])
        calls = self.sw.set_view_orientation.call_args_list
        self.assertEqual(calls[-2].args, ('front',))
        direction = calls[-1].kwargs['view_direction']
        self.assertAlmostEqual(sum(v*v for v in direction), 1.0, places=9)
        self.assertAlmostEqual(direction[2] / direction[0], -0.819 / 0.574, places=6)
        self.assertEqual(calls[-1].kwargs['up_direction'], [0, 1, 0])
        self.assertFalse(calls[-1].kwargs['zoom_to_fit'])
        # The custom frame is bound to the same revision and shares the scale.
        frame = data['images'][1]
        self.assertEqual(frame['revision'], '1')
        self.assertEqual(frame['native_scale'], data['images'][0]['native_scale'])
        self.assertEqual(frame['up'], [0, 1, 0])
        self.assertTrue(Path(frame['path']).name == 'three_quarter.png')

    def test_invalid_custom_views_are_rejected_before_capture(self):
        for views in ([{'name': 'front', 'direction': [0, 0, -1]}],
                      [{'name': 'a', 'direction': [0, 0, 0]}],
                      [{'name': 'a', 'direction': [0, 1, 0]}],
                      [{'name': 'a', 'direction': [0, 0, -1], 'up': [0, 0, 2]}],
                      [{'name': 'bad name', 'direction': [0, 0, -1]}],
                      [{'name': 'a', 'direction': [0, 0, -1]}, {'name': 'A', 'direction': [1, 0, 0]}],
                      [{'name': 'a'}], ['front', 'front'], ['perspective'], []):
            self.assert_code('INVALID_VIEWS', views=views)
        self.sw.take_screenshot.assert_not_called()

    def test_flat_writes_predictable_paths_and_never_overwrites_silently(self):
        target = Path(self.temp.name) / 'views'
        data = self.run_observe(directory=str(target), flat=True)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ['front.png', 'right.png'])
        self.assertEqual([image['path'] for image in data['images']],
                         [str(target / 'front.png'), str(target / 'right.png')])
        self.assertEqual(data['directory'], str(target))
        self.assertEqual(data['images'][0]['revision'], '1')
        first = (target / 'front.png').read_bytes()
        (target / 'front.png').write_bytes(b'user file')
        captures = self.sw.take_screenshot.call_count
        with self.assertRaises(ProgramError) as raised:
            self.run_observe(directory=str(target), flat=True)
        self.assertEqual(raised.exception.code, 'OUTPUT_EXISTS')
        self.assertEqual(raised.exception.details['existing'], ['front.png', 'right.png'])
        self.assertEqual((target / 'front.png').read_bytes(), b'user file')
        self.assertEqual(self.sw.take_screenshot.call_count, captures)
        self.run_observe(directory=str(target), flat=True, overwrite=True)
        self.assertEqual((target / 'front.png').read_bytes(), first)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ['front.png', 'right.png'])

    def test_default_layout_keeps_a_private_run_folder(self):
        data = self.run_observe()
        folder = Path(data['images'][0]['path']).parent
        self.assertEqual(folder.parent, Path(self.temp.name))
        self.assertEqual(data['directory'], str(folder))
        self.assertEqual(len(folder.name), 32)


if __name__ == '__main__': unittest.main()
