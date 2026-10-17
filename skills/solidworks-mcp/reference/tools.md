# Expert tool routing and lifecycle

The live catalog owns signatures and schemas. Discover only what is needed:

```json
{"action":"list","query":"sweep"}
{"action":"describe","name":"create_swept_member"}
{"action":"call","name":"open_document","arguments":{"filepath":"D:/Parts/Part.SLDPRT"}}
```

These are `expert_tools` arguments. `query` matches a case-insensitive literal
substring of the tool name or description; use one keyword or a tool name.
For example, change an existing body colour through the available tool:

```json
{"action":"list","query":"color"}
{"action":"describe","name":"set_body_color"}
{"action":"call","name":"set_body_color","arguments":{"name":"Body","rgb":[200,200,205]}}
```

Use the actual body name from `get_model_context`. Calls use the same serial STA, receipts and
recovery as direct tools. `SOLIDWORKS_MCP_TOOL_PROFILE=expert` exposes the full
catalog after server restart; it is unnecessary for ordinary shape modeling.
Public dimensions generally use user units; raw COM uses metres. Read each
schema for local/model coordinates, angles and scope.

## Choose a route

| Need outside shape constructors | Expert tools / reference |
|---|---|
| Open/save/close or assemblies | `open_document`, `save_document`, `close_document`, `create_new_assembly` (shape tools open or activate `document_path` themselves) |
| Session recovery after a lost MCP process | `restart_solidworks`, then `reconcile_session` |
| Release SolidWorks memory (`SOLIDWORKS_MEMORY_HIGH`) | Save, then `restart_solidworks`; documents are not reopened, the next call with `document_path` opens the part |
| Colour / transparency of an existing body | `set_body_color`, `set_body_transparency` |
| Review of a modeling run | `get_session_report` (calls, failures, answer sizes, slowest operations) |
| UI state when a call is blocked | `get_environment_status` (never gated by the modal check) |
| Custom constrained sketch | `create_parametric_sketch`, `add_dimensions_batch` |
| Partial model-space contour | `sketch_contour`; sketch-local edits: `draw_*` |
| Extrude/cut with custom end conditions | `advanced_extrude`, `advanced_cut`; explicitly scope multibody cuts |
| Custom revolve/sweep profiles | `create_revolved_body`, `create_swept_member` |
| Native shell, feature pattern or mirror in a hand-built part | Describe the concrete operation; [patterns.md](patterns.md). In a shape program these are constructors: `model.fillet`, `model.chamfer`, `model.pattern`, `model.mirror` |
| Planar insert and host pocket in a hand-built part | `create_multibody_insert`. In a shape program start from a template: `model_api_lookup("template:inlay")`, `"template:pin_socket"`, `"template:curved_inlay"` |
| Actual raster conversion | `image_to_sketch`; [vectorization.md](vectorization.md) |
| Calibrated geometry comparison | `compare_sketches`, `compare_sketch_to_reference`, `compare_body_silhouette_to_image` (a subset of bodies, line drawings, an explicit affine transform). The ordinary likeness check of a whole model is the main tool `compare_reference(source="native")` |
| A ray-selected entity | `probe_ray`, `probe_rays`, `select_face_by_ray`; visible bodies only |
| Live focus / internal view | `zoom_to`, `section_screenshot`; [working-view.md](working-view.md) |
| Additional export formats / selected bodies | `export_file`, `export_bundle` |
| Bounded expert operation sequence or DAG | `run_transaction`, `execute_cad_plan`, `sync_model_graph` |
| Unsupported native API / failure diagnosis | `execute_python`; a long run answers `RUNNING` and `wait_operation` collects it. [transactions-and-safety.md](transactions-and-safety.md) |

Do not wrap a shape call in another transaction. Zero faces on a consumed or
reference feature is not by itself a defect. Inspection and geometry validation
belong at useful boundaries, not after every line. Read exact scope/ray/selection
rules before custom operations; the compiler already handles them for shapes.

## Deprecated compatibility tools

Deprecation is visible in descriptions and `solidworks-mcp/deprecation` metadata;
`expert_tools list` also returns `deprecated` and `replacements`. Existing call
names, schemas and dispatch remain available. No automatic argument rewriting
occurs. `describe` contains migration details.

| Deprecated | Replacement | Migration |
|---|---|---|
| `extrude_sketch` | `advanced_extrude` | Map Blind/MidPlane arguments; use guarded verification |
| `cut_extrude` | `advanced_cut` | Map end condition; explicitly name affected bodies |
| `compare_sketch_to_image` | `compare_sketch_to_reference` | Same comparison arguments |
| `get_job_result` | `wait_operation` | `wait_operation(operation_id=<job_id>, wait_sec=<timeout>)`; no longer in the main tool list |
| `execute_python_async` | `execute_python` | Same code; when the call answers `RUNNING`, collect it with `wait_operation` |
| `list_jobs` | `get_executor_status`, `get_session_report` | One operation by `operation_id`, or the session summary |
| `create_semantic_primitive` | Concrete operation below | Unwrap its `parameters`; some kinds need translation |

Dispatcher kinds map to `create_revolved_body` (`revolved_shell`),
`create_swept_member` (`tubular_member`), `create_multibody_insert`
(`clearance_insert`, `clearance_pocket`, `cap_and_opening`), `shell`
(`uniform_shell`), `mirror_feature` (`symmetric_pair`), and the corresponding
linear/circular pattern. `hole_or_rib_array` selects a concrete pattern and drops
the dispatcher selector. `capsule_profile` needs a `create_parametric_sketch`
with two semicircles and two tangent lines; it is not a drop-in rename.
`printable_hinge` becomes its explicit `execute_cad_plan` with checkpoint/rollback
and existing invariants; it never inferred a hinge automatically.

The last three became redundant when every long call started to answer
`RUNNING` (6.25.0); they are marked since 6.29.4.

Expert does not mean deprecated. Custom profiles, raw API, operation DAGs and
alternate export contracts remain useful and have no full shape-API replacement.
