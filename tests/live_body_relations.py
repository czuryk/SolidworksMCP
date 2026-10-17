"""Opt-in public acceptance: body pairs, native edits and read-only integrity."""
import asyncio
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
TARGET = OUT/'Relations_Release.SLDPRT'
SOURCE = """R=model.parameter('R',5)
Separation=model.parameter('Separation',10.25)
model.ellipsoid('Outer',(R,R,R))
model.ellipsoid('Other',(R,R,R),center=(Separation,0,0))
model.ellipsoid('Inner',(2,2,2))
model.ellipsoid('Identical',(R,R,R))
model.revolve('CylinderA',[(20,2),(30,2)])
model.revolve('CylinderB',[(20,2),(30,2)],center_xz=(4.25,0))
"""
PAIRS = [
 {'id':'spheres','a':{'component':'Outer'},'b':{'component':'Other'},'min_clearance_mm':.2,'max_clearance_mm':.3},
 {'id':'nested','a':{'body':'Outer'},'b':{'component':'Inner'},'min_clearance_mm':.2},
 {'id':'identical','a':{'body':'Outer'},'b':{'body':'Identical'},'min_clearance_mm':0},
 {'id':'cylinders','a':{'component':'CylinderA'},'b':{'component':'CylinderB'},'min_clearance_mm':.2,'max_clearance_mm':.3},
]
SNAPSHOT = """import json
from solidworks_mcp.automation.com_utils import com_get, typed
from solidworks_mcp.automation.shape_parametric import read_record
mgr=typed(doc.SelectionManager,'ISelectionMgr')
selections=[]
for i in range(1,mgr.GetSelectedObjectCount2(-1)+1):
    obj=mgr.GetSelectedObject6(i,-1)
    selections.append([mgr.GetSelectedObjectType3(i,-1),mgr.GetSelectedObjectMark(i),str(com_get(obj,'Name'))])
print(json.dumps({'dirty':bool(com_get(doc,'GetSaveFlag')),'stamp':int(com_get(doc,'GetUpdateStamp')),
 'selection':selections,'ids':read_record(doc)['native']['feature_refs'],
 'command':bool(com_get(sw,'CommandInProgress'))}))
"""


async def main():
 report={'status':'IN_PROGRESS','calls':[]}
 def save(): (OUT/'live_verification.json').write_text(json.dumps(report,indent=2))
 config=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],cwd=str(ROOT))
 async with stdio_client(config) as (reader,writer):
  async with ClientSession(reader,writer) as client:
   report['version']=(await client.initialize()).serverInfo.version
   async def call(name,args,ok=True):
    t=time.monotonic(); result=(await client.call_tool(name,args)).structuredContent
    report['calls'].append({'tool':name,'seconds':time.monotonic()-t,'result':result});save()
    print(name,result['success'],result.get('message','')[:90],flush=True)
    assert result['success']==ok,result
    return result.get('data',{})
   async def raw(code):
    data=await call('expert_tools',{'action':'call','name':'execute_python','arguments':{'code':code}})
    return data['stdout']
   async def context(): return await call('get_model_context',{})
   async def target():
    ctx=await context()
    return {'document_path':str(TARGET),'expected_revision':ctx['revision']}
   if TARGET.exists():
    await call('expert_tools',{'action':'call','name':'open_document','arguments':{'filepath':str(TARGET)}})
   else:
    await call('execute_model_code',{'mode':'create','code':SOURCE,'output_path':str(TARGET),'observe':False})
   await raw("title=automation._get_doc_title(doc)\nsw.CloseDoc(title)\nr=automation.open_document("+repr(str(TARGET))+")\nassert r['success'],r")
   saved_ids=None
   for separation,relation,passed in [(10.25,'SEPARATED',True),(10.,'CONTACT_OR_NEAR',False),(9.,'OVERLAP',False),(10.25,'SEPARATED',True)]:
    await call('execute_model_code',{'mode':'edit',**await target(),'parameters':{'Separation':separation},'observe':False})
    await raw("from solidworks_mcp.automation.com_utils import create_select_data\nb=automation._find_body(doc,'Outer')\nassert b.Select2(False,create_select_data(doc,7))")
    before=json.loads(await raw(SNAPSHOT)); digest=hashlib.sha256(TARGET.read_bytes()).hexdigest()
    identity=await target()
    facts=await call('inspect_model',{**identity,'body_pairs':PAIRS,'precision':'delivery'})
    pairs={x['id']:x for x in facts['body_pairs']}
    assert pairs['spheres']['relation']==relation,pairs
    assert pairs['spheres']['clearance_check']['passed']==passed,pairs
    assert pairs['nested']['relation']=='OVERLAP' and pairs['nested']['boundary_distance_mm']>2.99,pairs
    assert pairs['nested']['clearance_check']['passed'] is False,pairs
    assert pairs['identical']['relation']=='OVERLAP' and pairs['identical']['clearance_check']['passed'] is False,pairs
    assert math.isclose(pairs['cylinders']['clearance_mm'],.25,abs_tol=1e-5),pairs
    assert pairs['cylinders']['clearance_check']['passed'],pairs
    if separation==9:
     expected=math.pi*(4*5+9)*(2*5-9)**2/12
     # Native mass integration differs by 4.13e-5 although exact spherical faces
     # and extrema match (mcp_batch16/intersection_geometry.json).
     assert math.isclose(pairs['spheres']['intersection_volume_mm3'],expected,rel_tol=1e-4),pairs
    after=json.loads(await raw(SNAPSHOT))
    assert before==after and not after['dirty'] and not after['command'],(before,after)
    assert hashlib.sha256(TARGET.read_bytes()).hexdigest()==digest
    if saved_ids is None:saved_ids=after['ids']
    assert saved_ids==after['ids']
   legacy=json.loads(await raw("import json\nprint(json.dumps(automation.check_clearance('Outer','Other',min_clearance=.2,unit='mm')))"))
   assert legacy['success'] and legacy['data']['clearance_ok'],legacy
   overlap=json.loads(await raw("import json\nprint(json.dumps(automation.check_clearance('Outer','Inner',min_clearance=.2,unit='mm')))"))
   assert not overlap['success'] and overlap['data']['clearance_ok'] is False,overlap
   assert json.loads(await raw(SNAPSHOT))==after
   await call('inspect_model',{'document_path':str(TARGET),'expected_revision':'stale','body_pairs':PAIRS},False)
   await raw('doc.ClearSelection2(True)')
   report['status']='BODY_RELATIONS_LIVE_PASS';save()

asyncio.run(main())
