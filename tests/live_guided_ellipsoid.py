"""Public MCP acceptance of the integrated guided ellipsoid recipe."""
import asyncio, json, sys, time, base64, hashlib
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
OUT=Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True,exist_ok=True)
TARGET=OUT/'Guided_A.SLDPRT'
SOURCE="""Rx=model.parameter('Rx',10)
Ry=model.parameter('Ry',20)
Rz=model.parameter('Rz',6)
Cx=model.parameter('Cx',40)
Cy=model.parameter('Cy',35)
Cz=model.parameter('Cz',12)
model.ellipsoid('Witness',(4,7,3),center=(-20,10,0))
oval=model.ellipsoid('Oval',(Rx,Ry,Rz),center=(Cx,Cy,Cz),approximate=True)
"""

PATCH="""Gap=model.parameter('Gap',.25)
mask=model.rounded_prism('Mask',8,8,6,1,center=(Cx,Cy,Cz+Rz+1.5))
model.surface_patch('Skin',oval,mask,1,offset=Gap)
"""

async def main():
    report={'status':'IN_PROGRESS','calls':[]}
    def save(): (OUT/'integrated_verification.json').write_text(json.dumps(report,indent=2))
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],cwd=str(ROOT))
    async with stdio_client(params) as (r,w):
      async with ClientSession(r,w) as client:
        report['version']=(await client.initialize()).serverInfo.version
        assert report['version']=='6.19.0'
        async def call(name,args,success=True):
            start=time.monotonic()
            raw=await client.call_tool(name,args)
            data=raw.structuredContent
            assert data is not None, raw
            report['calls'].append({'name':name,'seconds':time.monotonic()-start,'result':data})
            save()
            print(name,data['success'],data.get('message','')[:120],flush=True)
            assert data['success'] is success,data
            return raw,data.get('data')
        async def expert(name,args):
            return await call('expert_tools',{'action':'call','name':name,'arguments':args})
        async def context():return (await call('get_model_context',{}))[1]
        async def edit(**kwargs):
            c=await context()
            return await call('execute_model_code',dict(mode='edit',document_path=str(TARGET),expected_revision=c['revision'],observe=False,**kwargs))
        await call('execute_model_code',{'mode':'create','code':SOURCE,'output_path':str(TARGET),'observe':False})
        c=await context()
        assert c['components']['Oval']['geometry_fidelity']=='GUIDED_APPROXIMATION'
        await expert('execute_python',{'code':(ROOT/'tests/fixtures/guided_ellipsoid_cycle.py').read_text().replace('__OUTPUT_DIRECTORY__',OUT.as_posix())})
        await edit(parameters={'Rx':11,'Ry':22,'Rz':7,'Cy':38})
        await edit(parameters={'Rx':10,'Ry':20,'Rz':6,'Cy':35})
        await edit(code=SOURCE.replace('(Rx,Ry,Rz)','(Rx*1.05,Ry,Rz)'))
        await edit(code=SOURCE)
        c=await context()
        identity={'document_path':str(TARGET),'expected_revision':c['revision']}
        _,facts=await call('inspect_model',{**identity,'precision':'delivery'})
        assert len(facts['bodies'])==2 and all(b['kernel_health']['passed'] for b in facts['bodies'])
        raw,views=await call('observe_model',{**identity,'directory':str(OUT/'guided_views')})
        images=[c for c in raw.content if c.type=='image']
        assert len(images)==3
        for img,meta in zip(images,views['images']):
            assert hashlib.sha256(base64.b64decode(img.data)).hexdigest()==meta['delivered_sha256']
        async def target():
            c=await context()
            return {'document_path':str(TARGET),'expected_revision':c['revision']}
        ids="from solidworks_mcp.automation.shape_parametric import read_record\nprint(read_record(doc)['native']['feature_refs']['Witness'])"
        original=(await expert('execute_python',{'code':ids}))[1]['stdout']
        changed=(await edit(code=SOURCE.replace('(Rx,Ry,Rz)','(Rx,Ry,.6*Rx)').replace(',approximate=True','')))[1]
        assert changed['rebuilt_components']==['Oval'] and changed['preserved_components']==['Witness']
        changed=(await edit(code=SOURCE))[1]
        assert changed['rebuilt_components']==['Oval'] and changed['preserved_components']==['Witness']
        assert (await expert('execute_python',{'code':ids}))[1]['stdout']==original
        await edit(code=SOURCE+PATCH)
        await expert('execute_python',{'code':(ROOT/'tests/fixtures/guided_ellipsoid_gap.py').read_text().replace('__OUTPUT_DIRECTORY__',OUT.as_posix())})
        await call('inspect_model',{**await target(),'precision':'delivery'})
        before=hashlib.sha256(TARGET.read_bytes()).hexdigest()
        result=await call('export_model',{**await target(),'directory':str(OUT/'guided_delivery'),'expected_bodies':['Witness','Oval','Skin']})
        assert hashlib.sha256(TARGET.read_bytes()).hexdigest()==before
        report['status']='GUIDED_ELLIPSOID_6_19_PASS'
        save()

if __name__=='__main__':asyncio.run(main())
