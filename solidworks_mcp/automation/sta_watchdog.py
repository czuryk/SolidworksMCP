"""Win32-only UI monitor attached to an operation on the existing STA owner."""

import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .runtime import atomic_json_write, default_state_dir


def validate_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError('ui_guard must be an object')
    supported = {'enabled','process_isolated','interval_sec','max_runtime_sec',
                 'auto_resolve_known','expected_dialog_text','capture_screenshot',
                 'screenshot_path','caused_by','dialog_detail_wait_sec'}
    if set(policy) - supported:
        raise ValueError('Unknown ui_guard fields: '+', '.join(sorted(set(policy)-supported)))
    if policy.get('process_isolated') is False:
        raise ValueError('STA watchdog requires process_isolated=true')
    if policy.get('enabled') is False:
        if policy.get('auto_resolve_known'):
            raise ValueError('A disabled watchdog cannot request auto-resolution')
        return
    allowed = policy.get('auto_resolve_known', [])
    if not isinstance(allowed, list) or any(x != 'dimension_modify' for x in allowed):
        raise ValueError('Only dimension_modify may be auto-resolved')
    cause = policy.get('caused_by') or {}
    if not isinstance(cause, dict):
        raise ValueError('caused_by must be an object')
    if allowed and (not isinstance(cause.get('com_method'),str) or not cause['com_method'].strip() or
                    not isinstance(policy.get('expected_dialog_text'),str) or not policy['expected_dialog_text'].strip()):
        raise ValueError('Auto-resolution requires com_method and expected_dialog_text')
    for key in ['interval_sec','max_runtime_sec','dialog_detail_wait_sec']:
        if key in policy and (not math.isfinite(float(policy[key])) or float(policy[key]) <= 0):
            raise ValueError(key+' must be finite and positive')


class StaWatchdog:
    def __init__(self, operation, policy, abandon, state_dir=None):
        validate_policy(policy)
        self.operation = operation
        self.policy = dict(policy)
        self.abandon = abandon
        self.directory = Path(state_dir) if state_dir else default_state_dir() / 'sta-watchdogs' / operation.id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.paths = {name:str(self.directory/(name+'.json')) for name in ['ready','event','com-state','config']}
        self.stop_path = self.directory/'stop'
        self.process = None
        self.worker_log = None
        self.monitor = None
        self.finished = threading.Event()
        self.event = None

    def _read_event(self):
        try:
            return json.loads(Path(self.paths['event']).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None

    def start(self):
        config = {**self.policy, 'job_id':self.operation.id, 'owner_pid':os.getpid(),
                  'require_active_cause':True,
                  'interval_sec':min(.5,max(.05,float(self.policy.get('interval_sec',.1)))),
                  'max_runtime_sec':min(1800,float(self.policy.get('max_runtime_sec',self.operation.deadline))),
                  'ready_path':self.paths['ready'], 'event_path':self.paths['event'],
                  'com_state_path':self.paths['com-state'], 'stop_path':str(self.stop_path),
                  'caused_by':{**(self.policy.get('caused_by') or {}), 'request_id':self.operation.id}}
        atomic_json_write(self.paths['config'],config)
        self.worker_log = open(self.directory/'worker.log','ab',buffering=0)
        self.process = subprocess.Popen([sys.executable,'-m','solidworks_mcp.automation.ui_watchdog_worker',self.paths['config']],
            cwd=str(Path(__file__).resolve().parents[2]), stdin=subprocess.DEVNULL,
            stdout=self.worker_log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        deadline = time.monotonic()+5
        while time.monotonic()<deadline and not self.operation.stop_event.is_set():
            self.event = self._read_event()
            if self.event:
                raise RuntimeError('Watchdog preflight blocked: '+json.dumps(self.event))
            if Path(self.paths['ready']).exists():
                self.monitor = threading.Thread(target=self._monitor, name='SW-UI-monitor',daemon=True)
                self.monitor.start()
                return self
            if self.process.poll() is not None:
                raise RuntimeError('Watchdog exited before readiness')
            time.sleep(.01)
        raise RuntimeError('Watchdog readiness deadline exceeded or request cancelled')

    def _monitor(self):
        try:
            while not self.finished.wait(.02):
                if self.operation.stop_event.is_set():
                    self.stop_path.touch(exist_ok=True)
                    return
                self.event = self._read_event()
                if self.event:
                    if self.event.get('state') != 'UI_RECOVERED':
                        self.abandon(self.operation.id, 'WATCHDOG_'+str(self.event.get('state')))
                    return
                if self.process.poll() is not None:
                    self.event = {'state':'UI_RECOVERY_FAILED','event':{'code':'WATCHDOG_EXITED'}}
                    self.abandon(self.operation.id,'WATCHDOG_EXITED')
                    return
        except Exception as exc:
            self.event = {'state':'UI_RECOVERY_FAILED','event':{'code':'WATCHDOG_MONITOR_FAILED','message':str(exc)}}
            self.abandon(self.operation.id,'WATCHDOG_MONITOR_FAILED')

    def wrap_com_get(self, original):
        def tracked(obj, name, *args, **kwargs):
            if self.operation.stop_event.is_set():
                raise RuntimeError('Request scope ended; no further tracked COM calls are allowed')
            marker = {'request_id':self.operation.id, 'call_id':uuid.uuid4().hex,
                      'com_method':str(name), 'started_at':time.time(), 'active':True}
            atomic_json_write(self.paths['com-state'],marker)
            try:
                return original(obj,name,*args,**kwargs)
            finally:
                atomic_json_write(self.paths['com-state'],{**marker,'active':False,'returned_at':time.time()})
        return tracked

    def close(self):
        self.finished.set()
        self.stop_path.touch(exist_ok=True)
        if self.monitor:
            self.monitor.join(timeout=.5)
        self.event = self._read_event() or self.event
        if self.process and self.process.poll() is None:
            try:
                self.process.wait(timeout=.75)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=1)
        if self.worker_log:
            self.worker_log.close()
        return {'mode':'process_isolated_UI_only', 'request_id':self.operation.id,
                'event':self.event, 'diagnostic_directory':str(self.directory),
                'native_execution_owner':'shared_STA'}
