"""Native equation-backed shape recipes; no callbacks are needed to rebuild."""

import base64
import hashlib
import json
import logging
import math
import os
import zlib
import copy
from pathlib import Path

import pythoncom
from win32com.client import VARIANT

from ..shape_program import ProgramError
from ..shape_equations import (plan_recipes, topology, native_parameter_name,
                               reverse_native_curve, retained_native_curve)
from .shape_native import NativeCompiler
from .com_utils import (com_get, typed, select_by_id2, create_select_data,
                        resolve_solidworks_constant, feature_face_count)
from .runtime import phase

logger=logging.getLogger(__name__)
# SOLIDWORKS_MCP_EAGER_EQUATIONS=1 restores solving every equation as it is added.
EAGER_EQUATIONS=os.environ.get('SOLIDWORKS_MCP_EAGER_EQUATIONS')=='1'
# A definition edit ends with ForceRebuild3. SOLIDWORKS_MCP_REBUILD=verified tries a normal rebuild
# first; it is NOT safe as a default: measured on the 88-component reference, a normal rebuild
# after a one-component definition edit passed every check below and still left a dependent body
# 0.19 % off the forced result. Kept only as an opt-in for further investigation.
REBUILD_MODE='verified' if os.environ.get('SOLIDWORKS_MCP_REBUILD','forced').lower()=='verified' else 'forced'


def short_dimension(dimension):
    return '@'.join(str(com_get(dimension,'FullName')).split('@')[:2])


def persist(doc, obj):
    raw=doc.Extension.GetPersistReference3(obj)
    if not raw: raise ProgramError('PERSIST_REFERENCE_FAILED','Cannot persist native object')
    return base64.b64encode(bytes(raw)).decode('ascii')


def restore(doc, reference):
    raw=VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_UI1,base64.b64decode(reference))
    result=typed(doc.Extension,'IModelDocExtension').GetObjectByPersistReference3(raw)
    obj,status=result
    if obj is None or status!=0:
        raise ProgramError('NATIVE_MAPPING_STALE','A persisted native object no longer resolves')
    return obj


def property_manager(doc):
    return typed(doc.Extension.CustomPropertyManager(''),'ICustomPropertyManager')


def write_record(doc, record):
    raw=json.dumps(record,separators=(',',':'),ensure_ascii=True).encode()
    encoded=base64.b64encode(zlib.compress(raw)).decode()
    chunks=[encoded[i:i+1000] for i in range(0,len(encoded),1000)]
    if len(chunks)>256: raise ProgramError('RECORD_LIMIT','Native record exceeds 256 KB compressed')
    manager=property_manager(doc)
    for i,chunk in enumerate(chunks):
        manager.Add3(f'ShapeProgramData{i:03d}',30,chunk,2)
    header=json.dumps({'version':2,'chunks':len(chunks),'sha256':hashlib.sha256(raw).hexdigest()})
    manager.Add3('ShapeProgramRecord',30,header,2)
    if read_record(doc)!=json.loads(raw):
        raise ProgramError('RECORD_WRITE_FAILED','Native metadata did not round-trip')


def read_record(doc):
    manager=property_manager(doc)
    result=manager.Get6('ShapeProgramRecord',False)
    if not result[1]: return None
    try:
        header=json.loads(result[1]);n=int(header['chunks'])
        if header['version']!=2 or not 1<=n<=256: raise ValueError('Unsupported native record')
        encoded=''.join(manager.Get6(f'ShapeProgramData{i:03d}',False)[1] for i in range(n))
        raw=zlib.decompress(base64.b64decode(encoded))
        if hashlib.sha256(raw).hexdigest()!=header['sha256']: raise ValueError('Checksum mismatch')
        return json.loads(raw)
    except Exception as exc:
        raise ProgramError('NATIVE_RECORD_INVALID',str(exc)) from exc


def global_values(doc, record):
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    result={}
    for name,binding in record['native']['parameter_bindings'].items():
        i=binding['global_index']
        if eq.Equation(i).split('=')[0].strip()!=f'"{binding.get("native_name",name)}"':
            raise ProgramError('NATIVE_MAPPING_STALE','Global variable order changed')
        result[name]=float(eq.Value(i))
    return result


def observed_revision(doc, record):
    from .shape_observation import geometry_state
    path=Path(str(com_get(doc,'GetPathName')))
    state={**geometry_state(doc), 'revision':record['revision'],'parameters':global_values(doc,record),
           'feature_count':com_get(doc,'GetFeatureCount'),
           'saved_file_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
    return hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()


def write_equation(eq, index, text):
    """Set an equation only when the document holds another text.

    Every SetEquation re-solves the whole model (about 0.6 s on a 270-feature
    part), so rewriting unchanged equations dominated a parameter edit. The
    comparison reads the native text, not the stored record: an equation edited
    outside the program is still restored.
    """
    if str(eq.Equation(index)).replace(' ', '') == text.replace(' ', ''):
        return False
    eq.SetEquation(index, text)
    return True


def set_dimension_equation(eq, native, target, expression):
    """Returns whether the native equation had to be rewritten."""
    binding=native['dimension_equations'][target]
    changed=write_equation(eq, binding['index'], f'"{target}" = {expression}')
    binding['expression']=expression
    return changed


def curves_current(automation,doc,native,tolerance_mm=1e-3,curves=None):
    """Proof that every equation-driven curve was regenerated with the current dimension values.

    The expected end points of each recorded curve are computed from its expressions and
    the live dimensions, then looked up among the points of its native sketch. A sketch a
    normal rebuild skipped still holds the old points and fails this check. curves limits
    the proof to some of the recorded curves: the whole record costs about 65 ms a curve.
    """
    from ..native_expressions import curve_endpoints,referenced
    checked=native['curves'] if curves is None else curves
    names=set()
    for curve in checked:
        for expression in curve['xyz'][:3 if curve['mode']=='3d' else 2]:
            names|=referenced(expression)
    values={}
    for name in names:
        dimension=doc.Parameter(name)
        if dimension is None:return False
        values[name]=float(com_get(dimension,'SystemValue'))*1000
    sketches={}
    for curve in checked:sketches.setdefault(curve['sketch'],[]).append(curve)
    for name,members in sketches.items():
        feature=automation._find_feature(doc,name)
        if feature is None:return False
        points=[[float(com_get(p,k))*1000 for k in ('X','Y','Z')]
                for p in (com_get(com_get(feature,'GetSpecificFeature2'),'GetSketchPoints2') or [])]
        for curve in members:
            for expected in curve_endpoints(curve,values):
                limit=tolerance_mm*(1+max(abs(v) for v in expected)*1e-3)
                if not any(math.dist(expected,point)<=limit for point in points):
                    logger.info('Shape rebuild check: stale curve in %s',name)
                    return False
    return True


def forced_rebuild(automation,doc,equations,native,witnesses):
    """ForceRebuild3, proven on a few curves the edit must have moved; returns the rebuild mode.

    Seen once in a long session: the rebuild answered True, the dimensions held the new
    values and every equation curve kept its old points. The part was saved with a new H and
    bodies of the old size, and the edit reported success. A stale witness earns one more
    evaluation and rebuild; if it is still stale the edit fails and the document is restored.
    """
    if not bool(doc.ForceRebuild3(False)):
        raise rebuild_error(automation,doc,'Edited document did not rebuild',native.get('ownership'))
    if not witnesses or curves_current(automation,doc,native,curves=witnesses):
        return 'forced'
    logger.warning('Shape rebuild left curves stale (checked %s); evaluating and rebuilding once more',
                   [curve['sketch'] for curve in witnesses])
    com_get(equations,'EvaluateAll')
    if not bool(doc.ForceRebuild3(False)):
        raise rebuild_error(automation,doc,'Edited document did not rebuild',native.get('ownership'))
    if not curves_current(automation,doc,native,curves=witnesses):
        raise ProgramError('NATIVE_REBUILD_STALE',
                           'SolidWorks rebuilt the part without regenerating its curves for the new values',
                           sketches=[curve['sketch'] for curve in witnesses],rebuilds=2)
    return 'forced_twice'


def witness_curves(native,dimensions,rewritten,limit=3):
    """A few recorded curves an edit must have moved: the first, middle and last of those
    that depend on a rewritten dimension or were rewritten themselves."""
    from ..native_expressions import referenced
    marked={id(curve) for curve in rewritten}
    found=[curve for curve in native['curves'] if id(curve) in marked or
           any(referenced(e)&dimensions for e in curve['xyz'][:3 if curve['mode']=='3d' else 2])]
    if len(found)<=limit:return found
    return [found[round(i*(len(found)-1)/(limit-1))] for i in range(limit)]


FEATURE_HINTS={
    'Sweep':'a sweep fails when its path bends tighter than the tube radius: move the path points apart or reduce the radius',
    'Fillet':'the radius is too large for the neighbouring faces at these parameter values',
    'Chamfer':'the distance is too large for the neighbouring faces at these parameter values',
    'CombineBodies':'the inputs no longer overlap, or an input above it failed',
    'Blend':'neighbouring sections are too close or cross each other',
    'MirrorSolid':'the mirrored component failed above it',
    'LPattern':'the patterned component failed above it','CirPattern':'the patterned component failed above it',
}


def feature_owners(ownership):
    """Feature name -> shape component, from the ownership map of a compiled part."""
    owners={}
    for component,item in (ownership or {}).items():
        owners.setdefault(component,component)
        for feature in list(item.get('features',[]))+list(item.get('support',[])):
            owners.setdefault(feature,component)
    return owners


def rebuild_failures(automation,doc,ownership=None,limit=8):
    """Features that report an error after a rebuild, in tree order: the first one is the cause."""
    owners=feature_owners(ownership)
    found=[]
    try:
        for feature in automation._walk_features(doc):
            code,warning=typed(feature,'IFeature').GetErrorCode2()
            if not code:continue
            name=str(com_get(feature,'Name'))
            kind=str(com_get(feature,'GetTypeName2',default=''))
            found.append({'feature':name,'type':kind,'code':int(code),'warning':bool(warning),
                          **({'component':owners[name]} if name in owners else {}),
                          **({'hint':FEATURE_HINTS[kind]} if kind in FEATURE_HINTS and not found else {})})
            if len(found)>=limit:break
    except Exception as exc:
        logger.info('Shape rebuild diagnostics unavailable: %s',exc)
    return found


def rebuild_error(automation,doc,message,ownership=None):
    """NATIVE_REBUILD_FAILED that names the failing features; read before the document is restored."""
    failures=rebuild_failures(automation,doc,ownership)
    if failures:
        first=failures[0]
        message+=': '+first['feature']+' ('+first['type']+')'
        if len(failures)>1:message+=' and '+str(len(failures)-1)+' dependent feature(s)'
        if first.get('hint'):message+='; '+first['hint']
    return ProgramError('NATIVE_REBUILD_FAILED',message,features=failures,
                        **({'component':failures[0]['component']} if failures and failures[0].get('component') else {}))


def validate_feature_health(automation,doc,names):
    issues=[]
    for name in names:
        feature=automation._find_feature(doc,name)
        if feature is None:
            issues.append({'feature':name,'code':'MISSING'})
            continue
        code,warning=typed(feature,'IFeature').GetErrorCode2()
        if code: issues.append({'feature':name,'type':str(com_get(feature,'GetTypeName2',default='')),
                                'code':code,'warning':bool(warning)})
    if issues:
        first=issues[0]
        raise ProgramError('NATIVE_REBUILD_FAILED','Native feature errors or warnings: '+first['feature']+
                           (' ('+first['type']+')' if first.get('type') else ''),features=issues)


# Directions the camera may look along while loft connectors are picked.
PICK_VIEWS = ((-1, -1, -1), (0, 0, -1), (0, -1, 0), (-1, 0, 0), (1, -1, -1), (-1, -1, 1))


def pick_view(sections, picks):
    """Camera direction that shows every pick point apart from all other section vertices.

    SolidWorks resolves a pick in screen space. Seen along the line through two
    vertices they coincide and the connector starts at either one: the stations
    of a variable tube, whose vertices lie in front of each other in the Front
    view, then gave a loft that failed or twisted. Returns the direction and the
    smallest apparent separation, 0 (coincident) to 1 (seen square on).
    """
    vertices = [point for points in sections for point in points]
    best = None
    for candidate in PICK_VIEWS:
        length = math.sqrt(sum(v*v for v in candidate))
        look = [v/length for v in candidate]
        clear = 1.
        for pick in picks:
            for point in vertices:
                delta = [a-b for a, b in zip(point, pick)]
                size = math.sqrt(sum(v*v for v in delta))
                if size < 1e-9:
                    continue
                along = sum(a*b for a, b in zip(delta, look))
                clear = min(clear, math.sqrt(max(0., size*size-along*along))/size)
        if best is None or clear > best[1]+1e-9:
            best = (candidate, clear)
    return best


def recover_path_parameterization(doc, feature, objects, spec):
    """One shape-preserving retry for an equation-path solver warning."""
    if spec.get('role') != 'path' or spec['mode'] != '3d' or len(objects) != 1:
        return False
    code, warning = typed(feature, 'IFeature').GetErrorCode2()
    if code != 1 or not warning:
        return False
    original = spec['curves'][0]
    if original.get('parameter_reversed'):
        return False
    reversed_curve = reverse_native_curve(original)
    xyz = reversed_curve['xyz']
    ok = bool(typed(objects[0], 'ISketchSpline').SetEquationParameters2(
        *xyz, *original['range'], False, 0, 0, 0, True, True))
    rebuilt = bool(doc.ForceRebuild3(False)) if ok else None
    health = typed(feature, 'IFeature').GetErrorCode2() if rebuilt else None
    if not ok or not rebuilt or health[0]:
        raise ProgramError('NATIVE_PATH_FAILED',
                           'Native equation path failed both parameter directions',
                           feature=spec['name'], component=spec['name'].rsplit('_Path', 1)[0], attempts=2,
                           reversed_curve_accepted=ok, rebuilt=rebuilt,
                           error_code=None if health is None else int(health[0]))
    spec['curves'][0] = reversed_curve
    return True


def feature_inventory(automation,doc):
    return {str(com_get(f,'Name')):str(com_get(f,'GetTypeName2'))
            for f in automation._walk_features(doc)}


class ParametricCompiler(NativeCompiler):
    def __init__(self,automation,doc):
        super().__init__(automation,doc)
        self.bindings={};self.curves=[];self.dimension_equations={};self.feature_refs={}
        self.scale_features={}
        self.placement_features={}
        self.ownership={}
        self.spec_index={}          # Planned specs by component, for dependency closures.
        self.pending_equations=[]   # Dimension equations queued until finish().
        self.sketch_dimensions={}   # Sketch name -> {role: native dimension} of native entities.

    def add_equation(self,target,expression,immediate=False):
        """Bind a native dimension to an expression.

        Every equation in the manager is re-evaluated by each rebuild, and every sketch
        exit is a rebuild: adding them while features are still being created made a
        long build quadratic. A new equation only restates the value its dimension was
        created with, so it is queued and all of them are added, evaluated and checked
        once in finish(). Parameter bindings are immediate: curves depend on them.
        """
        if immediate or EAGER_EQUATIONS:
            return self._write_equation(target,expression,True)
        self.pending_equations.append(target)
        self.dimension_equations[target]={'index':-1,'expression':expression}
        return -1

    def _write_equation(self,target,expression,solve):
        equation=f'"{target}" = {expression}'
        manager=typed(com_get(self.doc,'GetEquationMgr'),'IEquationMgr')
        index=manager.Add2(-1,equation,solve)
        self.require(index>=0,f'Native equation rejected: {equation}','NATIVE_EQUATION_FAILED')
        self.dimension_equations[target]={'index':index,'expression':expression}
        return index

    def flush_equations(self):
        """Add the queued equations; called once, after the last feature of the batch."""
        pending,self.pending_equations=self.pending_equations,[]
        for target in pending:
            if target in self.dimension_equations and self.dimension_equations[target]['index']<0:
                self._write_equation(target,self.dimension_equations[target]['expression'],False)

    def parameters(self,program,sketch_name='ShapeParameters'):
        doc,sm=self.doc,self.doc.SketchManager
        typed(doc,'IModelDoc2').SetUserPreferenceIntegerValue(
            resolve_solidworks_constant('swUnitSystem'),resolve_solidworks_constant('swUnitSystem_MMGS'))
        if not program['parameters']: return
        self.select_standard_plane('Front')
        sm.InsertSketch(True)
        dimensions={}
        with self.a.dimension_input_guard():
            for i,(name,p) in enumerate(program['parameters'].items()):
                doc.ClearSelection2(True)
                sm.AddToDB=True
                try: segment=sm.CreateLine(0,i*.005,0,p['value']*.001,i*.005,0)
                finally: sm.AddToDB=False
                self.require(segment is not None,'Cannot create parameter driver')
                start=com_get(typed(segment,'ISketchLine'),'GetStartPoint2')
                start.Select4(False,create_select_data(doc,0))
                com_get(doc,'SketchAddConstraints','sgFIXED')
                doc.ClearSelection2(True)
                segment.Select4(False,create_select_data(doc,0))
                com_get(doc,'SketchAddConstraints','sgHORIZONTAL2D')
                segment.Select4(False,create_select_data(doc,0))
                display=typed(doc,'IModelDoc2').AddDimension2(p['value']*.0005,i*.005+.002,0)
                self.require(display is not None,'Cannot create native parameter dimension')
                dimensions[name]=com_get(display,'GetDimension2',0)
        self.finish_sketch(sketch_name)
        manager=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
        for name,p in program['parameters'].items():
            suffix='mm' if p['unit']=='mm' else ''
            native_name=native_parameter_name(name)
            index=manager.Add2(-1,f'"{native_name}" = {p["value"]:.15g}{suffix}',True)
            self.require(index>=0,'Cannot create global variable '+name)
            dimension=short_dimension(dimensions[name])
            self.add_equation(dimension,f'"{native_name}"'+(' * 1mm' if p['unit']=='ratio' else ''),immediate=True)
            self.bindings[name]={'dimension':dimension,'global_index':index,'unit':p['unit'],'native_name':native_name}
        self.require(bool(com_get(doc,'EditRebuild3')),'Parameter sketch did not rebuild')

    def create_sketch(self,spec):
        doc,sm=self.doc,self.doc.SketchManager
        doc.ClearSelection2(True)
        if spec['mode']=='3d': sm.Insert3DSketch(True)
        elif spec['mode']=='anchor':
            self.begin_profile(self.a._find_feature(doc,spec['plane']))
        elif spec['mode']=='normal':
            path=com_get(self.a._find_feature(doc,spec['path']),'GetSpecificFeature2')
            points=list(com_get(path,'GetSketchPoints2') or [])
            origin=[v*.001 for v in spec['origin']]
            start=min(points,key=lambda p:math.dist([com_get(p,k) for k in ('X','Y','Z')],origin))
            self.require(math.dist([com_get(start,k) for k in ('X','Y','Z')],origin)<1e-7,
                         'Cannot resolve native path endpoint')
            segment=com_get(path,'GetSketchSegments')[0]
            self.require(bool(segment.Select4(False,create_select_data(doc,0))),'Cannot select path tangent')
            self.require(bool(start.Select4(True,create_select_data(doc,0))),'Cannot select path endpoint')
            plane=typed(doc,'IModelDoc2').CreatePlanePerCurveAndPassPoint3(True,True)
            self.require(plane is not None,'Cannot create dependent normal plane')
            feature=self.last_feature();feature.Name=spec['name']+'_Frame'
            self.support.append(feature.Name)
            self.begin_profile(feature)
            self.require(math.dist(self.local(spec['origin']),(0,0,0))<1e-7,
                         'Native section origin does not coincide with the path endpoint')
        else:
            self.select_standard_plane(spec.get('plane','Front'))
            sm.InsertSketch(True)
            check=spec.get('frame_check')
            if check:
                # The recipe was planned in this plane's sketch frame; never build a mirrored profile.
                actual=self.a._model_to_sketch_point(doc,[v*.001 for v in check['model']])
                self.require(abs(actual[2])<1e-9 and all(abs(actual[i]*1000-check['sketch'][i])<1e-6 for i in range(2)),
                             'The native sketch frame of this plane differs from the planned one','PROFILE_FRAME_MISMATCH')
        objects=[]
        for curve in spec['curves']:
            xyz=curve['xyz']
            logger.info('Shape stage=curve sketch=%s expression_lengths=%s',spec['name'],list(map(len,xyz)))
            old_add=sm.AddToDB
            relation_option=resolve_solidworks_constant('swSketchAutomaticRelations')
            old_relations=self.a.app.GetUserPreferenceToggle(relation_option)
            try:
                self.a.app.SetUserPreferenceToggle(relation_option,False)
                sm.AddToDB=True
                segment=typed(sm,'ISketchManager').CreateEquationSpline2(
                    xyz[0],xyz[1],xyz[2] if spec['mode']=='3d' else '',
                    str(curve['range'][0]),str(curve['range'][1]),False,0,0,0,True,True)
            finally:
                sm.AddToDB=old_add
                self.a.app.SetUserPreferenceToggle(relation_option,old_relations)
            self.require(segment is not None,'Equation-driven curve was rejected')
            typed(segment,'ISketchSegment').ConstructionGeometry=False
            objects.append(segment)
        if spec['axis']:
            sm.AddToDB=True
            try:
                axis=(sm.CreateCenterLine(-.001,0,0,.1,0,0) if spec.get('axis_line')=='horizontal'
                      else sm.CreateCenterLine(0,-.001,0,0,.1,0))
            finally: sm.AddToDB=False
            self.require(axis is not None,'Cannot create native revolve axis')
        native_dimensions={}
        if spec.get('circle_through_tip'):self.circle_through_tip(objects)
        if spec.get('chord'):self.chord(objects)
        if spec.get('circle'):native_dimensions=self.native_circle(spec['circle'])
        elif spec.get('slot'):native_dimensions=self.native_slot(spec['slot'])
        feature=self.finish_sketch(spec['name'],spec['mode']=='3d')
        if native_dimensions:
            # Read the names after the sketch is renamed: they become D<n>@<sketch name>.
            self.sketch_dimensions[spec['name']]={role:short_dimension(dimension)
                                                  for role,dimension in native_dimensions.items()}
        recover_path_parameterization(doc, feature, objects, spec)
        for i,obj in enumerate(objects):
            self.curves.append({'sketch':spec['name'],'index':i,'mode':spec['mode'],
                                'persistent_id':persist(doc,obj),**spec['curves'][i]})
        return feature

    def constrain(self,entities,relation):
        """Add one sketch relation to explicitly selected entities of the active sketch."""
        doc=self.doc;doc.ClearSelection2(True)
        for i,entity in enumerate(entities):
            self.require(bool(entity.Select4(i>0,create_select_data(doc,0))),'Cannot select a sketch entity')
        com_get(doc,'SketchAddConstraints',relation)
        doc.ClearSelection2(True)

    def dimension(self,entities,x,y):
        """Driving dimension on explicitly selected entities; returns the native dimension."""
        doc=self.doc;doc.ClearSelection2(True)
        for i,entity in enumerate(entities):
            self.require(bool(entity.Select4(i>0,create_select_data(doc,0))),'Cannot select a sketch entity')
        with self.a.dimension_input_guard():
            display=typed(doc,'IModelDoc2').AddDimension2(x,y,0.)
        self.require(display is not None,'Cannot create a native driving dimension')
        doc.ClearSelection2(True)
        return com_get(display,'GetDimension2',0)

    def require_defined(self,what):
        status=int(com_get(com_get(self.doc.SketchManager,'ActiveSketch'),'GetConstrainedStatus'))
        self.require(status==resolve_solidworks_constant('swFullyConstrained'),
                     f'The native {what} sketch is not fully defined (status {status})')

    def native_circle(self,circle):
        """A true circle at the sketch origin, driven by one diameter dimension."""
        sm=self.doc.SketchManager;radius=circle['radius']*.001
        sm.AddToDB=True
        try: entity=sm.CreateCircleByRadius(0.,0.,0.,radius)
        finally: sm.AddToDB=False
        self.require(entity is not None,'Cannot create the native circle')
        self.constrain([com_get(typed(entity,'ISketchArc'),'GetCenterPoint2')],'sgFIXED')
        diameter=self.dimension([entity],radius*1.5,radius*1.5)
        self.require_defined('circle')
        return {'diameter':diameter}

    def circle_through_tip(self,objects):
        """A true circle about the sketch origin through the end of the equation-driven radius line."""
        sm=self.doc.SketchManager
        sketch=com_get(sm,'ActiveSketch')
        self.require(len(objects)==1,'The radius construction needs exactly one curve')
        typed(objects[0],'ISketchSegment').ConstructionGeometry=True
        points=list(com_get(sketch,'GetSketchPoints2') or [])
        self.require(bool(points),'The radius curve has no end point')
        tip=max(points,key=lambda p:math.hypot(com_get(p,'X'),com_get(p,'Y')))
        radius=math.hypot(com_get(tip,'X'),com_get(tip,'Y'))
        self.require(radius>1e-7,'The radius curve is degenerate')
        sm.AddToDB=True
        try: circle=sm.CreateCircleByRadius(0.,0.,0.,radius)
        finally: sm.AddToDB=False
        self.require(circle is not None,'Cannot create the native circle')
        self.constrain([com_get(typed(circle,'ISketchArc'),'GetCenterPoint2')],'sgFIXED')
        # The circle is sized by this relation alone: no dimension, no manager equation.
        self.constrain([tip,circle],'sgCOINCIDENT')

    def chord(self,objects):
        """A true line joining the ends of the equation-driven curve: a valid pattern direction or axis.

        An equation curve is a spline even when it is straight, and a spline is no
        direction. The line follows the curve ends by relation alone: no dimension.
        """
        sm=self.doc.SketchManager
        sketch=com_get(sm,'ActiveSketch')
        self.require(len(objects)==1,'The direction construction needs exactly one curve')
        typed(objects[0],'ISketchSegment').ConstructionGeometry=True
        points=list(com_get(sketch,'GetSketchPoints2') or [])
        self.require(len(points)==2,'The direction curve needs two end points')
        ends=[[float(com_get(p,k)) for k in ('X','Y','Z')] for p in points]
        self.require(math.dist(*ends)>1e-7,'The direction curve is degenerate')
        sm.AddToDB=True
        try: line=sm.CreateLine(*ends[0],*ends[1])
        finally: sm.AddToDB=False
        self.require(line is not None,'Cannot create the native direction line')
        line=typed(line,'ISketchLine')
        self.constrain([com_get(line,'GetStartPoint2'),points[0]],'sgCOINCIDENT')
        self.constrain([com_get(line,'GetEndPoint2'),points[1]],'sgCOINCIDENT')

    def native_slot(self,slot):
        """A straight slot centred on the sketch origin: two arcs and two lines, two dimensions."""
        sm=self.doc.SketchManager
        half,radius=slot['half_length']*.001,slot['radius']*.001
        ends=[(0.,-half),(0.,half)] if slot['vertical'] else [(-half,0.),(half,0.)]
        sm.AddToDB=True
        try:
            made=typed(sm,'ISketchManager').CreateSketchSlot(
                resolve_solidworks_constant('swSketchSlotCreationType_line'),
                resolve_solidworks_constant('swSketchSlotLengthType_CenterCenter'),
                2*radius,ends[0][0],ends[0][1],0.,ends[1][0],ends[1][1],0.,0.,0.,0.,1,False)
        finally: sm.AddToDB=False
        self.require(made is not None,'Cannot create the native slot')
        sketch=com_get(sm,'ActiveSketch')
        segments=list(com_get(sketch,'GetSketchSegments') or [])
        points=list(com_get(sketch,'GetSketchPoints2') or [])
        def at(x,y):
            found=[p for p in points if math.hypot(com_get(p,'X')-x,com_get(p,'Y')-y)<1e-8]
            self.require(len(found)==1,'Cannot identify a native slot point')
            return found[0]
        axis=[s for s in segments if bool(com_get(s,'ConstructionGeometry'))]
        lines=[s for s in segments if not bool(com_get(s,'ConstructionGeometry')) and
               int(com_get(s,'GetType'))==resolve_solidworks_constant('swSketchLINE')]
        self.require(len(axis)==1 and len(lines)==2,'Unexpected native slot entities')
        # Position and direction are fixed; the two dimensions carry all the size.
        self.constrain([at(0.,0.)],'sgFIXED')
        self.constrain([axis[0]],'sgVERTICAL2D' if slot['vertical'] else 'sgHORIZONTAL2D')
        offset=radius*2.5
        length=self.dimension([at(*ends[0]),at(*ends[1])],*((offset,0.) if slot['vertical'] else (0.,-offset)))
        width=self.dimension(lines,*((0.,half+offset) if slot['vertical'] else (half+offset,0.)))
        self.require_defined('slot')
        return {'slot_length':length,'diameter':width}

    def bind_dimensions(self,spec,targets):
        """Tie named native dimensions to their expressions and remember them for edits."""
        for role,expression in spec.get('dimensions',{}).items():
            self.require(role in targets,'Missing native dimension for '+role)
            self.add_equation(targets[role],expression)
        if targets:self.components[spec['id']]['native_dimensions']=dict(targets)

    def owned_features(self,component):
        """Native features that created the faces of a component, through its dependencies."""
        names=set(self.ownership.get(component,{}).get('features',[]))
        for seed in (self.spec_index.get(component,{}).get('seeds') or
                     self.components.get(component,{}).get('dependencies') or []):
            names|=self.owned_features(seed)
        return names

    def select_edges(self,body,selector):
        """Resolve a numbering-independent edge selector on the live body."""
        if selector.get('all'):
            return list(com_get(body,'GetEdges') or [])
        if 'planar_loop' in selector:
            index='XYZ'.index(selector['planar_loop']['axis'])
            sign=1 if selector['planar_loop']['side']=='max' else -1
            faces=[]
            for face in com_get(body,'GetFaces') or []:
                if not bool(com_get(com_get(face,'GetSurface'),'IsPlane')):continue
                if abs(abs(com_get(face,'Normal')[index])-1)>1e-9:continue
                faces.append((sign*com_get(face,'GetBox')[index],face))
            self.require(bool(faces),'No planar face is perpendicular to the requested axis','EDGES_NOT_FOUND')
            extreme=max(level for level,_ in faces)
            edges={}
            for level,face in faces:
                if extreme-level>1e-7:continue
                for loop in com_get(face,'GetLoops') or []:
                    if bool(com_get(loop,'IsOuter')):
                        for edge in com_get(loop,'GetEdges') or []:
                            edges[persist(self.doc,edge)]=edge
            return list(edges.values())
        first,second=(self.owned_features(n) for n in selector['between'])
        def owner(face):
            return str(com_get(com_get(face,'GetFeature'),'Name'))
        edges=[]
        for edge in com_get(body,'GetEdges') or []:
            pair=[f for f in (com_get(edge,'GetTwoAdjacentFaces2') or []) if f is not None]
            if len(pair)!=2:continue
            a,b=owner(pair[0]),owner(pair[1])
            if (a in first and b in second) or (a in second and b in first):edges.append(edge)
        return edges

    def edge_feature(self,spec):
        """Native Fillet or Chamfer on explicitly selected edges of one body."""
        doc=self.doc;name=spec['id'];seed=spec['seeds'][0];info=spec['edge_feature']
        self.stage='native_'+info['kind']
        source=self.components[seed]
        if not source['bodies'] and source.get('consumed_by'):
            raise ProgramError('COMPONENT_CONSUMED',f"{seed} is consumed by {source['consumed_by']}",
                               component=seed,consumed_by=source['consumed_by'],stage=self.stage)
        self.require(len(source['bodies'])==1,'An edge feature needs a component with one body')
        body_name=source['bodies'][0];body=self.a._find_body(doc,body_name)
        self.require(body is not None,'Edge feature body is missing: '+body_name)
        others=self.body_names()-{body_name}
        edges=self.select_edges(body,info['edges'])
        self.require(bool(edges),'No edge matches the selector '+repr(info['edges']),'EDGES_NOT_FOUND')
        doc.ClearSelection2(True)
        for i,edge in enumerate(edges):
            self.require(bool(typed(edge,'IEntity').Select4(i>0,create_select_data(doc,1))),'Cannot select an edge')
        manager=typed(doc.FeatureManager,'IFeatureManager');size=info['size']*.001
        if info['kind']=='fillet':
            feature=manager.FeatureFillet3(195,size,0.,0.,0,0,0,0,0,0,0,0,0,0)
        else:
            feature=manager.InsertFeatureChamfer(1,1,size,math.pi/4,0.,0.,0.,0.)
        doc.ClearSelection2(True)
        expected='Fillet' if info['kind']=='fillet' else 'Chamfer'
        # No partial result: a kernel refusal is an explicit failure and the edit rolls back.
        self.require(feature is not None and str(com_get(feature,'GetTypeName2'))==expected,
                     f'The native {info["kind"]} failed for size {info["size"]} mm; reduce it or narrow the edges',
                     'EDGE_FEATURE_FAILED')
        feature.Name=name
        result=[b for b in self.bodies() if str(com_get(b,'Name')) not in others]
        self.require(len(result)==1 and int(com_get(result[0],'GetFaceCount'))>0,
                     'The edge feature did not keep one solid body')
        result[0].Name=name
        source.update(bodies=[],face_count=0,consumed_by=name)
        self.components[name]={'feature':name,'bodies':[name],'dependencies':[seed],
                               'face_count':int(com_get(result[0],'GetFaceCount')),
                               'edge_count':len(edges),'edges':info['edges']}
        self.bind_dimensions(spec,{'size':'D1@'+name})

    def section_points(self,profiles):
        """Vertices of every 3D section sketch in model metres; None when a section is a plane sketch."""
        sections=[]
        for f in profiles:
            if str(com_get(f,'GetTypeName2',default=''))!='3DProfileFeature':return None
            sketch=com_get(f,'GetSpecificFeature2')
            points=[[float(com_get(p,k)) for k in ('X','Y','Z')] for p in com_get(sketch,'GetSketchPoints2') or []]
            if len(points)<2:return None
            sections.append(points)
        return sections

    def show_picks(self,profiles,picks):
        """Turn and frame the camera so that the pick points are unambiguous; loft() restores it.

        A camera failure is logged and never fails the build: the loft is still attempted.
        """
        doc=self.doc
        try:
            view=doc.ActiveView
            sections=self.section_points(profiles)
            direction,clear=pick_view(sections,picks)
            if getattr(self,'_loft_camera',None) is None:
                self._loft_camera=(view.Orientation3,view.Translation3,float(view.Scale2))
            vertices=[p for points in sections for p in points]
            from .com_utils import live_graphics
            with live_graphics(doc):
                turned=self.a.set_view_orientation(view_direction=list(direction),zoom_to_fit=False)
                self.a._apply_zoom_bbox_m(doc,[min(p[k] for p in vertices) for k in range(3)],
                                          [max(p[k] for p in vertices) for k in range(3)],margin=.5)
            if not turned.get('success') or clear<.2:
                logger.warning('Shape loft picks of %s may be ambiguous on screen: view=%s separation=%.2f',
                               self.component,turned.get('message'),clear)
        except Exception as exc:
            logger.warning('Shape loft could not show the pick points of %s: %s',self.component,exc)

    def restore_camera(self):
        camera,self._loft_camera=getattr(self,'_loft_camera',None),None
        if camera is None:return
        try:
            view=self.doc.ActiveView
            view.Orientation3=camera[0]
            view.Scale2=camera[2]
            # Scale2 also moves the view: the translation goes last.
            view.Translation3=camera[1]
        except Exception as exc:
            logger.warning('Shape loft could not restore the camera: %s',exc)

    def connector_points(self,profiles):
        """One vertex per 3D section sketch at the same angular position, in model metres.

        Without pick points SolidWorks starts the connector of an exact circle at
        an arbitrary vertex: an ellipse lofted to a circle came out twisted by a
        quarter turn, an hourglass instead of a cone. Returns None when a section
        is not a 3D sketch (its points are then in sketch space).
        """
        sections=self.section_points(profiles)
        if sections is None:return None
        result=[];direction=None
        for points in sections:
            center=[sum(p[k] for p in points)/len(points) for k in range(3)]
            arms=[[p[k]-center[k] for k in range(3)] for p in points]
            if direction is None:
                # Any fixed rule works for the first section; the others follow it.
                first=max(arms,key=lambda a:(round(a[0],9),round(a[1],9),round(a[2],9)))
                length=math.sqrt(sum(v*v for v in first))
                if length<1e-9:return None
                direction=[v/length for v in first]
            best=max(range(len(points)),key=lambda i:sum(a*d for a,d in zip(arms[i],direction)))
            result.append(points[best])
        return result

    def select_loft_profiles(self,profiles):
        """Select the sections with aligned pick points; plain contour selection otherwise."""
        doc=self.doc
        picks=self.connector_points(profiles)
        if picks is not None:
            self.show_picks(profiles,picks)
            doc.ClearSelection2(True)
            extension=typed(doc.Extension,'IModelDocExtension')
            if all(bool(extension.SelectByID2(str(com_get(f,'Name')),'SKETCH',p[0],p[1],p[2],i>0,1,None,0))
                   for i,(f,p) in enumerate(zip(profiles,picks))):
                return 'aligned'
            logger.warning('Shape loft pick points were not accepted for %s',self.component)
        doc.ClearSelection2(True)
        for i,f in enumerate(profiles):
            contours=list(com_get(com_get(f,'GetSpecificFeature2'),'GetSketchContours') or [])
            self.require(len(contours)==1 and bool(com_get(contours[0],'IsClosed')),
                         'Loft requires one closed contour per section')
            self.require(bool(contours[0].Select2(i>0,create_select_data(doc,1))),
                         'Cannot select native loft contour')
        return 'contours'

    def loft(self,profiles,path=None):
        """Native loft through closed section sketches, optionally along a centerline.

        SW2026 resolves loft connectors in screen space: with the sections only a
        few pixels wide (a fresh part at its default zoom) the same valid input
        returns no feature, and two vertices that coincide on screen are picked
        at random. The sections are shown clear of each other while they are
        selected; the camera is restored afterwards. Fit the view and try once
        more before failing.
        """
        doc=self.doc;manager=typed(doc.FeatureManager,'IFeatureManager')
        self._loft_camera=None
        try:
            for attempt in range(2):
                self.select_loft_profiles(profiles)
                if path is not None:
                    self.require(bool(path.Select2(True,4)),'Cannot select loft centerline')
                feature=manager.InsertProtrusionBlend2(
                    False,True,False,1.,0,0,1.,1.,True,True,False,0.,0.,0,False,False,False,0)
                if feature is not None:return feature
                if attempt==0:
                    logger.warning('Shape loft returned no feature for %s; fitting the view and retrying',self.component)
                    from .com_utils import live_graphics
                    with live_graphics(doc):
                        doc.ViewZoomtofit2()
                        doc.GraphicsRedraw2()
            doc.ClearSelection2(True)
            return None
        finally:
            self.restore_camera()

    def linear_spans(self,name,profiles,before):
        """One ruled loft per pair of neighbouring sections, united into one body."""
        doc=self.doc;manager=typed(doc.FeatureManager,'IFeatureManager');spans=[]
        self.require(len(profiles)%2==0,'Linear spans need two section sketches per span')
        for i in range(len(profiles)//2):
            existing=self.body_names()
            # Same native call as the smooth loft; only the section pairing differs.
            feature=self.loft(profiles[2*i:2*i+2])
            self.require(feature is not None,f'Native loft of linear span {i+1} failed')
            feature.Name=f'{name}_Span{i+1}'
            self.feature_refs[str(feature.Name)]=persist(doc,feature)
            # A later span may merge into its neighbour at once or stay a separate body.
            for body in self.bodies():
                if str(com_get(body,'Name')) not in existing:
                    body.Name=f'{name}_Span{i+1}';spans.append(str(com_get(body,'Name')))
        self.require(bool(spans),'Linear spans created no body')
        if len(spans)>1:
            doc.ClearSelection2(True)
            for i,body_name in enumerate(spans):
                self.require(bool(typed(self.a._find_body(doc,body_name),'IBody2').Select2(i>0,create_select_data(doc,2))),
                             'Cannot select a span body')
            feature=manager.InsertCombineFeature(resolve_solidworks_constant('SWBODYADD'),None,None)
            self.require(feature is not None,'Cannot unite the linear spans')
            doc.ClearSelection2(True)
        else:
            # Already one body: the last span feature becomes the component feature.
            del self.feature_refs[str(feature.Name)]
        # A Combine owns no faces of its own: validate the resulting body, not the feature.
        self.finish_transform(name,feature,before)

    def build(self,spec):
        name,kind=spec['id'],spec['kind'];self.component=name
        self.spec_index[name]=spec
        previous_support=set(self.support);previous_features=set(self.feature_refs)
        logger.info('Shape stage=component name=%s kind=%s',name,kind)
        before=self.body_names()
        sketches=[] if kind=='anchored_extrude' else [self.create_sketch(s) for s in spec['sketches']]
        doc=self.doc;doc.ClearSelection2(True)
        if kind=='anchored_extrude':
            from .shape_anchor import build_anchor
            build_anchor(self,spec)
        elif kind in {'sectioned_solid','variable_tube'}:
            self.stage='parametric_loft'
            path=None
            if kind=='variable_tube':
                path=self.centerline(name,spec,sketches)
                profiles=[f for s,f in zip(spec['sketches'],sketches) if s.get('role')=='section']
            else: profiles=sketches
            if spec.get('spans')=='linear':
                self.linear_spans(name,profiles,before)
            else:
                feature=self.loft(profiles,path)
                self.require(feature is not None,'Native loft failed; check that neighbouring sections do not cross')
                self.finish_solid(name,feature,before)
        elif kind=='tube' and spec['sketches'][-1].get('circle'):
            # A straight tube: one native circle extruded along its axis.
            self.stage='parametric_cylinder'
            from .shape_anchor import plane_basis
            profile=spec['sketches'][-1]['name']
            direction=spec['extrusion']['direction'];length=spec['extrusion']['value']
            normal=plane_basis(doc,sketches[-1])[2]
            dot=sum(n*d for n,d in zip(normal,direction))/length
            self.require(abs(abs(dot)-1)<1e-6,'The native section plane is not normal to the tube axis')
            result=self.a.advanced_extrude(sketch_name=profile,end_condition='blind',depth=length,
                direction_flip=dot<0,merge=False,feature_name=name,unit='mm')
            self.require(result.get('success'),result.get('message','Cylinder extrusion failed'))
            self.finish_solid(name,self.a._find_feature(doc,name),before)
            self.bind_dimensions(spec,{'length':'D1@'+name,**self.sketch_dimensions[profile]})
        elif kind=='tube':
            self.stage='parametric_sweep'
            path_sketch=str(self.centerline(name,spec,sketches).Name)
            result=self.a.create_sweep_feature(path_sketch=path_sketch,
                profile_sketch=spec['sketches'][-1]['name'],profile_type='sketch',
                body_name=name,feature_name=name,merge=False,unit='mm')
            self.require(result.get('success'),result.get('message','Sweep failed'))
            self.components[name]={'feature':name,'bodies':[name],
                                   'face_count':result['data']['face_count']}
        elif spec.get('guided_ellipsoid'):
            from .shape_ellipsoid import build_guided_ellipsoid
            build_guided_ellipsoid(self,spec,sketches)
        elif kind in {'rounded_prism','prism'} and 'sweep' in str(spec.get('variant','')).split('|'):
            # A planar 3D-sketch profile swept along its axis line: no dimension, no body move.
            self.stage='parametric_prism_sweep'
            result=self.a.create_sweep_feature(
                path_sketch=next(s['name'] for s in spec['sketches'] if s.get('role')=='path'),
                profile_sketch=spec['sketches'][-1]['name'],profile_type='sketch',
                body_name=name,feature_name=name,merge=False,unit='mm')
            self.require(result.get('success'),result.get('message','Prism sweep failed'))
            self.components[name]={'feature':name,'bodies':[name],
                                   'face_count':result['data']['face_count']}
        elif kind in {'dome','revolve','ellipsoid','rounded_prism','prism'}:
            extruded=kind in {'rounded_prism','prism'}
            self.stage='parametric_extrude' if extruded else 'parametric_revolve'
            base_name=name+'_Volume' if spec['placement'] or kind=='ellipsoid' else name
            if extruded:
                result=self.a.advanced_extrude(sketch_name=spec['sketches'][0]['name'],
                    end_condition='mid_plane',depth=spec['extrusion']['value'],merge=False,
                    feature_name=base_name,unit='mm')
                if result.get('success'):
                    self.add_equation('D1@'+base_name,spec['extrusion']['expression'])
            else:
                result=self.a.revolve_boss(sketch_name=spec['sketches'][0]['name'],
                                          merge=False,feature_name=base_name,unit='mm')
            self.require(result.get('success'),result.get('message','Revolve failed'))
            feature=self.a._find_feature(doc,base_name)
            if kind=='ellipsoid':
                self.feature_refs[base_name]=persist(doc,feature)
                body=[b for b in self.bodies() if str(com_get(b,'Name')) not in before]
                self.require(len(body)==1,'Cannot identify ellipsoid body')
                doc.ClearSelection2(True)
                self.require(bool(typed(body[0],'IBody2').Select2(False,create_select_data(doc,0))),
                             'Cannot select ellipsoid body')
                feature=typed(doc.FeatureManager,'IFeatureManager').InsertScale(1,False,1,1,spec['scale_z'])
                self.require(feature is not None,'Cannot create ellipsoid aspect feature')
                feature.Name=name+'_Scaled' if spec['placement'] else name
                self.feature_refs[str(feature.Name)]=persist(doc,feature)
                self.scale_features[name]={'feature':str(feature.Name),'scale_z':spec['scale_z']}
            if spec['placement']:
                body=[b for b in self.bodies() if str(com_get(b,'Name')) not in before]
                self.require(len(body)==1,'Cannot identify placed body')
                doc.ClearSelection2(True)
                self.require(bool(typed(body[0],'IBody2').Select2(False,create_select_data(doc,1))),
                             'Cannot select placed body')
                x,y,z=(spec['placement'].get(axis,{}).get('value',0)*.001 for axis in 'XYZ')
                feature=typed(doc.FeatureManager,'IFeatureManager').InsertMoveCopyBody2(
                    x,y,z,0,0,0,0,0,0,0,False,1)
                self.require(feature is not None,'Cannot create native placement')
                feature.Name=name
                for axis,item in spec['placement'].items():
                    self.add_equation({'X':'D1','Y':'D2','Z':'D3'}[axis]+'@'+name,item['expression'])
                self.feature_refs[base_name]=persist(doc,self.a._find_feature(doc,base_name))
                # MoveCopyBody owns no faces. Validate its resulting solid instead.
                bodies=[b for b in self.bodies() if str(com_get(b,'Name')) not in before]
                self.require(len(bodies)==1 and int(com_get(bodies[0],'GetFaceCount'))>0,
                             'Placement did not preserve a solid body')
                bodies[0].Name=name
                self.components[name]={'feature':name,'bodies':[name],
                                       'face_count':int(com_get(bodies[0],'GetFaceCount'))}
            elif kind=='ellipsoid':
                self.finish_transform(name,feature,before)
            else:
                self.finish_solid(name,feature,before)
        elif kind=='combine':
            self.combine(spec)
        elif kind in {'fillet','chamfer'}:
            self.edge_feature(spec)
        elif kind=='pattern':
            self.pattern(spec,sketches[0])
        elif kind=='place':
            from .shape_placement import build_placement
            build_placement(self,spec)
        elif kind=='surface_patch':
            from .shape_surface import build_surface_patch
            build_surface_patch(self,spec)
        else:
            self.mirror(name,{'seeds':spec['seeds'],'plane':spec['plane']})
        if kind=='rounded_prism' and self.sketch_dimensions.get(spec['sketches'][0]['name']):
            self.bind_dimensions(spec,self.sketch_dimensions[spec['sketches'][0]['name']])
        if spec.get('variant'):self.components[name]['recipe']=spec['variant']
        self.components[name]['kind']=kind
        self.feature_refs[name]=persist(doc,self.a._find_feature(doc,name))
        self.ownership[name]={'features':[f for f in self.feature_refs if f not in previous_features],
            'support':[f for f in self.support if f not in previous_support],
            'outputs':[{'name':body_name,'persistent_id':persist(doc,self.a._find_body(doc,body_name))}
                       for body_name in self.components[name]['bodies']]}
        # Frame useful solids once per component, never each helper curve.
        from .shape_working_view import focus_bodies
        working_view = focus_bodies(self.a, doc, self.components[name]['bodies'])
        if not working_view['verified']:
            logger.warning('Shape working view component=%s status=%s', name, working_view['status'])

    def pattern(self,spec,sketch):
        """Native body pattern along (row) or about (circle) the line of its sketch; the copies are the component."""
        self.stage='parametric_pattern'
        doc=self.doc;name=spec['id'];info=spec['pattern']
        groups=[self.components[key]['bodies'] for key in spec['seeds']]
        self.require(all(groups),'Pattern input component was already consumed')
        seeds=[body for group in groups for body in group]
        before=self.body_names()
        segments=list(com_get(typed(com_get(sketch,'GetSpecificFeature2'),'ISketch'),'GetSketchSegments') or [])
        lines=[s for s in segments if int(com_get(s,'GetType'))==resolve_solidworks_constant('swSketchLINE')]
        self.require(len(lines)==1,'Cannot find the pattern direction line')
        doc.ClearSelection2(True)
        self.require(bool(typed(lines[0],'ISketchSegment').Select4(False,create_select_data(doc,1))),
                     'Cannot select the pattern direction')
        for body_name in seeds:
            body=self.a._find_body(doc,body_name)
            self.require(body is not None and bool(typed(body,'IBody2').Select2(True,create_select_data(doc,256))),
                         'Cannot select a pattern seed body')
        manager=typed(doc.FeatureManager,'IFeatureManager')
        if info['mode']=='row':
            feature=manager.FeatureLinearPattern5(
                info['count'],info['spacing']*.001,1,0.,False,False,"","",
                False,False,              # GeometryPattern, VaryInstance
                False,False,False,False,False,False,False,False,0.,0.,False,False)
        else:
            feature=manager.FeatureCircularPattern5(
                info['count'],2*math.pi,False,"",
                False,True,               # GeometryPattern, EqualSpacing
                False,False,False,False,1,0.,"",False)
        self.require(feature is not None,'Native body pattern failed')
        expected=len(seeds)*(info['count']-1)
        feature.Name=name
        new=[b for b in self.bodies() if str(com_get(b,'Name')) not in before]
        self.require(len(new)==expected,f'Expected {expected} pattern bodies, found {len(new)}')
        names=[]
        for i,body in enumerate(new):
            self.require(int(com_get(body,'GetFaceCount'))>0,'The pattern created an empty body')
            body.Name=name if expected==1 else f'{name}_{i+1}'
            names.append(str(com_get(body,'Name')))
        self.components[name]={'feature':name,'bodies':names,'dependencies':list(spec['seeds']),
                               'face_count':sum(int(com_get(b,'GetFaceCount')) for b in new)}
        doc.ClearSelection2(True)
        if info['mode']=='row':
            self.bind_dimensions(spec,{'spacing':'D3@'+name})

    def centerline(self,name,spec,sketches):
        paths=[f for s,f in zip(spec['sketches'],sketches) if s.get('role')=='path']
        if len(paths)==1: return paths[0]
        self.doc.ClearSelection2(True)
        for i,f in enumerate(paths):
            self.require(bool(f.Select2(i>0,1)),'Cannot select composite path segment')
        self.require(bool(com_get(self.doc,'InsertCompositeCurve')),'Cannot create composite path')
        composite=self.last_feature()
        self.require(com_get(composite,'GetTypeName2')=='CompositeCurve','Cannot create composite path')
        composite.Name=name+'_Path';self.support.append(str(composite.Name))
        self.doc.ClearSelection2(True)
        return composite

    def finish_transform(self,name,feature,before):
        bodies=[b for b in self.bodies() if str(com_get(b,'Name')) not in before]
        self.require(len(bodies)==1 and int(com_get(bodies[0],'GetFaceCount'))>0,
                     'Transform did not preserve one solid body')
        feature.Name=name;bodies[0].Name=name
        self.components[name]={'feature':name,'bodies':[name],
                               'face_count':int(com_get(bodies[0],'GetFaceCount'))}

    def combine(self,spec):
        self.stage='parametric_boolean'
        doc=self.doc;name=spec['id'];seeds=spec['seeds']
        groups=[self.components[key]['bodies'] for key in seeds]
        self.require(all(groups),'Boolean input component was already consumed')
        names=[n for group in groups for n in group]
        self.require(len(names)==len(set(names)),'Boolean input bodies overlap in scope')
        before=self.body_names();untouched=before-set(names)
        operation=spec['operation'];doc.ClearSelection2(True)
        if operation=='difference':self.require(len(groups[0])==1,'Difference requires one target body')
        def select(order):
            doc.ClearSelection2(True)
            for i,body_name in enumerate(order):
                body=self.a._find_body(doc,body_name)
                self.require(body is not None,'Boolean input body is missing: '+body_name)
                mark=1 if operation=='difference' and i==0 else 2
                self.require(bool(typed(body,'IBody2').Select2(i>0,create_select_data(doc,mark))),
                             'Cannot select Boolean input body')
        def state():
            return {'features': feature_inventory(self.a,doc),
                    'feature_count': int(com_get(doc,'GetFeatureCount')),
                    'bodies': {str(com_get(b,'Name')): {
                        'reference': persist(doc,b), 'box': list(com_get(b,'GetBodyBox')),
                        'face_count': int(com_get(b,'GetFaceCount')),
                        'mass': list(com_get(typed(b,'IBody2'),'GetMassProperties',1000.))}
                        for b in self.bodies()}}
        saved_state = None
        diagnostics = {'operation':operation,'input_order':list(names),'resolved_order':list(names),
                       'input_components':list(seeds),
                       'component_body_counts':dict(zip(seeds,map(len,groups))),
                       'strategy':'input_order','native_feature_result':None}
        if operation=='union':
            try:
                saved_state=state()
            except Exception as exc:
                from .com_utils import is_rpc_disconnect
                if is_rpc_disconnect(exc): raise
                diagnostics['state_verification_error']=str(exc)
        select(names)
        code=resolve_solidworks_constant({'union':'SWBODYADD','difference':'SWBODYCUT',
                                         'intersection':'SWBODYINTERSECT'}[operation])
        def attempt():
            try:
                return typed(doc.FeatureManager,'IFeatureManager').InsertCombineFeature(code,None,None)
            except Exception as exc:
                from .com_utils import is_rpc_disconnect
                raise ProgramError('RPC_CONNECTION_LOST' if is_rpc_disconnect(exc) else 'NATIVE_BOOLEAN_FAILED',
                                   str(exc), component=name, stage=self.stage, **diagnostics,
                                   com_method='InsertCombineFeature', com_hresult=getattr(exc,'hresult',None)) from exc
        feature=attempt()
        if feature is None and operation=='union' and saved_state is not None:
            doc.ClearSelection2(True)
            retry=False
            try:
                from .boolean_order import native_state_changes, plan_union
                changes=native_state_changes(saved_state,state())
                unchanged=not changes
                diagnostics['failed_attempt_state_unchanged']=unchanged
                if not unchanged:
                    diagnostics['failed_attempt_state_changes']=changes
                if unchanged:
                    plan=plan_union(doc,{n:self.a._find_body(doc,n) for n in names})
                    diagnostics.update(plan)
                    # Planning only touched temporary copies; verify again before the one allowed retry.
                    planning_changes=native_state_changes(saved_state,state())
                    if planning_changes:
                        diagnostics['planning_state_changes']=planning_changes
                    retry=plan['connectivity']=='CONNECTED' and plan['resolved_order']!=names and not planning_changes
            except Exception as exc:
                from .com_utils import is_rpc_disconnect
                if is_rpc_disconnect(exc):
                    raise ProgramError('RPC_CONNECTION_LOST',str(exc),component=name,stage=self.stage,
                                       com_hresult=getattr(exc,'hresult',None),**diagnostics) from exc
                diagnostics['planning_error']={'code':getattr(exc,'code','BOOLEAN_PLAN_UNAVAILABLE'),'message':str(exc)}
            if retry:
                select(plan['resolved_order'])
                diagnostics['retry_count']=1
                feature=attempt()
        self.require(feature is not None,'Native Boolean failed; inspect input order and connectivity evidence',
                     'NATIVE_BOOLEAN_DISCONNECTED' if diagnostics.get('connectivity')=='DISCONNECTED'
                     else 'NATIVE_BOOLEAN_FAILED',**diagnostics)
        diagnostics['native_feature_result']='created'
        feature.Name=name
        bodies=self.bodies()
        self.require(untouched.issubset({str(com_get(b,'Name')) for b in bodies}),
                     'Boolean modified a body outside the explicit scope')
        result=[b for b in bodies if str(com_get(b,'Name')) not in untouched]
        self.require(len(result)==spec['expected_bodies'],
                     f"Boolean result body count differs from expected_bodies: expected {spec['expected_bodies']}, got {len(result)}",
                     expected_bodies=spec['expected_bodies'], actual_bodies=len(result), operation=operation)
        outputs=[]
        for i,body in enumerate(result):
            self.require(int(com_get(body,'GetFaceCount'))>0,'Boolean created an empty body')
            body.Name=name if len(result)==1 else f'{name}_{i+1}'
            outputs.append(str(com_get(body,'Name')))
        for seed in seeds:
            self.components[seed]['bodies']=[]
            self.components[seed]['face_count']=0
            self.components[seed]['consumed_by']=name
        self.components[name]={'feature':name,'bodies':outputs,
                               'face_count':sum(int(com_get(b,'GetFaceCount')) for b in result),
                               'dependencies':seeds,'operation':operation,'boolean_diagnostics':diagnostics}
        doc.ClearSelection2(True)

    def compile(self,program,timer=None):
        logger.info('Shape stage=parameters')
        self.parameters(program)
        specs=plan_recipes(program,{k:v['dimension'] for k,v in self.bindings.items()})
        self.spec_index={spec['id']:spec for spec in specs}
        for spec in specs: self.build(spec)
        return self.finish(program,specs,timer=timer)

    def finish(self,program,specs,timer=None):
        logger.info('Shape stage=rebuild')
        phase(timer,'evaluate')
        self.flush_equations()
        manager=typed(com_get(self.doc,'GetEquationMgr'),'IEquationMgr')
        com_get(manager,'EvaluateAll')
        for target,binding in self.dimension_equations.items():
            # Deferred equations are proven here: each must evaluate to a finite value.
            value=manager.Value(binding['index'])
            self.require(isinstance(value,(int,float)) and math.isfinite(value),
                         'Native equation did not evaluate: '+target,'NATIVE_EQUATION_FAILED')
        phase(timer,'rebuild')
        if not bool(self.doc.ForceRebuild3(False)):
            raise rebuild_error(self.a,self.doc,'Native model failed to rebuild',self.ownership)
        phase(timer,'validation')
        from .shape_placement import settle_placements
        settle_placements(self.doc,self.placement_features)
        validate_feature_health(self.a,self.doc,list(self.components)+self.support)
        from .shape_anchor import validate_anchors
        validate_anchors(self.a,self.doc,self.components)
        for name in self.support:
            f=self.a._find_feature(self.doc,name)
            if f is not None:
                self.doc.ClearSelection2(True)
                if f.Select2(False,0):
                    kind=com_get(f,'GetTypeName2')
                    com_get(self.doc,'BlankRefGeom' if kind in {'RefPlane','RefAxis','RefPoint','CompositeCurve','RefCurve'} else 'BlankSketch')
        self.doc.ClearSelection2(True)
        return {'components':self.components,'support_features':self.support,
                'parameter_bindings':self.bindings,'curves':self.curves,
                'dimension_equations':self.dimension_equations,'feature_refs':self.feature_refs,
                'scale_features':self.scale_features,
                'placement_features':self.placement_features,
                'ownership':self.ownership,'feature_inventory':feature_inventory(self.a,self.doc),
                'recipe_topology':topology(specs),'compiler_version':3,
                'native_parameter_bindings':True,'in_place_edit':True,
                'status':'NATIVE_GEOMETRY_CREATED'}


def update_native(automation,doc,record,program,*,validate=True,timer=None,rebuild_follows=False):
    """Update the definitions of existing native features in place.

    rebuild_follows: the caller creates more features and then runs its own full rebuild
    and validation. When nothing had to be written here, this function's rebuild would
    regenerate an unchanged model, so it is left to the caller.
    """
    native=record['native']
    phase(timer,'validation')
    from .shape_ellipsoid import validate_guided_profiles
    validate_guided_profiles(automation,doc,native['components'])
    from .shape_anchor import validate_anchors
    validate_anchors(automation,doc,native['components'])
    bindings=native['parameter_bindings']
    if set(bindings)!=set(program['parameters']):
        raise ProgramError('TOPOLOGY_CHANGE_REQUIRED','Adding/removing parameters requires a new part in this release')
    if any(bindings[k]['unit']!=v['unit'] for k,v in program['parameters'].items()):
        raise ProgramError('TOPOLOGY_CHANGE_REQUIRED','Parameter units cannot change during an edit')
    specs=plan_recipes(program,{k:v['dimension'] for k,v in bindings.items()})
    current_topology={s['id']:s for s in topology(specs)}
    stored_topology={s['id']:s for s in native['recipe_topology']}
    if json.dumps(current_topology,sort_keys=True)!=json.dumps(stored_topology,sort_keys=True):
        raise ProgramError('TOPOLOGY_CHANGE_REQUIRED','This release updates existing definitions; component topology must remain unchanged')
    curves={(s['name'],i):c for node in specs for s in node['sketches'] for i,c in enumerate(s['curves'])}
    objects=[restore(doc,c['persistent_id']) for c in native['curves']]
    for old,obj in zip(native['curves'],objects):
        actual=typed(obj,'ISketchSpline').GetEquationParameters2()
        expected=old['xyz'][:2]+([old['xyz'][2]] if old['mode']=='3d' else [''])
        if not actual[0] or list(actual[1:4])!=expected:
            raise ProgramError('NATIVE_MAPPING_STALE','A native curve was modified outside the embedded program: '+old['sketch'])
    for reference in native['feature_refs'].values(): restore(doc,reference)
    from .shape_placement import validate_placement_axes, update_placement_equations
    validate_placement_axes(doc,native.get('placement_features',{}))
    for item in native.get('scale_features',{}).values():
        feature=automation._find_feature(doc,item['feature'])
        definition=typed(com_get(feature,'GetDefinition'),'IScaleFeatureData')
        if abs(float(definition.ScaleZ)-item['scale_z'])>1e-9:
            raise ProgramError('NATIVE_MAPPING_STALE','Ellipsoid aspect was modified outside the program')
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    updates=0
    written=0      # Native writes of any kind: equations, curves, feature definitions.
    moved=set();rewritten=[]      # Dimensions and curves the rebuild must be seen to follow.
    phase(timer,'native_update')
    for name,p in program['parameters'].items():
        b=bindings[name]
        suffix='mm' if p['unit']=='mm' else ''
        if write_equation(eq,b['global_index'],f'"{b.get("native_name",name)}" = {p["value"]:.15g}{suffix}'):
            written+=1;moved.add(b['dimension'])
    for old,obj in zip(native['curves'],objects):
        new=retained_native_curve(curves[(old['sketch'],old['index'])], old)
        if old['xyz']==new['xyz'] and old['range']==new['range']: continue
        xyz=new['xyz']
        ok=typed(obj,'ISketchSpline').SetEquationParameters2(
            xyz[0],xyz[1],xyz[2] if old['mode']=='3d' else '',*new['range'],False,0,0,0,True,True)
        if not ok: raise ProgramError('NATIVE_EDIT_FAILED','Cannot update curve in '+old['sketch'])
        old.update(new);updates+=1;rewritten.append(old)
    for spec in specs:
        if spec['kind']=='place':written+=update_placement_equations(eq,native,spec)
        if spec['kind']=='anchored_extrude' and 'circle' in spec['anchor']:
            target=native['components'][spec['id']]['native_anchor']['circle']['dimension']
            written+=set_dimension_equation(eq,native,target,spec['anchor']['circle']['diameter_expression'])
        if spec['kind']=='surface_patch':
            for suffix,key in [('_Surface','offset_expression'),('_Thicken','thickness_expression')]:
                target='D1@'+spec['id']+suffix
                if target not in native['dimension_equations']:continue
                written+=set_dimension_equation(eq,native,target,spec['surface'][key])
        if spec['kind'] in {'rounded_prism','prism','anchored_extrude'} and spec.get('extrusion'):
            name=spec['id']+'_Volume' if spec['placement'] else spec['id']
            written+=set_dimension_equation(eq,native,'D1@'+name,spec['extrusion']['expression'])
        # Named native dimensions of the newer recipes: slot, circle, cylinder length, fillet size.
        targets=native['components'][spec['id']].get('native_dimensions',{})
        for role,expression in spec.get('dimensions',{}).items():
            written+=set_dimension_equation(eq,native,targets[role],expression)
        if spec['kind']=='ellipsoid' and not spec.get('guided_ellipsoid'):
            old=native['scale_features'][spec['id']]
            if old['scale_z']!=spec['scale_z']:
                feature=automation._find_feature(doc,old['feature'])
                definition=typed(com_get(feature,'GetDefinition'),'IScaleFeatureData')
                if not definition.AccessSelections(doc,None):
                    raise ProgramError('NATIVE_EDIT_FAILED','Cannot access ellipsoid aspect')
                try:
                    definition.ScaleZ=spec['scale_z']
                    if not typed(feature,'IFeature').ModifyDefinition(definition,doc,None):
                        raise ProgramError('NATIVE_EDIT_FAILED','Cannot modify ellipsoid aspect')
                finally: definition.ReleaseSelectionAccess()
                old['scale_z']=spec['scale_z']
                written+=1
        for axis,item in spec['placement'].items():
            target={'X':'D1','Y':'D2','Z':'D3'}[axis]+'@'+spec['id']
            written+=set_dimension_equation(eq,native,target,item['expression'])
    if rebuild_follows and not written and not updates:
        # Nothing changed in the preserved features: the caller's rebuild covers the batch.
        # Bodies restored by deleted features still need their current face counts.
        counts={str(com_get(b,'Name')):int(com_get(b,'GetFaceCount')) for b in automation._get_solid_bodies(doc)}
        for component in native['components'].values():
            component['face_count']=sum(counts.get(name,0) for name in component['bodies'])
        native['recipe_topology']=topology(specs)
        return {'updated_curve_count':0,'native_feature_ids_preserved':True,'rebuild_mode':'deferred_to_batch'}
    phase(timer,'evaluate')
    com_get(eq,'EvaluateAll')
    phase(timer,'rebuild')
    # The forced rebuild is the only one known to leave every dependent feature current
    # (see REBUILD_MODE). The opt-in path accepts a normal rebuild when it reports success,
    # nothing is pending, every equation curve matches the live dimensions and validation
    # passes; that is necessary but was measured not to be sufficient.
    mode='forced'
    if REBUILD_MODE=='verified':
        try:
            if (bool(com_get(doc,'EditRebuild3')) and int(com_get(doc.Extension,'NeedsRebuild2'))==0
                    and curves_current(automation,doc,native)):
                mode='verified'
        except Exception as exc:
            logger.info('Shape rebuild check failed, forcing: %s',exc)
    if mode=='forced':
        mode=forced_rebuild(automation,doc,eq,native,witness_curves(native,moved,rewritten))
    phase(timer,'validation')
    from .shape_placement import settle_placements

    def validated():
        settle_placements(doc,native.get('placement_features',{}))
        validate_anchors(automation,doc,native['components'])
        validate_feature_health(automation,doc,list(native['feature_refs'])+native['support_features'])
        for name,reference in native['feature_refs'].items():
            feature=typed(restore(doc,reference),'IFeature')
            if com_get(feature,'IsSuppressed'):
                raise ProgramError('NATIVE_REBUILD_FAILED','Edited feature is suppressed: '+name)
            # Consumed upstream features can legitimately own no remaining faces.
            has_booleans=any(c['kind'] in {'combine','surface_patch','place'} or c.get('native_profile')
                             for c in native['components'].values())
            if not has_booleans and com_get(feature,'GetTypeName2') not in {'MoveCopyBody','Scale'} and feature_face_count(feature)==0:
                raise ProgramError('NATIVE_REBUILD_FAILED','Edited feature has no faces: '+name)
        expected=sum(len(v['bodies']) for v in native['components'].values())
        found=automation._get_solid_bodies(doc)
        if validate and len(found)!=expected:
            raise ProgramError('NATIVE_REBUILD_FAILED','Body count changed during definition edit')
        if validate and {str(com_get(b,'Name')) for b in found}!={n for c in native['components'].values() for n in c['bodies']}:
            raise ProgramError('NATIVE_REBUILD_FAILED','Body ownership changed during definition edit')
        return found
    try:
        bodies=validated()
    except ProgramError:
        if mode!='verified':raise
        # The normal rebuild left something the checks reject: do the full one and judge that.
        phase(timer,'rebuild')
        mode='forced_after_check'
        if not bool(doc.ForceRebuild3(False)):
            raise rebuild_error(automation,doc,'Edited document did not rebuild',native.get('ownership'))
        phase(timer,'validation')
        bodies=validated()
    counts={str(com_get(b,'Name')):int(com_get(b,'GetFaceCount')) for b in bodies}
    for component in native['components'].values():
        component['face_count']=sum(counts.get(name,0) for name in component['bodies'])
    native['recipe_topology']=topology(specs)
    return {'updated_curve_count':updates,'native_feature_ids_preserved':True,'rebuild_mode':mode}
