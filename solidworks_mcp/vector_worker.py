"""Isolated native worker for deterministic offline image vectorization."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _progress(stage: str, **details) -> None:
    path = os.environ.get("SOLIDWORKS_MCP_WORKER_PROGRESS")
    if not path:
        return
    record = {"stage": stage, "pid": os.getpid(), **details}
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass


_progress("module_start")
from .automation import SolidWorksAutomation
from .tool_registry import dispatch_new_tool
_progress("imports_complete")


def main(request_path: str, response_path: str) -> int:
    _progress("request_read_start")
    with open(request_path, "r", encoding="utf-8") as handle:
        request = json.load(handle)
    _progress("request_read_complete")
    name = request.get("name")
    if name not in {"image_to_sketch", "compare_sketch_to_reference",
                    "compare_body_silhouette_to_image", "compare_sketches"}:
        raise ValueError(f"Unsupported offline worker operation: {name}")
    arguments = dict(request.get("arguments") or {})
    if name == "image_to_sketch":
        commit_mode = (arguments.get("commit") or {}).get(
            "mode", "commit_if_confident")
        if commit_mode not in {"analyze_only", "preview"}:
            raise ValueError("Offline worker may not perform a SolidWorks commit")
    elif name == "compare_sketch_to_reference" and not isinstance(
            arguments.get("geometry_payload"), dict):
        raise ValueError(
            "Offline sketch comparison requires exported geometry_payload")
    elif (name == "compare_body_silhouette_to_image" and
          arguments.get("capture_screenshot") is not False):
        raise ValueError(
            "Offline body comparison requires a pre-captured screenshot")
    elif (name == "compare_sketches" and
          (not isinstance(arguments.get("reference_geometry"), dict) or
           not isinstance(arguments.get("candidate_geometry"), dict))):
        raise ValueError(
            "Offline sketch comparison requires both exported geometry payloads")
    _progress("analysis_start")
    result = dispatch_new_tool(
        SolidWorksAutomation(), name, arguments)
    _progress("analysis_complete", success=bool(result.get("success")))
    response = Path(response_path)
    response.parent.mkdir(parents=True, exist_ok=True)
    temporary = response.with_name(response.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, response)
    _progress("response_committed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: vector_worker REQUEST_JSON RESPONSE_JSON")
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
