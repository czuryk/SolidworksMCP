"""
SolidworksMCP
---------------------
Main MCP server entry point with all tools.

Version: 6.5.31 (SolidWorks 2025/2026)
Live-tested on SW2026 SP2.1: transparency/colour fix confirmed; verified
constants SWBODYINTERSECT=15901/SWBODYADD=15903; CreateMeasure and the
Section View API are typed-only (FirstOffset/FirstReverseDirection,
CreateSectionView/RemoveSectionView).
Author: Samsaam Ali Baig

New in v6.5.31:
- Active 2D sketches now use a read-back-verified Normal To state machine:
  already-correct views are stable no-ops, native command 169 is verified,
  and typed Orientation3 is a bounded fallback.
- Sketch creation, activation, and geometry mutation always invoke a verified
  Fit to Screen, even when the initial frame looks acceptable. Active sketch
  geometry is projected through IModelView.Transform and checked for pixel
  occupancy, centering, and clipping before success; flat-box zoom overshoot
  is corrected through Scale2 while preserving Translation3 centering before
  whole-document fallbacks.
- Relative runtime log paths are stored under the user's local application-data
  directory instead of beside tracked source files.

New in v6.5.30:
- The process-isolated watchdog now waits a bounded 750 ms for a newly
  disabled SolidWorks frame to publish owner-drawn Modify window details.
  Non-deferred failure screenshots are now actually persisted to disk.

New in v6.5.29:
- The isolated watchdog process starts from the actual MCP package root, so it
  is importable regardless of the server launcher's working directory; child
  stdout/stderr is retained in the per-job state directory for diagnostics.

New in v6.5.28:
- Async UI monitoring runs in a separate Python process by default, removing
  GIL coupling to blocking COM Invoke calls. A file-backed causal marker binds
  recovery to the expected COM method before whitelist action is allowed.

New in v6.5.27:
- Async submission starts the watchdog first and waits for a clean UI
  preflight handshake before allowing the COM worker to run, eliminating the
  startup race in which a script could block before monitoring was active.

New in v6.5.26:
- Watchdog polling no longer reads window titles or child controls while the
  SolidWorks UI thread is blocked. It first checks the verified HWND/PID via
  non-blocking window state and owner relations, then gathers full evidence.

New in v6.5.25:
- Async jobs record the exact start and name of each script-level com_get call;
  watchdog detection latency is measured from the causative COM call when its
  identity matches ui_guard.caused_by, excluding unrelated setup work.

New in v6.5.24:
- Async watchdog polling uses a fast top-level-window pass, records the actual
  detection instant, then performs one full child-control inspection for safe
  classification and evidence without charging inspection time as detection.

New in v6.5.23:
- The modal watchdog reuses only a still-valid, process-verified SolidWorks
  main HWND/PID pair, keeping repeated checks below the 500 ms acceptance
  boundary without weakening process identity checks.

New in v6.5.22:
- Modal detection verifies the likely SOLIDWORKS frame before scanning other
  processes, and watchdog screenshots defer PNG compression until after a
  positively identified Modify dialog is accepted.

New in v6.5.21:
- Selected-body STL and silhouette meshes use the official IBody2 /
  ITessellation API with explicit tolerances, deterministic binary/ASCII
  writers, and no selection, SaveAs dialog, or global preference mutation.
- SW2026 owner-drawn Modify windows are identified through the active-sketch
  frame context and may be accepted with Enter only after exact identity match.
- Native-mesh validation errors are classified as INVALID_PLAN and runtime
  tessellation failures as INVARIANT_FAILED instead of false COM mismatches.

New in v6.5.20:
- Native body silhouettes use an idempotent per-triangle pixel union, so
  coincident front/back STL faces cannot cancel through an even-odd fill rule.
- Native SLDPRT inspection searches a bounded structural prefix and waits for
  the asynchronous SW2026 SaveCopy disk flush before rejecting the file.
- Async UI watchdogs capture full-window evidence only on a detected problem,
  preserve causal/recovery telemetry, and expose blocked/timeouts as failures.

New in v6.5.19:
- STL system-toggle writes and restoration are verified by read-back because
  ISldWorks.SetUserPreferenceToggle is a void COM method.

New in v6.5.18:
- Body/reference comparison uses a projected union of deterministic selected-
  body STL triangles as the candidate silhouette. Shaded viewport pixels are
  retained only as independent visual evidence, never as native geometry.
- SW2026 native SLDPRT validation accepts the stable record-prefix family, and
  export naming accepts either STL basenames or explicit .stl leaf names.
- Dimension reports use the documented driving/driven enum, expose verified
  Modify-dialog preference restoration, and budget stops increment telemetry.
- New documents resolve the native SOLIDWORKS default-template preference;
  semantic capsule bounds and changed model-graph idempotency are deterministic.

New in v6.5.17:
- Expected geometric failures use REFERENCE_MISMATCH instead of a false COM
  error, and body/direct-sketch comparison isolates all native image and
  scientific libraries from the SOLIDWORKS COM process.
- Atomic export recognizes the variable SW2026 native SLDPRT header, verifies
  STEP solid-body/shell counts and STL-to-CAD bboxes, applies deterministic
  scoped STL units/quality/origin settings, and restores user preferences.
- SVG metadata includes document, configuration, sketch plane, transform and
  bbox while preserving native primitive/entity identity.

New in v6.5.16:
- Sketch/reference comparison exports native CAD geometry in COM, then runs
  OpenCV/NumPy/SciPy segmentation, rasterization, metrics, and artifacts in a
  timeout-controlled isolated worker.

New in v6.5.15:
- Screenshot readability analysis is pure Pillow and cannot lazily load the
  NumPy native-DLL stack inside the connected SOLIDWORKS COM process.

New in v6.5.14:
- Screenshot verification detects background-gradient-only SaveAs3 frames
  and falls back to a DPI-correct on-screen graphics-viewport crop.
- Section screenshots verify cleanup through GetSectionViewData("") and
  fail explicitly if the active Section View remains enabled.

New in v6.5.13:
- Double-ended through-all extrudes/cuts pass Sd=False with ThroughAll on
  both API ends; scoped cuts reacquire post-topology bodies and resolve
  feature/body name collisions explicitly with rollback metadata.

New in v6.5.12:
- Sketch/reference reverse-rasterization uses an explicit OpenCV even-odd
  fill rule and never loads GEOS/Shapely in the SolidWorks COM process.

New in v6.5.11:
- Scoped cuts preserve explicitly requested host-body names across feature
  creation and feature rename, with verified restoration or rollback.

New in v6.5.10:
- Sketch/create/export bounding boxes include directed partial-arc cardinal
  extrema and exact full-circle/rotated-ellipse extents.

New in v6.5.9:
- Extrude/cut results re-read body names after feature rename, preventing
  stale `new_bodies` references from breaking transactional body renames.

New in v6.5.8:
- Robust Shapely/GEOS insert-pocket offsets run in a bounded isolated process
  so native geometry loading cannot deadlock the SOLIDWORKS COM server.

New in v6.5.7:
- Dependency-free sweep path intersection gate avoids a GEOS/Shapely
  load deadlock after SOLIDWORKS COM is active.

New in v6.5.6:
- Transaction snapshots no longer call GetPersistReference3 for every feature
  and body. Names/indices are sufficient for rollback, while mass persistent-ID
  read-back can indefinitely block on a dirty SW2026 document after an
  interrupted feature operation. Snapshot/checkpoint/sweep preflight boundaries
  are logged before each COM-risk stage.

New in v6.5.5:
- Created sweep paths are quality-checked from declared geometry before COM;
  created profiles rely on the atomic sketch commit instead of an immediate
  blocking read-back. Standard-plane path/profile contact is preflighted.
- Transactions log every step boundary. Multibody inserts enforce closed,
  contiguous profiles, through-all scoped pockets by default, actual returned
  mating names and an explicit numerical clearance tolerance.
- Native geometry sampling preserves full circles and supports ellipses,
  three-point arcs and explicit B-splines. Calibrated sketch comparison now
  reverse-rasterizes separate nested contours/holes, attributes symmetric
  deviations to real CAD entities, and emits parse-verified SVG diagnostics.
- Export bundles stage beside every target, validate native SLDPRT/STEP/STL
  structure, hash-check every replacement and restore all prior targets on any
  commit failure. Body silhouette checks require metric scale and report
  segmentation confidence plus maximum-deviation zones.

New in v6.5.4:
- Full circles read back by SW2026 as arc segments with coincident endpoints
  are now intrinsically closed in sketch topology and sweep-profile checks.

New in v6.5.3:
- Circular swept members materialize a native circle sketch and use the
  documented profile Mark=1 plus path Mark=4 contract. The SW2026 special
  circular-profile branch is rejected because it can indefinitely block both
  legacy and ISweepFeatureData feature creation. Sweep COM stages are logged.

New in v6.5.2:
- Sweep creation uses the current ISweepFeatureData definition architecture
  and IFeatureManager.CreateFeature; the blocking InsertProtrusionSwept4 path
  is no longer called.

New in v6.5.1:
- Sweep paths are selected as their real sketch curves with Mark=4. Selecting
  the sketch feature container can block InsertProtrusionSwept4 on SW2026.

New in v6.5.0:
- High-level revolve, sweep, and clearance-insert patterns are atomic through
  body naming and strict faces/bbox/volume validation. Sweep supports created
  paths plus circular, native elliptical, and custom sketch profiles, with
  exact profile/path selection marks and sampled spline curvature checks.

New in v6.4.11:
- Geometry-only reverse-raster export explicitly skips sketch and segment
  constraint-status solver calls. Public diagnostic exports keep them enabled.

New in v6.4.10:
- Construction-only reference sketches skip GetConstrainedStatus because it
  invokes the SW2026 solver without a solve target and can block on composite
  equation-NURBS geometry. Exact read-back and reverse-raster remain mandatory.

New in v6.4.9:
- Construction-only reference sketches skip SolidWorks contour enumeration,
  which is inapplicable to construction geometry and can block SW2026 on a
  large composite equation-NURBS. Reverse-raster validation remains mandatory.

New in v6.4.8:
- Construction-reference contours are transported as one composite cubic
  NURBS per loop. SolidWorks-returned segments are matched back to source IDs,
  construction flags, and endpoints, avoiding per-spline COM parameterization.

New in v6.4.7:
- Image-fit splines are materialized as bounded explicit cubic NURBS before
  COM. Post-COM verification reads only persistent IDs, entity types,
  construction flags, and endpoints, avoiding SW2026's multi-minute curve
  parameterization calls while preserving strict reverse-raster validation.

New in v6.4.6:
- Post-COM validation bulk-reads exact parameters for every sketch spline with
  ISketch.GetSplineParams3, then performs strict adaptive de Boor sampling
  locally without hundreds of cross-process ICurve.Evaluate2 calls.

New in v6.4.5:
- Post-COM spline validation uses bounded adaptive ICurve.Evaluate2 sampling
  with measured chord error and a deadline check after every curve point.

New in v6.4.4:
- Post-COM spline validation uses strict SolidWorks ICurve.GetTessPts
  tessellation instead of costly full NURBS reconstruction, enforces a
  cooperative deadline, and budgets creation plus reverse-raster sampling.

New in v6.4.3:
- Construction-reference vector commits do not add redundant endpoint
  relations, and failed atomic sketch rollback closes the transaction-owned
  active sketch before verified deletion.

New in v6.4.2:
- Per-entity spline caps and an empirical nonlinear COM cost model prevent a
  single high-control periodic spline from blocking SolidWorks for minutes.
- Auto fitting compares predicted CAD import time as well as geometric error;
  image-anchor values are now discoverable in the tool schema.

New in v6.4.1:
- construction_reference stays auxiliary and is reverse-raster validated.
- The two-phase vector commit gates costly locked traces against the remaining
  synchronous COM budget before mutating the document.

New in v6.4.0:
- image_to_sketch output modes now have enforced geometry/constraint semantics.
- Explicit four-point homography rectification preserves a source-pixel to
  sketch transform; trace_as_is is confidence-capped and never presented as
  an orthographic reconstruction.
- Region validation reports perimeter change in addition to area and distance.

New in v6.3.3:
- Mutating vectorization reverse-rasterizes exported SolidWorks geometry and
  rolls back a commit that fails the original image-space quality gates.
- Arc export uses ISketchArc.GetRotationDir; inactive-sketch relation export
  falls back to ISketchRelationManager.

New in v6.3.2:
- locked_trace deduplicates implicit shared SketchPoints and safely skips only
  SolidWorks-rejected redundant automatic Fix relations.
- Failed sketch transactions verify feature-tree rollback before reporting it.

New in v6.3.1:
- Capability discovery avoids Torch import and recursive Hugging Face scans.
- Mixed open/closed line-art graphs validate topology independently.
- locked_trace fixes sliding line/arc endpoints after leaving AddToDB mode.

New in v6.3.0:
- Verified DexiNed + TEED line-art ensemble with packaged checksummed models
- Explicit all-visible, stroke-centerline and inner/outer/both stroke-edge modes
- Graph recovery, perturbation pruning, one-entity path fitting and line raster gates

New in v6.2.5:
- Thread explicit alpha_threshold into sub-pixel topology extraction
- Report the effective topology level and reject invalid contour levels

New in v6.2.4:
- Exact NURBS export through GetBCurveParams5 out-array handling
- Closed/periodic spline topology, de Boor sampling, bbox and SVG support

New in v6.2.3:
- Normalize EditRebuild3 through the property/method COM adapter on SW2026

New in v6.2.2:
- Live-verified periodic B-spline commit through ISplineParamData on SW2026
- SolidWorks-native periodic knot/control layout with SciPy round-trip sampling

New in v6.2.1:
- Hardened isolated vector-worker launch with no stdin, unbuffered I/O,
  bytecode disabled, and durable stage diagnostics on timeout

New in v6.2.0:
- Complexity-bounded periodic B-spline optimizer for clean editable traces
- Hard fit/control-point budgets and explicit simplification strategy controls

New in v6.1.1:
- Two-phase image vectorization: killable GPU worker, then vector-only COM commit
- Hard analysis timeout before any SolidWorks mutation

New in v6.1.0:
- GPU silhouette tracing with SAM 2.1 Hiera Large and ViTMatte
- Explicit trace/component modes and auditable approximation presets
- Independent boundary, perturbation, primitive-fit, and reverse-raster gates

New in v6.0.0:
- Transaction/checkpoint/rollback, idempotency, budgets and telemetry
- Atomic parametric sketches, batch dimensions and DOF diagnostics
- Deterministic image_to_sketch with reverse-projection quality gates
- Vector geometry/SVG export and native comparison tools
- Bounded CAD plans, high-level patterns, export bundles and model DAGs
- Typed constants adapter and modal-dialog watchdog/recovery policy

New in v5.2.0 (see improvement.md P0/P1):
- set_body_transparency rewritten: VARIANT(VT_ARRAY|VT_R8) writes only
  (raw-list write corrupts appearance on SW2026), changes only index [7],
  verified read-back; new set_body_color
- Feature renames read back the ACTUAL name, auto-suffix on collision
- advanced_extrude/advanced_cut return feature_bbox + merged body names;
  guards expected_bbox / expected_merge_bodies (auto-rollback)
- sketch_contour validates closure (endpoint chain + closed contour count)
- New diagnostics tools: section_screenshot, probe_section,
  check_clearance, zoom_to, body_volume
- take_screenshot: zoom_to_bodies/zoom_bbox framing + unreadable-frame
  detector (dominant-tone histogram)

v5.0.0/v5.1.x history: typed IFeatureManager feature ops with GetFaces
auto-verification, Freeze Bar protection, ray probing, body tools,
sketch_contour, screenshots, async execution.
"""

import io
import os
import sys
import json
import copy
import logging
import subprocess
import traceback
import tempfile
import time
from typing import Dict
from pathlib import Path

# MCP imports
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Local imports
from .automation import SolidWorksAutomation
from .automation.jobs import JobManager
from .automation.com_utils import (com_get, typed, select_by_id2,
                                   select_by_ray, get_modeler,
                                   detect_modal_dialog, get_typed_module)
from .constants import SwErrors
from .config import get_config, save_config
from .utils import get_solidworks_info, set_default_unit
from .tool_registry import (NEW_TOOLS, NEW_TOOL_NAMES, MUTATING_TOOLS,
                            FIRST_GEOMETRY_TOOLS, augment_tool_schemas,
                            dispatch_new_tool)
from .automation.runtime import enrich_legacy_error

# Configure logging
config = get_config()


def _resolve_log_file(configured_path: str) -> Path:
    """Resolve relative log names outside the source checkout."""
    candidate = Path(configured_path).expanduser()
    if candidate.is_absolute():
        return candidate
    runtime_root = Path(
        os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    ) / "SolidworksMCP"
    return runtime_root / candidate


LOG_FILE = _resolve_log_file(config.log_file)

try:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _log_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
except OSError:
    LOG_FILE = Path(tempfile.gettempdir()) / "SolidworksMCP" / Path(
        config.log_file).name
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _log_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')

logging.basicConfig(
    level=config.get_log_level_int(),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[_log_handler]
)
logger = logging.getLogger("SolidworksMCP")

# ============================================================================
# Global Instances
# ============================================================================

sw_automation = SolidWorksAutomation()
job_manager = JobManager()
server = Server("SolidworksMCP")


# ============================================================================
# Tool Definitions
# ============================================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available SolidWorks tools"""
    tools = [
        # -------------------- Connection --------------------
        Tool(
            name="connect_solidworks",
            description="Connect to SolidWorks (launches it if needed). Disables "
                        "the Freeze Bar and reports any modal dialog. Most other "
                        "tools auto-connect, so calling this is optional.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="get_solidworks_info",
            description="Get SolidWorks installation information.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="get_environment_status",
            description="Diagnostics: connection, active doc, typed makepy "
                        "module availability, freeze-bar state, modal dialog.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),

        # -------------------- Documents --------------------
        Tool(
            name="create_new_part",
            description="Create a new part document.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="create_new_assembly",
            description="Create a new assembly document.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="open_document",
            description="Open an existing SolidWorks document.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to file"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="save_document",
            description="Save the active document. Save3 handled with proper "
                        "out-parameters (no Type mismatch on SW2026).",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to save (optional for Save As)"}
                },
                "required": []
            }
        ),
        Tool(
            name="close_document",
            description="Close the active document. RECOVERY: close_document("
                        "save=false) then open_document reverts the model to the "
                        "untouched on-disk copy (use after Freeze Bar / in-memory "
                        "corruption).",
            inputSchema={
                "type": "object",
                "properties": {
                    "save": {"type": "boolean", "default": False, "description": "Save before closing"}
                },
                "required": []
            }
        ),
        Tool(
            name="get_document_info",
            description="Get info about the active document (title, type, "
                        "path, feature/body counts). FIXED for SW2026.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="list_open_documents",
            description="List all open documents.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),

        # -------------------- Sketch --------------------
        Tool(
            name="create_sketch",
            description="Create a new sketch on a standard plane (Front/Top/Right) "
                        "with geometrically verified Normal To and Fit to Screen.",
            inputSchema={
                "type": "object",
                "properties": {
                    "plane": {"type": "string", "enum": ["Front", "Top", "Right"],
                              "default": "Front", "description": "Plane to sketch on"}
                },
                "required": []
            }
        ),
        Tool(
            name="create_sketch_on_face",
            description="Create a sketch on an existing body face, picked by a 3D "
                        "point that must lie ON that face (user units, mm default). "
                        "Applies verified Normal To and Fit to Screen.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "number", "default": 0},
                    "y": {"type": "number", "default": 0},
                    "z": {"type": "number", "default": 0},
                    "unit": {"type": "string", "description": "Unit (mm, inch, m)"}
                },
                "required": []
            }
        ),
        Tool(
            name="draw_line",
            description="Draw a line in the ACTIVE sketch (sketch-local 2D coords, "
                        "user units). For reference-accurate outlines in model "
                        "coordinates use sketch_contour. Normal To and pixel-"
                        "verified Fit to Screen are enforced before/after mutation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x1": {"type": "number", "default": 0},
                    "y1": {"type": "number", "default": 0},
                    "x2": {"type": "number", "default": 100},
                    "y2": {"type": "number", "default": 0},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="draw_circle",
            description="Draw a circle in the active sketch (sketch-local 2D "
                        "coords, user units) with verified Normal To / Fit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "number", "default": 0},
                    "y": {"type": "number", "default": 0},
                    "radius": {"type": "number", "default": 25},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="draw_rectangle",
            description="Draw a rectangle in the active sketch (sketch-local 2D "
                        "coords, user units) with verified Normal To / Fit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x1": {"type": "number", "default": -50},
                    "y1": {"type": "number", "default": -25},
                    "x2": {"type": "number", "default": 50},
                    "y2": {"type": "number", "default": 25},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="draw_arc",
            description="Draw an arc by center and angles in the active sketch "
                        "(sketch-local 2D coords) with verified Normal To / Fit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cx": {"type": "number", "default": 0},
                    "cy": {"type": "number", "default": 0},
                    "radius": {"type": "number", "default": 25},
                    "start_angle": {"type": "number", "default": 0},
                    "end_angle": {"type": "number", "default": 90},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="draw_polygon",
            description="Draw a regular polygon in the active sketch (sketch-local "
                        "2D coords) with verified Normal To / Fit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cx": {"type": "number", "default": 0},
                    "cy": {"type": "number", "default": 0},
                    "radius": {"type": "number", "default": 25},
                    "sides": {"type": "integer", "default": 6},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="sketch_contour",
            description="Draw a CLOSED contour from exact MODEL coordinates (user "
                        "units, mm default), transformed into the sketch CS via "
                        "ModelToSketchTransform; AddToDB avoids inference snapping. "
                        "Segment types: line, arc, centerline (revolve axis). "
                        "Provide 'plane' or 'face_ray' to auto-create the sketch, "
                        "or draw into the active one. Enforces geometrically "
                        "verified Normal To and pixel-verified Fit to Screen. "
                        "VALIDATES closure: returns closed_contours, "
                        "open_endpoints, contour_bbox; errors immediately on an "
                        "open or self-intersecting contour (wrong arc direction "
                        "+-1) instead of failing later at the extrude. Pass "
                        "close=false when intentionally drawing a partial chain.",
            inputSchema={
                "type": "object",
                "properties": {
                    "plane": {"type": "string", "enum": ["Front", "Top", "Right"],
                              "description": "Plane to create sketch on (if no active sketch)"},
                    "face_ray": {"type": "object",
                                 "description": "{'origin':[x,y,z],'direction':[dx,dy,dz]} to sketch on a face",
                                 "properties": {
                                     "origin": {"type": "array", "items": {"type": "number"}},
                                     "direction": {"type": "array", "items": {"type": "number"}}}},
                    "segments": {"type": "array",
                                 "description": "Segments in MODEL coords. line: {type,start[x,y,z],end}. arc: {type,center,start,end,direction}. centerline: {type,start,end} (construction/revolve axis)",
                                 "items": {"type": "object"}},
                    "add_to_db": {"type": "boolean", "default": True},
                    "close": {"type": "boolean", "default": True,
                              "description": "Expect a closed contour and error if it is not (false = partial chain, skip strict check)"},
                    "unit": {"type": "string"}
                },
                "required": ["segments"]
            }
        ),

        # -------------------- Basic Features --------------------
        Tool(
            name="extrude_sketch",
            description="DEPRECATED - use advanced_extrude. Legacy Boss-Extrude "
                        "(Blind/MidPlane only), NO Freeze Bar guard and NO result "
                        "verification (can silently leave a dead/frozen feature). "
                        "Depth in user units (mm default).",
            inputSchema={
                "type": "object",
                "properties": {
                    "depth": {"type": "number", "default": 10},
                    "both_directions": {"type": "boolean", "default": False},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="cut_extrude",
            description="DEPRECATED - use advanced_cut. Legacy Cut-Extrude "
                        "(Blind/ThroughAll only), NO feature scope (damages other "
                        "bodies in multibody parts), NO Freeze Bar guard, NO "
                        "verification. Depth in user units (mm default).",
            inputSchema={
                "type": "object",
                "properties": {
                    "depth": {"type": "number", "default": 10},
                    "through_all": {"type": "boolean", "default": False},
                    "both_directions": {"type": "boolean", "default": False},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="advanced_extrude",
            description="Full Boss-Extrude via typed IFeatureManager (auto-verifies "
                        "via GetFaces and deletes dead 0-face features; Freeze Bar "
                        "guard runs first). end_condition: blind, through_all, "
                        "through_all_both, through_next, up_to_vertex, "
                        "up_to_surface, mid_plane, offset_from_surface. "
                        "start_condition: sketch_plane, surface, vertex, offset. "
                        "depth = offset distance for offset_from_surface. "
                        "merge=false makes a NEW body. Reference faces are picked by "
                        "ray (ref_face_ray/start_face_ray) and must be on a VISIBLE "
                        "body. Distances in user units (mm default). If the feature "
                        "comes out dead/wrong-side, set auto_flags=true. Returns "
                        "feature_bbox (WHERE the geometry landed - CHECK IT) and "
                        "merged body names; guards: expected_bbox and "
                        "expected_merge_bodies auto-rollback on violation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sketch_name": {"type": "string", "description": "Profile sketch (last sketch if omitted)"},
                    "end_condition": {"type": "string", "default": "blind"},
                    "depth": {"type": "number", "default": 10,
                              "description": "Depth (or offset distance for offset_from_surface)"},
                    "direction_flip": {"type": "boolean", "default": False},
                    "offset_reverse": {"type": "boolean", "default": False},
                    "translate_surface": {"type": "boolean", "default": False},
                    "merge": {"type": "boolean", "default": True,
                              "description": "Merge result (False = new body)"},
                    "start_condition": {"type": "string", "default": "sketch_plane"},
                    "start_offset": {"type": "number", "default": 0},
                    "flip_start_offset": {"type": "boolean", "default": False},
                    "ref_face_ray": {"type": "object", "description": "Ray to pick end-condition reference face (Mark=1)"},
                    "start_face_ray": {"type": "object", "description": "Ray to pick start-condition reference face (Mark=32)"},
                    "feature_name": {"type": "string",
                                     "description": "Rename the created feature. ACTUAL name is returned (auto-suffix _2 on collision + warning)"},
                    "auto_verify": {"type": "boolean", "default": True},
                    "auto_flags": {"type": "boolean", "default": False,
                                   "description": "Try flag combinations (direction_flip/offset_reverse/flip_start) and pick the one that yields a live feature"},
                    "expected_bbox": {"type": "object",
                                      "description": "Guard zone {'min':[x,y,z],'max':[x,y,z],'tolerance':0.5} in user units: if the created feature's bbox falls outside, it is rolled back with an error. Combine with auto_flags to search for the combo landing in the right zone"},
                    "expected_merge_bodies": {"type": "array", "items": {"type": "string"},
                                              "description": "Bodies ALLOWED to be swallowed by the merge; if any other body merges, the feature is rolled back with an error"},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="advanced_cut",
            description="Full Cut-Extrude via typed IFeatureManager (same "
                        "conditions as advanced_extrude; auto-verifies & deletes "
                        "dead features). scope_bodies limits the cut to named "
                        "bodies (Mark=8) - ESSENTIAL in multibody parts (an "
                        "unscoped cut damages other bodies). NOTE: Cut flag "
                        "semantics differ from Boss (Cut offset-from-surface "
                        "usually needs direction_flip=True); set auto_flags=true if "
                        "unsure. Distances in user units; ref faces must be VISIBLE. "
                        "Returns feature_bbox (CHECK it - auto_flags can pick a "
                        "live-but-wrong-side cut) and merged body names; guards: "
                        "expected_bbox / expected_merge_bodies auto-rollback.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sketch_name": {"type": "string"},
                    "end_condition": {"type": "string", "default": "blind"},
                    "depth": {"type": "number", "default": 10},
                    "direction_flip": {"type": "boolean", "default": False},
                    "offset_reverse": {"type": "boolean", "default": False},
                    "translate_surface": {"type": "boolean", "default": False},
                    "start_condition": {"type": "string", "default": "sketch_plane"},
                    "start_offset": {"type": "number", "default": 0},
                    "flip_start_offset": {"type": "boolean", "default": False},
                    "ref_face_ray": {"type": "object"},
                    "start_face_ray": {"type": "object"},
                    "scope_bodies": {"type": "array", "items": {"type": "string"},
                                     "description": "Body names to limit the cut to (Mark=8)"},
                    "normal_cut": {"type": "boolean", "default": False},
                    "optimize_geometry": {"type": "boolean", "default": False},
                    "feature_name": {"type": "string",
                                     "description": "Rename the created feature. ACTUAL name is returned (auto-suffix _2 on collision + warning)"},
                    "auto_verify": {"type": "boolean", "default": True},
                    "auto_flags": {"type": "boolean", "default": False,
                                   "description": "Try flag combinations and pick the one that yields a live feature"},
                    "expected_bbox": {"type": "object",
                                      "description": "Guard zone {'min':[x,y,z],'max':[x,y,z],'tolerance':0.5} in user units: feature outside the zone is rolled back with an error. Combine with auto_flags to find the combo landing in the right zone"},
                    "expected_merge_bodies": {"type": "array", "items": {"type": "string"},
                                              "description": "Bodies ALLOWED to be merged/swallowed; any other merged body rolls the feature back with an error"},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="fillet_edges",
            description="Constant-radius fillet. Edges are picked by rays "
                        "(edge_rays, hit only VISIBLE bodies) or must be "
                        "pre-selected. Radius in user units (mm default). "
                        "Auto-verifies (GetFaces) and deletes dead features.",
            inputSchema={
                "type": "object",
                "properties": {
                    "radius": {"type": "number", "default": 2},
                    "edge_rays": {"type": "array",
                                  "description": "Rays selecting edges: [{'origin':[x,y,z],'direction':[dx,dy,dz]}]",
                                  "items": {"type": "object"}},
                    "feature_name": {"type": "string"},
                    "auto_verify": {"type": "boolean", "default": True},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="chamfer_edges",
            description="Distance-angle chamfer. Edges picked by rays (edge_rays, "
                        "hit only VISIBLE bodies) or pre-selected. distance in user "
                        "units, angle in degrees. Auto-verifies the result.",
            inputSchema={
                "type": "object",
                "properties": {
                    "distance": {"type": "number", "default": 2},
                    "angle": {"type": "number", "default": 45},
                    "edge_rays": {"type": "array",
                                  "description": "Rays selecting edges",
                                  "items": {"type": "object"}},
                    "feature_name": {"type": "string"},
                    "auto_verify": {"type": "boolean", "default": True},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="revolve_boss",
            description="Revolve a profile sketch into a solid (FeatureRevolve2). "
                        "The sketch must contain a centerline (sketch_contour type "
                        "'centerline') or provide axis_name. angle in degrees "
                        "(360=full); thin_thickness in user units. Auto-verifies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sketch_name": {"type": "string"},
                    "angle": {"type": "number", "default": 360},
                    "axis_name": {"type": "string", "description": "Axis entity name (else sketch centerline)"},
                    "reverse": {"type": "boolean", "default": False},
                    "merge": {"type": "boolean", "default": True},
                    "thin": {"type": "boolean", "default": False},
                    "thin_thickness": {"type": "number", "default": 1},
                    "feature_name": {"type": "string"},
                    "auto_verify": {"type": "boolean", "default": True},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="shell",
            description="Hollow the body to a wall thickness, removing the faces "
                        "hit by face_rays (which hit only VISIBLE bodies). No "
                        "face_rays = closed hollow. thickness in user units. "
                        "Auto-verifies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thickness": {"type": "number", "default": 2},
                    "face_rays": {"type": "array",
                                  "description": "Rays selecting faces to remove",
                                  "items": {"type": "object"}},
                    "outward": {"type": "boolean", "default": False},
                    "feature_name": {"type": "string"},
                    "auto_verify": {"type": "boolean", "default": True},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="reference_plane",
            description="Create a reference plane offset from a plane or planar "
                        "face (source by name, or source_ray to pick a face). "
                        "offset in user units (mm default).",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Source plane/face name, e.g. 'Front Plane'"},
                    "source_ray": {"type": "object", "description": "Ray to pick a planar face"},
                    "offset": {"type": "number", "default": 10},
                    "reverse": {"type": "boolean", "default": False},
                    "feature_name": {"type": "string"},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="reference_axis",
            description="Create a reference axis (InsertAxis2) from defining "
                        "entities, e.g. two planes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_names": {"type": "array",
                                     "description": "Entities 'name:type' (type PLANE/FACE/EDGE/VERTEX), e.g. ['Front Plane:PLANE','Right Plane:PLANE']",
                                     "items": {"type": "string"}},
                    "feature_name": {"type": "string"}
                },
                "required": ["entity_names"]
            }
        ),
        Tool(
            name="linear_pattern",
            description="Linear pattern of feature(s) along a direction (edge/axis "
                        "by ray or name). spacing in user units. NOTE: patterning a "
                        "lone base body (no cuts/bosses) does not materialize on "
                        "SW2026 - pattern the cut/boss feature instead. Auto-verifies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "seed_features": {"type": "array", "items": {"type": "string"}},
                    "direction_edge_ray": {"type": "object"},
                    "direction_entity": {"type": "string", "description": "Edge/axis/plane name for direction 1"},
                    "count": {"type": "integer", "default": 3},
                    "spacing": {"type": "number", "default": 20},
                    "reverse": {"type": "boolean", "default": False},
                    "count2": {"type": "integer", "default": 1},
                    "spacing2": {"type": "number", "default": 20},
                    "direction2_edge_ray": {"type": "object"},
                    "direction2_entity": {"type": "string"},
                    "reverse2": {"type": "boolean", "default": False},
                    "feature_name": {"type": "string"},
                    "auto_verify": {"type": "boolean", "default": True},
                    "unit": {"type": "string"}
                },
                "required": ["seed_features"]
            }
        ),
        Tool(
            name="circular_pattern",
            description="Circular pattern of feature(s) about an axis (axis_entity "
                        "name or axis_edge_ray). angle in degrees. Auto-verifies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "seed_features": {"type": "array", "items": {"type": "string"}},
                    "axis_entity": {"type": "string", "description": "Reference axis / circular edge name"},
                    "axis_edge_ray": {"type": "object"},
                    "count": {"type": "integer", "default": 4},
                    "angle": {"type": "number", "default": 360},
                    "equal_spacing": {"type": "boolean", "default": True},
                    "reverse": {"type": "boolean", "default": False},
                    "feature_name": {"type": "string"},
                    "auto_verify": {"type": "boolean", "default": True},
                    "unit": {"type": "string"}
                },
                "required": ["seed_features"]
            }
        ),
        Tool(
            name="mirror_feature",
            description="Mirror feature(s) or bodies about a plane/planar face "
                        "(mirror_plane name or mirror_face_ray). Provide "
                        "seed_features OR mirror_bodies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mirror_plane": {"type": "string", "description": "Mirror plane/face name, e.g. 'Right Plane'"},
                    "mirror_face_ray": {"type": "object"},
                    "seed_features": {"type": "array", "items": {"type": "string"}},
                    "mirror_bodies": {"type": "array", "items": {"type": "string"}},
                    "merge": {"type": "boolean", "default": True},
                    "feature_name": {"type": "string"},
                    "auto_verify": {"type": "boolean", "default": True},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="export_file",
            description="Export the active document by extension: STEP, STL, "
                        "IGES, Parasolid (.x_t/.x_b), 3MF, SAT, WRL, PLY, etc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Output path; extension selects format"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="list_features",
            description="List all features in the model.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="delete_feature",
            description="Delete a feature by name. Set delete_absorbed=true to also "
                        "remove the absorbed sketch (else it orphans on SW2026).",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "delete_absorbed": {"type": "boolean", "default": False,
                                        "description": "Also delete absorbed sketches/features"}
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="rename_feature",
            description="Rename a feature. Reads back the ACTUAL resulting name "
                        "(SolidWorks silently keeps the old name on collision); "
                        "on collision an auto-suffixed name (_2) is applied and "
                        "reported with a warning.",
            inputSchema={
                "type": "object",
                "properties": {
                    "old_name": {"type": "string"},
                    "new_name": {"type": "string"}
                },
                "required": ["old_name", "new_name"]
            }
        ),
        Tool(
            name="get_feature_status",
            description="Feature health: GetFaces count (0 = dead feature that "
                        "SolidWorks created silently), suppressed state, type. "
                        "Critical after feature operations.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"]
            }
        ),

        # -------------------- Bodies --------------------
        Tool(
            name="list_bodies",
            description="List solid bodies: name, visibility, bbox, face count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "include_hidden": {"type": "boolean", "default": True},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="show_body",
            description="Show a hidden body by name.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"]
            }
        ),
        Tool(
            name="hide_body",
            description="Hide a body by name.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"]
            }
        ),
        Tool(
            name="rename_body",
            description="Rename a solid body.",
            inputSchema={
                "type": "object",
                "properties": {
                    "old_name": {"type": "string"},
                    "new_name": {"type": "string"}
                },
                "required": ["old_name", "new_name"]
            }
        ),
        Tool(
            name="set_body_transparency",
            description="Set ONLY body transparency (0.0 opaque .. 1.0 invisible), "
                        "preserving colour. Write goes through a proper VARIANT "
                        "array and is verified by read-back (raw writes corrupt "
                        "appearance on SW2026 - black body). Useful for overlaying "
                        "a result over a reference body.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "transparency": {"type": "number", "default": 0.5}
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="set_body_color",
            description="Set body colour (and optionally transparency) preserving "
                        "other material properties. rgb=[r,g,b] as 0-255 (or 0-1 "
                        "if all <=1). Verified VARIANT write. Use to repair a "
                        "black body left by the old transparency bug.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "rgb": {"type": "array", "items": {"type": "number"},
                            "description": "[r,g,b] 0-255 (or 0-1)"},
                    "transparency": {"type": "number",
                                     "description": "Optional 0.0-1.0 (unchanged if omitted)"}
                },
                "required": ["name", "rgb"]
            }
        ),
        Tool(
            name="body_volume",
            description="Volume, surface area and centre of mass of one body (or "
                        "all bodies if name omitted) via GetMassProperties. "
                        "Instant sanity check that a part is not a sliver and "
                        "that a feature did not unexpectedly change a body.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Body name (all bodies if omitted)"},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="check_clearance",
            description="Clearance / interference between two bodies: minimum "
                        "distance (IMeasure) + intersection volume & bbox (temp "
                        "copies + Operations2 SWBODYINTERSECT). ESSENTIAL for "
                        "print-in-place multibody: SolidWorks allows separate "
                        "bodies to overlap SILENTLY. Set min_clearance (e.g. 0.4 "
                        "nozzle width) to make the tool fail when the gap is too "
                        "small or bodies intersect.",
            inputSchema={
                "type": "object",
                "properties": {
                    "body_a": {"type": "string"},
                    "body_b": {"type": "string"},
                    "min_clearance": {"type": "number",
                                      "description": "Required clearance in user units; below it -> success=false"},
                    "unit": {"type": "string"}
                },
                "required": ["body_a", "body_b"]
            }
        ),

        # -------------------- Ray probing --------------------
        Tool(
            name="probe_ray",
            description="Cast a ray, return the hit point (x,y,z) + body/entity "
                        "name. The workhorse for reverse-engineering reference "
                        "geometry. Coords/radius in user units (mm default). Hits "
                        "only VISIBLE bodies - show_body the target, hide occluders.",
            inputSchema={
                "type": "object",
                "properties": {
                    "origin": {"type": "array", "items": {"type": "number"},
                               "description": "[x,y,z] ray origin"},
                    "direction": {"type": "array", "items": {"type": "number"},
                                  "description": "[dx,dy,dz] ray direction"},
                    "sel_type": {"type": "string", "enum": ["face", "edge", "vertex", "body"],
                                 "default": "face"},
                    "radius": {"type": "number", "default": 0.01,
                               "description": "Ray radius / tolerance (user units)"},
                    "unit": {"type": "string"}
                },
                "required": ["origin", "direction"]
            }
        ),
        Tool(
            name="probe_rays",
            description="Batch ray probing (many rays in one call) - for efficient "
                        "reverse-engineering of edges/thicknesses/radii. Coords in "
                        "user units; hits only VISIBLE bodies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "rays": {"type": "array",
                             "description": "List of {'origin':[x,y,z],'direction':[dx,dy,dz]}",
                             "items": {"type": "object"}},
                    "sel_type": {"type": "string", "enum": ["face", "edge", "vertex", "body"],
                                 "default": "face"},
                    "radius": {"type": "number", "default": 0.01},
                    "unit": {"type": "string"}
                },
                "required": ["rays"]
            }
        ),
        Tool(
            name="select_face_by_ray",
            description="Select a face by ray with a given Mark, for reference "
                        "end/start-conditions (more reliable than SelectByID2 at "
                        "rounded coordinates). Coords in user units; hits only "
                        "VISIBLE bodies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "origin": {"type": "array", "items": {"type": "number"}},
                    "direction": {"type": "array", "items": {"type": "number"}},
                    "mark": {"type": "integer", "default": 0},
                    "append": {"type": "boolean", "default": False},
                    "radius": {"type": "number", "default": 0.01},
                    "unit": {"type": "string"}
                },
                "required": ["origin", "direction"]
            }
        ),
        Tool(
            name="probe_section",
            description="Section 'radar': cast a fan of rays inside the plane "
                        "axis=value (axis x|y|z) from origin and get the r(theta) "
                        "profile per body in ONE call - replaces dozens of manual "
                        "probe_rays for 'who is where' section analysis. theta=0 "
                        "along first in-plane axis (z->+X, x->+Y, y->+Z). Rays "
                        "hit only VISIBLE bodies (first hit per ray).",
            inputSchema={
                "type": "object",
                "properties": {
                    "axis": {"type": "string", "enum": ["x", "y", "z"],
                             "default": "z", "description": "Section plane normal"},
                    "value": {"type": "number", "default": 0,
                              "description": "Plane coordinate along axis (user units)"},
                    "origin": {"type": "array", "items": {"type": "number"},
                               "description": "Fan centre [x,y,z] (axis component overridden by value). Default [0,0,0]"},
                    "n_rays": {"type": "integer", "default": 36},
                    "angle_start": {"type": "number", "default": 0},
                    "angle_end": {"type": "number", "default": 360},
                    "sel_type": {"type": "string", "enum": ["face", "edge", "vertex", "body"],
                                 "default": "face"},
                    "radius": {"type": "number", "default": 0.01},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),

        # -------------------- View / Screenshot --------------------
        Tool(
            name="take_screenshot",
            description="Save a screenshot of the active document. Optional "
                        "camera orientation (named or custom view_direction), "
                        "zoom-to-fit or framing on bodies/bbox (zoom_to_bodies/"
                        "zoom_bbox). Detects black, solid-fill, and background-"
                        "gradient-only frames from central model-edge content; "
                        "a blank SaveAs3 export is replaced by a DPI-correct "
                        "on-screen viewport capture. If frame_unreadable=true, "
                        "do NOT use the frame as visual verification.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Output image path (.jpg/.png)"},
                    "orientation": {"type": "string",
                                    "description": "isometric, front, back, left, right, top, bottom, trimetric, dimetric"},
                    "view_direction": {"type": "array", "items": {"type": "number"},
                                       "description": "Custom look direction [dx,dy,dz]"},
                    "up_direction": {"type": "array", "items": {"type": "number"}},
                    "zoom_to_fit": {"type": "boolean", "default": True,
                                    "description": "Fit part to window before capture (default on)"},
                    "zoom_to_bodies": {"type": "array", "items": {"type": "string"},
                                       "description": "Frame the view on these bodies (overrides zoom_to_fit)"},
                    "zoom_bbox": {"type": "object",
                                  "description": "Frame on {'min':[x,y,z],'max':[x,y,z]} in user units (overrides zoom_to_fit)"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "compress": {"type": "boolean", "default": True},
                    "full_window": {"type": "boolean", "default": False,
                                    "description": "Capture the WHOLE SolidWorks window (ribbon, tree, status bar) for debugging, not just the model. Overlapping windows will appear."},
                    "unit": {"type": "string"}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="section_screenshot",
            description="Section-view screenshot in ONE call: enables Section "
                        "View on a plane (Front/Top/Right or any plane feature "
                        "name) with offset/flip, frames (zoom_to_bodies/zoom_bbox/"
                        "fit), captures, then switches the section OFF. The "
                        "cheapest 'X-ray' for inspecting internals (hinges in "
                        "pockets etc.) - transparency overlays are unreadable "
                        "for nested parts. Includes unreadable-frame detection "
                        "and verifies section cleanup with active-view data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Output image path (.jpg/.png)"},
                    "plane": {"type": "string", "default": "Front",
                              "description": "Front | Top | Right or exact plane feature name"},
                    "offset": {"type": "number", "default": 0,
                               "description": "Section plane offset (user units)"},
                    "flip": {"type": "boolean", "default": False,
                             "description": "Flip which side is cut away"},
                    "orientation": {"type": "string",
                                    "description": "Optional named view before capture"},
                    "view_direction": {"type": "array", "items": {"type": "number"}},
                    "up_direction": {"type": "array", "items": {"type": "number"}},
                    "zoom_to_bodies": {"type": "array", "items": {"type": "string"}},
                    "zoom_bbox": {"type": "object"},
                    "zoom_to_fit": {"type": "boolean", "default": True},
                    "keep_section": {"type": "boolean", "default": False,
                                     "description": "Leave the section view ON after capture"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "compress": {"type": "boolean", "default": True},
                    "unit": {"type": "string"}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="zoom_to",
            description="Frame the view on bodies / a feature / an explicit bbox "
                        "(union bbox + margin -> ViewZoomTo2). Use instead of "
                        "guessing ViewZoomTo2 coordinates (which produced "
                        "unreadable close-ups).",
            inputSchema={
                "type": "object",
                "properties": {
                    "bodies": {"type": "array", "items": {"type": "string"},
                               "description": "Body names to frame"},
                    "feature": {"type": "string", "description": "Feature name to frame"},
                    "bbox": {"type": "object",
                             "description": "{'min':[x,y,z],'max':[x,y,z]} in user units"},
                    "margin": {"type": "number", "default": 0.15,
                               "description": "Margin fraction of the largest span"},
                    "unit": {"type": "string"}
                },
                "required": []
            }
        ),
        Tool(
            name="normal_to_sketch",
            description="Reliably orient the active sketch face-on without "
                        "Ctrl+8's already-aligned side toggle. Reads the current "
                        "view, chooses the nearest side/up, tries native Normal "
                        "To, falls back to Orientation3, and verifies angular "
                        "read-back. By default always invokes Fit on the active "
                        "geometry and verifies its pixel occupancy/centering.",
            inputSchema={
                "type": "object",
                "properties": {
                    "zoom_to_fit": {"type": "boolean", "default": True},
                    "angular_tolerance_deg": {
                        "type": "number", "default": 0.1,
                        "minimum": 0.001, "maximum": 5.0},
                    "prefer_native": {"type": "boolean", "default": True}
                },
                "required": []
            }
        ),
        Tool(
            name="set_view_orientation",
            description="Set the camera orientation (named view or custom "
                        "view_direction/up_direction) and optionally zoom to fit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "orientation": {"type": "string", "default": "isometric"},
                    "view_direction": {"type": "array", "items": {"type": "number"}},
                    "up_direction": {"type": "array", "items": {"type": "number"}},
                    "zoom_to_fit": {"type": "boolean", "default": True}
                },
                "required": []
            }
        ),

        # -------------------- Sketch management --------------------
        Tool(
            name="close_sketch",
            description="Close/exit the active sketch.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="get_sketch_status",
            description="Diagnostic: active sketch state, sketch count, sketch names.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),

        # -------------------- Freeze bar --------------------
        Tool(
            name="fix_freeze_bar",
            description="Disable the Freeze Bar and move it to the top of the "
                        "tree. Freeze Bar silently freezes new API features "
                        "(0 faces, no error). Runs automatically on connect and "
                        "before feature ops; call manually if features come out dead.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),

        # -------------------- Utility --------------------
        Tool(
            name="set_units",
            description="Set default unit for dimensions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "unit": {"type": "string", "enum": ["mm", "inch", "m", "cm"]}
                },
                "required": ["unit"]
            }
        ),
        Tool(
            name="execute_python",
            description="Execute custom Python synchronously. Context: 'sw' (app), "
                        "'doc' (active doc), 'automation', and helpers "
                        "com_get/typed/select_by_id2/select_by_ray/get_modeler/"
                        "detect_modal_dialog. The SW API is in METERS - convert "
                        "user units yourself (mm*0.001 or automation._units."
                        "to_meters). Use print() for output. For scripts > ~30s "
                        "use execute_python_async.",
            inputSchema={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"]
            }
        ),
        Tool(
            name="execute_python_async",
            description="Submit Python for BACKGROUND execution (own COM "
                        "apartment + own SW connection). Returns a job_id; poll "
                        "with get_job_result. For long scripts that would exceed "
                        "the MCP request timeout.",
            inputSchema={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"]
            }
        ),
        Tool(
            name="get_job_result",
            description="Get the status/result of an async job by job_id. "
                        "Optionally block up to 'timeout' seconds.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "timeout": {"type": "number", "default": 0,
                                "description": "Seconds to block waiting (0 = return immediately)"}
                },
                "required": ["job_id"]
            }
        ),
        Tool(
            name="list_jobs",
            description="List all async execution jobs and their statuses.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
    ]
    return augment_tool_schemas(tools + NEW_TOOLS)


# ============================================================================
# Result Formatter
# ============================================================================

def format_result(r: Dict) -> str:
    """Format result dictionary as readable text"""
    status = "SUCCESS" if r["success"] else "ERROR"
    lines = [f"[{status}] {r['message']}"]

    if not r["success"]:
        lines.append(f"Error Code: {r['error_code']} ({r['error_name']})")

    if r.get("data"):
        lines.append("Details: " + json.dumps(r["data"], indent=2,
                                               ensure_ascii=False, default=str))

    return "\n".join(lines)


def _is_effectively_mutating(name: str, arguments: dict) -> bool:
    if name == "image_to_sketch":
        commit_mode = ((arguments or {}).get("commit") or {}).get(
            "mode", "commit_if_confident")
        if commit_mode in {"analyze_only", "preview"}:
            return False
    return name in MUTATING_TOOLS


def _offline_snapshot() -> Dict:
    return {
        "document": None,
        "path": None,
        "feature_count": 0,
        "solid_body_count": 0,
        "active_sketch": None,
    }


def _dispatch_offline_worker(automation, name: str, arguments: dict) -> Dict:
    if name not in {"image_to_sketch", "compare_sketch_to_reference",
                    "compare_body_silhouette_to_image", "compare_sketches"}:
        return automation._error(
            "CAPABILITY_UNAVAILABLE",
            f"Offline worker does not support '{name}'")
    budget = (arguments or {}).get("budget") or {}
    timeout = float(budget.get("max_elapsed_sec", 300))
    if timeout <= 0:
        return automation._error(
            "BUDGET_EXCEEDED", "Offline worker time budget is exhausted",
            details={"limit": "max_elapsed_sec", "allowed": timeout})
    with tempfile.TemporaryDirectory(
            prefix="solidworks-mcp-vector-worker-") as directory:
        request_path = str(Path(directory) / "request.json")
        response_path = str(Path(directory) / "response.json")
        progress_path = str(Path(directory) / "progress.jsonl")
        with open(request_path, "w", encoding="utf-8") as handle:
            json.dump({"name": name, "arguments": arguments or {}}, handle,
                      ensure_ascii=False)
        command = [sys.executable, "-B", "-u", "-m",
                   "solidworks_mcp.vector_worker",
                   request_path, response_path]
        worker_environment = os.environ.copy()
        worker_environment["SOLIDWORKS_MCP_WORKER_PROGRESS"] = progress_path

        def progress_tail():
            try:
                with open(progress_path, "r", encoding="utf-8") as handle:
                    return handle.read()[-4000:]
            except Exception:
                return ""
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout,
                cwd=str(Path(__file__).resolve().parent.parent), check=False,
                stdin=subprocess.DEVNULL, env=worker_environment)
        except subprocess.TimeoutExpired as exc:
            return automation._error(
                "BUDGET_EXCEEDED",
                f"Offline vector worker exceeded {timeout:g} seconds",
                details={"limit": "max_elapsed_sec", "allowed": timeout,
                         "stdout_tail": (exc.stdout or "")[-2000:],
                         "stderr_tail": (exc.stderr or "")[-2000:],
                         "progress_tail": progress_tail()})
        if completed.returncode != 0 or not os.path.isfile(response_path):
            return automation._error(
                "IMAGE_LOW_CONFIDENCE", "Offline image worker failed",
                details={"return_code": completed.returncode,
                         "stdout_tail": completed.stdout[-2000:],
                         "stderr_tail": completed.stderr[-2000:],
                         "progress_tail": progress_tail()})
        with open(response_path, "r", encoding="utf-8") as handle:
            result = json.load(handle)
        if not isinstance(result, dict) or "success" not in result:
            return automation._error(
                "IMAGE_LOW_CONFIDENCE",
                "Offline vector worker returned an invalid response")
        return result


def _dispatch_two_phase_sketch_comparison(automation,
                                          arguments: dict) -> Dict:
    """
    Export native sketch geometry in the COM process, then perform all image,
    NumPy/OpenCV/SciPy work in a killable subprocess with no SOLIDWORKS COM.
    """
    started = time.monotonic()
    original = copy.deepcopy(arguments or {})
    if "image_path" in original and "reference_image" not in original:
        original["reference_image"] = original.pop("image_path")
    sketch_name = str(original.get("sketch_name") or "")
    if not sketch_name:
        return automation._error(
            "INVALID_PLAN", "sketch_name is required")

    budget = dict(original.get("budget") or {})
    total_timeout = float(budget.get("max_elapsed_sec", 300))
    if total_timeout <= 0:
        return automation._error(
            "BUDGET_EXCEEDED", "Sketch comparison time budget is exhausted",
            details={"limit": "max_elapsed_sec", "allowed": total_timeout})

    logger.info(
        "Sketch comparison sketch=%s stage=com_geometry_export begin",
        sketch_name)
    export_result, geometry = automation._load_geometry_payload(
        sketch_name, "mm", include={
            "construction": True,
            "relations": False,
            "dimensions": False,
            "equations": False,
            "topology": True,
            "constraint_status": False,
        })
    if geometry is None:
        logger.info(
            "Sketch comparison sketch=%s stage=com_geometry_export failed",
            sketch_name)
        return export_result
    elapsed = time.monotonic() - started
    logger.info(
        "Sketch comparison sketch=%s stage=com_geometry_export complete "
        "entities=%s elapsed_sec=%.3f",
        sketch_name, len(geometry.get("entities") or []), elapsed)
    if elapsed >= total_timeout:
        return automation._error(
            "BUDGET_EXCEEDED",
            "Sketch geometry export exhausted the comparison budget",
            details={"limit": "max_elapsed_sec", "allowed": total_timeout,
                     "elapsed_sec": elapsed})

    worker_arguments = copy.deepcopy(original)
    worker_arguments["geometry_payload"] = geometry
    worker_arguments["budget"] = {
        **budget, "max_elapsed_sec": max(0.1, total_timeout - elapsed)}
    logger.info(
        "Sketch comparison sketch=%s stage=isolated_image_worker begin",
        sketch_name)
    result = _dispatch_offline_worker(
        automation, "compare_sketch_to_reference", worker_arguments)
    total_elapsed = time.monotonic() - started
    logger.info(
        "Sketch comparison sketch=%s stage=isolated_image_worker complete "
        "success=%s elapsed_sec=%.3f",
        sketch_name, bool(result.get("success")), total_elapsed)
    data = result.setdefault("data", {})
    data["execution_boundary"] = {
        "com_process": "native_geometry_export_only",
        "isolated_worker": "image_segmentation_rasterization_metrics",
        "native_image_libraries_in_com": False,
    }
    data["geometry_export"] = {
        "entity_count": len(geometry.get("entities") or []),
        "contour_count": len(geometry.get("contours") or []),
        "constraint_status_evaluation_skipped": True,
    }
    data["two_phase_elapsed_sec"] = round(total_elapsed, 3)
    for path in (original.get("outputs") or {}).values():
        if isinstance(path, str) and os.path.isfile(path):
            automation._runtime.increment("verification_artifacts")
    return result


def _dispatch_two_phase_body_comparison(automation,
                                        arguments: dict) -> Dict:
    """Capture UI evidence and export native body meshes before comparison."""
    started = time.monotonic()
    original = copy.deepcopy(arguments or {})
    reference_image = str(original.get("reference_image") or "")
    screenshot_path = str(original.get("screenshot_path") or "")
    if not reference_image or not screenshot_path:
        return automation._error(
            "INVALID_PLAN", "reference_image and screenshot_path are required")

    budget = dict(original.get("budget") or {})
    total_timeout = float(budget.get("max_elapsed_sec", 300))
    if total_timeout <= 0:
        return automation._error(
            "BUDGET_EXCEEDED", "Body comparison time budget is exhausted",
            details={"limit": "max_elapsed_sec", "allowed": total_timeout})

    candidate_source = str(
        original.get("candidate_source") or "native_mesh").lower()
    if candidate_source not in {"native_mesh", "screenshot_segmentation"}:
        return automation._error(
            "INVALID_PLAN",
            "candidate_source must be native_mesh or screenshot_segmentation")

    mesh_options = None
    applied_mesh_settings = None
    if candidate_source == "native_mesh":
        mesh_options = {
            "quality": "fine", "binary": True, "preserve_origin": True,
            **dict(original.get("mesh") or {}),
        }
        try:
            applied_mesh_settings = automation._resolve_stl_settings(
                mesh_options, "mm")
        except (TypeError, ValueError) as exc:
            return automation._error("INVALID_PLAN", str(exc))
        if not bool(applied_mesh_settings.get("preserve_origin", True)):
            return automation._error(
                "INVALID_PLAN",
                "Native silhouette mesh must preserve the CAD origin")

    orientation = str(original.get("orientation") or "front")
    requested_bodies = [str(name) for name in (original.get("bodies") or [])]
    mesh_directory = None
    mesh_context = None
    visibility_before = {}
    visibility_restored = True
    selected_names = list(requested_bodies)
    body_objects = {}
    doc = None
    if candidate_source == "native_mesh":
        doc, doc_error = automation.get_active_doc()
        if doc_error:
            return doc_error
        body_objects = {
            str(com_get(body, "Name", default="?")): body
            for body in (doc.GetBodies2(0, False) or [])}
        if not selected_names:
            selected_names = sorted(body_objects)
        missing = [name for name in selected_names if name not in body_objects]
        if missing:
            return automation._error(
                "INVARIANT_FAILED",
                f"Bodies not found for silhouette comparison: {missing}",
                details={"existing_bodies": sorted(body_objects)})
        if not selected_names:
            return automation._error(
                "INVARIANT_FAILED", "Document has no solid bodies")
        visibility_before = {
            name: bool(com_get(body, "Visible", default=True))
            for name, body in body_objects.items()}

    logger.info("Body comparison stage=com_screenshot begin")
    try:
        if candidate_source == "native_mesh":
            selected_set = set(selected_names)
            for name, body in body_objects.items():
                body.HideBody(name not in selected_set)
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass
        shot = automation.take_screenshot(
            screenshot_path, orientation=orientation,
            zoom_to_bodies=(selected_names if candidate_source == "native_mesh"
                            else requested_bodies or None),
            zoom_to_fit=not bool(selected_names or requested_bodies),
            compress=False)
    except Exception as exc:
        shot = automation._error(
            "IMAGE_LOW_CONFIDENCE",
            f"Body silhouette screenshot failed: {exc}")
    finally:
        if candidate_source == "native_mesh":
            restore_errors = []
            for name, body in body_objects.items():
                try:
                    body.HideBody(not visibility_before[name])
                except Exception as exc:
                    restore_errors.append(f"{name}: {exc}")
            visibility_restored = not restore_errors
            if restore_errors:
                logger.error(
                    "Body comparison visibility restore failed: %s",
                    restore_errors)
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass
    shot_data = shot.get("data") or {}
    shot_data["visibility_isolation"] = {
        "selected_bodies": selected_names,
        "previous_visibility": visibility_before,
        "restored": visibility_restored,
    }
    if (not shot.get("success") or shot_data.get("frame_unreadable") or
            not visibility_restored):
        logger.info("Body comparison stage=com_screenshot failed")
        return automation._error(
            "IMAGE_LOW_CONFIDENCE",
            "Body silhouette screenshot is unreadable or visibility was not restored",
            details={"screenshot": shot,
                     "visibility_restored": visibility_restored})
    elapsed = time.monotonic() - started
    logger.info(
        "Body comparison stage=com_screenshot complete elapsed_sec=%.3f",
        elapsed)
    if elapsed >= total_timeout:
        return automation._error(
            "BUDGET_EXCEEDED",
            "Body screenshot exhausted the comparison budget",
            details={"limit": "max_elapsed_sec", "allowed": total_timeout,
                     "elapsed_sec": elapsed})

    worker_arguments = copy.deepcopy(original)
    worker_arguments["candidate_source"] = candidate_source
    worker_arguments["capture_screenshot"] = False
    worker_arguments["screenshot_data"] = shot_data
    if candidate_source == "native_mesh":
        mesh_context = tempfile.TemporaryDirectory(
            prefix="solidworks-mcp-body-silhouette-")
        mesh_directory = mesh_context.__enter__()
        try:
            mesh_paths = []
            logger.info("Body comparison stage=com_native_mesh_export begin")
            try:
                for index, body_name in enumerate(selected_names):
                    mesh_path = os.path.join(
                        mesh_directory, f"body-{index:03d}.stl")
                    automation._export_body_stl(
                        doc, body_objects[body_name], mesh_path,
                        applied_mesh_settings)
                    automation._inspect_stl(mesh_path)
                    mesh_paths.append(mesh_path)
            except Exception as exc:
                logger.exception("Native body tessellation export failed")
                return automation._error(
                    "INVARIANT_FAILED",
                    f"Native body tessellation export failed: {exc}",
                    details={
                        "stage": "com_native_mesh_export",
                        "backend": "solidworks_itessellation",
                        "body_names": selected_names,
                    })
            elapsed = time.monotonic() - started
            logger.info(
                "Body comparison stage=com_native_mesh_export complete "
                "bodies=%s elapsed_sec=%.3f", len(mesh_paths), elapsed)
            if elapsed >= total_timeout:
                return automation._error(
                    "BUDGET_EXCEEDED",
                    "Native mesh export exhausted the comparison budget",
                    details={"limit": "max_elapsed_sec",
                             "allowed": total_timeout,
                             "elapsed_sec": elapsed})
            worker_arguments["mesh_paths"] = mesh_paths
            worker_arguments["mesh_settings"] = {
                **applied_mesh_settings,
                "body_names": selected_names,
                "unit": "mm",
            }
            worker_arguments["bodies"] = selected_names
            worker_arguments["budget"] = {
                **budget, "max_elapsed_sec": max(0.1, total_timeout - elapsed)}
            logger.info("Body comparison stage=isolated_mesh_worker begin")
            result = _dispatch_offline_worker(
                automation, "compare_body_silhouette_to_image",
                worker_arguments)
        finally:
            mesh_context.__exit__(None, None, None)
    else:
        worker_arguments["budget"] = {
            **budget, "max_elapsed_sec": max(0.1, total_timeout - elapsed)}
        logger.info("Body comparison stage=isolated_image_worker begin")
        result = _dispatch_offline_worker(
            automation, "compare_body_silhouette_to_image", worker_arguments)
    total_elapsed = time.monotonic() - started
    logger.info(
        "Body comparison stage=isolated_worker complete success=%s "
        "elapsed_sec=%.3f", bool(result.get("success")), total_elapsed)
    data = result.setdefault("data", {})
    data["execution_boundary"] = {
        "com_process": (
            "orthographic_screenshot_and_native_stl_export" if
            candidate_source == "native_mesh" else
            "orthographic_screenshot_only"),
        "isolated_worker": (
            "native_triangle_projection_union_and_reference_metrics" if
            candidate_source == "native_mesh" else
            "image_segmentation_alignment_metrics"),
        "native_image_libraries_in_com": False,
    }
    data["two_phase_elapsed_sec"] = round(total_elapsed, 3)
    for path in (original.get("outputs") or {}).values():
        if isinstance(path, str) and os.path.isfile(path):
            automation._runtime.increment("verification_artifacts")
    return result


def _dispatch_two_phase_sketches_comparison(automation,
                                            arguments: dict) -> Dict:
    """Export both sketches through COM, then compare in an isolated worker."""
    started = time.monotonic()
    original = copy.deepcopy(arguments or {})
    reference_name = str(original.get("reference_sketch") or "")
    candidate_name = str(original.get("candidate_sketch") or "")
    if not reference_name or not candidate_name:
        return automation._error(
            "INVALID_PLAN", "reference_sketch and candidate_sketch are required")
    unit = str(original.get("unit") or "mm")
    budget = dict(original.get("budget") or {})
    total_timeout = float(budget.get("max_elapsed_sec", 300))
    if total_timeout <= 0:
        return automation._error(
            "BUDGET_EXCEEDED", "Sketch comparison time budget is exhausted",
            details={"limit": "max_elapsed_sec", "allowed": total_timeout})
    include = {
        "construction": True, "relations": False, "dimensions": False,
        "equations": False, "topology": False,
        "constraint_status": False,
    }
    logger.info(
        "Direct sketch comparison stage=com_geometry_export begin "
        "reference=%s candidate=%s", reference_name, candidate_name)
    ref_result, reference = automation._load_geometry_payload(
        reference_name, unit, include=include)
    if reference is None:
        return ref_result
    if reference_name == candidate_name:
        candidate = copy.deepcopy(reference)
    else:
        cand_result, candidate = automation._load_geometry_payload(
            candidate_name, unit, include=include)
        if candidate is None:
            return cand_result
    elapsed = time.monotonic() - started
    if elapsed >= total_timeout:
        return automation._error(
            "BUDGET_EXCEEDED",
            "Sketch geometry export exhausted the comparison budget",
            details={"limit": "max_elapsed_sec", "allowed": total_timeout,
                     "elapsed_sec": elapsed})
    worker_arguments = copy.deepcopy(original)
    worker_arguments["reference_geometry"] = reference
    worker_arguments["candidate_geometry"] = candidate
    worker_arguments["budget"] = {
        **budget, "max_elapsed_sec": max(0.1, total_timeout - elapsed)}
    logger.info("Direct sketch comparison stage=isolated_geometry_worker begin")
    result = _dispatch_offline_worker(
        automation, "compare_sketches", worker_arguments)
    total_elapsed = time.monotonic() - started
    data = result.setdefault("data", {})
    data["execution_boundary"] = {
        "com_process": "native_geometry_export_only",
        "isolated_worker": "sampling_spatial_index_symmetric_metrics",
        "native_scientific_libraries_in_com": False,
    }
    data["geometry_export"] = {
        "reference_entity_count": len(reference.get("entities") or []),
        "candidate_entity_count": len(candidate.get("entities") or []),
        "same_sketch_export_reused": reference_name == candidate_name,
        "constraint_status_evaluation_skipped": True,
    }
    data["two_phase_elapsed_sec"] = round(total_elapsed, 3)
    if original.get("report_path") and os.path.isfile(original["report_path"]):
        automation._runtime.increment("verification_artifacts")
    logger.info(
        "Direct sketch comparison stage=isolated_geometry_worker complete "
        "success=%s elapsed_sec=%.3f", bool(result.get("success")),
        total_elapsed)
    return result


def _dispatch_two_phase_vector_commit(automation, arguments: dict) -> Dict:
    """Run deep analysis in a killable worker, then commit only vector JSON."""
    started = time.monotonic()
    original = copy.deepcopy(arguments or {})
    analysis_arguments = copy.deepcopy(original)
    original_commit = original.get("commit") or {}
    analysis_arguments["commit"] = {
        "mode": "analyze_only",
        "min_confidence": original_commit.get("min_confidence", 0.9),
        "rollback_on_failure": True,
    }
    analysis_arguments.pop("idempotency_key", None)
    debug = analysis_arguments.setdefault("debug", {})
    debug["save_vector_json"] = True
    debug["save_reference_raster"] = True
    budget = dict(original.get("budget") or {})
    total_timeout = float(budget.get("max_elapsed_sec", 300))
    analysis_timeout = min(
        total_timeout, float(budget.get("analysis_timeout_sec", total_timeout)))
    analysis_arguments["budget"] = {
        **budget, "max_elapsed_sec": analysis_timeout}
    analysis = _dispatch_offline_worker(
        automation, "image_to_sketch", analysis_arguments)
    if not analysis.get("success"):
        return analysis
    elapsed = time.monotonic() - started
    if elapsed >= total_timeout:
        return automation._error(
            "BUDGET_EXCEEDED",
            f"Offline vector analysis exhausted {total_timeout:g} seconds",
            details={"limit": "max_elapsed_sec", "allowed": total_timeout,
                     "elapsed_sec": elapsed, "analysis": analysis.get("data")})
    remaining_timeout = max(0.0, total_timeout - elapsed)
    result = automation.commit_vector_analysis(
        analysis_result=analysis,
        sketch_name=original.get("sketch_name", ""),
        plane=original.get("plane", "Front"),
        unit=original.get("unit", "mm"),
        commit=original_commit,
        validation=original.get("validation") or {},
        budget={**budget, "max_elapsed_sec": remaining_timeout},
        idempotency_key=original.get("idempotency_key"))
    result.setdefault("data", {})["two_phase_elapsed_sec"] = round(
        time.monotonic() - started, 3)
    return result


# ============================================================================
# Tool Handlers
# ============================================================================

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle MCP tool calls"""
    token = None
    try:
        logger.info(f"Tool: {name}, Args: {arguments}")

        mutating = _is_effectively_mutating(name, arguments)
        offline_analysis = (name == "image_to_sketch" and not mutating)
        before_snapshot = (_offline_snapshot() if offline_analysis else
                           sw_automation._document_snapshot())
        before_metrics = sw_automation._runtime.report()
        token = sw_automation._runtime.begin_tool(
            name, arguments or {}, before_snapshot)
        preflight = sw_automation.ui_preflight(
            name, arguments, mutating=mutating)
        if preflight is not None:
            sw_automation._runtime.finish_tool(token, False, before_snapshot)
            return [TextContent(type="text", text=format_result(preflight))]

        if name in FIRST_GEOMETRY_TOOLS and mutating:
            doc = sw_automation.app.ActiveDoc if sw_automation.app else None
            if doc is not None and not sw_automation._get_doc_path(doc):
                save_path = (arguments or {}).get("save_path")
                if save_path:
                    saved = sw_automation.save_document(save_path)
                    if not saved.get("success"):
                        sw_automation._runtime.finish_tool(
                            token, False, sw_automation._document_snapshot())
                        return [TextContent(type="text",
                                            text=format_result(saved))]
                elif not (arguments or {}).get("allow_unsaved_document", False):
                    blocked = sw_automation._error(
                        "DOCUMENT_UNSAVED",
                        "First sketch/body mutation requires save_path or "
                        "allow_unsaved_document=true",
                        recommended_actions=[
                            "Provide an absolute .SLDPRT save_path to preserve progress."])
                    sw_automation._runtime.finish_tool(
                        token, False, sw_automation._document_snapshot())
                    return [TextContent(type="text",
                                        text=format_result(blocked))]

        # -------------------- Connection --------------------
        if name == "connect_solidworks":
            result = sw_automation.connect()

        elif name == "get_solidworks_info":
            info = get_solidworks_info()
            result = {
                "success": info["found"],
                "message": f"SolidWorks {'found' if info['found'] else 'not found'}",
                "error_code": 0 if info["found"] else 105,
                "error_name": "swSuccess" if info["found"] else "swSolidWorksNotFound",
                "data": info
            }

        elif name == "get_environment_status":
            result = _get_environment_status()

        # -------------------- Documents --------------------
        elif name == "create_new_part":
            result = sw_automation.create_new_part()

        elif name == "create_new_assembly":
            result = sw_automation.create_new_assembly()

        elif name == "open_document":
            result = sw_automation.open_document(arguments.get("filepath", ""))

        elif name == "save_document":
            result = sw_automation.save_document(arguments.get("filepath"))

        elif name == "close_document":
            result = sw_automation.close_document(arguments.get("save", False))

        elif name == "get_document_info":
            result = sw_automation.get_document_info()

        elif name == "list_open_documents":
            result = sw_automation.list_open_documents()

        # -------------------- Sketch --------------------
        elif name == "create_sketch":
            result = sw_automation.create_sketch(arguments.get("plane", "Front"))

        elif name == "create_sketch_on_face":
            result = sw_automation.create_sketch_on_face(
                arguments.get("x", 0), arguments.get("y", 0),
                arguments.get("z", 0), arguments.get("unit"))

        elif name == "draw_line":
            result = sw_automation.draw_line(
                arguments.get("x1", 0), arguments.get("y1", 0),
                arguments.get("x2", 100), arguments.get("y2", 0),
                arguments.get("unit"))

        elif name == "draw_circle":
            result = sw_automation.draw_circle(
                arguments.get("x", 0), arguments.get("y", 0),
                arguments.get("radius", 25), arguments.get("unit"))

        elif name == "draw_rectangle":
            result = sw_automation.draw_rectangle(
                arguments.get("x1", -50), arguments.get("y1", -25),
                arguments.get("x2", 50), arguments.get("y2", 25),
                arguments.get("unit"))

        elif name == "draw_arc":
            result = sw_automation.draw_arc_center(
                arguments.get("cx", 0), arguments.get("cy", 0),
                arguments.get("radius", 25), arguments.get("start_angle", 0),
                arguments.get("end_angle", 90), arguments.get("unit"))

        elif name == "draw_polygon":
            result = sw_automation.draw_polygon(
                arguments.get("cx", 0), arguments.get("cy", 0),
                arguments.get("radius", 25), arguments.get("sides", 6),
                arguments.get("unit"))

        elif name == "sketch_contour":
            result = sw_automation.sketch_contour(
                plane=arguments.get("plane"),
                segments=arguments.get("segments"),
                face_ray=arguments.get("face_ray"),
                add_to_db=arguments.get("add_to_db", True),
                close=arguments.get("close", True),
                unit=arguments.get("unit"))

        # -------------------- Basic Features --------------------
        elif name == "extrude_sketch":
            result = sw_automation.extrude_sketch(
                arguments.get("depth", 10),
                arguments.get("both_directions", False),
                arguments.get("unit"))

        elif name == "cut_extrude":
            result = sw_automation.cut_extrude(
                arguments.get("depth", 10),
                arguments.get("through_all", False),
                arguments.get("both_directions", False),
                arguments.get("unit"))

        elif name == "advanced_extrude":
            result = sw_automation.advanced_extrude(
                sketch_name=arguments.get("sketch_name"),
                end_condition=arguments.get("end_condition", "blind"),
                depth=arguments.get("depth", 10),
                direction_flip=arguments.get("direction_flip", False),
                offset_reverse=arguments.get("offset_reverse", False),
                translate_surface=arguments.get("translate_surface", False),
                merge=arguments.get("merge", True),
                start_condition=arguments.get("start_condition", "sketch_plane"),
                start_offset=arguments.get("start_offset", 0),
                flip_start_offset=arguments.get("flip_start_offset", False),
                ref_face_ray=arguments.get("ref_face_ray"),
                start_face_ray=arguments.get("start_face_ray"),
                feature_name=arguments.get("feature_name"),
                auto_verify=arguments.get("auto_verify", True),
                auto_flags=arguments.get("auto_flags", False),
                expected_bbox=arguments.get("expected_bbox"),
                expected_merge_bodies=arguments.get("expected_merge_bodies"),
                unit=arguments.get("unit"))

        elif name == "advanced_cut":
            result = sw_automation.advanced_cut(
                sketch_name=arguments.get("sketch_name"),
                end_condition=arguments.get("end_condition", "blind"),
                depth=arguments.get("depth", 10),
                direction_flip=arguments.get("direction_flip", False),
                offset_reverse=arguments.get("offset_reverse", False),
                translate_surface=arguments.get("translate_surface", False),
                start_condition=arguments.get("start_condition", "sketch_plane"),
                start_offset=arguments.get("start_offset", 0),
                flip_start_offset=arguments.get("flip_start_offset", False),
                ref_face_ray=arguments.get("ref_face_ray"),
                start_face_ray=arguments.get("start_face_ray"),
                scope_bodies=arguments.get("scope_bodies"),
                normal_cut=arguments.get("normal_cut", False),
                optimize_geometry=arguments.get("optimize_geometry", False),
                feature_name=arguments.get("feature_name"),
                auto_verify=arguments.get("auto_verify", True),
                auto_flags=arguments.get("auto_flags", False),
                expected_bbox=arguments.get("expected_bbox"),
                expected_merge_bodies=arguments.get("expected_merge_bodies"),
                unit=arguments.get("unit"))

        elif name == "fillet_edges":
            result = sw_automation.fillet_edges(
                radius=arguments.get("radius", 2),
                edge_rays=arguments.get("edge_rays"),
                feature_name=arguments.get("feature_name"),
                auto_verify=arguments.get("auto_verify", True),
                unit=arguments.get("unit"))

        elif name == "chamfer_edges":
            result = sw_automation.chamfer_edges(
                distance=arguments.get("distance", 2),
                angle=arguments.get("angle", 45),
                edge_rays=arguments.get("edge_rays"),
                feature_name=arguments.get("feature_name"),
                auto_verify=arguments.get("auto_verify", True),
                unit=arguments.get("unit"))

        elif name == "revolve_boss":
            result = sw_automation.revolve_boss(
                sketch_name=arguments.get("sketch_name"),
                angle=arguments.get("angle", 360),
                axis_name=arguments.get("axis_name"),
                reverse=arguments.get("reverse", False),
                merge=arguments.get("merge", True),
                thin=arguments.get("thin", False),
                thin_thickness=arguments.get("thin_thickness", 1),
                feature_name=arguments.get("feature_name"),
                auto_verify=arguments.get("auto_verify", True),
                unit=arguments.get("unit"))

        elif name == "shell":
            result = sw_automation.shell(
                thickness=arguments.get("thickness", 2),
                face_rays=arguments.get("face_rays"),
                outward=arguments.get("outward", False),
                feature_name=arguments.get("feature_name"),
                auto_verify=arguments.get("auto_verify", True),
                unit=arguments.get("unit"))

        elif name == "reference_plane":
            result = sw_automation.reference_plane(
                source=arguments.get("source"),
                source_ray=arguments.get("source_ray"),
                offset=arguments.get("offset", 10),
                reverse=arguments.get("reverse", False),
                feature_name=arguments.get("feature_name"),
                unit=arguments.get("unit"))

        elif name == "reference_axis":
            result = sw_automation.reference_axis(
                entity_names=arguments.get("entity_names", []),
                feature_name=arguments.get("feature_name"))

        elif name == "linear_pattern":
            result = sw_automation.linear_pattern(
                seed_features=arguments.get("seed_features", []),
                direction_edge_ray=arguments.get("direction_edge_ray"),
                direction_entity=arguments.get("direction_entity"),
                count=arguments.get("count", 3),
                spacing=arguments.get("spacing", 20),
                reverse=arguments.get("reverse", False),
                count2=arguments.get("count2", 1),
                spacing2=arguments.get("spacing2", 20),
                direction2_edge_ray=arguments.get("direction2_edge_ray"),
                direction2_entity=arguments.get("direction2_entity"),
                reverse2=arguments.get("reverse2", False),
                feature_name=arguments.get("feature_name"),
                auto_verify=arguments.get("auto_verify", True),
                unit=arguments.get("unit"))

        elif name == "circular_pattern":
            result = sw_automation.circular_pattern(
                seed_features=arguments.get("seed_features", []),
                axis_entity=arguments.get("axis_entity"),
                axis_edge_ray=arguments.get("axis_edge_ray"),
                count=arguments.get("count", 4),
                angle=arguments.get("angle", 360),
                equal_spacing=arguments.get("equal_spacing", True),
                reverse=arguments.get("reverse", False),
                feature_name=arguments.get("feature_name"),
                auto_verify=arguments.get("auto_verify", True),
                unit=arguments.get("unit"))

        elif name == "mirror_feature":
            result = sw_automation.mirror_feature(
                mirror_plane=arguments.get("mirror_plane"),
                mirror_face_ray=arguments.get("mirror_face_ray"),
                seed_features=arguments.get("seed_features"),
                mirror_bodies=arguments.get("mirror_bodies"),
                merge=arguments.get("merge", True),
                feature_name=arguments.get("feature_name"),
                auto_verify=arguments.get("auto_verify", True),
                unit=arguments.get("unit"))

        elif name == "export_file":
            result = sw_automation.export_file(arguments.get("filepath", ""))

        elif name == "list_features":
            result = _list_features_fixed()

        elif name == "delete_feature":
            result = sw_automation.delete_feature(
                arguments.get("name", ""),
                arguments.get("delete_absorbed", False))

        elif name == "rename_feature":
            result = sw_automation.rename_feature(
                arguments.get("old_name", ""), arguments.get("new_name", ""))

        elif name == "get_feature_status":
            result = sw_automation.get_feature_status(arguments.get("name", ""))

        # -------------------- Bodies --------------------
        elif name == "list_bodies":
            result = sw_automation.list_bodies(
                arguments.get("include_hidden", True), arguments.get("unit"))

        elif name == "show_body":
            result = sw_automation.set_body_visibility(
                arguments.get("name", ""), True)

        elif name == "hide_body":
            result = sw_automation.set_body_visibility(
                arguments.get("name", ""), False)

        elif name == "rename_body":
            result = sw_automation.rename_body(
                arguments.get("old_name", ""), arguments.get("new_name", ""))

        elif name == "set_body_transparency":
            result = sw_automation.set_body_transparency(
                arguments.get("name", ""), arguments.get("transparency", 0.5))

        elif name == "set_body_color":
            result = sw_automation.set_body_color(
                arguments.get("name", ""), arguments.get("rgb"),
                arguments.get("transparency"))

        elif name == "body_volume":
            result = sw_automation.body_volume(
                arguments.get("name"), arguments.get("unit"))

        elif name == "check_clearance":
            result = sw_automation.check_clearance(
                arguments.get("body_a", ""), arguments.get("body_b", ""),
                arguments.get("min_clearance"), arguments.get("unit"))

        # -------------------- Ray probing --------------------
        elif name == "probe_ray":
            result = sw_automation.probe_ray(
                arguments.get("origin"), arguments.get("direction"),
                arguments.get("sel_type", "face"),
                arguments.get("radius", 0.01), arguments.get("unit"))

        elif name == "probe_rays":
            result = sw_automation.probe_rays(
                arguments.get("rays", []), arguments.get("sel_type", "face"),
                arguments.get("radius", 0.01), arguments.get("unit"))

        elif name == "select_face_by_ray":
            result = sw_automation.select_face_by_ray(
                arguments.get("origin"), arguments.get("direction"),
                arguments.get("mark", 0), arguments.get("append", False),
                arguments.get("radius", 0.01), arguments.get("unit"))

        elif name == "probe_section":
            result = sw_automation.probe_section(
                axis=arguments.get("axis", "z"),
                value=arguments.get("value", 0),
                origin=arguments.get("origin"),
                n_rays=arguments.get("n_rays", 36),
                angle_start=arguments.get("angle_start", 0),
                angle_end=arguments.get("angle_end", 360),
                sel_type=arguments.get("sel_type", "face"),
                radius=arguments.get("radius", 0.01),
                unit=arguments.get("unit"))

        # -------------------- View / Screenshot --------------------
        elif name == "take_screenshot":
            result = sw_automation.take_screenshot(
                path=arguments.get("path", ""),
                orientation=arguments.get("orientation"),
                view_direction=arguments.get("view_direction"),
                up_direction=arguments.get("up_direction"),
                zoom_to_fit=arguments.get("zoom_to_fit", True),
                zoom_to_bodies=arguments.get("zoom_to_bodies"),
                zoom_bbox=arguments.get("zoom_bbox"),
                width=arguments.get("width"), height=arguments.get("height"),
                compress=arguments.get("compress", True),
                full_window=arguments.get("full_window", False),
                unit=arguments.get("unit"))

        elif name == "section_screenshot":
            result = sw_automation.section_screenshot(
                path=arguments.get("path", ""),
                plane=arguments.get("plane", "Front"),
                offset=arguments.get("offset", 0),
                flip=arguments.get("flip", False),
                orientation=arguments.get("orientation"),
                view_direction=arguments.get("view_direction"),
                up_direction=arguments.get("up_direction"),
                zoom_to_bodies=arguments.get("zoom_to_bodies"),
                zoom_bbox=arguments.get("zoom_bbox"),
                zoom_to_fit=arguments.get("zoom_to_fit", True),
                keep_section=arguments.get("keep_section", False),
                width=arguments.get("width"), height=arguments.get("height"),
                compress=arguments.get("compress", True),
                unit=arguments.get("unit"))

        elif name == "zoom_to":
            result = sw_automation.zoom_to(
                bodies=arguments.get("bodies"),
                feature=arguments.get("feature"),
                bbox=arguments.get("bbox"),
                margin=arguments.get("margin", 0.15),
                unit=arguments.get("unit"))

        elif name == "normal_to_sketch":
            result = sw_automation.orient_normal_to_active_sketch(
                zoom_to_fit=arguments.get("zoom_to_fit", True),
                angular_tolerance_deg=arguments.get(
                    "angular_tolerance_deg"),
                prefer_native=arguments.get("prefer_native", True))

        elif name == "set_view_orientation":
            result = sw_automation.set_view_orientation(
                orientation=arguments.get("orientation", "isometric"),
                view_direction=arguments.get("view_direction"),
                up_direction=arguments.get("up_direction"),
                zoom_to_fit=arguments.get("zoom_to_fit", True))

        # -------------------- Sketch management --------------------
        elif name == "close_sketch":
            result = _close_sketch_handler()

        elif name == "get_sketch_status":
            result = _get_sketch_status_handler()

        # -------------------- Freeze bar --------------------
        elif name == "fix_freeze_bar":
            result = _fix_freeze_bar_handler()

        # -------------------- Utility --------------------
        elif name == "set_units":
            unit = arguments.get("unit", "mm")
            set_default_unit(unit)
            sw_automation._units.default_unit = unit
            result = {
                "success": True,
                "message": f"Default unit set to: {unit}",
                "error_code": 0, "error_name": "swSuccess",
                "data": {"unit": unit}
            }

        elif name == "execute_python":
            code = arguments.get("code", "")
            if not code:
                result = sw_automation._result(False, "Code is required",
                                               SwErrors.swInvalidInput)
            else:
                result = _execute_python_fixed(code)

        elif name == "execute_python_async":
            code = arguments.get("code", "")
            if not code:
                result = sw_automation._result(False, "Code is required",
                                               SwErrors.swInvalidInput)
            else:
                result = _execute_python_async(
                    code, arguments.get("ui_guard"), arguments.get("budget"))

        elif name == "get_job_result":
            result = _get_job_result(arguments.get("job_id", ""),
                                     arguments.get("timeout", 0))

        elif name == "list_jobs":
            result = {
                "success": True, "message": f"{len(job_manager.list())} job(s)",
                "error_code": 0, "error_name": "swSuccess",
                "data": {"jobs": job_manager.list()}
            }

        elif name in NEW_TOOL_NAMES:
            if offline_analysis:
                result = _dispatch_offline_worker(
                    sw_automation, name, arguments)
            elif name == "image_to_sketch":
                result = _dispatch_two_phase_vector_commit(
                    sw_automation, arguments)
            elif name in {"compare_sketch_to_reference",
                           "compare_sketch_to_image"}:
                result = _dispatch_two_phase_sketch_comparison(
                    sw_automation, arguments)
            elif name == "compare_body_silhouette_to_image":
                result = _dispatch_two_phase_body_comparison(
                    sw_automation, arguments)
            elif name == "compare_sketches":
                result = _dispatch_two_phase_sketches_comparison(
                    sw_automation, arguments)
            else:
                result = dispatch_new_tool(sw_automation, name, arguments)

        else:
            result = sw_automation._result(False, f"Unknown tool: {name}",
                                           SwErrors.swUnknownError)

        result = enrich_legacy_error(result)
        recovery_policy = (arguments or {}).get("recovery") or {}
        if (not result.get("success") and recovery_policy.get("auto_recover")
                and name in sw_automation.PLAN_OPERATIONS):
            recovered = sw_automation.recover_environment(
                {"op": name, "args": arguments}, max_retries=1)
            retry = (recovered.get("data") or {}).get("retry")
            if retry is not None:
                retry.setdefault("data", {})["automatic_recovery"] = {
                    "attempted": True, "attempts": 1,
                    "initial_error": result.get("data", {}).get("error"),
                    "recovery": recovered.get("data")}
                result = enrich_legacy_error(retry)
        if result.get("success") and name in {
                "save_document", "export_file"}:
            sw_automation._runtime.increment("files_saved")
        if result.get("success") and name in {"save_document", "export_bundle"}:
            snap = sw_automation._document_snapshot()
            if snap.get("solid_body_count", 0) > 0:
                import time as _time
                sw_automation._runtime.last_saved_body_at = _time.time()
        result = sw_automation.ui_postflight(
            name, result, mutating=mutating)
        after_snapshot = (_offline_snapshot() if offline_analysis else
                          sw_automation._document_snapshot())
        elapsed = sw_automation._runtime.finish_tool(
            token, bool(result.get("success")), after_snapshot)
        token = None
        after_metrics = sw_automation._runtime.report()
        budget = (arguments or {}).get("budget") or {}
        violation = sw_automation._runtime.budget_violation(
            budget,
            rebuilds=after_metrics["rebuilds"] - before_metrics["rebuilds"],
            solver_time=(after_metrics["solver_time_sec"] -
                         before_metrics["solver_time_sec"]),
            rollbacks=after_metrics["rollbacks"] - before_metrics["rollbacks"])
        if violation:
            if violation.get("warn_only"):
                result.setdefault("data", {}).setdefault(
                    "warnings", []).append({"code": "BUDGET_EXCEEDED",
                                             **violation})
            else:
                result = sw_automation._error(
                    "BUDGET_EXCEEDED",
                    f"Budget limit exceeded: {violation['limit']}",
                    details={"budget": violation,
                             "operation_result": result})
        result.setdefault("data", {})["server_elapsed_ms"] = round(
            elapsed * 1000, 3)
        logger.info(f"Result: success={result['success']}")
        return [TextContent(type="text", text=format_result(result))]

    except Exception as e:
        logger.error(f"Tool error: {e}\n{traceback.format_exc()}")
        if token is not None:
            sw_automation._runtime.finish_tool(
                token, False, sw_automation._document_snapshot())
        result = sw_automation._error(
            "COM_MEMBER_MISMATCH", f"Unhandled tool error: {e}",
            com_hresult=getattr(e, "hresult", None))
        return [TextContent(type="text", text=format_result(result))]


# ============================================================================
# Execution context (shared by sync + async)
# ============================================================================

def _build_exec_context(sw_app, doc):
    """Build the exec() globals with SW objects, libraries and helpers."""
    import win32com.client
    import pythoncom
    import math
    import os as os_module

    return {
        # SolidWorks objects
        'sw': sw_app,
        'doc': doc,
        'automation': sw_automation,

        # COM libraries
        'win32com': win32com.client,
        'pythoncom': pythoncom,

        # Standard libraries
        'math': math,
        'os': os_module,
        'json': json,

        # COM helpers (hide SW2026 quirks)
        'com_get': com_get,
        'typed': typed,
        'select_by_id2': select_by_id2,
        'select_by_ray': select_by_ray,
        'get_modeler': get_modeler,
        'get_typed_module': get_typed_module,
        'detect_modal_dialog': detect_modal_dialog,

        # Result placeholder
        'result': None,
    }


def _execute_python_fixed(code: str) -> Dict:
    """Execute custom Python synchronously with stdout capture."""
    try:
        if not sw_automation.is_connected:
            r = sw_automation.connect()
            if not r["success"]:
                return r

        exec_globals = _build_exec_context(
            sw_automation.app,
            sw_automation.app.ActiveDoc if sw_automation.app else None)

        old_stdout, old_stderr = sys.stdout, sys.stderr
        captured_stdout, captured_stderr = io.StringIO(), io.StringIO()

        try:
            sys.stdout, sys.stderr = captured_stdout, captured_stderr
            exec(code, exec_globals)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

        stdout_text = captured_stdout.getvalue()
        stderr_text = captured_stderr.getvalue()
        result_val = exec_globals.get('result')

        message_parts = []
        if stdout_text:
            message_parts.append(f"=== Output ===\n{stdout_text.rstrip()}")
        if stderr_text:
            message_parts.append(f"=== Stderr ===\n{stderr_text.rstrip()}")
        if result_val is not None:
            message_parts.append(f"=== Result ===\n{result_val}")
        if not message_parts:
            message_parts.append("Code executed successfully (no output)")

        return {
            "success": True,
            "message": "\n\n".join(message_parts),
            "error_code": 0, "error_name": "swSuccess",
            "data": {"stdout": stdout_text, "stderr": stderr_text,
                     "result": str(result_val) if result_val is not None else None}
        }

    except SyntaxError as e:
        return {
            "success": False, "message": f"Syntax error: {e}",
            "error_code": 999, "error_name": "swUnknownError",
            "data": {"error_type": "SyntaxError", "details": str(e)}
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "message": f"Execution error: {e}\n\nTraceback:\n{tb}",
            "error_code": 999, "error_name": "swUnknownError",
            "data": {"error_type": type(e).__name__,
                     "error_message": str(e), "traceback": tb}
        }


def _execute_python_async(code: str, ui_guard=None, budget=None) -> Dict:
    """Submit code for background execution; returns a job_id."""
    def context_factory():
        # Runs inside the worker thread (after CoInitialize): obtain a fresh
        # SolidWorks connection - COM pointers cannot cross apartments.
        import win32com.client
        try:
            app = win32com.client.GetObject(Class="SldWorks.Application")
        except Exception:
            app = win32com.client.Dispatch("SldWorks.Application")
        doc = app.ActiveDoc if app else None
        return _build_exec_context(app, doc)

    watchdog = dict(ui_guard or {})
    if budget and "max_elapsed_sec" in budget:
        watchdog.setdefault("max_runtime_sec", budget["max_elapsed_sec"])
    job = job_manager.submit(code, context_factory, watchdog=watchdog)
    return {
        "success": True,
        "message": f"Job submitted: {job.id}. Poll with get_job_result.",
        "error_code": 0, "error_name": "swSuccess",
        "data": {"job_id": job.id, "status": job.status}
    }


def _get_job_result(job_id: str, timeout: float = 0) -> Dict:
    """Get async job status/result."""
    if not job_id:
        return sw_automation._result(False, "job_id is required",
                                     SwErrors.swInvalidInput)
    job = job_manager.wait(job_id, float(timeout or 0))
    if job is None:
        return sw_automation._result(False, f"Job '{job_id}' not found",
                                     SwErrors.swInvalidInput)

    d = job.to_dict()
    success = job.status in {"pending", "running", "done"}
    msg = f"Job {job.id}: {job.status}"
    if job.status == "done":
        parts = []
        if d["stdout"]:
            parts.append(f"=== Output ===\n{d['stdout'].rstrip()}")
        if d["stderr"]:
            parts.append(f"=== Stderr ===\n{d['stderr'].rstrip()}")
        if d["result"] is not None:
            parts.append(f"=== Result ===\n{d['result']}")
        if parts:
            msg += "\n\n" + "\n\n".join(parts)
    elif job.status in {"error", "blocked", "timeout"}:
        msg += f"\n\n{job.error}"

    return {
        "success": success, "message": msg,
        "error_code": 0 if success else 999,
        "error_name": "swSuccess" if success else "swUnknownError",
        "data": d
    }


# ============================================================================
# FIXED: list_features
# ============================================================================

def _list_features_fixed() -> Dict:
    """
    List all features in the active document.
    Property access for SW 2025/2026 (FirstFeature, GetNextFeature,
    GetTypeName2). callable() must NOT be used on COM objects.
    """
    try:
        doc, err = sw_automation.get_active_doc()
        if err:
            return err

        features = []
        feat = com_get(doc, "FirstFeature", default=None)

        while feat is not None:
            try:
                name = com_get(feat, "Name", default="<unknown>")
                feat_type = com_get(feat, "GetTypeName2", default=None)
                if feat_type is None:
                    feat_type = com_get(feat, "GetTypeName", default="<unknown>")
                suppressed = bool(com_get(feat, "IsSuppressed", default=False))
                features.append({"name": name, "type": feat_type,
                                 "suppressed": suppressed})
            except Exception as e:
                features.append({"name": "<error>", "type": str(e),
                                 "suppressed": False})
            feat = com_get(feat, "GetNextFeature", default=None)

        return {
            "success": True, "message": f"{len(features)} features found",
            "error_code": 0, "error_name": "swSuccess",
            "data": {"features": features, "count": len(features)}
        }
    except Exception as e:
        logger.error(f"List features error: {e}\n{traceback.format_exc()}")
        return {"success": False, "message": f"Error: {e}",
                "error_code": 999, "error_name": "swUnknownError", "data": {}}


# ============================================================================
# Sketch handlers
# ============================================================================

def _close_sketch_handler() -> Dict:
    """Close the active sketch if one is open."""
    try:
        doc, err = sw_automation.get_active_doc()
        if err:
            return err

        had_active = False
        try:
            had_active = doc.SketchManager.ActiveSketch is not None
        except Exception:
            pass

        if had_active:
            try:
                doc.SketchManager.InsertSketch(True)
            except Exception:
                try:
                    doc.InsertSketch2(True)
                except Exception:
                    pass
            return {"success": True, "message": "Sketch closed successfully",
                    "error_code": 0, "error_name": "swSuccess",
                    "data": {"had_active_sketch": True, "action": "closed"}}
        return {"success": True, "message": "No active sketch to close",
                "error_code": 0, "error_name": "swSuccess",
                "data": {"had_active_sketch": False, "action": "none"}}
    except Exception as e:
        logger.error(f"Close sketch error: {e}\n{traceback.format_exc()}")
        return {"success": False, "message": f"Error: {e}",
                "error_code": 999, "error_name": "swUnknownError",
                "data": {"traceback": traceback.format_exc()}}


def _get_sketch_status_handler() -> Dict:
    """Diagnostic info about the current sketch state."""
    try:
        doc, err = sw_automation.get_active_doc()
        if err:
            return err

        info = {"has_active_sketch": False, "active_sketch_name": None,
                "sketch_count": 0, "sketch_names": [], "feature_count": 0,
                "extrusion_count": 0}

        try:
            active_sketch = doc.SketchManager.ActiveSketch
            if active_sketch is not None:
                info["has_active_sketch"] = True
                info["active_sketch_name"] = com_get(active_sketch, "Name",
                                                     default="<unknown>")
        except Exception:
            pass

        feat = com_get(doc, "FirstFeature", default=None)
        while feat is not None:
            try:
                feat_type = com_get(feat, "GetTypeName2", default=None)
                info["feature_count"] += 1
                if feat_type == "ProfileFeature":
                    info["sketch_count"] += 1
                    info["sketch_names"].append(com_get(feat, "Name",
                                                        default="?"))
                elif feat_type == "Extrusion":
                    info["extrusion_count"] += 1
            except Exception:
                pass
            feat = com_get(feat, "GetNextFeature", default=None)

        status = "OPEN" if info["has_active_sketch"] else "CLOSED"
        msg = (f"Sketch status: {status}. "
               f"Sketches: {info['sketch_count']} {info['sketch_names']}. "
               f"Extrusions: {info['extrusion_count']}. "
               f"Total features: {info['feature_count']}")

        return {"success": True, "message": msg, "error_code": 0,
                "error_name": "swSuccess", "data": info}
    except Exception as e:
        logger.error(f"Sketch status error: {e}\n{traceback.format_exc()}")
        return {"success": False, "message": f"Error: {e}",
                "error_code": 999, "error_name": "swUnknownError",
                "data": {"traceback": traceback.format_exc()}}


def _fix_freeze_bar_handler() -> Dict:
    """Manually disable & top-move the Freeze Bar."""
    try:
        if not sw_automation.is_connected:
            r = sw_automation.connect()
            if not r["success"]:
                return r
        info = sw_automation.ensure_features_not_frozen()
        return {"success": True,
                "message": f"Freeze bar handled: {info}",
                "error_code": 0, "error_name": "swSuccess", "data": info}
    except Exception as e:
        logger.error(f"Fix freeze bar error: {e}\n{traceback.format_exc()}")
        return {"success": False, "message": f"Error: {e}",
                "error_code": 999, "error_name": "swUnknownError", "data": {}}


def _get_environment_status() -> Dict:
    """Environment diagnostics: connection, doc, typed module, freeze, modal."""
    data = {
        "connected": False, "active_document": None,
        "typed_module_available": False, "modeler_available": False,
        "freeze_bar": None, "modal_dialog": None, "ui_state": None,
        "session_metrics": None,
    }
    try:
        data["connected"] = sw_automation.is_connected
        if not data["connected"]:
            r = sw_automation.connect()
            data["connected"] = r["success"]

        if data["connected"]:
            doc = sw_automation.app.ActiveDoc
            if doc is not None:
                data["active_document"] = com_get(doc, "GetTitle",
                                                  default="<unknown>")
            data["typed_module_available"] = get_typed_module() is not None
            data["modeler_available"] = get_modeler(sw_automation.app) is not None
            data["modal_dialog"] = detect_modal_dialog()
            data["ui_state"] = data["modal_dialog"].get("state")
            data["session_metrics"] = sw_automation._runtime.report()
            try:
                data["freeze_bar"] = sw_automation.ensure_features_not_frozen(doc)
            except Exception:
                pass

        return {"success": data["connected"],
                "message": "Environment status",
                "error_code": 0 if data["connected"] else 100,
                "error_name": "swSuccess" if data["connected"] else "swConnectionError",
                "data": data}
    except Exception as e:
        logger.error(f"Env status error: {e}\n{traceback.format_exc()}")
        return {"success": False, "message": f"Error: {e}",
                "error_code": 999, "error_name": "swUnknownError", "data": data}


# ============================================================================
# Main Entry Point
# ============================================================================

async def main():
    """Main entry point for MCP server"""
    logger.info("Starting SolidworksMCP v6.5.31...")
    logger.info(f"Log file: {LOG_FILE}")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def run():
    """Run the server"""
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    run()
