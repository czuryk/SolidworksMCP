"""Process-isolated SolidWorks UI watchdog for blocking COM calls."""

import json
import sys
import time
from pathlib import Path

from .com_utils import (detect_modal_dialog, resolve_known_dialog)
from .jobs import (capture_ui_problem_screenshot,
                   persist_ui_problem_screenshot)
from .runtime import atomic_json_write


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_event(config, state, event):
    atomic_json_write(config["event_path"], {
        "state": state, "event": event})


def run(config_path):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    interval = float(config.get("interval_sec", 0.1))
    max_runtime = float(config.get("max_runtime_sec", 300.0))
    allowed = list(config.get("auto_resolve_known") or [])
    expected_text = config.get("expected_dialog_text")
    caused_by = dict(config.get("caused_by") or {})
    detail_wait = min(2.0, max(
        0.0, float(config.get("dialog_detail_wait_sec", 0.75))))
    started_at = time.time()
    last_clear_check = started_at
    ready_written = False

    while True:
        if Path(config["stop_path"]).exists():
            return 0
        now = time.time()
        if now - started_at > max_runtime:
            event = {
                "code": "BUDGET_EXCEEDED",
                "stage": "async_watchdog_process",
                "limit": "max_runtime_sec",
                "allowed": max_runtime,
                "actual": now - started_at,
                "caused_by": caused_by,
            }
            _write_event(config, "TIMEOUT", event)
            return 2

        fast_ui = detect_modal_dialog(include_controls=False)
        if not fast_ui.get("modal"):
            last_clear_check = time.time()
            if not ready_written:
                atomic_json_write(config["ready_path"], {
                    "ready": True,
                    "checked_at": last_clear_check,
                    "scan_elapsed_ms": fast_ui.get("scan_elapsed_ms"),
                })
                ready_written = True
            time.sleep(interval)
            continue

        detected_at = float(fast_ui.get("detected_at") or time.time())
        detailed_ui = detect_modal_dialog(include_controls=True)
        if not detailed_ui.get("modal"):
            last_clear_check = time.time()
            time.sleep(interval)
            continue
        details = list(detailed_ui.get("window_details") or [])
        detail_deadline = time.monotonic() + detail_wait
        while (not details and time.monotonic() < detail_deadline and
               not Path(config["stop_path"]).exists()):
            # The main frame is disabled slightly before the owner-drawn
            # Modify window becomes enumerable. Keep the original detection
            # timestamp, but allow a bounded interval for safe classification.
            time.sleep(min(interval, 0.05))
            refreshed_ui = detect_modal_dialog(include_controls=True)
            if not refreshed_ui.get("modal"):
                detailed_ui = refreshed_ui
                break
            detailed_ui = refreshed_ui
            details = list(detailed_ui.get("window_details") or [])
        if not detailed_ui.get("modal"):
            last_clear_check = time.time()
            continue
        detail = details[0] if len(details) == 1 else details
        com_state = _read_json(config.get("com_state_path")) or {}
        expected_method = caused_by.get("com_method")
        causal_match = (not expected_method or
                        com_state.get("com_method") == expected_method)
        detected_within_ms = int(max(
            0.0, detected_at - last_clear_check) * 1000)
        causal_ms = None
        if com_state.get("started_at") is not None:
            causal_ms = int(max(
                0.0, detected_at - float(com_state["started_at"])) * 1000)

        screenshot = None
        resolution = None
        is_known = (len(details) == 1 and
                    details[0].get("classification") ==
                    "dimension_modify" and
                    "dimension_modify" in allowed and causal_match)
        if is_known:
            if config.get("capture_screenshot", True):
                screenshot = capture_ui_problem_screenshot(
                    detailed_ui, config["job_id"],
                    config.get("screenshot_path"), defer_save=True)
            resolution = resolve_known_dialog(
                details[0], allowed, expected_text)
            recovered_at = time.time()
            if (screenshot and
                    screenshot.get("_deferred_image") is not None):
                screenshot = persist_ui_problem_screenshot(screenshot)
            if resolution.get("resolved"):
                event = {
                    "code": "UI_RECOVERED",
                    "stage": "async_watchdog_process",
                    "dialog": detail,
                    "caused_by": caused_by,
                    "causal_identity_match": causal_match,
                    "auto_recovery_attempted": True,
                    "auto_recovery": resolution,
                    "detected_within_ms": detected_within_ms,
                    "causal_call_to_detection_ms": causal_ms,
                    "recovered_within_ms": int(max(
                        0.0, recovered_at - last_clear_check) * 1000),
                    "screenshot": (screenshot or {}).get("path"),
                    "screenshot_capture": screenshot,
                    "watchdog_process_isolated": True,
                }
                _write_event(config, "UI_RECOVERED", event)
                return 0

        if config.get("capture_screenshot", True) and screenshot is None:
            screenshot = capture_ui_problem_screenshot(
                detailed_ui, config["job_id"],
                config.get("screenshot_path"))
        event = {
            "code": "MODAL_DIALOG_BLOCKING",
            "stage": "async_watchdog_process",
            "recoverable": True,
            "dialog": detail,
            "caused_by": caused_by,
            "causal_identity_match": causal_match,
            "auto_recovery_attempted": resolution is not None,
            "auto_recovery": resolution,
            "detected_within_ms": detected_within_ms,
            "causal_call_to_detection_ms": causal_ms,
            "screenshot": (screenshot or {}).get("path"),
            "screenshot_capture": screenshot,
            "watchdog_process_isolated": True,
        }
        _write_event(config, "UI_RECOVERY_FAILED", event)
        return 3


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m solidworks_mcp.automation."
                         "ui_watchdog_worker <config.json>")
    raise SystemExit(run(sys.argv[1]))
