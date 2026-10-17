# SolidworksMCP

**SolidworksMCP** is a production-oriented Model Context Protocol server for
controlling a live SOLIDWORKS session through the Windows COM API.

Installed candidate: **6.30.0rc1**, ready for user acceptance. The native qualification
matrix passed on **SOLIDWORKS 2026 SP2.1** on October 12–13, 2026: all 12 fixture
lifecycles at 128/256/512 nodes across straight, curved, loft and lattice families;
16 Boolean/contact/scope cases; and all three full-tower union orders with strict
export, rollback and edit/revert. This qualifies the tested fixtures, not every
possible 512-node program. The candidate is not promoted to a stable release.

The hash-locked Eiffel benchmark passed all 26 exports, retaining 186,152 facets,
normal reads, strict validation and no mesh repair:

| Reader / session | Runs | Median export | Maximum export |
|---|---:|---:|---:|
| compat / fresh | 3 | 278.15 s | 278.86 s |
| compat / repeated | 10 | 233.05 s | 250.88 s |
| compat / after five edit/revert cycles | 10 | 340.70 s | 366.44 s |
| bound / fresh | 3 | 349.48 s | 381.09 s |

The two compat series ran in the same SolidWorks process without a restart. No
native RPC failures occurred in these runs. The slowest after-edit export was
1.32 times the fresh compat median, below the 2-times qualification budget.
Bound showed no measured speed advantage; load conditions differed between series,
so this is not an isolated causal comparison. **compat remains the default**.
STL bytes can vary across fresh sessions or edit/revert due to local triangulation;
strict acceptance passed, but binary determinism is not promised.

Known limits: long build sessions accumulated up to 46.88 GiB of SolidWorks private
memory; save and restart between completed jobs when warned. Never restart inside
active CAD work or a benchmark series. One of the recorded status/metrics pairs
took 1.563 s during STL validation, so the subsecond target was not met universally.
The original RPC-loss cause and a universal degradation fix remain unproven.

## 6.30.0rc1: large programs, observable exports and durable results

- The interpreter component budget increases from 128 to **512**. Limit errors
  identify the rejected node and numerical budget; analyze returns component_count,
  max_components and remaining_components even in compact responses. Geometry hashes
  are unchanged. All 12 size/family fixture lifecycles have passed.
- RUNNING answers, executor status and MCP notifications include phase counters,
  timestamps, phase_percent, rate_per_sec and seconds_without_progress. Heartbeats
  do not imply work advanced. Expert get_session_metrics stays available while CAD
  is busy. Status includes the current/last tessellation call without using COM.
- Exports retain normals, mesh settings, cache behavior and strict validation.
  Reader telemetry reports per-method time/call/cache/conversion statistics;
  RPC errors keep the HRESULT, method, phase, progress and partial-file facts.
  The default reader remains `compat`; `SOLIDWORKS_MCP_TESSELLATION_READER=bound`
  is an experimental A/B option using the same typed methods. Its three fresh
  native exports passed strict acceptance, but did not establish a speed benefit.
  No universal fix for session degradation or the original RPC loss is claimed.
- A failed union may retry once in a computed order, only after confirming unchanged
  features and bodies and proving a connected union on temporary copies. AABB only
  prunes candidates. Difference and intersection retain their input order. The body
  map records input_order/resolved_order and connectivity evidence. No sequential
  extra Combine features, geometry moves or relaxed tolerances are introduced.
  State comparison tolerates machine-roundoff differences in native mass properties
  (64 ULP at the measurement scale), while identities, feature inventory and face
  counts remain exact. This does not change geometry or delivery tolerances.
- Completed operation results survive reconnect in the operation_results table of
  the existing request-receipts database. This is historical evidence only; receipt
  retry decisions and session quarantine retain their existing authority. Results
  are checksum protected and committed atomically. Images are managed copies with
  hashes. Ordinary history expires after seven days, 128 results or 128 MiB; each
  JSON result and its image set are limited to 16 MiB apiece. Expiry tombstones are
  capped at 1024. Unresolved operations and request receipts are never auto-evicted.
  Missing assets, expired/corrupt results and unknown IDs have distinct diagnostics.
  Persistence failures do not repeat CAD or change a known mutation outcome.

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

## 6.29.5: response isolation and operation diagnostics

- Invalid tool results return `RESULT_SERIALIZATION_ERROR` before reaching the
  MCP writer. Handler outcome and response preparation are traced separately;
  rendering never repeats a CAD operation or changes its receipt.
- Session metrics join starts and waits by operation ID. Completed mutation
  outcomes and durations are counted once; waits and status queries are
  separate from recovery. `errors_by_code` remains a per-call counter;
  `operation_errors_by_code` counts distinct operation outcomes.
- Boolean count mismatches report expected/actual counts and the operation.
  Invalid component sequences return `INVALID_SEEDS` with an example.
- Tool descriptions clarify draft limitations, Boolean count assertions,
  component sequences, literal expert search and intentional overlaps.

## 6.29.4: descriptions and deprecations match the behaviour

No change in behaviour; what a client reads about the tools is now what they do.

- **Deprecated**, because every long call answers `RUNNING` and is collected with
  `wait_operation`: `get_job_result` (alias; no longer in the main tool list,
  still dispatched and listed in the expert catalog), `execute_python_async`
  (use `execute_python`) and `list_jobs` (use `get_executor_status` or
  `get_session_report`). Seven tools are deprecated in all; each names its
  replacement and `expert_tools describe` gives the migration.
- **Main tools.** `model_api_lookup` names the `templates` topics;
  `execute_model_code` names the warnings to read, the restore after a failed
  edit and what `idempotency_key` does now (replay, repeat after revert, repeat
  of a failed create); `observe_model`, `inspect_model` and `export_model` say
  that a closed part is opened; `compare_reference` explains `tolerance_mm`;
  `get_executor_status` names the version, code-changed and memory fields.
- **Expert tools.** `execute_python` no longer points to the asynchronous
  entry; `restart_solidworks` is described as the way to release memory too;
  feature-level fillet, chamfer, pattern and mirror tools, `export_file`,
  `create_multibody_insert`, `create_revolved_body` and
  `compare_body_silhouette_to_image` point to the shape constructor, template
  or main tool that now covers the ordinary case.
- **Reference topics** (`model_api_lookup`): `draft` describes the exact plane
  cuts and the warnings; `lifecycle` the answer fields `rebuild_mode` and
  `request_receipt` and when a repeat runs again; `tube_path` the tangent rule
  and the bend limit; `execution` the receipt rules and the status fields.

## 6.29.3: a rebuild is proven, not trusted

A full run of the live acceptance suite on 6.29.2 failed one case of
seventeen, once: after `H` went from 150 to 75 the bodies kept their old size.
The part the edit had saved held `H = 75` in its equations and dimensions and
every equation curve at its old points; a plain `ForceRebuild3` on the reopened
file regenerated all of it. SOLIDWORKS had answered the forced rebuild with
success without re-evaluating the curves, and the edit had reported success:
a saved part whose geometry did not match its parameters. The case passes when
it is run again; the cause in SOLIDWORKS is not known.

After the forced rebuild of an edit the server now checks up to three
equation curves that the edit must have moved (first, middle and last of those
that depend on a rewritten parameter or were rewritten themselves) against the
live dimensions, about 0.6 s. A stale curve earns one more evaluation and
rebuild (`rebuild_mode: forced_twice`); if it is still stale the edit fails
with `NATIVE_REBUILD_STALE` and the saved part is restored.

## 6.29.2: one revision per saved part

Verifying 6.29.1 through the MCP client confirmed its fix and found a defect
that had been there since document-bound receipts: an edit addressed to a part
that was **not open** answered `STALE_REVISION`. The request layer opened the
part under its normalised lower-case path, SolidWorks then reported the
document under that spelling, and the revision hashes the reported path. The
same saved bytes had two revisions, one per spelling.

- The request layer opens a document under the spelling the caller used; the
  lower-case form stays an identity for keys and receipts only.
- The revision uses the spelling the file system stores (`true_case`), so it
  is the same however the document was opened. Parts opened under their real
  spelling keep the revision they had.

## 6.29.1: the same edit after a revert

Verifying 6.29.0 through the MCP client confirmed its four fixes and found one
more defect of the request receipts. An edit, a `mode="revert"` and the same
edit again answered `IDEMPOTENCY_STATE_CHANGED`: the request key is derived
from the document, its starting revision and the payload, all three were the
same, and the receipt of the first run no longer matched the document. A
completed request whose document is back at the exact saved state it started
from now runs again (`request_receipt.attempt`); so does a create whose saved
part was deleted. `IDEMPOTENCY_STATE_CHANGED` has a recommended action.

## 6.29.0: fixes from verifying 6.28.0 through the MCP client

Every 6.28.0 fix was exercised through the MCP client (lookups, analyze
warnings, create, failing and successful edits, revert, inspection, native
silhouette, colour queries, export). All of them hold. The run turned up four
more defects:

- **Tube paths were solved twice.** SOLIDWORKS 2026 marks a 3D-sketch equation
  spline over defined when the tangent at its end is parallel to its chord and
  the tangent at its start is not. An interpolated path meets that in its last
  segment and wherever a segment ends a straight run, so every `tube` and
  `variable_tube` through three or more points earned a warning, a reversal of
  the curve and a forced rebuild of the whole part: 2 to 5 s per tube end in a
  65-component model, and once the reversal itself failed with
  `NATIVE_PATH_FAILED`. New paths are planned from the end whose tangent lies
  along the chord (`orient_path_chord`, recipe `path_chord_v2`); saved parts
  keep the direction their native curves were solved in. The reversal stays as
  a fallback and its error now says which step failed.
- **Lofts along a centerline.** SOLIDWORKS resolves the pick point of a loft
  section in screen space. The stations of a `variable_tube` have two vertices
  that lie in front of each other in the Front view the build runs in, so the
  connector started at either one: some hoses failed to loft with the aligned
  pick points of 6.28.0, others without them. The camera is now turned to the
  direction that shows every pick point clear of all other section vertices
  and framed on the sections while they are selected; it is restored
  afterwards (`pick_view`).
- **A failed create could not be repeated.** The identical call answered
  `DOCUMENT_NOT_FOUND`: the request receipt asked for the state of a part that
  was never saved. A create that failed and left no file now runs again under
  the same request key (`request_receipt.attempt`). A replayed failed edit
  says so in its message: `Not executed again: ...`.
- **Causes of a failed rebuild.** `likely_causes` listed every program warning,
  so a failed fillet was blamed on a tube elsewhere in the part. Only warnings
  about the component that failed are causes; the rest stay in
  `details.program_warnings`. Edit failures carry `stage: native_edit`.

`NATIVE_PATH_FAILED` now has a recommended action (repeat once, then move a
point by 0.01 mm). A recipe id of another kind stored under the same component
name no longer leaks into `sectioned_solid`.

## 6.28.0: fixes from the end-to-end retest

A complete figure (65 components, nine bodies, eight glued joints) was built
through the MCP client from two reference views. What it exposed:

- **Twisted lofts.** A loft between an ellipse and an exact circle came out
  twisted by a quarter turn: an hourglass instead of a cone, in
  `sectioned_solid` with two sections and in `spans="linear"`. SolidWorks
  starts the connector of a circle at an arbitrary vertex; every section is now
  selected with a pick point at the same angular position
  (`ParametricCompiler.connector_points`). The same change removed the kernel
  faults of a three-section loft that ends in a circle.
- **A failed rebuild names its feature.** `NATIVE_REBUILD_FAILED` lists the
  failing features in tree order with their type and component, explains the
  first one and recommends the next call.
- **Tight tube bends are announced.** analyze, create and edit warn with
  `TUBE_BEND_TOO_TIGHT` when a path bends tighter than the tube radius: such a
  sweep can build and then fail when a parameter scales the model
  (`solidworks_mcp/shape_checks.py`).
- **Invalid bodies are reported at once.** create and edit warn with
  `NATIVE_BODY_INVALID` (body, fault count, owning components) instead of
  leaving it to `export_model`, whose refusal now names the components too.
- **`export_model` answer.** The compact answer carried text under `details`,
  which the server reads as the dictionary of a failed call: a successful
  export ended in an unhandled error after the files were written. Only the
  MCP path was affected.
- **Wall check.** A wall is counted where the far face is within 35 degrees of
  parallel; acute edges are reported separately and never fail. `thin_spots`
  lists up to three thin places with their thickness and area.
- **`compare_reference`.** A level passes when the model matches at a height
  within the tolerance of the level, so a horizontal edge that is slightly off
  no longer fails the level it crosses.
- **`measure_reference`.** A query may name a `color`: it then measures that
  coloured element inside the figure (eyes, teeth), ignoring hairlines.
- **Draft.** A plain box that covers one whole side of its target is a plane
  cut, an intersection is clipped to a box mask, an inward surface patch stays
  inside its host. The first two are exact.
- **Guidance.** New pitfalls (tube bends, wide surface-patch masks on curved
  hosts) and the template `curved_inlay`: an inlay that wraps round a cylinder
  with radial walls.

## 6.27.0: checks on the built bodies, patterns, templates

- **Native silhouette.** `compare_reference(document_path, source="native")`
  compares the tessellation of the bodies as built with the reference image, so
  cuts, surface patches and anchored features count. The default
  `source="draft"` stays COM-free (`solidworks_mcp/mesh_checks.py`,
  `automation/native_mesh.py`).
- **Printability.** `inspect_model(printability=true | {min_wall_mm, up})`
  reports whether the assembled model stands on its base (centre of mass over
  the footprint, margin, tip angle) and the thinnest wall of every body with
  its location. `passed=null` marks a thin spot smaller than 2x2 walls, usually
  a sharp edge.
- **Patterns.** `model.pattern(name, components, count=, step=(dx, dy, dz))`
  and `model.pattern(..., count=, axis="Y", center=...)` create one native body
  pattern instead of `count` placed components. A row costs one spacing
  dimension, a full circle none: the direction is a native line tied to the
  ends of an equation curve.
- **Templates.** `model_api_lookup("templates")` lists vetted programs for
  common printable parts (`pin_socket`, `inlay`, `hinge`, `collar`,
  `mirrored_limb`, `open_shell`); `model_api_lookup("template:<name>")` returns
  the code. Every template is built and its clearance measured by the live
  acceptance test (`solidworks_mcp/shape_templates.py`).
- **Speed.** `rounded_prism` and `prism` are swept planar 3D-sketch profiles:
  no depth dimension and no body move, so nothing enters the equation manager.
  On the 88-component reference an edit takes 25 s instead of 30 s (67 s in
  6.24.0). Parts saved with the extrusion recipe keep it.

## 6.26.0: draft first, build once

A native build of a 90-component model takes minutes and every edit a full
rebuild. This release adds the fast loop that precedes them.

- **Draft.** `execute_model_code(mode="analyze")` returns predicted extents of
  every resulting body in milliseconds; `draft_views` adds silhouette images.
  No SolidWorks call is made (`solidworks_mcp/shape_draft.py`).
- **Reference comparison.** `compare_reference(image_path, view, code |
  document_path)` compares the draft silhouette with a reference image: IoU,
  size deltas, a level-by-level table of left/right extents in mm with the
  worst deviations first, and an overlay image.
- **Revert.** `mode="revert"` restores the saved state before the last edit in
  seconds, up to three edits back (`.solidworks-mcp-history` beside the part).
- **Constructors.** `prism` (polygon outline), `rounded_prism` with radius 0
  (plain box), `revolve(closed=True)` (ring).
- **Self-contained guidance.** The server instructions carry the workflow;
  `model_api_lookup` has `workflow`, `draft`, `examples` and `did_you_mean`.
- **Speed.** Straight tubes are analytic cylinders with no equation-manager
  entries; dimension equations are added once at the end of a build; feature
  renames use the native name index; tree and graphics refresh are suspended
  during a build (`SOLIDWORKS_MCP_LIVE_BUILD=1` restores it). Measured cost: each
  equation in the SolidWorks equation manager adds about 0.28 s to every forced
  rebuild.
- **Operations.** `get_session_report` (expert) summarises the local call
  trace; `get_executor_status` reports whether the running process is older
  than the code on disk and how much memory SolidWorks holds; mutations warn
  with `SOLIDWORKS_MEMORY_HIGH`; the native undo list is released after every
  committed mutation. `export_model` and `observe_model` answers are compact;
  `execute_model_code(observe=[...])` selects the views returned after a
  mutation. Standard planes are found by position, not by their English names.

## 6.25.0: long operations, compact answers, new recipes

- **Long operations.** A tool call waits at most `SOLIDWORKS_MCP_SYNC_WAIT_SEC`
  (default 45 s, below the 60 s request limit of MCP clients). A longer
  operation keeps running and the call answers `status: RUNNING` with an
  `operation_id`; `wait_operation` returns the original result, images
  included. A finished operation never quarantines the session.
  `reconcile_session` is only for a really unknown outcome.
- **Recovery.** `get_executor_status` separates busy from orphaned work by the
  identity of the owning process. `restart_solidworks` (expert) performs the
  verified restart and refuses while unsaved documents are open. Mutations
  journal their checkpoint, source hash and starting revision.
- **Compact answers.** `get_model_context`, `reconcile_session`,
  `get_executor_status`, create/edit, analyze and `inspect_model` return what
  the next step needs; `verbose=true` or `detail="full"` restore the full form.
- **Patch edits.** `execute_model_code(mode="edit", patch=[{old, new}])` edits
  the embedded program in place; `analyze` with `document_path` previews the
  rebuild plan. Every mutation reports `timings_ms` by phase.
- **Recipes.** Slot (stadium) `rounded_prism`; `fillet` and `chamfer` with
  numbering-independent edge selectors; a two-point `tube` as an analytic
  cylinder; `axis` on `rounded_prism`, `revolve`, `dome`, `sectioned_solid`;
  `spans="linear"` for ruled lofts.
- **Tools.** Shape tools open or activate `document_path`; `observe_model`
  takes custom cameras and `flat=true`; `export_model` writes
  `stl/<BodyName>.stl` and supports `replace=true`; `measure_reference`
  checks sizes against `expected_mm`; errors carry executable
  `recommended_actions`; tooltips no longer count as modal dialogs.
- **Speed.** Unchanged native equations are no longer rewritten: a parameter
  edit of the 270-feature reference went from 67 s to about 33 s.

## Discovery and working view

- Shape constructor lookup uses actual signatures, including keyword-only arguments.
  Shared server instructions are short; detailed limits are fetched by topic.
- Four compatibility tools are marked `DEPRECATED` with concrete replacements:
  `extrude_sketch`, `cut_extrude`, `compare_sketch_to_image`,
  `create_semantic_primitive`. Schemas and dispatch remain available; discovery
  adds `solidworks-mcp/deprecation` metadata. Expert tools with unique capabilities
  remain active.
- Shape construction frames each completed component; create finishes on the
  whole model and edit frames changed live outputs. Camera orientation is
  preserved and pixel framing is checked. Camera failures are warnings and do
  not roll back valid solids. Perspective views need manual framing.
- Observation still restores the working camera. No extra screenshot or CAD
  helper is required for live framing; expert/raw edits can use `zoom_to`.
- Geometry recipes, saved-program ownership and strict delivery acceptance retain
  the 6.23 behavior. Restart the MCP client to load this release.

## Post-retest compatibility and export polish

- New `sectioned_solid` profiles share emitted radius coefficients at every
  rational-quarter seam. Saved v1/trigonometric recipes retain their original
  representation during edits; no document migration is required.
- Affine dimensional equations keep coefficients in model units. This avoids
  tiny factored multipliers and huge integer terms without rewriting the curve
  emitter or rounding geometry to different dimensions.
- `get_model_context(check_edit_compatibility=True)` optionally compares native
  feature names/types to ownership metadata. Fast context returns `NOT_CHECKED`
  and conditional editing; `document_editable` only means an embedded program
  exists. Native definition/rebuild guards still run at execution.
- `request_receipt.native_mutation_started` distinguishes shape-program
  preflight rejection from a mutation attempt. Unknown legacy operations report
  null. `callback_invoked` is explicit; legacy `cad_executed` retains callback-entry
  semantics. `source_preserved` compares clean native fingerprints, not just a
  successful callback. Replay describes a historical result and does not mutate.
- `export_model` remains strict by default (`mesh_cleanup="none"`). Optional
  `mesh_cleanup="bounded"` applies only after raw STL fails: seam displacement
  <= 0.00006 mm, at most 10 collapsed facets and 64 local winding corrections,
  with tighter count-relative limits. No hole filling or general remeshing.
  Multiple connected surfaces are refused for cleanup, so inner shells are not
  inverted. Valid raw hollow meshes bypass cleanup. The unchanged validator
  checks the written float32 STL against native bounds/volume. Raw rejected STL,
  hashes, facet indices and displacement audit remain separate diagnostic files
  inside successful deliveries; only `kind="stl"` entries are final meshes.
  `diagnostics_directory` optionally retains raw STL and bounded facet samples
  after rejection, in a separate new directory. Source SLDPRT remains identical.

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

The default catalog contains 12 tools. Start with `get_model_context`, inspect
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

## Highlights

- Native sketch, feature, body, reference-geometry, view, probe, and export
  operations.
- Transactional CAD plans with checkpoints, invariants, idempotency, budgets,
  and rollback.
- Parametric sketches with named entities, constraints, dimensions, equations,
  persistent IDs, and degree-of-freedom analysis.
- Multibody tools with explicit merge scope, clearance, interference, bounding
  box, and volume verification.
- Deterministic raster-to-sketch pipelines for silhouettes and line art,
  including GPU-backed segmentation, topology preservation, primitive fitting,
  and reverse-raster quality gates.
- Native sketch/body export and calibrated geometric comparison instead of
  screenshot-only acceptance.
- Verified active-sketch Normal To and Fit to Screen. The server reads the
  resulting camera matrix and pixel occupancy instead of trusting a UI command.
- Structured diagnostics, modal preflight/postflight, native dimension guards,
  durable timeout quarantine and explicit reconciliation.

## Requirements

- Windows 10 or Windows 11.
- SOLIDWORKS 2025 or 2026, installed and licensed.
- Python 3.10 or newer; Python 3.11 is the acceptance-tested runtime.
- Git LFS for the packaged line-art checkpoints.
- An NVIDIA CUDA environment is strongly recommended for the deep
  vectorization modes. `get_capabilities` reports the actual backend state.

The server drives the interactive SOLIDWORKS process. It is not a headless CAD
kernel, and an unknown modal dialog must be inspected by a human.

## Clone and install

Install Git LFS before cloning, or pull the model objects afterwards:

```powershell
git lfs install
git clone https://github.com/czuryk/SolidworksMCP.git
Set-Location SolidworksMCP
git lfs pull
```

Create an isolated Python environment and install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

For CUDA vectorization, install the PyTorch build appropriate for the machine's
CUDA driver before or together with `requirements.txt`.

## MCP configuration

Point the MCP client at the repository entry point using the virtual
environment's Python executable:

```json
{
  "mcpServers": {
    "SolidworksMCP": {
      "command": "C:\\path\\to\\SolidworksMCP\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\path\\to\\SolidworksMCP\\solidworks_mcp_server.py"
      ]
    }
  }
}
```

Start SOLIDWORKS, restart or reconnect the MCP client, then call
`get_capabilities` and `get_environment_status` before a substantial modeling
operation.

### Stable compatibility identifiers

`SolidworksMCP` is the public project and MCP handshake name. The Python package
remains `solidworks_mcp`, the configured tool namespace remains `solidworks`,
and existing artifact schemas retain their `solidworks-mcp/...` identifiers.
Those internal names are compatibility contracts and are intentionally not
renamed.

## Operation families

The server exposes both small native operations and higher-level verified
workflows. Important families include:

- documents and environment: create/open/save/close, capabilities, session
  metrics, modal-state inspection, recovery;
- sketches: standard planes, planar faces, exact model-coordinate contours,
  primitive drawing, parametric sketches, dimensions, constraints, equations;
- features: verified extrude/cut, revolve, sweep, fillet, chamfer, shell,
  mirrors, patterns, reference axes and planes;
- bodies: visibility, naming, appearance, volume, clearance, interference,
  multibody insert workflows;
- verification: ray/section probes, sketch topology, native geometry export,
  sketch-to-sketch and calibrated image comparisons, body silhouettes;
- vectorization: selectable silhouette/region/line-art modes, deep segmentation,
  controllable approximation, CAD commit, and reverse-raster verification;
- orchestration: transactions, declarative CAD plans, semantic primitives,
  model-graph synchronization, atomic export bundles.

Use MCP tool discovery for the authoritative live schema. Tool descriptions and
`get_capabilities` are versioned with the server.

## Safety rules

- Supply an absolute `save_path` before the first mutation of an unsaved
  document, or explicitly opt into disposable work with
  `allow_unsaved_document=true`.
- Treat `success` as execution evidence. Verify topology, body count, bounding
  boxes, volume, tolerances, and a readable independent artifact.
- Use `scope_bodies` and `expected_merge_bodies` for multibody cuts and bosses.
- Do not auto-confirm unknown dialogs. Raw-code watchdog recovery requires an
  active tracked call and exact expected text; prefer native dimension guards.
- Native MCP tool coordinates use the declared user unit, normally millimetres.
  Raw SOLIDWORKS COM coordinates use metres.
- Do not invoke a blind `Ctrl+8` after sketch operations. Version 6.5.32 already
  applies and verifies the stable side/up orientation and working-geometry fit.

## Vectorization models

The deterministic line-art checkpoints live under
`solidworks_mcp/models/lineart/` and are stored with Git LFS:

- `dexined_biped_v2.pt`
- `teed_biped_5.pt`

Their source and license notices are documented in
`solidworks_mcp/models/lineart/LICENSES.md`. A clone containing small LFS pointer
files instead of the binary objects is incomplete; run `git lfs pull` before
starting the server.

SOLIDWORKS Autotrace/Picture to Sketch is an interactive PropertyManager feature
and has no supported public API entry point. This server therefore uses its own
deterministic segmentation and line-art pipelines.

## Configuration

Default runtime settings are stored in `solidworks_mcp/config.json`. Keep
machine-specific or sensitive overrides in `config.local.json`; that file is
ignored by Git.

Notable defaults include:

- automatic SOLIDWORKS executable and template discovery;
- millimetres as the user unit;
- operation-scoped dimension dialog guard;
- bounded transaction/runtime limits;
- automatic checkpoint directory management;
- INFO-level file logging.

Relative `log_file` values are written under
`%LOCALAPPDATA%\SolidworksMCP\`, not into the source checkout. An absolute path
in `config.json` remains supported.

## Tests

The repository contains the deterministic unit and regression suite:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Release 6.15.0 adds durable retry/conflict, native rollback, checkpoint retention
and scoped UI-monitor regression tests.
`tests/live_request_receipts.py --output C:/Tests/Receipts.SLDPRT --report C:/Tests/receipts.json`
checks native replay across reopen/MCP restart, changed targets/payloads/state,
and the UI-only monitor on the shared owner.
`tests/live_sta_executor.py --output C:/Tests/Executor.SLDPRT --report C:/Tests/executor.json`
checks native H round trips, responsive status/analysis, a delayed Python request,
and reconciliation after restarting the MCP process. It does not hang or kill SolidWorks.
Opt-in live acceptance: `tests/live_shape_program.py`, `tests/live_shape_edit.py`,
`tests/live_shape_clearance.py`, and `tests/live_shape_mcp.py`. Each creation test
requires a new explicit .SLDPRT path. Run only one live CAD test at a time.
These tests do not replace live acceptance
for COM behavior: qualifying another SOLIDWORKS major version should also cover
standard planes, planar faces, existing-sketch activation, view recovery,
features, transactions, vectorization, and export.

The following opt-in tests require an open, unblocked SOLIDWORKS session and
create new test parts. Use absolute output paths that do not already exist:

```powershell
python tests/live_shape_program.py --output C:/Tests/NativeShape.SLDPRT --observe --report C:/Tests/native.json
python tests/live_shape_mcp.py --output C:/Tests/McpShape.SLDPRT --report C:/Tests/mcp.json
```

The first test saves/reopens the B-rep and checks analytical volumes and native
mirror mass/centroids. The second starts a fresh stdio server, tests strict
schemas and writer exclusion, creates another native part and decodes the
inline images. Neither replaces the final short-prompt reconstruction benchmark.

## Project layout

```text
SolidworksMCP/
|-- solidworks_mcp_server.py       MCP stdio entry point
|-- requirements.txt
|-- LICENSE
|-- solidworks_mcp/
|   |-- server.py                  MCP schemas and dispatch
|   |-- tool_registry.py           high-level tool metadata
|   |-- body_capture_worker.py     isolated SolidWorks body tessellation
|   |-- vector_worker.py           isolated scientific worker
|   |-- config.py / config.json
|   |-- constants.py
|   |-- automation/                SOLIDWORKS COM implementation
|   |-- models/lineart/            Git-LFS checkpoints and licenses
|   `-- utils/
`-- tests/
    `-- test_v6.py                 unit and regression suite
```

Runtime logs, caches, local client settings, checkpoints, generated reports,
acceptance artifacts, and manual probes are intentionally excluded from the
repository.

## License

The server is distributed under the MIT License; see `LICENSE`. Packaged model
notices are retained separately in `solidworks_mcp/models/lineart/LICENSES.md`.

Original project: [alisamsam/Solidworks-MCP](https://github.com/alisamsam/Solidworks-MCP).
