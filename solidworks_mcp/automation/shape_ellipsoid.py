"""Native independently driven ellipsoid approximation with verified relations."""
import math

import pythoncom
from win32com.client import VARIANT

from ..shape_program import ProgramError
from .com_utils import com_get, typed, create_select_data, resolve_solidworks_constant
from .shape_parametric import persist


def relation_signature(doc, sketch):
    """Relations cannot be persisted directly; their entity identities can."""
    manager = typed(com_get(sketch, 'RelationManager'), 'ISketchRelationManager')
    return sorted((int(com_get(r, 'GetRelationType')),
                   sorted(persist(doc, e) for e in com_get(r, 'GetEntities')))
                  for r in (com_get(manager, 'GetRelations', 0) or []))


def validate_guided_profiles(automation, doc, components):
    for name, component in components.items():
        binding = component.get('native_profile')
        if not binding:
            continue
        feature = automation._find_feature(doc, binding['feature'])
        if feature is None:
            raise ProgramError('NATIVE_MAPPING_STALE', 'Missing ellipsoid profile: '+name)
        sketch = com_get(feature, 'GetSpecificFeature2')
        actual = relation_signature(doc, sketch)
        expected = [(int(kind), sorted(entities)) for kind, entities in binding['relations']]
        if actual != expected:
            raise ProgramError('NATIVE_MAPPING_STALE', 'Ellipsoid profile relations changed: '+name)


def nearest_point(compiler, points, target):
    def distance(point):
        return math.dist([float(com_get(point, axis)) for axis in 'XYZ'], target)
    point = min(points, key=distance)
    compiler.require(distance(point) < 1e-8, 'Native profile endpoint does not match its guide')
    return point


def build_guided_ellipsoid(c, spec, sketches):
    """Four meridians control a deformable ellipse up to the pole; mirror in Y."""
    doc, name = c.doc, spec['id']
    c.stage = 'native_guided_ellipsoid'
    rx, _, rz = spec['guided_ellipsoid']['radii']
    sm = c.begin_profile(c.standard_plane('Top'))
    toggle = resolve_solidworks_constant('swSketchAutomaticRelations')
    old_relations = c.a.app.GetUserPreferenceToggle(toggle)
    old_add = sm.AddToDB
    try:
        c.a.app.SetUserPreferenceToggle(toggle, False)
        sm.AddToDB = True
        ellipse = sm.CreateEllipse(*c.local((0,0,0)), *c.local((rx,0,0)), *c.local((0,0,rz)))
    finally:
        sm.AddToDB = old_add
        c.a.app.SetUserPreferenceToggle(toggle, old_relations)
    c.require(ellipse is not None, 'Cannot create native ellipsoid equator')
    sketch = com_get(sm, 'ActiveSketch')
    manager = typed(com_get(sketch, 'RelationManager'), 'ISketchRelationManager')
    for guide, world in zip(sketches[:3], [(0,0,0), (rx,0,0), (0,0,rz)]):
        p = nearest_point(c, com_get(sketch, 'GetSketchPoints2'), c.local(world))
        q = nearest_point(c, com_get(com_get(guide, 'GetSpecificFeature2'), 'GetSketchPoints2'),
                          [v/1000 for v in world])
        before = len(com_get(manager, 'GetRelations', 0) or [])
        relation = manager.AddRelation(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [p,q]),
                                      resolve_solidworks_constant('swConstraintType_COINCIDENT'))
        c.require(relation is not None and len(com_get(manager, 'GetRelations', 0) or []) == before+1,
                  'SolidWorks did not create the native ellipse dependency')
    binding = {'feature': name+'_Equator', 'relations': relation_signature(doc, sketch)}
    profile = c.finish_sketch(name+'_Equator')
    doc.ClearSelection2(True)
    c.require(bool(profile.Select2(False, 1)), 'Cannot select equator')
    for i, feature in enumerate(sketches):
        contours = list(com_get(com_get(feature, 'GetSpecificFeature2'), 'GetSketchContours') or [])
        c.require(len(contours) == 1, 'Sweep requires one contour per guide')
        c.require(bool(contours[0].Select2(True, create_select_data(doc, 4 if i == 0 else 2))),
                  'Cannot select native ellipsoid guide')
    fm = typed(doc.FeatureManager, 'IFeatureManager')
    definition = fm.CreateDefinition(resolve_solidworks_constant('swFmSweep'))
    c.require(definition is not None, 'Cannot create Sweep definition')
    definition.TangentPropagation = False
    definition.AlignWithEndFaces = False
    definition.TwistControlType = resolve_solidworks_constant('swTwistControlKeepNormalConstant')
    definition.MaintainTangency = False
    definition.AdvancedSmoothing = False
    definition.StartTangencyType = 0
    definition.EndTangencyType = 0
    definition.ThinFeature = False
    definition.PathAlignmentType = 0
    definition.Merge = False
    definition.FeatureScope = True
    definition.AutoSelect = True
    definition.SetTwistAngle(0.0)
    definition.MergeSmoothFaces = False
    definition.CircularProfile = False
    definition.Direction = 0
    half, other = name+'_Half', name+'_OtherHalf'
    before = c.body_names()
    feature = fm.CreateFeature(definition)
    c.finish_solid(half, feature, before)
    c.feature_refs[half] = persist(doc, feature)
    c.mirror(other, {'seeds': [half], 'plane': 'Top'})
    c.feature_refs[other] = persist(doc, c.a._find_feature(doc, other))
    volume = name+'_Volume' if spec['placement'] else name
    c.combine({'id': volume, 'seeds': [half,other], 'operation': 'union', 'expected_bodies': 1})
    c.feature_refs[volume] = persist(doc, c.a._find_feature(doc, volume))
    # Internal halves belong to one public component, not to the user's graph.
    c.components.pop(half)
    c.components.pop(other)
    c.components[volume].pop('dependencies', None)
    if spec['placement']:
        body = c.a._find_body(doc, volume)
        doc.ClearSelection2(True)
        c.require(bool(typed(body, 'IBody2').Select2(False, create_select_data(doc,1))),
                  'Cannot select ellipsoid placement body')
        xyz = [spec['placement'].get(axis,{}).get('value',0)/1000 for axis in 'XYZ']
        feature = fm.InsertMoveCopyBody2(*xyz,0,0,0,0,0,0,0,False,1)
        c.require(feature is not None, 'Cannot place guided ellipsoid')
        feature.Name = name
        for axis, item in spec['placement'].items():
            c.add_equation({'X':'D1','Y':'D2','Z':'D3'}[axis]+'@'+name,item['expression'])
        c.components.pop(volume)
        c.finish_transform(name, feature, before)
    c.components[name].update(native_profile=binding, geometry_fidelity='GUIDED_APPROXIMATION',
                              continuous_error_bound_mm=None, analytic_surface_anchor=False)
    doc.ClearSelection2(True)
