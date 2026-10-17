"""Isolated SolidWorks body screenshot and native tessellation worker."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

import pythoncom

from .automation import SolidWorksAutomation
from .automation.com_utils import com_get
from .automation.runtime import atomic_json_write


def _update_diagnostic(path, state, stage, **details):
    state["updated_at"] = time.time()
    state.update(details)
    if stage.endswith("_started"):
        state["active_stage"] = stage
    elif stage == "worker_failed":
        state["status"] = "failed"
        state["failed_stage"] = state.get("active_stage")
    else:
        state["last_completed_stage"] = stage
        state["active_stage"] = None
    atomic_json_write(path, state)


def run(request):
    arguments = dict(request.get("arguments") or {})
    mesh_directory = str(request["mesh_directory"])
    diagnostic_path = str(request["diagnostic_path"])
    selected_names = [str(name) for name in arguments.get("bodies") or []]
    settings = dict(request["mesh_settings"])
    state = {
        "worker_pid": os.getpid(),
        "started_at": time.time(),
        "selected_bodies": selected_names,
        "triangle_count": 0,
        "body_triangles": {},
        "visibility_restored": None,
        "last_completed_stage": None,
    }
    _update_diagnostic(diagnostic_path, state, "worker_started")
    automation = SolidWorksAutomation()
    connected = automation.connect()
    if not connected.get("success"):
        raise RuntimeError(connected.get("message", "SolidWorks connection failed"))
    doc, doc_error = automation.get_active_doc()
    if doc_error:
        return doc_error
    body_objects = {
        str(com_get(body, "Name", default="?")): body
        for body in (doc.GetBodies2(0, False) or [])}
    if not selected_names:
        selected_names = sorted(body_objects)
        state["selected_bodies"] = selected_names
    missing = [name for name in selected_names if name not in body_objects]
    if missing:
        return automation._error(
            "INVARIANT_FAILED",
            f"Bodies not found for silhouette comparison: {missing}",
            details={"existing_bodies": sorted(body_objects),
                     "diagnostic_path": diagnostic_path})
    if not selected_names:
        return automation._error(
            "INVARIANT_FAILED", "Document has no solid bodies",
            details={"diagnostic_path": diagnostic_path})
    visibility_before = {
        name: bool(com_get(body, "Visible", default=True))
        for name, body in body_objects.items()}
    shot = None
    restore_errors = []
    try:
        selected_set = set(selected_names)
        for name, body in body_objects.items():
            body.HideBody(name not in selected_set)
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        _update_diagnostic(
            diagnostic_path, state, "visibility_isolated",
            previous_visibility=visibility_before)
        shot = automation.take_screenshot(
            str(arguments["screenshot_path"]),
            orientation=str(arguments.get("orientation") or "front"),
            zoom_to_bodies=selected_names, zoom_to_fit=False,
            compress=False)
        _update_diagnostic(diagnostic_path, state, "screenshot_captured")
    finally:
        for name, body in body_objects.items():
            try:
                body.HideBody(not visibility_before[name])
            except Exception as exc:
                restore_errors.append(f"{name}: {exc}")
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        state["visibility_restored"] = not restore_errors
        _update_diagnostic(
            diagnostic_path, state, "visibility_restored",
            visibility_restore_errors=restore_errors)
    shot_data = (shot or {}).get("data") or {}
    shot_data["visibility_isolation"] = {
        "selected_bodies": selected_names,
        "previous_visibility": visibility_before,
        "restored": not restore_errors,
    }
    if (not (shot or {}).get("success") or
            shot_data.get("frame_unreadable") or restore_errors):
        return automation._error(
            "IMAGE_LOW_CONFIDENCE",
            "Body silhouette screenshot is unreadable or visibility was not restored",
            details={"screenshot": shot,
                     "visibility_restore_errors": restore_errors,
                     "diagnostic_path": diagnostic_path})

    mesh_paths = []
    for index, body_name in enumerate(selected_names):
        _update_diagnostic(
            diagnostic_path, state, "native_mesh_export_started",
            active_body=body_name)
        mesh_path = os.path.join(mesh_directory, f"body-{index:03d}.stl")
        export = automation._export_body_stl(
            doc, body_objects[body_name], mesh_path, settings)
        inspection = automation._inspect_stl(mesh_path)
        triangles = int(export.get("triangles") or
                        inspection.get("triangles") or 0)
        state["body_triangles"][body_name] = triangles
        state["triangle_count"] = sum(state["body_triangles"].values())
        mesh_paths.append(mesh_path)
        _update_diagnostic(
            diagnostic_path, state, "native_mesh_exported",
            active_body=body_name, mesh_path=mesh_path)
    _update_diagnostic(
        diagnostic_path, state, "native_capture_complete", active_body=None)
    return automation._result(
        True, "Native body capture completed", data={
            "screenshot_data": shot_data,
            "mesh_paths": mesh_paths,
            "mesh_settings": {
                **settings, "body_names": selected_names, "unit": "mm"},
            "body_names": selected_names,
            "diagnostic_path": diagnostic_path,
            "triangle_count": state["triangle_count"],
        })


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    if len(argv) != 2:
        raise SystemExit("usage: body_capture_worker REQUEST_JSON RESPONSE_JSON")
    request_path, response_path = argv
    pythoncom.CoInitialize()
    diagnostic_path = None
    try:
        with open(request_path, "r", encoding="utf-8") as handle:
            request = json.load(handle)
        diagnostic_path = str(request["diagnostic_path"])
        result = run(request)
    except Exception as exc:
        result = {
            "success": False,
            "message": f"Native body capture failed: {exc}",
            "error_code": 1,
            "error_name": "swUnknownError",
            "data": {"exception": str(exc),
                     "traceback": traceback.format_exc(),
                     "diagnostic_path": diagnostic_path},
        }
        if diagnostic_path:
            try:
                with open(diagnostic_path, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
            except Exception:
                state = {"worker_pid": os.getpid(), "triangle_count": 0}
            _update_diagnostic(
                diagnostic_path, state, "worker_failed", error=str(exc))
    finally:
        pythoncom.CoUninitialize()
    atomic_json_write(response_path, result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
