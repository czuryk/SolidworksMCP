# Raster-to-sketch vectorization

This is a specialist tracing workflow. For ordinary 3D reconstruction from
reference images, start with shape volumes; this pipeline is not a prerequisite.

Use `image_to_sketch` for controlled, validated conversion of a raster
projection into a SolidWorks sketch. The production pipeline is deliberately
heavy: deep segmentation or learned line extraction, calibrated projection,
bounded curve fitting, CAD commit, and reverse-raster verification.

## Contents

1. [Safety sequence](#safety-sequence)
2. [Call shape](#call-shape)
3. [Choose the semantic mode](#choose-the-semantic-mode)
4. [Projection and calibration](#projection-and-calibration)
5. [Placement](#placement)
6. [Approximation and curve count](#approximation-and-curve-count)
7. [Output modes](#output-modes)
8. [Validation and commit policy](#validation-and-commit-policy)
9. [Recipes](#recipes)
10. [Debug artifacts](#debug-artifacts)
11. [Limitations and known issues](#limitations-and-known-issues)

## Safety sequence

1. Call `get_capabilities`; require the requested backend and model hashes.
2. Run `image_to_sketch` with `commit.mode="analyze_only"`.
3. Inspect the selected mask/edges, vector overlay, topology, curve count, and
   validation metrics. Reject halo capture or missing characteristic corners.
4. Repeat with corrected semantic controls if necessary. Do not compensate for
   a wrong mask by loosening curve tolerances.
5. Commit with a new idempotency key and `commit_if_confident`.
6. Accept only after the server's post-COM reverse-raster check passes.

Analysis runs in an isolated worker. Only the final CAD mutation crosses COM.

## Call shape

```json
{
  "image_path": "D:\\refs\\candidate_front_v01.png",
  "sketch_name": "Front_Reference",
  "plane": "Front",
  "unit": "mm",
  "image_mode": "filled_silhouette",
  "contour_selection": {},
  "trace": {"backend": "sam2_vitmatte", "mode": "outer_silhouette"},
  "projection": {"mode": "orthographic"},
  "require_orthographic": true,
  "calibration": {"mode": "bbox_height", "value": 180.0},
  "placement": {
    "image_anchor": "silhouette_bottom_center",
    "model_anchor": [0.0, 0.0],
    "rotation_deg": 0.0,
    "mirror_x": false,
    "mirror_y": false
  },
  "approximation": {"preset": "ultra", "output_mode": "construction_reference"},
  "validation": {},
  "commit": {"mode": "analyze_only"},
  "debug": {"directory": "D:\\TMP\\vector_debug"},
  "idempotency_key": "front-analysis-v1"
}
```

`approximation` is the preferred fitting block. `geometry` remains a legacy
alias; when both exist, `approximation` wins. Read the capability schema
returned by the running server when generating calls dynamically.

## Choose the semantic mode

Choose what the pixels mean before tuning thresholds.

| Intended geometry | `image_mode` | `trace.mode` | Production backend |
|---|---|---|---|
| Only the outside boundary of a filled object | `filled_silhouette` | `outer_silhouette` | `sam2_vitmatte` or `deep_matting` |
| Outside boundary plus true openings | `filled_silhouette` | `silhouette_with_holes` | `sam2_vitmatte` or `deep_matting` |
| Every boundary between retained regions | `filled_silhouette` | `all_region_boundaries` | `sam2_vitmatte` or `deep_matting` |
| Prompt-selected objects only | `filled_silhouette` | `guided_components` | `sam2_vitmatte` |
| Centreline of drawn strokes | `technical_drawing` or `line_drawing` | `stroke_centerlines` | `dexined_teed` |
| Both sides of drawn strokes | `technical_drawing` or `line_drawing` | `stroke_edges` | `dexined_teed` |
| Every visible learned edge | `technical_drawing` or `line_drawing` | `all_visible_edges` | `dexined_teed` |

`outer_silhouette` follows only the selected foreground region's outer border;
it does not recursively move inward and does not retain interior holes. Use
`silhouette_with_holes` only for real openings; `guided_components` may retain
holes belonging to the selected component. This is the correct mode for a single
product outline. `all_visible_edges` is intentionally permissive and can
produce dense, non-manufacturable sketches.

Useful region controls in `trace` include `backend`, `mode`, `edge_semantics`,
`component_policy`, `alpha_threshold`, `background_threshold`, `min_area_px`,
`max_components`, `roi_px`, `box_px`, `positive_points_px`, and
`negative_points_px`. Guided selection is the reliable way to exclude nearby
objects with similar colour.

Useful line-art controls include `line_probability_threshold`,
`dexined_threshold`, `teed_threshold`, `consensus_radius_px`,
`path_consensus_radius_px`, `min_branch_length_px`, `max_paths`,
`stroke_edge_side`, `edge_topology_source`, and `trimap_radius_px`. Learned
edges are the production result; classical edges are diagnostic evidence and
must not silently replace an unavailable deep backend.

When both verified DexiNed and TEED checkpoints are loaded, consensus reports
`both_verified_checkpoints_loaded`, checkpoint hashes, probability summaries,
band/centerline pixel counts, and two distinct agreement measurements. The
acceptance fields `dexined_support`, `teed_support`, and `balanced_support`
measure each model centerline against the other model's probability band;
`centerline_*` support and distance fields remain diagnostic and may be low for
wide overlapping strokes whose ridges are offset.

## Projection and calibration

Choose one projection mode:

- `orthographic`: the source is an orthographic view. Use for metric CAD work.
- `homography`: rectify a planar perspective view before tracing.
- `trace_as_is`: trace image coordinates without claiming metric orthography.

Set `require_orthographic=true` for production sketches. If projection is
omitted, the server reports an unspecified orthographic state and can reject it.
`trace_as_is` is intentionally capped at a lower confidence (normally 0.75).

Homography example:

```json
{
  "projection": {
    "mode": "homography",
    "source_quad_px": [[132, 84], [1810, 102], [1772, 1204], [160, 1188]],
    "output_size_px": [1680, 1120]
  }
}
```

Order `source_quad_px` as top-left, top-right, bottom-right, bottom-left. The quad
must be convex, inside the image, and have an area of at least 64 pixels².
Output width and height must each be between 64 and 16384 pixels.

Supported calibration patterns:

```json
{"calibration": {"mode": "bbox_height", "value": 180.0}}
```

```json
{"calibration": {"mode": "bbox_width", "value": 96.0}}
```

```json
{
  "calibration": {
    "mode": "two_points",
    "pixel_points": [[220, 1040], [220, 140]],
    "distance": 180.0
  }
}
```

```json
{"calibration": {"mode": "mm_per_pixel", "value": 0.2}}
```

The `scale` form also accepts an explicit scale value. Use two well-separated
points or a known bbox dimension. Calibration does not remove perspective.

## Placement

Available image anchors include:

- `silhouette_bottom_center`
- `silhouette_bbox_center`
- `image_center`
- `centroid`
- `pixel_point` with an explicit source coordinate

Map the selected anchor to `placement.model_anchor`, then apply
`rotation_deg`, `mirror_x`, and `mirror_y`. Confirm placement with the returned
metric bbox; a good trace in the wrong coordinate frame is still a failure.

## Approximation and curve count

Presets are bounded starting points, not permission to ignore validation:

| Preset | Max error (mm) | Entity cap | Min feature (mm) | Target/max segment (mm) | Corner angle | Smoothing |
|---|---:|---:|---:|---:|---:|---:|
| `coarse` | 0.50 | 40 | 0.80 | 8 / 32 | 20° | 0.40 |
| `balanced` | 0.15 | 80 | 0.40 | 4 / 20 | 12° | 0.25 |
| `fine` | 0.08 | 160 | 0.20 | 2 / 10 | 8° | 0.15 |
| `ultra` | 0.04 | 240 | 0.10 | 1 / 5 | 5° | 0.20 |

`ultra` is the current default. It uses a 256-point spline sampling target and
a total fit budget of 900; the lower presets use progressively smaller budgets.
The per-spline control-point safety cap is 64.

If one accurate periodic/closed spline exceeds that cap, the fitter tries the
smallest sequence of open cubic NURBS split at stable curvature transitions.
Every entity must remain within the per-spline cap and the combined candidate
must still pass the global symmetric error bound and total budgets. Accepted
entities contain `curvature_split`; rejected analyses expose the complete
machine-readable candidate tree under `error.details.curve_fit` instead of a
generic fit failure.

Tune these deliberately:

- `max_error_mm`: maximum allowed simplification/fitting deviation.
- `smoothing`: 0 preserves local shape; 1 smooths aggressively.
- `min_feature_mm`: remove detail below a real-world size, not a pixel guess.
- `min_segment_length_mm`, `target_segment_length_mm`,
  `max_segment_length_mm`: constrain sampling density;
  maintain `min <= target <= max`.
- `corner_angle_deg`: lower it to retain subtle but characteristic bends.
- entity and fitting budgets: prevent a correct mask from becoming a CAD mess.

Prefer a slightly lower corner threshold or max error when a characteristic
antenna-like tip becomes angular. Do not globally shorten every segment first;
that recreates the excessive-curve failure. The server never relaxes requested
tolerances automatically.

## Output modes

- `locked_trace`: fixed traced geometry for a faithful reference.
- `minimal_parametric`: hybrid primitives where safe, with fewer entities.
- `reference_spline`: periodic spline-oriented representation.
- `construction_reference`: construction-only curves for downstream manual or
  parametric modeling; this is often the safest projection workflow.

`reference_spline` forces spline/periodic behavior. `minimal_parametric` forces
the hybrid fitter. `construction_reference` preserves a useful calibrated
reference but does not create a fully dimensioned production sketch. For a
construction-only reference the expensive SolidWorks constraint-status solve
is intentionally skipped; `constraint_status_evaluation_skipped` and
`solve_target_evaluation_skipped` make that explicit even if a shared payload
requested `solve.target="fully_defined"`.

## Validation and commit policy

Commit modes:

- `analyze_only` (also exposed as preview): no CAD mutation.
- `commit_if_confident`: commit only when confidence reaches the configured
  threshold; the normal minimum is 0.90.
- `force_commit`: requires an explicit acknowledgement of validation failure.

Rollback is the default on any CAD or verification failure. After COM creates
the curves, the server exports the actual sketch geometry, rasterizes it, and
compares it with the selected source geometry. This reverse-raster pass is
mandatory evidence; pre-CAD analysis alone is insufficient.

Typical region gates are `min_iou=0.985` and `max_hausdorff_mm=0.3`. Typical
line-art gates are `min_reverse_line_support=0.95` and
`max_hausdorff_mm=0.5`, with backend-stability checks. Use project-specific
values only when their manufacturing meaning is known.

## Recipes

Outer silhouette, analysis first:

```json
{
  "image_path": "D:\\refs\\front.png",
  "sketch_name": "Front_Outer",
  "plane": "Front",
  "image_mode": "filled_silhouette",
  "contour_selection": {},
  "trace": {"backend": "sam2_vitmatte", "mode": "outer_silhouette", "max_components": 1},
  "projection": {"mode": "orthographic"},
  "require_orthographic": true,
  "calibration": {"mode": "bbox_height", "value": 180.0},
  "placement": {"image_anchor": "silhouette_bottom_center", "model_anchor": [0, 0]},
  "approximation": {"preset": "ultra", "max_error_mm": 0.035, "smoothing": 0.16, "output_mode": "construction_reference"},
  "validation": {"min_iou": 0.985, "max_hausdorff_mm": 0.3},
  "commit": {"mode": "analyze_only"},
  "idempotency_key": "front-outer-analyze-v2"
}
```

Technical line art:

```json
{
  "image_path": "D:\\refs\\drawing.png",
  "sketch_name": "Drawing_Centerlines",
  "plane": "Front",
  "image_mode": "technical_drawing",
  "trace": {
    "backend": "dexined_teed",
    "mode": "stroke_centerlines",
    "min_branch_length_px": 18,
    "max_paths": 120
  },
  "projection": {"mode": "orthographic"},
  "require_orthographic": true,
  "calibration": {"mode": "two_points", "pixel_points": [[100, 900], [100, 100]], "distance": 80},
  "approximation": {"preset": "fine", "smoothing": 0.12, "output_mode": "locked_trace"},
  "validation": {"min_reverse_line_support": 0.95, "max_hausdorff_mm": 0.5},
  "commit": {"mode": "analyze_only"},
  "idempotency_key": "drawing-lines-analyze-v1"
}
```

After accepting the overlay, change the commit mode to
`commit_if_confident` and use a new idempotency key.

## Debug artifacts

Keep the reported mask/edge image, fitted-vector overlay, reverse-raster image,
metric report, and selected-component metadata. A green outline on a screenshot
is not acceptance by itself: inspect whether it follows the true foreground or
a compression halo.

## Limitations and known issues

- Raster commits with rollback require a saved, clean document. One native file
  checkpoint covers creation and mandatory post-CAD validation; failure restores
  verified saved bytes and reopens the document. Local sketch deletion alone is
  reported separately and does not prove full document restoration.
- Hybrid fits preserve native shared endpoints. Arc endpoints must have the same
  radius; spline seam coordinates are fixed before scoring geometric accuracy.
  A successful diagnostic classical transfer does not certify segmentation.

- SolidWorks Autotrace has no stable public automation API suitable for this
  server. The MCP uses its own validated pipeline.
- Deep production backends require their model files; CUDA strongly improves
  runtime. Missing models are an explicit capability failure, not a reason to
  fall back silently to classical thresholding.
- Homography corrects a planar perspective view. It cannot reconstruct hidden
  depth or a genuinely non-planar perspective object.
- `all_visible_edges` and `all_region_boundaries` can intentionally create
  dense results. Use component/path caps and a semantic selection mode.
- Thin, antialiased, compressed, or low-contrast boundaries remain sensitive
  to prompts and thresholds. Inspect the segmentation before fitting.
- Dense periodic B-splines can burden the SolidWorks solver. The per-spline cap
  is 64 control points; the bounded fallback splits only at measured curvature
  transitions and still enforces global error and total-control budgets.
- `construction_reference` is faithful reference geometry, not automatically a
  fully constrained production design.
- Orthographic tracing reconstructs only the chosen 2D projection. It does not
  infer the complete 3D part.
