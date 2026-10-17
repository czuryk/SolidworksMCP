"""Opt-in fresh stdio acceptance for scoped native observation and raster estimates."""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import sys
import time
import math
from pathlib import Path

from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from solidworks_mcp.automation.session_lease import SessionLease

PROGRAM="""
H=model.parameter('H',40)
model.tube('Round',[(-15,0,0),(-15,H,0)],5)
model.rounded_prism('Flat',10,H,2,1,center=(15,H/2,0))
"""


async def acceptance(output, reuse=False):
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],cwd=str(ROOT))
    report={'status':'IN_PROGRESS','timings_sec':{}}
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            initialized=await session.initialize()
            assert initialized.serverInfo.version=='6.11.0'
            async def call(name,args):
                start=time.monotonic();r=await session.call_tool(name,args)
                report['timings_sec'].setdefault(name,[]).append(round(time.monotonic()-start,3))
                assert not r.isError, r
                return r
            with SessionLease():
                r=await call('measure_reference',{'image_path':'D:/TMP/bender/reference_front_approved.png',
                    'projection':'front','calibration':{'pixel_distance':1250,'length_mm':150},
                    'queries':[{'id':'torso_width','kind':'width_at_height','region':[365,600,690,880],'y':700},
                               {'id':'hidden_depth','kind':'depth_range','region':[365,600,690,880],'y':700}],
                    'annotation_directory':str(output.parent/'reference_crops')})
                report['reference']=r.structuredContent['data']
                assert r.structuredContent['data']['cad_entities_created']==0
                assert r.structuredContent['data']['measurements'][1]['status']=='UNKNOWN'
                assert any(c.type=='image' for c in r.content)
            if reuse:
                await call('open_document',{'filepath':str(output)})
            else:
                created=await call('execute_model_code',{'code':PROGRAM,'mode':'create','output_path':str(output),'observe':True})
                assert len([c for c in created.content if c.type=='image'])==3,created
                assert created.structuredContent['data']['warnings']==[],created
            context=await call('get_model_context',{})
            revision=context.structuredContent['data']['revision']
            args={'directory':str(output.parent/'observations'),'document_path':str(output),
                  'expected_revision':revision,'width':850}
            wrong=await session.call_tool('observe_model',{**args,'document_path':str(output.parent/'other.SLDPRT')})
            assert wrong.isError and 'DOCUMENT_MISMATCH' in str(wrong.structuredContent)
            stale=await session.call_tool('observe_model',{**args,'expected_revision':'stale'})
            assert stale.isError and 'STALE_REVISION' in str(stale.structuredContent)
            sections=[{'component':name,'center':[x,20,0],'normal':[0,1,0],
                       'expected_radius':5,'tolerance_mm':.01} for name,x in [('Round',-15),('Flat',15)]]
            observed=await call('observe_model',{**args,'components':['Round','Flat'],'sections':sections})
            data=observed.structuredContent['data']; report['observation']=data
            by_name={b['name']:b for b in data['bodies']}
            assert by_name['Round']['components'][0]['native_feature_type']=='Sweep'
            assert data['sections'][0]['radius_check']['passed'] is True,data['sections']
            assert data['sections'][1]['radius_check']['passed'] is False,data['sections']
            assert by_name['Flat']['surface_types']['plane']>=4,by_name['Flat']
            assert math.isclose(by_name['Round']['volume_exact'],math.pi*25*40,rel_tol=.001)
            assert data['camera_restored']
            assert len({i['native_scale'] for i in data['images']})==1
            for frame,info in zip([c for c in observed.content if c.type=='image'],data['images']):
                raw=base64.b64decode(frame.data)
                assert hashlib.sha256(raw).hexdigest()==info['delivered_sha256']
                with Image.open(io.BytesIO(raw)) as img: assert list(img.size)==info['delivered_size_px']
            local=await call('observe_model',{**args,'components':['Round'],'views':['isometric']})
            assert len(local.structuredContent['data']['bodies'])==1
            assert local.structuredContent['data']['images'][0]['path']!=data['images'][-1]['path']
            assert (await call('get_model_context',{})).structuredContent['data']['revision']==revision
            edited=await call('execute_model_code',{'mode':'edit','document_path':str(output),
                'expected_revision':revision,'parameters':{'H':44},'observe':True})
            assert edited.structuredContent['data']['warnings']==[],edited
            assert len([c for c in edited.content if c.type=='image'])==3
            stale=await session.call_tool('observe_model',args)
            assert stale.isError and 'STALE_REVISION' in str(stale.structuredContent)
            context=await call('get_model_context',{})
            returned=await call('execute_model_code',{'mode':'edit','document_path':str(output),
                'expected_revision':context.structuredContent['data']['revision'],'parameters':{'H':40},'observe':False})
            assert returned.structuredContent['data']['native_feature_ids_preserved']
            await call('close_document',{'save':False})
            await call('open_document',{'filepath':str(output)})
            context=await call('get_model_context',{})
            assert context.structuredContent['data']['parameters']['H']==40
            reopened=await call('observe_model',{**args,'expected_revision':context.structuredContent['data']['revision'],
                'views':['isometric'],'components':['Round','Flat']})
            report['reopened']=reopened.structuredContent['data']
            report['status']='FRESH_MCP_REFERENCE_OBSERVATION_PASS'
            report['mcp_version']=initialized.serverInfo.version
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--report',required=True);parser.add_argument('--reuse',action='store_true')
    args=parser.parse_args()
    result=asyncio.run(acceptance(Path(args.output).resolve(),args.reuse))
    Path(args.report).write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(result['status'])
