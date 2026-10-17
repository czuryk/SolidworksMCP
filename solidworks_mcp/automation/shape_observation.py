"""Document-bound native observations with consistent geometry and framing."""

import hashlib
import json
import math
import os
import re
import uuid
import time
import copy
from pathlib import Path

from PIL import Image

from ..shape_program import ProgramError
from .com_utils import com_get, typed, array_r8, resolve_solidworks_constant, command_scope
from .shape_parametric import read_record, observed_revision


def canonical_path(value):
    return os.path.normcase(os.path.abspath(value))


def true_case(path):
    """The spelling of a path as the file system stores it; a path that differs in more than case is kept.

    SolidWorks reports a document under the spelling it was opened with. The
    same saved part must not get another revision because a caller wrote
    d:\\parts\\a.sldprt for D:\\Parts\\A.SLDPRT.
    """
    if not path:
        return path
    try:
        real = os.path.realpath(path)
    except OSError:
        return path
    return real if os.path.normcase(real) == os.path.normcase(os.path.abspath(path)) else path


def activate_target(automation, document_path):
    """Make the requested saved document the active one; others are left untouched.

    An already open document is activated, a closed one is opened. Returns
    whether the active document had to change.
    """
    active, _ = automation.get_active_doc()
    if active is not None and canonical_path(automation._get_doc_path(active) or '') == canonical_path(document_path):
        if getattr(automation, '_target_activated', None) is None:
            automation._target_activated = False
        return False
    if not Path(document_path).is_file():
        raise ProgramError('DOCUMENT_NOT_FOUND', 'The requested document does not exist: ' + str(document_path))
    opened = automation.open_document(str(document_path))
    if not opened.get('success'):
        raise ProgramError('DOCUMENT_ACTIVATION_FAILED',
                           opened.get('message', 'Cannot activate the requested document'),
                           document_path=str(document_path))
    automation._target_activated = True
    return True


def geometry_state(doc):
    """The SW stamp covers rebuilds/configuration, not color or camera changes."""
    return {'path': true_case(str(com_get(doc, 'GetPathName'))),
            'configuration': str(com_get(doc.ConfigurationManager.ActiveConfiguration, 'Name')),
            'update_stamp': int(com_get(doc, 'GetUpdateStamp'))}


def document_revision(doc, record=None):
    if record:
        return observed_revision(doc, record)
    state = geometry_state(doc)
    path = Path(state['path'])
    state['saved_file_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def require_target(automation, document_path, expected_revision):
    if not document_path or not Path(document_path).is_absolute() or not expected_revision:
        raise ProgramError('OBSERVATION_TARGET_REQUIRED', 'Use absolute document_path and fresh expected_revision from get_model_context')
    activate_target(automation, document_path)
    doc, error = automation.get_active_doc()
    if error:
        raise ProgramError('NO_ACTIVE_DOCUMENT', error.get('message', 'No active document'))
    if canonical_path(automation._get_doc_path(doc)) != canonical_path(document_path):
        raise ProgramError('DOCUMENT_MISMATCH', 'The active document does not match the requested path')
    if int(com_get(doc, 'GetType')) != 1:
        raise ProgramError('PART_REQUIRED', 'Shape observation currently supports native part documents')
    record = read_record(doc)
    revision = document_revision(doc, record)
    if revision != expected_revision:
        raise ProgramError('STALE_REVISION', 'The model changed; retrieve a fresh get_model_context')
    if record:
        from .shape_placement import require_placement_ready
        require_placement_ready(doc,record['native'].get('placement_features',{}))
        from .shape_anchor import validate_anchors
        if any(c.get('native_anchor') for c in record['native']['components'].values()):
            with command_scope(automation.app):
                validate_anchors(automation,doc,record['native']['components'])
    return doc, record, revision


def ensure_unchanged(automation, doc, state):
    active = automation.app.ActiveDoc
    # Configuration changes increment the native stamp. Avoid rereading the
    # configuration manager and name through COM twice for every frame.
    if (active is None or str(com_get(active, 'GetPathName')) != state['path'] or
            int(com_get(active, 'GetUpdateStamp')) != state['update_stamp'] or
            int(com_get(doc, 'GetUpdateStamp')) != state['update_stamp']):
        raise ProgramError('OBSERVATION_STATE_CHANGED', 'Document, configuration or native geometry changed during observation')


def body_signature(bodies):
    return [(str(com_get(b, 'Name')), bool(com_get(b, 'Visible'))) for b in bodies]


def require_full_model_view(doc):
    manager = typed(com_get(doc, 'ModelViewManager'), 'IModelViewManager')
    if manager is None:
        raise ProgramError('VIEW_STATE_UNAVAILABLE', 'Cannot inspect the native section-view state')
    if manager.GetSectionViewData('') is not None:
        raise ProgramError('ACTIVE_SECTION_VIEW', 'Full-model observation requires the current section view to be turned off')
    if com_get(doc, 'GetActiveSketch2') is not None:
        raise ProgramError('ACTIVE_SKETCH', 'Close the active sketch before observing the solid model')


def collect_facts(automation, doc, record, components=None, revision=None):
    bodies = automation._get_solid_bodies(doc)
    by_name = {str(com_get(b, 'Name')): b for b in bodies}
    mapping = record['native']['components'] if record else {}
    if components is not None:
        if (not isinstance(components, list) or not 1 <= len(components) <= 32 or
                any(not isinstance(x, str) for x in components) or len(set(components)) != len(components)):
            raise ProgramError('INVALID_COMPONENTS', 'Supply one to 32 unique component names')
        if not record:
            raise ProgramError('COMPONENT_MAPPING_REQUIRED', 'This part has no shape program; omit components to inspect its bodies')
        selected = []
        for name in components:
            if name not in mapping:
                raise ProgramError('UNKNOWN_COMPONENT', f'Unknown component {name}')
            item = mapping[name]
            if item.get('consumed_by') or not item.get('bodies'):
                raise ProgramError('COMPONENT_CONSUMED', f'{name} is consumed by {item.get("consumed_by")}; inspect the resulting component',
                                   component=name, consumed_by=item.get('consumed_by'))
            selected.extend(item['bodies'])
        names = list(dict.fromkeys(selected))
    else:
        names = [name for name, b in by_name.items() if bool(com_get(b, 'Visible'))]
    if not names or len(names) > 64:
        raise ProgramError('INVALID_BODY_SCOPE', 'Observation requires one to 64 visible bodies; select components for a smaller scope')
    facts = []
    signature = body_signature(bodies)
    cache_key = (revision, tuple(signature)) if revision else None
    cache = getattr(automation, '_shape_observation_cache', {})
    if not isinstance(cache, dict): cache = {}
    cached = cache.get(cache_key, {}) if cache_key else {}
    surface_names = {resolve_solidworks_constant(enum): label for enum, label in (
        ('PLANE_TYPE', 'plane'), ('CYLINDER_TYPE', 'cylinder'), ('CONE_TYPE', 'cone'),
        ('SPHERE_TYPE', 'sphere'), ('TORUS_TYPE', 'torus'), ('BSURF_TYPE', 'bspline'))}
    for name in names:
        body = by_name.get(name)
        if body is None:
            raise ProgramError('NATIVE_MAPPING_STALE', f'Component body {name} is missing')
        if not bool(com_get(body, 'Visible')):
            raise ProgramError('HIDDEN_TARGET', f'Target body {name} is hidden')
        if name in cached:
            facts.append(copy.deepcopy(cached[name]))
            continue
        info = automation._body_info(body, 'mm')
        props = automation._body_mass_props(body, 'mm')
        if not info.get('bbox') or not props:
            raise ProgramError('NATIVE_MEASUREMENT_FAILED', f'Cannot read native body {name}')
        info.update(props)
        info['dimensions_mm'] = [round(b-a, 4) for a, b in zip(info['bbox']['min'], info['bbox']['max'])]
        owners = [key for key, value in mapping.items() if name in value.get('bodies', []) and not value.get('consumed_by')]
        info['components'] = []
        for owner in owners:
            feature = automation._find_feature(doc, mapping[owner]['feature'])
            if feature is None:
                raise ProgramError('NATIVE_MAPPING_STALE', f'Feature for {owner} is missing')
            info['components'].append({'name': owner, 'id': record['model_id']+':'+owner,
                                      'declared_recipe': mapping[owner]['kind'],
                                      'native_feature_type': str(com_get(feature, 'GetTypeName2')),
                                      **{key:mapping[owner][key] for key in
                                         ('geometry_fidelity','continuous_error_bound_mm','analytic_surface_anchor')
                                         if key in mapping[owner]}})
        # Surface families give direct evidence of flat vs rounded geometry.
        # This is diagnostic evidence, not an automatic likeness verdict.
        surfaces = {}
        faces = com_get(body, 'GetFaces') or []
        if len(faces) <= 128:
            for face in faces:
                surface = com_get(face, 'GetSurface')
                kind = surface_names.get(int(com_get(surface, 'Identity')), 'other')
                surfaces[kind] = surfaces.get(kind, 0)+1
            info['surface_types'] = surfaces
        else:
            info['surface_types'] = {'status': 'SKIPPED_FACE_BUDGET', 'limit': 128}
        facts.append(info)
        cached[name] = copy.deepcopy(info)
    if cache_key:
        cache[cache_key] = cached
        while len(cache)>8: del cache[next(iter(cache))]
        automation._shape_observation_cache = cache
    bbox = {'min': [min(b['bbox']['min'][i] for b in facts) for i in range(3)],
            'max': [max(b['bbox']['max'][i] for b in facts) for i in range(3)]}
    return facts, bbox, signature


STANDARD_VIEWS = ('front', 'back', 'left', 'right', 'top', 'bottom', 'isometric')


def normalize_views(views):
    """Standard view names and custom {name, direction, up} cameras as one list of records."""
    views = ['front', 'right', 'isometric'] if views is None else views
    if not isinstance(views, list) or not 1 <= len(views) <= 6:
        raise ProgramError('INVALID_VIEWS', 'Use one to six views')
    result = []
    for view in views:
        if isinstance(view, str):
            if view not in STANDARD_VIEWS:
                raise ProgramError('INVALID_VIEWS', 'Standard views are ' + ', '.join(STANDARD_VIEWS))
            result.append({'name': view})
            continue
        if not isinstance(view, dict) or set(view) - {'name', 'direction', 'up'} or not {'name', 'direction'} <= set(view):
            raise ProgramError('INVALID_VIEWS', 'A custom view is {name, direction:[x,y,z], up:[x,y,z]}')
        name = view['name']
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', name) or
                name.lower() in STANDARD_VIEWS):
            raise ProgramError('INVALID_VIEWS', 'A custom view name uses 1-40 letters, digits, _ or - '
                                                'and is not a standard view name')
        vectors = {}
        for key, default in (('direction', None), ('up', [0, 1, 0])):
            value = view.get(key, default)
            if (not isinstance(value, (list, tuple)) or len(value) != 3 or
                    any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value)):
                raise ProgramError('INVALID_VIEWS', key + ' must contain three finite numbers')
            length = math.sqrt(sum(v*v for v in value))
            if length < 1e-9:
                raise ProgramError('INVALID_VIEWS', key + ' must be nonzero')
            vectors[key] = [v/length for v in value]
        # The camera looks along direction; up fixes the roll and cannot be parallel to it.
        if abs(sum(a*b for a, b in zip(vectors['direction'], vectors['up']))) > 1 - 1e-6:
            raise ProgramError('INVALID_VIEWS', 'up must not be parallel to direction')
        result.append({'name': name, **vectors})
    names = [view['name'].lower() for view in result]
    if len(set(names)) != len(names):
        raise ProgramError('INVALID_VIEWS', 'View names must be unique')
    return result


def observe(automation, directory, document_path, expected_revision, components=None,
            views=None, width=1000, sections=None, flat=False, overwrite=False):
    started = time.monotonic()
    from .shape_sections import validate_sections
    sections = validate_sections(sections)
    views = normalize_views(views)
    if not isinstance(width, int) or isinstance(width, bool) or not 256 <= width <= 2000:
        raise ProgramError('INVALID_IMAGE_SIZE', 'width must be an integer from 256 to 2000')
    if not isinstance(flat, bool) or not isinstance(overwrite, bool):
        raise ProgramError('INVALID_ARGUMENT', 'flat and overwrite are booleans')
    target = Path(directory)
    if not target.is_absolute():
        raise ProgramError('INVALID_PATH', 'Observation directory must be absolute')
    if flat and not overwrite:
        existing = [view['name'] + '.png' for view in views if (target / (view['name'] + '.png')).exists()]
        if existing:
            raise ProgramError('OUTPUT_EXISTS', 'flat=true never replaces files without overwrite=true',
                               directory=str(target), existing=existing)
    doc, record, revision = require_target(automation, document_path, expected_revision)
    with command_scope(automation.app):
        return _observe_bound(automation, target, doc, record, revision, components,
                              views, width, sections, started, flat)


def _observe_bound(automation, target, doc, record, revision, components,
                   views, width, sections, started, flat=False):
    """Capture real native views within a restoring external command scope."""
    from .shape_sections import measure_sections
    state = geometry_state(doc)
    require_full_model_view(doc)
    facts, bbox, signature = collect_facts(automation, doc, record, components, revision)
    section_facts = measure_sections(automation, doc, record, sections)
    ensure_unchanged(automation, doc, state)
    facts_done = time.monotonic()
    view = doc.ActiveView
    if view is None:
        raise ProgramError('NO_ACTIVE_VIEW', 'Cannot capture a native model view')
    original = {'orientation': view.Orientation3, 'translation': view.Translation3,
                'scale': float(view.Scale2), 'perspective': bool(com_get(view, 'HasPerspective'))}
    if original['perspective']:
        original['distance'] = view.ObjectSizesAway
    hide_enum = resolve_solidworks_constant('swViewDisplayHideAllTypes')
    option = resolve_solidworks_constant('swDetailingNoOptionSpecified')
    hide_original = doc.Extension.GetUserPreferenceToggle(hide_enum, option)
    run_dir = target / uuid.uuid4().hex
    frames = []; saved = []; cleanup_errors = []
    failed = None
    try:
        doc.ClearSelection2(True)
        doc.Extension.SetUserPreferenceToggle(hide_enum, option, True)
        if not doc.Extension.GetUserPreferenceToggle(hide_enum, option):
            raise ProgramError('VIEW_STATE_UNAVAILABLE', 'Cannot hide construction geometry for capture')
        com_get(view, 'RemovePerspective')
        if com_get(view, 'HasPerspective'):
            raise ProgramError('CAMERA_FAILED', 'Cannot disable perspective for metric views')
        # ViewZoomTo2 in SW2026 can retain the Front translation after a named
        # view rotation. Calibrate one Front frame, then center each orientation
        # explicitly. A sphere enclosing the target bbox fits every rotation.
        oriented = automation.set_view_orientation('front', zoom_to_fit=False)
        if not oriented.get('success'):
            raise ProgramError('CAMERA_FAILED', 'Cannot initialize the Front frame')
        center = [(a+b)/2000 for a,b in zip(bbox['min'],bbox['max'])]
        radius = math.dist(bbox['min'],bbox['max'])/2000
        automation._apply_zoom_bbox_m(doc, [x-radius for x in center],
                                     [x+radius for x in center], margin=.12)
        common_scale = float(view.Scale2)
        if not math.isfinite(common_scale) or common_scale <= 0:
            raise ProgramError('CAMERA_FAILED', 'Invalid native camera scale')
        math_util = typed(com_get(automation.app, 'GetMathUtility'), 'IMathUtility')
        rotated = math_util.CreatePoint(array_r8(center)).MultiplyTransform(view.Orientation3).ArrayData
        # Preserve native viewport bias (e.g. the feature-tree panel offset).
        bias = [t+p*common_scale for t,p in zip(view.Translation3.ArrayData,rotated)]
        run_dir.mkdir(parents=True)
        for spec in views:
            name = spec['name']
            ensure_unchanged(automation, doc, state)
            if 'direction' in spec:
                oriented = automation.set_view_orientation(view_direction=spec['direction'],
                                                           up_direction=spec['up'], zoom_to_fit=False)
            else:
                oriented = automation.set_view_orientation(name, zoom_to_fit=False)
            if not oriented.get('success'):
                raise ProgramError('CAMERA_FAILED', f'Cannot orient {name}')
            view.Scale2 = common_scale
            rotated = math_util.CreatePoint(array_r8(center)).MultiplyTransform(view.Orientation3).ArrayData
            view.Translation3 = math_util.CreateVector(array_r8([b-p*common_scale for b,p in zip(bias,rotated)]))
            path = run_dir / (name+'.png'); saved.append(path)
            result = automation.take_screenshot(str(path), zoom_to_fit=False,
                                                width=width, height=width, compress=False)
            ensure_unchanged(automation, doc, state)
            detail = result.get('data', {})
            if not result.get('success') or detail.get('frame_unreadable') or not path.is_file():
                raise ProgramError('FRAME_INVALID', f'Native {name} capture failed or is unreadable', capture=result)
            if abs(float(view.Scale2)-common_scale) > max(1e-9, common_scale*1e-6):
                raise ProgramError('CAMERA_CHANGED', 'Native camera scale changed during capture')
            with Image.open(path) as img:
                size = list(img.size)
            frames.append({'view': name, 'path': str(path), 'document': state['path'],
                           'revision': revision, 'configuration': state['configuration'],
                           'size_px': size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                           'native_scale': common_scale, 'perspective': False,
                           **({'direction': spec['direction'], 'up': spec['up']} if 'direction' in spec else {})})
        if signature != body_signature(automation._get_solid_bodies(doc)):
            raise ProgramError('OBSERVATION_STATE_CHANGED', 'Body names or visibility changed during capture')
        if document_revision(doc, read_record(doc)) != revision:
            raise ProgramError('OBSERVATION_STATE_CHANGED', 'Native revision changed during capture')
    except Exception as exc:
        failed = exc
    finally:
        # Restore the captured document's camera, never a newly active document.
        for label, action in [
                ('orientation', lambda: setattr(view, 'Orientation3', original['orientation'])),
                ('scale', lambda: setattr(view, 'Scale2', original['scale'])),
                ('translation', lambda: setattr(view, 'Translation3', original['translation'])),
                ('helpers', lambda: doc.Extension.SetUserPreferenceToggle(hide_enum, option, hide_original))]:
            try: action()
            except Exception as exc: cleanup_errors.append({'property': label, 'error': str(exc)})
        try:
            if original['perspective']:
                com_get(view, 'AddPerspective'); view.ObjectSizesAway = original['distance']
            doc.GraphicsRedraw2()
            if (bool(com_get(view, 'HasPerspective')) != original['perspective'] or
                    doc.Extension.GetUserPreferenceToggle(hide_enum, option) != hide_original or
                    abs(float(view.Scale2)-original['scale']) > max(1e-9, original['scale']*1e-6)):
                raise RuntimeError('Restored camera/helper state did not round-trip')
        except Exception as exc:
            cleanup_errors.append({'property': 'perspective_redraw', 'error': str(exc)})
    if failed or cleanup_errors:
        for path in saved:
            if path.is_file(): path.unlink()
        if run_dir.is_dir(): run_dir.rmdir()
        if failed: raise failed
        raise ProgramError('CAMERA_RESTORE_FAILED', 'Failed to restore view state', cleanup_errors=cleanup_errors)
    if flat:
        # Predictable paths: <directory>/<view>.png. The capture ran in a private folder first.
        for frame in frames:
            final = target / (frame['view'] + '.png')
            os.replace(frame['path'], final)
            frame['path'] = str(final)
        run_dir.rmdir()
    return {'document': state['path'], 'revision': revision, 'configuration': state['configuration'],
            'update_stamp': state['update_stamp'], 'components': components, 'bodies': facts,
            'directory': str(target if flat else run_dir),
            'bbox_mm': bbox, 'bbox_method': 'native_approximate_GetBodyBox',
            'images': frames, 'sections': section_facts, 'camera_restored': True,
            'timings_sec': {'target_and_facts': round(facts_done-started,3),
                            'camera_capture_restore': round(time.monotonic()-facts_done,3)},
            'framing': 'shared_native_scale_bounding_sphere_with_context',
            'frame_sphere_mm': {'center': [x*1000 for x in center], 'radius': radius*1000, 'margin': .12},
            'acceptance': 'OBSERVED_NOT_SHAPE_ACCEPTED'}
