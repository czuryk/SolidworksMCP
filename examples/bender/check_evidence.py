"""Verify distributed Bender evidence and current interpreter compatibility without CAD."""

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from solidworks_mcp import __version__
from solidworks_mcp.shape_program import run_program


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    provenance = read_json(HERE / "provenance.json")
    for item in provenance["files"]:
        path = (ROOT / item["file"]).resolve()
        require(path.is_relative_to(ROOT), "Evidence path escapes the repository")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == item["published_sha256"],
                f"Evidence hash mismatch: {item['file']}")
    for case, expected_nodes in (("appearance-24-body", 76), ("printable-9-body", 65)):
        code = (HERE / case / "source.py").read_text(encoding="utf-8")
        program = run_program(code)
        require(len(program["nodes"]) == expected_nodes, f"Unexpected component count: {case}")
        if case == "printable-9-body":
            smaller = run_program(code, {"H": 120})
            require(smaller["parameters"]["H"]["value"] == 120, "Height override was not applied")
            require(smaller["parameters"]["Clr"]["value"] == 0.25, "Clearance input changed with height")
        print(f"{case}: {expected_nodes} components compile on {__version__}")
    for view, expected_iou in (("front", 0.9157), ("side", 0.9059)):
        result = read_json(HERE / "appearance-24-body" / f"{view}-comparison.json")
        data = result["data"]
        require(result["success"] and data["source"] == "native", "Missing native comparison success")
        require(data["iou"] == expected_iou and data["levels"] == {"total": 24, "passed": 24, "failed": 0},
                f"Unexpected recorded {view} comparison")
    run = read_json(HERE / "appearance-24-body/run-evidence.json")
    bodies = run["inspection"]["data"]["bodies"]
    require(len(bodies) == 24 and all(b["kernel_health"]["passed"] for b in bodies), "Recorded native body check failed")
    manifest = read_json(HERE / "printable-9-body/delivery-manifest.json")
    require(manifest["acceptance"]["status"] == "EXPORT_VALIDATED", "Missing historical export acceptance")
    stls = [item for item in manifest["files"] if item["kind"] == "stl"]
    require(len(stls) == 9 and all(item["inspection"]["passed"] for item in stls), "Recorded STL checks failed")
    require(all(not item["inspection"]["repair_performed"] for item in stls), "Unexpected repaired STL")
    print(f"Verified {len(provenance['files'])} artifact hashes and archived native comparison/export records.")
    print("No live SOLIDWORKS operations were performed; native measurements remain historical evidence.")


if __name__ == "__main__":
    main()
