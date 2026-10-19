"""Opt-in live MCP acceptance; delay Python only, never intentionally hang CAD."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from fixtures.reference_image import write_reference

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = """H=model.parameter('H',40)
model.tube('Round',[(-15,0,0),(-15,H,0)],5)
model.rounded_prism('Flat',10,H,2,1,center=(15,H/2,0))
"""


async def acceptance(output, report_path):
    reference = write_reference(output.with_suffix('.reference.png'))
    params = StdioServerParameters(command=sys.executable, args=['-X','utf8',str(ROOT/'solidworks_mcp_server.py')], cwd=str(ROOT))
    report = {'status':'IN_PROGRESS', 'calls':[]}

    async def call(session, name, args, success=True):
        start = time.monotonic()
        result = await session.call_tool(name, args)
        value = result.structuredContent
        report['calls'].append({'name':name, 'elapsed_sec':round(time.monotonic()-start,3),
                                'success':value.get('success') if value else None,
                                'error_name':value.get('error_name') if value else None})
        if success:
            assert not result.isError and value['success'], value
        else:
            assert result.isError, value
        return result

    try:
        async with stdio_client(params) as (r,w):
            async with ClientSession(r,w) as session:
                init = await session.initialize()
                assert init.serverInfo.version == '6.12.0'
                report['version'] = init.serverInfo.version
                schema = await session.list_tools()
                for name in ['get_executor_status','reconcile_session']:
                    assert any(t.name == name for t in schema.tools)
                await call(session,'get_environment_status',{})
                await call(session,'execute_model_code',{'mode':'create','code':PROGRAM,'output_path':str(output),'observe':False})
                ctx = (await call(session,'get_model_context',{})).structuredContent['data']
                owner = ctx['executor']['sta_thread_id']
                observed = await call(session,'observe_model',{'document_path':str(output), 'expected_revision':ctx['revision'],
                    'directory':str(output.parent/'executor_views'),'width':850,
                    'sections':[{'component':'Round','center':[-15,20,0],'normal':[0,1,0], 'expected_radius':5,'tolerance_mm':.01}]})
                assert len([c for c in observed.content if c.type=='image']) == 3
                report['observation'] = observed.structuredContent['data']
                assert report['observation']['sections'][0]['radius_check']['passed']
                for height in [44,40]:
                    ctx = (await call(session,'get_model_context',{})).structuredContent['data']
                    edit = (await call(session,'execute_model_code',{'mode':'edit','document_path':str(output),
                        'expected_revision':ctx['revision'],'parameters':{'H':height},'observe':False})).structuredContent['data']
                    assert edit['parameters']['H']==height and edit['native_feature_ids_preserved']
                    assert edit['executor']['sta_thread_id']==owner
                await call(session,'close_document',{'save':False})
                await call(session,'open_document',{'filepath':str(output)})
                ctx = (await call(session,'get_model_context',{})).structuredContent['data']
                assert ctx['parameters']['H']==40 and ctx['body_state']['count']==2
                report['round_trip'] = ctx
                print('NATIVE_ROUND_TRIP_PASS',flush=True)

                background = (await call(session,'execute_python_async',{'code':
                    "import time; time.sleep(2); result = com_get(doc, 'GetTitle')"})).structuredContent['data']
                await asyncio.sleep(.15)
                state = (await call(session,'get_executor_status',{})).structuredContent['data']
                assert state['worker_busy']
                await call(session,'measure_reference',{'image_path':str(reference),
                    'queries':[{'id':'width','kind':'width_at_height','region':[10,10,190,230],'y':120}]})
                queued = await call(session,'get_document_info',{'execution_timeout_sec':.05},False)
                assert queued.structuredContent['error_name']=='NOT_EXECUTED'
                result = (await call(session,'get_job_result',{'job_id':background['job_id'],'timeout':5})).structuredContent['data']['operation']
                assert result['status']=='COMPLETED'
                assert output.stem in result['result']['data']['result']
                assert result['result']['data']['executor']['sta_thread_id']==owner
                report['background'] = result
                print('RESPONSIVE_MCP_PASS',flush=True)

                timeout = await call(session,'execute_python',{'code':
                    "import time; time.sleep(2); result = com_get(doc, 'GetTitle')",
                    'execution_timeout_sec':.4},False)
                assert timeout.structuredContent['error_name']=='OUTCOME_UNKNOWN',timeout
                operation_id = timeout.structuredContent['data']['operation_id']
                blocked = await call(session,'get_model_context',{},False)
                assert blocked.structuredContent['error_name']=='SESSION_QUARANTINED'
                await asyncio.sleep(2.5)
                late = (await call(session,'get_executor_status',{'operation_id':operation_id})).structuredContent['data']
                assert late['journal']['status']=='RETURNED_LATE' and not late['worker_busy'],late
                report['late_return'] = late
        # Reconnect the actual stdio server, retaining the durable quarantine.
        async with stdio_client(params) as (r,w):
            async with ClientSession(r,w) as session:
                await session.initialize()
                state = (await call(session,'get_executor_status',{})).structuredContent['data']
                assert state['journal']['operation_id']==operation_id
                blocked = await call(session,'get_model_context',{},False)
                assert blocked.structuredContent['error_name']=='SESSION_QUARANTINED'
                recovery = (await call(session,'reconcile_session',{'operation_id':operation_id})).structuredContent['data']
                assert recovery['reconciliation']['original_operation_replayed'] is False
                assert recovery['parameters']['H']==40
                report['reconciliation'] = recovery
                final = (await call(session,'get_model_context',{})).structuredContent['data']
                assert final['parameters']['H']==40 and final['body_state']['count']==2
                assert final['revision']==ctx['revision']
                report['final_context'] = final
                report['status'] = 'LIVE_STA_EXECUTOR_PASS'
                print(report['status'],flush=True)
    finally:
        report_path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--report',required=True)
    args=parser.parse_args()
    asyncio.run(acceptance(Path(args.output).resolve(),Path(args.report).resolve()))
