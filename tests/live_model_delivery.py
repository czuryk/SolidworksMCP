"""Opt-in compact workflow and checked export acceptance on a disposable part."""
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
OUT=Path(sys.argv[1]).resolve()
PROGRAM="""H=model.parameter('H',40)
model.tube('Round',[(-15,0,0),(-15,H,0)],5)
model.rounded_prism('Flat',10,H,2,1,center=(15,H/2,0))
"""


async def main():
    report={'status':'IN_PROGRESS','calls':[]}
    OUT.mkdir(parents=True,exist_ok=True)
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],cwd=str(ROOT))
    async with stdio_client(params) as (r,w):
        async with ClientSession(r,w) as session:
            async def call(name,args,success=True):
                start=time.monotonic();result=await session.call_tool(name,args);data=result.structuredContent
                report['calls'].append({'name':name,'elapsed_sec':round(time.monotonic()-start,3),
                                        'result':data})
                (OUT/'live_verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
                assert bool(data['success'])==success,data
                print(name,data['success'],data.get('error_name'),flush=True)
                return data
            init=await session.initialize();assert init.serverInfo.version=='6.14.0'
            catalog=await session.list_tools();assert len(catalog.tools)==12
            assert 'draw_line' not in {t.name for t in catalog.tools}
            report['tool_count']=len(catalog.tools)
            await call('expert_tools',{'action':'describe','name':'open_document'})
            lookup=await call('model_api_lookup',{})
            assert 'topics' in lookup['data'] and 'example' not in lookup['data']
            await call('expert_tools',{'action':'call','name':'get_environment_status'})
            path=OUT/'Delivery_01.SLDPRT'
            if not path.exists():
                await call('execute_model_code',{'mode':'create','code':PROGRAM,'output_path':str(path),'observe':False})
            else:
                await call('expert_tools',{'action':'call','name':'open_document','arguments':{'filepath':str(path)}})
            ctx=(await call('get_model_context',{}))['data'];revision=ctx['revision']
            inspection=await call('inspect_model',{'document_path':str(path),'expected_revision':revision,'precision':'delivery'})
            assert all(b['kernel_health']['passed'] for b in inspection['data']['bodies'])
            tube=next(b for b in inspection['data']['bodies'] if b['name']=='Round')
            assert max(abs(a-b) for a,b in zip(tube['dimensions_mm'],[10,40,10]))<.01,tube
            section=await call('inspect_model',{'document_path':str(path),'expected_revision':revision,
                'components':['Round'],'sections':[{'component':'Round','center':[-15,20,0],
                'normal':[0,1,0],'expected_radius':5,'tolerance_mm':.01}]})
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            directory=OUT/'delivery'
            args={'document_path':str(path),'expected_revision':revision,'directory':str(directory),'expected_bodies':['Round','Flat']}
            wrong=await call('export_model',{**args,'directory':str(OUT/'wrong'),'expected_bodies':['Missing']},False)
            assert not (OUT/'wrong').exists()
            result=await call('export_model',args)
            assert result['data']['source_preserved']
            assert before==hashlib.sha256(path.read_bytes()).hexdigest()
            assert (directory/'model.SLDPRT').read_bytes()==path.read_bytes()
            manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
            assert manifest['mcp_version']=='6.14.0' and manifest['unit']=='mm'
            assert len([f for f in manifest['files'] if f['kind']=='stl'])==2
            for f in manifest['files']:
                assert hashlib.sha256((directory/f['path']).read_bytes()).hexdigest()==f['sha256']
            await call('export_model',args,False)
            for f in manifest['files']:
                assert hashlib.sha256((directory/f['path']).read_bytes()).hexdigest()==f['sha256']
            obs=await call('observe_model',{'document_path':str(path),'expected_revision':revision,'views':['isometric']})
            report['observation_images']=obs['data']['images']
            # Open the delivered native copy through the expert layer. It must
            # recover source and parameters without a sidecar.
            await call('expert_tools',{'action':'call','name':'open_document','arguments':{'filepath':str(directory/'model.SLDPRT')}})
            delivered=(await call('get_model_context',{'include_source':True}))['data']
            assert delivered['parameters']['H']==40 and delivered['source']==PROGRAM
            await call('expert_tools',{'action':'call','name':'open_document','arguments':{'filepath':str(path)}})
            final=(await call('get_model_context',{}))['data']
            assert final['body_state']['count']==2 and final['parameters']['H']==40
            state=await call('get_executor_status',{});assert not state['data']['quarantined']
            report['status']='COMPACT_DELIVERY_PASS'
            (OUT/'live_verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(report['status'],flush=True)

asyncio.run(main())
