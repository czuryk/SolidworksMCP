# Modeling guide

[Project overview](../README.md) · [All tools](tools.md) · [Companion skill](../skills/solidworks-mcp/SKILL.md)

## Ordinary modeling workflow

Check an actual fit without writing raw COM or making measurement sketches:

```json
{"body_pairs": [{"id": "fit", "a": {"component": "Socket"}, "b": {"body": "Insert"},
                 "min_clearance_mm": 0.20, "max_clearance_mm": 0.30}]}
```

Pass this optional batch to `inspect_model` with the active document and fresh
revision. It returns native boundary distance, closest points and intersection
volume. Containment is overlap even with positive surface distance. Read each
`relation` and `clearance_check.passed` (true/false/null); an inspection response
is not automatic acceptance. Measurements do not change selections or create
features. Limits apply to the global minimum distance, not uniform local fit.
Native failures remain UNKNOWN. The legacy `check_clearance` uses the same
adapter and no longer treats a failed Boolean as zero overlap.

Attach a detail to actual host geometry with a deferred native anchor:

```python
anchor = model.face_anchor(host, origin=(2, 30, 0), direction=(0, 0, 1))
detail = model.anchored_extrude('Control', anchor, {'circle': 2}, 3)
```

The first positive ray hit selects one trimmed face; ambiguous seam/tangent hits
are rejected. The compiler creates a native axis/intersection point/plane and
a useful solid. Native parameters move the origin and resize the profile/depth
after reopening, without rerunning Python. The plane is perpendicular to the
constant ray direction. Positive depth follows the ray; negative depth reverses
it. This is not a tangent or conformal surface; `surface_patch` remains the
fitted-detail operation. Ellipse and rounded_rectangle profiles are also
supported in the reported native local X/Y axes. Their equation curves are
kernel approximations; the circle uses a native circle with a driven diameter.
Before delivery/observation/editing, the compiler checks actual hits for live
hosts and native dependency links. Consumed historical hosts retain structural
checks without pretending to ray-test an unavailable final body.
See `model_api_lookup(topic="face_anchors")` for limits and failure semantics.

Model compilation, reconciliation and embedded-record writing now share a
restoring `CommandInProgress` scope on the existing STA. The previous state is
restored before saving or recovering a file, including failed mutations.

Build a component in local coordinates and place it with
`model.place(name, component_or_list, frame=model.frame(origin=..., rotation=...))`.
The frame applies right-handed rotations about world X, Y and Z in that order,
in radians, then translation. The compiler creates native equation-driven
rotation and translation features; source bodies are consumed and body count
is preserved. `frame.point()` and `frame.direction()` retain symbolic parameters.
`surface_point` covers two-station round/elliptical sides, domes and ellipsoids;
`path_point` and `path_tangent` follow the actual Hermite centerline. Anchors also
follow single-seed placements. These are declared-shape anchors, not arbitrary
B-rep face queries. Keep each active placement axis nonzero and with the same
sign for direct native parameter cycling; source edits reconcile changed axes.
Active rotations must have magnitude below 2*pi; identity placement is rejected.
SOLIDWORKS 2026 edge-angle features can require a second native rebuild after
reopen. The compiler detects dimension/feature lag and performs at most one
extra native pass, then verifies agreement. Read-only observation/inspection/export
reject unresolved lag; context reports it. Standalone native angle edits may
need a second Ctrl+Q; no Python regeneration is required.
See `model_api_lookup(topic="placement")` for the full contract.

Context and native observations now batch their external calls under a restoring
command scope. Observation still redraws and captures every requested view,
validates document/revision and image bytes, and restores the camera. No cached
image substitutes for a new capture. The opt-in `tests/live_observation_latency.py`
measures cold latency and nearest-rank p95 over at least 20 warm public MCP calls,
including actual ImageContent delivery. Latency depends on the model and host.

Native polynomial curves use bounded coefficients in Horner form. Input numeric
constants follow the existing 15 significant digit output contract before exact
algebra, preventing binary noise from creating false high-order terms. If a 3D
equation path returns the verified generic solver warning, the compiler tries
the exact reversed parameterization once, checks native health, and retains that
choice in the native record for edits. This changes neither the geometric locus
nor the shape family; failed recovery remains an error.

Native STL reading uses a typed ITessellation interface, caches shared vertex
data, and temporarily enables ISldWorks.CommandInProgress for the read loop.
The previous command state is restored on both success and failure. Requested
mesh tolerances remain explicit and no display mesh or global STL preferences
are substituted. The delivery manifest records per-body tessellation counts,
native data reads and elapsed time.

The default catalog contains 13 tools. Start with `get_model_context`, inspect
the reference images and write a compact `execute_model_code` shape program.
Use Python functions and loops to compose the volumes. Native profiles, frames,
parameter bindings, checkpoints and automatic observations belong to the compiler.
`model_api_lookup` returns a small index by default; request a topic as needed.
Tracing and reference measurements are optional.

Use `inspect_model` for revision-bound native body facts and selected radial
sections. `precision="delivery"` adds actual extreme-point bounds and kernel and
feature health. `observe_model` provides real native images and can choose its
output directory automatically. Inspection does not create helper geometry.

`export_model` publishes the saved native part, STEP, one STL per solid body and
a manifest into a new directory. It checks source identity, kernel validity,
STEP units/body counts, and STL closed/oriented topology, volume and bounds before
publishing. Existing output is refused unless `replace=true` and the directory
is an earlier delivery of this tool. The source is preserved; failures clean
only the staging directory created by that call. A long export answers
`RUNNING`; collect it with `wait_operation`.
Export success does not establish visual likeness, parameter cycling, clearances,
wall thickness, mesh self-intersections or a Unity rig; these remain explicit
acceptance fields. External outputs do not use native-only receipt replay.

Use `expert_tools(action="list", query=...)`, then `describe` and `call` for
document management, legacy features, raw Python or diagnostics. Gateway calls
use the same executor and protections. Set `SOLIDWORKS_MCP_TOOL_PROFILE=expert`
before server startup to list the full legacy catalog. Existing direct tool names
remain callable for compatibility.

## Execution and recovery

Expert Python failures retain stdout, stderr and the traceback in both the text
response and structured data. Python syntax/runtime failures, disconnected COM
objects, missing COM members and other HRESULTs have distinct structured codes;
native ProgramError codes and details are preserved. A returned exception after
execution starts reports `PARTIAL_EFFECTS_POSSIBLE`, without implying rollback
or allowing an automatic retry. No additional CAD call is made to diagnose that
failure. A disconnected object does not by itself prove the application crashed.
Watchdog cleanup happens once and cannot replace the primary failure.

`model.ellipsoid(..., approximate=True)` adds independently driven X/Y/Z radii
using a native constrained ellipse, four meridian guides, Sweep, Mirror and union.
The default preserves the existing exact constant-aspect Revolve/Scale recipe.
The guided variant reports `GUIDED_APPROXIMATION`; it has no continuous error
bound. Saved native parameters rebuild without Python. Analytic `surface_point`
is rejected for this variant; use actual B-rep inspection and `surface_patch`
for fitted details. Bounds alone do not establish geometric accuracy.

All CAD requests, including reads and raw background Python, enter one bounded
serial queue on a dedicated STA thread. COM proxies stay on that owner. An OS
lease excludes other current-version MCP processes. The existing native capture
child runs only while its parent owns that lease; it cannot run beside another
CAD request. Pure shape analysis, reference measurement, API lookup and executor
status use no COM and remain available during native work.

`execution_timeout_sec` only shortens the wait of one call, including queue time
(default and maximum `SOLIDWORKS_MCP_SYNC_WAIT_SEC`, 45 seconds). When the wait
ends first, the call answers `status: RUNNING` and the operation continues;
`wait_operation(operation_id)` collects the result. A request still queued at
that moment is withdrawn and answered `CAD_SESSION_BUSY`; it was not executed.
Waiting or client cancellation never cancels COM. The owner keeps the lease
until the callback returns and then records `COMPLETED`. Only an abandoned call
(UI watchdog, shutdown), a killed native client, a dead owner process or a failed
mutation without confirmed rollback leaves an unresolved journal.
`get_executor_status` and `wait_operation` never probe COM (`get_job_result` is a
deprecated alias of `wait_operation`).
Background Python returns a `job_id` for the same executor; it no longer creates
another COM writer.

The session journal is atomically persisted beside the OS lock under local
application data (`SolidworksMCP/session-writer.state.json`). It retains the
operation identity, request hash, process identity and bounded result summary.
After a known late return, `reconcile_session(operation_id)` reads native state;
it never replays the original operation or asserts delivery acceptance. After
an orphaned invocation or killed native capture client, reconciliation requires
a verified SolidWorks process restart. Restarting only MCP does not clear the
quarantine. A corrupt journal fails closed. Full prior result payloads are
retained only in the current process; the durable summary survives restart.

Native mutation receipts are stored under local application data in
`SolidworksMCP/request-receipts.sqlite3` (SQLite, full synchronous commits).
Public MCP shape create/edit derives a deterministic key automatically; callers
may supply `idempotency_key`. The key binds tool, absolute document, original
revision and payload. An identical retry returns the historical result only if
the active native part is clean and its saved bytes, configuration and model
identity match the recorded post-state. Old images are omitted on replay.
`get_request_status(idempotency_key)` reads the receipt without COM. Conflicting
payloads return `IDEMPOTENCY_CONFLICT`; changed native state returns
`IDEMPOTENCY_STATE_CHANGED`. An interrupted RUNNING receipt never automatically
reexecutes, even after session quarantine has been reconciled. Receipt keys are
not automatically evicted. Journal damage fails closed.

For legacy public mutations, an explicit key (or `execute_cad_plan.plan_id`)
also requires `document_path` and `expected_revision`. Unkeyed legacy requests
have no retry guarantee. Unsafe internal string-only caches were removed; direct
Python method calls are not the durable public request boundary. Current receipt
state verification supports saved native parts, not unsaved parts or assemblies.
A failed/partial result is never promoted to success. External file side effects
of arbitrary code are not made idempotent by a native part receipt.

Raw Python `ui_guard` now launches a Win32-only monitor beside the same STA
operation; it creates no second COM writer. It waits for clean preflight, stops
when the owning process/request ends, and can resolve at most one causally
matched `dimension_modify` dialog. Auto-resolution requires `caused_by.com_method`
and `expected_dialog_text`; the actual call must use injected `com_get`. The
monitor verifies request identity, an active call marker, the original SW frame,
and rechecks cancellation/call return immediately before acting. Unknown or
pre-existing dialogs are never accepted. Untracked direct API calls do not
receive automatic UI resolution. Native dimension guards remain preferred.
A caller deadline is not a sandbox for arbitrary Python/native extensions.

Transaction rollback now restores verified checkpoint bytes to the original
saved native path and reopens it; matching names/body counts are not proof of
restoration. Save manual changes before a rollback-capable transaction. Missing
or invalid checkpoints report unproven restoration; switching ActiveDoc stops
subsequent plan operations. Save failure cannot return a committed transaction.
Checkpoint retention deletes only files with matching server-owned manifests.
Rollback covers the native part, not arbitrary external export side effects.

## Shape programs

Use `model_api_lookup` for the compact Python API. `execute_model_code` accepts
functions, loops and parameter expressions with an injected `model` builder;
it compiles those shapes into native CAD rather than exposing COM details.
Supported constructors are elliptical `sectioned_solid` (Loft), spatial circular
`tube` (Sweep), `variable_tube` (centerline Loft), spherical-cap `dome`,
sparse-profile `revolve`, `ellipsoid`, `rounded_prism`, explicit `combine`
(union/difference/intersection), body `mirror`, and `surface_patch`.
All coordinates are model-space millimetres, Y up, X width, Z depth.

`observe_model` requires the active `document_path` and fresh `expected_revision`
from context. Optional component names frame a local region with surrounding
geometry. Frames share a native scale and carry revision, configuration and
image hashes; camera/helper visibility settings are restored. Native body facts
include approximate bbox, volume, center of mass and surface families. Optional
radial `sections` query one component's actual B-rep in a single ray batch,
without sketch entities or UI selection. A sampled radius check distinguishes
a round tube from a flat extrusion; it is not continuous circularity acceptance.
Neither a Sweep feature name nor a cylinder surface label alone proves shape.

`measure_reference` is a COM-free, optional helper for specific reference
uncertainties. Batch up to 12 visible foreground extent questions in explicit
pixel ROIs (bbox, center, aspect ratio, width at a row, Right-view depth).
It returns estimates, heuristic uncertainty and optional annotated crop images;
it creates no CAD entities. Metric calibration requires a declared orthographic
view. Occluded or unknown boundaries remain unknown. The lightweight backend
assumes separable foreground on a uniform background; it does not infer semantic
parts or hidden surfaces. Existing tracing remains available independently.
Ordinary 3D reconstruction does not require reference measurements or tracing.

Observation revisions include the native update stamp and configuration as well
as the embedded revision/parameters and saved-file hash. The stamp tracks native
geometry changes, not all appearance edits. Observation rejects stale targets
and mixed-state capture, uses unique artifact directories, and returns hashes of
the actual delivered pixels. These checks do not prevent a user from manually
changing the interactive document during an operation.

`mode="analyze"` validates without COM. `mode="create"` requires a new absolute
`.SLDPRT` output path and returns native Front/Right/Isometric images as MCP
ImageContent, component state and a `.shape.json` sidecar. Existing files are
never overwritten. The compiler owns only its newly created document and closes
it on construction failure. Errors after save identify `committed_document`.

Native global variables drive dimensions and equation-backed Loft/Sweep/Revolve
geometry; Mirror and dependent path planes rebuild inside the saved SLDPRT.
Use `get_model_context` for a fresh revision, then `mode="edit"` with
`document_path`, `expected_revision` and parameter overrides or revised code.
Same-topology edits update existing curves and features. A revised complete program
can also add/remove/replace components: only changed topology and its Boolean/Mirror
dependents are rebuilt. Owned obsolete features and their equations are deleted;
independent feature IDs survive. New parameters receive native driver bindings.
Existing parameter declarations and units must remain. Source and persistent
mappings are embedded in the part; the sidecar is optional for editing. A failed
edit restores the saved file checkpoint and reopens it. Save manual changes first.

Parameters are positive mm or dimensionless ratios. Keep expressions symbolic;
`model.value(parameter)` is rejected to prevent freezing native dependencies.
Boolean inputs are consumed; later operations reference the resulting component.
Definition edits rebuild these dependencies without recreating native features.
Default ellipsoids retain constant Z/X aspect: shared H rebuilds natively, and
definition edits can change the aspect. Explicit `approximate=True` supports
independent native radii and rejects analytic surface anchors. Native profile
relations are verified on creation and checked against persisted entity identities
before edits or dependency deletion; a void API return is never treated as proof.
Rounded prisms use a rounded XY rectangle and symmetric Z extrusion.
Topology reconciliation requires compiler v3 ownership metadata. Older v2 files
remain editable with the same topology; unmanaged feature changes block destructive
reconciliation rather than being silently deleted. Native face anchors currently
provide ray-normal extrusion frames. Tangent frames, parameter-driven ray
directions and parameter removal/unit changes remain unsupported. Native rigid
rotation/translation is available through `model.place` within its stated domain.

`surface_patch(name, host, mask, thickness, offset=0, inward=False)` creates a
separate solid following the host's actual curved faces. Native Offset Surface
and Thicken features are clipped to an explicit solid mask. The mask is consumed;
the host is retained. Offset and thickness are normal distances with native
equation bindings. Use the patch as a `combine` input for a recess or emboss.
The mask must intersect one connected patch; planar host faces are excluded.
Offsets that fail in the native kernel return a construction/rollback failure.

`surface_point(host, fraction=.5, angle=0)` returns symbolic model coordinates on
a two-station `sectioned_solid` or `revolve`, dome, exact-aspect ellipsoid or their
single-seed placement. Fraction is normalized height; angle is radians from +Z
toward +X. `face_anchor` instead targets actual B-rep faces for anchored_extrude.
New elliptical Loft sections use rational quarters with exact endpoint closure.
Saved older section recipes retain their native curve topology during edits.
The public parameter `Thickness` maps to `ShapeParameter_Thickness` in Equation
Manager because SolidWorks reserves the unqualified name.

Use `get_model_context(include_source=true)` to retrieve the editable source
embedded in the native part, including its parameter declarations after restart.
The default response stays compact and omits source.
Legacy CAD tools also use the serial STA executor. Their individual geometry
validation and rollback contracts remain separate from the shape compiler.

## Program limits

Shape programs accept up to **512 components** at the interpreter boundary. A
component is one node added by a shape constructor, including pattern, mirror,
placement and Boolean operations. Consumed nodes still count. Parameters and
expressions do not count. This is an MCP program limit, independent of SolidWorks
body/feature counts. Read the active values with `model_api_lookup(topic="limits")`.

Pattern count remains 2–100 including the seed; combine expected_bodies remains
1–32. Source is limited to 150,000 characters, range to 10,000 entries and execution
to 50,000 line events. Delivery still accepts 1–64 named bodies. STL triangle
budgets, native record size and diagnostic face budgets remain separate.

Nested patterns can expand far beyond their component count. Analyze estimates
created body instances and warns with EXPANDED_GEOMETRY_HIGH above 4096. Drafts
have a separate 2,000,000-point retained geometry budget (48 MB of coordinate
payload before hulls and other overhead); DRAFT_COMPLEXITY_LIMIT rejects excessive
expansion before pattern copies are allocated. This limits draft memory, not native
component count. Consumed components are retained. Shared point arrays in union,
fillet and chamfer drafts are counted once; transformed copies consume the budget
separately. The sampling density and native geometry are unchanged.

Reproducible live qualification scripts: `tests/live_shape_limits.py`,
`tests/live_export_benchmark.py`, `tests/live_union_regression.py`, and
`tests/live_union_cases.py`. Use a new output
directory and an idle session. These are opt-in and are not part of unit discovery.
