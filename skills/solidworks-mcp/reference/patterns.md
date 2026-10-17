# SolidWorks 2025/2026 COM Patterns & Gotchas

Why these matter: each cost a real debugging session. The server already
encapsulates them, but you need to recognize the symptoms — and you must
re-apply them by hand inside `execute_python`.

## Contents

1. [Units](#1-units-the-api-is-in-meters)
2. [Properties and methods](#2-property-vs-method--never-callable-on-a-com-object)
3. [Typed wrappers](#3-typed-makepy-wrappers-are-mandatory-for-feature-creation)
4. [VARIANT arguments](#4-variant-for-null-and-out-parameters)
5. [Freeze Bar](#5-the-freeze-bar-silently-kills-api-features)
6. [Dead-feature detection](#6-dead-feature-detection--getfaces-not-gettypename2)
7. [Boss and Cut flags](#7-verified-boss-vs-cut-flag-combinations)
8. [Selection marks and rays](#8-selection-marks-used-by-feature-creating-apis)
9. [Model and sketch coordinates](#10-modelsketch-coordinates-sketch_contour)
10. [Verified working view](#10a-verified-working-view)
11. [Return-value and naming quirks](#11-apis-that-return-a-bool-instead-of-the-feature)
12. [Modal dialogs](#13-modal-dialogs-swallow-api-calls)
13. [Long operations](#14-long-operations--the-mcp-timeout)
14. [Native tessellation](#15-native-tessellation-for-body-silhouettes)

---

## 1. Units: the API is in METERS

SolidWorks COM takes/returns **meters** for every length. Native tools convert
from your `unit` (default mm) automatically. Inside `execute_python` you convert
yourself:

```python
x_m = 30.53 * 0.001                       # or:
x_m = automation._units.to_meters(30.53, "mm")
mm  = automation._units.from_meters(x_m, "mm")
```

Angles are in **radians** in the API (`math.radians(deg)`).

---

## 2. Property vs. method — never `callable()` on a COM object

In SW 2025/2026 dynamic dispatch, members like `FirstFeature`, `GetNextFeature`,
`GetTypeName2`, `GetFirstDocument`, `GetNext`, `IsSuppressed`, `RevisionNumber`
are exposed as **properties**, not methods. The classic antipattern
`if callable(x): x = x()` is fatal: **every live win32com dynamic object
implements `__call__`, so `callable()` is always `True`** — it then "calls" the
property and returns garbage/empty, silently truncating tree traversal (the bug
that made `list_features` return 1 feature instead of 20, and
`list_open_documents` crash with `Member not found`).

Rule: use the `com_get(obj, name, *args)` helper — property access first, method
fallback via `_FlagAsMethod`. Available in `execute_python`:

```python
feat = com_get(doc, "FirstFeature", default=None)
while feat is not None:
    name = com_get(feat, "Name", default="?")
    typ  = com_get(feat, "GetTypeName2", default=None)
    feat = com_get(feat, "GetNextFeature", default=None)
```

Symptoms of getting this wrong: `'int' object is not callable`,
`Member not found (-2147352573)`, traversals that stop after one element.

---

## 3. Typed makepy wrappers are mandatory for feature creation

`FeatureExtrusion3` / `FeatureCut4` (and `IMathUtility.CreateTransform`,
`GetModeler`) via **dynamic dispatch raise COM `61704` "Internal application
error"** (or `-2147417851` "server threw an exception"). They only work through a
**typed makepy** interface:

```python
fm = typed(doc.FeatureManager, "IFeatureManager")   # helper in execute_python
feat = fm.FeatureExtrusion3(...)                     # now works
```

**Members that simply DO NOT EXIST in dynamic dispatch** (`Member not found
-2147352573`), typed wrapper is the only path — live-verified list:
- `ISldWorks.GetModeler`
- `IModelDocExtension.CreateMeasure` (then `measure.Calculate(None)` works on
  the typed object; `Distance` is in meters, `IsIntersect` bool)
- `IModelViewManager.CreateSectionViewData` / `CreateSectionView` /
  `RemoveSectionView`

Notes:
- `EnsureDispatch` fails on SW objects ("cannot automate the makepy process") —
  the server generates the module from `sldworks.tlb` and wraps raw
  `obj._oleobj_`. Done once per process (`get_typed_module`).
- After makepy is cached, `Dispatch()` starts returning **typed** objects where
  `GetTitle`/`FirstFeature`/… become **methods**, breaking all property code. So
  the app is deliberately **force-wrapped back to dynamic**; typed wrappers are
  taken pointwise via `typed()`. Keep this in mind if you `Dispatch` yourself.
- The `advanced_*` / `revolve_boss` / `shell` / pattern / mirror tools already do
  this — you only need it in raw `execute_python`.

---

## 4. `VARIANT` for null and out-parameters

- **Null COM-object argument** (e.g. `SelectByID2`'s `Callout`): pass
  `VARIANT(VT_DISPATCH, None)` — plain `None` gives `Type mismatch, param 8`.
  Hidden inside the `select_by_id2` helper.
- **Out-parameters** (`Save3`, `Extension.SaveAs` errors/warnings): use
  `VARIANT(VT_BYREF | VT_I4, 0)` placeholders and read `.value` after.
- **SAFEARRAY of doubles** (`CreateTransform`): `VARIANT(VT_ARRAY | VT_R8, [...])`.
- **Array PROPERTY writes marshal corrupted without VARIANT.** Writing
  `Body2.MaterialPropertyValues2 = (0.62, 0.62, 0.64, …)` as a raw Python
  tuple/list "succeeds" but the stored array is garbage: RGB reads back as
  zeros (black body) or `1.95e-310`. Confirmed live on SW2026; write
  `VARIANT(VT_ARRAY | VT_R8, vals)` and **verify by reading the array back**.
  `set_body_transparency` / `set_body_color` do this (and change only the
  slots they own: transparency = index [7], RGB = [0..2]).

---

## 5. The Freeze Bar silently kills API features

If the Freeze Bar sits at the end of the tree, **every new API feature is created
frozen: 0 faces, no geometry, no error** (it reports type "ICE"). The server
calls `ensure_features_not_frozen()` on connect and before every feature op:
`SetUserPreferenceToggle(swUserEnableFreezeBar=461, False)` +
`EditFreeze(swMoveFreezeBarToTop=4, "", True)`. Manual tool: `fix_freeze_bar`.

Pitfalls learned the hard way:
- The move codes are `swMoveFreezeBarTo*`: ToEnd=1, Before=2, After=3, **ToTop=4**
  (an earlier wrong `EditFreeze(0,...)` did nothing).
- Dragging the bar in the UI can push the model into a rollback-like state where
  bodies stop displaying.
- A modal **Options** dialog open on screen swallows the API toggle.
- If the model is already corrupted, recover with
  `close_document(save=false)` + `open_document(...)` (disk copy is intact).

---

## 6. Dead-feature detection — `GetFaces`, not `GetTypeName2`

A feature with **`GetFaces == 0` is dead** (SolidWorks creates empty features for
infeasible parameters). `GetTypeName2` is **unreliable** for health — it returns
"ICE" even for healthy extrusions via dynamic access. All feature tools verify
via `feature_face_count()` and delete dead features (`auto_verify`).

**Patterns and mirror report `GetFaces` unreliably too** (0 even on success), so
`_verify_and_finish` treats them as alive if `GetFaces > 0` **OR** the model's
total face count changed (delta signal).

Some features legitimately have 0 faces and are never "dead": `ProfileFeature`
(sketches), `RefPlane`, `RefAxis`, origin, folders.

---

## 7. Verified Boss vs. Cut flag combinations

Boss and Cut interpret `direction_flip` (Dir), `offset_reverse` (OffsetReverse1)
and `flip_start_offset` differently. When in doubt, set **`auto_flags=true`** and
read the returned `chosen_flags`. Confirmed live on SW2026:

| Intent | Tool | `direction_flip` | `offset_reverse` | `flip_start_offset` |
|--------|------|:---:|:---:|:---:|
| Boss, offset-from-surface (into wall) | `advanced_extrude` | false | false | — |
| Boss, up-to-surface | `advanced_extrude` | false | — | — |
| Cut, offset-from-surface (same side as the Boss above) | `advanced_cut` | **true** | false | — |
| Cut, start outside then cut inward to a surface offset | `advanced_cut` | false | — | **false** (with `offset_reverse=true`) |
| `through_all` on a sketch lying on the bottom face | either | **true** | — | — |

Rule of thumb: if a feature is dead or lands on the wrong side, first flip
`direction_flip`; for offset/up-to-surface conditions also try `offset_reverse`;
for `start_condition="offset"` also try `flip_start_offset`. `auto_flags`
enumerates exactly the relevant subset.

**Flag facts confirmed in later sessions:**
- `flip_start_offset` controls the **direction of `start_offset`** for
  `start_condition="offset"`; with the default the start can move to +Z when
  you meant −Z, planting the feature in the wrong material. `auto_flags` finds
  a *live* variant, which may still be the wrong side geometrically.
- The cut direction at `direction_flip=false` **differs between a
  lines-only contour and a contour containing arcs** (the contour normal ends
  up different).
- Because of both: check `data.feature_bbox`; for an intersecting scoped cut,
  prefer `data.newly_affected_region_bbox` and retain
  `data.connected_result_face_bbox` as topology context. Pass `expected_bbox`
  to guard the affected region when available (auto-rollback if outside).
  `auto_flags=true` +
  `expected_bbox` searches for the combo that is live AND in the right zone —
  this closes the class of "false merge / wrong side" failures that once cost
  ~10 blind iterations.

---

## 8. Selection marks (used by feature-creating APIs)

The mark tags a selection so the feature API knows its role.

| Mark | Meaning | Where |
|:---:|---------|-------|
| 0 | Profile sketch | extrude/cut/revolve profile |
| 1 | End-condition reference face (dir-1) | `ref_face_ray` up_to/offset_from_surface |
| 2 | Mirror plane; pattern direction-2 | mirror / linear_pattern dir2 |
| 4 | Pattern seed feature(s); dir-2 end ref | linear/circular_pattern seeds |
| 8 | **Cut feature-scope body** | `advanced_cut` `scope_bodies` |
| 32 | Start-condition reference face | `start_face_ray` start_condition="surface" |

**Cut feature scope requires Mark=8 + `UseFeatScope=True` + `UseAutoSelect=False`**
— with Mark=2 the feature is not created at all. Pattern marks: direction entity
=1 (dir2=2), seed features=4. Mirror: plane=2, features=1, faces=128,
bodies=256. Body mirror requires `IBody2.Select2(append, SelectData)` with
`SelectData.Mark=256`; feature `Select2` takes an integer mark instead.

---

## 9. Ray selection beats coordinate selection

`SelectByID2` by coordinates is brittle (tolerance ~0.01 mm at rounded values).
`SelectByRay` is robust and is how the server picks reference faces/edges. A ray
is `{"origin":[x,y,z],"direction":[dx,dy,dz]}` (user units; direction
auto-normalized). **It hits only visible bodies** — `show_body` the target,
`hide_body` occluders. Ray probing uses `SelectByRay` + `GetSelectionPoint2`
because `IModeler.GetRayIntersections` is absent from the SW2026 typelib.

---

## 10. Model↔sketch coordinates (`sketch_contour`)

`sketch_contour` transforms your model-coordinate points into the sketch plane
via `ModelToSketchTransform.ArrayData` (16 doubles, row-major rotation r11..r33,
translation at [9..11], scale at [12]; row-vector convention). From the same
transform: sketch **+Z axis = ArrayData[2,5,8]**, sketch **+Y = ArrayData[1,4,7]**
— used to auto "Normal-To" the view.

The old "invisible sketch lines" bug was `SketchManager.DisplayWhenAdded=False`
paired with `AddToDB=True`: geometry entered the DB but wasn't drawn until a
rebuild. Fixed by removing that and issuing `GraphicsRedraw2()` (not
`EditRebuild3`, which would close the sketch).

---

## 10a. Verified working view

Never implement active-sketch Normal To as an unconditional `Ctrl+8` or command
169 call. SOLIDWORKS can toggle an already aligned view to the opposite side.
Read `IModelView::Orientation3`, derive the sketch basis from
`ISketch::ModelToSketchTransform`, choose the nearest side/up, and verify all
three axes after every native or matrix attempt. `solidworks-mcp` encapsulates
this in `normal_to_sketch` and the sketch mutation guards.

Fit is also a postcondition, not a fire-and-forget command. Measure the active
geometry through `IModelView::Transform` and the view frame. On SW2026,
`ViewZoomTo2` can over-zoom a flat 3D box, and changing `Scale2` silently changes
the zoom pivot in `Translation3`; preserve and restore that translation before
the final measurement. Read [`working-view.md`](working-view.md) for the exact
contract, diagnostics, examples, thresholds, and limitations.

---

## 11. APIs that return a bool instead of the feature

`InsertFeatureShell` and `InsertAxis2` return a **bool**, not the created
feature. Fetch the newest feature with `FeatureByPositionReverse(0)` and confirm
its `GetTypeName2` (`"Shell"` / `"RefAxis"`). The server handles this internally.

---

## 11a. Renames silently fail on name collision

Setting `feat.Name = "X"` when a feature named `X` already exists **does
nothing and raises nothing** — SW keeps the default name (`Cut-Extrude1`) while
the old server code reported success with the requested name (a nameless
feature was later found in the tree only by eye). All rename paths now read
back the **actual** `feat.Name`, auto-suffix on collision (`X_2`) and return
`rename_warning`. In `execute_python`, never trust an assignment to `.Name` —
read it back.

---

## 11b. Bodies merge and overlap silently

Two facts that cost a whole session each:
- **`merge=true` swallows zero-gap neighbours.** A boss added to body A whose
  faces coincide with body B merges B in as well — the only trace is
  `bodies 3 → 2`. The tools now return `merged_bodies` (names) and accept
  `expected_merge_bodies` (auto-rollback if anything else merged).
- **Separate bodies (merge=false) may overlap with NO warning anywhere.** For
  print-in-place designs the collision is invisible to every listing/screenshot
  tool. `check_clearance(a, b, min_clearance=…)` is the check: IMeasure min
  distance + intersection volume via body copies + `Operations2`.

---

## 11c. `Operations2` needs the LARGE swBodyOperationType constants

`IBody2::Operations2` takes `swBodyOperationType_e`: **SWBODYINTERSECT=15901,
SWBODYCUT=15902, SWBODYADD=15903** — mapping VERIFIED LIVE on SW2026
(15901 on disjoint bodies → empty result; 15903 → union lumps whose volume
equals the SUM of both bodies — that false "interference" is how the swap was
caught). Do not trust doc snippets that swap ADD/INTERSECT. Passing a small
int (e.g. `2`) fails with `err=2`. Also: call it on a **temp copy**
(`body.Copy2(False)`), never on the live body; typed `IBody2` handles the
ByRef error out-param (returns a `(bodies, err)` tuple). `check_clearance`
additionally sanity-checks result volume ≤ min(source volumes) — an
"intersection" bigger than a source body means the op didn't intersect.

---

## 12. Meaningless return values (live-verified on SW2026)

- `ShowNamedView2` returns `None` on success (not a bool). Treat **only an
  explicit `False` as failure** (this once broke `set_view_orientation`).
- `IModelViewManager.RemoveSectionView()` returns **`False` on actual
  success** — the section view is verifiably off afterwards. Ignore the
  return; only an exception matters.
- `IModelDoc2.SaveAs3(path_to_image)` can return **`False` while writing the
  file anyway** (seen with Section View active). The reliable success signal
  is "file exists afterwards" (delete any stale file first) — the screenshot
  tools do exactly that.
- Section view data members are named `FirstOffset` / `FirstReverseDirection`
  (NOT FirstPlaneOffset/FlipFirstPlane); showing the section is
  `CreateSectionView(data)` (its bool IS meaningful), hiding is
  `RemoveSectionView()`.

---

## 13. Modal dialogs swallow API calls

An open modal dialog (Options, a Save prompt, an error box) blocks the SolidWorks
main frame and can silently stall COM. Detection identifies the main frame
strictly by its owning `SLDWORKS.exe` process, not by title.

Preflight never closes an existing dialog. It returns a structured UI-blocked
state and requires a human to inspect and clear it. During an explicitly guarded
operation, a process-isolated watchdog may resolve only a strongly classified
`dimension_modify` dialog with matching operation, transaction, request, and
expected text context. It captures a full-window screenshot before sending the
targeted known action. A generic `#32770` window, an unknown dialog, or a weak
causal match is never sufficient for auto-resolution.

See [`transactions-and-safety.md`](transactions-and-safety.md) for the exact
guard contract and post-recovery invariants.

---

## 14. Long operations & the MCP timeout

A tool call waits at most 45 s. A longer operation keeps running on the single
STA owner and the call answers `status: RUNNING` with an `operation_id`;
`wait_operation(operation_id)` returns the original result. This applies to
`execute_python` as well, so `execute_python_async`, `get_job_result` and
`list_jobs` are deprecated (they remain callable through `expert_tools`).

High-level deep analysis and the UI watchdog use isolated worker processes when
available. Keep CAD mutation in the owning COM apartment; pass serialized plans
or analysis results across the boundary, never COM pointers.

---

## 15. Native tessellation for body silhouettes

Use `IBody2`/`ITessellation` mesh data for projected body comparison. Preserve
model-space coordinates, project with the declared standard orientation, and
set mesh deviation below the acceptance tolerance. Do not derive the production
silhouette from a viewport screenshot or mutate the user's global tessellation
preferences. See
[`verification-and-export.md`](verification-and-export.md#compare-a-body-silhouette).
