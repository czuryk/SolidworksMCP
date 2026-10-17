"""Targeted native inspection without screenshots or construction geometry."""
import math

from ..shape_program import ProgramError
from .com_utils import com_get, typed, command_scope
from .shape_observation import (require_target, geometry_state, ensure_unchanged,
                                collect_facts, body_signature, document_revision)
from .shape_parametric import global_values
from .shape_sections import validate_sections, measure_sections
from .body_relations import validate_pairs, measure_pairs


def exact_bbox(body):
    native = typed(body, 'IBody2')
    bounds = {'min': [], 'max': []}
    for label, sign in (('min', -1.), ('max', 1.)):
        for axis in range(3):
            direction = [0., 0., 0.]; direction[axis] = sign
            answer = native.GetExtremePoint(*direction)
            if not answer or len(answer) != 4 or not answer[0]:
                raise ProgramError('NATIVE_MEASUREMENT_FAILED', 'GetExtremePoint failed')
            coords = [float(v)*1000 for v in answer[1:]]
            if not all(math.isfinite(v) for v in coords):
                raise ProgramError('NATIVE_MEASUREMENT_FAILED', 'Nonfinite extreme point')
            bounds[label].append(coords[axis])
    return bounds


def kernel_health(body):
    faults = com_get(typed(body, 'IBody2'), 'Check3')
    if faults is None:
        raise ProgramError('NATIVE_CHECK_UNAVAILABLE', 'Check3 returned no fault report')
    count = int(com_get(typed(faults, 'IFaultEntity'), 'Count'))
    if count < 0:
        raise ProgramError('NATIVE_CHECK_UNAVAILABLE', 'Invalid native fault count')
    return {'method': 'IBody2.Check3', 'fault_count': count, 'passed': count == 0}


def fault_components(body, owners):
    """Shape components whose features own the faulty entities of a body; best effort."""
    names = []
    try:
        faults = typed(com_get(typed(body, 'IBody2'), 'Check3'), 'IFaultEntity')
        for index in range(int(com_get(faults, 'Count'))):
            entity = faults.Entity(index)
            faces = []
            try:
                face = typed(entity, 'IFace2')
                if com_get(face, 'GetFeature') is not None:
                    faces = [face]
            except Exception:
                pass
            if not faces:
                try:
                    faces = [typed(f, 'IFace2') for f in typed(entity, 'IEdge').GetTwoAdjacentFaces2() or [] if f is not None]
                except Exception:
                    faces = []
            for face in faces:
                feature = com_get(face, 'GetFeature')
                name = str(com_get(feature, 'Name')) if feature is not None else None
                owner = owners.get(name, name)
                if owner and owner not in names:
                    names.append(owner)
    except Exception:
        pass
    return names


def kernel_faults(automation, doc, ownership=None):
    """Bodies the kernel rejects, with the components that own the faulty faces.

    A body with kernel faults cannot be exported and gives unknown fit checks.
    """
    from .shape_parametric import feature_owners
    owners = feature_owners(ownership)
    found = []
    for body in automation._get_solid_bodies(doc):
        try:
            health = kernel_health(body)
        except ProgramError:
            continue
        if not health['passed']:
            found.append({'body': str(com_get(body, 'Name')), 'fault_count': health['fault_count'],
                          'components': fault_components(body, owners)})
    return found


def printability_options(value):
    """Normalised {min_wall_mm, up} of the printability request, or None when it is off."""
    if value in (None, False):
        return None
    options = {} if value is True else value
    if not isinstance(options, dict) or set(options) - {'min_wall_mm', 'up'}:
        raise ProgramError('INVALID_ARGUMENT', 'printability is true or {min_wall_mm, up}')
    from ..mesh_checks import DEFAULT_MIN_WALL_MM
    return {'min_wall_mm': options.get('min_wall_mm', DEFAULT_MIN_WALL_MM), 'up': options.get('up', 'Y')}


def inspect_model(automation, document_path, expected_revision, components=None,
                  sections=None, precision='summary', body_pairs=None, printability=None):
    if precision not in {'summary', 'delivery'}:
        raise ProgramError('INVALID_ARGUMENT', 'precision must be summary or delivery')
    printability = printability_options(printability)
    sections = validate_sections(sections)
    body_pairs = validate_pairs(body_pairs)
    doc, record, revision = require_target(automation, document_path, expected_revision)
    state = geometry_state(doc)
    facts, bbox, signature = collect_facts(automation, doc, record, components, revision)
    section_facts = measure_sections(automation, doc, record, sections)
    pair_facts = []
    if body_pairs:
        dirty_before = bool(com_get(doc, 'GetSaveFlag'))
        with command_scope(automation.app):
            pair_facts = measure_pairs(automation, doc, record, body_pairs)
        if bool(com_get(doc, 'GetSaveFlag')) != dirty_before:
            raise ProgramError('INSPECTION_STATE_CHANGED', 'Body-pair inspection changed the document dirty state')
    if precision == 'delivery':
        by_name = {str(com_get(b, 'Name')): b for b in automation._get_solid_bodies(doc)}
        for fact in facts:
            body = by_name[fact['name']]
            fact['bbox'] = exact_bbox(body)
            fact['bbox_method'] = 'IBody2.GetExtremePoint'
            fact['dimensions_mm'] = [b-a for a,b in zip(fact['bbox']['min'], fact['bbox']['max'])]
            fact['kernel_health'] = kernel_health(body)
        # Tree folders/end tags are not necessarily addressable by FeatureByName.
        # Inspect the actual objects returned by native traversal.
        issues=[]
        for feature in automation._walk_features(doc):
            code,warning=typed(feature,'IFeature').GetErrorCode2()
            if code:issues.append({'feature':str(com_get(feature,'Name')),'code':code,'warning':bool(warning)})
        if issues:raise ProgramError('NATIVE_REBUILD_FAILED','Native feature errors or warnings',features=issues)
        bbox = {'min': [min(f['bbox']['min'][i] for f in facts) for i in range(3)],
                'max': [max(f['bbox']['max'][i] for f in facts) for i in range(3)]}
    print_facts = None
    if printability:
        from ..mesh_checks import printability as check_printability
        from .native_mesh import document_meshes
        with command_scope(automation.app):
            meshes = document_meshes(automation, doc, {fact['name'] for fact in facts})
        print_facts = check_printability(meshes, **printability)
    ensure_unchanged(automation, doc, state)
    if body_signature(automation._get_solid_bodies(doc)) != signature or document_revision(doc, record) != revision:
        raise ProgramError('INSPECTION_STATE_CHANGED', 'Native state changed during inspection')
    return {'document_path': document_path, 'revision': revision, 'configuration': state['configuration'],
            'unit': 'mm', 'bodies': facts, 'bbox': bbox,
            'bbox_method': 'IBody2.GetExtremePoint' if precision == 'delivery' else 'approximate_GetBodyBox',
            'parameters': global_values(doc, record) if record else {}, 'sections': section_facts,
            'body_pairs': pair_facts, **({'printability': print_facts} if print_facts else {}),
            'acceptance': 'MEASURED_NOT_DELIVERY_VALIDATED', 'cad_entities_created': 0}
