"""Opt-in persistence, native rebuild and transactional edit acceptance."""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import com_get,typed
from solidworks_mcp.automation.shape_parametric import read_record,restore,update_native,validate_feature_health
from solidworks_mcp.automation.session_lease import SessionLease


def masses(sw,doc):
    return {str(com_get(b,'Name')):sw._body_mass_props(b,'mm')['volume_exact']
            for b in sw._get_solid_bodies(doc)}


def native_h(sw,doc,value):
    record=read_record(doc)
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    eq.SetEquation(record['native']['parameter_bindings']['H']['global_index'],f'"H" = {value}mm')
    com_get(eq,'EvaluateAll')
    assert doc.ForceRebuild3(False),'Native rebuild failed'
    validate_feature_health(sw,doc,list(record['native']['feature_refs'])+record['native']['support_features'])
    return masses(sw,doc)


def acceptance(path):
    sw=SolidWorksAutomation();assert sw.connect()['success']
    existing=sw.app.GetOpenDocumentByName(str(path))
    if existing is not None:sw.app.CloseDoc(sw._get_doc_title(existing))
    assert sw.open_document(str(path))['success']
    doc=sw.app.ActiveDoc;record=read_record(doc)
    references=record['native']['feature_refs']
    for ref in references.values(): assert restore(doc,ref) is not None
    initial=masses(sw,doc);assert len(initial)==5
    larger=native_h(sw,doc,165)
    ratios={k:larger[k]/v for k,v in initial.items()}
    assert all(math.isclose(v,1.331,rel_tol=.001) for v in ratios.values()),ratios
    restored=native_h(sw,doc,150)
    assert all(math.isclose(restored[k],v,rel_tol=.0001) for k,v in initial.items())
    assert sw.save_document(str(path))['success']
    print('NATIVE_H_REBUILD_PASS',ratios,flush=True)
    revision=sw.get_model_context()['data']['revision']
    edited=sw.execute_model_code(mode='edit',document_path=str(path),
        expected_revision=revision,parameters={'H':165},observe=False)
    assert edited['success'],edited
    assert edited['data']['updated_curve_count']==0,edited
    assert read_record(doc)['native']['feature_refs']==references
    stale=sw.execute_model_code(mode='edit',document_path=str(path),
        expected_revision=revision,parameters={'H':150},observe=False)
    assert not stale['success'] and stale['data']['error']['code']=='STALE_REVISION',stale
    print('PARAMETER_EDIT_PASS',flush=True)
    current=sw.get_model_context()['data']['revision']
    code=record['source'].replace('.097*H','.102*H').replace('x+.03*H','x+.035*H')
    geometry_edit=sw.execute_model_code(code,mode='edit',document_path=str(path),
        expected_revision=current,parameters={'H':150},observe=False)
    assert geometry_edit['success'],geometry_edit
    assert geometry_edit['data']['updated_curve_count']==4,geometry_edit
    assert masses(sw,doc)['Torso']>initial['Torso']
    assert not math.isclose(masses(sw,doc)['Leg_Left'],initial['Leg_Left'],rel_tol=.001)
    assert read_record(doc)['native']['feature_refs']==references
    print('DEFINITION_EDIT_PASS',flush=True)
    before=path.read_bytes();context=sw.get_model_context()['data']
    def fail_after_mutation(*args):
        update_native(*args)
        raise RuntimeError('Injected fault after real native mutation')
    with patch('solidworks_mcp.automation.shape_service.update_native',side_effect=fail_after_mutation):
        failed=sw.execute_model_code(mode='edit',document_path=str(path),
            expected_revision=context['revision'],parameters={'H':160},observe=False)
    assert not failed['success'] and failed['data']['error']['document_restored'],failed
    assert path.read_bytes()==before,'Rollback changed saved file bytes'
    doc=sw.app.ActiveDoc
    assert read_record(doc)['native']['feature_refs']==references
    for ref in references.values():assert restore(doc,ref) is not None
    print('ROLLBACK_AND_REOPEN_PASS',flush=True)
    context=sw.get_model_context()['data']
    topology_edit=sw.execute_model_code(code+'\nmodel.dome("Extra",4,2)',mode='edit',
        document_path=str(path),expected_revision=context['revision'],observe=False)
    if record['native'].get('compiler_version',0)>=3:
        assert topology_edit['success'] and topology_edit['data']['added_components']==['Extra'],topology_edit
        assert read_record(doc)['native']['feature_refs']['Torso']==references['Torso']
        context=sw.get_model_context()['data']
    else:
        assert not topology_edit['success'] and topology_edit['data']['error']['code']=='TOPOLOGY_MAPPING_MISSING',topology_edit
    final=sw.execute_model_code(record['source'],mode='edit',document_path=str(path),
        expected_revision=context['revision'],parameters={'H':150},observe=False)
    assert final['success'],final
    sw.app.CloseDoc(sw._get_doc_title(doc));assert sw.open_document(str(path))['success']
    doc=sw.app.ActiveDoc;assert doc.ForceRebuild3(False)
    final_masses=masses(sw,doc)
    assert all(math.isclose(final_masses[k],v,rel_tol=.0001) for k,v in initial.items())
    return {'status':'NATIVE_REBUILD_EDIT_ROLLBACK_PASS','scale_volume_ratios':ratios,
            'native_feature_ids_preserved':True,'definition_curves_updated':4,
            'byte_exact_rollback_verified':True,'stale_revision_rejected':True,
            'topology_edit_supported':record['native'].get('compiler_version',0)>=3,'final_reopen_verified':True}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--document',required=True);p.add_argument('--report',required=True)
    args=p.parse_args()
    with SessionLease():report=acceptance(Path(args.document).resolve())
    Path(args.report).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
