"""Progress stays available through the actual MCP route while STA is blocked."""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from solidworks_mcp import server
from solidworks_mcp.automation.runtime import RuntimeState
from solidworks_mcp.automation.progress import update_progress, progress_snapshot
from solidworks_mcp.automation.sta_executor import StaExecutor


class ProgressTests(unittest.TestCase):
    def test_heartbeat_never_advances_counter_or_timestamp(self):
        p=update_progress(None,{'stage':'read','completed':10,'total':100,'unit':'facets'},100)
        p=update_progress(p,{'stage':'read','completed':9},102)
        self.assertEqual(p['completed'],10)
        self.assertEqual(p['last_advance_at'],100)
        self.assertEqual(progress_snapshot(p,105)['seconds_without_progress'],5)
        self.assertEqual(progress_snapshot(p,105)['phase_percent'],10)
        next_phase=update_progress(p,{'stage':'write'},106)
        self.assertIsNone(next_phase['completed'])
        self.assertIsNone(next_phase['total'])
        reset=update_progress(p,{'stage':'read','completed':0,'reset':True},107)
        self.assertEqual(reset['completed'],0)
        self.assertNotIn('reset',reset)
        advanced=update_progress(reset,{'stage':'read','completed':20},108)
        self.assertEqual(advanced['started_at'],107)

    def test_busy_executor_metrics_and_delayed_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            executor=StaExecutor(Path(directory)/'session.lock',identity_provider=lambda:[],
                                 initialize=lambda:None,finalize=lambda:None,pump=lambda:None)
            runtime=RuntimeState(); runtime.progress_sink=executor.report_stage
            entered, release=threading.Event(), threading.Event()
            def callback():
                runtime.record_progress('tessellation_read',completed=250,total=1000,unit='facets')
                runtime.begin_com_read('GetVertexPoint',123)
                entered.set(); release.wait(3)
                runtime.end_com_read(.1)
                runtime.record_progress('tessellation_read',completed=1000,total=1000,unit='facets')
                return {'success':True,'data':{'original':'result'}}
            try:
                op=executor.submit('export_model',{},callback,detached=True)
                self.assertTrue(entered.wait(1))
                with patch.object(server,'cad_executor',executor), \
                     patch.object(server.sw_automation,'_runtime',runtime), \
                     patch.object(server.sw_automation,'ui_preflight',side_effect=AssertionError('COM metrics')), \
                     patch.object(server.sw_automation,'_document_snapshot',side_effect=AssertionError('COM metrics')):
                    started=time.perf_counter()
                    metrics=asyncio.run(server._dispatch_tool('expert_tools',{
                        'action':'call','name':'get_session_metrics','arguments':{}})).structuredContent
                    self.assertLess(time.perf_counter()-started,1)
                    self.assertTrue(metrics['success'],metrics)
                    self.assertEqual(metrics['data']['current_com_call']['method'],'GetVertexPoint')
                    a=executor.operation_snapshot(op)['progress']['seconds_without_progress']
                    time.sleep(.02)
                    status=asyncio.run(server._dispatch_tool('get_executor_status',{})).structuredContent
                    self.assertGreater(status['data']['active']['progress']['seconds_without_progress'],a)
                    waited=asyncio.run(server._wait_operation(op.id,0))[1]
                    self.assertEqual(waited['data']['progress']['completed'],250)
                release.set()
                final=op.response.result(2)
                self.assertEqual(final['data']['original'],'result')
                self.assertEqual(final['data']['executor']['progress']['completed'],1000)
            finally:
                release.set(); executor.close()

    def test_notifications_include_counters_without_phase_change(self):
        class Session:
            def __init__(self): self.messages=[]
            async def send_progress_notification(self,*args,**kwargs): self.messages.append((args,kwargs))
        class Executor:
            calls=0
            def operation_snapshot(self,op):
                self.calls+=1
                return {'stage':'read','progress':{'completed':self.calls,'total':10}}
        async def scenario():
            session=Session(); executor=Executor()
            with patch.object(server,'cad_executor',executor):
                task=asyncio.create_task(server._report_stages(session,'token',object()))
                await asyncio.sleep(1.05); task.cancel()
                try: await task
                except asyncio.CancelledError: pass
            return session.messages
        messages=asyncio.run(scenario())
        self.assertEqual(len(messages),2)
        self.assertIn('"completed": 2',messages[-1][1]['message'])


if __name__=='__main__': unittest.main()
