"""Opt-in durable retry and shared-owner watchdog acceptance on a new fixture."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
PROGRAM="""H=model.parameter('H',40)
model.tube('Round',[(-15,0,0),(-15,H,0)],5)
model.rounded_prism('Flat',10,H,2,1,center=(15,H/2,0))
"""


async def acceptance(output, report_path, other_document):
    if not other_document.is_file() or other_document == output:
        raise ValueError('Supply a separate existing part with --other-document.')
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')],cwd=str(ROOT))
    report={'status':'IN_PROGRESS','calls':[]}
    async def call(session,name,args,success=True):
        start=time.monotonic()
        result=await session.call_tool(name,args)
        value=result.structuredContent
        report['calls'].append({'name':name,'elapsed_sec':round(time.monotonic()-start,3),
            'success':value.get('success') if value else None,'error_name':value.get('error_name') if value else None})
        assert bool(result.isError) is (not success),value
        return value
    create={'mode':'create','code':PROGRAM,'output_path':str(output),'observe':False}
    try:
        async with stdio_client(params) as (r,w):
            async with ClientSession(r,w) as session:
                init=await session.initialize();assert init.serverInfo.version=='6.13.0'
                await call(session,'get_environment_status',{})
                original=await call(session,'execute_model_code',create)
                report['create']=original
                retry=await call(session,'execute_model_code',create)
                assert retry['data']['request_receipt']['replayed']
                ctx=(await call(session,'get_model_context',{}))['data']
                assert ctx['parameters']['H']==40 and ctx['body_state']['count']==2
                edit={'mode':'edit','document_path':str(output),'expected_revision':ctx['revision'],
                    'parameters':{'H':44},'observe':False,'idempotency_key':output.stem+'-edit44'}
                first=await call(session,'execute_model_code',edit)
                assert first['data']['native_feature_ids_preserved']
                assert first['data']['request_receipt']['post_state_verified']
                report['edit']=first
                retry=await call(session,'execute_model_code',edit)
                assert retry['data']['request_receipt']['replayed']
                assert retry['data']['revision']==first['data']['revision']
                conflict=await call(session,'execute_model_code',{**edit,'parameters':{'H':45}},False)
                assert conflict['error_name']=='IDEMPOTENCY_CONFLICT'
                conflict=await call(session,'execute_model_code',{**edit,'document_path':str(output.with_name('Other.SLDPRT'))},False)
                assert conflict['error_name']=='IDEMPOTENCY_CONFLICT'
                await call(session,'close_document',{'save':False})
                await call(session,'open_document',{'filepath':str(output)})
                retry=await call(session,'execute_model_code',edit)
                assert retry['data']['request_receipt']['replayed']
                report['reopen_replay']=retry['data']['request_receipt']
                print('RECEIPTS_NATIVE_PASS',flush=True)
        async with stdio_client(params) as (r,w):
            async with ClientSession(r,w) as session:
                await session.initialize()
                retry=await call(session,'execute_model_code',edit)
                assert retry['data']['request_receipt']['replayed']
                report['mcp_restart_replay']=retry
                state=await call(session,'get_request_status',{'idempotency_key':edit['idempotency_key']})
                assert state['data']['receipt']['status']=='COMPLETED'
                await call(session,'open_document',{'filepath':str(other_document)})
                switched=await call(session,'execute_model_code',edit,False)
                assert switched['error_name']=='DOCUMENT_MISMATCH'
                await call(session,'open_document',{'filepath':str(output)})
                ctx=(await call(session,'get_model_context',{}))['data']
                back=await call(session,'execute_model_code',{'mode':'edit','document_path':str(output),
                    'expected_revision':ctx['revision'],'parameters':{'H':40},'observe':False})
                assert back['data']['parameters']['H']==40
                changed=await call(session,'execute_model_code',edit,False)
                assert changed['error_name']=='IDEMPOTENCY_STATE_CHANGED'
                ctx=(await call(session,'get_model_context',{}))['data']
                tx={'name':'ReceiptSave','operations':[],'document_path':str(output),
                    'expected_revision':ctx['revision'],'idempotency_key':output.stem+'-transaction'}
                committed=await call(session,'run_transaction',tx)
                replay=await call(session,'run_transaction',tx)
                assert replay['data']['request_receipt']['replayed']
                report['transaction']={'first':committed,'replay':replay}
                conflict=await call(session,'run_transaction',{**tx,'invariants':{'solid_body_count':3}},False)
                assert conflict['error_name']=='IDEMPOTENCY_CONFLICT'
                print('DURABLE_RESTART_AND_LEGACY_PASS',flush=True)
                ctx=(await call(session,'get_model_context',{}))['data']
                background=await call(session,'execute_python_async',{'code':
                    "import time; time.sleep(.5); result = com_get(doc, 'GetTitle')",
                    'ui_guard':{'process_isolated':True,'interval_sec':.1,'max_runtime_sec':10,
                                'capture_screenshot':False}})
                status=await call(session,'get_job_result',{'job_id':background['data']['job_id'],'timeout':15})
                operation=status['data']['operation']
                assert operation['status']=='COMPLETED',operation
                native=operation['result']
                assert native['success'] and output.stem in native['data']['result']
                assert native['data']['watchdog']['native_execution_owner']=='shared_STA'
                assert native['data']['watchdog']['event'] is None
                assert native['data']['executor']['sta_thread_id']==ctx['executor']['sta_thread_id']
                report['watchdog']=native
                after=(await call(session,'get_model_context',{}))['data']
                assert after['revision']==ctx['revision']
                obs=await call(session,'observe_model',{'document_path':str(output),'expected_revision':after['revision'],
                    'directory':str(output.parent/'receipt_views'),'views':['isometric'],'width':850})
                report['observation']=obs
                report['final_context']=after
                report['status']='LIVE_REQUEST_RECEIPTS_PASS'
                print(report['status'],flush=True)
    finally:
        report_path.write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--report',required=True)
    parser.add_argument('--other-document', required=True, help='Existing separate part for the document-switch check')
    args=parser.parse_args();asyncio.run(acceptance(Path(args.output).resolve(),Path(args.report).resolve(),Path(args.other_document).resolve()))
