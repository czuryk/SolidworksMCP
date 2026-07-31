"""Schemas and dispatch for the v6 high-level tool set."""

from __future__ import annotations

import inspect
from typing import Any, Dict

from mcp.types import Tool


def _object(description, properties=None, required=None):
    return {"type": "object", "description": description,
            "properties": properties or {}, "required": required or [],
            "additionalProperties": True}


def _array(description, items=None):
    return {"type": "array", "description": description,
            "items": items or {"type": "object",
                               "additionalProperties": True}}


NEW_TOOLS = [
    Tool(name="run_transaction",
         description="Run a whitelisted sequence in one COM session with a "
                     "save-copy checkpoint, invariants, idempotency, and full rollback.",
         inputSchema=_object("Transactional CAD operation", {
             "name": {"type": "string"},
             "operations": _array("Ordered {op,args,when} operations"),
             "checkpoint": _object("Checkpoint policy/path"),
             "invariants": _object("Post-commit invariants"),
             "on_failure": {"type": "string", "enum": ["rollback", "leave_partial"]},
             "idempotency_key": {"type": "string"},
             "save_policy": _object("save_before/save_after_success policy"),
             "budget": _object("Elapsed/rebuild/solver/rollback limits"),
             "save_path": {"type": "string"},
             "allow_unsaved_document": {"type": "boolean", "default": False}},
             ["name", "operations"])),
    Tool(name="execute_cad_plan",
         description="Execute a bounded declarative CAD plan. Supports $steps.N "
                     "result references and only predefined safe conditions.",
         inputSchema=_object("CAD plan", {
             "plan_id": {"type": "string"}, "operations": _array("Plan steps"),
             "transaction": _object("Checkpoint/rollback policy"),
             "invariants": _object("Plan invariants"),
             "unit": {"type": "string"}, "budget": _object("Plan budget"),
             "save_path": {"type": "string"},
             "allow_unsaved_document": {"type": "boolean", "default": False}},
             ["plan_id", "operations"])),
    Tool(name="create_parametric_sketch",
         description="Atomically create named line/arc/circle/spline geometry, "
                     "relations, batched dimensions, equations, one rebuild, "
                     "topology validation, persistent IDs, rollback, and "
                     "verified Normal To / pixel-measured Fit to Screen.",
         inputSchema=_object("Parametric sketch", {
             "name": {"type": "string"},
             "plane": {"type": "string", "enum": ["Front", "Top", "Right"]},
             "unit": {"type": "string"},
             "entities": _array("Entities with stable id and type"),
             "constraints": _array("Topological/geometric relations"),
             "dimensions": _array("Batch dimensions"),
             "equations": _array("Dimension equations"),
             "solve": _object("Definition target and guard policy"),
             "validation": _object("Contour/entity limits"),
             "transaction": _object("Rollback/checkpoint policy"),
             "idempotency_key": {"type": "string"},
             "output_mode": {"type": "string",
                             "enum": ["locked_trace", "minimal_parametric",
                                      "reference_spline", "construction_reference"]}},
             ["name", "entities"])),
    Tool(name="add_dimensions_batch",
         description="Create driving dimensions with swInputDimValOnCreate "
                     "suppressed and restored, one rebuild, verified values, "
                     "request-id/name mapping, atomic rollback, and verified "
                     "Normal To / Fit when the sketch is activated.",
         inputSchema=_object("Dimension batch", {
             "sketch_name": {"type": "string"},
             "dimensions": _array("Dimension requests"),
             "suppress_modify_dialog": {"type": "boolean", "default": True},
             "rebuild": {"type": "string", "enum": ["once", "none"]},
             "rollback_on_failure": {"type": "boolean", "default": True},
             "guard_policy": {"type": "string",
                              "enum": ["operation_scoped", "session_scoped",
                                       "leave_disabled"]},
             "unit": {"type": "string"}}, ["sketch_name", "dimensions"])),
    Tool(name="analyze_sketch_dof",
         description="Explain under/over-definition per entity: coordinates, "
                     "free motions, connectivity, minimal recommendations, "
                     "conflict candidates, and Fix-quality warnings.",
         inputSchema=_object("DOF analysis", {
             "sketch_name": {"type": "string"},
             "include_recommendations": {"type": "boolean", "default": True}},
             ["sketch_name"])),
    Tool(name="get_session_metrics",
         description="Return MCP/COM call counts, rebuild/solver/rollback metrics, "
                     "saved-result progress, artifact ratio, and per-tool timing.",
         inputSchema=_object("Metrics")),
    Tool(name="get_capabilities",
         description="Discover SolidWorks/MCP versions, typed interfaces, native "
                     "operations, image/geometry backends, export formats, limits, "
                     "and version-specific known limitations.",
         inputSchema=_object("Capabilities")),
    Tool(name="recover_environment",
         description="Finite recovery policy: UI check, clear selection, exit "
                     "sketch edit, repair Freeze Bar, and at most one explicit retry.",
         inputSchema=_object("Recovery request", {
             "retry_operation": _object("Optional {op,args} retry"),
             "max_retries": {"type": "integer", "minimum": 0, "maximum": 1}})),
    Tool(name="image_to_sketch",
         description="GPU raster-to-CAD pipeline using SAM 2.1/ViTMatte for "
                     "regions and a verified DexiNed+TEED ensemble for line art, "
                     "with explicit trace/output modes, verified four-point "
                     "homography rectification, and "
                     "complexity-bounded B-spline/primitive optimization, "
                     "independent perturbation checks, "
                     "reverse-raster validation, an isolated timeout-controlled "
                     "analysis worker, and transactional vector-only SolidWorks commit.",
         inputSchema=_object("Image vectorization", {
             "image_path": {"type": "string"}, "sketch_name": {"type": "string"},
             "plane": {"type": "string", "enum": ["Front", "Top", "Right"]},
             "unit": {"type": "string"},
             "image_mode": {"type": "string",
                            "enum": ["filled_silhouette", "technical_drawing",
                                     "line_drawing", "trace_as_is"]},
             "contour_selection": _object("ROI/topology selection"),
             "trace": _object("Explicit boundary selection policy", {
                 "backend": {"type": "string",
                             "enum": ["deep_matting", "sam2_vitmatte",
                                      "line_art", "deep_line_art",
                                      "dexined_teed", "classical"]},
                 "mode": {"type": "string",
                          "enum": ["outer_silhouette",
                                   "silhouette_with_holes",
                                   "all_region_boundaries",
                                   "guided_components",
                                   "stroke_centerlines", "stroke_edges",
                                   "all_visible_edges"]},
                 "edge_semantics": {"type": "string",
                                    "enum": ["physical_outer_ink_edge",
                                             "matte_alpha"]},
                 "component_policy": {"type": "string",
                                      "enum": ["largest_prompted", "largest",
                                               "all_above_min_area",
                                               "prompted_only"]},
                 "alpha_threshold": {"type": "number", "minimum": 0.05,
                                     "maximum": 0.95},
                 "background_threshold": {},
                 "min_area_px": {"type": "integer", "minimum": 1},
                 "max_components": {"type": "integer", "minimum": 1,
                                    "maximum": 64},
                 "roi_px": {"type": "array", "minItems": 4, "maxItems": 4,
                            "items": {"type": "number"}},
                 "box_px": {"type": "array", "minItems": 4, "maxItems": 4,
                            "items": {"type": "number"}},
                 "positive_points_px": _array("Positive [x,y] prompt points",
                                               {"type": "array",
                                                "items": {"type": "number"}}),
                 "negative_points_px": _array("Negative [x,y] prompt points",
                                               {"type": "array",
                                                "items": {"type": "number"}}),
                 "line_probability_threshold": {
                     "type": "number", "minimum": 0.05, "maximum": 0.95},
                 "dexined_threshold": {
                     "type": "number", "minimum": 0.05, "maximum": 0.95},
                 "teed_threshold": {
                     "type": "number", "minimum": 0.05, "maximum": 0.95},
                 "consensus_radius_px": {
                     "type": "number", "minimum": 0.5, "maximum": 8},
                 "path_consensus_radius_px": {
                     "type": "number", "minimum": 0.5, "maximum": 16},
                 "min_branch_length_px": {"type": "number", "minimum": 1},
                 "max_paths": {"type": "integer", "minimum": 1,
                               "maximum": 4096},
                 "stroke_threshold": {},
                 "stroke_edge_side": {
                     "type": "string",
                     "enum": ["outer_edge", "inner_edge", "both"]},
                 "edge_topology_source": {
                     "type": "string",
                     "enum": ["ink_centerline", "ensemble_ridge"]},
                 "trimap_radius_px": {"type": "integer", "minimum": 1}}),
             "calibration": _object("bbox/two-point/mm-per-pixel calibration"),
             "placement": _object("Named image anchor and model placement", {
                 "image_anchor": {
                     "oneOf": [
                         {"type": "string", "enum": [
                             "silhouette_bottom_center",
                             "silhouette_bbox_center", "image_center",
                             "centroid", "pixel_point"]},
                         {"type": "array", "minItems": 2, "maxItems": 2,
                          "items": {"type": "number"}}]},
                 "pixel_point": {"type": "array", "minItems": 2,
                                 "maxItems": 2,
                                 "items": {"type": "number"}},
                 "model_anchor": {"type": "array", "minItems": 2,
                                  "maxItems": 3,
                                  "items": {"type": "number"}},
                 "rotation_deg": {"type": "number"},
                 "mirror_x": {"type": "boolean"},
                 "mirror_y": {"type": "boolean"}}),
             "geometry": _object("Legacy alias for approximation controls"),
             "approximation": _object("Primitive fitting controls", {
                 "preset": {"type": "string",
                            "enum": ["coarse", "balanced", "fine", "ultra"]},
                 "prefer": {"type": "array",
                            "items": {"type": "string",
                                      "enum": ["line", "arc", "circle", "spline"]}},
                 "max_error_mm": {"type": "number", "exclusiveMinimum": 0},
                 "max_entities": {"type": "integer", "minimum": 1},
                 "min_feature_mm": {"type": "number", "exclusiveMinimum": 0},
                 "min_segment_length_mm": {"type": "number",
                                           "exclusiveMinimum": 0},
                 "target_segment_length_mm": {"type": "number",
                                              "exclusiveMinimum": 0},
                 "max_segment_length_mm": {"type": "number",
                                           "exclusiveMinimum": 0},
                 "corner_angle_deg": {"type": "number", "exclusiveMinimum": 0,
                                      "maximum": 180},
                 "max_spline_fit_points": {"type": "integer", "minimum": 4,
                                           "maximum": 256},
                 "spline_fit_tolerance_ratio": {"type": "number",
                                                "minimum": 0.05,
                                                "maximum": 1},
                 "simplification_tolerance_mm": {"type": "number",
                                                 "exclusiveMinimum": 0},
                 "max_total_fit_points": {"type": "integer", "minimum": 4,
                                          "maximum": 10000},
                 "max_total_control_points": {"type": "integer", "minimum": 4,
                                              "maximum": 10000},
                 "max_control_points_per_spline": {
                     "type": "integer", "minimum": 4, "maximum": 512,
                     "description": (
                         "Hard per-B-spline COM safety cap; default 64")},
                 "explicit_spline_tolerance_mm": {
                     "type": "number", "exclusiveMinimum": 0,
                     "description": (
                         "Maximum symmetric error when materializing free-form "
                         "curves as deterministic explicit NURBS; default is "
                         "0.8 * max_error_mm and never exceeds max_error_mm")},
                 "curve_strategy": {"type": "string",
                                    "enum": ["auto", "periodic_bspline",
                                             "hybrid_primitives"]},
                 "entity_complexity_weight": {"type": "number",
                                              "exclusiveMinimum": 0},
                 "smoothing": {"type": "number", "minimum": 0, "maximum": 1},
                 "output_mode": {"type": "string",
                                 "enum": ["locked_trace", "minimal_parametric",
                                          "reference_spline",
                                          "construction_reference"]}}),
             "validation": _object("IoU/Hausdorff/topology gates"),
             "commit": _object("Confidence/checkpoint/rollback policy"),
             "debug": _object("Overlay/vector artifact policy"),
             "models": _object("Optional local SAM/ViTMatte model IDs"),
             "require_orthographic": {
                 "type": "boolean", "default": False,
                 "description": (
                     "Reject implicit perspective tracing unless projection.mode "
                     "is orthographic or homography")},
             "projection": _object(
                 "Explicit orthographic/perspective policy", {
                     "mode": {"type": "string",
                              "enum": ["orthographic", "homography",
                                       "trace_as_is"]},
                     "source_quad_px": {
                         "type": "array", "minItems": 4, "maxItems": 4,
                         "items": {"type": "array", "minItems": 2,
                                   "maxItems": 2,
                                   "items": {"type": "number"}}},
                     "output_size_px": {
                         "type": "array", "minItems": 2, "maxItems": 2,
                         "items": {"type": "integer", "minimum": 64,
                                   "maximum": 16384}},
                     "confidence_cap": {"type": "number", "minimum": 0,
                                        "maximum": 1}}),
             "idempotency_key": {"type": "string"}},
             ["image_path", "sketch_name", "calibration"])),
    Tool(name="export_sketch_geometry",
         description="Export true CAD primitives, stable IDs, transforms, topology, "
                     "relations and compact summary; full geometry goes to a file "
                     "unless inline mode is explicitly requested.",
         inputSchema=_object("Sketch geometry export", {
             "sketch_name": {"type": "string"},
             "coordinate_system": {"type": "string"}, "unit": {"type": "string"},
             "include": _object("construction/relations/dimensions/topology"),
             "output": _object("summary_and_file/compact/inline output")},
             ["sketch_name"])),
    Tool(name="render_sketch_svg",
         description="Render one or more exported sketches to layered, parse-verified "
                     "SVG while preserving line/circle/arc/spline entity identities.",
         inputSchema=_object("SVG render", {
             "sketch_names": {"type": "array", "items": {"type": "string"}},
             "path": {"type": "string"}, "view": _object("viewBox/unit/padding"),
             "style": _object("Layer/entity colors")}, ["sketch_names", "path"])),
    Tool(name="compare_sketches",
         description="Compare two sketches directly as vector geometry with symmetric "
                     "distances and entity-linked error ranking.",
         inputSchema=_object("Sketch comparison", {
             "reference_sketch": {"type": "string"},
             "candidate_sketch": {"type": "string"},
             "tolerance": _object("mean/p95/max/sample tolerances"),
             "unit": {"type": "string"}, "report_path": {"type": "string"}},
             ["reference_sketch", "candidate_sketch"])),
    Tool(name="compare_sketch_to_reference",
         description="Reverse-rasterize topology-preserving CAD primitives against a "
                     "calibrated image/mask without automatic best-fit. Handles nested "
                     "contours and holes, returns symmetric metrics, maximum-deviation "
                     "zones, true per-entity attribution, PNG/SVG overlays and a report. "
                     "Native CAD export stays in COM; all image/scientific libraries run "
                     "in a timeout-controlled isolated worker.",
         inputSchema=_object("Sketch/reference comparison", {
             "sketch_name": {"type": "string"},
             "reference_image": {"type": "string"},
             "transform": _object("saved calibration or explicit pixel-to-sketch matrix"),
             "tolerance": _object("draft/balanced/strict profile, explicit mean_mm, "
                                  "p95_mm, max_mm, min_iou/min_line_support, sampling"),
             "outputs": _object("overlay/png_preview, svg_overlay and report paths"),
             "contour_selection": _object("Reference segmentation"),
             "image_mode": {"type": "string"}},
             ["sketch_name", "reference_image"])),
    Tool(name="compare_sketch_to_image",
         description="Alias of compare_sketch_to_reference for image-focused workflows.",
         inputSchema=_object("Sketch/image comparison", {
             "sketch_name": {"type": "string"}, "image_path": {"type": "string"},
             "transform": _object("Calibration transform"),
             "tolerance": _object("Quality profile and explicit metric thresholds"),
             "outputs": _object("PNG/SVG/report artifacts")},
             ["sketch_name", "image_path"])),
    Tool(name="create_revolved_body",
         description="Create a named parametric profile and axis, revolve it, verify "
                     "faces/body/bbox/volume, atomically rename the real feature/body, "
                     "checkpoint and save. Always creates a distinct body (merge=false).",
         inputSchema=_object("Revolved body", {
             "sketch": _object("create_parametric_sketch args"),
             "revolve": _object("revolve_boss args"),
             "body_name": {"type": "string"}, "feature_name": {"type": "string"},
             "checkpoint": {"type": "boolean"}, "save_path": {"type": "string"},
             "allow_unsaved_document": {"type": "boolean"},
             "idempotency_key": {"type": "string"}},
             ["sketch", "revolve", "body_name"])),
    Tool(name="create_swept_member",
         description="Atomically create/use a path and create a circular, elliptical "
                     "or custom-profile sweep. Checks line/spline self-intersection, "
                     "branches, sharp corners, numeric spline/arc bend radius, exact "
                     "SOLIDWORKS selection marks, profile-plane contact, faces, bbox, "
                     "body name and volume. On SW2026 circles are materialized as a "
                     "native profile sketch; the blocking special circular mode is "
                     "never called.",
         inputSchema=_object("Swept member", {
             "path_sketch": {"type": "string"},
             "path": _object("Optional create_parametric_sketch path args"),
             "profile": _object("circle/ellipse/custom profile", {
                 "type": {"type": "string", "enum": [
                     "circle", "ellipse", "custom", "sketch"]},
                 "diameter": {"type": "number"},
                 "radius": {"type": "number"},
                 "major_radius": {"type": "number"},
                 "minor_radius": {"type": "number"},
                 "plane": {"type": "string"},
                 "center": {"type": "array", "items": {"type": "number"}},
                 "rotation_deg": {"type": "number"},
                 "sketch_name": {"type": "string"},
                 "sketch": _object("Explicit create_parametric_sketch profile"),
                 "max_radius": {"type": "number"},
                 "bend_radius_factor": {"type": "number"},
                 "contact_tolerance": {"type": "number"},
                 "advanced_smoothing": {"type": "boolean"}}),
             "body_name": {"type": "string"}, "feature_name": {"type": "string"},
             "merge": {"type": "boolean"}, "min_bend_radius": {"type": "number"},
             "allow_sharp_corners": {"type": "boolean"},
             "checkpoint": {"type": "boolean"},
             "save_path": {"type": "string"},
             "allow_unsaved_document": {"type": "boolean"},
             "idempotency_key": {"type": "string"},
             "unit": {"type": "string"}, "auto_verify": {"type": "boolean"}},
             ["profile", "body_name"])),
    Tool(name="create_multibody_insert",
         description="Create a separate insert body and scoped host pocket with explicit "
                     "clearance, then verify both bodies and interference in one rollback-safe call.",
         inputSchema=_object("Multibody insert", {
             "insert_sketch": _object("Insert profile"),
             "insert_extrude": _object("Insert boss"),
             "host_body": {"type": "string"}, "insert_body": {"type": "string"},
             "clearance": {"type": "number"},
             "clearance_tolerance": {"type": "number"},
             "pocket_sketch": _object("Optional explicit pocket profile"),
             "pocket_cut": _object("Scoped cut args"),
             "checkpoint": {"type": "boolean"},
             "save_path": {"type": "string"},
             "allow_unsaved_document": {"type": "boolean"},
             "idempotency_key": {"type": "string"}},
             ["insert_sketch", "insert_extrude", "host_body",
              "insert_body", "clearance"])),
    Tool(name="create_semantic_primitive",
         description="Create a reusable named CAD pattern (revolved_shell, tubular_member, "
                     "capsule_profile, clearance_insert, uniform_shell, symmetric_pair, "
                     "linear_feature_array, circular_feature_array).",
         inputSchema=_object("Semantic primitive", {
             "kind": {"type": "string"}, "parameters": _object("Pattern parameters")},
             ["kind", "parameters"])),
    Tool(name="export_bundle",
         description="Rebuild and verify named solid bodies, stage SLDPRT, STEP and "
                     "selected-body STLs beside their targets, structurally inspect every "
                     "format, then atomically replace all files with hash verification and "
                     "full rollback before writing a SHA-256 manifest.",
         inputSchema=_object("Export bundle", {
             "sldprt_path": {"type": "string"}, "step_path": {"type": "string"},
             "stl_directory": {"type": "string"},
             "bodies": {"type": "array", "items": {"type": "string"}},
             "naming": _object("Body-to-filename mapping"),
             "stl": _object("STL quality, deviation_mm, angle_tolerance_deg, "
                            "binary, and preserve_origin settings"),
             "report": {"type": "boolean"}, "overwrite": {"type": "boolean"},
             "unit": {"type": "string"}},
             ["sldprt_path", "step_path", "stl_directory"])),
    Tool(name="compare_body_silhouette_to_image",
         description="Export selected bodies as deterministic native meshes, project the "
                     "triangle union orthographically, and compare it to a reference with "
                     "an explicit model-mm-to-pixel transform. A framed screenshot remains "
                     "independent visual evidence; legacy screenshot segmentation is "
                     "diagnostic and opt-in. Reports symmetric metric deviations and "
                     "maximum-deviation zones; bbox best-fit remains diagnostic and opt-in.",
         inputSchema=_object("Body silhouette comparison", {
             "reference_image": {"type": "string"},
             "screenshot_path": {"type": "string"},
             "orientation": {"type": "string", "enum": [
                 "front", "back", "right", "left", "top", "bottom"]},
             "bodies": {"type": "array", "items": {"type": "string"}},
             "candidate_source": {"type": "string", "default": "native_mesh",
                                  "enum": ["native_mesh",
                                           "screenshot_segmentation"]},
             "mesh": _object("Native mesh quality, deviation_mm, "
                             "angle_tolerance_deg and max_triangles settings"),
             "reference_mode": {"type": "string", "default": "filled_silhouette",
                                "enum": ["filled_silhouette",
                                         "technical_drawing", "line_drawing",
                                         "trace_as_is"]},
             "contour_selection": _object("Reference ROI/topology selection"),
             "transform": _object("native_mesh model-mm-to-reference-pixel 3x3 affine "
                                  "matrix and required reference mm_per_pixel; "
                                  "allow_bbox_fit is diagnostic"),
             "tolerance": _object("draft/balanced/strict IoU, Hausdorff and "
                                  "segmentation-confidence thresholds"),
             "outputs": _object("Overlay/report paths")},
             ["reference_image", "screenshot_path"])),
    Tool(name="sync_model_graph",
         description="Topologically sort and diff a declarative CAD DAG, preview changes, "
                     "or apply only changed whitelisted nodes transactionally.",
         inputSchema=_object("Declarative model graph", {
             "graph_id": {"type": "string"}, "nodes": _array("DAG nodes"),
             "mode": {"type": "string", "enum": ["plan", "apply"]},
             "invariants": _object("Graph invariants"),
             "save_path": {"type": "string"},
             "allow_unsaved_document": {"type": "boolean"}},
             ["graph_id", "nodes"])),
]


NEW_TOOL_NAMES = {tool.name for tool in NEW_TOOLS}

MUTATING_TOOLS = {
    "create_new_part", "create_new_assembly", "open_document", "save_document",
    "close_document", "create_sketch", "create_sketch_on_face", "draw_line",
    "draw_circle", "draw_rectangle", "draw_arc", "draw_polygon", "sketch_contour",
    "extrude_sketch", "cut_extrude", "advanced_extrude", "advanced_cut",
    "fillet_edges", "chamfer_edges", "revolve_boss", "shell", "reference_plane",
    "reference_axis", "linear_pattern", "circular_pattern", "mirror_feature",
    "delete_feature", "rename_feature", "show_body", "hide_body", "rename_body",
    "set_body_transparency", "set_body_color", "set_units", "execute_python",
    "execute_python_async", "fix_freeze_bar", "close_sketch", "export_file",
} | (NEW_TOOL_NAMES - {"analyze_sketch_dof", "get_session_metrics",
                       "get_capabilities", "export_sketch_geometry",
                       "render_sketch_svg", "compare_sketches",
                       "compare_sketch_to_reference", "compare_sketch_to_image",
                       "compare_body_silhouette_to_image"})

FIRST_GEOMETRY_TOOLS = {
    "create_sketch", "create_sketch_on_face", "sketch_contour",
    "create_parametric_sketch", "image_to_sketch", "create_revolved_body",
    "create_multibody_insert", "create_semantic_primitive",
    "advanced_extrude", "advanced_cut", "revolve_boss", "create_swept_member",
}


def augment_tool_schemas(tools):
    for tool in tools:
        if tool.name not in MUTATING_TOOLS:
            continue
        properties = tool.inputSchema.setdefault("properties", {})
        properties.setdefault("allow_unsaved_document", {
            "type": "boolean", "default": False,
            "description": "Explicitly allow first geometry in an unsaved document"})
        properties.setdefault("save_path", {
            "type": "string",
            "description": "Save unsaved document before first geometry"})
        properties.setdefault("budget", {
            "type": "object", "additionalProperties": True,
            "description": "Stop-loss limits"})
        properties.setdefault("ui_guard", {
            "type": "object", "additionalProperties": True,
            "description": "Watchdog and known-dialog policy"})
        properties.setdefault("recovery", {
            "type": "object", "additionalProperties": True,
            "description": "Finite recovery policy; auto retry is capped at one"})
    return tools


def dispatch_new_tool(automation, name: str, arguments: Dict[str, Any]):
    method = getattr(automation, name)
    signature = inspect.signature(method)
    accepted = {key: value for key, value in (arguments or {}).items()
                if key in signature.parameters}
    return method(**accepted)
