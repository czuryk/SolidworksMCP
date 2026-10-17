"""Opt-in Test A: shared analytic volumes, native sections, edits and H rebuild.

The input is an engineering oracle, never input to the short-prompt benchmark.
Run with a new disposable directory containing shape_a.py.
"""
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
OUT=Path(sys.argv[1]).resolve()
DOC=OUT/'Shape_A.SLDPRT'
S=150/2.398

def section_queries():
    q=[]
    for f in (.25,.75):
        q.append({'component':'Torso','center':[0,(.757+(1.425-.757)*f)*S,0],
                  'normal':[0,1,0],'samples':32})
    for t in (.25,.75):
        q.append({'component':'Leg_Left',
                  'center':[(.119+.113*math.sin(t*math.pi*.72))*S,(.77-.622*t)*S,
                            -(.023-.065*math.sin(math.pi*t))*S],
                  'normal':[.113*math.pi*.72*math.cos(t*math.pi*.72),-.622,
                            .065*math.pi*math.cos(math.pi*t)],
                  'samples':32,'expected_radius':.053*S,'tolerance_mm':.04})
    return q

async def main():
    report={'status':'IN_PROGRESS','calls':[], 'scope':
            'Torso central taper, smooth ungrooved leg axis, upper ellipsoid foot; trim lips and segmentation excluded. H is character normalization, not fixture height.'}
    def save():
        (OUT/'analytic_verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],cwd=str(ROOT))
    async with stdio_client(params) as (r,w):
        async with ClientSession(r,w) as client:
            await client.initialize()
            async def call(name,args):
                start=time.monotonic();result=(await client.call_tool(name,args)).structuredContent
                report['calls'].append({'name':name,'elapsed_sec':round(time.monotonic()-start,3),'result':result})
                save();print(name,result['success'],flush=True)
                assert result['success'],result
                return result['data']
            if DOC.exists():
                await call('expert_tools',{'action':'call','name':'open_document','arguments':{'filepath':str(DOC)}})
            else:
                await call('execute_model_code',{'mode':'create','code':(OUT/'shape_a.py').read_text(),
                    'output_path':str(DOC),'observe':True})
            async def context():return await call('get_model_context',{'include_source':True})
            ctx=await context()
            args={'document_path':str(DOC),'expected_revision':ctx['revision']}
            facts=await call('inspect_model',{**args,'precision':'delivery','sections':section_queries()})
            assert len(facts['bodies'])==5
            assert all(b['kernel_health']['passed'] for b in facts['bodies'])
            bodies={b['name']:b for b in facts['bodies']}
            torso=bodies['Torso'];foot=bodies['Foot_Left']
            expected_torso=math.pi*.82*((1.425-.757)*S)/3*((.239*S)**2+.239*.30*S*S+(.30*S)**2)
            expected_foot=2*math.pi/3*(.172*S)*(.143*S)*(.172*1.06*S)
            volume_errors={'Torso':abs(torso['volume_exact']/expected_torso-1),
                           'Foot_Left':abs(foot['volume_exact']/expected_foot-1)}
            assert max(volume_errors.values())<.001,volume_errors
            ellipse_errors=[]
            for f,section in zip((.25,.75),facts['sections'][:2]):
                rx=(.239+(.30-.239)*f)*S;rz=rx*.82
                expected=[1/math.sqrt((math.cos(i*math.tau/32)/rx)**2+(math.sin(i*math.tau/32)/rz)**2) for i in range(32)]
                assert section['status']=='SAMPLED'
                ellipse_errors.append(max(abs(a-b) for a,b in zip(section['radii_mm'],expected)))
            assert max(ellipse_errors)<.02,ellipse_errors
            assert all(s['radius_check']['passed'] for s in facts['sections'][2:]),facts['sections']
            assert abs(foot['dimensions_mm'][0]-2*.172*S)<.02
            assert abs(foot['dimensions_mm'][1]-.143*S)<.02
            assert abs(foot['dimensions_mm'][2]-2*.172*1.06*S)<.02
            mirrored=[b for b in facts['bodies'] if b['name'].startswith('Leg_Foot_Pair')]
            assert len(mirrored)==2
            for b in mirrored:
                original=min((bodies['Leg_Left'],foot),key=lambda a:abs(a['volume_exact']-b['volume_exact']))
                # Native B-spline mass integration is approximate. Reflection is
                # established by the native Mirror feature and reflected bounds;
                # volume is a numerical consistency check, not exact arithmetic.
                assert any(c['native_feature_type']=='MirrorSolid' for c in b['components'])
                assert math.isclose(b['volume_exact'],original['volume_exact'],rel_tol=1e-4)
                assert abs(b['bbox']['min'][0]+original['bbox']['max'][0])<.01
                assert abs(b['bbox']['max'][0]+original['bbox']['min'][0])<.01
                for side in ('min','max'):
                    assert all(abs(b['bbox'][side][i]-original['bbox'][side][i])<.01 for i in (1,2))
            report['geometry']={'relative_volume_errors':volume_errors,'ellipse_max_errors_mm':ellipse_errors,
                                'native_mirror_verified':True,'native_bodies':5,'mesh_imports':0}
            save()
            await call('execute_model_code',{**args,'mode':'edit','parameters':{'Depth':.9},'observe':False})
            ctx=await context()
            changed=await call('inspect_model',{'document_path':str(DOC),'expected_revision':ctx['revision'],'precision':'delivery'})
            volumes={b['name']:b['volume_exact'] for b in changed['bodies']}
            assert math.isclose(volumes['Torso']/torso['volume_exact'],.9/.82,rel_tol=.001)
            assert all(math.isclose(volumes[n],b['volume_exact'],rel_tol=1e-7) for n,b in bodies.items() if n!='Torso')
            await call('execute_model_code',{'mode':'edit','document_path':str(DOC),'expected_revision':ctx['revision'],
                       'parameters':{'Depth':.82},'observe':False})
            # Only Equation Manager and native rebuild are used in this proof.
            raw=f'''import json, math
from pathlib import Path
from solidworks_mcp.automation.shape_parametric import read_record, restore, validate_feature_health
target=Path({str(DOC)!r})
assert Path(com_get(doc,'GetPathName')).resolve()==target
sw.CloseDoc(com_get(doc,'GetTitle'))
assert automation.open_document(str(target))['success']
doc=sw.ActiveDoc
record=read_record(doc)
refs=record['native']['feature_refs']
def masses():
    return {{str(com_get(b,'Name')):automation._body_mass_props(b,'mm')['volume_exact'] for b in automation._get_solid_bodies(doc)}}
before=masses();cycles=[]
eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
for h in (165,150):
    eq.SetEquation(record['native']['parameter_bindings']['H']['global_index'],f'"H" = {{h}}mm')
    com_get(eq,'EvaluateAll')
    assert doc.ForceRebuild3(False)
    validate_feature_health(automation,doc,list(refs)+record['native']['support_features'])
    ratios={{n:v/before[n] for n,v in masses().items()}}
    assert all(math.isclose(v,(h/150)**3,rel_tol=.001) for v in ratios.values()),ratios
    cycles.append({{'H':h,'volume_ratios':ratios}})
assert all(restore(doc,ref) is not None for ref in refs.values())
assert automation.save_document(str(target))['success']
sw.CloseDoc(com_get(doc,'GetTitle'))
assert automation.open_document(str(target))['success']
doc=sw.ActiveDoc
assert read_record(doc)['native']['feature_refs']==refs
result={{'status':'NATIVE_CYCLE_PASS','cycles':cycles,'native_ids_preserved':True,'reopened':True}}
Path({str(OUT/'native_cycle.json')!r}).write_text(json.dumps(result,indent=2))
print(json.dumps(result))
'''
            await call('expert_tools',{'action':'call','name':'execute_python','arguments':{'code':raw}})
            ctx=await context()
            expected={'H':150,'Depth':.82,'LegRadius':1}
            assert set(ctx['parameters'])==set(expected)
            assert all(math.isclose(ctx['parameters'][k],v,rel_tol=1e-12) for k,v in expected.items())
            report['status']='ANALYTIC_NATIVE_PASS';save()
            print(report['status'],report['geometry'],flush=True)

asyncio.run(main())
