"""Opt-in live qualification helpers using the normal executor and MCP dispatcher."""

import asyncio
import hashlib
import json
import platform
import time
import traceback
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from solidworks_mcp import __version__, server
from solidworks_mcp.automation.runtime import atomic_json_write


class LiveRun:
    def __init__(self, output):
        self.output=Path(output).resolve()
        self.output.mkdir(parents=True,exist_ok=False)
        self.report={'mcp_version':__version__,'platform':platform.platform(),'python':sys.version,
                     'started_at':time.time(),'qualification':'IN_PROGRESS','calls':[]}
        sources={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted((ROOT/'solidworks_mcp').rglob('*.py'))}
        self.report['source_sha256']=hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest()

    def save(self):
        atomic_json_write(self.output/'qualification.json',self.report)

    async def call(self,name,arguments,*,allow_failure=False):
        started=time.perf_counter()
        self.report['active_call']={'tool':name,'arguments':{k:v for k,v in arguments.items() if k!='code'},
                                    'started_at':time.time()}
        self.save()
        result=(await server._dispatch_tool(name,arguments)).structuredContent
        snapshots=[]
        while result.get('success') and result.get('data',{}).get('status')=='RUNNING':
            op=result['data']['operation_id']
            status_started=time.perf_counter()
            status=(await server._dispatch_tool('get_executor_status',{'operation_id':op})).structuredContent
            metrics=(await server._dispatch_tool('expert_tools',{
                'action':'call','name':'get_session_metrics','arguments':{}})).structuredContent
            snapshots.append({'status':status,'metrics':metrics,
                              'response_sec':time.perf_counter()-status_started})
            self.report['active_call']['snapshots']=snapshots
            self.save()
            reader=status.get('data',{}).get('reader') or {}
            progress=reader.get('progress') or {}
            print(name,'RUNNING',progress.get('phase'),progress.get('completed'),
                  '/',progress.get('total'),flush=True)
            assert status['success'] and metrics['success'],(status,metrics)
            result=(await server._dispatch_tool('wait_operation',{'operation_id':op})).structuredContent
        self.report['calls'].append({'tool':name,'arguments':{k:v for k,v in arguments.items() if k!='code'},
                                     'elapsed_sec':time.perf_counter()-started,'result':result,'snapshots':snapshots})
        self.report.pop('active_call',None)
        self.save()
        print(name,result.get('success'),result.get('error_name'),flush=True)
        if not allow_failure:
            assert result.get('success'),result
        return result

    async def expert(self,name,**arguments):
        return await self.call('expert_tools',{'action':'call','name':name,'arguments':arguments})

    async def context(self,path=None):
        if path:
            opened=await self.expert('open_document',filepath=str(path))
            if 'environment' not in self.report:
                environment=await self.expert('get_environment_status')
                self.report['environment']=environment
        return (await self.call('get_model_context',{}))['data']

    async def inspect(self,path,revision):
        result=await self.call('inspect_model',{'document_path':str(path),'expected_revision':revision,
                                               'precision':'delivery'})
        assert all(b['kernel_health']['passed'] for b in result['data']['bodies']),result
        return result['data']

    async def ready(self):
        status=await self.call('get_executor_status',{})
        assert not status['data']['busy'] and not status['data']['quarantined'],status
        self.report['initial_status']=status['data']


def run_script(function,args):
    run=LiveRun(args.output)
    try:
        asyncio.run(function(run,args))
        run.report['qualification']='PASS'
    except BaseException as exc:
        run.report.update(qualification='FAILED',error=str(exc),error_type=type(exc).__name__,
                          traceback=traceback.format_exc())
        raise
    finally:
        run.report['finished_at']=time.time()
        run.save()
        server.cad_executor.close()
