# Tool reference

Generated from SolidworksMCP **6.30.0rc1**: **97 tools**, **13 in the default profile**, **84 expert operations**, including **7 deprecated compatibility calls**.

[Project overview](../README.md) · [Companion skill](../skills/solidworks-mcp/SKILL.md) · [Complete JSON schemas](tool-schemas.json)

Descriptions, defaults and input constraints below come from the server code. Required means required by the JSON schema; descriptions can impose additional conditions, such as a saved document and a fresh revision for an edit. Nested structures, unions and complete validation rules are in `tool-schemas.json`, under `tools.<tool_name>.inputSchema`.

## Calling tools

Call a default-profile tool directly. For an expert operation, discover its name, read its schema, and pass its arguments through `expert_tools`:

```json
{"action": "list", "query": "color"}
{"action": "describe", "name": "set_body_color"}
```

Then call `expert_tools` with `action="call"`, `name` and an `arguments` object matching the described schema. Search is a case-insensitive literal substring, not a multi-keyword query. `SOLIDWORKS_MCP_TOOL_PROFILE=expert` exposes the full catalog directly. Existing direct names remain callable for compatibility.

A long call can return `RUNNING`. Collect it with `wait_operation`; do not submit the mutation again. `get_executor_status` is available while CAD is busy. Use `reconcile_session` only for an unknown, quarantined outcome.

## Default profile

| Tool | Purpose |
|---|---|
| [`get_model_context`](#get_model_context) | Start here: connect and read the active document, revision, parameters and bodies. Compact summary by default; components=[names] adds those component records, detail=full the whole map. include_source retrieves its editable program; check_edit_compatibility optionally checks unmanaged features. |
| [`model_api_lookup`](#model_api_lookup) | Look up shape constructor signatures, a topic or example; COM-free. topic='index' lists constructors and topics; topic='all' returns the whole reference in one call (cheaper than many lookups); 'workflow' is the recommended order of calls, 'pitfalls' behaviour worth knowing before the first model, 'examples' one working snippet per constructor, 'templates' vetted programs for typical joints (peg and socket, inlay, hinge, collar) and 'template:&lt;name&gt;' one of them as code. An unknown topic answers with did_you_mean. The limits topic gives numerical component, execution and draft budgets. |
| [`execute_model_code`](#execute_model_code) | Build or edit native solids from Python using the injected model. Analyze is COM-free and returns the predicted extents of every resulting body in milliseconds (draft_views adds silhouette images): check proportions there before a native build that takes minutes; with document_path it previews the edit plan (would_rebuild/would_add/would_remove) without mutation. Create saves a new part. Edit needs document_path and a fresh expected_revision plus one of: patch (small exact replacements in the embedded program, preferred), code (a complete revised program), or only parameters; a failed edit restores the saved part. Revert undoes the last edit. Create and edit frame the working geometry and return views by default. Read warnings in every answer: TUBE_BEND_TOO_TIGHT and SURFACE_PATCH_WIDE_MASK predict a native failure, NATIVE_BODY_INVALID means export_model will refuse the body. Analyze includes component_count, max_components and remaining_components; read model_api_lookup(topic="limits") for the active budgets. |
| [`observe_model`](#observe_model) | Capture revision-bound native views at shared scale; restores the working camera afterward. components gives a close-up with surrounding context. Defaults to Front, Right and Isometric; a view may also be a custom camera {name, direction, up}. flat=true writes &lt;directory&gt;/&lt;view&gt;.png instead of a random subfolder. sections adds targeted radial samples. |
| [`measure_reference`](#measure_reference) | Measure visible image extents in explicit pixel ROIs; COM-free, no CAD entities. With calibration, expected_mm and tolerance_mm each query returns check {delta_mm, passed: true/false/null}: batch 5-10 landmarks per approved view before delivery. Mark occluded boundaries unknown. A query with color measures one coloured element inside the figure (eyes, teeth). Not needed to start the first volume; this is not semantic segmentation. |
| [`compare_reference`](#compare_reference) | Compare the model silhouette with a reference image in one call, seconds. The silhouette is drafted without SolidWorks from code (before any native build) or from the saved part at document_path; source="native" with document_path uses the bodies as built (cuts, patches and anchored features included) and is the check before delivery. Returns IoU, size deltas and a level-by-level table of left/right extents in mm with the worst deviations first, plus an overlay image. Use it to fix proportions before create and as the numeric likeness check before delivery; one call per approved orthographic view. |
| [`inspect_model`](#inspect_model) | Measure native geometry without images or helper sketches. body_pairs checks a specific fit; read each passed=true/false/null. precision=delivery adds exact bounds and kernel/feature health. printability=true adds print checks on the built bodies: does the assembled model stand on its base (centre of mass over the footprint, tip angle) and the thinnest wall of every body. |
| [`export_model`](#export_model) | Publish checked SLDPRT, STEP, per-body STL (stl/&lt;BodyName&gt;.stl, stable across rebuilds) and manifest to a new directory from a saved clean part; replace=true republishes over its own earlier delivery. Preserves source; strict raw STL validation by default. Export success does not certify likeness, parameter cycling or print-process readiness. |
| [`get_request_status`](#get_request_status) | COM-free durable receipt lookup by idempotency_key (request_receipt.key of a mutation answer). Historical evidence only; never executes or retries CAD. |
| [`wait_operation`](#wait_operation) | Collect a long operation: COM-free bounded wait by operation_id. While it runs returns status RUNNING with elapsed_sec and stage; once it finishes returns exactly the result of the original tool, including views. Never cancels, repeats or alters the operation. |
| [`get_executor_status`](#get_executor_status) | COM-free executor and durable quarantine status. Safe while SolidWorks is busy. busy=true is ordinary running work (use wait_operation); quarantined=true is an unknown outcome. Also reports server.version, server.code_changed_since_start (true: reconnect the MCP server to load the code on disk) and SolidWorks memory. With operation_id adds its status and result_summary; the full result comes from wait_operation. |
| [`reconcile_session`](#reconcile_session) | Only for an unknown outcome (quarantined session): inspect native state by the exact journal operation_id. Not needed after a long operation that finished. Never replays the original command; an orphaned or killed COM worker requires a verified SolidWorks restart. |
| [`expert_tools`](#expert_tools) | Discover expert operations: list with optional query, describe one schema, then call with nested arguments. Covers document management (open_document, close_document), session care (restart_solidworks, get_session_report), custom features, tracing and raw API (execute_python). DEPRECATED entries include replacements; calls remain compatible. |

## Expert index

| Tool | Lifecycle |
|---|---|
| [`add_dimensions_batch`](#add_dimensions_batch) | Supported |
| [`advanced_cut`](#advanced_cut) | Supported |
| [`advanced_extrude`](#advanced_extrude) | Supported |
| [`analyze_sketch_dof`](#analyze_sketch_dof) | Supported |
| [`body_volume`](#body_volume) | Supported |
| [`chamfer_edges`](#chamfer_edges) | Supported |
| [`check_clearance`](#check_clearance) | Supported |
| [`circular_pattern`](#circular_pattern) | Supported |
| [`close_document`](#close_document) | Supported |
| [`close_sketch`](#close_sketch) | Supported |
| [`compare_body_silhouette_to_image`](#compare_body_silhouette_to_image) | Supported |
| [`compare_sketch_to_image`](#compare_sketch_to_image) | Deprecated; use `compare_sketch_to_reference` |
| [`compare_sketch_to_reference`](#compare_sketch_to_reference) | Supported |
| [`compare_sketches`](#compare_sketches) | Supported |
| [`connect_solidworks`](#connect_solidworks) | Supported |
| [`create_multibody_insert`](#create_multibody_insert) | Supported |
| [`create_new_assembly`](#create_new_assembly) | Supported |
| [`create_new_part`](#create_new_part) | Supported |
| [`create_parametric_sketch`](#create_parametric_sketch) | Supported |
| [`create_revolved_body`](#create_revolved_body) | Supported |
| [`create_semantic_primitive`](#create_semantic_primitive) | Deprecated; use `create_revolved_body`, `create_swept_member`, `create_multibody_insert`, `shell`, `mirror_feature`, `linear_pattern`, `circular_pattern`, `create_parametric_sketch`, `execute_cad_plan` |
| [`create_sketch`](#create_sketch) | Supported |
| [`create_sketch_on_face`](#create_sketch_on_face) | Supported |
| [`create_swept_member`](#create_swept_member) | Supported |
| [`cut_extrude`](#cut_extrude) | Deprecated; use `advanced_cut` |
| [`delete_feature`](#delete_feature) | Supported |
| [`draw_arc`](#draw_arc) | Supported |
| [`draw_circle`](#draw_circle) | Supported |
| [`draw_line`](#draw_line) | Supported |
| [`draw_polygon`](#draw_polygon) | Supported |
| [`draw_rectangle`](#draw_rectangle) | Supported |
| [`execute_cad_plan`](#execute_cad_plan) | Supported |
| [`execute_python`](#execute_python) | Supported |
| [`execute_python_async`](#execute_python_async) | Deprecated; use `execute_python`, `wait_operation` |
| [`export_bundle`](#export_bundle) | Supported |
| [`export_file`](#export_file) | Supported |
| [`export_sketch_geometry`](#export_sketch_geometry) | Supported |
| [`extrude_sketch`](#extrude_sketch) | Deprecated; use `advanced_extrude` |
| [`fillet_edges`](#fillet_edges) | Supported |
| [`fix_freeze_bar`](#fix_freeze_bar) | Supported |
| [`get_capabilities`](#get_capabilities) | Supported |
| [`get_document_info`](#get_document_info) | Supported |
| [`get_environment_status`](#get_environment_status) | Supported |
| [`get_feature_status`](#get_feature_status) | Supported |
| [`get_job_result`](#get_job_result) | Deprecated; use `wait_operation` |
| [`get_session_metrics`](#get_session_metrics) | Supported |
| [`get_session_report`](#get_session_report) | Supported |
| [`get_sketch_status`](#get_sketch_status) | Supported |
| [`get_solidworks_info`](#get_solidworks_info) | Supported |
| [`hide_body`](#hide_body) | Supported |
| [`image_to_sketch`](#image_to_sketch) | Supported |
| [`linear_pattern`](#linear_pattern) | Supported |
| [`list_bodies`](#list_bodies) | Supported |
| [`list_features`](#list_features) | Supported |
| [`list_jobs`](#list_jobs) | Deprecated; use `get_executor_status`, `get_session_report` |
| [`list_open_documents`](#list_open_documents) | Supported |
| [`mirror_feature`](#mirror_feature) | Supported |
| [`normal_to_sketch`](#normal_to_sketch) | Supported |
| [`open_document`](#open_document) | Supported |
| [`probe_ray`](#probe_ray) | Supported |
| [`probe_rays`](#probe_rays) | Supported |
| [`probe_section`](#probe_section) | Supported |
| [`recover_environment`](#recover_environment) | Supported |
| [`reference_axis`](#reference_axis) | Supported |
| [`reference_plane`](#reference_plane) | Supported |
| [`rename_body`](#rename_body) | Supported |
| [`rename_feature`](#rename_feature) | Supported |
| [`render_sketch_svg`](#render_sketch_svg) | Supported |
| [`restart_solidworks`](#restart_solidworks) | Supported |
| [`revolve_boss`](#revolve_boss) | Supported |
| [`run_transaction`](#run_transaction) | Supported |
| [`save_document`](#save_document) | Supported |
| [`section_screenshot`](#section_screenshot) | Supported |
| [`select_face_by_ray`](#select_face_by_ray) | Supported |
| [`set_body_color`](#set_body_color) | Supported |
| [`set_body_transparency`](#set_body_transparency) | Supported |
| [`set_units`](#set_units) | Supported |
| [`set_view_orientation`](#set_view_orientation) | Supported |
| [`shell`](#shell) | Supported |
| [`show_body`](#show_body) | Supported |
| [`sketch_contour`](#sketch_contour) | Supported |
| [`sync_model_graph`](#sync_model_graph) | Supported |
| [`take_screenshot`](#take_screenshot) | Supported |
| [`zoom_to`](#zoom_to) | Supported |

## Default tool details

### get_model_context

Start here: connect and read the active document, revision, parameters and bodies. Compact summary by default; components=[names] adds those component records, detail=full the whole map. include_source retrieves its editable program; check_edit_compatibility optionally checks unmanaged features.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `include_source` | boolean | No | `false` |  |
| `detail` | string | No | `"summary"` | full adds the complete component map and static API fields enum: `["summary", "full"]` |
| `components` | array | No | — | Component names whose records are added to the summary minItems: `1` maxItems: `32` uniqueItems: `true` Nested definition: see complete JSON schema. |
| `verbose` | boolean | No | `false` | Return the complete legacy answer instead of the compact one |
| `check_edit_compatibility` | boolean | No | `false` | Optional native feature inventory check for unmanaged changes; adds a feature walk. Does not certify every native definition. |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### model_api_lookup

Look up shape constructor signatures, a topic or example; COM-free. topic='index' lists constructors and topics; topic='all' returns the whole reference in one call (cheaper than many lookups); 'workflow' is the recommended order of calls, 'pitfalls' behaviour worth knowing before the first model, 'examples' one working snippet per constructor, 'templates' vetted programs for typical joints (peg and socket, inlay, hinge, collar) and 'template:&lt;name&gt;' one of them as code. An unknown topic answers with did_you_mean. The limits topic gives numerical component, execution and draft budgets.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `topic` | string | No | `"index"` |  |

### execute_model_code

Build or edit native solids from Python using the injected model. Analyze is COM-free and returns the predicted extents of every resulting body in milliseconds (draft_views adds silhouette images): check proportions there before a native build that takes minutes; with document_path it previews the edit plan (would_rebuild/would_add/would_remove) without mutation. Create saves a new part. Edit needs document_path and a fresh expected_revision plus one of: patch (small exact replacements in the embedded program, preferred), code (a complete revised program), or only parameters; a failed edit restores the saved part. Revert undoes the last edit. Create and edit frame the working geometry and return views by default. Read warnings in every answer: TUBE_BEND_TOO_TIGHT and SURFACE_PATCH_WIDE_MASK predict a native failure, NATIVE_BODY_INVALID means export_model will refuse the body. Analyze includes component_count, max_components and remaining_components; read model_api_lookup(topic="limits") for the active budgets.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `code` | string | No | — | maxLength: `150000` |
| `patch` | array | No | — | Edit the embedded program in place: ordered replacements, each old must occur exactly once. Mutually exclusive with code. minItems: `1` maxItems: `64` Nested definition: see complete JSON schema. |
| `mode` | string | No | `"analyze"` | revert restores the saved state before the last edit (document_path and expected_revision only; up to three edits back, seconds instead of a rebuild) enum: `["analyze", "create", "edit", "revert"]` |
| `output_path` | string | No | — | New absolute .SLDPRT path; required for create |
| `parameters` | object | No | — |  |
| `observe` | boolean / array | No | `true` | Views returned after create/edit: true (front, right, isometric), false (none), or a list of views as in observe_model, e.g. ["front"] Nested definition: see complete JSON schema. |
| `draft_views` | array | No | — | analyze only: COM-free silhouette images of the program in these views minItems: `1` maxItems: `6` uniqueItems: `true` Nested definition: see complete JSON schema. |
| `draft_components` | array | No | — | analyze only: also return predicted extents of these components (consumed ones included) minItems: `1` maxItems: `32` uniqueItems: `true` Nested definition: see complete JSON schema. |
| `draft_directory` | string | No | — | analyze only: absolute directory for draft images; a temporary one by default |
| `verbose` | boolean | No | `false` | Return the complete legacy answer instead of the compact one |
| `document_path` | string | No | — | Absolute saved part path; required for edit and revert, optional for analyze (edit plan). An open document is activated, a closed one is opened |
| `expected_revision` | string | No | — | Fresh revision from get_model_context or the last answer; required for edit and revert |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |

### observe_model

Capture revision-bound native views at shared scale; restores the working camera afterward. components gives a close-up with surrounding context. Defaults to Front, Right and Isometric; a view may also be a custom camera {name, direction, up}. flat=true writes &lt;directory&gt;/&lt;view&gt;.png instead of a random subfolder. sections adds targeted radial samples.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `directory` | string | No | — | Optional absolute image directory; defaults beside the part |
| `document_path` | string | Yes | — | Absolute saved part path; an open document is activated, a closed one is opened |
| `expected_revision` | string | Yes | — | Fresh revision from get_model_context or the last answer |
| `components` | array | No | — | Shape component names to frame; consumed components must be addressed through their result minItems: `1` maxItems: `32` uniqueItems: `true` Nested definition: see complete JSON schema. |
| `sections` | array | No | — | Optional targeted native radial sections; no CAD entities. Center must be inside the selected single-body component. Samples are first outward hits, not a full circularity certificate. minItems: `1` maxItems: `4` Nested definition: see complete JSON schema. |
| `views` | array | No | — | Standard view names and/or custom cameras, each with a unique name minItems: `1` maxItems: `6` Nested definition: see complete JSON schema. |
| `flat` | boolean | No | `false` | Write &lt;directory&gt;/&lt;view&gt;.png directly; existing files are refused unless overwrite=true |
| `overwrite` | boolean | No | `false` | With flat=true, replace existing view files |
| `width` | integer | No | `1000` | minimum: `256` maximum: `2000` |
| `verbose` | boolean | No | `false` | Return the complete legacy answer instead of the compact one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### measure_reference

Measure visible image extents in explicit pixel ROIs; COM-free, no CAD entities. With calibration, expected_mm and tolerance_mm each query returns check {delta_mm, passed: true/false/null}: batch 5-10 landmarks per approved view before delivery. Mark occluded boundaries unknown. A query with color measures one coloured element inside the figure (eyes, teeth). Not needed to start the first volume; this is not semantic segmentation.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `image_path` | string | Yes | — | Absolute local reference image path |
| `projection` | string | No | `"unknown"` | enum: `["front", "right", "top", "perspective", "unknown"]` |
| `queries` | array | Yes | — | minItems: `1` maxItems: `12` Nested definition: see complete JSON schema. |
| `calibration` | object | No | — | Nested definition: see complete JSON schema. |
| `background` | array | No | — | minItems: `3` maxItems: `3` Nested definition: see complete JSON schema. |
| `threshold` | integer | No | `32` | minimum: `9` maximum: `246` |
| `annotation_directory` | string | No | — | Optional absolute directory for annotated crop images |

### compare_reference

Compare the model silhouette with a reference image in one call, seconds. The silhouette is drafted without SolidWorks from code (before any native build) or from the saved part at document_path; source="native" with document_path uses the bodies as built (cuts, patches and anchored features included) and is the check before delivery. Returns IoU, size deltas and a level-by-level table of left/right extents in mm with the worst deviations first, plus an overlay image. Use it to fix proportions before create and as the numeric likeness check before delivery; one call per approved orthographic view.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `image_path` | string | Yes | — | Absolute path of the reference image; uniform background |
| `view` | string | No | `"front"` | Orthographic view the reference shows enum: `["front", "back", "right", "left", "top", "bottom"]` |
| `code` | string | No | — | Shape program to draft; alternative to document_path maxLength: `150000` |
| `document_path` | string | No | — | Saved part: its embedded program is drafted, or with source=native its built bodies are measured |
| `source` | string | No | `"draft"` | draft: predicted from the program, COM-free; native: tessellation of the built bodies of document_path enum: `["draft", "native"]` |
| `parameters` | object | No | — |  |
| `height_mm` | number | No | — | Real height of the reference silhouette in mm; default: the model height (then only proportions are compared) exclusiveMinimum: `0` |
| `levels` | integer | No | `24` | Number of height levels in the table minimum: `4` maximum: `64` |
| `tolerance_mm` | number | No | `1.5` | A level passes when the model matches within this distance, at a height within the same distance of the level exclusiveMinimum: `0` |
| `align` | string | No | `"bbox"` | bbox: centre both silhouettes; origin: the reference centre is the model X=0 (Z=0 for side views) enum: `["bbox", "origin"]` |
| `background` | array | No | — | minItems: `3` maxItems: `3` Nested definition: see complete JSON schema. |
| `threshold` | integer | No | `32` | minimum: `9` maximum: `246` |
| `directory` | string | No | — | Absolute directory for the overlay image; defaults beside the part |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### inspect_model

Measure native geometry without images or helper sketches. body_pairs checks a specific fit; read each passed=true/false/null. precision=delivery adds exact bounds and kernel/feature health. printability=true adds print checks on the built bodies: does the assembled model stand on its base (centre of mass over the footprint, tip angle) and the thinnest wall of every body.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `document_path` | string | Yes | — | Absolute saved part path; an open document is activated, a closed one is opened |
| `expected_revision` | string | Yes | — | Fresh revision from get_model_context or the last answer |
| `components` | array | No | — | Shape component names to frame; consumed components must be addressed through their result minItems: `1` maxItems: `32` uniqueItems: `true` Nested definition: see complete JSON schema. |
| `sections` | array | No | — | Optional targeted native radial sections; no CAD entities. Center must be inside the selected single-body component. Samples are first outward hits, not a full circularity certificate. minItems: `1` maxItems: `4` Nested definition: see complete JSON schema. |
| `body_pairs` | array | No | — | Targeted global body-pair distances and native intersection volumes; no selections or sketches. Read relation and tri-state clearance_check.passed; UNKNOWN never certifies a gap. minItems: `1` maxItems: `8` Nested definition: see complete JSON schema. |
| `precision` | string | No | `"summary"` | enum: `["summary", "delivery"]` |
| `printability` | boolean / object | No | — | true, or {min_wall_mm (default 1.2), up (default Y)}: support and wall thickness of the built bodies Nested definition: see complete JSON schema. |
| `verbose` | boolean | No | `false` | Return the complete legacy answer instead of the compact one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### export_model

Publish checked SLDPRT, STEP, per-body STL (stl/&lt;BodyName&gt;.stl, stable across rebuilds) and manifest to a new directory from a saved clean part; replace=true republishes over its own earlier delivery. Preserves source; strict raw STL validation by default. Export success does not certify likeness, parameter cycling or print-process readiness.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `document_path` | string | Yes | — | Absolute saved part path; an open document is activated, a closed one is opened |
| `expected_revision` | string | Yes | — | Fresh revision from get_model_context or the last answer |
| `directory` | string | Yes | — | New absolute delivery directory, or with replace=true an earlier export_model delivery |
| `verbose` | boolean | No | `false` | Return the complete legacy answer instead of the compact one |
| `replace` | boolean | No | `false` | Atomically replace directory when it holds only an earlier export_model delivery (its manifest and listed files). Any other existing directory is still refused with OUTPUT_EXISTS. |
| `expected_bodies` | array | No | — | minItems: `1` maxItems: `64` uniqueItems: `true` Nested definition: see complete JSON schema. |
| `deviation_mm` | number | No | `0.02` | minimum: `0.001` maximum: `0.1` |
| `mesh_cleanup` | string | No | `"none"` | Optional bounded STL seam welding and local winding correction after strict validation fails. Never fills holes. Retains raw STL and audit; native geometry stays unchanged. enum: `["none", "bounded"]` |
| `diagnostics_directory` | string | No | — | Optional NEW absolute directory for the raw failed STL and bounded facet diagnostics. Separate from delivery; created only on mesh rejection. |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### get_request_status

COM-free durable receipt lookup by idempotency_key (request_receipt.key of a mutation answer). Historical evidence only; never executes or retries CAD.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `idempotency_key` | string | Yes | — | minLength: `1` maxLength: `256` |

### wait_operation

Collect a long operation: COM-free bounded wait by operation_id. While it runs returns status RUNNING with elapsed_sec and stage; once it finishes returns exactly the result of the original tool, including views. Never cancels, repeats or alters the operation.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `operation_id` | string | Yes | — | minLength: `1` |
| `wait_sec` | number | No | `45.0` | How long this call waits; call again while status is RUNNING minimum: `0` maximum: `45.0` |

### get_executor_status

COM-free executor and durable quarantine status. Safe while SolidWorks is busy. busy=true is ordinary running work (use wait_operation); quarantined=true is an unknown outcome. Also reports server.version, server.code_changed_since_start (true: reconnect the MCP server to load the code on disk) and SolidWorks memory. With operation_id adds its status and result_summary; the full result comes from wait_operation.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `operation_id` | string | No | — |  |
| `verbose` | boolean | No | `false` | Include the complete retained result of operation_id |

### reconcile_session

Only for an unknown outcome (quarantined session): inspect native state by the exact journal operation_id. Not needed after a long operation that finished. Never replays the original command; an orphaned or killed COM worker requires a verified SolidWorks restart.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `operation_id` | string | Yes | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### expert_tools

Discover expert operations: list with optional query, describe one schema, then call with nested arguments. Covers document management (open_document, close_document), session care (restart_solidworks, get_session_report), custom features, tracing and raw API (execute_python). DEPRECATED entries include replacements; calls remain compatible.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `action` | string | No | `"list"` | enum: `["list", "describe", "call"]` |
| `query` | string | No | — | Case-insensitive literal substring of the tool name or description. Use one keyword or a tool name. |
| `name` | string | No | — |  |
| `arguments` | object | No | — |  |


## Expert tool details

### add_dimensions_batch

Add driving dimensions in one rebuild with verified values and atomic rollback. Suppresses/restores the value-entry dialog and fits an activated sketch.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch_name` | string | Yes | — |  |
| `dimensions` | array | Yes | — | Dimension requests Nested definition: see complete JSON schema. |
| `suppress_modify_dialog` | boolean | No | `true` |  |
| `rebuild` | string | No | — | enum: `["once", "none"]` |
| `rollback_on_failure` | boolean | No | `true` |  |
| `guard_policy` | string | No | — | enum: `["operation_scoped", "session_scoped", "leave_disabled"]` |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### advanced_cut

Create a verified Cut-Extrude; scope_bodies confines multibody cuts. Ray references require visible faces; auto_flags resolves supported direction flags. expected_bbox guards the affected region and expected_merge_bodies guards scope. Distances use user units.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch_name` | string | No | — |  |
| `end_condition` | string | No | `"blind"` |  |
| `depth` | number | No | `10` |  |
| `direction_flip` | boolean | No | `false` |  |
| `offset_reverse` | boolean | No | `false` |  |
| `translate_surface` | boolean | No | `false` |  |
| `start_condition` | string | No | `"sketch_plane"` |  |
| `start_offset` | number | No | `0` |  |
| `flip_start_offset` | boolean | No | `false` |  |
| `ref_face_ray` | object | No | — |  |
| `start_face_ray` | object | No | — |  |
| `scope_bodies` | array | No | — | Body names to limit the cut to (Mark=8) Nested definition: see complete JSON schema. |
| `normal_cut` | boolean | No | `false` |  |
| `optimize_geometry` | boolean | No | `false` |  |
| `feature_name` | string | No | — | Rename the created feature. ACTUAL name is returned (auto-suffix _2 on collision + warning) |
| `auto_verify` | boolean | No | `true` |  |
| `auto_flags` | boolean | No | `false` | Try flag combinations and pick the one that yields a live feature |
| `expected_bbox` | object | No | — | Guard zone {'min':[x,y,z],'max':[x,y,z],'tolerance':0.5} in user units: a scoped cut checks the newly affected profile/sweep region instead of the full connected face grid; violation rolls back |
| `expected_merge_bodies` | array | No | — | Bodies ALLOWED to be merged/swallowed; any other merged body rolls the feature back with an error Nested definition: see complete JSON schema. |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### advanced_extrude

Create a verified Boss-Extrude with explicit start/end conditions. merge=false creates a separate body; ray references require visible faces. depth is the offset for offset_from_surface. expected_bbox/expected_merge_bodies guard placement; auto_flags resolves supported direction flags.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch_name` | string | No | — | Profile sketch (last sketch if omitted) |
| `end_condition` | string | No | `"blind"` |  |
| `depth` | number | No | `10` | Depth (or offset distance for offset_from_surface) |
| `direction_flip` | boolean | No | `false` |  |
| `offset_reverse` | boolean | No | `false` |  |
| `translate_surface` | boolean | No | `false` |  |
| `merge` | boolean | No | `true` | Merge result (False = new body) |
| `start_condition` | string | No | `"sketch_plane"` |  |
| `start_offset` | number | No | `0` |  |
| `flip_start_offset` | boolean | No | `false` |  |
| `ref_face_ray` | object | No | — | Ray to pick end-condition reference face (Mark=1) |
| `start_face_ray` | object | No | — | Ray to pick start-condition reference face (Mark=32) |
| `feature_name` | string | No | — | Rename the created feature. ACTUAL name is returned (auto-suffix _2 on collision + warning) |
| `auto_verify` | boolean | No | `true` |  |
| `auto_flags` | boolean | No | `false` | Try flag combinations (direction_flip/offset_reverse/flip_start) and pick the one that yields a live feature |
| `expected_bbox` | object | No | — | Guard zone {'min':[x,y,z],'max':[x,y,z],'tolerance':0.5} in user units: if the created feature's bbox falls outside, it is rolled back with an error. Combine with auto_flags to search for the combo landing in the right zone |
| `expected_merge_bodies` | array | No | — | Bodies ALLOWED to be swallowed by the merge; if any other body merges, the feature is rolled back with an error Nested definition: see complete JSON schema. |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### analyze_sketch_dof

Explain under/over-definition per entity: coordinates, free motions, connectivity, minimal recommendations, conflict candidates, and Fix-quality warnings.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch_name` | string | Yes | — |  |
| `include_recommendations` | boolean | No | `true` |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### body_volume

Read native volume, surface area and centre of mass for one body, or all bodies if name is omitted.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `name` | string | No | — | Body name (all bodies if omitted) |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### chamfer_edges

Distance-angle chamfer. Edges picked by rays (edge_rays, hit only VISIBLE bodies) or pre-selected. distance in user units, angle in degrees. Auto-verifies the result. In a shape program use model.chamfer.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `distance` | number | No | `2` |  |
| `angle` | number | No | `45` |  |
| `edge_rays` | array | No | — | Rays selecting edges Nested definition: see complete JSON schema. |
| `feature_name` | string | No | — |  |
| `auto_verify` | boolean | No | `true` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### check_clearance

Measure two named bodies in user units; containment counts as overlap. min_clearance fails the check on insufficient gap or overlap. Use inspect_model(body_pairs) for batched revision-bound queries with lower/upper bounds.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `body_a` | string | Yes | — |  |
| `body_b` | string | Yes | — |  |
| `min_clearance` | number | No | — | Required clearance in user units; below it -&gt; success=false |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### circular_pattern

Circular pattern of feature(s) about an axis (axis_entity name or axis_edge_ray). angle in degrees. Auto-verifies. In a shape program use model.pattern(count=, axis=, center=).

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `seed_features` | array | Yes | — | Nested definition: see complete JSON schema. |
| `axis_entity` | string | No | — | Reference axis / circular edge name |
| `axis_edge_ray` | object | No | — |  |
| `count` | integer | No | `4` |  |
| `angle` | number | No | `360` |  |
| `equal_spacing` | boolean | No | `true` |  |
| `reverse` | boolean | No | `false` |  |
| `feature_name` | string | No | — |  |
| `auto_verify` | boolean | No | `true` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### close_document

Close the active document; save=false discards unsaved changes. Reopen the saved file only when an intentional rollback is needed.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `save` | boolean | No | `false` | Save before closing |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### close_sketch

Close/exit the active sketch.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### compare_body_silhouette_to_image

Compare selected native body mesh silhouettes with a calibrated image transform. Returns symmetric deviations and overlays; native capture and raster comparison use separate bounded workers. Best-fit alignment and screenshot segmentation are diagnostic opt-ins. For the ordinary likeness check of a whole model use compare_reference(source="native"): one call, no transform; this tool remains for a subset of bodies, line drawings and an explicit affine transform.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `reference_image` | string | Yes | — |  |
| `screenshot_path` | string | Yes | — |  |
| `orientation` | string | No | — | enum: `["front", "back", "right", "left", "top", "bottom"]` |
| `bodies` | array | No | — | Nested definition: see complete JSON schema. |
| `candidate_source` | string | No | `"native_mesh"` | enum: `["native_mesh", "screenshot_segmentation"]` |
| `mesh` | object | No | — | Native mesh quality, deviation_mm, angle_tolerance_deg and max_triangles settings Nested definition: see complete JSON schema. |
| `reference_mode` | string | No | `"filled_silhouette"` | enum: `["filled_silhouette", "technical_drawing", "line_drawing", "trace_as_is"]` |
| `contour_selection` | object | No | — | Reference ROI/topology selection Nested definition: see complete JSON schema. |
| `transform` | object | No | — | native_mesh model-mm-to-reference-pixel 3x3 affine matrix and required reference mm_per_pixel; allow_bbox_fit is diagnostic Nested definition: see complete JSON schema. |
| `tolerance` | object | No | — | draft/balanced/strict IoU, Hausdorff and segmentation-confidence thresholds Nested definition: see complete JSON schema. |
| `outputs` | object | No | — | Overlay/report paths Nested definition: see complete JSON schema. |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### compare_sketch_to_image

DEPRECATED: use compare_sketch_to_reference. Duplicate alias of compare_sketch_to_reference. Compatibility calls remain available; describe for migration details.

**Migration:**

```json
{
  "deprecated": true,
  "catalog_since": "6.24.0",
  "callable": true,
  "replacements": [
    "compare_sketch_to_reference"
  ],
  "reason": "Duplicate alias of compare_sketch_to_reference.",
  "migration": "Same comparison arguments; use compare_sketch_to_reference."
}
```

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch_name` | string | Yes | — |  |
| `image_path` | string | Yes | — |  |
| `transform` | object | No | — | Calibration transform Nested definition: see complete JSON schema. |
| `tolerance` | object | No | — | Quality profile and explicit metric thresholds Nested definition: see complete JSON schema. |
| `outputs` | object | No | — | PNG/SVG/report artifacts Nested definition: see complete JSON schema. |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### compare_sketch_to_reference

Compare actual CAD contours with a calibrated image/mask without automatic best-fit. Returns symmetric errors, per-entity attribution and overlays. CAD export runs on STA; raster metrics run in an isolated worker.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch_name` | string | Yes | — |  |
| `reference_image` | string | Yes | — |  |
| `transform` | object | No | — | saved calibration or explicit pixel-to-sketch matrix Nested definition: see complete JSON schema. |
| `tolerance` | object | No | — | draft/balanced/strict profile, explicit mean_mm, p95_mm, max_mm, min_iou/min_line_support, sampling Nested definition: see complete JSON schema. |
| `outputs` | object | No | — | overlay/png_preview, svg_overlay and report paths Nested definition: see complete JSON schema. |
| `contour_selection` | object | No | — | Reference segmentation Nested definition: see complete JSON schema. |
| `image_mode` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### compare_sketches

Compare two sketches directly as vector geometry with symmetric distances and entity-linked error ranking.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `reference_sketch` | string | Yes | — |  |
| `candidate_sketch` | string | Yes | — |  |
| `tolerance` | object | No | — | mean/p95/max/sample tolerances Nested definition: see complete JSON schema. |
| `unit` | string | No | — |  |
| `report_path` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### connect_solidworks

Connect to SolidWorks (launches it if needed). Disables the Freeze Bar and reports any modal dialog. Most other tools auto-connect, so calling this is optional.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### create_multibody_insert

Create an insert and scoped host pocket from explicit profiles with clearance verification and rollback. Useful for planar profile fits in a hand-built part. In a shape program start from a template (model_api_lookup: template:inlay, template:pin_socket, template:curved_inlay); model.surface_patch handles supported curved skins.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `insert_sketch` | object | Yes | — | Insert profile Nested definition: see complete JSON schema. |
| `insert_extrude` | object | Yes | — | Insert boss Nested definition: see complete JSON schema. |
| `host_body` | string | Yes | — |  |
| `insert_body` | string | Yes | — |  |
| `clearance` | number | Yes | — |  |
| `clearance_tolerance` | number | No | — |  |
| `pocket_sketch` | object | No | — | Optional explicit pocket profile Nested definition: see complete JSON schema. |
| `pocket_cut` | object | No | — | Scoped cut args Nested definition: see complete JSON schema. |
| `checkpoint` | boolean | No | — |  |
| `save_path` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | — |  |
| `idempotency_key` | string | No | — |  |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### create_new_assembly

Create a new assembly document.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### create_new_part

Create a new part document.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### create_parametric_sketch

Create named primitives, relations, dimensions and equations atomically with one rebuild, topology checks and rollback. Automatically verifies 2D working-view orientation and fit.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `name` | string | Yes | — |  |
| `plane` | string | No | — | enum: `["Front", "Top", "Right"]` |
| `unit` | string | No | — |  |
| `entities` | array | Yes | — | Entities with stable id and type Nested definition: see complete JSON schema. |
| `constraints` | array | No | — | Topological/geometric relations Nested definition: see complete JSON schema. |
| `dimensions` | array | No | — | Batch dimensions Nested definition: see complete JSON schema. |
| `equations` | array | No | — | Dimension equations Nested definition: see complete JSON schema. |
| `solve` | object | No | — | Definition target and guard policy Nested definition: see complete JSON schema. |
| `validation` | object | No | — | Contour/entity limits Nested definition: see complete JSON schema. |
| `transaction` | object | No | — | Rollback/checkpoint policy Nested definition: see complete JSON schema. |
| `idempotency_key` | string | No | — |  |
| `output_mode` | string | No | — | enum: `["locked_trace", "minimal_parametric", "reference_spline", "construction_reference"]` |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### create_revolved_body

Create and verify a separate revolved body from an explicit parametric sketch/axis with checkpoint and save. Use for profiles with arcs or splines; model.revolve covers polyline profiles, open or closed (rings, bushings).

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch` | object | Yes | — | create_parametric_sketch args Nested definition: see complete JSON schema. |
| `revolve` | object | Yes | — | revolve_boss args Nested definition: see complete JSON schema. |
| `body_name` | string | Yes | — |  |
| `feature_name` | string | No | — |  |
| `checkpoint` | boolean | No | — |  |
| `save_path` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | — |  |
| `idempotency_key` | string | No | — |  |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### create_semantic_primitive

DEPRECATED: use create_revolved_body, create_swept_member, create_multibody_insert, shell, mirror_feature, linear_pattern, circular_pattern, create_parametric_sketch, execute_cad_plan. Generic dispatcher hides the argument contract of existing concrete tools. Compatibility calls remain available; describe for migration details.

**Migration:**

```json
{
  "deprecated": true,
  "catalog_since": "6.24.0",
  "callable": true,
  "replacements": [
    "create_revolved_body",
    "create_swept_member",
    "create_multibody_insert",
    "shell",
    "mirror_feature",
    "linear_pattern",
    "circular_pattern",
    "create_parametric_sketch",
    "execute_cad_plan"
  ],
  "reason": "Generic dispatcher hides the argument contract of existing concrete tools.",
  "migration": {
    "revolved_shell": "create_revolved_body",
    "tubular_member": "create_swept_member",
    "clearance_insert|clearance_pocket|cap_and_opening": "create_multibody_insert",
    "uniform_shell": "shell",
    "symmetric_pair": "mirror_feature",
    "linear_feature_array": "linear_pattern",
    "circular_feature_array": "circular_pattern",
    "hole_or_rib_array": "linear_pattern or circular_pattern; remove the dispatcher pattern argument",
    "capsule_profile": "create_parametric_sketch with two semicircles and two tangent lines; not a drop-in argument rename",
    "printable_hinge": "execute_cad_plan with the existing explicit operations/invariants and checkpoint+rollback transaction"
  }
}
```

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `kind` | string | Yes | — |  |
| `parameters` | object | Yes | — | Pattern parameters Nested definition: see complete JSON schema. |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### create_sketch

Create a new sketch on a standard plane (Front/Top/Right), including localized or renamed planes, with geometrically verified Normal To and Fit to Screen. Resolves native RefPlane features in tree order and selects the feature directly; selection diagnostics include the resolved name and method.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `plane` | string | No | `"Front"` | Plane to sketch on enum: `["Front", "Top", "Right"]` |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### create_sketch_on_face

Create a sketch on an existing body face, picked by a 3D point that must lie ON that face (user units, mm default). Applies verified Normal To and Fit to Screen.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `x` | number | No | `0` |  |
| `y` | number | No | `0` |  |
| `z` | number | No | `0` |  |
| `unit` | string | No | — | Unit (mm, inch, m) |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### create_swept_member

Create a checked sweep from a new/existing path and circular, elliptical or custom profile. Validates path and profile contact; uses a native circle sketch on SW2026. For ordinary round members use model.tube or model.variable_tube.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `path_sketch` | string | No | — |  |
| `path` | object | No | — | Optional create_parametric_sketch path args Nested definition: see complete JSON schema. |
| `profile` | object | Yes | — | circle/ellipse/custom profile Nested definition: see complete JSON schema. |
| `body_name` | string | Yes | — |  |
| `feature_name` | string | No | — |  |
| `merge` | boolean | No | — |  |
| `min_bend_radius` | number | No | — |  |
| `allow_sharp_corners` | boolean | No | — |  |
| `checkpoint` | boolean | No | — |  |
| `save_path` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | — |  |
| `idempotency_key` | string | No | — |  |
| `unit` | string | No | — |  |
| `auto_verify` | boolean | No | — |  |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### cut_extrude

DEPRECATED: use advanced_cut. Legacy cut lacks explicit multibody scope and guarded verification. Compatibility calls remain available; describe for migration details.

**Migration:**

```json
{
  "deprecated": true,
  "catalog_since": "6.24.0",
  "callable": true,
  "replacements": [
    "advanced_cut"
  ],
  "reason": "Legacy cut lacks explicit multibody scope and guarded verification.",
  "migration": "Use advanced_cut with explicit scope_bodies for multibody parts; describe its schema first."
}
```

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `depth` | number | No | `10` |  |
| `through_all` | boolean | No | `false` |  |
| `both_directions` | boolean | No | `false` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### delete_feature

Delete a feature by name. Set delete_absorbed=true to also remove the absorbed sketch (else it orphans on SW2026).

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `name` | string | Yes | — |  |
| `delete_absorbed` | boolean | No | `false` | Also delete absorbed sketches/features |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### draw_arc

Draw an arc by center and angles in the active sketch (sketch-local 2D coords) with verified Normal To / Fit.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `cx` | number | No | `0` |  |
| `cy` | number | No | `0` |  |
| `radius` | number | No | `25` |  |
| `start_angle` | number | No | `0` |  |
| `end_angle` | number | No | `90` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### draw_circle

Draw a circle in the active sketch (sketch-local 2D coords, user units) with verified Normal To / Fit.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `x` | number | No | `0` |  |
| `y` | number | No | `0` |  |
| `radius` | number | No | `25` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### draw_line

Draw in the active sketch using sketch-local 2D coordinates and user units. For model-space contours use sketch_contour. Automatically verifies Normal To and Fit.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `x1` | number | No | `0` |  |
| `y1` | number | No | `0` |  |
| `x2` | number | No | `100` |  |
| `y2` | number | No | `0` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### draw_polygon

Draw a regular polygon in the active sketch (sketch-local 2D coords) with verified Normal To / Fit.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `cx` | number | No | `0` |  |
| `cy` | number | No | `0` |  |
| `radius` | number | No | `25` |  |
| `sides` | integer | No | `6` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### draw_rectangle

Draw a rectangle in the active sketch (sketch-local 2D coords, user units) with verified Normal To / Fit.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `x1` | number | No | `-50` |  |
| `y1` | number | No | `-25` |  |
| `x2` | number | No | `50` |  |
| `y2` | number | No | `25` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### execute_cad_plan

Run a bounded expert operation plan with $steps.N result references and predefined conditions. Use execute_model_code for ordinary shape composition.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `plan_id` | string | Yes | — |  |
| `operations` | array | Yes | — | Plan steps Nested definition: see complete JSON schema. |
| `transaction` | object | No | — | Checkpoint/rollback policy Nested definition: see complete JSON schema. |
| `invariants` | object | No | — | Plan invariants Nested definition: see complete JSON schema. |
| `unit` | string | No | — |  |
| `budget` | object | No | — | Plan budget Nested definition: see complete JSON schema. |
| `save_path` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` |  |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### execute_python

Run Python on the shared STA with sw, doc, automation and typed COM helpers; print returns output. Raw API units are metres. Prefer shape programs for supported geometry. A run longer than one call answers status RUNNING: collect it with wait_operation; waiting or a caller timeout never cancels COM.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `code` | string | Yes | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### execute_python_async

DEPRECATED: use execute_python, wait_operation. Every call now answers RUNNING after the synchronous wait, so a separate asynchronous entry is redundant. Compatibility calls remain available; describe for migration details.

**Migration:**

```json
{
  "deprecated": true,
  "catalog_since": "6.29.4",
  "callable": true,
  "replacements": [
    "execute_python",
    "wait_operation"
  ],
  "reason": "Every call now answers RUNNING after the synchronous wait, so a separate asynchronous entry is redundant.",
  "migration": "Call execute_python with the same code; when it answers status RUNNING collect the result with wait_operation(operation_id)."
}
```

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `code` | string | Yes | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### export_bundle

Export selected bodies to explicit SLDPRT/STEP/STL destinations with rebuild, structural checks, hashes and atomic rollback. Supports expert destination/overwrite workflows; export_model is the stricter new-directory shape delivery route.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sldprt_path` | string | Yes | — |  |
| `step_path` | string | Yes | — |  |
| `stl_directory` | string | Yes | — |  |
| `bodies` | array | No | — | Nested definition: see complete JSON schema. |
| `naming` | object | No | — | Body-to-filename mapping Nested definition: see complete JSON schema. |
| `stl` | object | No | — | STL quality, deviation_mm, angle_tolerance_deg, binary, and preserve_origin settings Nested definition: see complete JSON schema. |
| `report` | boolean | No | — |  |
| `overwrite` | boolean | No | — |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### export_file

Export the active document by extension: STEP, STL, IGES, Parasolid (.x_t/.x_b), 3MF, SAT, WRL, PLY, etc. A shape model is delivered with export_model (checked SLDPRT, STEP, STL).

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `filepath` | string | Yes | — | Output path; extension selects format |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### export_sketch_geometry

Export true CAD primitives, stable IDs, transforms, topology, relations and compact summary; full geometry goes to a file unless inline mode is explicitly requested.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch_name` | string | Yes | — |  |
| `coordinate_system` | string | No | — |  |
| `unit` | string | No | — |  |
| `include` | object | No | — | construction/relations/dimensions/topology Nested definition: see complete JSON schema. |
| `output` | object | No | — | summary_and_file/compact/inline output Nested definition: see complete JSON schema. |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### extrude_sketch

DEPRECATED: use advanced_extrude. Legacy extrusion lacks the guarded feature verification of advanced_extrude. Compatibility calls remain available; describe for migration details.

**Migration:**

```json
{
  "deprecated": true,
  "catalog_since": "6.24.0",
  "callable": true,
  "replacements": [
    "advanced_extrude"
  ],
  "reason": "Legacy extrusion lacks the guarded feature verification of advanced_extrude.",
  "migration": "Map the legacy Blind/MidPlane arguments to advanced_extrude; describe its schema first."
}
```

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `depth` | number | No | `10` |  |
| `both_directions` | boolean | No | `false` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### fillet_edges

Constant-radius fillet. Edges are picked by rays (edge_rays, hit only VISIBLE bodies) or must be pre-selected. Radius in user units (mm default). Auto-verifies (GetFaces) and deletes dead features. In a shape program use model.fillet: it belongs to the program and survives edits.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `radius` | number | No | `2` |  |
| `edge_rays` | array | No | — | Rays selecting edges: [{'origin':[x,y,z],'direction':[dx,dy,dz]}] Nested definition: see complete JSON schema. |
| `feature_name` | string | No | — |  |
| `auto_verify` | boolean | No | `true` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### fix_freeze_bar

Disable the Freeze Bar and move it to the tree top. Automatic on connect and supported feature operations; use manually only to recover frozen features.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### get_capabilities

Inspect installed interfaces, operations, backends, export formats and limits when diagnosing an unsupported operation. Reports optional vectorization weights, missing files, model paths and CUDA readiness. Ordinary modeling starts with get_model_context.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### get_document_info

Read the active document title, type, path and feature/body counts. Use get_model_context for shape-program parameters and revision.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### get_environment_status

Diagnostics: connection, active doc, typed makepy module availability, freeze-bar state, modal dialog.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### get_feature_status

Read feature type, suppression and owned face count. Zero faces alone does not prove failure for reference geometry, transforms or consumed upstream features.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `name` | string | Yes | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### get_job_result

DEPRECATED: use wait_operation. Alias kept from before every long call answered RUNNING. Compatibility calls remain available; describe for migration details.

**Migration:**

```json
{
  "deprecated": true,
  "catalog_since": "6.29.4",
  "callable": true,
  "replacements": [
    "wait_operation"
  ],
  "reason": "Alias kept from before every long call answered RUNNING.",
  "migration": "wait_operation(operation_id=<job_id>, wait_sec=<timeout>) returns the same result."
}
```

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `job_id` | string | Yes | — |  |
| `timeout` | number | No | `0` | Seconds to block waiting (0 = return immediately) |

### get_session_metrics

Read runtime counters of the expert transaction layer: rebuild/rollback counts, per-tool timings and the progress of the active operation. To review a modeling run (calls, answer sizes, slowest operations) use get_session_report.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### get_session_report

COM-free counters of this MCP session from its local call trace: calls and seconds by tool, mutations and how many answered RUNNING, recovery calls, errors by code, answer and input sizes, slowest operations. Program text is never stored. Use it to review a modeling run.

No named inputs in the schema.

### get_sketch_status

Diagnostic: active sketch state, sketch count, sketch names.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### get_solidworks_info

Get SolidWorks installation information.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### hide_body

Hide a body by name.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `name` | string | Yes | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### image_to_sketch

Convert raster line art or silhouettes into validated native sketch geometry. Use for an actual tracing task; choose trace/output modes and calibration explicitly. Deep backends require optional model weights; MODEL_WEIGHTS_MISSING or MODEL_WEIGHTS_INVALID explains how to install or repair them. Ordinary CAD remains available without weights. Analysis is isolated; CAD commit is transactional. Ordinary 3D references do not require tracing.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `image_path` | string | Yes | — |  |
| `sketch_name` | string | Yes | — |  |
| `plane` | string | No | — | enum: `["Front", "Top", "Right"]` |
| `unit` | string | No | — |  |
| `image_mode` | string | No | — | enum: `["filled_silhouette", "technical_drawing", "line_drawing", "trace_as_is"]` |
| `contour_selection` | object | No | — | ROI/topology selection Nested definition: see complete JSON schema. |
| `trace` | object | No | — | Explicit boundary selection policy Nested definition: see complete JSON schema. |
| `calibration` | object | Yes | — | bbox/two-point/mm-per-pixel calibration Nested definition: see complete JSON schema. |
| `placement` | object | No | — | Named image anchor and model placement Nested definition: see complete JSON schema. |
| `geometry` | object | No | — | Legacy alias for approximation controls Nested definition: see complete JSON schema. |
| `approximation` | object | No | — | Primitive fitting controls Nested definition: see complete JSON schema. |
| `validation` | object | No | — | IoU/Hausdorff/topology gates Nested definition: see complete JSON schema. |
| `commit` | object | No | — | Confidence/checkpoint/rollback policy Nested definition: see complete JSON schema. |
| `debug` | object | No | — | Overlay/vector artifact policy Nested definition: see complete JSON schema. |
| `models` | object | No | — | Optional local SAM/ViTMatte model IDs Nested definition: see complete JSON schema. |
| `require_orthographic` | boolean | No | `false` | Reject implicit perspective tracing unless projection.mode is orthographic or homography |
| `projection` | object | No | — | Explicit orthographic/perspective policy Nested definition: see complete JSON schema. |
| `idempotency_key` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### linear_pattern

Linear pattern of feature(s) along a direction (edge/axis by ray or name). spacing in user units. NOTE: patterning a lone base body (no cuts/bosses) does not materialize on SW2026 - pattern the cut/boss feature instead. Auto-verifies. In a shape program use model.pattern(count=, step=).

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `seed_features` | array | Yes | — | Nested definition: see complete JSON schema. |
| `direction_edge_ray` | object | No | — |  |
| `direction_entity` | string | No | — | Edge/axis/plane name for direction 1 |
| `count` | integer | No | `3` |  |
| `spacing` | number | No | `20` |  |
| `reverse` | boolean | No | `false` |  |
| `count2` | integer | No | `1` |  |
| `spacing2` | number | No | `20` |  |
| `direction2_edge_ray` | object | No | — |  |
| `direction2_entity` | string | No | — |  |
| `reverse2` | boolean | No | `false` |  |
| `feature_name` | string | No | — |  |
| `auto_verify` | boolean | No | `true` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### list_bodies

List solid bodies: name, visibility, bbox, face count.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `include_hidden` | boolean | No | `true` |  |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### list_features

List all features in the model.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### list_jobs

DEPRECATED: use get_executor_status, get_session_report. Lists only the jobs of execute_python_async. Compatibility calls remain available; describe for migration details.

**Migration:**

```json
{
  "deprecated": true,
  "catalog_since": "6.29.4",
  "callable": true,
  "replacements": [
    "get_executor_status",
    "get_session_report"
  ],
  "reason": "Lists only the jobs of execute_python_async.",
  "migration": "get_executor_status(operation_id=...) reports one operation; get_session_report summarises the session."
}
```

No named inputs in the schema.

### list_open_documents

List all open documents.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### mirror_feature

Mirror feature(s) or bodies about a plane/planar face (mirror_plane name or mirror_face_ray). Provide seed_features OR mirror_bodies. In a shape program use model.mirror.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `mirror_plane` | string | No | — | Mirror plane/face name, e.g. 'Right Plane' |
| `mirror_face_ray` | object | No | — |  |
| `seed_features` | array | No | — | Nested definition: see complete JSON schema. |
| `mirror_bodies` | array | No | — | Nested definition: see complete JSON schema. |
| `merge` | boolean | No | `true` |  |
| `feature_name` | string | No | — |  |
| `auto_verify` | boolean | No | `true` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### normal_to_sketch

Orient an active 2D sketch face-on, preserve the nearest stable side/up, then verify axes and pixel framing. Automatic for supported sketch tools; no extra call needed after them.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `zoom_to_fit` | boolean | No | `true` |  |
| `angular_tolerance_deg` | number | No | `0.1` | minimum: `0.001` maximum: `5.0` |
| `prefer_native` | boolean | No | `true` |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### open_document

Open an existing SolidWorks document. Shape tools that take document_path open or activate that part themselves.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `filepath` | string | Yes | — | Path to file |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### probe_ray

Return the first hit point and entity/body along a ray. Hits visible bodies only; hide occluders when needed. Coordinates and tolerance use user units.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `origin` | array | Yes | — | [x,y,z] ray origin Nested definition: see complete JSON schema. |
| `direction` | array | Yes | — | [dx,dy,dz] ray direction Nested definition: see complete JSON schema. |
| `sel_type` | string | No | `"face"` | enum: `["face", "edge", "vertex", "body"]` |
| `radius` | number | No | `0.01` | Ray radius / tolerance (user units) |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### probe_rays

Batch first-hit ray queries on visible bodies. Use for a specific location or section question; coordinates use user units.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `rays` | array | Yes | — | List of {'origin':[x,y,z],'direction':[dx,dy,dz]} Nested definition: see complete JSON schema. |
| `sel_type` | string | No | `"face"` | enum: `["face", "edge", "vertex", "body"]` |
| `radius` | number | No | `0.01` |  |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### probe_section

Sample visible bodies with a radial ray fan in axis=value (x/y/z). Returns the first hit per ray; theta=0 is +X for z, +Y for x, +Z for y. For body-scoped shape sections use inspect_model.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `axis` | string | No | `"z"` | Section plane normal enum: `["x", "y", "z"]` |
| `value` | number | No | `0` | Plane coordinate along axis (user units) |
| `origin` | array | No | — | Fan centre [x,y,z] (axis component overridden by value). Default [0,0,0] Nested definition: see complete JSON schema. |
| `n_rays` | integer | No | `36` |  |
| `angle_start` | number | No | `0` |  |
| `angle_end` | number | No | `360` |  |
| `sel_type` | string | No | `"face"` | enum: `["face", "edge", "vertex", "body"]` |
| `radius` | number | No | `0.01` |  |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### recover_environment

Apply a bounded recovery policy: inspect UI, clear selection, exit sketch, repair Freeze Bar and at most one explicitly requested retry. Does not resolve an unknown COM outcome.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `retry_operation` | object | No | — | Optional {op,args} retry Nested definition: see complete JSON schema. |
| `max_retries` | integer | No | — | minimum: `0` maximum: `1` |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### reference_axis

Create a reference axis (InsertAxis2) from defining entities, e.g. two planes.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `entity_names` | array | Yes | — | Entities 'name:type' (type PLANE/FACE/EDGE/VERTEX), e.g. ['Front Plane:PLANE','Right Plane:PLANE'] Nested definition: see complete JSON schema. |
| `feature_name` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### reference_plane

Create a reference plane offset from a plane or planar face (source by name, or source_ray to pick a face). offset in user units (mm default).

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `source` | string | No | — | Source plane/face name, e.g. 'Front Plane' |
| `source_ray` | object | No | — | Ray to pick a planar face |
| `offset` | number | No | `10` |  |
| `reverse` | boolean | No | `false` |  |
| `feature_name` | string | No | — |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### rename_body

Rename a solid body.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `old_name` | string | Yes | — |  |
| `new_name` | string | Yes | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### rename_feature

Rename a feature. Reads back the ACTUAL resulting name (SolidWorks silently keeps the old name on collision); on collision an auto-suffixed name (_2) is applied and reported with a warning.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `old_name` | string | Yes | — |  |
| `new_name` | string | Yes | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### render_sketch_svg

Render one or more exported sketches to layered, parse-verified SVG while preserving line/circle/arc/spline entity identities.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch_names` | array | Yes | — | Nested definition: see complete JSON schema. |
| `path` | string | Yes | — |  |
| `view` | object | No | — | viewBox/unit/padding Nested definition: see complete JSON schema. |
| `style` | object | No | — | Layer/entity colors Nested definition: see complete JSON schema. |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### restart_solidworks

Verified SolidWorks restart. Use it to release memory after a SOLIDWORKS_MEMORY_HIGH warning (get_executor_status: memory), and for a quarantined session whose MCP owner process exited (owner_alive=false); then follow with reconcile_session(operation_id). Lists open documents and refuses when any is unsaved unless discard_unsaved=true; then exits SolidWorks, waits for the process to end, starts it and reconnects. Documents are not reopened: shape tools open document_path themselves. Returns previous and new PIDs. Takes minutes: collect with wait_operation.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `discard_unsaved` | boolean | No | `false` | Close unsaved documents without saving and end an unresponsive process |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### revolve_boss

Revolve a profile sketch into a solid (FeatureRevolve2). The sketch must contain a centerline (sketch_contour type 'centerline') or provide axis_name. angle in degrees (360=full); thin_thickness in user units. Auto-verifies.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `sketch_name` | string | No | — |  |
| `angle` | number | No | `360` |  |
| `axis_name` | string | No | — | Axis entity name (else sketch centerline) |
| `reverse` | boolean | No | `false` |  |
| `merge` | boolean | No | `true` |  |
| `thin` | boolean | No | `false` |  |
| `thin_thickness` | number | No | `1` |  |
| `feature_name` | string | No | — |  |
| `auto_verify` | boolean | No | `true` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### run_transaction

Execute a whitelisted operation sequence with checkpoint, invariants and rollback. For expert operations; execute_model_code already owns its transaction.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `name` | string | Yes | — |  |
| `operations` | array | Yes | — | Ordered {op,args,when} operations Nested definition: see complete JSON schema. |
| `checkpoint` | object | No | — | Checkpoint policy/path Nested definition: see complete JSON schema. |
| `invariants` | object | No | — | Post-commit invariants Nested definition: see complete JSON schema. |
| `on_failure` | string | No | — | enum: `["rollback", "leave_partial"]` |
| `idempotency_key` | string | No | — |  |
| `save_policy` | object | No | — | save_before/save_after_success policy Nested definition: see complete JSON schema. |
| `budget` | object | No | — | Elapsed/rebuild/solver/rollback limits Nested definition: see complete JSON schema. |
| `save_path` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` |  |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### save_document

Save the active document, optionally to an explicit path.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `filepath` | string | No | — | Path to save (optional for Save As) |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### section_screenshot

Temporarily enable a section plane, frame and capture it, then disable the section and verify cleanup. Returns unreadable-frame diagnostics. Use for a specific internal-geometry question.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `path` | string | Yes | — | Output image path (.jpg/.png) |
| `plane` | string | No | `"Front"` | Front \| Top \| Right or exact plane feature name |
| `offset` | number | No | `0` | Section plane offset (user units) |
| `flip` | boolean | No | `false` | Flip which side is cut away |
| `orientation` | string | No | — | Optional named view before capture |
| `view_direction` | array | No | — | Nested definition: see complete JSON schema. |
| `up_direction` | array | No | — | Nested definition: see complete JSON schema. |
| `zoom_to_bodies` | array | No | — | Nested definition: see complete JSON schema. |
| `zoom_bbox` | object | No | — |  |
| `zoom_to_fit` | boolean | No | `true` |  |
| `keep_section` | boolean | No | `false` | Leave the section view ON after capture |
| `width` | integer | No | — |  |
| `height` | integer | No | — |  |
| `compress` | boolean | No | `true` |  |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### select_face_by_ray

Select a face by ray with a given Mark, for reference end/start-conditions (more reliable than SelectByID2 at rounded coordinates). Coords in user units; hits only VISIBLE bodies.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `origin` | array | Yes | — | Nested definition: see complete JSON schema. |
| `direction` | array | Yes | — | Nested definition: see complete JSON schema. |
| `mark` | integer | No | `0` |  |
| `append` | boolean | No | `false` |  |
| `radius` | number | No | `0.01` |  |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### set_body_color

Set body colour, optionally transparency, preserving other material properties. rgb accepts 0-255 or 0-1 when all values are at most 1.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `name` | string | Yes | — |  |
| `rgb` | array | Yes | — | [r,g,b] 0-255 (or 0-1) Nested definition: see complete JSON schema. |
| `transparency` | number | No | — | Optional 0.0-1.0 (unchanged if omitted) |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### set_body_transparency

Set body transparency from 0 (opaque) to 1 (invisible), preserving colour; verified by read-back.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `name` | string | Yes | — |  |
| `transparency` | number | No | `0.5` |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### set_units

Set default unit for dimensions.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `unit` | string | Yes | — | enum: `["mm", "inch", "m", "cm"]` |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### set_view_orientation

Set the camera orientation (named view or custom view_direction/up_direction) and optionally zoom to fit.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `orientation` | string | No | `"isometric"` |  |
| `view_direction` | array | No | — | Nested definition: see complete JSON schema. |
| `up_direction` | array | No | — | Nested definition: see complete JSON schema. |
| `zoom_to_fit` | boolean | No | `true` |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### shell

Hollow the body to a wall thickness, removing the faces hit by face_rays (which hit only VISIBLE bodies). No face_rays = closed hollow. thickness in user units. Auto-verifies.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `thickness` | number | No | `2` |  |
| `face_rays` | array | No | — | Rays selecting faces to remove Nested definition: see complete JSON schema. |
| `outward` | boolean | No | `false` |  |
| `feature_name` | string | No | — |  |
| `auto_verify` | boolean | No | `true` |  |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### show_body

Show a hidden body by name.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `name` | string | Yes | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### sketch_contour

Draw model-space line/arc/centerline segments in a sketch; coordinates use user units. plane or face_ray creates the sketch. Checks closure and self-intersection; close=false permits an open chain. Automatically verifies Normal To and Fit.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `plane` | string | No | — | Plane to create sketch on (if no active sketch) enum: `["Front", "Top", "Right"]` |
| `face_ray` | object | No | — | {'origin':[x,y,z],'direction':[dx,dy,dz]} to sketch on a face Nested definition: see complete JSON schema. |
| `segments` | array | Yes | — | Segments in MODEL coords. line: {type,start[x,y,z],end}. arc: {type,center,start,end,direction}. centerline: {type,start,end} (construction/revolve axis) Nested definition: see complete JSON schema. |
| `add_to_db` | boolean | No | `true` |  |
| `close` | boolean | No | `true` | Expect a closed contour and error if it is not (false = partial chain, skip strict check) |
| `unit` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | `false` | Explicitly allow first geometry in an unsaved document |
| `save_path` | string | No | — | Save unsaved document before first geometry |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### sync_model_graph

Diff and apply a whitelisted expert operation DAG transactionally. Retained for operation graphs beyond shape constructors; execute_model_code owns ordinary shape dependencies.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `graph_id` | string | Yes | — |  |
| `nodes` | array | Yes | — | DAG nodes Nested definition: see complete JSON schema. |
| `mode` | string | No | — | enum: `["plan", "apply"]` |
| `invariants` | object | No | — | Graph invariants Nested definition: see complete JSON schema. |
| `save_path` | string | No | — |  |
| `allow_unsaved_document` | boolean | No | — |  |
| `budget` | object | No | — | Stop-loss limits |
| `ui_guard` | object | No | — | Watchdog and known-dialog policy |
| `recovery` | object | No | — | Finite recovery policy; auto retry is capped at one |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |
| `idempotency_key` | string | No | — | Durable request identity; shape mutations derive it from document, starting revision and payload when omitted. An identical request is answered from its receipt (request_receipt.replayed) instead of running twice. It runs again when nothing would be duplicated: a create that left no file, or the same edit after mode=revert. An identical edit that FAILED is not run again: pass a new key to repeat it. minLength: `1` maxLength: `256` |
| `document_path` | string | No | — | Active saved part path; required with idempotency_key or plan_id |
| `expected_revision` | string | No | — | Starting revision from get_model_context; required with idempotency_key or plan_id |

### take_screenshot

Capture the active document with optional orientation and body/bbox framing. full_window includes the UI; zoom_to_fit=false preserves framing. frame_unreadable=true is failed visual evidence. For revision-bound multiview capture use observe_model.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `path` | string | Yes | — | Output image path (.jpg/.png) |
| `orientation` | string | No | — | isometric, front, back, left, right, top, bottom, trimetric, dimetric |
| `view_direction` | array | No | — | Custom look direction [dx,dy,dz] Nested definition: see complete JSON schema. |
| `up_direction` | array | No | — | Nested definition: see complete JSON schema. |
| `zoom_to_fit` | boolean | No | `true` | Fit part to window before capture (default on) |
| `zoom_to_bodies` | array | No | — | Frame the view on these bodies (overrides zoom_to_fit) Nested definition: see complete JSON schema. |
| `zoom_bbox` | object | No | — | Frame on {'min':[x,y,z],'max':[x,y,z]} in user units (overrides zoom_to_fit) |
| `width` | integer | No | — |  |
| `height` | integer | No | — |  |
| `compress` | boolean | No | `true` |  |
| `full_window` | boolean | No | `false` | Capture the WHOLE SolidWorks window (ribbon, tree, status bar) for debugging, not just the model. Overlapping windows will appear. |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

### zoom_to

Center the live working view on named bodies, a feature or a bbox with margin. Use before focused expert/raw edits; observe_model restores this working view after its captures.

| Input | Type | Required | Default | Description and constraints |
|---|---|---|---|---|
| `bodies` | array | No | — | Body names to frame Nested definition: see complete JSON schema. |
| `feature` | string | No | — | Feature name to frame |
| `bbox` | object | No | — | {'min':[x,y,z],'max':[x,y,z]} in user units |
| `margin` | number | No | `0.15` | Margin fraction of the largest span |
| `unit` | string | No | — |  |
| `execution_timeout_sec` | number | No | — | How long this call waits, including queue time. The operation keeps running after that: the call returns status RUNNING and wait_operation collects the result. maximum: `45.0` exclusiveMinimum: `0` |

## Maintaining this reference

```powershell
python scripts/generate_tool_docs.py
python scripts/generate_tool_docs.py --check
```

Generation only reads tool definitions. It does not connect to or modify SOLIDWORKS.
