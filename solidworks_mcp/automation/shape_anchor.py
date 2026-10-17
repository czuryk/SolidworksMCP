"""Native ray/face reference points and dependent local profile planes."""
import math

import pythoncom
from win32com.client import VARIANT

from ..shape_program import ProgramError
from .com_utils import com_get, typed, array_r8, create_select_data, resolve_solidworks_constant
from .shape_parametric import persist, restore, validate_feature_health, short_dimension


def first_hit(points, count, origin, direction, enter_flag, exit_flag):
    """Reject grazing, seam and malformed hits instead of guessing a branch."""
    if count < 0 or len(points) != 9*count:
        raise ProgramError('ANCHOR_RAY_FAILED', 'Unexpected native ray layout')
    hits = []
    for i in range(count):
        row = points[9*i:9*i+9]
        if int(row[0]) != 0 or int(row[1]) != 0:
            raise ProgramError('ANCHOR_RAY_FAILED', 'Unexpected body or ray index')
        xyz = [v*1000 for v in row[3:6]]
        distance = sum((v-o)*d for v,o,d in zip(xyz,origin,direction))
        if distance > 1e-4:
            hits.append((distance, int(row[2]), xyz, row[6:9]))
    hits.sort(key=lambda h:h[0])
    if not hits:
        raise ProgramError('ANCHOR_NO_HIT', 'No positive hit on the requested host body')
    hit = hits[0]
    flags = hit[1]
    if (len([h for h in hits if abs(h[0]-hit[0]) < 1e-4]) != 1 or
            bool(flags & enter_flag) == bool(flags & exit_flag) or
            abs(sum(v*d for v,d in zip(hit[3],direction))) < 1e-6):
        raise ProgramError('ANCHOR_AMBIGUOUS', 'Ray touches a seam, edge or tangent; move its origin away from the boundary')
    return hit[2]


def ray_hit(doc, body, origin, direction):
    options = resolve_solidworks_constant('swRayPtsOptsENTRY_EXIT') | resolve_solidworks_constant('swRayPtsOptsNORMALS')
    count = typed(doc.Extension, 'IModelDocExtension').RayIntersections(
        VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [body]),
        array_r8([v/1000 for v in origin]), array_r8(direction), options, 1e-8, 1e-7, True)
    return first_hit(list(com_get(doc,'GetRayIntersectionsPoints') or []), count, origin, direction,
                     resolve_solidworks_constant('swRayPtsResultsENTER'),
                     resolve_solidworks_constant('swRayPtsResultsEXIT'))


def point_xyz(feature):
    return [1000*v for v in com_get(com_get(com_get(feature,'GetSpecificFeature2'),'GetRefPoint'),'ArrayData')]


def face_at(body, xyz):
    matches=[]
    for face in com_get(body, 'GetFaces') or []:
        q = typed(face,'IFace2').GetClosestPointOn(*[v/1000 for v in xyz])
        if q is not None and math.dist(xyz,[v*1000 for v in q[:3]]) < 1e-4:
            matches.append(face)
    if len(matches) != 1:
        raise ProgramError('ANCHOR_AMBIGUOUS', 'The ray hit does not identify one trimmed host face')
    return matches[0]


def plane_basis(doc, feature):
    """Get world-space local axes directly from the native sketch transform."""
    sketch = com_get(feature,'GetSpecificFeature2')
    transform = com_get(sketch,'ModelToSketchTransform')
    data = list(com_get(transform,'ArrayData'))
    # Rows of the inverse rotation are columns of the world-to-sketch rotation.
    return [[data[i],data[i+3],data[i+6]] for i in range(3)]


def circle_profile(c,spec,frame,point):
    doc,name=c.doc,spec['id']
    sm=c.begin_profile(frame)
    sm.AddToDB=True
    try:
        circle=sm.CreateCircleByRadius(0.,0.,0.,spec['anchor']['circle']['radius']/1000)
    finally:
        sm.AddToDB=False
    c.require(circle is not None,'Cannot create native anchor circle')
    sketch=com_get(sm,'ActiveSketch')
    manager=typed(com_get(sketch,'RelationManager'),'ISketchRelationManager')
    center=com_get(typed(circle,'ISketchArc'),'GetCenterPoint2')
    before=len(com_get(manager,'GetRelations',0) or [])
    relation=manager.AddRelation(VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_DISPATCH,
        [center,com_get(point,'GetSpecificFeature2')]),resolve_solidworks_constant('swConstraintType_COINCIDENT'))
    c.require(relation is not None and len(com_get(manager,'GetRelations',0) or [])==before+1,
              'Cannot constrain the native circle to its anchor')
    doc.ClearSelection2(True)
    c.require(bool(circle.Select4(False,create_select_data(doc,0))),'Cannot dimension anchor circle')
    with c.a.dimension_input_guard():
        display=typed(doc,'IModelDoc2').AddDimension2(.005,.005,0.)
    c.require(display is not None,'Cannot create native circle diameter')
    dimension=com_get(display,'GetDimension2',0)
    feature=c.finish_sketch(name+'_Profile')
    target=short_dimension(dimension)
    c.add_equation(target,spec['anchor']['circle']['diameter_expression'])
    from .shape_ellipsoid import relation_signature
    return feature,{'dimension':target,'relations':relation_signature(doc,sketch)}


def build_anchor(c, spec):
    doc, name = c.doc, spec['id']
    c.stage = 'native_face_anchor'
    binding = spec['anchor']
    host = c.components[spec['seeds'][0]]
    c.require(len(host['bodies']) == 1, 'Anchor requires one live host body', 'ANCHOR_BODY_SCOPE')
    body = c.a._find_body(doc,host['bodies'][0])
    xyz = ray_hit(doc,body,binding['origin'],binding['direction'])
    face = face_at(body,xyz)
    ray = c.create_sketch(spec['sketches'][0])
    points = list(com_get(com_get(ray,'GetSpecificFeature2'),'GetSketchPoints2') or [])
    c.require(len(points) == 2, 'Anchor axis requires two native endpoints')
    doc.ClearSelection2(True)
    for i,p in enumerate(points):
        c.require(bool(p.Select4(i>0,create_select_data(doc,0))), 'Cannot select anchor axis endpoint')
    c.require(bool(doc.InsertAxis2(True)), 'Cannot create native anchor axis')
    axis = c.last_feature(); axis.Name = name+'_Axis'; c.support.append(str(axis.Name))
    doc.ClearSelection2(True)
    c.require(bool(axis.Select2(False,0)), 'Cannot select native anchor axis')
    c.require(bool(typed(face,'IEntity').Select4(True,create_select_data(doc,0))), 'Cannot select actual host face')
    fm = typed(doc.FeatureManager,'IFeatureManager')
    result = fm.InsertReferencePoint(resolve_solidworks_constant('swRefPointIntersection'),0,0.,1)
    point = result[0] if isinstance(result,(tuple,list)) and len(result)==1 else None
    c.require(point is not None, 'Native ray/face reference point was rejected', 'ANCHOR_NATIVE_FAILED')
    point.Name = name+'_Anchor'; c.support.append(str(point.Name))
    c.require(math.dist(point_xyz(point),xyz) < 1e-4,
              'Native reference point selected a different intersection branch', 'ANCHOR_BRANCH_MISMATCH')
    doc.ClearSelection2(True)
    c.require(bool(axis.Select2(False,0)) and bool(point.Select2(True,1)), 'Cannot select native anchor frame references')
    plane = fm.InsertRefPlane(resolve_solidworks_constant('swRefPlaneReferenceConstraint_Perpendicular'),0.,
                             resolve_solidworks_constant('swRefPlaneReferenceConstraint_Coincident'),0.,0,0.)
    c.require(plane is not None, 'Cannot create native anchor frame')
    frame = c.last_feature(); frame.Name = name+'_Frame'; c.support.append(str(frame.Name))
    circle_binding=None
    if 'circle' in binding:
        profile,circle_binding=circle_profile(c,spec,frame,point)
    else:
        profile = c.create_sketch(spec['sketches'][1])
    basis = plane_basis(doc,profile)
    dot = sum(v*d for v,d in zip(basis[2],binding['direction']))
    c.require(abs(abs(dot)-1) < 1e-6, 'Native profile plane is not perpendicular to the declared ray')
    # The native frame origin must coincide with the intersection, not a copied XYZ.
    sketch = com_get(profile,'GetSpecificFeature2')
    transform = com_get(sketch,'ModelToSketchTransform')
    math_point = typed(com_get(c.a.app,'GetMathUtility'),'IMathUtility').CreatePoint(array_r8([v/1000 for v in xyz]))
    local = list(com_get(math_point.MultiplyTransform(transform),'ArrayData'))
    c.require(math.hypot(*local) < 1e-7, 'Native anchor frame origin is not the intersection')
    before = c.body_names()
    result = c.a.advanced_extrude(sketch_name=str(profile.Name),end_condition='blind',
        depth=spec['extrusion']['value'],direction_flip=dot*binding['depth_sign']<0,
        merge=False,feature_name=name,unit='mm')
    c.require(result.get('success'),result.get('message','Anchored extrusion failed'))
    c.add_equation('D1@'+name,spec['extrusion']['expression'])
    c.finish_solid(name,c.a._find_feature(doc,name),before)
    c.components[name].update(dependencies=spec['seeds'], attachment='NATIVE_FACE_INTERSECTION',
        attachment_frame={'x_axis':basis[0],'y_axis':basis[1],'normal':basis[2]},
        profile_representation='native_circle' if circle_binding else 'equation_curves',
        native_anchor={'host':spec['seeds'][0], 'ray':name+'_Ray', 'direction':binding['direction'],
                       'point':name+'_Anchor', 'frame':name+'_Frame', 'profile':name+'_Profile',
                       'refs':{str(f.Name):persist(doc,f) for f in [ray,axis,point,frame]},
                       'point_parents':sorted(persist(doc,p) for p in com_get(point,'GetParents') or []),
                       'plane_basis':basis, 'depth_sign':binding['depth_sign'],
                       **({'circle':circle_binding} if circle_binding else {})})


def validate_anchors(automation,doc,components):
    """Check native branch and plane after native edits, including manual rebuild."""
    for name, component in components.items():
        binding = component.get('native_anchor')
        if not binding:
            continue
        for reference in binding['refs'].values():
            restore(doc,reference)
        validate_feature_health(automation,doc,list(binding['refs'])+[binding['profile']])
        validate_anchor_links(automation,doc,name,binding)
        ray = automation._find_feature(doc,binding['ray'])
        points = list(com_get(com_get(ray,'GetSpecificFeature2'),'GetSketchPoints2') or [])
        if len(points) != 2:
            raise ProgramError('NATIVE_MAPPING_STALE','Native anchor axis endpoints changed: '+name)
        xyzs = [[1000*float(com_get(p,k)) for k in 'XYZ'] for p in points]
        direction = binding['direction']
        xyzs.sort(key=lambda p:sum(v*d for v,d in zip(p,direction)))
        if math.dist([b-a for a,b in zip(*xyzs)],direction) > 1e-4:
            raise ProgramError('NATIVE_MAPPING_STALE','Native anchor ray changed: '+name)
        point = automation._find_feature(doc,binding['point'])
        xyz = point_xyz(point)
        host = components[binding['host']]
        if not host.get('consumed_by'):
            if len(host['bodies']) != 1:
                raise ProgramError('ANCHOR_BODY_SCOPE','Anchor host no longer owns one body')
            body = automation._find_body(doc,host['bodies'][0])
            hit = ray_hit(doc,body,xyzs[0],direction)
            if math.dist(xyz,hit) > 1e-4:
                raise ProgramError('ANCHOR_BRANCH_MISMATCH','Native anchor no longer matches the first ray hit: '+name)
        profile = automation._find_feature(doc,binding['profile'])
        if 'circle' in binding:
            from .shape_ellipsoid import relation_signature
            actual=relation_signature(doc,com_get(profile,'GetSpecificFeature2'))
            expected=[(int(k),sorted(v)) for k,v in binding['circle']['relations']]
            if actual!=expected:
                raise ProgramError('NATIVE_MAPPING_STALE','Native anchor circle relations changed: '+name)
        basis = plane_basis(doc,profile)
        if any(math.dist(a,b)>1e-6 for a,b in zip(basis,binding['plane_basis'])):
            raise ProgramError('ANCHOR_FRAME_CHANGED','Native anchor frame orientation changed: '+name)
        transform = com_get(com_get(profile,'GetSpecificFeature2'),'ModelToSketchTransform')
        point = typed(com_get(automation.app,'GetMathUtility'),'IMathUtility').CreatePoint(array_r8([v/1000 for v in xyz]))
        local = list(com_get(point.MultiplyTransform(transform),'ArrayData'))
        if math.hypot(*local) > 1e-7:
            raise ProgramError('ANCHOR_FRAME_CHANGED','Native profile origin detached from its anchor: '+name)


def validate_anchor_links(automation,doc,name,binding):
    """Current coincidence alone does not prove a surviving native dependency."""
    axis=automation._find_feature(doc,name+'_Axis')
    parents=[persist(doc,p) for p in com_get(axis,'GetParents') or []]
    axis_type=com_get(typed(com_get(axis,'GetDefinition'),'IRefAxisFeatureData'),'Type')
    if axis_type!=resolve_solidworks_constant('swAxisTypeTwoPoints') or parents != [binding['refs'][binding['ray']]]:
        raise ProgramError('NATIVE_MAPPING_STALE','Anchor axis is no longer driven by its ray: '+name)
    point=automation._find_feature(doc,binding['point'])
    if binding.get('point_parents') and sorted(persist(doc,p) for p in com_get(point,'GetParents') or [])!=binding['point_parents']:
        raise ProgramError('NATIVE_MAPPING_STALE','Native anchor host dependencies changed: '+name)
    data=typed(com_get(point,'GetDefinition'),'IRefPointFeatureData')
    selected=list(com_get(data,'Selections') or [])
    if (com_get(data,'Type')!=resolve_solidworks_constant('swRefPointIntersection') or
            len(selected)!=2 or persist(doc,selected[0])!=binding['refs'][name+'_Axis']):
        raise ProgramError('NATIVE_MAPPING_STALE','Native face-intersection definition changed: '+name)
    frame=automation._find_feature(doc,binding['frame'])
    data=typed(com_get(frame,'GetDefinition'),'IRefPlaneFeatureData')
    constraints=[data.Constraint(i) for i in range(3)]
    expected=[resolve_solidworks_constant('swRefPlaneReferenceConstraint_Perpendicular'),
              resolve_solidworks_constant('swRefPlaneReferenceConstraint_Coincident'),0]
    objects=[data.Reference(i) for i in range(3)]
    references=[persist(doc,obj) if obj is not None else None for obj in objects]
    if constraints!=expected or references!=[binding['refs'][name+'_Axis'],binding['refs'][binding['point']],None]:
        raise ProgramError('NATIVE_MAPPING_STALE','Native anchor plane dependencies changed: '+name)
