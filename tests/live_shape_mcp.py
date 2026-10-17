"""Opt-in MCP stdio acceptance against a fresh server and live SolidWorks."""

import argparse
import asyncio
import base64
import io
import json
from pathlib import Path
import sys

from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from solidworks_mcp.automation.session_lease import SessionLease


PROGRAM = """
H = model.parameter('H', 50)
model.revolve('Vessel', [(0,.24*H),(.36*H,.24*H),(.48*H,.16*H)])
pipe = model.tube('Handle', [(.6*H,0,0),(.6*H,.5*H,.1*H)], .06*H)
model.sectioned_solid('Elliptic', [(0,.1*H,.18*H),(.4*H,.16*H,.24*H)], center_xz=(-.6*H,0))
model.mirror('HandlePair', [pipe], plane='Front')
"""

EXTENDED_PROGRAM = """
H=model.parameter('H',50)
a=model.rounded_prism('Outside',.2*H,.14*H,.1*H,.02*H,center=(.4*H,.15*H,0))
b=model.rounded_prism('Inside',.16*H,.1*H,.12*H,.012*H,center=(.4*H,.15*H,0))
rim=model.combine('Rim',[a,b],operation='difference')
model.ellipsoid('Oval',(.12*H,.2*H,.09*H),center=(-.4*H,.25*H,0))
model.variable_tube('Member',[(0,0,0),(.04*H,.2*H,.02*H),(0,.4*H,0)],[.035*H,.05*H,.03*H])
model.mirror('RimPair',[rim],plane='Right')
"""


async def acceptance(output,extended=False,surface=False):
    if surface:
        from live_shape_surface import BASE,PATCH
        program=(BASE+PATCH).replace("'Wall'","'Thickness'").replace("'H',100","'H',50")
    else:program=EXTENDED_PROGRAM if extended else PROGRAM
    params = StdioServerParameters(
        command=sys.executable, args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],
        cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.version == '6.11.0', initialized
            assert 'execute_model_code' in initialized.instructions
            listed = await session.list_tools()
            expected = {'get_model_context','model_api_lookup','execute_model_code','observe_model','measure_reference'}
            assert {t.name for t in listed.tools[:5]} == expected
            lookup = await session.call_tool('model_api_lookup', {})
            assert not lookup.isError and lookup.structuredContent
            assert 'model.combine' in lookup.structuredContent['data']['constructors']
            with SessionLease():
                analyzed = await session.call_tool('execute_model_code', {'code':program})
                assert not analyzed.isError
                blocked = await session.call_tool('execute_model_code', {
                    'code':program,'mode':'create','output_path':str(output),'observe':False})
                assert blocked.isError and 'CAD_SESSION_BUSY' in str(blocked.structuredContent)
            invalid = await session.call_tool('execute_model_code', {'code':program,'merge_typo':True})
            assert invalid.isError
            created = await session.call_tool('execute_model_code', {
                'code':program,'mode':'create','output_path':str(output),'observe':True})
            assert not created.isError, created
            data = created.structuredContent['data']
            assert len(data['components']) == (6 if extended else 4)
            assert len(data['body_state']['bodies']) == (3 if surface else 4)
            assert data['native_parameter_bindings'] is True
            frames = [c for c in created.content if c.type == 'image']
            assert len(frames) == 3, len(frames)
            sizes = []
            for frame in frames:
                with Image.open(io.BytesIO(base64.b64decode(frame.data))) as img:
                    assert 0 < max(img.size) <= 1000
                    sizes.append(img.size)
            context = await session.call_tool('get_model_context', {})
            assert not context.isError and context.structuredContent['data']['mcp_version'] == '6.11.0'
            if surface:
                assert 'model.surface_patch' in lookup.structuredContent['data']['constructors']
                assert context.structuredContent['data']['parameters']['Thickness']==2
            assert context.structuredContent['data']['document_editable']
            edited = await session.call_tool('execute_model_code', {
                'mode':'edit','document_path':str(output),'observe':False,
                'expected_revision':context.structuredContent['data']['revision'],
                'parameters':{'H':55}})
            assert not edited.isError,edited
            assert edited.structuredContent['data']['native_feature_ids_preserved']
            context = await session.call_tool('get_model_context', {})
            grown_program=program+"\nDetail=model.parameter('Detail',2)\nmodel.dome('NewDetail',Detail,Detail/2,center=(0,0,1.5*H))\n"
            grown=await session.call_tool('execute_model_code',{
                'code':grown_program,'mode':'edit','document_path':str(output),'observe':False,
                'expected_revision':context.structuredContent['data']['revision'],'parameters':{'H':55}})
            assert not grown.isError,grown
            growth=grown.structuredContent['data']
            assert growth['added_components']==['NewDetail'] and growth['added_parameters']==['Detail']
            assert growth['native_feature_ids_preserved']
            context=await session.call_tool('get_model_context',{})
            reduced=await session.call_tool('execute_model_code',{
                'code':program+"\nDetail=model.parameter('Detail',2)\n",'mode':'edit',
                'document_path':str(output),'observe':False,'parameters':{'H':55},
                'expected_revision':context.structuredContent['data']['revision']})
            assert not reduced.isError,reduced
            assert reduced.structuredContent['data']['removed_components']==['NewDetail']
            observation_context = await session.call_tool('get_model_context', {})
            observed = await session.call_tool('observe_model', {
                'document_path':str(output), 'expected_revision':observation_context.structuredContent['data']['revision'],
                'directory': str(output.parent/(output.stem+'_observed')),
                'views':['isometric'], 'width':600})
            assert not observed.isError, observed
            frame = next(c for c in observed.content if c.type == 'image')
            with Image.open(io.BytesIO(base64.b64decode(frame.data))) as img:
                assert max(img.size) <= 600
            repeat = await session.call_tool('execute_model_code', {
                'code':program,'mode':'create','output_path':str(output),'observe':False})
            assert repeat.isError and 'OUTPUT_EXISTS' in str(repeat.structuredContent)
            return {'status':'FRESH_STDIO_SERVER_PASS', 'mcp_version':initialized.serverInfo.version,
                    'tool_count':len(listed.tools), 'strict_schema_verified':True,'extended_families':extended,
                    'surface_patch_and_anchor':surface,
                    'writer_exclusion_verified':True, 'com_free_analyze_while_busy':True,
                    'inline_image_sizes':sizes, 'created_model':data,
                    'native_parameter_edit_verified':True,
                    'incremental_add_remove_and_parameter_growth_verified':True,
                    'codex_restart_verification':'PENDING'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--extended',action='store_true')
    parser.add_argument('--surface',action='store_true')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    assert not output.exists(), 'Use a new test output path'
    report = asyncio.run(acceptance(output,args.extended,args.surface))
    Path(args.report).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'created_model'}, indent=2))
