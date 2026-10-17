"""Native rigid placement with equation-driven angles around fixed axes."""
import math

from ..shape_program import ProgramError
from .com_utils import com_get, typed, create_select_data


def build_placement(compiler, spec):
    from .shape_parametric import persist, short_dimension
    c=compiler;doc=c.doc;name=spec['id'];c.stage='parametric_placement'
    groups=[c.components[s]['bodies'] for s in spec['seeds']]
    c.require(all(groups),'Placement input was already consumed')
    names=[n for group in groups for n in group]
    c.require(len(names)==len(set(names)),'Placement bodies overlap in scope')
    untouched=c.body_names()-set(names)
    axes={};axis_records={};steps=spec['transform']
    rotations=[s for s in steps if s['kind']=='rotate']
    if rotations:
        doc.ClearSelection2(True);sm=doc.SketchManager;sm.Insert3DSketch(True)
        old_add=sm.AddToDB
        try:
            sm.AddToDB=True
            for step in rotations:
                axis=step['axis'];end=[0.,0.,0.];end['XYZ'.index(axis)]=step['direction']*.01
                line=sm.CreateLine(0,0,0,*end)
                c.require(line is not None,'Cannot create placement axis')
                typed(line,'ISketchSegment').ConstructionGeometry=True
                doc.ClearSelection2(True)
                c.require(bool(line.Select4(False,create_select_data(doc,0))),'Cannot select placement axis')
                com_get(doc,'SketchAddConstraints','sgFIXED')
                axes[axis]=line
                axis_records[axis]={'persistent_id':persist(doc,line),'end':end}
        finally:sm.AddToDB=old_add
        c.finish_sketch(name+'_Axes',True)
    records=[]
    for step in steps:
        bodies=[b for b in c.bodies() if str(com_get(b,'Name')) not in untouched]
        c.require(len(bodies)==len(names),'Placement changed body count')
        doc.ClearSelection2(True)
        for i,body in enumerate(bodies):
            c.require(bool(typed(body,'IBody2').Select2(i>0,create_select_data(doc,1))),
                      'Cannot select placement body')
        manager=typed(doc.FeatureManager,'IFeatureManager')
        if step['kind']=='rotate':
            c.require(bool(axes[step['axis']].Select4(True,create_select_data(doc,2))),
                      'Cannot select rotation axis')
            feature=manager.InsertMoveCopyBody2(0,0,0,0,0,0,0,step['value'],0,0,False,1)
        else:
            xyz=[step['axes'].get(axis,{}).get('value',0)*.001 for axis in 'XYZ']
            feature=manager.InsertMoveCopyBody2(*xyz,0,0,0,0,0,0,0,False,1)
        c.require(feature is not None,'Native placement failed')
        feature.Name=step['feature'];targets={}
        if step['kind']=='rotate':
            display=com_get(feature,'GetFirstDisplayDimension')
            c.require(display is not None,'Native rotation has no driving dimension','NATIVE_ANGLE_UNAVAILABLE')
            target=short_dimension(com_get(display,'GetDimension2',0))
            c.add_equation(target,step['expression']);targets['angle']=target
        else:
            for axis,item in step['axes'].items():
                target=f'D{"XYZ".index(axis)+1}@'+step['feature']
                c.add_equation(target,item['expression']);targets[axis]=target
        c.feature_refs[step['feature']]=persist(doc,feature)
        records.append({'feature':step['feature'],'kind':step['kind'],'targets':targets})
    bodies=c.bodies()
    c.require(untouched.issubset({str(com_get(b,'Name')) for b in bodies}),
              'Placement modified a body outside its scope')
    result=[b for b in bodies if str(com_get(b,'Name')) not in untouched]
    c.require(len(result)==len(names),'Placement did not preserve its solid bodies')
    outputs=[]
    for i,body in enumerate(result):
        c.require(int(com_get(body,'GetFaceCount'))>0,'Placement produced an empty body')
        body.Name=name if len(result)==1 else f'{name}_{i+1}'
        outputs.append(str(com_get(body,'Name')))
    for seed in spec['seeds']:
        c.components[seed].update(bodies=[],face_count=0,consumed_by=name)
    c.components[name]={'feature':name,'bodies':outputs,'dependencies':spec['seeds'],
                        'face_count':sum(int(com_get(b,'GetFaceCount')) for b in result)}
    c.placement_features[name]={'steps':records,'axes':axis_records}
    doc.ClearSelection2(True)


def validate_placement_axes(doc, placements):
    from .shape_parametric import restore
    for placement in placements.values():
        for axis in placement['axes'].values():
            line=typed(restore(doc,axis['persistent_id']),'ISketchLine')
            for member,expected in [('GetStartPoint2',[0,0,0]),('GetEndPoint2',axis['end'])]:
                point=com_get(line,member)
                actual=[float(com_get(point,k)) for k in 'XYZ']
                if math.dist(actual,expected)>1e-9:
                    raise ProgramError('NATIVE_MAPPING_STALE','A placement axis was modified outside the embedded program')


def update_placement_equations(eq, native, spec):
    """Returns how many native equations had to be rewritten."""
    from .shape_parametric import set_dimension_equation
    record=native['placement_features'][spec['id']]
    written=0
    for step,stored in zip(spec['transform'],record['steps']):
        for key,target in stored['targets'].items():
            expression=step['expression'] if key=='angle' else step['axes'][key]['expression']
            written+=set_dimension_equation(eq,native,target,expression)
    return written


def pending_placements(doc, placements):
    """Read dimension versus feature state; never rebuild from a read-only tool."""
    pending=[]
    for placement in placements.values():
        for step in placement['steps']:
            feature=doc.FeatureByName(step['feature'])
            if feature is None:
                raise ProgramError('NATIVE_MAPPING_STALE','Missing placement feature: '+step['feature'])
            definition=typed(com_get(feature,'GetDefinition'),'IMoveCopyBodyFeatureData')
            for axis,target in step['targets'].items():
                dimension=doc.Parameter(target)
                if dimension is None:
                    raise ProgramError('NATIVE_MAPPING_STALE','Missing placement dimension: '+target)
                expected=float(com_get(dimension,'SystemValue'))
                member='TransformX' if axis=='angle' else 'Transform'+axis
                actual=abs(float(getattr(definition,member)))
                if not math.isfinite(expected+actual) or abs(actual-expected)>1e-8:
                    pending.append({'feature':step['feature'],'dimension':target,
                                    'dimension_value_si':expected,'feature_value_si':actual})
    return pending


def require_placement_ready(doc, placements):
    pending=pending_placements(doc,placements)
    if pending:
        raise ProgramError('NATIVE_REBUILD_PENDING',
            'Placement dimensions and native transforms differ; perform a full native rebuild before observation or export',
            pending=pending)


def settle_placements(doc, placements):
    """One bounded native-only second pass for SW's deferred edge-angle update."""
    if not pending_placements(doc,placements):return 0
    com_get(typed(com_get(doc,'GetEquationMgr'),'IEquationMgr'),'EvaluateAll')
    if not doc.ForceRebuild3(False):
        raise ProgramError('NATIVE_REBUILD_FAILED','Placement settling rebuild failed')
    require_placement_ready(doc,placements)
    return 1
