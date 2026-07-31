"""
Asynchronous Python Execution
-----------------------------
Runs long Python scripts in background threads with their own COM apartment,
so scripts that exceed the MCP request timeout (~30-60s) no longer die.
Submit -> poll pattern: execute_python_async returns a job_id,
get_job_result polls it.

Each worker thread calls CoInitialize and obtains its OWN SolidWorks
connection via a fresh GetObject/Dispatch (COM interface pointers cannot
be shared across apartments).
"""

import io
import os
import sys
import time
import uuid
import json
import logging
import subprocess
import threading
import traceback
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def capture_ui_problem_screenshot(ui: Dict, job_id: str,
                                  path: str = None,
                                  defer_save: bool = False) -> Dict:
    """Capture the SolidWorks frame without making a blocking COM call."""
    started = time.perf_counter()
    hwnd = int((ui or {}).get("main_window_hwnd") or 0)
    if not hwnd:
        return {"captured": False, "error": "main_window_hwnd_unavailable"}

    dpi_context = None
    set_dpi_context = None
    try:
        import ctypes
        import win32con
        import win32gui
        from PIL import Image, ImageGrab
        from .runtime import default_state_dir

        try:
            set_dpi_context = ctypes.windll.user32.SetThreadDpiAwarenessContext
            set_dpi_context.restype = ctypes.c_void_p
            dpi_context = set_dpi_context(ctypes.c_void_p(-4))
        except Exception:
            set_dpi_context = None
            dpi_context = None

        try:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.BringWindowToTop(hwnd)
            dialog_details = (ui or {}).get("window_details") or []
            if len(dialog_details) == 1 and dialog_details[0].get("hwnd"):
                win32gui.BringWindowToTop(int(dialog_details[0]["hwnd"]))
        except Exception:
            pass
        time.sleep(0.02)

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right <= left or bottom <= top:
            return {"captured": False, "error": "invalid_window_bounds"}
        target = (path or str(
            default_state_dir() /
            f"ui-watchdog-{int(time.time() * 1000)}-{job_id}.png"))
        target = str(Path(target).resolve())
        Path(target).parent.mkdir(parents=True, exist_ok=True)

        image = ImageGrab.grab(
            bbox=(left, top, right, bottom), all_screens=True)
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail((2000, 2000), Image.Resampling.BILINEAR)
        capture = {
            "captured": True,
            "path": target,
            "frame_bounds": [left, top, right, bottom],
            "full_window": True,
            "capture_method": "watchdog_win32_frame",
            "capture_elapsed_ms": round(
                (time.perf_counter() - started) * 1000.0, 3),
            "_deferred_image": image,
        }
        if defer_save:
            return capture
        return persist_ui_problem_screenshot(capture)
    except Exception as exc:
        logger.exception("Watchdog full-window screenshot failed")
        return {"captured": False, "error": str(exc)}
    finally:
        if set_dpi_context is not None and dpi_context:
            try:
                set_dpi_context(dpi_context)
            except Exception:
                pass


def persist_ui_problem_screenshot(capture: Dict) -> Dict:
    """Persist a frame grabbed before dialog recovery, then drop image state."""
    image = capture.pop("_deferred_image", None)
    if image is None:
        return capture
    started = time.perf_counter()
    try:
        target = capture["path"]
        image.save(target, "PNG", compress_level=1)
        capture["size_bytes"] = os.path.getsize(target)
        capture["persist_elapsed_ms"] = round(
            (time.perf_counter() - started) * 1000.0, 3)
        return capture
    except Exception as exc:
        logger.exception("Deferred watchdog screenshot save failed")
        return {key: value for key, value in capture.items()
                if not key.startswith("_")} | {
                    "captured": False, "error": str(exc)}


class Job:
    """State of one asynchronous script execution."""

    __slots__ = ("id", "status", "code", "stdout", "stderr", "result",
                 "error", "created_at", "started_at", "finished_at", "thread",
                 "watchdog_thread", "watchdog", "last_progress_at",
                 "last_com_started_at", "last_com_method",
                 "watchdog_com_state_path", "watchdog_expected_com_method")

    def __init__(self, job_id: str, code: str):
        self.id = job_id
        self.code = code
        self.status = "pending"   # pending | running | done | error
        self.stdout = ""
        self.stderr = ""
        self.result = None
        self.error = None
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.thread = None
        self.watchdog_thread = None
        self.watchdog = None
        self.last_progress_at = self.created_at
        self.last_com_started_at = None
        self.last_com_method = None
        self.watchdog_com_state_path = None
        self.watchdog_expected_com_method = None

    def to_dict(self, include_code: bool = False) -> Dict:
        d = {
            "job_id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "runtime_sec": (round((self.finished_at or time.time())
                                  - self.started_at, 2)
                            if self.started_at else None),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "result": self.result,
            "error": self.error,
            "watchdog": self.watchdog,
            "last_com_started_at": self.last_com_started_at,
            "last_com_method": self.last_com_method,
        }
        if include_code:
            d["code"] = self.code
        return d


class JobManager:
    """Manages background execution jobs. Thread-safe."""

    def __init__(self, max_jobs: int = 100,
                 use_isolated_watchdog: bool = True):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self._use_isolated_watchdog = bool(use_isolated_watchdog)

    def submit(self, code: str, context_factory, watchdog=None) -> Job:
        """
        Submit code for background execution.

        Args:
            code: Python source to exec()
            context_factory: Callable returning the exec globals dict.
                             Invoked INSIDE the worker thread (after
                             CoInitialize) so COM objects belong to the
                             worker's apartment.
        """
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, code)

        with self._lock:
            self._prune_locked()
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run, args=(job, context_factory), daemon=True)
        job.thread = thread
        if watchdog is not False:
            monitor_ready = threading.Event()
            policy = dict(watchdog or {})
            use_isolated = bool(policy.get(
                "process_isolated", self._use_isolated_watchdog))
            monitor_target = (self._watchdog_isolated
                              if use_isolated and os.name == "nt"
                              else self._watchdog)
            monitor = threading.Thread(
                target=monitor_target,
                args=(job, policy, monitor_ready), daemon=True)
            job.watchdog_thread = monitor
            monitor.start()
            if not monitor_ready.wait(5.0):
                event = {
                    "code": "WATCHDOG_STARTUP_TIMEOUT",
                    "stage": "async_watchdog_preflight",
                    "allowed_sec": 5.0,
                }
                job.watchdog = {
                    "state": "UI_RECOVERY_FAILED", "events": [event],
                    "last_event": event}
                job.error = json.dumps(event)
                job.status = "blocked"
                job.finished_at = time.time()
                return job
            if job.status == "pending":
                thread.start()
        else:
            thread.start()
        return job

    @staticmethod
    def _set_isolated_event(job: Job, payload: Dict):
        event = dict(payload.get("event") or payload)
        state = str(payload.get("state") or "UI_RECOVERY_FAILED")
        job.watchdog = {"state": state, "events": [event],
                        "last_event": event}
        code = event.get("code")
        if code == "UI_RECOVERED":
            job.last_progress_at = time.time()
            return
        job.error = json.dumps(event, ensure_ascii=False, default=str)
        job.status = "timeout" if state == "TIMEOUT" else "blocked"
        job.finished_at = time.time()

    def _watchdog_isolated(self, job: Job, policy, ready_event=None):
        """Coordinate a watchdog running in a separate Python process."""
        from .runtime import atomic_json_write, default_state_dir

        state_dir = default_state_dir() / "watchdog-jobs" / job.id
        state_dir.mkdir(parents=True, exist_ok=True)
        config_path = state_dir / "config.json"
        ready_path = state_dir / "ready.json"
        event_path = state_dir / "event.json"
        stop_path = state_dir / "stop"
        com_state_path = state_dir / "com-state.json"
        caused_by = dict(policy.get("caused_by") or {})
        job.watchdog_expected_com_method = caused_by.get("com_method")
        job.watchdog_com_state_path = str(com_state_path)
        config = {
            "job_id": job.id,
            "interval_sec": min(
                0.5, max(0.05, float(policy.get("interval_sec", 0.25)))),
            "max_runtime_sec": max(
                1.0, float(policy.get("max_runtime_sec", 300.0))),
            "auto_resolve_known": list(
                policy.get("auto_resolve_known", [])),
            "expected_dialog_text": policy.get("expected_dialog_text"),
            "capture_screenshot": bool(
                policy.get("capture_screenshot", True)),
            "screenshot_path": policy.get("screenshot_path"),
            "caused_by": caused_by,
            "ready_path": str(ready_path),
            "event_path": str(event_path),
            "stop_path": str(stop_path),
            "com_state_path": str(com_state_path),
        }
        atomic_json_write(config_path, config)
        command = [sys.executable, "-m",
                   "solidworks_mcp.automation.ui_watchdog_worker",
                   str(config_path)]
        process = None
        worker_log = None
        try:
            package_root = Path(__file__).resolve().parents[2]
            worker_log = open(
                state_dir / "worker.log", "ab", buffering=0)
            process = subprocess.Popen(
                command, cwd=str(package_root), stdin=subprocess.DEVNULL,
                stdout=worker_log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            startup_deadline = time.time() + 5.0
            while time.time() < startup_deadline:
                if event_path.exists():
                    payload = json.loads(event_path.read_text(encoding="utf-8"))
                    self._set_isolated_event(job, payload)
                    if ready_event is not None:
                        ready_event.set()
                    return
                if ready_path.exists():
                    if ready_event is not None:
                        ready_event.set()
                    break
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Watchdog process exited with code {process.returncode}")
                time.sleep(0.01)
            else:
                raise TimeoutError("Isolated watchdog readiness timed out")

            while job.status in ("pending", "running"):
                if event_path.exists():
                    payload = json.loads(event_path.read_text(encoding="utf-8"))
                    self._set_isolated_event(job, payload)
                    return
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Watchdog process exited with code {process.returncode}")
                time.sleep(0.02)
        except Exception as exc:
            event = {"code": "WATCHDOG_PROCESS_FAILED",
                     "stage": "async_watchdog", "error": str(exc)}
            self._set_isolated_event(
                job, {"state": "UI_RECOVERY_FAILED", "event": event})
        finally:
            if ready_event is not None:
                ready_event.set()
            try:
                stop_path.touch(exist_ok=True)
            except Exception:
                pass
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=1.0)
                except Exception:
                    pass
            if worker_log is not None:
                try:
                    worker_log.close()
                except Exception:
                    pass

    def _watchdog(self, job: Job, policy, ready_event=None):
        """Monitor UI state independently from the COM worker thread."""
        from .com_utils import detect_modal_dialog, resolve_known_dialog
        interval = min(0.5, max(0.1, float(policy.get("interval_sec", 0.25))))
        max_runtime = max(1.0, float(policy.get("max_runtime_sec", 300.0)))
        allowed = list(policy.get("auto_resolve_known", []))
        expected_text = policy.get("expected_dialog_text")
        capture_screenshot = bool(policy.get("capture_screenshot", True))
        screenshot_path = policy.get("screenshot_path")
        caused_by = dict(policy.get("caused_by") or {})
        events = []
        last_clear_check = time.time()
        empty_modal_since = None
        while job.status in ("pending", "running"):
            now = time.time()
            if job.started_at and now - job.started_at > max_runtime:
                event = {
                    "code": "BUDGET_EXCEEDED", "stage": "async_watchdog",
                    "limit": "max_runtime_sec", "allowed": max_runtime,
                    "actual": now - job.started_at,
                    "seconds_since_last_progress":
                        max(0.0, now - job.last_progress_at),
                    "caused_by": caused_by}
                events.append(event)
                job.watchdog = {
                    "state": "TIMEOUT", "events": events,
                    "last_event": event}
                job.error = json.dumps(event, ensure_ascii=False)
                job.status = "timeout"
                job.finished_at = now
                if ready_event is not None:
                    ready_event.set()
                return
            # Poll with a cheap top-level-window scan. When a blocker is
            # present, run the detailed child-control inspection exactly once
            # for classification/evidence while preserving the instant at
            # which the fast watchdog scan first detected the modal state.
            ui = detect_modal_dialog(include_controls=False)
            if ui.get("modal"):
                fast_detected_at = ui.get("detected_at") or time.time()
                if ui.get("inspection_level") == "basic":
                    detailed_ui = detect_modal_dialog(include_controls=True)
                    if detailed_ui.get("modal"):
                        ui = detailed_ui
                        ui["detected_at"] = fast_detected_at
                    else:
                        last_clear_check = time.time()
                        time.sleep(interval)
                        continue
                resolution = None
                details = ui.get("window_details", [])
                if not details:
                    empty_modal_since = empty_modal_since or time.time()
                    if time.time() - empty_modal_since < 0.5:
                        time.sleep(interval)
                        continue
                else:
                    empty_modal_since = None
                detected_at = ui.get("detected_at") or time.time()
                detection_reference = last_clear_check
                expected_com_method = caused_by.get("com_method")
                if (expected_com_method and
                        job.last_com_method == expected_com_method and
                        job.last_com_started_at is not None):
                    detection_reference = max(
                        detection_reference, job.last_com_started_at)
                detected_within_ms = int(max(
                    0.0, detected_at - detection_reference) * 1000)
                detail = details[0] if len(details) == 1 else details
                if (len(details) == 1 and
                        details[0].get("classification") ==
                        "dimension_modify" and
                        "dimension_modify" in allowed):
                    screenshot = (capture_ui_problem_screenshot(
                        ui, job.id, screenshot_path, defer_save=True)
                        if capture_screenshot else None)
                    resolution = resolve_known_dialog(
                        details[0], allowed, expected_text)
                    recovered_at = time.time()
                    if (screenshot and
                            screenshot.get("_deferred_image") is not None):
                        screenshot = persist_ui_problem_screenshot(screenshot)
                    if resolution.get("resolved"):
                        job.last_progress_at = recovered_at
                        recovered_within_ms = int(max(
                            0.0, job.last_progress_at - last_clear_check) * 1000)
                        event = {
                            "code": "UI_RECOVERED",
                            "stage": "async_watchdog",
                            "dialog": detail,
                            "caused_by": caused_by,
                            "auto_recovery_attempted": True,
                            "auto_recovery": resolution,
                            "detected_within_ms": detected_within_ms,
                            "detection_reference": {
                                "com_method": job.last_com_method,
                                "com_call_started_at":
                                    job.last_com_started_at,
                            },
                            "recovered_within_ms": recovered_within_ms,
                            "screenshot": ((screenshot or {}).get("path")),
                            "screenshot_capture": screenshot,
                        }
                        events.append(event)
                        job.watchdog = {
                            "state": "UI_RECOVERED", "events": events,
                            "last_event": event}
                        last_clear_check = job.last_progress_at
                        time.sleep(interval)
                        continue
                else:
                    screenshot = (capture_ui_problem_screenshot(
                        ui, job.id, screenshot_path)
                        if capture_screenshot else None)
                event = {
                    "code": "MODAL_DIALOG_BLOCKING",
                    "stage": "async_watchdog", "recoverable": True,
                    "dialog": detail,
                    "caused_by": caused_by,
                    "auto_recovery_attempted": resolution is not None,
                    "auto_recovery": resolution,
                    "detected_within_ms": detected_within_ms,
                    "detection_reference": {
                        "com_method": job.last_com_method,
                        "com_call_started_at": job.last_com_started_at,
                    },
                    "screenshot": ((screenshot or {}).get("path")),
                    "screenshot_capture": screenshot,
                    "seconds_since_last_progress":
                        max(0.0, time.time() - job.last_progress_at),
                }
                events.append(event)
                job.watchdog = {
                    "state": "UI_RECOVERY_FAILED",
                    "events": events, "last_event": event}
                job.error = json.dumps(event, ensure_ascii=False,
                                       default=str)
                job.status = "blocked"
                job.finished_at = time.time()
                if ready_event is not None:
                    ready_event.set()
                return
            empty_modal_since = None
            last_clear_check = time.time()
            if ready_event is not None and not ready_event.is_set():
                ready_event.set()
            time.sleep(interval)
        if ready_event is not None:
            ready_event.set()

    def _run(self, job: Job, context_factory):
        import pythoncom
        job.status = "running"
        job.started_at = time.time()
        job.last_progress_at = job.started_at

        old_stdout, old_stderr = sys.stdout, sys.stderr
        cap_out, cap_err = io.StringIO(), io.StringIO()
        com_ready = False
        try:
            pythoncom.CoInitialize()
            com_ready = True
            exec_globals = context_factory()
            original_com_get = exec_globals.get("com_get")
            if callable(original_com_get):
                def observed_com_get(obj, name, *args, **kwargs):
                    started_at = time.time()
                    job.last_com_started_at = started_at
                    job.last_com_method = str(name)
                    if (job.watchdog_com_state_path and
                            job.watchdog_expected_com_method == str(name)):
                        try:
                            from .runtime import atomic_json_write
                            atomic_json_write(job.watchdog_com_state_path, {
                                "com_method": str(name),
                                "started_at": started_at,
                            })
                        except Exception:
                            pass
                    return original_com_get(obj, name, *args, **kwargs)

                exec_globals["com_get"] = observed_com_get

            sys.stdout, sys.stderr = cap_out, cap_err
            exec(job.code, exec_globals)

            sys.stdout, sys.stderr = old_stdout, old_stderr
            job.stdout = cap_out.getvalue()
            job.stderr = cap_err.getvalue()
            rv = exec_globals.get("result")
            job.result = str(rv) if rv is not None else None
            if job.status == "running":
                job.status = "done"
        except Exception as e:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            job.stdout = cap_out.getvalue()
            job.stderr = cap_err.getvalue()
            job.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            if job.status == "running":
                job.status = "error"
            logger.error(f"Job {job.id} failed: {e}")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            if com_ready:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
            if job.finished_at is None:
                job.finished_at = time.time()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def wait(self, job_id: str, timeout: float = 0.0) -> Optional[Job]:
        """
        Get a job, optionally blocking up to `timeout` seconds for it to
        finish. timeout=0 returns immediately with the current state.
        """
        job = self.get(job_id)
        if job is None:
            return None
        if timeout > 0 and job.status in ("pending", "running"):
            job.thread.join(timeout)
        return job

    def list(self) -> list:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values()]

    def _prune_locked(self):
        """Drop oldest finished jobs when over capacity."""
        if len(self._jobs) < self._max_jobs:
            return
        finished = sorted(
            (j for j in self._jobs.values()
            if j.status in ("done", "error", "blocked", "timeout")),
            key=lambda j: j.finished_at or 0)
        for j in finished[:max(1, len(self._jobs) - self._max_jobs + 1)]:
            self._jobs.pop(j.id, None)
