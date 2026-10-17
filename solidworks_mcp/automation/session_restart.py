"""Verified SolidWorks restart for a session whose COM owner was lost.

When the MCP process that owned a COM call dies, nothing can prove that call
has stopped except the end of the SolidWorks process itself. This operation
performs that restart with the user's documents in mind: unsaved work is
listed and protected unless the caller explicitly discards it.
"""

import ctypes
import logging
import os
import subprocess
import time
from ctypes import wintypes

from .com_utils import com_get
from .session_identity import process_created_at, solidworks_processes

logger = logging.getLogger(__name__)


def process_image_path(pid):
    """Executable path of a running process, or None."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        kernel.CloseHandle(handle)


def open_documents(automation, app):
    """Every loaded document with its save state; an untitled document counts as unsaved."""
    documents = []
    for doc in list(com_get(app, 'GetDocuments', default=None) or []):
        path = str(automation._get_doc_path(doc) or '')
        documents.append({'title': str(automation._get_doc_title(doc)), 'path': path or None,
                          'unsaved': bool(com_get(doc, 'GetSaveFlag')) or not path})
    return documents


def wait_until(condition, timeout, interval=.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


class SessionRecoveryOperations:
    def restart_solidworks(self, discard_unsaved=False, exit_timeout_sec=90, startup_timeout_sec=300):
        """Exit SolidWorks, wait for the process to end, start it again and reconnect."""
        started = time.monotonic()
        stage = 'inspect'
        try:
            before = solidworks_processes()
            executable = next((path for path in (process_image_path(p['pid']) for p in before) if path), None)
            documents, readable = [], True
            if before:
                try:
                    if not self._try_connect_com():
                        raise RuntimeError('SolidWorks does not answer COM')
                    documents = open_documents(self, self._sw_app)
                except Exception as exc:
                    readable = False
                    logger.warning('Restart preflight cannot read documents: %s', exc)
            unsaved = [d for d in documents if d['unsaved']]
            if not discard_unsaved and (unsaved or not readable):
                # Never lose work silently: the caller must see it and decide.
                return self._error(
                    'UNSAVED_DOCUMENTS' if readable else 'SOLIDWORKS_UNRESPONSIVE',
                    ('SolidWorks has unsaved documents; save or close them, or pass discard_unsaved=true'
                     if readable else
                     'SolidWorks does not answer, so unsaved work cannot be ruled out; '
                     'pass discard_unsaved=true to end the process anyway'),
                    stage=stage, recoverable=True,
                    details={'unsaved_documents': unsaved, 'open_documents': documents,
                             'solidworks_processes': before, 'restarted': False})
            stage = 'exit'
            self._runtime.record_progress('solidworks_exit')
            if before and readable:
                try:
                    if documents:
                        self._sw_app.CloseAllDocuments(True)
                    self._sw_app.ExitApp()
                except Exception as exc:
                    logger.warning('ExitApp failed: %s', exc)
            # Proxies of the ending process must not be touched again.
            self._sw_app = None
            self._connected = False
            self._shape_observation_cache = {}
            old = {(p['pid'], p['created_at']) for p in before}

            def exited():
                return not any(process_created_at(pid) == created for pid, created in old)
            forced = []
            if not wait_until(exited, exit_timeout_sec):
                if not discard_unsaved:
                    return self._error('SOLIDWORKS_EXIT_TIMEOUT',
                        'SolidWorks did not exit; it may be waiting for input. Close it, or pass discard_unsaved=true',
                        stage=stage, recoverable=True,
                        details={'solidworks_processes': solidworks_processes(), 'restarted': False})
                for pid, created in old:
                    if process_created_at(pid) == created:
                        subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True, timeout=30)
                        forced.append(pid)
                if not wait_until(exited, 30):
                    return self._error('SOLIDWORKS_EXIT_TIMEOUT', 'The SolidWorks process could not be ended',
                                       stage=stage, details={'solidworks_processes': solidworks_processes(),
                                                             'restarted': False})
            stage = 'start'
            self._runtime.record_progress('solidworks_start')
            if executable and os.path.exists(executable):
                self._sw_exe_path = executable

            # COM activation starts SolidWorks exactly as an ordinary connect does. Starting the
            # executable as well would race it and leave a second instance behind.
            def ready():
                try:
                    if not self._try_connect_com():
                        return False
                    com_get(self._sw_app, 'RevisionNumber')
                    return bool(com_get(self._sw_app, 'StartupProcessCompleted', default=True))
                except Exception:
                    self._sw_app = None
                    return False
            stage = 'connect'
            if not wait_until(ready, startup_timeout_sec, interval=2):
                return self._error('SOLIDWORKS_START_FAILED', 'SolidWorks did not become ready in time',
                                   stage=stage, details={'solidworks_processes': solidworks_processes(),
                                                         'restarted': False})
            self._runtime.record_progress('solidworks_connect')
            connected = self.connect()
            after = solidworks_processes()
            return self._result(True, 'SolidWorks restarted and reconnected', data={
                'restarted': True, 'previous_pids': [p['pid'] for p in before],
                'new_pids': [p['pid'] for p in after], 'previous_processes': before,
                'solidworks_processes': after, 'forced_termination': forced,
                'closed_documents': documents, 'discarded_unsaved': unsaved,
                'connected': bool(connected.get('success')), 'executable': executable,
                'elapsed_sec': round(time.monotonic() - started, 1),
                'next': 'reconcile_session(operation_id) for a quarantined session; reopen documents by path'})
        except Exception as exc:
            return self._error('SOLIDWORKS_RESTART_FAILED', str(exc), stage=stage,
                               details={'solidworks_processes': solidworks_processes()})
