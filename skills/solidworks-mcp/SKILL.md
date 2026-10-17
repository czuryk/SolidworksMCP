---
name: solidworks-mcp
description: >-
  Build, edit, inspect and export native SolidWorks models through solidworks-mcp.
  Use for CAD modeling, raster-to-sketch work and SolidWorks COM/MCP diagnostics.
  Read before calling a SolidWorks MCP tool.
---

# SolidWorks MCP

A model is a short Python shape program plus the native solids it compiles to.
Coordinates are mm: Y up, X width, Z depth. The compiler owns sketches,
selections, native equations, checkpoints and saving.

## Tools

| Tool | Use |
|---|---|
| `get_model_context` | Start here: active document, revision, parameters, bodies with exact extents. `components=[names]` or `detail="full"` adds component records, `include_source=true` the embedded program |
| `model_api_lookup` | Constructors, topics, examples; `topic="all"` once instead of many lookups; `"templates"` and `"template:<name>"` for vetted joints |
| `execute_model_code` | `mode`: `analyze` (draft, no SolidWorks), `create`, `edit`, `revert` |
| `compare_reference` | Model silhouette against a reference image, per orthographic view: draft before the build, `source="native"` before delivery |
| `measure_reference` | Extents of single elements in a reference image; `color=[r,g,b]` for an element inside the figure |
| `inspect_model` | Numbers from the built bodies: extents, fits (`body_pairs`), sections, `printability=true` |
| `observe_model` | Native views, close-ups (`components`), custom cameras |
| `export_model` | Checked SLDPRT, STEP, one STL per body, manifest |
| `wait_operation` | Collect an operation that answered `RUNNING` |
| `get_executor_status` | COM-free: busy or quarantined, server version, SolidWorks memory |
| `reconcile_session` | Only for a quarantined session (unknown outcome) |
| `get_request_status` | Receipt of an earlier mutation by its key |
| `expert_tools` | Everything outside the constructors: `list`, `describe`, `call` |

Tools that take `document_path` activate that part or open it when it is closed.
The revision of a saved part does not depend on how its path is spelled.

## Draft first, build once

A native build takes minutes, a draft milliseconds.

1. `get_model_context`, then `model_api_lookup(topic="all")` once. Its `pitfalls`
   topic lists behaviour that otherwise costs a probe part. `*` marks keyword-only
   arguments. Helpers and loops work with the injected `model`; no imports.
2. Read the references as volumes and write the whole program with a few named
   parameters. For a typical joint start from a template: `model_api_lookup("templates")`
   lists them, `"template:hinge"` returns code that builds as written. Repeat a
   body with `model.pattern`, not a loop.
3. `execute_model_code(mode="analyze", code=..., draft_views=["front", "right"])`
   returns the predicted extents of every body, silhouette images and `warnings`.
   The interpreter allows **512 components**, including consumed nodes, patterns,
   placements and Booleans. Check `component_count`, `max_components` and
   `remaining_components`; `model_api_lookup(topic="limits")` gives active limits.
   The 6.30.0rc1 fixture matrix passed on SOLIDWORKS 2026 SP2.1: 128/256/512 nodes
   across straight, curved, loft and lattice lifecycles. This covers the tested
   fixtures; inspect each new native model and heed memory warnings. Nested patterns
   also have an expanded-complexity warning and a
   separate draft point budget; shared arrays count once, transformed copies separately.
4. With reference images call `compare_reference(image_path, view, code=...,
   height_mm=...)` per orthographic view: IoU, a table of left/right extents per
   height level with deviations in mm (worst first) and an overlay. Correct the
   reliable proportion mismatches. If a mismatch follows a documented draft
   limitation (estimated bodies or unsupported cuts), record it and verify it
   natively after building. Do not loosen tolerance to pass the draft; keep the
   agreed tolerance for the final native check.
5. `mode="create"` with a **new absolute .SLDPRT path**. Read the returned
   Front/Right/Isometric views and the warnings.

Do not build probe parts to learn extents or proportions. Tracing, measurements
and extra preflight calls are not a prerequisite to the first volume.

## Warnings in analyze, create and edit answers

| Warning | Meaning | Do |
|---|---|---|
| `TUBE_BEND_TOO_TIGHT` | A tube path bends tighter than the tube is thick; the sweep may build and then fail when a parameter scales the model | Spread the named path points or reduce the radius before building |
| `SURFACE_PATCH_WIDE_MASK` | A mask almost as wide as its curved host: slivers, and pocket and insert end at the same plane | Narrow the mask or use template `curved_inlay` |
| `NATIVE_BODY_INVALID` | The kernel rejects a built body; `export_model` will refuse it | Change the named components or revert |
| `SOLIDWORKS_MEMORY_HIGH` | SolidWorks keeps memory from every rebuild | Save, then `expert_tools` → `restart_solidworks` |

## Edit

Use a fresh `document_path`/`expected_revision` and one of: a **patch**
(`patch=[{"old": ..., "new": ...}]`, exact replacements in the embedded program,
each `old` exactly once; preferred, because nothing else can change), a complete
revised program in `code`, or only `parameters`. Every edit costs a full native
rebuild: put related changes into one patch.

- The answer lists `rebuilt_components`, `added_components`,
  `removed_components` and `timings_ms`: confirm that only the intended parts
  changed. `mode="analyze"` with `document_path` previews that plan and the new
  draft without mutation.
- A failed edit restores the saved part. `NATIVE_REBUILD_FAILED` names the
  failing native feature, its component and the likely cause; follow
  `recommended_actions`.
- `mode="revert"` restores the state before the last edit in seconds, up to
  three edits back.
- `rebuild_mode: forced_twice` is a good result: SolidWorks left curves stale
  after the first rebuild and a second one fixed it. `NATIVE_REBUILD_STALE`
  means it did not; the part was restored, repeat the edit with a new
  `idempotency_key`.
- After a restart `include_source=true` retrieves the embedded program.
  `check_edit_compatibility=true` is useful after manual feature additions.
- Keep dimensions symbolic so the saved model rebuilds without Python.

## Check

`inspect_model` answers a concrete geometric question; `body_pairs` checks a fit
without helper sketches. Read each `passed` value (true/false/null). For a
printed part add `printability=true`: whether the assembled model stands on its
base (`support.stable`, `margin_mm`, `tip_angle_deg`) and the thinnest wall of
every body; `thin_spots` gives place, thickness and area, `passed=null` marks
lesser thin places such as the rim of a hole in a sloping face.

Any positive overlap fails a clearance criterion, even when its numeric bounds
include zero. For intentional overlaps, omit clearance bounds and inspect
`relation` and `intersection_volume_mm3`.

**Before delivery a numeric check is required.** Run
`compare_reference(image_path, view, document_path=..., source="native")` for
every approved view (the silhouette of the bodies as built) and report its table.
For single elements (eyes, mouth) add one `measure_reference` batch with
`expected_mm` and `tolerance_mm`. Screenshots judged by eye do not replace this.

`observe_model(components=[...])` gives a close-up and restores the working
camera; it accepts custom cameras (`{"name", "direction", "up"}`) and `flat=true`
for predictable file paths. A tall model is small in the default frame: use
`width=2000` or close-ups. The compiler frames completed components by itself.

## Deliver

`export_model` to a **new directory**, or with `replace=true` over its own
earlier delivery. Bodies are written as `stl/<BodyName>.stl`. It checks
native/STEP/STL structure and preserves the source. Likeness, parameter cycling,
clearances and print readiness need the checks above. Strict STL validation is
the default; read the delivery reference before opting into mesh cleanup.

## Long operations

A call waits at most 45 s. A longer operation answers `status: RUNNING` with an
`operation_id`: call `wait_operation(operation_id)` until it returns the original
result, then continue with its revision. This is not an error; do not retry and
do not reconcile. Meanwhile other CAD tools answer `CAD_SESSION_BUSY`.

Typical on a 65-component, nine-body model: create 90 s, edit 20 s, revert 6 s,
`inspect_model` with eight pairs and printability 55 s, export 100-115 s,
native silhouette comparison 4 s.

## Repeating a call

Mutations carry a receipt (`request_receipt`); the key is derived from document,
starting revision and payload unless `idempotency_key` is given.

| Situation | What the same call does |
|---|---|
| It succeeded and the document is still in that state | Answered from the receipt (`replayed: true`), nothing runs |
| A create failed and left no file, or the part was deleted | Runs again (`attempt: 2`) |
| The edit was undone with `mode="revert"` | Runs again |
| The edit failed | Not executed again; the message says so. Change the program or pass a new `idempotency_key` |
| The document changed to another state | `IDEMPOTENCY_STATE_CHANGED`: read the current revision and use a new key |

## Session care

- Only a quarantined session (`get_executor_status`: `quarantined: true`) has an
  unknown outcome: resolve it with `reconcile_session(operation_id)` before
  another mutation; do not bypass it with a new request key. With
  `owner_alive: false` first run `expert_tools` → `restart_solidworks`.
- `restart_solidworks` refuses while unsaved documents are open and does not
  reopen documents; the next tool call with `document_path` opens the part.
- `server.code_changed_since_start: true` means the MCP process is older than
  the code on disk: ask the user to reconnect the MCP server.
- Failed calls carry `recommended_actions` with the exact tool and arguments to
  run next. Unknown modal dialogs are not auto-confirmed.
- `expert_tools` → `get_session_report` summarises a run: calls, failures,
  answer sizes, slowest operations.

`wait_operation` and executor status expose numeric progress for the current
phase, including `seconds_without_progress`. A heartbeat is not proof of an
advance. `expert_tools` → `get_session_metrics` is COM-free while CAD is busy;
executor status includes the last/current tessellation method and elapsed time.
Completed results survive MCP reconnect for a bounded retention period and carry
`result_history.from_journal`; historical success does not verify today's file
hashes. RPC loss keeps the original HRESULT and does not trigger an automatic retry.

For a union of separated supports connected by a platform, put the platform first.
The compiler can attempt one verified reorder after an unchanged native failure;
read its connectivity diagnostics on failure. Never reorder difference inputs or
treat overlapping bounding boxes/point contact as proof of one valid solid.

## Outside the constructors

`expert_tools`: `list` with a query, `describe` for the exact schema, then `call`
with nested `arguments`. `query` is a case-insensitive literal substring of
the tool name or description: use one keyword or a tool name. For body colour
and transparency, use `set_body_color` / `set_body_transparency`; see the
[expert routing example](reference/tools.md). Use the replacement of a `DEPRECATED` tool
(`get_job_result`, `execute_python_async` and `list_jobs` are replaced by
`wait_operation`, `execute_python` and `get_executor_status`). Raw COM uses
**metres**. For expert or raw local edits use `zoom_to` on the target before
editing and after its extent changes. Read only the reference relevant to the
current need:

| Need | Reference |
|---|---|
| Shape choices, symbolic code and edits | [shape-programs.md](reference/shape-programs.md) |
| Choosing the shortest workflow | [workflows.md](reference/workflows.md) |
| Export acceptance and mesh diagnostics | [model-delivery.md](reference/model-delivery.md) |
| Expert routing and deprecated replacements | [tools.md](reference/tools.md) |
| Raster conversion | [vectorization.md](reference/vectorization.md) |
| Expert transactions or dialog recovery | [transactions-and-safety.md](reference/transactions-and-safety.md) |
| Typed COM and selection quirks | [patterns.md](reference/patterns.md) |
| Calibrated comparisons / expert export | [verification-and-export.md](reference/verification-and-export.md) |
| Live camera diagnostics | [working-view.md](reference/working-view.md) |
| A specific failure | [troubleshooting.md](reference/troubleshooting.md) |
