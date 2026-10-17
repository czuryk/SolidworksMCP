"""Export acceptance rejects wrong/partial artifacts and preserves source files."""
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np

from solidworks_mcp.mesh_acceptance import inspect_mesh
from solidworks_mcp.automation.model_delivery import export_model,verify_step_units
from solidworks_mcp.automation.high_level import HighLevelOperations
from solidworks_mcp.shape_program import ProgramError


VERTICES=np.array([[0.,0,0],[1.,0,0],[0,1.,0],[0,0,1.]])
FACES=np.array([[0,2,1],[0,1,3],[0,3,2],[1,2,3]])
TETRA=VERTICES[FACES]
BOX={'min':[0.,0,0],'max':[1.,1,1]}


class MeshAcceptanceTests(unittest.TestCase):
    def check(self,triangles,volume=1/6,box=None):
        return inspect_mesh(triangles,volume,box or BOX,.02)

    def test_closed_oriented_tetra(self):
        data=self.check(TETRA);self.assertTrue(data['passed']);self.assertAlmostEqual(data['signed_volume_mm3'],1/6)
        self.assertFalse(data['repair_performed']);self.assertEqual(data['self_intersections'],'NOT_CHECKED')

    def test_hole_rejected(self):
        data=self.check(TETRA[:-1]);self.assertFalse(data['passed']);self.assertEqual(data['boundary_edges'],3)

    def test_one_reversed_face_rejected(self):
        mesh=TETRA.copy();mesh[0]=mesh[0,::-1]
        data=self.check(mesh);self.assertFalse(data['passed']);self.assertGreater(data['inconsistent_edges'],0)

    def test_inward_shell_rejected(self):
        data=self.check(TETRA[:,::-1]);self.assertFalse(data['passed']);self.assertLess(data['signed_volume_mm3'],0)

    def test_duplicate_and_degenerate_rejected(self):
        for extra in (TETRA[:1],np.zeros((1,3,3))):
            self.assertFalse(self.check(np.concatenate([TETRA,extra]))['passed'])

    def test_nonmanifold_vertex_rejected_even_with_closed_edges(self):
        mesh=np.concatenate([TETRA,-TETRA[:,::-1]])
        data=self.check(mesh,1/3,{'min':[-1,-1,-1],'max':[1,1,1]})
        self.assertEqual(data['boundary_edges'],0);self.assertGreater(data['nonmanifold_vertices'],0)
        self.assertFalse(data['passed'])

    def test_wrong_volume_or_origin_rejected(self):
        self.assertFalse(self.check(TETRA,100)['passed'])
        self.assertFalse(self.check(TETRA+1)['passed'])

    def test_large_translation_volume_is_stable(self):
        offset=10000
        self.assertTrue(self.check(TETRA+offset,box={'min':[offset]*3,'max':[offset+1]*3})['passed'])

    def test_nonfinite_rejected(self):
        mesh=TETRA.copy();mesh[0,0,0]=float('nan')
        with self.assertRaises(ValueError):self.check(mesh)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.source=self.root/'source.SLDPRT';self.source.write_bytes(b'saved-native-fixture')
        self.doc=SimpleNamespace(GetSaveFlag=False,GetActiveSketch2=None,
            Extension=SimpleNamespace(NeedsRebuild2=0),ClearSelection2=Mock())
        self.body=SimpleNamespace(Name='Part/One',Visible=True)
        self.sw=Mock();self.sw._get_solid_bodies.return_value=[self.body]
        self.sw._inspect_sldprt.return_value={'container':'fixture'}
        self.sw._inspect_step.side_effect=HighLevelOperations._inspect_step
        self.sw._resolve_stl_settings.return_value={}
        self.sw._load_stl_triangles.side_effect=HighLevelOperations._load_stl_triangles
        self.mesh=TETRA.copy()
        def stl(doc,body,path,settings):
            Path(path).parent.mkdir(parents=True,exist_ok=True)
            triangles=[((0,0,1),*tri) for tri in self.mesh]
            HighLevelOperations._write_stl(path,triangles,True,'fixture')
        self.sw._export_body_stl.side_effect=stl
        def step(path):
            Path(path).write_text('ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=MANIFOLD_SOLID_BREP(\'\',#2);\n#2=CLOSED_SHELL(\'\',());\n#3=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));\nENDSEC;\nEND-ISO-10303-21;\n'+' '*128)
            return {'success':True}
        self.sw.export_file.side_effect=step
        stack=ExitStack();self.addCleanup(stack.close)
        module='solidworks_mcp.automation.model_delivery.'
        stack.enter_context(patch(module+'require_target',return_value=(self.doc,None,'r1')))
        stack.enter_context(patch(module+'geometry_state',return_value={'configuration':'Default'}))
        self.unchanged=stack.enter_context(patch(module+'ensure_unchanged'))
        stack.enter_context(patch(module+'document_revision',return_value='r1'))
        stack.enter_context(patch(module+'inspect_model',return_value={'bodies':[{
            'name':self.body.Name,'volume':1/6,'bbox':BOX,'kernel_health':{'passed':True}}]}))

    def export(self,**kwargs):
        return export_model(self.sw,str(self.source),'r1',str(self.root/'delivery'),**kwargs)

    def assert_code(self,code,**kwargs):
        with self.assertRaises(ProgramError) as cm:self.export(**kwargs)
        self.assertEqual(cm.exception.code,code)
        self.assertFalse((self.root/'delivery').exists())
        self.assertFalse(list(self.root.glob('.solidworks-mcp-delivery-*')))

    def test_saved_bytes_files_mapping_and_manifest(self):
        result=self.export();folder=Path(result['directory'])
        self.assertEqual(self.source.read_bytes(),(folder/'model.SLDPRT').read_bytes())
        manifest=json.loads((folder/'manifest.json').read_text())
        stl=next(f for f in manifest['files'] if f['kind']=='stl')
        self.assertEqual(stl['body'],'Part/One');self.assertTrue((folder/stl['path']).is_file())
        self.assertEqual(manifest['acceptance']['wall_thickness'],'NOT_CHECKED')
        before={p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}
        with self.assertRaises(ProgramError) as cm:self.export()
        self.assertEqual(cm.exception.code,'OUTPUT_EXISTS')
        self.assertEqual(before,{p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()})

    def test_stl_names_follow_body_names_not_body_order(self):
        from solidworks_mcp.automation.model_delivery import stl_leaves
        names=['Head','EyeL','Torso','LegR']
        self.assertEqual(stl_leaves(names),stl_leaves(list(reversed(names))))
        self.assertEqual(stl_leaves(names)['Head'],'Head')
        # Names that collide after sanitising stay distinct and deterministic.
        clash=stl_leaves(['Part/One','Part One','part_one','!!'])
        self.assertEqual(clash,{'!!':'Body','Part One':'Part_One','Part/One':'Part_One_2','part_one':'part_one_3'})
        other=SimpleNamespace(Name='Alpha',Visible=True)
        facts=lambda bodies:{'bodies':[{'name':b.Name,'volume':1/6,'bbox':BOX,'kernel_health':{'passed':True}} for b in bodies]}
        def step(path):
            Path(path).write_text('ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=MANIFOLD_SOLID_BREP(\'\',#2);\n#2=CLOSED_SHELL(\'\',());\n#4=MANIFOLD_SOLID_BREP(\'\',#5);\n#5=CLOSED_SHELL(\'\',());\n#3=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));\nENDSEC;\nEND-ISO-10303-21;\n'+' '*128)
            return {'success':True}
        self.sw.export_file.side_effect=step
        paths=[]
        for index,order in enumerate(([self.body,other],[other,self.body])):
            self.sw._get_solid_bodies.return_value=order
            with patch('solidworks_mcp.automation.model_delivery.inspect_model',return_value=facts(order)):
                result=export_model(self.sw,str(self.source),'r1',str(self.root/f'order{index}'))
            paths.append([f['path'] for f in result['files'] if f['kind']=='stl'])
        self.assertEqual(paths[0],['stl/Alpha.stl','stl/Part_One.stl'])
        self.assertEqual(paths[0],paths[1])

    def test_replace_republishes_only_its_own_delivery(self):
        first=self.export();folder=Path(first['directory'])
        self.assertFalse(first['replaced_previous_delivery'])
        self.assertIn('body_stl',first['timings_ms'])
        # Without the flag the existing delivery is refused and preserved.
        with self.assertRaises(ProgramError) as cm:self.export()
        self.assertEqual(cm.exception.code,'OUTPUT_EXISTS')
        self.source.write_bytes(b'saved-native-fixture-v2')
        second=self.export(replace=True)
        self.assertTrue(second['replaced_previous_delivery'])
        self.assertEqual((folder/'model.SLDPRT').read_bytes(),b'saved-native-fixture-v2')
        manifest=json.loads((folder/'manifest.json').read_text())
        self.assertEqual(manifest['commit_method'],'replace_own_delivery_rename')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['delivery','source.SLDPRT'])
        # A foreign file makes the directory someone else's: never replaced.
        (folder/'notes.txt').write_text('mine')
        with self.assertRaises(ProgramError) as cm:self.export(replace=True)
        self.assertEqual(cm.exception.code,'OUTPUT_EXISTS')
        self.assertEqual(cm.exception.details['foreign'],['notes.txt'])
        self.assertEqual((folder/'notes.txt').read_text(),'mine')
        self.assertEqual((folder/'model.SLDPRT').read_bytes(),b'saved-native-fixture-v2')
        self.sw.export_file.reset_mock()
        plain=self.root/'plain';plain.mkdir();(plain/'data.bin').write_bytes(b'x')
        with self.assertRaises(ProgramError) as cm:
            export_model(self.sw,str(self.source),'r1',str(plain),replace=True)
        self.assertEqual(cm.exception.code,'OUTPUT_EXISTS')
        self.assertEqual((plain/'data.bin').read_bytes(),b'x')
        self.sw.export_file.assert_not_called()

    def test_failed_replacement_keeps_the_previous_delivery(self):
        folder=Path(self.export()['directory'])
        before={p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        self.mesh=TETRA[:-1]
        with self.assertRaises(ProgramError) as cm:self.export(replace=True)
        self.assertEqual(cm.exception.code,'STL_VALIDATION_FAILED')
        self.assertEqual(before,{p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['delivery','source.SLDPRT'])

    def test_wrong_body_set_rejected_before_export(self):
        self.assert_code('BODY_SET_MISMATCH',expected_bodies=['Other'])
        self.sw.export_file.assert_not_called()

    def test_dirty_or_unrebuilt_part_rejected(self):
        self.doc.GetSaveFlag=True;self.assert_code('DOCUMENT_DIRTY')
        self.doc.GetSaveFlag=False;self.doc.Extension.NeedsRebuild2=1
        self.assert_code('REBUILD_REQUIRED');self.sw.export_file.assert_not_called()

    def test_hidden_body_not_silently_omitted(self):
        self.body.Visible=False;self.assert_code('HIDDEN_BODY')

    def test_mesh_failure_leaves_no_partial_delivery(self):
        self.mesh=TETRA[:-1];self.assert_code('STL_VALIDATION_FAILED')
        self.assertEqual(self.source.read_bytes(),b'saved-native-fixture')

    def test_opt_in_cleanup_keeps_raw_hash_and_written_mesh_audit(self):
        self.mesh[2]=self.mesh[2,::-1]
        result=self.export(mesh_cleanup='bounded')
        stl=next(f for f in result['files'] if f['kind']=='stl')
        raw=next(f for f in result['files'] if f['kind']=='raw_stl_diagnostic')
        self.assertTrue(stl['inspection']['passed'])
        self.assertTrue(stl['inspection']['repair_performed'])
        self.assertFalse(raw['inspection']['passed'])
        self.assertEqual(stl['cleanup']['raw_sha256'],raw['sha256'])
        self.assertEqual(stl['cleanup']['output_sha256'],stl['sha256'])
        self.assertEqual(stl['cleanup']['reoriented_facet_indices'],[2])
        self.assertEqual(self.source.read_bytes(),b'saved-native-fixture')

    def test_valid_raw_mesh_bypasses_cleanup(self):
        with patch('solidworks_mcp.automation.model_delivery.bounded_cleanup',side_effect=AssertionError('Already valid')):
            result=self.export(mesh_cleanup='bounded')
        self.assertEqual(len(result['files']),3)
        self.assertNotIn('cleanup',result['files'][-1])

    def test_explicit_failure_diagnostics_preserve_raw_without_delivery(self):
        self.mesh=TETRA[:-1]
        path=self.root/'diagnostic'
        self.assert_code('STL_VALIDATION_FAILED',diagnostics_directory=str(path),mesh_cleanup='bounded')
        self.assertTrue((path/'raw.stl').is_file())
        data=json.loads((path/'diagnostics.json').read_text())
        self.assertEqual(data['status'],'EXPORT_REJECTED')
        self.assertEqual(data['inspection']['boundary_edges'],3)
        self.assertEqual(data['cleanup_error']['code'],'MESH_CLEANUP_REFUSED')

    def test_existing_or_nested_diagnostics_never_overwritten(self):
        self.assert_code('OUTPUT_EXISTS',diagnostics_directory=str(self.root))
        self.assert_code('INVALID_PATH',diagnostics_directory=str(self.root/'delivery'/'diagnostic'))
        self.sw.export_file.assert_not_called()

    def test_source_change_stops_publish(self):
        original=self.sw.export_file.side_effect
        def changed(path):
            result=original(path);self.doc.GetSaveFlag=True;return result
        self.sw.export_file.side_effect=changed
        self.assert_code('EXPORT_SOURCE_CHANGED')

    def test_switch_stops_before_export(self):
        self.unchanged.side_effect=ProgramError('DOCUMENT_MISMATCH','switched')
        self.assert_code('DOCUMENT_MISMATCH');self.sw.export_file.assert_not_called()

    def test_wrong_step_units_do_not_publish(self):
        original=self.sw.export_file.side_effect
        def wrong(path):
            result=original(path);p=Path(path);p.write_text(p.read_text().replace('.MILLI.','$'));return result
        self.sw.export_file.side_effect=wrong;self.assert_code('STEP_UNITS_UNVERIFIED')

    def test_unknown_precision_is_not_silently_used(self):
        from solidworks_mcp.automation.model_inspection import inspect_model
        with self.assertRaises(ProgramError):inspect_model(self.sw,'x','r',precision='magic')


class CompactToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_modeless_welcome_does_not_hide_real_modal(self):
        from solidworks_mcp.automation import com_utils as module
        def enum(callback,data):
            callback(101,data);callback(102,data)
        state={'enabled':True,'class':'swWelcomeScreen'}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(module._SW_MAIN_WINDOW_CACHE,{'hwnd':101,'pid':202}))
            stack.enter_context(patch('win32gui.IsWindow',return_value=True))
            stack.enter_context(patch('win32gui.IsWindowVisible',return_value=True))
            stack.enter_context(patch('win32gui.IsWindowEnabled',side_effect=lambda h:state['enabled'] if h==101 else True))
            stack.enter_context(patch('win32gui.EnumWindows',side_effect=enum))
            stack.enter_context(patch('win32gui.GetWindowText',side_effect=lambda h:'SOLIDWORKS' if h==101 else ''))
            stack.enter_context(patch('win32gui.GetClassName',side_effect=lambda h:'SldWorksFrame' if h==101 else state['class']))
            stack.enter_context(patch('win32gui.GetWindowRect',return_value=(0,0,800,600)))
            stack.enter_context(patch('win32gui.GetWindowLong',return_value=0x80000000))
            stack.enter_context(patch('win32process.GetWindowThreadProcessId',return_value=(303,202)))
            stack.enter_context(patch.object(module,'_window_owner_chain',return_value=[101]))
            stack.enter_context(patch.object(module,'_window_controls',return_value=[]))
            for full in (False,True):
                self.assertFalse(module.detect_modal_dialog(full)['modal'])
                state['enabled']=False
                self.assertTrue(module.detect_modal_dialog(full)['modal'])
                state['enabled']=True;state['class']='#32770'
                self.assertTrue(module.detect_modal_dialog(full)['modal'])
                state['class']='swWelcomeScreen'

    async def test_default_surface_and_expert_profile(self):
        from solidworks_mcp.server import list_tools
        with patch.dict(os.environ,{'SOLIDWORKS_MCP_TOOL_PROFILE':'shape'}):compact=await list_tools()
        with patch.dict(os.environ,{'SOLIDWORKS_MCP_TOOL_PROFILE':'expert'}):full=await list_tools()
        names={t.name for t in compact}
        self.assertLessEqual(len(names),14)
        self.assertTrue({'execute_model_code','inspect_model','export_model','expert_tools'}<=names)
        self.assertNotIn('draw_line',names);self.assertIn('draw_line',{t.name for t in full})
        self.assertLess(len(json.dumps([t.model_dump() for t in compact])),len(json.dumps([t.model_dump() for t in full]))*.2)

    async def test_expert_description_preserves_schema_and_validates_call(self):
        from solidworks_mcp.server import _resolve_expert
        schema=_resolve_expert({'action':'describe','name':'draw_circle'})['data']['tool']['inputSchema']
        self.assertEqual(schema['properties']['radius']['type'],'number')
        with self.assertRaises(Exception):_resolve_expert({'action':'call','name':'draw_circle','arguments':{'radius':'bad'}})
        with self.assertRaises(Exception):_resolve_expert({'action':'call','name':'expert_tools'})
        with self.assertRaises(Exception):_resolve_expert({'action':'call','name':'missing'})

    async def test_expert_call_uses_same_executor(self):
        from solidworks_mcp import server
        from unittest.mock import AsyncMock
        executor=Mock();executor.wait=AsyncMock(return_value={'success':True,'message':'fixture','data':{}})
        with patch.object(server,'cad_executor',executor):
            result=await server.call_tool('expert_tools',{'action':'call','name':'get_document_info'})
        self.assertFalse(result.isError)
        self.assertEqual(executor.submit.call_args.args[0],'get_document_info')
        executor.wait.assert_awaited_once()

    async def test_delivery_does_not_offer_native_only_replay(self):
        from solidworks_mcp.automation.request_execution import request_spec
        from solidworks_mcp.automation.request_journal import RequestConflict
        with self.assertRaises(RequestConflict):request_spec('export_model',{'idempotency_key':'unsafe'})


if __name__=='__main__':unittest.main()
