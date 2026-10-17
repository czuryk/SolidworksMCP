"""Opt-in multiple-body placement, reverse rotation and dependency removal."""
import asyncio,json,sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
ROOT=Path(__file__).resolve().parents[1]
OUT=Path(sys.argv[1]).resolve();DOC=OUT/'Group_01.SLDPRT'
BASE="""H=model.parameter('H',40)
Tilt=model.parameter('Tilt',.35,unit='ratio')
a=model.dome('Left',3,2,center=(6,0,0))
b=model.dome('Right',4,3,center=(15,0,0))
model.dome('Witness',5,3,center=(40,0,0))
"""
GROUP="""p=model.place('Placed',[a,b],frame=model.frame(origin=(-H/2,-H/4,H/5),rotation=(0,0,-Tilt)))
model.mirror('Pair',[p])
"""

async def main():
    report={'status':'IN_PROGRESS','calls':[]}
    def save():(OUT/'group_verification.json').write_text(json.dumps(report,indent=2))
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],cwd=str(ROOT))
    async with stdio_client(params) as (r,w):
        async with ClientSession(r,w) as client:
            assert (await client.initialize()).serverInfo.version=='6.17.0'
            async def call(name,args):
                result=(await client.call_tool(name,args)).structuredContent
                report['calls'].append({'name':name,'result':result});save()
                print(name,result['success'],flush=True);assert result['success'],result
                return result['data']
            async def target():
                ctx=await call('get_model_context',{})
                return {'document_path':str(DOC),'expected_revision':ctx['revision']}
            await call('execute_model_code',{'mode':'create','code':BASE+GROUP,'output_path':str(DOC),'observe':False})
            facts=await call('inspect_model',{**await target(),'precision':'delivery'})
            assert len(facts['bodies'])==5 and all(b['kernel_health']['passed'] for b in facts['bodies'])
            # Removing the consuming transform must restore its source body ownership.
            edit=await call('execute_model_code',{**await target(),'mode':'edit','code':BASE,'observe':False})
            assert set(edit['preserved_components'])=={'Left','Right','Witness'}
            facts=await call('inspect_model',{**await target(),'precision':'delivery'})
            assert {b['name'] for b in facts['bodies']}=={'Left','Right','Witness'}
            await call('execute_model_code',{**await target(),'mode':'edit','code':BASE+GROUP,'observe':False})
            changed=(BASE+GROUP).replace('(0,0,-Tilt)','(0,0,Tilt)')
            edit=await call('execute_model_code',{**await target(),'mode':'edit','code':changed,'observe':False})
            assert set(edit['rebuilt_components'])=={'Placed','Pair'}
            facts=await call('inspect_model',{**await target(),'precision':'delivery'})
            assert len(facts['bodies'])==5 and all(b['kernel_health']['passed'] for b in facts['bodies'])
            await call('expert_tools',{'action':'call','name':'open_document','arguments':{'filepath':str(OUT/'Frame_02.SLDPRT')}})
            report['status']='NATIVE_PLACEMENT_GROUP_PASS';save();print(report['status'],flush=True)
asyncio.run(main())
