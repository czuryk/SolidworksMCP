"""Schemas and dispatch for the v6 high-level tool set."""

from __future__ import annotations

import inspect
from typing import Any, Dict

from mcp.types import Tool
from .shape_tools import SHAPE_TOOLS, SHAPE_TOOL_NAMES


def _object(description, properties=None, required=None):
    return {"type": "object", "description": description,
            "properties": properties or {}, "required": required or [],
            "additionalProperties": True}


def _array(description, items=None):
    return {"type": "array", "description": description,
            "items": items or {"type": "object",
                               "additionalProperties": True}}


NEW_TOOLS = SHAPE_TOOLS + [
    Tool(name="run_transaction",
         description='Execute a whitelisted operation sequence with checkpoint, invariants and rollback. For expert operations; execute_model_code already owns its transaction.',
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
         description='Run a bounded expert operation plan with $steps.N result references and predefined conditions. Use execute_model_code for ordinary shape composition.',
         inputSchema=_object("CAD plan", {
             "plan_id": {"type": "string"}, "operations": _array("Plan steps"),
             "transaction": _object("Checkpoint/rollback policy"),
             "invariants": _object("Plan invariants"),
             "unit": {"type": "string"}, "budget": _object("Plan budget"),
             "save_path": {"type": "string"},
             "allow_unsaved_document": {"type": "boolean", "default": False}},
             ["plan_id", "operations"])),
    Tool(name="create_parametric_sketch",
         description='Create named primitives, relations, dimensions and equations atomically with one rebuild, topology checks and rollback. Automatically verifies 2D working-view orientation and fit.',
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
         description='Add driving dimensions in one rebuild with verified values and atomic rollback. Suppresses/restores the value-entry dialog and fits an activated sketch.',
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
         description='Read runtime counters of the expert transaction layer: rebuild/rollback counts, per-tool timings and the progress of the active operation. To review a modeling run (calls, answer sizes, slowest operations) use get_session_report.',
         inputSchema=_object("Metrics")),
    Tool(name="get_capabilities",
         description='Inspect installed interfaces, operations, backends, export formats and limits when diagnosing an unsupported operation. Ordinary modeling starts with get_model_context.',
         inputSchema=_object("Capabilities")),
    Tool(name="recover_environment",
         description='Apply a bounded recovery policy: inspect UI, clear selection, exit sketch, repair Freeze Bar and at most one explicitly requested retry. Does not resolve an unknown COM outcome.',
         inputSchema=_object("Recovery request", {
             "retry_operation": _object("Optional {op,args} retry"),
             "max_retries": {"type": "integer", "minimum": 0, "maximum": 1}})),
    Tool(name="restart_solidworks",
         description='Verified SolidWorks restart. Use it to release memory after a SOLIDWORKS_MEMORY_HIGH warning (get_executor_status: memory), and for a quarantined session whose MCP owner process exited (owner_alive=false); then follow with reconcile_session(operation_id). Lists open documents and refuses when any is unsaved unless discard_unsaved=true; then exits SolidWorks, waits for the process to end, starts it and reconnects. Documents are not reopened: shape tools open document_path themselves. Returns previous and new PIDs. Takes minutes: collect with wait_operation.',
         inputSchema=_object("Restart request", {
             "discard_unsaved": {"type": "boolean", "default": False,
                                 "description": "Close unsaved documents without saving and end an unresponsive process"}})),
    Tool(name="image_to_sketch",
         description='Convert raster line art or silhouettes into validated native sketch geometry. Use for an actual tracing task; choose trace/output modes and calibration explicitly. Analysis is isolated; CAD commit is transactional. Ordinary 3D references do not require tracing.',
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
         description='Compare actual CAD contours with a calibrated image/mask without automatic best-fit. Returns symmetric errors, per-entity attribution and overlays. CAD export runs on STA; raster metrics run in an isolated worker.',
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
         description='Create and verify a separate revolved body from an explicit parametric sketch/axis with checkpoint and save. Use for profiles with arcs or splines; model.revolve covers polyline profiles, open or closed (rings, bushings).',
         inputSchema=_object("Revolved body", {
             "sketch": _object("create_parametric_sketch args"),
             "revolve": _object("revolve_boss args"),
             "body_name": {"type": "string"}, "feature_name": {"type": "string"},
             "checkpoint": {"type": "boolean"}, "save_path": {"type": "string"},
             "allow_unsaved_document": {"type": "boolean"},
             "idempotency_key": {"type": "string"}},
             ["sketch", "revolve", "body_name"])),
    Tool(name="create_swept_member",
         description='Create a checked sweep from a new/existing path and circular, elliptical or custom profile. Validates path and profile contact; uses a native circle sketch on SW2026. For ordinary round members use model.tube or model.variable_tube.',
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
         description='Create an insert and scoped host pocket from explicit profiles with clearance verification and rollback. Useful for planar profile fits in a hand-built part. In a shape program start from a template (model_api_lookup: template:inlay, template:pin_socket, template:curved_inlay); model.surface_patch handles supported curved skins.',
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
         description='Export selected bodies to explicit SLDPRT/STEP/STL destinations with rebuild, structural checks, hashes and atomic rollback. Supports expert destination/overwrite workflows; export_model is the stricter new-directory shape delivery route.',
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
         description='Compare selected native body mesh silhouettes with a calibrated image transform. Returns symmetric deviations and overlays; native capture and raster comparison use separate bounded workers. Best-fit alignment and screenshot segmentation are diagnostic opt-ins. For the ordinary likeness check of a whole model use compare_reference(source="native"): one call, no transform; this tool remains for a subset of bodies, line drawings and an explicit affine transform.',
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
         description='Diff and apply a whitelisted expert operation DAG transactionally. Retained for operation graphs beyond shape constructors; execute_model_code owns ordinary shape dependencies.',
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
MUTATING_TOOLS -= {"get_model_context", "model_api_lookup", "measure_reference", "compare_reference",
                   "inspect_model"}

FIRST_GEOMETRY_TOOLS = {
    "create_sketch", "create_sketch_on_face", "sketch_contour",
    "create_parametric_sketch", "image_to_sketch", "create_revolved_body",
    "create_multibody_insert", "create_semantic_primitive",
    "advanced_extrude", "advanced_cut", "revolve_boss", "create_swept_member",
}


def augment_tool_schemas(tools):
    for tool in tools:
        if tool.name in SHAPE_TOOL_NAMES:
            continue
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
    arguments = dict(arguments or {})
    presentation = {}
    if name in SHAPE_TOOL_NAMES:
        # Presentation options shape the answer; they never reach the service.
        options = ('verbose', 'detail', 'components') if name == 'get_model_context' else ('verbose',)
        presentation = {key: arguments.pop(key) for key in options if key in arguments}
        if presentation.get('detail', 'summary') not in {'summary', 'full'}:
            return automation._error("INVALID_ARGUMENTS", "detail must be summary or full")
        unknown = sorted(set(arguments or {}) - set(signature.parameters))
        if unknown:
            return automation._error(
                "INVALID_ARGUMENTS", "Unknown shape tool arguments",
                details={"unknown": unknown})
    accepted = {key: value for key, value in (arguments or {}).items()
                if key in signature.parameters}
    result = method(**accepted)
    if name in SHAPE_TOOL_NAMES and not presentation.get('verbose'):
        from .shape_responses import compact_response
        result = compact_response(name, presentation, result)
    return result
