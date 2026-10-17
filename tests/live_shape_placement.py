"""Opt-in native frame acceptance; requires a disposable directory and fixture."""
import asyncio
import base64
import hashlib
import json
import math
import sys
from pathlib import Path
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
OUT=Path(sys.argv[1]).resolve()
DOC=OUT/(sys.argv[2] if len(sys.argv)>2 else 'Frame_01.SLDPRT')
CODE=(OUT/'frame_fixture.py').read_text()


def matrix(a):
    x,y,z=a,-a/2,a*.75
    rx=np.array([[1,0,0],[0,math.cos(x),-math.sin(x)],[0,math.sin(x),math.cos(x)]])
    ry=np.array([[math.cos(y),0,math.sin(y)],[0,1,0],[-math.sin(y),0,math.cos(y)]])
    rz=np.array([[math.cos(z),-math.sin(z),0],[math.sin(z),math.cos(z),0],[0,0,1]])
    return rz@ry@rx


async def main():
    report={'status':'IN_PROGRESS','calls':[],'geometry':[]}
    def save(): (OUT/'placement_verification.json').write_text(json.dumps(report,indent=2))
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],cwd=str(ROOT))
    async with stdio_client(params) as (r,w):
        async with ClientSession(r,w) as client:
            assert (await client.initialize()).serverInfo.version=='6.17.0'
            async def call(name,args):
                raw=await client.call_tool(name,args);result=raw.structuredContent
                report['calls'].append({'name':name,'result':result});save()
                print(name,result['success'],flush=True);assert result['success'],result
                data=result.get('data',{})
                if data.get('images'):
                    images=[x for x in raw.content if x.type=='image']
                    assert len(images)==len(data['images'])
                    for im,meta in zip(images,data['images']):
                        assert hashlib.sha256(base64.b64decode(im.data)).hexdigest()==meta['delivered_sha256']
                return data
            async def raw(code):return await call('expert_tools',{'action':'call','name':'execute_python','arguments':{'code':code}})
            async def context():return await call('get_model_context',{'include_source':True})
            async def args():
                ctx=await context()
                return {'document_path':str(DOC),'expected_revision':ctx['revision']}
            async def verify(h,a):
                facts=await call('inspect_model',{**await args(),'precision':'delivery'})
                bodies={b['name']:b for b in facts['bodies']};assert len(bodies)==4
                assert all(b['kernel_health']['passed'] for b in bodies.values())
                rot=matrix(a);center=np.array([h/2,h,h/3])+rot@np.array([h*.075,h*.1,h*.05])
                radii=np.array([h*.15,h*.225,h*.075]);extent=np.sqrt((rot*rot)@(radii*radii))
                errors=[]
                for key,expected in [('min',center-extent),('max',center+extent)]:
                    errors.extend(np.abs(np.array(bodies['Placed']['bbox'][key])-expected).tolist())
                assert max(errors)<.02,errors
                volume=4*math.pi/3*np.prod(radii)
                assert abs(bodies['Placed']['volume_exact']/volume-1)<.001
                for side in ('min','max'):
                    opposite='max' if side=='min' else 'min'
                    assert abs(bodies['Pair']['bbox'][side][0]+bodies['Placed']['bbox'][opposite][0])<.01
                    np.testing.assert_allclose(bodies['Pair']['bbox'][side][1:],bodies['Placed']['bbox'][side][1:],atol=.01)
                report['geometry'].append({'H':h,'Tilt':a,'analytic_bounds_max_error_mm':max(errors),'kernel_pass':True})
                save()
            if DOC.exists():
                await call('expert_tools',{'action':'call','name':'open_document','arguments':{'filepath':str(DOC)}})
                await raw("from solidworks_mcp.automation.com_utils import com_get,typed\napp=typed(sw,'ISldWorks');doc=app.ActiveDoc\ntyped(com_get(doc,'GetEquationMgr'),'IEquationMgr').AutomaticRebuild=False\nassert doc.ForceRebuild3(False)\nassert automation.save_document(str(com_get(doc,'GetPathName')))['success']")
            else:
                await call('execute_model_code',{'mode':'create','code':CODE,'output_path':str(DOC),'observe':False})
            await verify(40,.4)
            await call('observe_model',{**await args(),'directory':str(OUT/'views')})
            await call('execute_model_code',{**await args(),'mode':'edit','parameters':{'H':44,'Tilt':.6},'observe':False})
            await verify(44,.6)
            await call('execute_model_code',{**await args(),'mode':'edit','parameters':{'H':40,'Tilt':.4},'observe':False})
            # Reopen and drive only native global equations; the generator is not called.
            await raw(f'''import json,math
from pathlib import Path
from solidworks_mcp.automation.com_utils import com_get,typed,command_scope
from solidworks_mcp.automation.shape_parametric import read_record,restore,validate_feature_health
from solidworks_mcp.automation.model_inspection import exact_bbox
target=Path({str(DOC)!r})
app=typed(sw,'ISldWorks');doc=app.ActiveDoc
app.CloseDoc(com_get(doc,'GetTitle'));assert automation.open_document(str(target))['success']
doc=app.ActiveDoc;record=read_record(doc);native=record['native'];rows=[]
with command_scope(app):
 eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
 for h,a in [(48,.7),(40,.4)]:
  for name,value,suffix in [('H',h,'mm'),('Tilt',a,'')]:
   eq.SetEquation(native['parameter_bindings'][name]['global_index'],f'"{{name}}" = {{value}}{{suffix}}')
  # Native edge-angle features can lag dimensions on the first pass after reopen.
  # Both passes are native rebuilds; no dimension arrays or geometry are rewritten.
  for rebuild_pass in range(2):
   com_get(eq,'EvaluateAll');assert doc.ForceRebuild3(False)
  validate_feature_health(automation,doc,list(native['feature_refs'])+native['support_features'])
  body=automation._find_body(doc,'Placed')
  rows.append({{'H':h,'Tilt':a,'bbox':exact_bbox(body),'volume':automation._body_mass_props(body,'mm')['volume_exact']}})
  for ref in native['feature_refs'].values():assert restore(doc,ref) is not None
assert automation.save_document(str(target))['success']
app.CloseDoc(com_get(doc,'GetTitle'));assert automation.open_document(str(target))['success']
assert read_record(app.ActiveDoc)['native']['feature_refs']==native['feature_refs']
Path({str(OUT/'native_cycle.json')!r}).write_text(json.dumps({{'status':'COLLECTED_FOR_ANALYTIC_CHECK','rows':rows,'refs':native['feature_refs'],'native_rebuild_passes':2,'python_regeneration':False}},indent=2))
''')
            cycle=json.loads((OUT/'native_cycle.json').read_text())
            for row in cycle['rows']:
                h,a=row['H'],row['Tilt'];rot=matrix(a)
                center=np.array([h/2,h,h/3])+rot@np.array([h*.075,h*.1,h*.05])
                radii=np.array([h*.15,h*.225,h*.075]);extent=np.sqrt((rot*rot)@(radii*radii))
                np.testing.assert_allclose(row['bbox']['min'],center-extent,atol=.02)
                np.testing.assert_allclose(row['bbox']['max'],center+extent,atol=.02)
                assert abs(row['volume']/(4*math.pi/3*np.prod(radii))-1)<.001
            cycle['status']='NATIVE_FRAME_CYCLE_PASS'
            (OUT/'native_cycle.json').write_text(json.dumps(cycle,indent=2))
            # Same topology changes angle expressions without replacing native IDs.
            revised=CODE.replace('Tilt*.75','Tilt*.85')
            edit=await call('execute_model_code',{**await args(),'mode':'edit','code':revised,'observe':False})
            assert edit['native_feature_ids_preserved']
            await call('execute_model_code',{**await args(),'mode':'edit','code':CODE,'observe':False})
            # Change active axes, preserving independent solids and rebuilding the Mirror dependency.
            revised=CODE.replace('Tilt,-Tilt/2,Tilt*.75','Tilt,0,Tilt*.75')
            changed=await call('execute_model_code',{**await args(),'mode':'edit','code':revised,'observe':False})
            assert set(changed['rebuilt_components'])=={'Placed','Pair'}
            assert 'Independent' in changed['preserved_components']
            await call('execute_model_code',{**await args(),'mode':'edit','code':CODE,'observe':False})
            await verify(40,.4)
            await call('observe_model',{**await args(),'directory':str(OUT/'final_views')})
            status=await call('get_executor_status',{})
            assert not status['worker_busy'] and not status['quarantined']
            report['status']='NATIVE_PLACEMENT_PASS';report['native_cycle']=cycle;save()
            print(report['status'],flush=True)


asyncio.run(main())
