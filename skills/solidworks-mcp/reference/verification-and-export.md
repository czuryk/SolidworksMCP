# Native verification, reference comparison, and atomic export

Use this expert reference for requested calibrated comparisons or explicit-path
`export_bundle` delivery. Ordinary shape delivery uses `export_model`; see
[model-delivery.md](model-delivery.md). Comparison calibration and diagnostic
negative controls are not mandatory stages of every modeling task.

## Contents

1. [Evidence strategy](#evidence-strategy)
2. [Export native sketch geometry](#export-native-sketch-geometry)
3. [Render SVG](#render-svg)
4. [Compare two sketches](#compare-two-sketches)
5. [Compare a sketch with a reference](#compare-a-sketch-with-a-reference)
6. [Compare a body silhouette](#compare-a-body-silhouette)
7. [Tolerance profiles](#tolerance-profiles)
8. [Export an atomic bundle](#export-an-atomic-bundle)
9. [Acceptance checklist](#acceptance-checklist)
10. [Limitations and known issues](#limitations-and-known-issues)

## Evidence strategy

Use at least two independent layers for critical geometry:

- SolidWorks-native entities/topology from the active model.
- Calibrated metric comparison or ray probing.
- A rendered overlay or structurally inspected exchange file.
- Persistence proof after save/reopen or manifest hash verification.

Do not use an uncalibrated screenshot as metric proof. Do not use a matching
bbox alone as a shape proof.

## Export native sketch geometry

```json
{
  "sketch_name": "Front_Reference",
  "coordinate_system": "sketch_2d",
  "unit": "mm",
  "include": {
    "construction": true,
    "relations": true,
    "dimensions": true,
    "equations": true,
    "topology": true
  },
  "output": {
    "mode": "summary_and_file",
    "path": "D:\\TMP\\verify\\front_geometry.json",
    "numeric_precision": 9
  }
}
```

`export_sketch_geometry` returns the plane transform, bbox, contours, entity
types, construction state, endpoints/control data, and stable `seg_*` IDs.
Repeated exports should preserve topology and IDs; output-path and timestamp
metadata may differ.

Use the JSON for exact entity accounting, topology checks, downstream SVG,
and attribution of error zones to a particular segment.

## Render SVG

```json
{
  "sketch_names": ["Front_Reference"],
  "path": "D:\\TMP\\verify\\front.svg",
  "view": {"padding": 5.0, "unit": "mm", "invert_y_for_display": true},
  "style": {"stroke_width": 0.25}
}
```

The renderer preserves native primitives where possible, layers construction
geometry separately, and uses non-scaling strokes. Validate the SVG as XML,
confirm the viewBox against the native bbox plus padding, and account for every
expected stable segment ID.

## Compare two sketches

```json
{
  "candidate_sketch": "Front_Reference",
  "reference_sketch": "Front_Master",
  "tolerance": {"profile": "strict", "sample_step_mm": 0.025},
  "unit": "mm",
  "report_path": "D:\\TMP\\verify\\sketch_compare.json"
}
```

`compare_sketches` reports mean, p95, maximum, and Hausdorff deviations,
topology differences, and maximum-error zones tied to stable entities. Inspect
both the aggregate values and localized zones.

## Compare a sketch with a reference

```json
{
  "sketch_name": "Front_Reference",
  "reference_image": "D:\\refs\\front.png",
  "transform": {
    "mode": "explicit",
    "matrix": [[0.2, 0.0, -120.0], [0.0, -0.2, 180.0], [0.0, 0.0, 1.0]]
  },
  "image_mode": "technical_drawing",
  "tolerance": {"profile": "balanced", "min_line_support": 0.95},
  "outputs": {
    "png_preview": "D:\\TMP\\verify\\front_overlay.png",
    "svg_overlay": "D:\\TMP\\verify\\front_overlay.svg",
    "report": "D:\\TMP\\verify\\front_report.json"
  }
}
```

Provide a verified 3x3 pixel-to-sketch transform in `transform.matrix`; the
worker inverts it for rasterization. `saved_calibration` can reuse the exact
calibration recorded by `image_to_sketch`. For filled references use IoU and
boundary deviation; for line references use reverse support and distance
metrics. Confidence/support supplements p95 and maximum error; it does not
replace them.

## Compare a body silhouette

For the ordinary likeness check of a whole shape model use the main tool
`compare_reference(image_path, view, document_path=..., source="native")`: one
call, no transform, a level table in mm. The expert tool below remains for a
subset of bodies, line drawings and an explicit affine transform.

`compare_body_silhouette_to_image` extracts selected SolidWorks bodies'
projection
from native `IBody2`/`ITessellation` mesh data. It does not infer the silhouette
from the viewport screenshot.

```json
{
  "reference_image": "D:\\TMP\\verify\\insert_reference.png",
  "screenshot_path": "D:\\TMP\\verify\\body_evidence.png",
  "orientation": "front",
  "bodies": ["Insert_Acceptance"],
  "candidate_source": "native_mesh",
  "mesh": {"deviation_mm": 0.02, "angle_tolerance_deg": 10.0},
  "transform": {
    "candidate_to_reference": [[4.0, 0.0, 320.0], [0.0, -4.0, 280.0], [0.0, 0.0, 1.0]],
    "mm_per_pixel": 0.25,
    "allow_bbox_fit": false
  },
  "tolerance": {"profile": "balanced"},
  "outputs": {
    "overlay": "D:\\TMP\\verify\\body_overlay.png",
    "report": "D:\\TMP\\verify\\body_report.json"
  }
}
```

Use an explicit affine `candidate_to_reference` and `mm_per_pixel` for
acceptance. The server rejects inconsistent declared scale beyond its scale
conflict tolerance (normally 2%). `allow_bbox_fit=true` is diagnostic only:
fitting both bboxes can hide scale or placement errors.

The view/orientation mapping must match the reference. Keep mesh deviation well
below the intended comparison tolerance. Native tessellation preserves model
coordinates and does not mutate user display or tessellation preferences.

For `candidate_source="native_mesh"`, screenshot isolation and native
tessellation run in a killable capture process; triangle projection runs in a
second isolated worker. `budget.max_elapsed_sec` bounds both phases, and client
cancellation is propagated to the active child. The capture process restores
body visibility in `finally`. A durable diagnostic is written beside the
screenshot as `<screenshot_path>.body-comparison-diagnostic.json`; on timeout or
cancellation inspect `last_completed_stage`, `active_body`, `triangle_count`,
`body_triangles`, and `visibility_restored`.

## Tolerance profiles

Sketch distance profiles, before pixel-scale floors:

| Profile | Mean (mm) | p95 (mm) | Max (mm) |
|---|---:|---:|---:|
| `draft` | 0.25 | 0.50 | 1.00 |
| `balanced` | 0.10 | 0.25 | 0.50 |
| `strict` | 0.05 | 0.10 | 0.25 |

Body silhouette profiles:

| Profile | Minimum IoU | Base max Hausdorff (mm) |
|---|---:|---:|
| `draft` | 0.950 | 1.00 |
| `balanced` | 0.985 | 0.30 |
| `strict` | 0.995 | 0.15 |

Raster resolution creates a lower bound on meaningful distance thresholds. The
body comparator uses at least twice the declared mm-per-pixel for Hausdorff;
sketch thresholds are also floored by pixel scale. Use adequate resolution
instead of asking a coarse raster to prove a finer tolerance.

## Export an atomic bundle

```json
{
  "sldprt_path": "D:\\deliverables\\bracket\\bracket_v12.SLDPRT",
  "step_path": "D:\\deliverables\\bracket\\bracket_v12.STEP",
  "stl_directory": "D:\\deliverables\\bracket\\stl",
  "bodies": ["Host", "Insert"],
  "naming": {"Host": "bracket_v12_host.stl", "Insert": "bracket_v12_insert.stl"},
  "overwrite": false,
  "report": true,
  "unit": "mm",
  "stl": {
    "quality": "custom",
    "binary": true,
    "deviation_mm": 0.02,
    "angle_tolerance_deg": 10.0,
    "preserve_origin": true,
    "max_triangles": 5000000
  }
}
```

The exporter:

1. Requires a saved active document and validates unique target paths.
2. Refuses existing targets when `overwrite=false` before altering files.
3. Rebuilds and checks body faces, bbox, and requested invariants.
4. Stages files in temporary directories beside their final targets.
5. Verifies the native SLDPRT signature.
6. Structurally inspects STEP entities, body count, and closed shells.
7. Exports each requested body to native STL and checks triangles and bbox.
8. Calculates file hashes.
9. Atomically replaces the targets and commits the manifest, or restores the
   previous target set on a controlled failure.

For multibody output, treat per-body STL counts and bboxes as acceptance data.
Do not infer that a non-empty STL contains the correct body.

A call waits at most 45 s; a longer export answers `RUNNING` and
`wait_operation` reports its `stage` until the result arrives.
`get_session_metrics.active_operation.progress` exposes the current bounded
stage, and the returned manifest/result includes `progress_events`. Expected
stages distinguish rebuild, body verification, SLDPRT, STEP, each body STL,
hashing, manifest, and atomic commit. A changing stage/last-progress timestamp
is evidence of healthy work.

## Acceptance checklist

- Active source document is saved and rebuilt.
- Required feature/body names are present and unambiguous.
- Native face count and solid-body count match the plan.
- If calibrated comparison is required, it passes the chosen task tolerance.
- SLDPRT, STEP, and each STL pass structural inspection.
- STEP body and closed-shell counts match the source.
- STL triangle count is non-zero and bbox matches the selected source body.
- Manifest paths, sizes, hashes, units, versions, and geometry summaries match
  the committed files.

Negative-control comparisons and intentional overwrite rejection belong in
exporter/comparator regression tests, not each user's normal delivery.

## Limitations and known issues

- Native silhouette comparison validates one projection. Hidden geometry and
  depth still need other views, sections, rays, or volume/topology checks.
- Explicit affine calibration is only as reliable as the correspondences used
  to derive it. Bbox fitting is not acceptance-grade calibration.
- Tessellation is an approximation. Set mesh deviation substantially below the
  comparison tolerance and record it in the report.
- Raster IoU depends on image resolution and antialiasing. Always pair it with
  metric boundary distances.
- SVG can approximate unsupported native curve types; use exported native JSON
  for authoritative geometry.
- STEP importers may reorder entities. Validate body/shell structure and bbox,
  not textual ordering.
- Atomic replacement protects controlled application failures, but an OS or
  power failure can still interrupt filesystem persistence. The committed
  manifest and its hashes are the source of truth.
- `overwrite=true` intentionally replaces prior deliverables. Preserve or
  version old bundles when rollback across runs is required.
