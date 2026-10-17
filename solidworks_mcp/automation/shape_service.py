"""Application boundary for composing and observing native shape programs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import traceback
import copy
import shutil
import tempfile
import uuid

from ..shape_program import ProgramError, run_program, apply_patch, changed_line_count
from ..shape_limits import limits, component_budget
from .shape_parametric import (ParametricCompiler, read_record, write_record,
                              observed_revision, global_values, update_native)
from .shape_reconcile import prepare_reconcile,reconcile_native
from ..shape_equations import plan_recipes
from ..shape_checks import program_warnings
from .com_utils import com_get, command_scope, build_scope
from .runtime import atomic_json_write, PhaseTimer, phase
from .shape_working_view import focus_bodies, changed_body_names


SHAPE_INSTRUCTIONS = """Native parametric SolidWorks modeling from a short Python shape program
(mm; Y up, X width, Z depth). A native build takes minutes, a draft takes milliseconds: iterate
on the draft, build once.
1. get_model_context: document, revision, parameters, bodies.
2. model_api_lookup(topic="all") once: constructors, pitfalls, workflow, examples.
3. Draft: execute_model_code(mode="analyze", code, draft_views=["front","right"]) returns predicted
   extents and silhouettes without SolidWorks. With reference images call compare_reference per
   view and correct reliable proportion mismatches. If a mismatch follows a documented draft
   limitation, record it and verify natively after building; do not loosen tolerance to pass the draft.
4. Build once: mode="create" with a new absolute output_path. Read the returned views and warnings.
5. Change with mode="edit" and patch=[{old,new}] (or parameters); batch related changes, each edit
   costs a full rebuild. The answer lists what was rebuilt; mode="revert" undoes the last edit.
6. Verify numbers with inspect_model (fits via body_pairs, printability=true) and
   compare_reference(document_path, source='native').
7. Deliver with export_model.
A call waits at most 45 s. A longer operation answers status RUNNING with an operation_id: collect
it with wait_operation; do not retry or reconcile. Failed calls carry recommended_actions with the
exact next tool call. expert_tools covers operations outside the shape constructors.
"""


API = {
    "coordinate_system": {"unit": "mm", "up": "Y", "width": "X", "depth": "Z"},
    "constructors": {
        "model.parameter": "parameter(name, value, *, unit='mm') -> expression (mm or ratio)",
        "model.frame": "frame(*, origin=(0, 0, 0), rotation=(0, 0, 0)) -> Frame; radians, extrinsic X/Y/Z then translation",
        "model.place": "place(name, components, *, frame) -> component; consumes inputs; accepts a single component/name or a sequence",
        "model.sectioned_solid": "sectioned_solid(name, sections, *, center_xz=(0, 0), axis='Y', origin=None, spans='smooth') -> component; sections are (y, rx, rz); axis X: (x, ry, rz), axis Z: (z, rx, ry), heights from origin; spans='linear' gives ruled spans between neighbouring sections instead of one smooth loft",
        "model.tube": "tube(name, points, radius) -> component with start/end; points are (x,y,z); two points give an analytic cylinder (1 cylindrical face, 2 planar ends), more points a swept tube",
        "model.dome": "dome(name, radius, height, *, center=(0, 0, 0), axis='Y') -> component; spherical cap, center at base, height <= radius; axis X/Y/Z or -X/-Y/-Z points from the base to the apex",
        "model.ellipsoid": "ellipsoid(name, radii, *, center=(0, 0, 0), approximate=False) -> component; exact rz/rx constant, otherwise opt in to approximate",
        "model.variable_tube": "variable_tube(name, points, radii)",
        "model.rounded_prism": "rounded_prism(name, width, height, depth, corner_radius, *, center=(0, 0, 0), axis='Z') -> component; extruded along axis: Z has width on X and height on Y, Y has width X and height Z, X has width Z and height Y; corner_radius = min(width, height)/2 is a slot (stadium) with true cylindrical ends, a cylinder when width == height; corner_radius = 0 is a plain box",
        "model.prism": "prism(name, points, depth, *, center=(0, 0, 0), axis='Z') -> component; closed polygon of 3-64 (a, b) points relative to center, extruded symmetrically along axis: Z has a on X and b on Y, Y has a on X and b on Z, X has a on Z and b on Y; straight edges, concave outlines allowed, crossing ones rejected",
        "model.fillet": "fillet(name, component, radius, *, edges='all') -> component; native constant-radius Fillet, consumes component; edges: 'all', {'planar_loop': {'axis': 'Z', 'side': 'max'}} or {'between': [A, B]}",
        "model.chamfer": "chamfer(name, component, distance, *, edges='all') -> component; native 45 degree Chamfer with the same edge selectors",
        "model.face_anchor": "face_anchor(host, *, origin, direction) -> deferred attachment; ray-normal plane, constant direction",
        "model.anchored_extrude": "anchored_extrude(name, anchor, profile, depth) -> separate solid; circle/ellipse/rounded_rectangle profile",
        "model.combine": "combine(name, components, *, operation='union', expected_bodies=1); components is a sequence of components/names, e.g. [body, cutter]. expected_bodies is an assertion checked after the native Boolean. Analyze uses the declared count; it does not validate the native result.",
        "model.surface_patch": "surface_patch(name, host, mask, thickness, *, offset=0, inward=False) -> curved skin; direct unconsumed supported host, consumes mask",
        "model.surface_point": "surface_point(host, *, fraction=0.5, angle=0)",
        "model.path_point": "path_point(host, *, segment=0, fraction=0.5)",
        "model.path_tangent": "path_tangent(host, *, segment=0, fraction=0.5)",
        "model.revolve": "revolve(name, profile, *, center_xz=(0, 0), axis='Y', origin=None, closed=False) -> component; profile is (height, radius) stations along the axis from origin; closed=True takes the stations as a closed outline that need not touch the axis (ring, bushing)",
        "model.mirror": "mirror(name, components, *, plane='Right') -> copied component; Right is X=0; components is a sequence of components/names, e.g. [seed] even for one component",
        "model.pattern": "pattern(name, components, *, count, step=None, axis=None, center=(0, 0, 0)) -> copied component; components is a sequence of components/names, e.g. [seed] even for one component; step=(dx, dy, dz) gives the count-1 copies in a row, each moved by one more step; axis='Y' (with center) gives the count-1 copies spread over a full circle about that axis through center. One native pattern feature instead of count placed components. count includes the seed and is a constant; like mirror the seed stays its own component, so combine both: model.combine('Teeth', [tooth, row])",
    },
    "math": "sin(), cos(), sqrt(), pi, symbolic arithmetic. Branch/loop counts must use constants: model.value(parameter) is rejected because it would freeze a native dependency.",
    "code": "Use the injected model. Functions, for loops and comprehensions are supported. No imports.",
    "parameters": "Native global variables drive parameter dimensions and equation curves. Parameters must be positive; coordinates may be signed. mm and ratio are supported. Full native rebuild needs ForceRebuild3 / Ctrl+Q.",
    "draft": "execute_model_code(mode='analyze', code=..., draft_views=['front','right'], draft_components=[names], draft_directory=None) evaluates the program without SolidWorks in milliseconds. The answer always carries draft {bbox, size, bodies: {name: [min_x,min_y,min_z,max_x,max_y,max_z]}, estimated: [names]} in mm; draft_views adds silhouette images (front, back, right, left, top, bottom; same directions as the native standard views), draft_components adds extents of named components, consumed ones included. Extents are sampled from the recipe formulas: exact to about 0.01 mm for axis-aligned primitives. A plain box that covers one whole side of its target is subtracted as a plane cut and an intersection is clipped to a box mask, both exactly; other cuts are not subtracted, so that difference keeps the extents of its target. Bodies listed in estimated depend on native faces (surface_patch, anchored_extrude, other intersections and cuts) and are upper bounds. warnings predict native failures: TUBE_BEND_TOO_TIGHT (a tube path bends tighter than the tube radius) and SURFACE_PATCH_WIDE_MASK (a mask almost as wide as its curved host). compare_reference(image_path, view, code=... | document_path=..., height_mm=None, levels=24, tolerance_mm=1.5, align='bbox'|'origin', source='draft'|'native') compares the model silhouette with a reference image; source='native' with document_path uses the tessellation of the bodies as built instead of the draft: iou, reference_size_mm, model_size_mm, delta_size_mm, worst (levels with the largest deviation: y_mm, reference and model [left, right] extents in mm, delta_left, delta_right, delta_width, or missing), profile (all levels) and an overlay image (red: model outside the reference, blue: reference not covered). height_mm is the real height of the reference silhouette; without it both are scaled to the same height and only proportions are compared. A level passes when the model matches within tolerance_mm at a height within tolerance_mm of the level (model_y_mm reports the height used), so a horizontal edge that is slightly off does not fail the level it crosses. A warning REFERENCE_MATCHES_OPPOSITE_VIEW means the image shows the other side (use left instead of right). With analyze(document_path, patch) the draft shows the effect of a patch before the edit.",
    "lifecycle": "mode='revert' (document_path, expected_revision) restores the saved state before the last edit in seconds, up to three edits back; states are kept in .solidworks-mcp-history beside the part. analyze is COM-free. create needs a NEW absolute output_path .SLDPRT. edit needs document_path (an open part is activated, a closed one opened) and a fresh expected_revision, plus one of: patch=[{old,new}] exact replacements in the embedded program (preferred: small input, nothing else can change; each old must occur exactly once, else PATCH_NOT_FOUND/PATCH_NOT_UNIQUE before any mutation), code with a complete revised program, or only parameters. A patch or parameter-only edit keeps current native parameter values. analyze with document_path and patch/code returns plan {would_rebuild, would_add, would_remove} without mutation. Answers are compact: revision, parameters, bodies, rebuilt/added/removed components, preserved_count, warnings, timings_ms, rebuild_mode, request_receipt; verbose=true returns the full legacy form. A failed edit restores the saved part and its revision. rebuild_mode forced_twice means SolidWorks left equation curves stale after the first forced rebuild and a second one fixed it; NATIVE_REBUILD_STALE means it did not and the part was restored. request_receipt.replayed=true: this identical request ran before and was answered from its receipt. A repeat runs again (request_receipt.attempt) when nothing would be duplicated: a create that left no file, or the same edit after revert. An identical edit that failed is not run again (message starts with Not executed again): change the program or pass a new idempotency_key. The revision of a saved part does not depend on the spelling of its path.",
    "edit": "Definition edits update existing curves/equations. Topology edits rebuild only affected components and dependent Boolean/Mirror nodes, removing owned obsolete features. Independent IDs survive. New parameters can be added; retain old declarations and units. Unmanaged feature changes are rejected for topology edits. get_model_context reports conditional edit compatibility by default; check_edit_compatibility=True explicitly checks the feature inventory without changing ownership. document_editable means an embedded program is available, not guaranteed edit success. Compiler v2 files retain same-topology editing; v3 ownership metadata is required for reconciliation.",
    "tube_path": "C1 piecewise cubic Hermite interpolation through supplied points; the tangent at an interior point is half the chord between its neighbours, at an end point it is the chord of the end segment; no mesh rings. The path must bend more gently than the tube is thick (warning TUBE_BEND_TOO_TIGHT): space the points evenly and start a tube that seats on a flat face with two points in line with the face normal. variable_tube lofts one circular station per point along the same path.",
    "placement": "Construct in local coordinates, then model.place(name, component_or_list, frame=model.frame(origin=..., rotation=...)). Rotations about world X, Y, Z are applied in that order around (0,0,0), followed by translation. Radians may be ratio parameter expressions. Active rotations require 0 < abs(angle) < 2*pi; active translations cannot start at zero. Keep each active axis nonzero with the same sign for direct native parameter cycling; source edits that change active axes/signs rebuild placement and dependents. An identity frame is useful for points but place rejects a no-op. The compiler creates one fixed axes sketch and at most three equation-driven rotation features plus one translation; no per-point helper geometry. Selected solids move together and are consumed. frame.point/direction can be used without place. Same-topology edits retain IDs. Use resulting components for later operations; shape-family anchors follow single-seed placements, not Boolean faces. SW2026 edge-angle updates may require a second native rebuild after reopening. The compiler detects dimension/feature lag, performs at most one extra native pass and verifies it. Read-only observation/inspection/export reject NATIVE_REBUILD_PENDING; context reports pending values. Standalone angle edits may need a second Ctrl+Q, without Python regeneration.",
    "edges": "fillet(name, component, radius, edges=...) and chamfer(name, component, distance, edges=...) are native features owned by the program: they consume the component, rebuild when geometry upstream changes, and their size is an expression of the parameters. Edge selectors never use face numbers: 'all' (every edge of the body); {'planar_loop': {'axis': 'X'|'Y'|'Z', 'side': 'min'|'max'}} (outer contour of the planar face that is extreme along the axis); {'between': [A, B]} (edges where faces that came from component A meet faces from component B after a combine). A size the kernel cannot build fails explicitly and the edit rolls back; reduce the size or narrow the edge set. Changing the selector rebuilds the feature; changing only the size is a definition update.",
    "dependencies": "Boolean inputs become consumed: use the resulting component for later operations. Mirror copies its input. Source geometry edits update dependents; changed operation/seeds/count triggers native dependency reconciliation. The response lists preserved, rebuilt, added and removed components.",
    "face_anchors": "face_anchor(host, origin=..., direction=...) is a lazy attachment descriptor: it creates no standalone construction model and is not a numeric point or Frame. anchored_extrude consumes that descriptor to build a useful separate solid using a native ray, axis, face-intersection point and perpendicular plane. Host must own one body; the first positive hit must identify one trimmed face, away from seams/tangencies. Ambiguous or unsupported native intersections fail explicitly. Origin expressions and profile/depth dimensions follow native parameters after save/reopen without Python regeneration. Direction is constant; source edits changing it or the depth sign rebuild the dependent component. The sketch plane origin follows the intersection; its X/Y axes are native and reported in attachment_frame. Positive depth follows the ray, negative depth reverses it. This is a ray-normal frame, not a tangent plane or a conformal contact surface; use surface_patch for fitted skin/gaps. Circle uses a native circle and diameter equation; ellipse/rounded_rectangle use native equation curves with kernel approximation and no claimed continuous error bound. Live hosts receive first-hit validation after edits and before inspect/observe/export. After downstream Boolean consumption the historical host is unavailable as a final body: native reference types, parent links, plane constraints and point/profile agreement are checked, not a fresh ray through the consumed host. External broken links are rejected even when positions still coincide. Unsupported: parameter-driven ray directions, tangent frames and passing the descriptor to arbitrary point/placement constructors.",
    "ellipsoid": "Default: native ellipse Revolve + scoped constant Z aspect + placement, preserving the exact-target legacy recipe. rz/rx must simplify to a constant. For independently driven Rx/Ry/Rz, use approximate=True: a deformable native ellipse constrained to four meridian guides, apex Sweep, Top Mirror and union. Saved native equations rebuild without Python, including X/Z radius order reversal. This is an approximation: exact declared extents do not certify the surface. No universal or continuous error bound is claimed; check actual faces for fit-sensitive work. surface_point rejects this variant; surface_patch follows its actual B-rep. Component metadata explicitly reports GUIDED_APPROXIMATION.",
    "surface_example": (
        "# Recess and insert with a uniform clearance on a curved host. Create it as written.\n"
        "H = model.parameter('H', 100)\n"
        "Clr = model.parameter('Clr', 0.25)\n"
        "u = H / 100\n"
        "host = model.sectioned_solid('Body', [(0, 20*u, 20*u), (40*u, 20*u, 20*u)])\n"
        "# The mask is a solid that reaches through ONE side of the host (+Z here) and stops inside it,\n"
        "# so it clips exactly one connected patch. inward=True thickens from the surface into the host.\n"
        "pocket_mask = model.rounded_prism('PocketMask', 16*u, 10*u, 30*u + Clr, 3*u,\n"
        "                                  center=(0, 20*u, (40*u - Clr) / 2))\n"
        "pocket = model.surface_patch('Pocket', host, pocket_mask, 4*u + Clr, inward=True)\n"
        "# The insert uses the SAME host surface: one clearance smaller on every side and one clearance\n"
        "# shallower, so the gap is uniform on the walls and the floor. Build both patches before the\n"
        "# host is consumed by the difference.\n"
        "insert_mask = model.rounded_prism('InsertMask', 16*u - 2*Clr, 10*u - 2*Clr, 30*u, 3*u - Clr,\n"
        "                                  center=(0, 20*u, 20*u))\n"
        "insert = model.surface_patch('Insert', host, insert_mask, 4*u, inward=True)\n"
        "model.combine('Carved', [host, pocket], operation='difference')\n"
        "# Check: inspect_model(body_pairs=[{'id':'fit','a':{'body':'Carved'},'b':{'body':'Insert'},\n"
        "#        'min_clearance_mm':.20,'max_clearance_mm':.30}])\n"),
    "surface": "Worked example: topic surface_example. offset > 0 moves the patch outward from the host surface, offset < 0 inward; thickness then grows outward, or into the host with inward=True. For an emboss use an outward patch that overlaps the host slightly (negative offset) and combine union. surface_patch follows actual curved B-rep faces on sectioned_solid/revolve/dome/ellipsoid, including native edits. Host must be a direct, unconsumed sectioned_solid/revolve/dome/ellipsoid (not place/combine). Host and mask must each own one body; clip exactly one connected patch. Use a rounded_prism or composed solid as the mask, reaching through the desired side of the host. Planar faces are excluded. Offset and thickness are normal distances, not a Z translation. For a recess use an inward patch and combine difference; for emboss use overlapping offset and combine union. Complex offsets can fail in the native kernel and roll back. surface_point is an analytic anchor for declared round/elliptical families and their placements; it is not a general face query.",
    "observation": "observe_model(directory, document_path, expected_revision, components=None, views=None, width=1000, sections=None, flat=False, overwrite=False). views mixes standard names (front, back, left, right, top, bottom, isometric) with custom cameras {name, direction:[x,y,z], up:[x,y,z]}: direction is the look direction from the eye into the model (front is [0,0,-1], right is [-1,0,0], a three-quarter view [0.574,-0.14,-0.819]); up defaults to [0,1,0]. flat=true writes <directory>/<view>.png instead of a random subfolder and refuses existing files unless overwrite=true. Use fresh context identity. Native images share one camera scale, preserve camera settings, and carry revision/configuration, captured/delivered image hashes and dimensions. Components frame a local region with surrounding model context, not isolated bodies. Facts include approximate native bbox, mass properties and surface types. Optional sections [{component,center:[x,y,z],normal:[x,y,z],samples:16,expected_radius:5,tolerance_mm:.01}] use body-scoped native rays; center must be inside. Radius check is sampled evidence, not continuous circularity or resemblance acceptance. A Sweep may contain B-spline surfaces even for a circular section.",
    "reference_measurements": "measure_reference(image_path, queries, projection='unknown', calibration=None, annotation_directory=None). COM-free visible raster extents in explicit pixel regions. Query {id,kind,region:[left,top,right,bottom],y?,visibility?,expected_mm?,tolerance_mm?,color?,color_tolerance?}; color=[r,g,b] measures only the pixels of that colour (an eye, the teeth) instead of everything off the background, ignoring lines thinner than 3 px; kinds bbox/center/aspect_ratio/width_at_height/depth_range, y required for row measurements. Right/bottom exclusive. Metric scale {pixel_distance,length_mm,origin_px?} requires declared orthographic view. With expected_mm (width/height/center_x/center_y from the program) and tolerance_mm each measurement returns check {measured_mm, delta_mm, passed}; passed is true/false, or null when occluded, uncalibrated, clipped by the region or without tolerance. Before delivery: list 5-10 identifying elements, measure each on every approved view in one batch, compare with the program sizes and report the deviations; when one size differs between two views by more than the tolerance, report that to the user instead of averaging. Depth needs Right; hidden boundaries remain unknown. Not needed to start the first volume. Default simple uniform-background segmentation does not identify semantic parts; approximate intervals describe raster sensitivity, not manufacturing tolerance.",
    "execution": "CAD tools run serially on one STA owner. One call waits at most SOLIDWORKS_MCP_SYNC_WAIT_SEC (default 45 s); execution_timeout_sec can only shorten that wait and never bounds COM execution. A longer operation keeps running and the call returns success with status RUNNING, operation_id, elapsed_sec and stage: call wait_operation(operation_id) until it returns the original tool result, views included. This is not an error: do not retry, and reconcile_session is not needed; continue with the revision from that result. While an operation runs, other CAD tools answer CAD_SESSION_BUSY with its operation_id; COM-free tools (wait_operation, get_executor_status, get_request_status, model_api_lookup, measure_reference, analyze, compare_reference with the draft source) keep working. Waiting or client cancellation never cancels or alters the operation. Only a really unknown outcome quarantines CAD until reconcile_session(operation_id): the owning MCP process exited (get_executor_status reports owner_alive=false), a native client was killed, a UI watchdog abandoned the call, or a failed mutation could not confirm its rollback. An orphaned/killed native client additionally needs a verified SolidWorks restart. No automatic retry or delivery acceptance. Public MCP shape mutations derive durable keys automatically; optional idempotency_key binds tool/document/starting revision/payload. Identical retries return historical results only when the saved file, native clean state, configuration and model identity still match; a create that left no file and a completed request whose document is back at its starting state (after revert) run again instead, and an identical edit that failed answers Not executed again. Conflicts and unknown receipts never execute. get_executor_status also reports server.version, code_changed_since_start (reconnect the MCP server) and SolidWorks memory; after a SOLIDWORKS_MEMORY_HIGH warning save and run expert_tools restart_solidworks. get_request_status is COM-free. Explicit keys on legacy mutations and plan_id require document_path/expected_revision. Raw ui_guard runs a UI-only isolated monitor on the same STA operation; automatic Modify recovery requires an active tracked com_get call, exact operation identity and expected dialog text.",
    "delivery": "export_model(document_path, expected_revision, directory, expected_bodies=None, deviation_mm=.02, mesh_cleanup=none|bounded, diagnostics_directory=None, replace=False). New directory, or replace=true to republish atomically over a directory that holds only an earlier export_model delivery (any other existing directory is refused with OUTPUT_EXISTS). Per-body files are stl/<BodyName>.stl: stable across rebuilds and body reordering. All visible solids, saved clean source. Checked native bytes, kernel/extrema, STEP units/body counts, closed oriented STL topology and volume. Strict default mesh_cleanup=none preserves raw tessellation. Opt-in bounded cleanup welds sub-resolution seams (maximum displacement .00006 mm), removes at most ten collapsed facets and flips at most 64 inconsistent facets, with tighter count-relative limits. It refuses open/nonmanifold edges, duplicate facets and multiple shells; valid raw hollow meshes pass unchanged. The unchanged structural validator checks the written float32 STL. Raw STL, hashes and every changed facet are retained in the delivery audit. Optional diagnostics_directory preserves rejected raw STL separately; no partial delivery. Native geometry is never repaired. Likeness, native parameter cycles, clearances, self-intersections and print-process readiness remain separate checks.",
    "inspection": "inspect_model(document_path, expected_revision, components=None, sections=None, precision=summary|delivery, body_pairs=None, printability=None). printability=true or {min_wall_mm: 1.2, up: 'Y'} measures the built bodies: support {stable, margin_mm (distance of the centre of mass from the footprint edge, negative outside), tip_angle_deg, contact_area_mm2, resting_bodies} for the assembled model standing on its lowest flat faces, and walls [{body, min_thickness_mm, at_mm, thin_area_mm2, thin_spots: [{at_mm, thickness_mm, area_mm2}], sharp_edge_area_mm2, passed}]. A wall is where the far face is within 35 degrees of parallel; acute edges are only counted in sharp_edge_area_mm2. passed=false needs a thin place two walls wide, or a smaller one under half the wall; passed=null marks lesser thin places such as the feathered rim of a hole in a sloping face: read thin_spots. No images or CAD entities; delivery precision adds exact extrema and native kernel/feature health. body_pairs is an optional batch of up to eight queries; see body_pairs for exact references, limits and tri-state acceptance.",
    "body_pairs": "inspect_model(..., body_pairs=[{'id':'fit','a':{'component':'Socket'},'b':{'body':'Insert'},'min_clearance_mm':.20,'max_clearance_mm':.30}]). Each endpoint names exactly one existing body or an unconsumed single-body component; multiple-body components need an explicit body. Pairs are independent of the components framing filter. Measures global boundary_distance_mm and intersection_volume_mm3 on real native solids, without selecting entities or creating features. OVERLAP includes containment and coincident solids, even when surface distance is positive; clearance_mm is then zero. SEPARATED or CONTACT_OR_NEAR requires successful distance and Boolean evidence. CONTACT_OR_NEAR uses a reported 1e-5 mm numeric threshold, not a manufacturing tolerance or proof of bonded support. Any unknown measurement preserves null fields and structured errors; clearance_check.passed is true/false/null. Top-level tool success means the inspection returned, not that every criterion passed. Optional min/max bounds test the global minimum clearance, not maximum local separation or uniform mating fit. Any positive intersection volume fails a clearance criterion, even when clearance_mm is zero and the numeric bounds include zero. For intentional overlaps, omit clearance bounds and inspect relation and intersection_volume_mm3. Operations2 uses only temporary copies, checks kernel/error/volume, and disambiguates empty near-contact intersections with a cut. No mesh fallback, measurement rounding or geometry repair. Thin walls, self-intersections, local clearances and print readiness are separate checks.",
    "working_view": "Shape construction frames each completed component automatically; create finishes with the whole model, edit frames changed live bodies. Camera failures are warnings, not geometry rollback. observe_model restores this working camera after capture. Expert/raw edits use zoom_to before a local edit and after an extent change; sketch tools already fit automatically.",
    "workflow": "Recommended order; every step is one tool call. (1) get_model_context. (2) model_api_lookup('all'). (3) Decide volume families from the references: a Front/Right pair explains volume better than many local measurements. (4) Write the whole program with a few named parameters and draft it: execute_model_code(mode='analyze', code=..., draft_views=['front','right']) costs milliseconds and returns predicted extents per body, silhouette images and warnings about a tube that bends too tightly or a surface patch mask that is too wide; nothing is built. (5) With reference images run compare_reference(image_path, view, code=..., height_mm=<real height>) for each orthographic view: it returns IoU and a level table of left/right extents with deviations in mm, worst first, and an overlay image. Fix reliable proportion mismatches. If a mismatch follows a documented draft limitation (estimated bodies or unsupported cuts), record it and verify it natively after building. Do not loosen tolerance to pass the draft; keep the agreed tolerance for the final native check. (6) Build once: mode='create' with a new absolute output_path (minutes; a RUNNING answer is collected with wait_operation). (7) Look at the returned native views and warnings; correct with mode='edit' and patch=[{old,new}], batching related changes because every edit costs a full native rebuild; analyze with document_path and patch previews the rebuild plan and the new draft. mode='revert' restores the state before the last edit. (8) Check fits numerically: inspect_model(body_pairs=[...]) for clearances, precision='delivery' for exact extents and kernel health, printability=true for support (stable, margin_mm, tip_angle_deg) and the thinnest wall per body. (9) Before delivery repeat compare_reference(document_path=..., source='native') per approved view and report the table (element or level, reference, model, deviation); report a size that differs between two views instead of averaging it. (10) export_model to a new directory (replace=true to republish). Do not create probe parts to learn extents or proportions: the draft answers that. Do not judge likeness from screenshots alone.",
    "pitfalls": "Known behaviour that costs a probe part when unknown. (1) sectioned_solid with three or more sections is ONE smooth loft: it bulges between sections; use spans='linear' for ruled spans, or revolve for round straight segments. (2) A consumed component (input of combine, place, fillet, chamfer, mask of surface_patch) has no body: inspect_model and observe_model address the resulting component; get_model_context(components=[name]) shows consumed_by. (3) Faces: a two-point tube is an analytic cylinder; a tube through three or more points, a guided ellipsoid and equation-curve profiles produce B-spline faces; rounded_prism corners smaller than the full round are B-spline strips, the full round (slot) is cylindrical. (4) place rotates about world X, then Y, then Z around the origin, then translates; a primitive that only needs another direction should use axis= instead (rounded_prism, revolve, dome, sectioned_solid). (5) The standard Right view looks from +X toward -X; Front looks from +Z; Left from -X. (6) get_model_context reports exact extents (bbox_method IBody2.GetExtremePoint); observe_model facts use the approximate native box, which can exceed the body by up to about 1 mm. (7) Every edit costs a full native rebuild (about 10-20 s on a 270-feature part) plus checks: batch related changes into one patch. (8) An operation longer than one call returns status RUNNING: collect it with wait_operation, do not retry. (9) Driven placements cannot start at zero: use a literal 0 for a fixed axis. (10) SolidWorks cannot open two documents with the same file name: give probe parts distinct names. (11) SolidWorks picks loft connectors on screen: the compiler turns and frames the camera while it selects the sections of sectioned_solid and variable_tube, then restores it. (12) Repeat a body with model.pattern (one native feature) instead of a loop of placed copies; for a peg and socket, flush insert, hinge, collar or hollow cup start from model_api_lookup('templates'). (13) A tube path must bend more gently than the tube is thick: tangents at interior points are half the chord between their neighbours, so a short segment next to a long one turns sharply. analyze, create and edit warn with TUBE_BEND_TOO_TIGHT (min_bend_radius_mm, tube_radius_mm, between_points); such a sweep may build and then fail when a parameter scales the model. Start a tube that must seat on a flat face with two points in line with the face normal, well apart. (14) surface_patch with a mask almost as wide as its curved host leaves slivers where the mask walls leave the surface at a shallow angle, and pocket and insert both end at the back plane of the mask: keep the mask under about 0.7 of the host radius, or build a wrapped inlay as a ring intersected with a sector (template curved_inlay). (15) The draft subtracts a plain box that covers one whole side of its target and clips intersections to a box mask exactly; other cuts, patches and masks stay listed in estimated. A straight cone that the native silhouette shows as an hourglass is a twisted loft: report it. (16) A create that failed leaves no file: the same call can be repeated as it is. An identical edit that failed is answered from its receipt (message starts with Not executed again); change the program or pass a new idempotency_key to run it once more. After mode=revert the same edit runs again.",
    "examples": {
        "parameters": "H = model.parameter('H', 100)\nClr = model.parameter('Clr', 0.25)\nu = H / 100\nmodel.dome('Cap', radius=.2*H, height=.1*H, center=(0, .5*H - Clr, 0))",
        "sectioned_solid": "model.sectioned_solid('Hull', [(0, 12, 9), (30, 16, 11), (40, 8, 6)], spans='linear')",
        "revolve": "model.revolve('Knob', [(0, 6), (4, 6), (6, 3), (14, 3)], axis='Z', origin=(0, 20, 10))",
        "dome": "model.dome('Cap', radius=12, height=8, center=(0, 40, 0))",
        "ellipsoid": "model.ellipsoid('Eye', (5, 4, 4), center=(6, 50, 9))",
        "tube": "model.tube('Pin', [(0, 0, 0), (0, 0, 20)], 5)\nmodel.tube('Hose', [(10, 40, 0), (16, 30, 2), (15, 18, 0)], 2.5)",
        "variable_tube": "model.variable_tube('Horn', [(0, 60, 0), (4, 70, 0), (10, 76, 0)], [3, 2, 1])",
        "rounded_prism": "model.rounded_prism('Panel', 30, 18, 4, 3, center=(0, 20, 8))\nmodel.rounded_prism('Slot', 28, 12, 10, 6, center=(0, 0, 5))",
        "prism": "model.prism('Bracket', [(0, 0), (30, 0), (30, 6), (6, 6), (6, 24), (0, 24)], 10)",
        "place": "seed = model.dome('CupSeed', radius=5, height=5)\nmodel.place('Cup', [seed], frame=model.frame(origin=(16, 80, 0), rotation=(0, 0, -pi/2)))",
        "combine": "a = model.dome('A', 8, 6)\nb = model.tube('B', [(0, 2, -12), (0, 2, 12)], 2)\nmodel.combine('Bored', [a, b], operation='difference')",
        "mirror": "arm = model.tube('ArmL', [(12, 40, 0), (20, 20, 0)], 3)\nmodel.mirror('ArmR', [arm], plane='Right')",
        "pattern": "plate = model.rounded_prism('Plate', 60, 12, 4, 2, center=(0, 0, 2))\ntooth = model.prism('Tooth', [(-2, 0), (2, 0), (0, 5)], 4, center=(-24, 6, 2))\nrow = model.pattern('Row', [tooth], count=7, step=(8, 0, 0))\nmodel.combine('Rack', [plate, tooth, row])\nstud = model.tube('Stud', [(12, 0, 30), (12, 6, 30)], 2)\nmodel.pattern('Studs', [stud], count=6, axis='Y', center=(0, 0, 30))",
        "fillet": "plate = model.rounded_prism('Plate', 30, 20, 6, 2, center=(0, 0, 3))\nmodel.fillet('PlateRim', plate, 1.5, edges={'planar_loop': {'axis': 'Z', 'side': 'max'}})",
        "chamfer": "pin = model.tube('Stud', [(0, 0, 0), (0, 12, 0)], 3)\nmodel.chamfer('StudBevel', pin, 0.5)",
        "surface_patch": "see topic surface_example",
        "fit_with_clearance": "Clr = model.parameter('Clr', 0.25)\npeg = model.tube('Peg', [(0, 0, 0), (0, 10, 0)], 3)\nblock = model.rounded_prism('Block', 20, 8, 20, 2, center=(0, 4, 0), axis='Y')\nhole = model.tube('Hole', [(0, -1, 0), (0, 10 + Clr, 0)], 3 + Clr)\nmodel.combine('Socket', [block, hole], operation='difference')",
    },
    "unsupported": ["parameter_driven_face_anchor_direction", "native_tangent_anchor_frame", "face_anchor_as_numeric_point_or_arbitrary_placement", "exact_parameter_driven_ellipsoid_aspect", "parameter_removal_or_unit_change", "automatic_external_feature_rebase"],
    "example": (
        "H = model.parameter('H', 100)\n"
        "body = model.sectioned_solid('Housing', [(0, .18*H, .14*H), (.5*H, .24*H, .17*H)])\n"
        "pipe = model.tube('Pipe', [(0,.5*H,0), (0,.7*H,0), (.2*H,.85*H,.1*H)], .04*H)\n"
        "model.dome('Cap', radius=.24*H, height=.12*H, center=(0,.5*H,0))\n"),
}


API['progress'] = (
    'RUNNING answers, wait_operation and get_executor_status expose progress for the current phase: '
    'completed, total (null if unknown), unit, updated_at, last_advance_at, rate_per_sec, '
    'seconds_without_progress and phase_percent. Phase percentage is not overall export percentage. '
    'A heartbeat does not advance a counter. NO_PROGRESS_OBSERVED never cancels or retries COM. '
    'Expert get_session_metrics remains COM-free while CAD is busy. Executor reader diagnostics '
    'show the current and last tessellation COM method, index, elapsed time and HRESULT. '
    'Per-method timings and cache hits are retained in delivery manifest tessellation records.')
API['operation_history'] = (
    'wait_operation reads completed results after reconnect without CAD execution; '
    'result_history.from_journal marks historical evidence, not validation of current files. '
    'Status COMPLETED is separate from success and error_name. Results expire after seven days, '
    '128 ordinary completed results or 128 MiB. Each JSON payload and its image set is limited '
    'to 16 MiB. Unresolved operations and request receipts are never automatically evicted. '
    'Missing/changed assets, expired/corrupt results and unknown IDs have distinct diagnostics. '
    'An owner lost before completion still requires OUTCOME_UNKNOWN recovery.')
API['execution'] += ' Expert get_session_metrics is also COM-free; read progress and operation_history for details.'
API['pitfalls'] += (
    ' (17) A platform connecting separate supports is a useful first union input. A failed union '
    'can retry once after proving unchanged feature/body state and a connected temporary union. '
    'Read input_order, resolved_order and connectivity diagnostics. Bounding-box overlap or point '
    'contact alone does not prove one valid solid. Difference inputs are never reordered.')

HISTORY_DEPTH = 3     # Saved states kept per part for mode="revert".


def history_directory(path):
    return path.parent / '.solidworks-mcp-history' / path.stem


def history_entries(path):
    """Saved earlier states of a part, oldest first; only pairs this module wrote."""
    folder = history_directory(path)
    if not folder.is_dir():
        return []
    # Ordered by the sequence number in the name: clock resolution cannot order quick edits.
    return sorted((entry for entry in folder.glob('*.SLDPRT')
                   if entry.stem.split('-')[0].isdigit() and entry.with_suffix('.shape.json').is_file()),
                  key=lambda entry: int(entry.stem.split('-')[0]))


def keep_history(path, checkpoint, record, checksum):
    """Keep the state before a committed edit so it can be restored without another rebuild."""
    folder = history_directory(path)
    folder.mkdir(parents=True, exist_ok=True)
    existing = history_entries(path)
    sequence = int(existing[-1].stem.split('-')[0]) + 1 if existing else 1
    entry = folder / f'{sequence:08d}-{uuid.uuid4().hex[:8]}.SLDPRT'
    shutil.move(str(checkpoint), entry)
    atomic_json_write(str(entry.with_suffix('.shape.json')), {**record, 'native_file_sha256': checksum})
    for old in history_entries(path)[:-HISTORY_DEPTH]:
        for stale in (old, old.with_suffix('.shape.json')):
            try: stale.unlink()
            except OSError: pass
    return entry


def release_undo(doc):
    """Drop the native undo history after a committed mutation.

    Every feature created or deleted stays in SolidWorks' undo list; over a long
    session that is gigabytes of committed memory. The committed state is on
    disk and the previous one in the edit history, so undo data is not needed.
    """
    try:
        doc.ClearUndoList()
    except Exception:
        pass


def memory_warning(warnings):
    """Tell the caller before SolidWorks runs out of committed memory."""
    try:
        from .session_identity import solidworks_memory
        state = solidworks_memory()
        if state and state.get('warning'):
            warnings.append({'code': state['warning'], **{k: v for k, v in state.items() if k != 'warning'}})
    except Exception:
        pass


def body_health_warnings(automation, doc, native):
    """A body the kernel rejects cannot be exported: say so with the mutation, not at delivery."""
    try:
        from .model_inspection import kernel_faults
        with command_scope(automation.app):
            faulty = kernel_faults(automation, doc, native.get('ownership'))
    except Exception:
        return []
    return [{'code': 'NATIVE_BODY_INVALID', **item,
             'action': ('the kernel reports faults in this body: export_model will refuse it and fit checks '
                        'answer UNKNOWN. Change the named components (a loft or a Boolean with nearly '
                        'coincident faces is the usual cause) or revert')} for item in faulty]


def failure_causes(exc, program):
    """Program warnings of a failed native build, and those about the component that failed.

    Only the second list is a likely cause: a warning about another component must not
    send the caller to the wrong place.
    """
    predicted = program_warnings(program) if program else []
    details = getattr(exc, 'details', None) or {}
    failed = {details.get('component')} | {item.get('component') for item in details.get('features') or []
                                           if isinstance(item, dict)}
    causes = [w['component'] + ': ' + w['code'] + '; ' + w['action'] for w in predicted
              if w.get('component') in failed]
    return predicted, causes


def failure_trace(exc):
    """Tracebacks are for unexpected exceptions; validation errors explain themselves."""
    if os.environ.get('SOLIDWORKS_MCP_DEBUG') == '1' or not isinstance(exc, ProgramError):
        return {'traceback': traceback.format_exc()}
    return {}


def stored_recipes(record):
    """Saved components keep the native recipe they were built with."""
    recipes = {}
    for node in record['program']['source_graph']['nodes']:
        geometry = node['geometry']
        if node['kind'] == 'sectioned_solid':
            recipes[node['id']] = geometry.get('profile_representation', 'trig_halves')
        elif node['kind'] == 'tube' and len(geometry['points']) == 2:
            # Straight tubes saved before the cylinder recipe stay sweeps.
            recipes[node['id']] = geometry.get('recipe', 'tube_sweep_v1')
        elif node['kind'] in {'tube', 'variable_tube'}:
            # Interpolated paths saved before the chord rule keep the direction their curves were solved in.
            recipes[node['id']] = geometry.get('recipe', 'path_v1')
        elif node['kind'] in {'rounded_prism', 'prism'}:
            # Prisms saved as extrusions keep their native features.
            recipes[node['id']] = geometry.get('recipe', 'prism_extrude_v1')
    return recipes


def edited_program(record, code, patch, parameters, current_values):
    """Compile the requested revision of an embedded program.

    current_values is a callable: native parameter values are read only when needed.

    code replaces the program and takes its own parameter defaults. A patch or
    a parameter-only edit keeps the current native values, except where the
    patch itself changes a declared default.
    """
    source = record['source']
    if patch is not None:
        code = apply_patch(source, patch)
        defaults = {k: v['default'] for k, v in run_program(code)['parameters'].items()}
        stored = {k: v['default'] for k, v in record['program']['parameters'].items()}
        keep = {k: v for k, v in current_values().items() if k in defaults and defaults[k] == stored.get(k)}
        parameters = {**keep, **(parameters or {})}
    elif code is None:
        code = source
        parameters = {**current_values(), **(parameters or {})}
    program = run_program(code, parameters, recipe_versions=stored_recipes(record))
    return code, program, changed_line_count(source, code) if isinstance(source, str) else None


def edit_plan(reconcile):
    return {'would_rebuild': [n for n in reconcile['build'] if n not in reconcile['added']],
            'would_add': reconcile['added'], 'would_remove': reconcile['deleted'],
            'preserved_count': len(reconcile['preserve']),
            'added_parameters': reconcile['added_parameters'],
            'edit_strategy': ('dependency_reconcile' if reconcile['remove'] or reconcile['build'] or
                              reconcile['added_parameters'] else 'definition_update')}


class ShapeProgramOperations:
    def model_api_lookup(self, topic="index"):
        from ..shape_templates import INDEX, TEMPLATES, USAGE
        api = {**API, 'limits': limits()}
        template = str(topic).removeprefix('template:').removeprefix('template_')
        if topic == 'templates':
            return self._result(True, "Shape program templates", data={'templates': INDEX, 'usage': USAGE})
        if str(topic) != template and template in TEMPLATES:
            item = TEMPLATES[template]
            return self._result(True, "Shape program template", data={
                'template': template, 'purpose': item['purpose'], 'bodies': item['bodies'],
                'clearance_pairs': [list(pair) for pair in item['fits']], 'code': item['code']})
        if topic == 'index':
            data = {'topics': list(api) + ['templates', 'all'], 'constructors': API['constructors'],
                    'limits': api['limits'],
                    'coordinate_system': API['coordinate_system'], 'code': API['code'],
                    'hint': "topic='all' returns every topic in one call; read 'pitfalls' before the first model"}
        elif topic == "all":
            data = {**api, 'templates': {**INDEX, 'usage': "model_api_lookup('template:<name>') returns the code"}}
        elif topic in api:
            data = {topic: api[topic]}
        elif 'model.'+topic.removeprefix('model.') in API['constructors']:
            name = 'model.'+topic.removeprefix('model.')
            data = {name: API['constructors'][name]}
        else:
            import difflib
            known = (list(API) + ['all', 'templates'] + ['template:' + name for name in TEMPLATES] +
                     [name.removeprefix('model.') for name in API['constructors']])
            close = difflib.get_close_matches(str(topic).removeprefix('model.'), known, n=4, cutoff=.5)
            return self._error("UNKNOWN_API_TOPIC", f"Unknown topic {topic!r}",
                               details={'did_you_mean': close, 'topics': list(API) + ['templates', 'all'],
                                        'constructors': [n.removeprefix('model.') for n in API['constructors']]},
                               recommended_actions=[{'tool': 'model_api_lookup',
                                                     'arguments': {'topic': close[0] if close else 'index'},
                                                     'why': 'Closest known topic' if close else 'Lists every topic'}])
        return self._result(True, "Shape program API", data=data)

    def get_model_context(self, include_source=False, check_edit_compatibility=False):
        if not self.is_connected:
            result = self.connect()
            if not result.get("success"):
                return result
        with command_scope(self.app):
            return self._read_model_context(include_source, check_edit_compatibility)

    def _read_model_context(self, include_source, check_edit_compatibility=False):
        doc = self.app.ActiveDoc
        from .. import __version__
        data = {"mcp_version": __version__, "shape_program_version": 3,
                "coordinate_system": API["coordinate_system"],
                "native_parameter_bindings": True, "in_place_edit": True,
                "workflow": "reconstruct_shape", "document": None}
        from ..build_info import server_state
        from .session_identity import solidworks_memory
        data['server'] = server_state()
        data['memory'] = solidworks_memory()
        data['entry_points'] = ['execute_model_code','observe_model','inspect_model',
                                'model_api_lookup','measure_reference','export_model','expert_tools']
        data['constructors'] = [name.removeprefix('model.') for name in API['constructors']]
        data['defaults'] = {'observations': 'automatic after create/edit', 'checkpoint': 'automatic for edit',
                            'request_identity': 'automatic for shape mutations',
                            'reference_measurements': 'optional', 'delivery': 'explicit export_model'}
        if doc is not None:
            data["document"] = {"title": self._get_doc_title(doc),
                                "path": self._get_doc_path(doc)}
            data['document']['dirty'] = bool(com_get(doc,'GetSaveFlag'))
            data['document']['type'] = int(com_get(doc,'GetType'))
            data["body_state"] = self._shape_body_state()
            record = read_record(doc) if str(self._get_doc_path(doc)).lower().endswith('.sldprt') else None
            # Backward-compatible capability flag, not a compatibility certificate.
            data['document_editable'] = record is not None
            data['document_editable_meaning'] = 'embedded_program_available'
            if record:
                if record['native'].get('placement_features'):
                    from .shape_placement import pending_placements
                    pending=pending_placements(doc,record['native']['placement_features'])
                    data['native_rebuild_state']={'status':'NATIVE_REBUILD_PENDING' if pending else 'READY','pending':pending}
                data.update(revision=observed_revision(doc,record),
                            parameters=global_values(doc,record),
                            components=self._public_shape_native(record['native'])['components'],model_id=record['model_id'],
                            document_edit_scope='dependency_reconcile' if record['native'].get('ownership') else 'same_topology')
                from .shape_edit_compatibility import edit_compatibility
                data['edit_compatibility'] = edit_compatibility(self, doc, record, check_edit_compatibility)
                if include_source:
                    data['source']=record['source']
                    data['source_parameters']=record['program']['parameters']
            elif str(self._get_doc_path(doc)).lower().endswith('.sldprt'):
                from .shape_observation import document_revision
                data['revision'] = document_revision(doc)
        return self._result(True, "Shape modeling context", data=data)

    def measure_reference(self, image_path, queries, projection='unknown', calibration=None,
                          background=None, threshold=32, annotation_directory=None):
        from ..reference_measure import measure_reference
        try:
            data = measure_reference(image_path, queries, projection, calibration,
                                     background, threshold, annotation_directory)
            return self._result(True, 'Read-only reference measurements', data=data)
        except Exception as exc:
            return self._error(getattr(exc, 'code', 'REFERENCE_MEASUREMENT_FAILED'), str(exc))

    def _shape_body_state(self):
        """Body list with exact extents; GetBodyBox alone overstates them."""
        state = dict(self.list_bodies(include_hidden=True, unit="mm").get("data", {}))
        items = state.get('bodies') or []
        try:
            if not items or len(items) > 64:
                raise ValueError('Exact extents are read for 1-64 bodies')
            from .model_inspection import exact_bbox
            doc = self.app.ActiveDoc
            by_name = {str(com_get(b, 'Name')): b for b in self._get_solid_bodies(doc, True)}
            exact = {item['name']: exact_bbox(by_name[item['name']]) for item in items}
            state['bodies'] = [{**item, 'bbox': {k: [round(v, 4) for v in exact[item['name']][k]]
                                                 for k in ('min', 'max')}} for item in items]
            state['bbox_method'] = 'IBody2.GetExtremePoint'
        except Exception:
            # Never present an approximate box with more digits than it deserves.
            state['bodies'] = [{**item, 'bbox': ({k: [round(v, 1) for v in item['bbox'][k]] for k in ('min', 'max')}
                                                 if item.get('bbox') else None)} for item in items]
            state['bbox_method'] = 'approximate_GetBodyBox'
        return state

    def observe_model(self, directory=None, document_path=None, expected_revision=None,
                      components=None, views=None, width=1000, sections=None, flat=False, overwrite=False):
        from .shape_observation import observe
        try:
            if directory is None:
                if not document_path or not Path(document_path).is_absolute():
                    raise ProgramError('OBSERVATION_TARGET_REQUIRED','Use absolute document_path')
                directory = str(Path(document_path).parent/'.solidworks-mcp-observations')
            data = observe(self, directory, document_path, expected_revision, components, views, width,
                           sections, flat, overwrite)
            return self._result(True, 'Revision-checked native model observation', data=data)
        except Exception as exc:
            return self._error(getattr(exc, 'code', 'OBSERVATION_FAILED'), str(exc),
                               details=getattr(exc, 'details', {}))

    def inspect_model(self, document_path, expected_revision, components=None, sections=None, precision='summary',
                      body_pairs=None, printability=None):
        from .model_inspection import inspect_model
        try:
            return self._result(True,'Native model inspection',data=inspect_model(
                self,document_path,expected_revision,components,sections,precision,body_pairs,printability))
        except Exception as exc:
            return self._error(getattr(exc,'code','INSPECTION_FAILED'),str(exc),details=getattr(exc,'details',{}))

    def export_model(self, document_path, expected_revision, directory, expected_bodies=None, deviation_mm=.02,
                     mesh_cleanup='none', diagnostics_directory=None, replace=False):
        from .model_delivery import export_model
        try:
            return self._result(True,'Validated files published',data=export_model(
                self,document_path,expected_revision,directory,expected_bodies,deviation_mm,mesh_cleanup,
                diagnostics_directory,replace))
        except Exception as exc:
            details = getattr(exc,'details',{})
            return self._error(getattr(exc,'code','EXPORT_FAILED'),str(exc),details=details,
                               stage=details.get('stage','export'),
                               com_hresult=details.get('com_hresult',getattr(exc,'hresult',None)))

    def _draft(self, program, views=None, components=None, directory=None):
        """COM-free predicted extents and optional silhouette images of a compiled program."""
        from ..shape_draft import (VIEWS, draft_components, draft_extents, component_extents,
                                   render_view)
        if views is not None and (not isinstance(views, list) or not 1 <= len(views) <= 6 or
                                  len(set(views)) != len(views) or any(v not in VIEWS for v in views)):
            raise ProgramError('INVALID_VIEWS', 'draft_views is a list of unique views: ' + ', '.join(VIEWS))
        try:
            drafted = draft_components(program)
            draft = draft_extents(program, drafted)
        except ProgramError:
            raise
        except Exception as exc:
            if views or components:
                raise
            # Extents are advisory: a draft defect must never fail a valid program.
            return {'unavailable': f'{type(exc).__name__}: {exc}'}, []
        if components:
            draft['components'] = component_extents(program, components, drafted)
        images = []
        if views:
            if directory is None:
                from .runtime import default_state_dir
                directory = default_state_dir() / 'drafts' / uuid.uuid4().hex
            elif not Path(directory).is_absolute():
                raise ProgramError('INVALID_PATH', 'draft_directory must be absolute')
            images = [render_view(drafted, view, Path(directory) / f'draft_{view}.png') for view in views]
        return draft, images

    def compare_reference(self, image_path, view='front', code=None, parameters=None, document_path=None,
                          height_mm=None, levels=24, tolerance_mm=1.5, align='bbox', background=None,
                          threshold=32, directory=None, source='draft'):
        """Compare the silhouette of a program or a saved part with a reference image.

        source='draft' predicts the silhouette from the program without COM;
        source='native' tessellates the bodies of the open saved part.
        """
        from ..reference_compare import compare_reference
        from ..shape_draft import draft_components
        try:
            if source not in {'draft', 'native'}:
                raise ProgramError('INVALID_ARGUMENT', "source is 'draft' or 'native'")
            if (code is None) == (document_path is None):
                raise ProgramError('INVALID_ARGUMENT', 'Pass either code or document_path')
            if source == 'native':
                return self._compare_native(image_path, view, document_path, parameters, height_mm, levels,
                                            tolerance_mm, align, background, threshold, directory)
            if code is not None:
                program = run_program(code, parameters)
            else:
                record = self._sidecar_record(Path(document_path))
                current = {k: v['value'] for k, v in record['program']['parameters'].items()}
                _, program, _ = edited_program(record, None, None, parameters, lambda: current)
                if directory is None:
                    directory = str(Path(document_path).parent / (Path(document_path).stem + '_views'))
            if directory is None:
                from .runtime import default_state_dir
                directory = str(default_state_dir() / 'compare')
            data = compare_reference(image_path, draft_components(program), view, height_mm, levels,
                                     tolerance_mm, align, background, threshold, directory)
            data['parameters'] = {k: v['value'] for k, v in program['parameters'].items()}
            return self._result(True, 'Draft silhouette compared with the reference', data=data)
        except Exception as exc:
            return self._error(getattr(exc, 'code', 'REFERENCE_COMPARISON_FAILED'), str(exc),
                               details={**getattr(exc, 'details', {}), **failure_trace(exc)})

    def _compare_native(self, image_path, view, document_path, parameters, height_mm, levels, tolerance_mm,
                        align, background, threshold, directory):
        """Silhouette of the bodies as built: cuts, patches and anchored features included."""
        from ..mesh_checks import MeshSilhouette
        from ..reference_compare import compare_reference
        from .native_mesh import document_meshes
        from .shape_observation import activate_target, canonical_path, document_revision
        import numpy as np
        if document_path is None or parameters:
            raise ProgramError('INVALID_ARGUMENT',
                "source='native' compares the saved part at document_path as built; parameters are not applied")
        if not Path(document_path).is_absolute():
            raise ProgramError('INVALID_PATH', 'document_path must be absolute')
        activated = activate_target(self, document_path)
        doc, error = self.get_active_doc()
        if error or canonical_path(self._get_doc_path(doc)) != canonical_path(document_path):
            raise ProgramError('DOCUMENT_MISMATCH', 'The requested part could not be activated')
        with command_scope(self.app):
            meshes = document_meshes(self, doc)
        if directory is None:
            directory = str(Path(document_path).parent / (Path(document_path).stem + '_views'))
        data = compare_reference(image_path, MeshSilhouette(np.vstack(list(meshes.values()))), view, height_mm,
                                 levels, tolerance_mm, align, background, threshold, directory)
        record = read_record(doc)
        data.update(document_path=document_path, revision=document_revision(doc, record),
                    bodies=list(meshes), triangles=int(sum(len(m) for m in meshes.values())),
                    **({'activated': activated} if activated else {}))
        return self._result(True, 'Native silhouette compared with the reference', data=data)

    @staticmethod
    def _sidecar_record(path):
        """The record written by the last commit, verified against the saved part; no COM."""
        if not path.is_absolute():
            raise ProgramError('INVALID_PATH', 'document_path must be absolute')
        sidecar = path.with_suffix('.shape.json')
        try:
            record = json.loads(sidecar.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ProgramError('PLAN_SOURCE_UNAVAILABLE',
                'No readable .shape.json beside the part; the plan is reported by the edit itself') from exc
        if not path.is_file() or record.get('native_file_sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ProgramError('PLAN_SOURCE_STALE',
                'The .shape.json sidecar does not describe the saved part; the plan is reported by the edit itself')
        return record

    def _plan_shape_edit(self, code, parameters, document_path, patch, draft_views=None,
                         draft_components=None, draft_directory=None):
        """COM-free preview of an edit from the sidecar written by the last commit."""
        path = Path(document_path)
        record = self._sidecar_record(path)
        current = {k: v['value'] for k, v in record['program']['parameters'].items()}
        _, program, changed = edited_program(record, code, patch, parameters, lambda: current)
        reconcile, _, _ = prepare_reconcile(record, program)
        draft, images = self._draft(program, draft_views, draft_components, draft_directory)
        return self._result(True, 'Edit plan computed without CAD mutation', data={
            'status': 'PROGRAM_VALIDATED', **program, **component_budget(program),
            'document': str(path), 'plan': edit_plan(reconcile),
            'changed_source_lines': changed, 'patch_applied': patch is not None,
            'draft': draft, 'images': images, 'warnings': program_warnings(program)})

    def execute_model_code(self, code=None, mode="analyze", output_path=None,
                           parameters=None, observe=True, document_path=None,
                           expected_revision=None, patch=None, draft_views=None,
                           draft_components=None, draft_directory=None):
        started = time.monotonic()
        committed_document = None
        program = None
        execution = {'native_mutation_started':False, 'source_preserved':None, 'phase':'preflight'}
        try:
            if patch is not None and code is not None:
                raise ProgramError('INVALID_ARGUMENT', 'Use either code or patch, not both')
            if patch is not None and not (mode == 'edit' or (mode == 'analyze' and document_path)):
                raise ProgramError('INVALID_ARGUMENT',
                    'patch edits the embedded program: use mode=edit, or analyze with document_path')
            if mode == 'revert':
                if code is not None or patch is not None or parameters or output_path:
                    raise ProgramError('INVALID_ARGUMENT', 'revert takes only document_path and expected_revision')
                return self._revert_shape_program(document_path, expected_revision, observe, execution)
            if mode == 'edit':
                if output_path:
                    raise ProgramError('INVALID_ARGUMENT','edit uses document_path, not output_path')
                return self._edit_shape_program(code,parameters,document_path,expected_revision,observe,
                                                execution,patch=patch)
            if mode not in {"analyze", "create"}:
                raise ProgramError("INVALID_MODE", "Use analyze, create, edit or revert")
            if mode != 'analyze' and (draft_views or draft_components or draft_directory):
                raise ProgramError('INVALID_ARGUMENT', 'draft_* options belong to mode=analyze')
            if mode == 'analyze' and document_path:
                return self._plan_shape_edit(code, parameters, document_path, patch, draft_views,
                                             draft_components, draft_directory)
            timer = PhaseTimer(self._runtime.record_progress) if mode == 'create' else None
            phase(timer, 'preflight')
            program = run_program(code, parameters)
            plan_recipes(program,{k:f'D{i+1}@ShapeParameters' for i,k in enumerate(program['parameters'])})
            if mode == "analyze":
                draft, images = self._draft(program, draft_views, draft_components, draft_directory)
                return self._result(True, "Shape program compiled without CAD mutation",
                                    data={"status": "PROGRAM_VALIDATED", **program, **component_budget(program),
                                          "draft": draft, "images": images,
                                          "warnings": program_warnings(program)})
            path = Path(output_path or "")
            if not path.is_absolute() or path.suffix.lower() != ".sldprt":
                raise ProgramError("INVALID_PATH", "create requires a new absolute .SLDPRT output_path")
            if path.exists() or path.with_suffix(".shape.json").exists():
                raise ProgramError("OUTPUT_EXISTS", "Choose a new output path; existing files are preserved")
            if not self.is_connected:
                connected = self.connect()
                if not connected.get("success"):
                    return connected
            blocked = self.ui_preflight("execute_model_code", {}, mutating=True)
            if blocked:
                return blocked
            execution.update(native_mutation_started=True, phase='create')
            phase(timer, 'native_create')
            made = self.create_new_part()
            if not made.get("success"):
                return made
            doc = self.app.ActiveDoc
            title = self._get_doc_title(doc)
            compiler = ParametricCompiler(self, doc)
            try:
                with command_scope(self.app), build_scope(doc):
                    native = compiler.compile(program, timer=timer)
                    native['document']=str(path)
                    native['program_hash']=program['program_hash']
                    record={'schema_version':2,'model_id':uuid.uuid4().hex,'revision':uuid.uuid4().hex,
                            'program':program,'native':native,'source':code}
                    phase(timer, 'record')
                    write_record(doc,record)
                if self._get_doc_title(self.app.ActiveDoc) != self._get_doc_title(doc):
                    raise ProgramError("DOCUMENT_CHANGED", "The active document changed during compilation")
                phase(timer, 'save')
                saved = self.save_document(str(path))
                if not saved.get("success"):
                    raise ProgramError("SAVE_FAILED", saved.get("message", "Cannot save native part"))
                if not path.is_file() or path.stat().st_size == 0:
                    raise ProgramError("SAVE_FAILED", "Native output file is missing")
                committed_document = str(path)
                release_undo(doc)
            except Exception:
                # The compiler owns only this newly-created document. Never
                # delete features in a pre-existing user's part during recovery.
                try:
                    self.app.CloseDoc(self._get_doc_title(doc) or title)
                except Exception:
                    pass
                raise
            native["document"] = str(path)
            native["program_hash"] = program["program_hash"]
            phase(timer, 'sidecar')
            atomic_json_write(str(path.with_suffix(".shape.json")), {
                **record,'native_file_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            frames = []
            warnings = program_warnings(program)
            warnings.extend(body_health_warnings(self, doc, native))
            memory_warning(warnings)
            phase(timer, 'working_view')
            working_view = focus_bodies(self, doc)
            if not working_view['verified']:
                warnings.append({'code': 'WORKING_VIEW_FAILED', 'details': working_view})
            phase(timer, 'observe')
            if observe:
                observation = self.observe_model(str(path.parent / (path.stem+"_views")),
                    document_path=str(path), expected_revision=observed_revision(doc, record),
                    views=observe if isinstance(observe, list) else None)
                frames = observation.get("data", {}).get("images", [])
                if not observation.get("success"):
                    warnings.append({"code": "OBSERVATION_FAILED", "result": observation})
            phase(timer, 'readback')
            body_state = {"data": self._shape_body_state()}
            revision = observed_revision(doc,record)
            return self._result(True, "Native shape program created",
                                data={**self._public_shape_native(native),
                                      'revision':revision, 'timings_ms': timer.result(),
                                      "native_mutation_started": True,
                                      "images": frames, "warnings": warnings,
                                      "working_view": working_view,
                                      "body_state": body_state.get("data", {}),
                                      "program_parameters": program["parameters"],
                                      "elapsed_sec": round(time.monotonic()-started, 3),
                                      "acceptance": "EXECUTED_NOT_DELIVERY_VALIDATED"})
        except Exception as exc:
            predicted, causes = failure_causes(exc, program if execution['native_mutation_started'] else None)
            return self._error(
                getattr(exc, "code", "SHAPE_PROGRAM_FAILED"), str(exc),
                stage=getattr(exc, "details", {}).get("stage", "shape_program"),
                likely_causes=causes,
                details={**getattr(exc, "details", {}),
                         **({'program_warnings': predicted} if predicted else {}),
                         "committed_document": committed_document,
                         "native_file_preserved": (execution['source_preserved'] if mode=='edit'
                                                   else committed_document is not None),
                         **execution,
                         **failure_trace(exc),
                         "elapsed_sec": round(time.monotonic()-started, 3)})

    @staticmethod
    def _public_shape_native(native):
        keys=('components','status','native_parameter_bindings','in_place_edit',
              'compiler_version','document','program_hash')
        result = {k:native[k] for k in keys if k in native}
        if 'components' in result:
            result['components'] = {name:{k:v for k,v in component.items() if k not in {'native_profile','native_anchor'}}
                                    for name,component in result['components'].items()}
        return result

    def _revert_shape_program(self, document_path, expected_revision, observe, execution):
        """Restore the saved state before the last committed edit; no native rebuild."""
        timer = PhaseTimer(self._runtime.record_progress)
        timer.start('preflight')
        path = Path(document_path or '')
        if not path.is_absolute() or not path.is_file():
            raise ProgramError('DOCUMENT_MISMATCH', 'Revert requires the absolute path of a saved part')
        from .shape_observation import activate_target
        activate_target(self, str(path))
        doc, error = self.get_active_doc()
        if error:
            return error
        record = read_record(doc)
        if not record:
            raise ProgramError('NATIVE_MAPPING_MISSING', 'This document has no embedded shape program')
        if not expected_revision or expected_revision != observed_revision(doc, record):
            raise ProgramError('STALE_REVISION', 'Refresh get_model_context before reverting')
        if bool(com_get(doc, 'GetSaveFlag')):
            raise ProgramError('DOCUMENT_DIRTY', 'Save or discard manual changes before reverting')
        entries = history_entries(path)
        if not entries:
            raise ProgramError('NO_HISTORY', 'No earlier state of this part is kept; history holds the last '
                               f'{HISTORY_DEPTH} edits made by this server')
        entry = entries[-1]
        previous = json.loads(entry.with_suffix('.shape.json').read_text(encoding='utf-8'))
        if hashlib.sha256(entry.read_bytes()).hexdigest() != previous.get('native_file_sha256'):
            raise ProgramError('HISTORY_CORRUPT', 'The kept state does not match its record; nothing was changed')
        blocked = self.ui_preflight('execute_model_code', {}, mutating=True)
        if blocked:
            return blocked
        timer.start('checkpoint')
        safety = Path(tempfile.gettempdir()) / 'SolidworksMCP' / ('shape-revert-' + uuid.uuid4().hex + '.SLDPRT')
        safety.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, safety)
        current_sidecar = path.with_suffix('.shape.json')
        sidecar_bytes = current_sidecar.read_bytes() if current_sidecar.is_file() else None
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        journal = getattr(self._runtime, 'journal_sink', None)
        if journal is not None:
            journal(checkpoint=str(safety), source_sha256=checksum, starting_revision=expected_revision,
                    document=str(path))
        execution.update(native_mutation_started=True, source_preserved=None, phase='revert')
        timer.start('restore')
        try:
            self.app.CloseDoc(self._get_doc_title(doc))
            shutil.copyfile(entry, path)
            atomic_json_write(str(current_sidecar), previous)
            opened = self.open_document(str(path))
            if not opened.get('success'):
                raise ProgramError('REVERT_FAILED', opened.get('message', 'Cannot open the restored part'))
            doc = self.app.ActiveDoc
            restored_record = read_record(doc)
            if not restored_record or restored_record.get('revision') != previous.get('revision'):
                raise ProgramError('REVERT_FAILED', 'The restored part does not carry the expected program revision')
        except Exception as exc:
            restored = False
            try:
                try: self.app.CloseDoc(path.name)
                except Exception: pass
                shutil.copyfile(safety, path)
                if sidecar_bytes is not None:
                    current_sidecar.write_bytes(sidecar_bytes)
                reopened = self.open_document(str(path))
                restored = bool(reopened.get('success') and hashlib.sha256(path.read_bytes()).hexdigest() == checksum)
            except Exception:
                pass
            return self._error(getattr(exc, 'code', 'REVERT_FAILED'), str(exc), document_restored=restored,
                               details={**getattr(exc, 'details', {}), **execution, 'source_preserved': restored,
                                        'native_file_preserved': restored, 'checkpoint': str(safety),
                                        'timings_ms': timer.result(), **failure_trace(exc)})
        # The restored entry is consumed: a second revert goes one edit further back.
        for stale in (entry, entry.with_suffix('.shape.json'), safety):
            try: stale.unlink()
            except OSError: pass
        warnings = []
        timer.start('working_view')
        working_view = focus_bodies(self, doc)
        if not working_view['verified']:
            warnings.append({'code': 'WORKING_VIEW_FAILED', 'details': working_view})
        timer.start('observe')
        images = []
        if observe:
            try:
                observed = self.observe_model(str(path.parent / (path.stem + '_views')), document_path=str(path),
                                              expected_revision=observed_revision(doc, restored_record),
                                              views=observe if isinstance(observe, list) else None)
                images = observed.get('data', {}).get('images', [])
                if not observed.get('success'):
                    warnings.append({'code': 'OBSERVATION_FAILED'})
            except Exception as exc:
                warnings.append({'code': 'OBSERVATION_FAILED', 'message': str(exc)})
        timer.start('readback')
        return self._result(True, 'Restored the state before the last edit', data={
            **self._public_shape_native(restored_record['native']), 'status': 'NATIVE_GEOMETRY_REVERTED',
            'committed_document': str(path), 'native_file_preserved': True, 'native_mutation_started': True,
            'revision': observed_revision(doc, restored_record), 'parameters': global_values(doc, restored_record),
            'body_state': self._shape_body_state(),
            'preserved_components': list(restored_record['native'].get('components', {})),
            'rebuilt_components': [], 'added_components': [], 'removed_components': [],
            'edit_strategy': 'restore_saved_state', 'history_depth': len(history_entries(path)),
            'timings_ms': timer.result(), 'working_view': working_view, 'images': images, 'warnings': warnings,
            'acceptance': 'EXECUTED_NOT_DELIVERY_VALIDATED'})

    def _edit_shape_program(self,code,parameters,document_path,expected_revision,observe,execution=None,
                            patch=None):
        if execution is None:
            execution = {'native_mutation_started':False, 'source_preserved':None, 'phase':'preflight'}
        timer=PhaseTimer(self._runtime.record_progress)
        timer.start('preflight')
        path=Path(document_path or '')
        if not path.is_absolute():
            raise ProgramError('DOCUMENT_MISMATCH','Edit requires the absolute path of a saved part')
        if not path.is_file(): raise ProgramError('DOCUMENT_UNSAVED','Save the document before editing')
        from .shape_observation import activate_target
        activate_target(self,str(path))
        doc,error=self.get_active_doc()
        if error: return error
        record=read_record(doc)
        if not record: raise ProgramError('NATIVE_MAPPING_MISSING','This document has no embedded shape program')
        if not expected_revision or expected_revision!=observed_revision(doc,record):
            raise ProgramError('STALE_REVISION','Refresh get_model_context before editing')
        if bool(com_get(doc,'GetSaveFlag')):
            raise ProgramError('DOCUMENT_DIRTY','Save manual changes before transactional editing')
        execution['source_preserved'] = True
        patched=patch is not None
        code,program,changed_lines=edited_program(record,code,patch,parameters,lambda:global_values(doc,record))
        reconcile,_,_=prepare_reconcile(record,program)
        blocked=self.ui_preflight('execute_model_code',{},mutating=True)
        if blocked: return blocked
        timer.start('checkpoint')
        checkpoint=Path(tempfile.gettempdir())/'SolidworksMCP'/('shape-edit-'+uuid.uuid4().hex+'.SLDPRT')
        checkpoint.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,checkpoint)
        checksum=hashlib.sha256(path.read_bytes()).hexdigest()
        # Recovery after a lost owner process needs these facts in the session journal.
        journal=getattr(self._runtime,'journal_sink',None)
        if journal is not None:
            journal(checkpoint=str(checkpoint),source_sha256=checksum,starting_revision=expected_revision,
                    document=str(path))
        working=copy.deepcopy(record)
        committed=False
        timer.start('working_view')
        focus_names = changed_body_names(record['program'], program, record['native'].get('components', {}))
        if focus_names:
            focus_bodies(self, doc, focus_names)
        execution.update(native_mutation_started=True, source_preserved=None, phase='native_edit')
        try:
            timer.start('native_update')
            with command_scope(self.app), build_scope(doc):
                if reconcile['remove'] or reconcile['build'] or reconcile['added_parameters']:
                    edits=reconcile_native(self,doc,working,program,timer=timer)
                else:
                    edits=update_native(self,doc,working,program,timer=timer)
                if Path(self._get_doc_path(self.app.ActiveDoc)).resolve()!=path.resolve():
                    raise ProgramError('DOCUMENT_CHANGED','Active document changed during edit')
                timer.start('record')
                working.update(program=program,source=code,revision=uuid.uuid4().hex)
                working['native']['program_hash']=program['program_hash']
                write_record(doc,working)
            timer.start('save')
            saved=self.save_document(str(path))
            if not saved.get('success'): raise ProgramError('SAVE_FAILED',saved.get('message','Cannot save edit'))
            committed=True
        except Exception as exc:
            restored=False
            try:
                self.app.CloseDoc(self._get_doc_title(doc))
                shutil.copyfile(checkpoint,path)
                opened=self.open_document(str(path))
                restored=bool(opened.get('success') and hashlib.sha256(path.read_bytes()).hexdigest()==checksum)
            except Exception:
                pass
            predicted,causes=failure_causes(exc,program)
            return self._error(getattr(exc,'code','NATIVE_EDIT_FAILED'),str(exc),
                stage=getattr(exc,'details',{}).get('stage','native_edit'),
                document_restored=restored,
                likely_causes=causes,
                details={**getattr(exc,'details',{}), **execution, 'source_preserved':restored, 'native_file_preserved':restored,
                         'checkpoint':str(checkpoint),'committed':committed,
                         **({'program_warnings':predicted} if predicted else {}),
                         'timings_ms':timer.result(),**failure_trace(exc)})
        finally:
            if committed:
                # The pre-edit state becomes the newest history entry for mode="revert".
                try: keep_history(path,checkpoint,record,checksum)
                except Exception:
                    try: checkpoint.unlink(missing_ok=True)
                    except OSError: pass
        warnings=program_warnings(program)
        warnings.extend(body_health_warnings(self,doc,working['native']))
        release_undo(doc)
        memory_warning(warnings)
        timer.start('working_view')
        focus_names = changed_body_names(record['program'], program, working['native'].get('components', {}))
        working_view = focus_bodies(self, doc, focus_names or None)
        if not working_view['verified']:
            warnings.append({'code':'WORKING_VIEW_FAILED', 'details':working_view})
        timer.start('sidecar')
        try: atomic_json_write(str(path.with_suffix('.shape.json')),{
            **working,'native_file_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        except Exception as exc: warnings.append({'code':'SIDECAR_WRITE_FAILED','message':str(exc)})
        images=[]
        timer.start('observe')
        if observe:
            try:
                observed=self.observe_model(str(path.parent/(path.stem+'_views')),
                    document_path=str(path), expected_revision=observed_revision(doc, working),
                    views=observe if isinstance(observe, list) else None)
                images=observed.get('data',{}).get('images',[])
                if not observed.get('success'): warnings.append({'code':'OBSERVATION_FAILED'})
            except Exception as exc:
                warnings.append({'code':'OBSERVATION_FAILED','message':str(exc)})
        # A read-back failure cannot undo a successful native save. Report the
        # commit explicitly so the caller does not repeat a completed mutation.
        timer.start('readback')
        revision=None
        values=None
        body_state={}
        try:
            revision=observed_revision(doc,working)
            values=global_values(doc,working)
            body_state=self._shape_body_state()
        except Exception as exc:
            warnings.append({'code':'POST_COMMIT_READ_FAILED','message':str(exc),
                             'recovery':'Refresh get_model_context before the next edit'})
        # A definition update preserves every component; say so in the same terms.
        edits={'preserved_components':list(working['native'].get('components',{})),'rebuilt_components':[],
               'removed_components':[],'added_components':[],'edit_strategy':'definition_update',**edits}
        return self._result(True,'Native shape definitions updated',data={
            **self._public_shape_native(working['native']),**edits,'status':'NATIVE_GEOMETRY_UPDATED',
            'committed_document':str(path),'native_file_preserved':True,'native_mutation_started':True,
            'revision':revision,'parameters':values,'body_state':body_state,
            'changed_source_lines':changed_lines,'patch_applied':patched,
            'history_depth':len(history_entries(path)),
            'timings_ms':timer.result(),
            'working_view':working_view,
            'images':images,'warnings':warnings,'acceptance':'EXECUTED_NOT_DELIVERY_VALIDATED'})
