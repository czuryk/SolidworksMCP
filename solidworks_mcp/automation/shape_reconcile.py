"""Transactional native graph reconciliation; the service owns file rollback."""
import copy
import json
from ..shape_program import ProgramError
from ..shape_equations import plan_recipes,topology
from ..shape_reconcile import reconcile_plan
from .com_utils import com_get,typed
from .shape_parametric import (ParametricCompiler,feature_inventory,persist,restore,
                               update_native,validate_feature_health)
from .runtime import phase


def prepare_reconcile(record,program):
    native=record['native'];bindings=native['parameter_bindings']
    if set(bindings)-set(program['parameters']) or any(
            bindings[k]['unit']!=v['unit'] for k,v in program['parameters'].items() if k in bindings):
        raise ProgramError('PARAMETER_SCHEMA_CHANGE','Existing parameter names and units must remain; additions are allowed')
    dims={k:v['dimension'] for k,v in bindings.items()}
    added=[k for k in program['parameters'] if k not in bindings]
    block='ShapeParameters_'+added[0] if added else None
    dims.update({k:f'D{i+1}@{block}' for i,k in enumerate(added)})
    before=plan_recipes(record['program'],dims);after=plan_recipes(program,dims)
    plan=reconcile_plan(before,after)
    plan.update(added_parameters=added,parameter_block=block)
    if plan['remove'] or plan['build'] or added:
        if not native.get('ownership') or not native.get('feature_inventory'):
            raise ProgramError('TOPOLOGY_MAPPING_MISSING',
                'Topology edits require ownership metadata from compiler v3; this older part supports definition edits')
    return plan,before,after


def reduced_program(program,keep):
    result=copy.deepcopy(program)
    result['nodes']=[n for n in result['nodes'] if n['id'] in keep]
    result['source_graph']['nodes']=[n for n in result['source_graph']['nodes'] if n['id'] in keep]
    return result


def reindex_equations(doc,native):
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    names={eq.Equation(i).split('=')[0].strip().strip('"'):i for i in range(com_get(eq,'GetCount'))}
    for name,binding in native['parameter_bindings'].items():binding['global_index']=names[binding.get('native_name',name)]
    for name,binding in native['dimension_equations'].items():binding['index']=names[name]


def reconcile_native(automation,doc,record,program,timer=None):
    plan,before,after=prepare_reconcile(record,program)
    if not plan['remove'] and not plan['build'] and not plan['added_parameters']:
        return update_native(automation,doc,record,program,timer=timer)
    native=record['native']
    phase(timer,'validation')
    current=feature_inventory(automation,doc)
    if current!=native['feature_inventory']:
        raise ProgramError('EXTERNAL_FEATURE_CHANGE',
            'Feature inventory changed outside the embedded program; automatic topology rebase is unavailable',
            added=sorted(current.keys()-native['feature_inventory'].keys()),
            removed=sorted(native['feature_inventory'].keys()-current.keys()))
    # Validate definitions before deletion. Do not silently discard external edits.
    from .shape_ellipsoid import validate_guided_profiles
    validate_guided_profiles(automation,doc,native['components'])
    from .shape_anchor import validate_anchors
    validate_anchors(automation,doc,native['components'])
    for curve in native['curves']:
        obj=typed(restore(doc,curve['persistent_id']),'ISketchSpline')
        params=obj.GetEquationParameters2()
        expected=curve['xyz'][:2]+([curve['xyz'][2]] if curve['mode']=='3d' else [''])
        if not params[0] or list(params[1:4])!=expected:
            raise ProgramError('NATIVE_MAPPING_STALE','A native curve changed outside the program: '+curve['sketch'])
    for reference in native['feature_refs'].values():restore(doc,reference)
    phase(timer,'native_update')
    if plan['added_parameters']:
        creator=ParametricCompiler(automation,doc)
        creator.parameters({'parameters':{k:program['parameters'][k] for k in plan['added_parameters']}},
                           sketch_name=plan['parameter_block'])
        native['parameter_bindings'].update(creator.bindings)
        native['dimension_equations'].update(creator.dimension_equations)
        native['support_features'].extend(creator.support)
    removed_features=set();removed_support=set()
    for name in plan['remove']:
        owned=native['ownership'][name]
        removed_features.update(owned['features']);removed_support.update(owned['support'])
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    discarded=[k for k in native['dimension_equations'] if k.split('@')[-1] in removed_features|removed_support]
    for key in sorted(discarded,key=lambda k:native['dimension_equations'][k]['index'],reverse=True):
        if eq.Delete(native['dimension_equations'][key]['index'])<0:
            raise ProgramError('NATIVE_DELETE_FAILED','Cannot remove owned dimension equation: '+key)
        del native['dimension_equations'][key]
    for name in plan['remove']:
        owned=native['ownership'][name]
        for feature_name in list(reversed(owned['features']))+list(reversed(owned['support'])):
            feature=automation._find_feature(doc,feature_name)
            if feature is None:continue
            doc.ClearSelection2(True)
            if not feature.Select2(False,0) or not typed(doc.Extension,'IModelDocExtension').DeleteSelection2(0):
                raise ProgramError('NATIVE_DELETE_FAILED','Cannot delete owned feature: '+feature_name)
    for name in removed_features|removed_support:
        if automation._find_feature(doc,name) is not None:
            raise ProgramError('NATIVE_DELETE_FAILED','Obsolete feature remained: '+name)
    preserved=set(plan['preserve'])
    native['feature_refs']={k:v for k,v in native['feature_refs'].items() if k not in removed_features}
    native['support_features']=[k for k in native['support_features'] if k not in removed_support]
    native['curves']=[c for c in native['curves'] if c['sketch'] not in removed_support]
    for key in ('components','ownership','scale_features','placement_features'):
        native[key]={k:v for k,v in native.get(key,{}).items() if k in preserved}
    reindex_equations(doc,native)
    # Deleted Combine features restore upstream native bodies. Resolve original
    # persistent body identities before assigning semantic names again.
    consumers={seed:s['id'] for s in after if s['id'] in preserved
               for seed in (s['seeds'] if s['kind']=='combine' else s.get('consumes',[]))}
    actual={persist(doc,b) for b in automation._get_solid_bodies(doc)}
    found=set()
    for name,component in native['components'].items():
        component['bodies']=[];component.pop('consumed_by',None)
        if name in consumers:
            component['consumed_by']=consumers[name];component['face_count']=0
            continue
        for output in native['ownership'][name]['outputs']:
            body=typed(restore(doc,output['persistent_id']),'IBody2')
            ref=persist(doc,body)
            if ref not in actual or ref in found:
                raise ProgramError('NATIVE_BODY_MAPPING_STALE','Cannot restore unique body ownership: '+name)
            found.add(ref);body.Name=output['name'];component['bodies'].append(output['name'])
    if found!=actual:
        raise ProgramError('NATIVE_BODY_MAPPING_STALE','Reconciliation left unowned solid bodies')
    subset=reduced_program(program,preserved)
    native['recipe_topology']=topology([s for s in after if s['id'] in preserved])
    # Existing definitions may change together with topology. They still update
    # in place before the dependent replacement features are created.
    edits=update_native(automation,doc,record,subset,timer=timer,rebuild_follows=True)
    phase(timer,'native_update')
    compiler=ParametricCompiler(automation,doc)
    for attribute,key in [('components','components'),('support','support_features'),
                          ('bindings','parameter_bindings'),('curves','curves'),
                          ('dimension_equations','dimension_equations'),('feature_refs','feature_refs'),
                          ('scale_features','scale_features'),('placement_features','placement_features'),('ownership','ownership')]:
        setattr(compiler,attribute,native[key])
    compiler.spec_index={spec['id']:spec for spec in after}
    for spec in after:
        if spec['id'] in plan['build']:compiler.build(spec)
    updated=compiler.finish(program,after,timer=timer)
    for key in ('document','program_hash'):updated[key]=native.get(key)
    record['native']=updated
    validate_feature_health(automation,doc,list(updated['feature_refs'])+updated['support_features'])
    # Every untouched feature must retain its persistent ID.
    for name,reference in native['feature_refs'].items():
        if persist(doc,automation._find_feature(doc,name))!=reference:
            raise ProgramError('NATIVE_MAPPING_STALE','Preserved feature identity changed: '+name)
    edits.pop('rebuild_mode',None)
    return {**edits,'native_feature_ids_preserved':not plan['remove'],
            'preserved_components':plan['preserve'],'rebuilt_components':plan['build'],
            'removed_components':plan['deleted'],'added_components':plan['added'],
            'added_parameters':plan['added_parameters'],
            'obsolete_features_removed':sorted(removed_features|removed_support),
            'edit_strategy':'dependency_reconcile'}
