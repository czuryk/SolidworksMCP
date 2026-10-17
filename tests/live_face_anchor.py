"""Opt-in public MCP acceptance for native face attachments and scoped compile."""
import asyncio,base64,hashlib,json,sys,time
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
OUT=Path(sys.argv[1]).resolve();OUT.mkdir(parents=True,exist_ok=True)
TARGET=OUT/'Anchor_Release.SLDPRT'
SOURCE="""Rx=model.parameter('Rx',10)
Ry=model.parameter('Ry',20)
Rz=model.parameter('Rz',6)
X=model.parameter('X',32)
Radius=model.parameter('Radius',2)
Depth=model.parameter('Depth',2)
model.ellipsoid('Witness',(3,4,2),center=(-10,10,0))
host=model.ellipsoid('Host',(Rx,Ry,Rz),center=(30,25,10),approximate=True)
anchor=model.face_anchor(host,origin=(X,30,10),direction=(0,0,1))
model.anchored_extrude('Boss',anchor,{'circle':Radius},Depth)
"""

async def main():
 report={'status':'IN_PROGRESS','calls':[]}
 def save():(OUT/'release_live_verification.json').write_text(json.dumps(report,indent=2))
 params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],cwd=str(ROOT))
 async with stdio_client(params) as (r,w):
  async with ClientSession(r,w) as client:
   report['version']=(await client.initialize()).serverInfo.version
   assert report['version']=='6.20.0'
   async def call(name,args,ok=True):
    start=time.monotonic();raw=await client.call_tool(name,args);data=raw.structuredContent
    report['calls'].append({'name':name,'seconds':time.monotonic()-start,'result':data});save()
    print(name,data['success'],data.get('message','')[:140],flush=True);assert data['success']==ok,data
    return raw,data.get('data')
   async def context():return (await call('get_model_context',{}))[1]
   async def expert(code):return await call('expert_tools',{'action':'call','name':'execute_python','arguments':{'code':code}})
   await call('execute_model_code',{'mode':'create','code':SOURCE,'output_path':str(TARGET),'observe':False})
   c=await context();assert c['components']['Boss']['attachment']=='NATIVE_FACE_INTERSECTION'
   assert 'attachment_frame' in c['components']['Boss'] and 'native_anchor' not in c['components']['Boss']
   assert 'native_profile' not in c['components']['Host']
   await expert((ROOT/'tests/fixtures/face_anchor_cycle.py').read_text().replace('__OUTPUT_DIRECTORY__',OUT.as_posix()))
   c=await context()
   await call('execute_model_code',{'mode':'edit','document_path':str(TARGET),'expected_revision':c['revision'],
                                   'parameters':{'X':32.5,'Radius':2.2},'observe':False})
   c=await context()
   await call('execute_model_code',{'mode':'edit','document_path':str(TARGET),'expected_revision':c['revision'],
                                   'parameters':{'X':32,'Radius':2},'observe':False})
   c=await context();identity={'document_path':str(TARGET),'expected_revision':c['revision']}
   _,facts=await call('inspect_model',{**identity,'precision':'delivery'})
   assert len(facts['bodies'])==3 and all(b['kernel_health']['passed'] for b in facts['bodies'])
   raw,views=await call('observe_model',{**identity,'directory':str(OUT/'views')})
   images=[item for item in raw.content if item.type=='image'];assert len(images)==3
   for img,meta in zip(images,views['images']):
    assert hashlib.sha256(base64.b64decode(img.data)).hexdigest()==meta['delivered_sha256']
   checksum=hashlib.sha256(TARGET.read_bytes()).hexdigest()
   await call('export_model',{**identity,'directory':str(OUT/'delivery'),'expected_bodies':['Host','Boss','Witness']})
   assert hashlib.sha256(TARGET.read_bytes()).hexdigest()==checksum
   _,state=await expert("from solidworks_mcp.automation.com_utils import com_get\nprint(com_get(sw,'CommandInProgress'))\nprint(com_get(doc,'GetSaveFlag'))")
   assert state['stdout'].split()==['False','False']
   report['status']='ANCHOR_RELEASE_6_20_PASS';save()

asyncio.run(main())
