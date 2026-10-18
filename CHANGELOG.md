# Changelog

[Project overview](README.md) · [Current tool reference](docs/tools.md)

## Post-candidate update: optional vectorization weights

- Move the verified DexiNed/TEED exports to
  [SolidworksMCP-models](https://github.com/czuryk/SolidworksMCP-models).
  The main checkout and source ZIP no longer need Git LFS or model downloads.
- Keep CAD modeling available without weights. Tracing reports
  `MODEL_WEIGHTS_MISSING` or `MODEL_WEIGHTS_INVALID` with filenames, paths and
  installation instructions. Missing-weight requests stop before CAD queueing.
- Add an offline installer with SHA-256 verification and an optional external
  model directory. Existing installations remain supported. Region tracing
  identifies its separate SAM 2.1 / ViTMatte cache requirements.
- Verify missing, partial, corrupt and LFS-pointer installations with regression
  tests and a fresh native MCP lifecycle. [Setup](docs/setup.md#optional-vectorization-models)
  and [validation](docs/validation.md#optional-model-acceptance).

## Post-candidate fix: localized standard planes (issue #1)

- `create_sketch(Front/Top/Right)` resolves the first three top-level `RefPlane`
  features and selects the feature with `Select2(False, 0)`. Localized and renamed
  planes no longer depend on English display names. Discovery is shared with the
  shape compiler; the public arguments remain unchanged.
- Failed lookup/selection reports the requested plane, resolved name, method and
  original HRESULT when available, without opening a sketch or misclassifying a
  false selection result as `COM_MEMBER_MISMATCH`.
- Reproduced on SOLIDWORKS 2026 SP2.1; all six English/Polish-name native lifecycles
  passed. Added ten regression tests. Thanks to **piotrostapiuk-ship-it** for the
  confirmed cause and proposed fix. [Evidence and scope](docs/issue-1-standard-planes.md).

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
