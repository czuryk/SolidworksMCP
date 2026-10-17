"""Compact MCP surface for native shape programming."""

from mcp.types import Tool


def schema(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


VERBOSE = {"type": "boolean", "default": False,
           "description": "Return the complete legacy answer instead of the compact one"}

SHAPE_TOOLS = [
    Tool(name="get_model_context",
         description='Start here: connect and read the active document, revision, parameters and bodies. Compact summary by default; components=[names] adds those component records, detail=full the whole map. include_source retrieves its editable program; check_edit_compatibility optionally checks unmanaged features.',
         inputSchema=schema({"include_source":{"type":"boolean","default":False},
             "detail":{"type":"string","enum":["summary","full"],"default":"summary",
                 "description":"full adds the complete component map and static API fields"},
             "components":{"type":"array","minItems":1,"maxItems":32,"uniqueItems":True,"items":{"type":"string"},
                 "description":"Component names whose records are added to the summary"},
             "verbose":VERBOSE,
             "check_edit_compatibility":{"type":"boolean","default":False,
                 "description":"Optional native feature inventory check for unmanaged changes; adds a feature walk. Does not certify every native definition."}})),
    Tool(name="model_api_lookup",
         description="Look up shape constructor signatures, a topic or example; COM-free. topic='index' lists constructors and topics; topic='all' returns the whole reference in one call (cheaper than many lookups); 'workflow' is the recommended order of calls, 'pitfalls' behaviour worth knowing before the first model, 'examples' one working snippet per constructor, 'templates' vetted programs for typical joints (peg and socket, inlay, hinge, collar) and 'template:<name>' one of them as code. An unknown topic answers with did_you_mean.",
         inputSchema=schema({"topic": {"type": "string", "default": "index"}})),
    Tool(name="execute_model_code",
         description=(
             'Build or edit native solids from Python using the injected model. Analyze is COM-free and returns the predicted extents of every resulting body in milliseconds (draft_views adds silhouette images): check proportions there before a native build that takes minutes; with document_path it previews the edit plan (would_rebuild/would_add/would_remove) without mutation. Create saves a new part. Edit needs document_path and a fresh expected_revision plus one of: patch (small exact replacements in the embedded program, preferred), code (a complete revised program), or only parameters; a failed edit restores the saved part. Revert undoes the last edit. Create and edit frame the working geometry and return views by default. Read warnings in every answer: TUBE_BEND_TOO_TIGHT and SURFACE_PATCH_WIDE_MASK predict a native failure, NATIVE_BODY_INVALID means export_model will refuse the body.'),
         inputSchema=schema({
             "code": {"type": "string", "maxLength": 150000},
             "patch": {"type": "array", "minItems": 1, "maxItems": 64,
                       "description": "Edit the embedded program in place: ordered replacements, each old must occur exactly once. Mutually exclusive with code.",
                       "items": schema({"old": {"type": "string", "minLength": 1},
                                        "new": {"type": "string"}}, ["old", "new"])},
             "mode": {"type": "string", "enum": ["analyze", "create", "edit", "revert"], "default": "analyze",
                      "description": "revert restores the saved state before the last edit (document_path and expected_revision only; up to three edits back, seconds instead of a rebuild)"},
             "output_path": {"type": "string", "description": "New absolute .SLDPRT path; required for create"},
             "parameters": {"type": "object", "additionalProperties": {"type": "number"}},
             "observe": {"default": True,
                         "description": "Views returned after create/edit: true (front, right, isometric), false (none), or a list of views as in observe_model, e.g. [\"front\"]",
                         "oneOf": [{"type": "boolean"},
                                   {"type": "array", "minItems": 1, "maxItems": 6,
                                    "items": {"oneOf": [{"type": "string"}, {"type": "object"}]}}]},
             "draft_views": {"type": "array", "minItems": 1, "maxItems": 6, "uniqueItems": True,
                             "items": {"type": "string", "enum": ["front", "back", "right", "left", "top", "bottom"]},
                             "description": "analyze only: COM-free silhouette images of the program in these views"},
             "draft_components": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True,
                                  "items": {"type": "string"},
                                  "description": "analyze only: also return predicted extents of these components (consumed ones included)"},
             "draft_directory": {"type": "string", "description": "analyze only: absolute directory for draft images; a temporary one by default"},
             "verbose": VERBOSE,
             "document_path": {"type": "string", "description": "Absolute saved part path; required for edit and revert, optional for analyze (edit plan). An open document is activated, a closed one is opened"},
             "expected_revision": {"type": "string", "description": "Fresh revision from get_model_context or the last answer; required for edit and revert"},
         })),
    Tool(name="observe_model",
         description='Capture revision-bound native views at shared scale; restores the working camera afterward. components gives a close-up with surrounding context. Defaults to Front, Right and Isometric; a view may also be a custom camera {name, direction, up}. flat=true writes <directory>/<view>.png instead of a random subfolder. sections adds targeted radial samples.',
         inputSchema=schema({
             "directory": {"type": "string", "description": "Optional absolute image directory; defaults beside the part"},
             "document_path": {"type": "string", "description": "Absolute saved part path; an open document is activated, a closed one is opened"},
             "expected_revision": {"type": "string", "description": "Fresh revision from get_model_context or the last answer"},
             "components": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True,
                            "items": {"type": "string"}, "description": "Shape component names to frame; consumed components must be addressed through their result"},
             "sections": {"type": "array", "minItems": 1, "maxItems": 4,
                          "description": "Optional targeted native radial sections; no CAD entities. Center must be inside the selected single-body component. Samples are first outward hits, not a full circularity certificate.",
                          "items": schema({
                              "component": {"type": "string"},
                              "center": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                              "normal": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                              "samples": {"type": "integer", "minimum": 8, "maximum": 64, "default": 16},
                              "expected_radius": {"type": "number", "exclusiveMinimum": 0},
                              "tolerance_mm": {"type": "number", "exclusiveMinimum": 0},
                          }, ["component", "center", "normal"])},
             "views": {"type": "array", "minItems": 1, "maxItems": 6,
                       "description": "Standard view names and/or custom cameras, each with a unique name",
                       "items": {"oneOf": [
                           {"type": "string", "enum": [
                               "front", "back", "right", "left", "top", "bottom", "isometric"]},
                           schema({"name": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,39}$",
                                            "description": "File and view name; not a standard view name"},
                                   "direction": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                                                 "description": "Camera look direction in model coordinates, from the eye into the model; front is [0,0,-1], right is [-1,0,0]"},
                                   "up": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                                          "description": "Screen-up vector; default [0,1,0]"}},
                                  ["name", "direction"])]}},
             "flat": {"type": "boolean", "default": False,
                      "description": "Write <directory>/<view>.png directly; existing files are refused unless overwrite=true"},
             "overwrite": {"type": "boolean", "default": False, "description": "With flat=true, replace existing view files"},
             "width": {"type": "integer", "minimum": 256, "maximum": 2000, "default": 1000},
             "verbose": VERBOSE,
         }, ["document_path", "expected_revision"])),
    Tool(name="measure_reference",
         description='Measure visible image extents in explicit pixel ROIs; COM-free, no CAD entities. With calibration, expected_mm and tolerance_mm each query returns check {delta_mm, passed: true/false/null}: batch 5-10 landmarks per approved view before delivery. Mark occluded boundaries unknown. A query with color measures one coloured element inside the figure (eyes, teeth). Not needed to start the first volume; this is not semantic segmentation.',
         inputSchema=schema({
             "image_path": {"type": "string", "description": "Absolute local reference image path"},
             "projection": {"type": "string", "enum": ["front", "right", "top", "perspective", "unknown"], "default": "unknown"},
             "queries": {"type": "array", "minItems": 1, "maxItems": 12, "items": schema({
                 "id": {"type": "string", "minLength": 1, "maxLength": 80},
                 "kind": {"type": "string", "enum": ["bbox", "center", "aspect_ratio", "width_at_height", "depth_range"]},
                 "region": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 4, "maxItems": 4,
                            "description": "[left,top,right,bottom] pixels; right/bottom exclusive; image origin top left"},
                 "y": {"type": "integer", "minimum": 0, "description": "Image row for width_at_height/depth_range"},
                 "visibility": {"type": "string", "enum": ["visible", "occluded", "unknown"], "default": "visible"},
                 "expected_mm": {"type": "object", "minProperties": 1, "additionalProperties": False,
                                 "description": "Model size of this element in mm: width/height/center_x/center_y for bbox, center_x/center_y for center, width for row measurements. Needs calibration",
                                 "properties": {key: {"type": "number"} for key in ("width", "height", "center_x", "center_y")}},
                 "tolerance_mm": {"type": "number", "exclusiveMinimum": 0,
                                  "description": "Allowed absolute deviation per expected value; without it passed stays null"},
                 "color": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "integer", "minimum": 0, "maximum": 255},
                           "description": "Measure only pixels of this RGB colour (an eye, the teeth, a dark outline) instead of everything that differs from the background"},
                 "color_tolerance": {"type": "integer", "minimum": 4, "maximum": 120, "default": 30,
                                     "description": "Largest channel difference from color that still counts"},
             }, ["id", "kind", "region"])},
             "calibration": schema({"pixel_distance": {"type": "number", "exclusiveMinimum": 0},
                                    "length_mm": {"type": "number", "exclusiveMinimum": 0},
                                    "origin_px": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"},
                                                  "description": "Pixel of the model origin; expected centers are then model coordinates with Y up"}},
                                   ["pixel_distance", "length_mm"]),
             "background": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "integer", "minimum": 0, "maximum": 255}},
             "threshold": {"type": "integer", "minimum": 9, "maximum": 246, "default": 32},
             "annotation_directory": {"type": "string", "description": "Optional absolute directory for annotated crop images"},
         }, ["image_path", "queries"])),
]

SHAPE_TOOLS.append(
    Tool(name="compare_reference",
         description='Compare the model silhouette with a reference image in one call, seconds. The silhouette is drafted without SolidWorks from code (before any native build) or from the saved part at document_path; source="native" with document_path uses the bodies as built (cuts, patches and anchored features included) and is the check before delivery. Returns IoU, size deltas and a level-by-level table of left/right extents in mm with the worst deviations first, plus an overlay image. Use it to fix proportions before create and as the numeric likeness check before delivery; one call per approved orthographic view.',
         inputSchema=schema({
             "image_path": {"type": "string", "description": "Absolute path of the reference image; uniform background"},
             "view": {"type": "string", "enum": ["front", "back", "right", "left", "top", "bottom"], "default": "front",
                      "description": "Orthographic view the reference shows"},
             "code": {"type": "string", "maxLength": 150000, "description": "Shape program to draft; alternative to document_path"},
             "document_path": {"type": "string", "description": "Saved part: its embedded program is drafted, or with source=native its built bodies are measured"},
             "source": {"type": "string", "enum": ["draft", "native"], "default": "draft",
                        "description": "draft: predicted from the program, COM-free; native: tessellation of the built bodies of document_path"},
             "parameters": {"type": "object", "additionalProperties": {"type": "number"}},
             "height_mm": {"type": "number", "exclusiveMinimum": 0,
                           "description": "Real height of the reference silhouette in mm; default: the model height (then only proportions are compared)"},
             "levels": {"type": "integer", "minimum": 4, "maximum": 64, "default": 24,
                        "description": "Number of height levels in the table"},
             "tolerance_mm": {"type": "number", "exclusiveMinimum": 0, "default": 1.5,
                              "description": "A level passes when the model matches within this distance, at a height within the same distance of the level"},
             "align": {"type": "string", "enum": ["bbox", "origin"], "default": "bbox",
                       "description": "bbox: centre both silhouettes; origin: the reference centre is the model X=0 (Z=0 for side views)"},
             "background": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "integer", "minimum": 0, "maximum": 255}},
             "threshold": {"type": "integer", "minimum": 9, "maximum": 246, "default": 32},
             "directory": {"type": "string", "description": "Absolute directory for the overlay image; defaults beside the part"},
         }, ["image_path"])))

_observation_properties = next(t for t in SHAPE_TOOLS if t.name=='observe_model').inputSchema['properties']
_body_ref = {'oneOf': [schema({'body': {'type': 'string', 'minLength': 1}}, ['body']),
                       schema({'component': {'type': 'string', 'minLength': 1}}, ['component'])]}
SHAPE_TOOLS.extend([
    Tool(name='inspect_model', description='Measure native geometry without images or helper sketches. body_pairs checks a specific fit; read each passed=true/false/null. precision=delivery adds exact bounds and kernel/feature health. printability=true adds print checks on the built bodies: does the assembled model stand on its base (centre of mass over the footprint, tip angle) and the thinnest wall of every body.',
         inputSchema=schema({
             'document_path':_observation_properties['document_path'],
             'expected_revision':_observation_properties['expected_revision'],
             'components':_observation_properties['components'], 'sections':_observation_properties['sections'],
             'body_pairs': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                 'description': 'Targeted global body-pair distances and native intersection volumes; no selections or sketches. Read relation and tri-state clearance_check.passed; UNKNOWN never certifies a gap.',
                 'items': schema({'id': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                                  'a': _body_ref, 'b': _body_ref,
                                  'min_clearance_mm': {'type': 'number', 'minimum': 0},
                                  'max_clearance_mm': {'type': 'number', 'minimum': 0}}, ['id', 'a', 'b'])},
             'precision':{'type':'string','enum':['summary','delivery'],'default':'summary'},
             'printability': {'description': 'true, or {min_wall_mm (default 1.2), up (default Y)}: support and wall thickness of the built bodies',
                              'oneOf': [{'type': 'boolean'},
                                        schema({'min_wall_mm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 50},
                                                'up': {'type': 'string', 'enum': ['X', 'Y', 'Z']}}, [])]},
             'verbose':VERBOSE
         },['document_path','expected_revision'])),
    Tool(name='export_model', description='Publish checked SLDPRT, STEP, per-body STL (stl/<BodyName>.stl, stable across rebuilds) and manifest to a new directory from a saved clean part; replace=true republishes over its own earlier delivery. Preserves source; strict raw STL validation by default. Export success does not certify likeness, parameter cycling or print-process readiness.',
         inputSchema=schema({
             'document_path':_observation_properties['document_path'],
             'expected_revision':_observation_properties['expected_revision'],
             'directory':{'type':'string','description':'New absolute delivery directory, or with replace=true an earlier export_model delivery'},
             'verbose':VERBOSE,
             'replace':{'type':'boolean','default':False,
                 'description':'Atomically replace directory when it holds only an earlier export_model delivery (its manifest and listed files). Any other existing directory is still refused with OUTPUT_EXISTS.'},
             'expected_bodies':{'type':'array','minItems':1,'maxItems':64,'uniqueItems':True,'items':{'type':'string'}},
             'deviation_mm':{'type':'number','minimum':.001,'maximum':.1,'default':.02},
             'mesh_cleanup':{'type':'string','enum':['none','bounded'],'default':'none',
                 'description':'Optional bounded STL seam welding and local winding correction after strict validation fails. Never fills holes. Retains raw STL and audit; native geometry stays unchanged.'},
             'diagnostics_directory':{'type':'string',
                 'description':'Optional NEW absolute directory for the raw failed STL and bounded facet diagnostics. Separate from delivery; created only on mesh rejection.'}
         },['document_path','expected_revision','directory'])),
])
SHAPE_TOOL_NAMES = {tool.name for tool in SHAPE_TOOLS}

for _tool in SHAPE_TOOLS:
    if _tool.name == 'execute_model_code':
        _tool.description += (' Analyze includes component_count, max_components and remaining_components; '
                              'read model_api_lookup(topic="limits") for the active budgets.')
    elif _tool.name == 'model_api_lookup':
        _tool.description += ' The limits topic gives numerical component, execution and draft budgets.'
