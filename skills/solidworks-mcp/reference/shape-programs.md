# Native shape programs

The model is a short editable program plus native solids, equations and ownership
metadata stored in SLDPRT. A sidecar is convenient, not the source of truth.
`model_api_lookup(topic="index")` owns the current signatures; choose individual
constructors or topics for constraints. This guide covers modeling decisions.

## Program budget

The interpreter accepts **512 components**. A tube, a pattern of 100 instances
and a combine each add one node: those three calls consume three components,
even after combine consumes the first two. Parameters and expressions add none.
Pattern count includes the seed, and does not change the component budget.
Read runtime values with `model_api_lookup(topic="limits")`; analyze returns
`component_count`, `max_components` and `remaining_components` in compact answers.
The 6.30.0rc1 matrix passed all 12 native fixture lifecycles on SOLIDWORKS 2026 SP2.1:
128/256/512 nodes across straight, curved, loft and lattice families. Individual
models still require native acceptance; large builds may take over an hour and
accumulate memory across a session. Nested patterns create many bodies from few nodes; heed
EXPANDED_GEOMETRY_HIGH. DRAFT_COMPLEXITY_LIMIT is the separate 2,000,000-point
retained draft budget, not a different component limit. Shared point arrays are
counted once; pattern, mirror and placement copies consume additional points.

For a connected union of supports and a platform, put the connecting platform
first. On a native failure the compiler may compute a deterministic order on
temporary copies and retry once, only when features and bodies are unchanged.
Inspect `boolean_diagnostics` for both orders and actual connectivity evidence.
Difference inputs are never reordered: the first component is the target.

## Choose the geometry before the operation

| Shape or relationship | Preferred construction | Lookup topic |
|---|---|---|
| Tapered round/elliptical body | `sectioned_solid` with a few stations | `model.sectioned_solid` |
| Round member following a curved path | `tube`; changing radius: `variable_tube` | `tube_path` |
| Spherical cap / flattened round volume | `dome` / `ellipsoid` | `ellipsoid` |
| Axial profile made of straight spans | `revolve` | `model.revolve` |
| Straight pin, socket, cuff | `tube` through two points: an analytic cylinder | `model.tube` |
| Tapered elliptical body with straight sides | `sectioned_solid(..., spans="linear")` | `pitfalls` |
| Rounded planar panel, solid mask | `rounded_prism` (radius 0: plain box) | `model.rounded_prism` |
| Plate, bracket or fin with a polygon outline | `prism` | `model.prism` |
| Ring, bushing, band | `revolve(..., closed=True)` | `model.revolve` |
| Slot, pill, capsule section | `rounded_prism` with `corner_radius = min(width, height)/2` | `model.rounded_prism` |
| Rounded or bevelled edges | `fillet` / `chamfer` with an edge selector | `edges` |
| A primitive pointing along X or Z | `axis=` on `rounded_prism`, `revolve`, `dome`, `sectioned_solid` | `pitfalls` |
| A local group moved/rotated together | `frame` + `place` | `placement` |
| Detail following an analytic family/path | `surface_point`, `path_point`, `path_tangent` | `dependencies` |
| Attachment to an actual host face | `face_anchor` + `anchored_extrude` | `face_anchors` |
| Curved panel, skin, recess or normal gap | `surface_patch`, then a Boolean if needed | `surface` |
| Union, cut or common volume | `combine` with explicit inputs | `dependencies` |
| Exact symmetry | Build one side, then native `mirror` | `model.mirror` |
| Repeated element in a row or around an axis | `pattern(count=, step=)` / `pattern(count=, axis=, center=)`: one native feature | `model.pattern` |
| Peg and socket, flush insert, hinge, collar, jointed limb, hollow cup | Start from a template | `templates` |
| Inlay that wraps round a cylinder (mouth, label) | Ring intersected with a sector: template `curved_inlay`, not a wide `surface_patch` mask | `pitfalls` |

Use rounded sections for rounded members. A flat polygon extruded in depth will
keep its slab-like character even if its Front outline matches. Choose the
construction that explains both Front and Right views; use the perspective
reference to resolve depth and overlap. Rounded dimensions are appropriate for
an illustrative reference. Only a fit-critical detail justifies tighter checks.

## Small complete program

Send as `execute_model_code(mode="create", output_path=<new absolute path>, code=...)`:

```python
H = model.parameter("H", 100)
body = model.sectioned_solid("Housing", [
    (0, .18*H, .14*H), (.50*H, .24*H, .17*H)])
cap = model.dome("Cap", radius=.24*H, height=.12*H,
                 center=(0, .49*H, 0))
model.combine("HousingWithCap", [body, cap])
arm = model.tube("Arm", [(.26*H, .44*H, 0),
                         (.34*H, .28*H, 0), (.32*H, .10*H, 0)], .035*H)
model.mirror("OtherArm", [arm], plane="Right")
```

This demonstrates native parameters, rounded volumes and symmetry; it is not
a deliverable template. Review resulting solids before deciding which detail
needs correction. Reuse helpers/loops for repeated features instead of issuing
one MCP call per sketch entity.

## Parametric dependencies

Keep positions and dimensions as expressions in `model.parameter`. Do not call
`float` or `model.value` to freeze a dependent dimension. Declare fixed assembly
clearance independently of a global height parameter; scaling every coordinate
also scales gaps. Native parameter cycling must rebuild saved geometry without
regenerating the source.

Placement and Boolean inputs are consumed. Use their resulting component in
later operations; Mirror copies its seed. Stable component names allow the
compiler to preserve independent features through edits. Round edges with
`model.fillet` / `model.chamfer`: they belong to the program, rebuild with their
input and do not block later topology edits. Manual fillets added through
`expert_tools` are outside the program and can block topology reconciliation.
An inventory check diagnoses ownership conflicts; it cannot certify every
native definition.

## Details that avoid failed attempts

- Optional positional-looking arguments such as `center`, `plane`, `frame`,
  `origin`, `direction`, `offset` and `fraction` are keyword-only where lookup
  shows `*`. `face_anchor` requires named `origin` and `direction`.
- `dome.center` is its base center; `ellipsoid.center` is its volume center.
  A rounded prism lies in XY with depth along Z unless `axis` says otherwise.
- `sectioned_solid` with three or more sections is one smooth loft and bulges
  between sections. Use `spans="linear"` when the sides must be straight.
- A component consumed by `combine`, `place`, `fillet`, `chamfer` or as a patch
  mask has no body. Inspect and observe the resulting component.
- The standard Right view looks from +X; Front looks from +Z.
- A tube path must bend more gently than the tube is thick. The tangent at an
  interior point is half the chord between its neighbours, so a short segment
  next to a long one turns sharply. Space the points evenly; start a tube that
  seats on a flat face with two points in line with the face normal. analyze
  warns with `TUBE_BEND_TOO_TIGHT` and names the points. Such a sweep can build
  at one size and fail when a parameter scales the model.
- A mask almost as wide as its curved host makes a bad `surface_patch`
  (`SURFACE_PATCH_WIDE_MASK`): build a wrapped inlay as a ring intersected with
  a sector, template `curved_inlay`.
- Exact ellipsoid mode requires constant `rz/rx`. Independent radii need explicit
  `approximate=True`; declared extents do not certify the whole fitted surface.
- `surface_patch` needs a direct, unconsumed supported host with one body.
  Make the patch **before** placing or combining the host. A one-body result of
  `place` or `combine` is not itself a supported patch host. The mask is consumed;
  the host remains available. Offset/thickness follow the actual surface normal.
- A face anchor's sketch is perpendicular to its constant ray, not tangent to
  the face. Positive extrusion follows the ray; a negative depth reverses it.
  Use a surface patch for conformal contact, not a translated flat panel.
- Active placement axes must retain nonzero values and signs during direct
  parameter cycling. Consult `placement` before planning a range through zero.

## Edit, view, finish

For a parameter change, obtain current context and call edit with
`parameters={"H": 120}` and no code. For a shape change, send
`patch=[{"old": "17.8*u, 12.0*u", "new": "17.8*u, 11.0*u"}]`: every `old` must
match the embedded program exactly once, and the answer shows what was rebuilt.
A complete revised program in `code` remains available for large rewrites. Do
not create a new part for every refinement. `mode="revert"` undoes the last edit
without a rebuild. Every edit costs a full native
rebuild, so put related changes into one patch. Read `edit` if
topology/ownership changes fail.

A failed edit restores the saved part; `NATIVE_REBUILD_FAILED` lists the failing
native features in tree order with their component, and `likely_causes` holds
only the warnings about that component. The same edit can be sent again after a
`revert`. An identical edit that failed is answered from its receipt ("Not
executed again"): change the program or pass a new `idempotency_key`.

Working view follows useful completed components automatically, and create ends
with the whole model framed. A camera warning leaves successful geometry intact;
do not repeat the mutation to repair framing. For another angle or a local
review use `observe_model`; for a dimension or gap use `inspect_model`.

Saved older recipe versions are retained during edits. Do not rebuild an accepted
part merely to adopt new seam/equation formatting. Once actual task requirements
are met, use [model-delivery.md](model-delivery.md).
