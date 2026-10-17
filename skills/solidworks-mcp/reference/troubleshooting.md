# Troubleshooting

Every entry is a failure seen in practice. Format: **symptom → cause → fix**.
First move for almost anything weird: **`get_environment_status`** (connection,
freeze bar, modal dialog, typed module).

## Contents

1. [Feature creation](#feature-creation)
2. [Bodies and appearance](#bodies--appearance)
3. [Tree, traversal, and documents](#tree--traversal--documents)
4. [Sketching](#sketching)
5. [Selection and rays](#selection--rays)
6. [Environment](#environment)
7. [Screenshots as a debugging tool](#screenshots-as-a-debugging-tool)

---

## Feature creation

### PROGRAM_LIMIT with numerical component details

The interpreter maximum is **512** nodes, including consumed inputs. The error
reports existing_components, attempted_components, max_components, component,
component_kind and limit_kind. It does not claim to know the full source total
after execution stops at the first excess. Read `model_api_lookup(topic="limits")`,
use pattern for repetition or split genuinely independent parts. Do not repeat an
unchanged program above the limit. DRAFT_COMPLEXITY_LIMIT instead means nested
geometry exceeded the retained draft point budget; reduce that expansion. Union
drafts reuse point arrays and do not count those shared coordinates twice.

### NATIVE_BOOLEAN_FAILED or NATIVE_BOOLEAN_DISCONNECTED

Read input_order, resolved_order, component_body_counts and connectivity. Put
the connecting platform before separate supports. One internal reorder is allowed
only after proving unchanged document state and a valid union on temporary bodies.
Disconnected bounding-box groups have positive separation; overlapping boxes or
zero distance alone are inconclusive. Inspect body pairs before adding a real
connection. Difference keeps its original target order. A failed edit must report
document_restored/source_preserved before another mutation.

### RPC_CONNECTION_LOST during tessellation

The original HRESULT, COM method, index, progress and partial-file information
are preserved. Check `get_executor_status` and COM-free expert get_session_metrics.
Wait for active work to return; never restart SolidWorks inside an active COM call.
Save recoverable work, then use the existing restart/reconcile flow if needed.
The 6.30.0rc1 native benchmarks passed 3 fresh, 10 repeated and 10 after-edit compat
exports without RPC loss. They do not establish the original RPC-loss cause;
memory growth alone does not prove a leak or crash cause. The bound reader passed
three fresh exports but showed no measured speed advantage; keep compat by default.
Memory warnings can also reflect system-wide commit pressure. Read both the
SolidWorks private usage and available system commit before attributing the cause.

### Historical operation result after reconnect

wait_operation reads a saved result without replaying CAD and reports
result_history.from_journal. COMPLETED is execution status, not an error code.
OPERATION_RESULT_EXPIRED, OPERATION_RESULT_CORRUPT and OPERATION_NOT_FOUND are
distinct; OPERATION_ASSET_MISSING/CHANGED means image evidence is unavailable,
not that the original CAD outcome changed. Recheck delivery hashes for current
acceptance. An owner lost before completion still requires OUTCOME_UNKNOWN recovery.

### Feature reported DEAD, "0 faces" / auto-deleted
`advanced_*` returns `success:false`, message "created a DEAD feature (0 faces)".
Causes, in order of likelihood:
1. **Wrong flags** (Boss vs Cut semantics). → Set `auto_flags=true`, or flip
   `direction_flip` / `offset_reverse` / `flip_start_offset` (see the flag table
   in `patterns.md`). The tool already deleted the dead feature.
2. **Reference ray missed** for `up_to_surface`/`offset_from_surface`. → Check
   `ref_face_ray` origin/direction; make the target face's body visible
   (`show_body`); widen `radius`. Confirm with `probe_ray` that the ray hits.
3. **Open/self-intersecting profile.** → Re-check the sketch is a single closed
   loop (`get_sketch_status`); rebuild with `sketch_contour` and `add_to_db=true`.
4. **Freeze Bar** (if *every* feature is dead — see below).

### Every new feature is dead / type shows "ICE"
**Freeze Bar** at the end of the tree freezing all API features. →
`fix_freeze_bar`, then retry. If the model is already in a bad rollback-like
state (bodies stopped displaying), recover with `close_document(save=false)` +
`open_document(...)` — the disk copy is intact — then `fix_freeze_bar`.

### `COM 61704 "Internal application error"` on extrude/cut
Feature API called via **dynamic dispatch**. → Use the native `advanced_*` tools
(they use typed makepy). In `execute_python`, wrap:
`fm = typed(doc.FeatureManager,"IFeatureManager")`. See `patterns.md` §3.

### `-2147417851 "server threw an exception"` on a custom view
`IMathUtility.CreateTransform` via dynamic dispatch. → Use `set_view_orientation`
(it wraps typed `IMathUtility` and marshals the array as `VARIANT(VT_ARRAY|VT_R8)`).

### A cut damaged / carved into another body
Cut had **no feature scope**. → `advanced_cut(..., scope_bodies=["TargetBody"])`
(Mark=8). The legacy `cut_extrude` has no scope — don't use it in multibody parts.
Verify the untouched body with a probe afterward.

### A cut/extrude produced an unexpected extra body or cut the wrong region
Wrong flag combination selecting the wrong side/region (`flip_start_offset`
with `start_condition="offset"` is the classic: the start walks to +Z when you
meant −Z). → Read `data.feature_bbox` — it tells exactly where the feature
landed. Then either fix the flag, or rerun with
`expected_bbox={"min":…,"max":…}` (auto-rollback outside the zone), optionally
`auto_flags=true` to search for the combo landing in the right zone. Roll back
leftovers with `delete_feature(name, delete_absorbed=true)`.

### Bodies unexpectedly merged (bodies 3 → 2, a body name vanished)
`merge=true` silently swallows a neighbour whose faces coincide (zero-gap
door-on-frame). → The result data lists `merged_bodies` — check it after every
merge in a multibody part. Prevent it structurally: pass
`expected_merge_bodies=["OnlyIntendedBody"]` (auto-rollback + error naming the
offenders), add a clearance, or build with `merge=false`.

### feature_name did not apply / a default-named feature appeared in the tree
Name collision: SW silently keeps `Cut-Extrude1` when the requested name is
taken; old builds reported success with the requested name. → Current tools
return the ACTUAL `feature_name` + `rename_warning` and auto-suffix (`_2`).
Read `data.feature_name` — never assume the requested name stuck.

### Extrude fails with 103 on a contour that "looks fine"
Open or self-intersecting profile. → `sketch_contour` now validates at draw
time: `open_endpoints` = chain breaks; connected chain but
`closed_contours: 0` = self-intersection, almost always an arc drawn to the
wrong side — flip that segment's `direction` (±1). Fix the contour, don't
fight the extrude.

### Pattern of a base body produced nothing
Patterning a lone base **solid body** (no cuts/bosses) does not materialize on
SW2026 (API edge case). → Pattern the cut/boss **feature** instead of the body,
or investigate `BodyLinearPattern` via `execute_python`.

### Pattern/mirror reported success but you're unsure
Their `GetFaces` is unreliable (0 even on success), so the tool verifies by
**total-face-count delta**. Trust `bodies_before/after` and `total_faces_*` in
`data`; confirm visually with a screenshot.

---

## Bodies & appearance

### Body turned BLACK after setting transparency (old builds)
The old `set_body_transparency` wrote the material array as a raw Python list —
dynamic dispatch marshals it corrupted (RGB → zeros) while reporting success.
→ Fixed: writes go through `VARIANT(VT_ARRAY|VT_R8)`, change only index [7]
and are verified by read-back. **Repair an already-black body** with
`set_body_color(name, [200,200,205])`. In `execute_python`, never assign a raw
list to `MaterialPropertyValues2` — see `patterns.md` §4.

### Two separate bodies overlap and nothing reports it
By design: SW never warns about body-body interference inside a part
(print-in-place parts with `merge=false`). A hinge cradle intersecting the pin
neck survived every listing/screenshot check in one session. →
Use `inspect_model(body_pairs=...)` for the affected fit, with clearance bounds
from the actual design/process. Read each tri-state result; containment counts
as overlap. Nozzle diameter alone does not define a universal clearance.

---

## Tree / traversal / documents

### `'int' object is not callable` / traversal stops after 1 item / `Member not found (-2147352573)`
The `callable()`-on-COM antipattern, or calling a **property** as a method
(`GetFirstDocument()`, `GetNextFeature()`, `GetTypeName2()`). → Use `com_get`
(property first, method fallback). These were the root cause of the old
`list_features`/`list_open_documents` breakage; the built-in tools are fixed —
this only bites in your own `execute_python`.

### `get_document_info` / `list_open_documents` behave oddly
Check the running server version and `server.code_changed_since_start`. Update your
installed server checkout and reconnect the MCP client to load the new code.

### Deleted a feature but its sketch is orphaned
`delete_feature` without absorbing. → `delete_feature(name, delete_absorbed=true)`
(explicitly removes the absorbed sketch; the SW `swDelete_Absorbed` flag alone
leaves it orphaned on SW2026).

---

## Sketching

### Sketch lines are invisible until I click a sketch tool
Old `DisplayWhenAdded=False` + `AddToDB` bug — geometry was in the DB but not
drawn until a rebuild. Fixed in `sketch_contour` (it issues `GraphicsRedraw2`). If
you draw via raw `execute_python`, call `doc.GraphicsRedraw2()` yourself (not
`EditRebuild3`, which closes the sketch).

### Sketch plane isn't facing the screen (hard to watch the build)
API-created sketches aren't auto-oriented. → `normal_to_sketch` (the
`create_sketch*`/`sketch_contour` tools already do this automatically).

### Contour doesn't match the reference outline
You likely used `draw_*` (sketch-local 2D) instead of `sketch_contour` (model
coords). → Rebuild with `sketch_contour`, passing real model `(x,y,z)` points and
`add_to_db=true` to avoid inference snapping.

---

## Selection / rays

### "Ray missed — no face selected"
1. Target body **hidden** — probes/selection hit only visible bodies. →
   `show_body(target)`.
2. Origin inside the body or direction sign wrong. → Move the origin outside and
   point inward; verify with `probe_ray` first.
3. Tolerance too tight. → Increase `radius` (e.g. 0.05).

### `SelectByID2` by coordinates fails intermittently
Coordinate tolerance (~0.01 mm) at rounded values. → Prefer ray selection
(`ref_face_ray`, `start_face_ray`, `edge_rays`, `select_face_by_ray`).

---

## Environment

### A tool call hangs or silently does nothing
A **modal dialog** may be blocking SolidWorks. → Call `get_environment_status`
and inspect `modal_dialog`. A pre-existing or unknown dialog must be inspected
and cleared by a human. Do not send generic Enter/Escape. For dimension creation,
use the causally scoped process watchdog described in
[`transactions-and-safety.md`](transactions-and-safety.md#ui-watchdog-and-recovery).

### Modify dialog returned after batch dimensioning

The SolidWorks input-on-create preference was not suppressed/restored, or a
dimension escaped the guarded operation. → Use `add_dimensions_batch` with one
rebuild and preference restoration. Verify the returned preference value, the
dimension's driving state/value, no active sketch, and clean reopen. Use the
scoped watchdog only as recovery evidence, not as the normal dimensioning path.

### Operation exceeds the MCP request timeout
→ Nothing to do: every tool call waits at most 45 s and then answers
`status: RUNNING` with an `operation_id`. Call `wait_operation(operation_id)`
until it returns the original result. Do not retry and do not reconcile.

### `SOLIDWORKS_MEMORY_HIGH` warning, or a "critically low on committed memory" dialog
→ SolidWorks keeps memory from every rebuild of a long session. Save, then run
`expert_tools` → `restart_solidworks`. It does not reopen documents: the next
tool call with `document_path` opens the part.
`get_executor_status` shows `memory` without touching COM.

### `STALE_REVISION`
→ The part changed since the revision was read: another edit, a revert or a
manual save. Read `get_model_context` and repeat with its `revision`. Opening
the part under another spelling of its path does not change the revision.

### `NATIVE_REBUILD_FAILED` after an edit or a parameter change
→ The edit was rolled back. `details.features` lists the failing native features
in tree order with `type` and `component`; the first one is the cause, the rest
depend on it. A `Sweep` fails when its tube path bends tighter than the tube
radius (analyze warns with `TUBE_BEND_TOO_TIGHT`, often only at a smaller `H`);
a `Fillet` or `Chamfer` when its size no longer fits. Fix that component and
repeat the edit. `likely_causes` lists only the warnings about the component that
failed; `details.program_warnings` holds all of them.

### A failed call answers "Not executed again: this identical request already failed"
→ An edit that failed is answered from its request receipt when it is repeated
unchanged. Change the program, or pass a new `idempotency_key` to run the same
edit once more. A **create** that failed left no file and simply runs again.

### `NATIVE_REBUILD_STALE`, or `rebuild_mode: forced_twice` in an edit answer
→ SolidWorks answered a forced rebuild without regenerating the equation curves
(seen once in a long session). The server checks three curves after every edit:
`forced_twice` means a second rebuild fixed it and the result is good.
`NATIVE_REBUILD_STALE` means it did not; the saved part was restored. Repeat the
edit with a new `idempotency_key`; if it fails again, `expert_tools` →
`restart_solidworks` and repeat.

### `IDEMPOTENCY_STATE_CHANGED`
→ This exact request (same document, starting revision and payload) ran before
and the document has changed since. Read the current revision with
`get_model_context`; to apply the request to the current state pass a new
`idempotency_key`. From 6.29.1 the same edit after `mode="revert"` simply runs
again.

### `NATIVE_PATH_FAILED` on a `tube` or `variable_tube`
→ SolidWorks rejected one path segment in both parameter directions
(`details.feature`, `details.component`). Repeat the call once; if it fails
again, move one point of that tube by 0.01 mm.

### `NATIVE_BODY_INVALID` warning after create or edit
→ The kernel reports faults in a body: `export_model` will refuse it and
`body_pairs` answers `UNKNOWN` for it. `components` names the owners of the
faulty faces. Rebuild that component differently or `mode="revert"`.

### An insert touches its pocket, or slivers at the edge of a recess on a curved host
→ The `surface_patch` mask is almost as wide as the host (warning
`SURFACE_PATCH_WIDE_MASK`): its walls leave the surface at a shallow angle and
pocket and insert both end at the back plane of the mask. Keep the mask under
about 0.7 of the host width, or build the inlay as a ring intersected with a
sector: `model_api_lookup("template:curved_inlay")`.

### A straight cone or taper looks like an hourglass
→ A twisted loft (fixed in 6.28.0 for `sectioned_solid`, in 6.29.0 for
`variable_tube`). `compare_reference(..., source="native")` shows it as a waist
that the draft does not have. Report it.

### `server.code_changed_since_start: true`
→ The MCP process is older than the server code on disk. Reconnect the MCP
server in the client; until then the old behaviour applies.

### `SESSION_QUARANTINED`
→ `get_executor_status` names the unresolved operation and the cause. With
`owner_alive: false` (the MCP process died during a COM call) run
`expert_tools` → `restart_solidworks`, then `reconcile_session(operation_id)`.
The restart refuses while unsaved documents are open and lists them.

### `MODAL_DIALOG_BLOCKING` with nothing visible
→ Tooltips and shadows no longer count as dialogs. `expert_tools` →
`get_environment_status` is never blocked and lists the real dialog in
`modal_dialog.window_details`.

### Vector trace follows a pale compression halo

The selected pixels represent antialiasing/background rather than the object.
→ Use `image_mode="filled_silhouette"`,
`trace.mode="outer_silhouette"`, and a deep segmentation/matting backend.
Inspect the analysis mask before fitting. Add positive/negative prompts or an
ROI; do not fix segmentation by increasing curve density. See
[`vectorization.md`](vectorization.md).

### Vector trace is a mess of curves

The semantic mode is too broad (`all_visible_edges`) or fitting budgets are
unbounded. → Select the intended outer/component contour, apply real-world
minimum-feature and entity caps, and tune smoothing/max error together. Preserve
characteristic corners with the corner threshold rather than uniformly creating
short segments.

### Reference comparison looks good but fails scale or placement

An overlay was bbox-fitted or used an unverified pixel transform. → Supply the
explicit 3x3 affine transform and `mm_per_pixel`; reject scale conflicts. Bbox
fit is diagnostic only. See
[`verification-and-export.md`](verification-and-export.md).

### Export bundle failed or an existing file changed unexpectedly

Inspect the structured stage, rollback data, staged structural reports, and
manifest hashes. With `overwrite=false`, an existing target must fail preflight
with no hash changes. Do not deliver partial manually exported files; rerun the
atomic bundle after correcting the invariant or target policy.

---

## Screenshots as a debugging tool

Two distinct modes — use the right one:

### Part/model screenshot (default, `full_window=false`)
Captures just the 3D model view via `SaveAs3`, PIL-compressed to save tokens.
Use for **geometry** checks:
- Straight named views (`front`, `isometric`, …) to judge shape/proportions.
- **Transparency overlay** to compare result vs. reference: make the result body
  ~0.6 transparent over the visible reference and shoot `front` (see
  [workflows.md](workflows.md#existing-reference-body)). Misalignment is obvious.
- **Internals** (a hinge inside a pocket): transparency overlays turn to mush —
  use `section_screenshot` (plane + offset + `zoom_to_bodies`) instead.
- Custom `view_direction`/`up_direction` to match a reference photo's angle.
- **Frame with `zoom_to`/`zoom_to_bodies`**, not hand-typed zoom coordinates.
- **Tune the shot**: `hide_body`/`set_body_transparency` occluders; re-orient and
  re-shoot until the region is unambiguous. Don't judge from a rough default view.

### Screenshot came back `frame_unreadable: true`
≥95% of pixels are 1–2 tones: a black frame (body-colour corruption — see
"Body turned BLACK"), or a solid fill (zoomed point-blank into one face).
**The frame proves nothing — do not "verify" by it.** Fix the colour
(`set_body_color`) or the framing (`zoom_to_bodies`/`zoom_to_fit`), reshoot.

### Full-window screenshot (`full_window=true`)
Captures the **entire SolidWorks application window** — ribbon, FeatureManager
tree, status bar, and **any open dialog** — by grabbing the SW frame's screen
region and raising it to the front. Use for **UI/state** debugging:
- See a **stuck modal dialog** that's swallowing API calls.
- Inspect the **Freeze Bar** position in the tree (the blue line) and whether the
  model is in a rollback-like state.
- Read an on-screen **error/warning banner** the API didn't surface.
- Confirm which document/body is actually active.
Caveat: it grabs the screen region, so overlapping windows appear in the image —
close or move them first. Requires Pillow (PIL) installed.

Rule: **geometry wrong → part screenshot + overlay; tool stuck/silent → full-
window screenshot + `get_environment_status`.**
