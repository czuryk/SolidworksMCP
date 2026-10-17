import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp.automation import ui_watchdog_worker as worker
from solidworks_mcp.automation.runtime import atomic_json_write
from solidworks_mcp.automation.sta_watchdog import StaWatchdog, validate_policy


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'job_id':'test-request','require_active_cause':True,
            'interval_sec':.01,'max_runtime_sec':1,'capture_screenshot':False,
            'auto_resolve_known':['dimension_modify'],'expected_dialog_text':'D1@Fixture',
            'caused_by':{'com_method':'AddDimension2','request_id':'test-request'},
            'ready_path':str(self.root/'ready.json'),'event_path':str(self.root/'event.json'),
            'com_state_path':str(self.root/'com.json'),'stop_path':str(self.root/'stop')}
        self.config_path=self.root/'config.json'
        self.marker={'active':True,'request_id':'test-request','com_method':'AddDimension2','call_id':'call1'}

    def run_worker(self, *, preexisting=False, marker_change=None, before_action=None, unknown=False):
        atomic_json_write(self.config_path,self.config)
        detail={'hwnd':7,'classification':'unknown' if unknown else 'dimension_modify'}
        calls=0
        def detect(**kwargs):
            nonlocal calls
            calls+=1
            if calls==1 and not preexisting:
                atomic_json_write(self.config['com_state_path'],{**self.marker,'started_at':time.time(),**(marker_change or {})})
                return {'modal':False,'main_window_hwnd':101}
            return {'modal':True,'main_window_hwnd':101,'detected_at':time.time(),'window_details':[detail]}
        def capture(*args,**kwargs):
            if before_action:
                before_action()
            return {'captured':False}
        with patch.object(worker,'detect_modal_dialog',side_effect=detect), \
             patch.object(worker,'resolve_known_dialog',return_value={'resolved':True}) as resolve, \
             patch.object(worker,'capture_ui_problem_screenshot',side_effect=capture):
            result=worker.run(str(self.config_path))
        event=json.loads(Path(self.config['event_path']).read_text())
        return result,event,resolve

    def test_active_matching_call_can_resolve_one_known_dialog(self):
        result,event,resolve=self.run_worker()
        self.assertEqual(result,0)
        self.assertEqual(event['state'],'UI_RECOVERED')
        resolve.assert_called_once()

    def test_preexisting_dialog_never_auto_resolves(self):
        result,event,resolve=self.run_worker(preexisting=True)
        self.assertEqual(result,3)
        self.assertFalse(event['event']['causal_identity_match'])
        resolve.assert_not_called()

    def test_returned_wrong_request_or_wrong_call_cannot_authorize_action(self):
        for delta in [{'active':False},{'request_id':'other'},{'com_method':'OtherCall'}, {'started_at':0}]:
            with self.subTest(delta=delta):
                result,event,resolve=self.run_worker(marker_change=delta)
                self.assertEqual(result,3)
                resolve.assert_not_called()

    def test_unknown_dialog_is_not_auto_resolved(self):
        result,event,resolve=self.run_worker(unknown=True)
        self.assertEqual(result,3)
        resolve.assert_not_called()

    def test_scope_ending_during_capture_disarms_resolution(self):
        self.config['capture_screenshot']=True
        def returned():
            marker=json.loads(Path(self.config['com_state_path']).read_text())
            atomic_json_write(self.config['com_state_path'],{**marker,'active':False})
        result,event,resolve=self.run_worker(before_action=returned)
        self.assertEqual(result,3)
        resolve.assert_not_called()
        self.assertEqual(event['event']['auto_recovery']['reason'],'causal_scope_ended')

    def test_cancel_during_capture_disarms_resolution(self):
        self.config['capture_screenshot']=True
        result,event,resolve=self.run_worker(before_action=lambda:Path(self.config['stop_path']).touch())
        self.assertEqual(result,3)
        resolve.assert_not_called()

    def test_wrapped_call_marker_is_active_only_inside_native_call(self):
        op=SimpleNamespace(id='job',deadline=5,stop_event=threading.Event())
        guard=StaWatchdog(op,{},Mock(),self.root)
        seen=[]
        def native(*args,**kwargs):
            seen.append(json.loads(Path(guard.paths['com-state']).read_text()))
            raise RuntimeError('native failure')
        with self.assertRaises(RuntimeError):guard.wrap_com_get(native)(object(),'AddDimension2',1)
        after=json.loads(Path(guard.paths['com-state']).read_text())
        self.assertTrue(seen[0]['active'])
        self.assertFalse(after['active'])
        self.assertEqual(after['request_id'],'job')
        self.assertEqual(after['call_id'],seen[0]['call_id'])

    def test_policy_requires_scoped_identity_and_isolated_ui_only_monitor(self):
        for policy in [{'process_isolated':False},{'auto_resolve_known':['unknown']},
                       {'auto_resolve_known':['dimension_modify']},{'arbitrary':True},
                       {'interval_sec':float('nan')}]:
            with self.assertRaises(ValueError):validate_policy(policy)
        validate_policy({'auto_resolve_known':['dimension_modify'],
                         'caused_by':{'com_method':'AddDimension2'},'expected_dialog_text':'D1@Fixture'})


if __name__=='__main__':unittest.main()
