# Choose the shortest useful workflow

## Model from reference images

Read the images as volumes. Establish overall height, a few major widths/depths,
and relationships, then write the whole program and **draft it before building**:

1. `execute_model_code(mode="analyze", code=..., draft_views=["front", "right"])`
   returns predicted extents, silhouettes and `warnings` in milliseconds. Fix a
   `TUBE_BEND_TOO_TIGHT` or `SURFACE_PATCH_WIDE_MASK` warning before building.
2. `compare_reference(image_path, view, code=..., height_mm=...)` per orthographic
   reference returns IoU and the levels where the silhouette is too wide, too
   narrow or missing, in mm. Fix reliable proportion mismatches and repeat.
3. Create once those mismatches are resolved. If a remaining mismatch follows
   a documented draft limitation, record it and verify it natively after building.
   Do not loosen tolerance to pass the draft; keep the agreed tolerance for the
   final native check.

The draft subtracts a plain box that covers one whole side of its target and
clips an intersection to a box mask, both exactly. Other cuts are not subtracted,
and bodies that depend on native faces are listed in `estimated`; fits and
clearances are checked natively with `inspect_model`.
The compiler handles native construction and returns views. Correct the largest
silhouette mismatch first, then add characteristic details and required fits.
No tracing, calibration sheet or measurement sketch is needed just to begin.

When one proportion is genuinely unclear, use one `measure_reference` batch with
explicit regions. Visible raster extents are estimates; hidden boundaries remain
unknown. A Front/Right pair explains volume better than dozens of local 2D
measurements. Do not optimize illustrative contours to subpixel precision.
Measurements are optional only at this stage, before the first volume exists.

Use [shape-programs.md](shape-programs.md) for family choices and pitfalls.
Keep the number of modeling stages appropriate to the object and user request;
there is no mandatory Bender-specific sequence or fixed screenshot count.

## Refine a saved shape program

`get_model_context(include_source=true)` restores the editable source and fresh
revision. A closed part does not need opening first: a tool call with its
`document_path` opens it. Change parameters, send a `patch` of exact
replacements, or submit the full revised program with stable IDs. Prefer the
patch for small changes. A failed edit restores the saved part and names the
native feature that failed; `mode="revert"` undoes the last successful edit.
Use a local observation if only one region changed. Ask a geometric question
with `inspect_model` when the image alone cannot resolve it. Optional
`check_edit_compatibility=true` is useful if the part was manually modified.

Do not reconstruct a validated component because a different recipe now exists.
Once a check passes, repeat it only after a relevant change or new concern.

## Existing reference body

An imported CAD body can supply information unavailable in a raster. Inspect
native bounds and body facts first. For a specific section/attachment, batch rays
or use body-scoped sections. Global ray tools see visible bodies only: avoid
occluders and restore any visibility changes. Work in the document's coordinates;
the shape API's Y-up convention is not evidence that imported geometry is Y-up.

For a custom sweep profile, shell, fillet or other unsupported constructor, use
`expert_tools` to describe the concrete operation. Fit the live view to the
working body/feature with `zoom_to` before local raw work and after extent changes.
For nested internals, a targeted section image is clearer than transparency.

## Native sketch or raster tracing request

Use `create_parametric_sketch` for clean canonical lines/arcs and a deliberate
dimension scheme. Use `image_to_sketch` only when actual raster-to-sketch
conversion is requested; follow [vectorization.md](vectorization.md) for its
calibration and validation contract. Those specialist checks are not ordinary
3D-modeling preflight.

## Before delivery: numeric check against the references

Likeness judged from screenshots misses errors of 10 % and more in internal
elements, and consumed components cannot be measured by `inspect_model`.
Before `export_model`:

1. For every approved orthographic view call
   `compare_reference(image_path, view, document_path=..., height_mm=..., source="native")`.
   `source="native"` compares the bodies as built (cuts and patches included); the
   default draft source predicts from the program and needs no SolidWorks.
   For a printed part also run `inspect_model(printability=true)`.
   Read `iou`, `delta_size_mm` and `worst`; fix what fails.
2. For 5-10 identifying inner elements (visor, eyes, mouth: whatever the outline
   does not show) send **one** `measure_reference` batch per view with
   `calibration` and, per query, `expected_mm` from the program and a
   `tolerance_mm`.
3. Read each `check.passed` (true / false / null) and `delta_mm`. Fix what
   fails, then repeat.
4. Report a table: element, reference, model, deviation.
5. If the same size measured on two views differs by more than the tolerance,
   tell the user; do not average it silently.

## Finish

Save and inspect the useful result, satisfy the user's actual tolerances, then
export. `export_model` of a large model takes one to two minutes and answers
`RUNNING`: collect it with `wait_operation`. A mesh export cannot prove parameter behavior, uniform local mating fit,
wall thickness or rigging. See [model-delivery.md](model-delivery.md). Extra
documents, repeated hashes and screenshots do not make unfinished geometry done.
