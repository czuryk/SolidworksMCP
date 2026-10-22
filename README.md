# SolidworksMCP

## Major Update

From a short description and reference images to **native, editable SOLIDWORKS
models**. This update brings a new modeling workflow, **[a companion agent SKILL](skills/solidworks-mcp/SKILL.md)**,
and measured checks from the first draft to the final delivery.

### 1. Build complete parametric models

Describe the model in a compact Python shape program. Preview its proportions
before building, then create named solids, native features and equations in
SOLIDWORKS. Keep dimensions as parameters so the saved model can evolve.

### 2. Edit with confidence

Change parameters or patch just the intended part of the program. Failed edits
restore the saved model; revert takes you back to an earlier state. Long
operations report progress, and completed results survive an MCP reconnect.

### 3. Deliver a result you can check

Compare the built model with reference images, measure fits and inspect native
body health. Export SLDPRT, STEP and per-body STL with validation results and
file hashes. See exactly what passed and what still needs checking.

## Why the companion skill matters

**The skill is a core part of the modeling experience.** The MCP server provides
CAD tools; the [solidworks-mcp skill](skills/solidworks-mcp/SKILL.md) gives the
agent the workflow and practical SOLIDWORKS knowledge to use them together.

It guides the agent through planning parametric geometry, checking proportions
in a fast draft, building native solids, making focused edits and verifying the
result before export. Tool selection, reusable joint patterns, recovery steps
and known limitations are included, so you can focus your prompt on the model
you want instead of explaining the CAD process each time.

**Install the skill alongside the server** to give your agent the modeling
guidance used throughout this project. [Read the skill](skills/solidworks-mcp/SKILL.md)
or [follow the installation steps](docs/setup.md#install-the-companion-skill).

[Get started](docs/setup.md) · [Install the skill](docs/setup.md#install-the-companion-skill) ·
[Modeling guide](docs/modeling-guide.md) · [All tools](docs/tools.md)

<details>
<summary><h2>Main tools</h2></summary>

- [`get_model_context`](docs/tools.md#get_model_context) — Read the active model, parameters, bodies and editable source.
- [`model_api_lookup`](docs/tools.md#model_api_lookup) — Find modeling commands, examples and ready-to-use joint templates.
- [`execute_model_code`](docs/tools.md#execute_model_code) — Preview, build, edit or revert a native parametric model.
- [`observe_model`](docs/tools.md#observe_model) — Capture native views, custom camera angles and close-ups.
- [`measure_reference`](docs/tools.md#measure_reference) — Measure dimensions and selected details in a reference image.
- [`compare_reference`](docs/tools.md#compare_reference) — Compare model proportions with a reference image and highlight deviations.
- [`inspect_model`](docs/tools.md#inspect_model) — Check native geometry, dimensions, fits and printability.
- [`export_model`](docs/tools.md#export_model) — Export validated SLDPRT, STEP and per-body STL with a results manifest.

[And more.](docs/tools.md)

</details>

<details>
<summary><h2>Additional tools</h2></summary>

Discover and call these operations through [`expert_tools`](docs/tools.md#expert_tools).

- [`create_parametric_sketch`](docs/tools.md#create_parametric_sketch) — Build a sketch with named geometry, constraints, dimensions and equations.
- [`image_to_sketch`](docs/tools.md#image_to_sketch) — Trace raster drawings and silhouettes into validated sketch geometry.
- [`add_dimensions_batch`](docs/tools.md#add_dimensions_batch) — Add and verify multiple driving dimensions in one operation.
- [`analyze_sketch_dof`](docs/tools.md#analyze_sketch_dof) — Find missing constraints, free motions and conflicting sketch relations.
- [`create_sketch_on_face`](docs/tools.md#create_sketch_on_face) — Start a sketch directly on an existing body face.
- [`advanced_extrude`](docs/tools.md#advanced_extrude) — Create a verified extrusion with explicit start, end and body-merge conditions.
- [`advanced_cut`](docs/tools.md#advanced_cut) — Cut selected bodies with controlled depth, end conditions and scope.
- [`create_revolved_body`](docs/tools.md#create_revolved_body) — Revolve a parametric profile, including arcs and splines, into a separate body.
- [`create_swept_member`](docs/tools.md#create_swept_member) — Sweep a circular, elliptical or custom profile along a path.
- [`fillet_edges`](docs/tools.md#fillet_edges) — Round selected edges with a verified constant-radius fillet.
- [`chamfer_edges`](docs/tools.md#chamfer_edges) — Bevel selected edges by distance and angle.
- [`shell`](docs/tools.md#shell) — Hollow a body to a specified wall thickness, optionally removing faces.
- [`mirror_feature`](docs/tools.md#mirror_feature) — Mirror features or bodies across a plane or planar face.
- [`linear_pattern`](docs/tools.md#linear_pattern) — Repeat features along a direction at a defined spacing.
- [`circular_pattern`](docs/tools.md#circular_pattern) — Repeat features around an axis at a defined angle.
- [`reference_plane`](docs/tools.md#reference_plane) — Create a construction plane offset from an existing plane or face.
- [`create_multibody_insert`](docs/tools.md#create_multibody_insert) — Build a matching insert and host pocket with verified clearance.
- [`check_clearance`](docs/tools.md#check_clearance) — Measure the gap or overlap between two named bodies.
- [`probe_section`](docs/tools.md#probe_section) — Sample a section of visible geometry with a radial set of rays.
- [`probe_rays`](docs/tools.md#probe_rays) — Locate the first visible surface hit along each ray in a batch.
- [`set_body_color`](docs/tools.md#set_body_color) — Change body color while preserving other material properties.
- [`set_body_transparency`](docs/tools.md#set_body_transparency) — Adjust body transparency and verify the applied value.
- [`zoom_to`](docs/tools.md#zoom_to) — Focus the working view on selected bodies, a feature or a region.
- [`open_document`](docs/tools.md#open_document) — Open an existing SOLIDWORKS document.
- [`save_document`](docs/tools.md#save_document) — Save the active document to its current or a specified path.
- [`run_transaction`](docs/tools.md#run_transaction) — Execute supported operations with checkpoints, checks and rollback.
- [`execute_cad_plan`](docs/tools.md#execute_cad_plan) — Run a bounded operation sequence with conditions and references to earlier results.
- [`execute_python`](docs/tools.md#execute_python) — Run custom Python against the live SOLIDWORKS API with shared COM helpers.
- [`get_capabilities`](docs/tools.md#get_capabilities) — Check supported operations, export formats and optional vectorization readiness.
- [`restart_solidworks`](docs/tools.md#restart_solidworks) — Restart SOLIDWORKS with unsaved-document checks and verified reconnection.

[And more.](docs/tools.md#expert-index)

</details>

## Built with SolidworksMCP

![Bender built as native SOLIDWORKS solids](docs/assets/bender/appearance-hero.png)

**Bender, 150 mm.** A compact program builds the visor, convex eyes, curved teeth,
access door, limbs and hands as native solids. Front and side comparisons measure
the result against the approved references.

| Detailed character | Glue assembly |
|---|---|
| 24 healthy native bodies | Nine separate bodies with locating joints |
| Every sampled silhouette level within 1.5 mm | Recorded height change preserves the 0.25 mm joint gaps |
| Native views and comparison overlays | Validated SLDPRT, STEP and nine STL exports |

## Start modeling

[Install the server](docs/setup.md) and the included
[solidworks-mcp skill](skills/solidworks-mcp/SKILL.md), then connect your MCP client
to SOLIDWORKS. The skill guides the agent through drafting, building, editing,
verification and export.

**No model weights are needed for ordinary CAD work.** Download this project as
a ZIP or clone it normally. For image vectorization, install the
[optional weights](docs/setup.md#optional-vectorization-models) separately.

Start with `get_model_context` and `model_api_lookup(topic="all")`. A first shape
program can be as small as:

```python
Height = model.parameter("Height", 80)
model.revolve("Pedestal", [(0, 20), (Height / 4, 20), (Height, 8)])
```

Pass it to `execute_model_code` in `analyze` mode to preview, then `create` with a
new absolute `.SLDPRT` path to build. [Continue with the modeling guide](docs/modeling-guide.md).

## Explore

| You want to… | Open |
|---|---|
| Set up the server and companion skill | [Installation](docs/setup.md) |
| Learn the modeling and editing workflow | [Modeling guide](docs/modeling-guide.md) |
| Find any tool, its parameters or its replacement | [Complete tool reference](docs/tools.md) |
| Read test coverage and remaining limits | [Validation](docs/validation.md) |
| Follow the release history | [Changelog](CHANGELOG.md) |

## License

[MIT](LICENSE). Optional model notices: [LICENSES.md](solidworks_mcp/models/lineart/LICENSES.md).
