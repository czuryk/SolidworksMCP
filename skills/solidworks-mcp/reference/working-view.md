# Live working view

Shape programs automatically frame real visible bodies after each completed
component. Creation finishes with the whole model framed; edits frame changed
live outputs and their dependencies, following consumed components to their
result. Bounds are read from native bodies, not a reference image. This happens
even with `observe=false`; no extra screenshots or helper entities are created.

The current camera orientation is preserved. Front calibration removes stale
SW2026 translation, then the original orientation and explicit center are applied.
Pixel read-back checks fill, centering and clipping, with at most one scale
correction. A failed or unsupported (perspective) view returns a warning rather
than rolling back good geometry. Internal helper curves are not separately
framed; this avoids camera churn for every sketch segment.

`observe_model` creates temporary standardized captures and restores this working
camera. Therefore a successful capture alone does not prove the live workspace
was framed. For expert/raw local work, `zoom_to` before editing and after changed
extents is still needed. A merged output body can require a feature/bbox close-up
when its whole-body bounds are too broad for the desired detail.

## Verified active-sketch view

Use this reference when a task creates, activates, or edits a 2D sketch; when a
sketch is tilted, upside down, tiny, off-centre, or clipped; or when interpreting
the orientation diagnostics returned by sketch tools.

## Contents

1. [Contract](#contract)
2. [Standalone syntax](#standalone-syntax)
3. [Automatic coverage](#automatic-coverage)
4. [How orientation is chosen](#how-orientation-is-chosen)
5. [How Fit to Screen is verified](#how-fit-to-screen-is-verified)
6. [Diagnostics and acceptance](#diagnostics-and-acceptance)
7. [Examples](#examples)
8. [Limitations and known issues](#limitations-and-known-issues)

## Contract

For an active 2D sketch, the working-view postcondition is:

- the camera is normal to the sketch plane;
- the screen-up and screen-right axes match the nearest stable sketch-axis
  orientation, so the sheet does not flip by 180 degrees;
- when fitting is enabled, a Fit command is always invoked, even if the initial
  frame already looks valid;
- the fitted active geometry is neither tiny nor clipped and remains reasonably
  centred;
- the orientation is read back again after fitting;
- any failed orientation or fit postcondition is returned as failure rather
  than hidden behind a successful COM call.

The automatic sketch paths use this contract before and/or after mutation. Do
not add a second manual `Ctrl+8` or generic Zoom To Fit after them.

## Standalone syntax

```jsonc
normal_to_sketch({
  "zoom_to_fit": true,
  "angular_tolerance_deg": 0.1,
  "prefer_native": true
})
```

| Argument | Default | Meaning |
|---|---:|---|
| `zoom_to_fit` | `true` | Always invoke and verify working-geometry Fit after orientation. Set `false` only for an explicit orientation-only diagnostic or pre-mutation check. |
| `angular_tolerance_deg` | `0.1` | Maximum read-back error for normal, up, and right axes. Accepted range: `0.001..5.0`. |
| `prefer_native` | `true` | Try SOLIDWORKS command 169 when the current view needs correction and the command is enabled. Typed `Orientation3` remains the bounded fallback. |

Requirements: an active document, active model view, active sketch, valid typed
`IMathUtility`, and a readable `ModelToSketchTransform`. The implementation is
designed and acceptance-tested for 2D sketches.

## Automatic coverage

| Path | Before geometry | After geometry / activation |
|---|---|---|
| `create_sketch`, `create_sketch_on_face` | n/a | Normal To + always-invoked Fit |
| `draw_line`, `draw_circle`, `draw_rectangle`, `draw_arc`, `draw_polygon` | Normal To, no Fit | Normal To + always-invoked Fit |
| `sketch_contour` | Normal To, no Fit | Normal To + always-invoked Fit |
| `create_parametric_sketch` and committed `image_to_sketch` paths | handled during creation | Reopen the exact created sketch after rebuild, then Normal To + always-invoked Fit |
| Existing-sketch activation and `add_dimensions_batch` | activation is verified | Normal To + always-invoked Fit after the operation |

Plane or face selection used only as a service reference for a mirror, pattern
direction, end condition, or new reference plane must not change the camera.
The automatic contract applies when the plane/face becomes the active working
sketch.

Pre-mutation failure means no geometry is created. Atomic
`create_parametric_sketch` reactivates its created ProfileFeature after rebuild;
if reactivation or the view postcondition fails it returns
`SKETCH_WORKING_VIEW_UNAVAILABLE` or `SKETCH_WORKING_VIEW_FAILED` and follows
the requested transaction rollback policy. Incremental legacy mutations can
still report `geometry_created:true`; inspect `document_restored` instead of
assuming whether geometry survived.

## How orientation is chosen

1. Read and validate the right-handed orthonormal camera basis from
   `IModelView::Orientation3`.
2. Read and validate sketch +X, +Y, and normal from
   `ISketch::ModelToSketchTransform`.
3. Choose `sketch_front` or `sketch_back` by whichever sketch normal is nearer
   the current `toward_viewer` direction. Choose `+sketch_y` or `-sketch_y` by
   the nearer screen-up direction.
4. If all three axes already satisfy the tolerance, do not call native Normal
   To. This is the guard against SOLIDWORKS' already-aligned side toggle.
5. Otherwise try native command 169 when enabled, redraw, and read the axes back.
6. If native read-back fails, assign the exact basis through typed
   `IMathUtility.CreateTransform` / `IModelView::Orientation3`, with at most two
   attempts and read-back after each.
7. After Fit, read the orientation once more. Fit is not allowed to invalidate
   the face-on result.

`prefer_native=false` is useful for diagnosing the deterministic matrix path;
it is not a reason to bypass read-back verification.

## How Fit to Screen is verified

The target priority is:

1. active-sketch model points, with conservative full-radius extrema for circles
   and arcs;
2. bounding-box corners of visible solid bodies when the sketch has no usable
   extent;
3. `empty_geometry` when neither target exists.

For measurable geometry, model points are transformed to actual screen pixels
with `IModelView::Transform`. The accepted frame has:

- dominant width/height fill in `0.35..0.90`;
- centre-offset ratio at most `0.18`;
- no point bounding-box edge outside the view frame (one-pixel allowance).

SOLIDWORKS can publish `IModelView::Transform` in physical desktop coordinates
while a newly tiled MDI view still reports local `FrameWidth`/`FrameHeight`.
The verifier evaluates both local coordinates and the physical
`ISldWorks.FrameLeft/Top/Width/Height` frame, then uses the geometrically
plausible mapping. Inspect `coordinate_space` and `frame_origin_screen_px` in
each measurement when diagnosing multi-monitor or DPI-scaled layouts.

The bounded fit sequence is `ViewZoomTo2(active_geometry)`, then
`ViewZoomtofit2`, then native Zoom To Fit command 332 as needed. If a result has
a measurable but invalid fill, `Scale2` targets a dominant fill of `0.55`.
SOLIDWORKS 2026 changes `Translation3` when `Scale2` changes, so the server
snapshots and restores `Translation3` before re-measuring. Inspect
`translation_preserved:true` on that correction attempt.

When geometry is empty, the server still calls `ViewZoomtofit2`, returns
`verified:true`, `verification_applicable:false`, and `reason:"empty_geometry"`.
That is a valid empty-sketch state, not geometric evidence of occupancy.

## Diagnostics and acceptance

Use these fields instead of trusting the visual result alone:

- `verified`: combined orientation and Fit postcondition;
- `normal_to_verified`: orientation postcondition independent of Fit;
- `initial_axes`, `sketch_axes`, `target_axes`, `actual_axes`;
- `target_axes.side` and `target_axes.up_axis`;
- `angular_error_deg.normal_deg|up_deg|right_deg`;
- `methods`, `attempts`, and `native_command`;
- `fit_to_screen.verification_applicable`, `scope`, `methods`, and `attempts`;
- `fit_to_screen.actual.dominant_fill_ratio`, `center_offset_ratio`, `clipped`,
  `screen_bbox_px`, `frame_px`, `coordinate_space`,
  `frame_origin_screen_px`, and `scale2`.

Accept a non-empty result only when all of the following are true:

```text
success == true
data.verified == true
data.normal_to_verified == true
max(data.angular_error_deg.*) <= data.angular_tolerance_deg
data.fit_to_screen.verified == true
data.fit_to_screen.verification_applicable == true
data.fit_to_screen.actual.clipped == false
```

For a final visual audit, capture the full window with `zoom_to_fit:false` so the
screenshot operation does not replace the accepted camera state:

```jsonc
take_screenshot({
  "path": "D:\\TMP\\acceptance.png",
  "full_window": true,
  "zoom_to_fit": false,
  "compress": false
})
```

## Examples

Repair and audit a manually tilted or zoomed active sketch:

```jsonc
normal_to_sketch({
  "zoom_to_fit": true,
  "angular_tolerance_deg": 0.1,
  "prefer_native": true
})
```

Exercise only the matrix fallback during troubleshooting:

```jsonc
normal_to_sketch({
  "zoom_to_fit": true,
  "prefer_native": false
})
```

Perform an orientation-only audit without changing the accepted zoom:

```jsonc
normal_to_sketch({
  "zoom_to_fit": false,
  "angular_tolerance_deg": 0.05
})
```

Normal sketch creation needs no follow-up camera command:

```jsonc
create_sketch({"plane":"Top"})
draw_circle({"x":0,"y":0,"radius":25,"unit":"mm"})
```

Inspect the `orientation` payload returned by both calls; the drawing result
contains the pre-geometry and post-geometry evidence.

## Limitations and known issues

- The public contract is for an active 2D sketch. No active sketch, no active
  model view, an invalid transform, or an unavailable typed `IMathUtility`
  produces a structured failure. Do not treat this as a general 3D-sketch
  camera solver.
- `angular_tolerance_deg` is configurable, but Fit thresholds and the `0.55`
  target are intentionally fixed in version 6.5.32. Use returned metrics and a
  full-window screenshot when a workflow needs stricter visual framing.
- Fit uses sketch points plus conservative circle/arc extrema. A pathological
  spline whose true extrema are not represented by its returned sketch points
  can require independent screenshot or exported-geometry verification.
- Empty or zero-extent sketch geometry falls back to visible-body bounds. Hidden
  bodies do not participate. If no visible body has extent, occupancy
  verification is explicitly non-applicable.
- `ViewZoomTo2` can over-zoom planar geometry on SOLIDWORKS 2026; the verified
  `Scale2` correction handles this. `Scale2` also rewrites `Translation3` on
  that version, so centring preservation is mandatory, not optional cleanup.
- Native Normal To may be disabled, return `None`, or choose the wrong side.
  None of those return values is accepted by itself; only matrix read-back is
  authoritative.
- A modal SOLIDWORKS dialog can block COM and therefore orientation. Preflight
  must report it; only a recognized, operation-scoped Modify dialog may be
  handled automatically. Unknown dialogs require a human.
- Atomic parametric creation rolls back a failed working-view postcondition when
  `transaction.rollback_on_failure=true`. Incremental drawing tools can retain
  already-created geometry; check `document_restored` and `geometry_created`.
- Acceptance was performed on SOLIDWORKS 2026 SP2.1. Keep unit/regression tests
  and the same live read-back matrix when qualifying another major version.
