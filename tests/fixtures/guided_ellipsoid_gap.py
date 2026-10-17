"""Verify constant clearance against actual curved faces after native rebuild."""
import json
from pathlib import Path
from solidworks_mcp.automation.com_utils import com_get, typed, command_scope
from solidworks_mcp.automation.shape_parametric import read_record, persist, validate_feature_health
target='__OUTPUT_DIRECTORY__/Guided_A.SLDPRT'
app=typed(sw,'ISldWorks')
assert str(com_get(doc,'GetPathName')).replace('\\','/')==target
app.CloseDoc(com_get(doc,'GetTitle'))
assert automation.open_document(target)['success']
doc=app.ActiveDoc
record=read_record(doc)
refs=record['native']['feature_refs']
eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
report={'status':'IN_PROGRESS','cycles':[]}
path=Path('__OUTPUT_DIRECTORY__/gap_verification.json')
with command_scope(app):
 for radii in [(12,17,8),(8,17,12),(10,20,6)]:
    for name,value in zip(['Rx','Ry','Rz'],radii):
        eq.SetEquation(record['native']['parameter_bindings'][name]['global_index'],f'"{name}"={value}mm')
    com_get(eq,'EvaluateAll')
    assert doc.ForceRebuild3(False)
    validate_feature_health(automation,doc,list(refs)+record['native']['support_features'])
    result=automation.check_clearance('Oval','Skin',unit='mm')
    assert result['success'],result
    gap=result['data']['min_distance']
    row={'radii_mm':radii,'actual_min_distance_mm':gap,
         'same_feature_ids':all(persist(doc,automation._find_feature(doc,n))==ref for n,ref in refs.items())}
    report['cycles'].append(row);path.write_text(json.dumps(report,indent=2));print(json.dumps(row))
    assert abs(gap-.25)<.005 and row['same_feature_ids'],row
 assert automation.save_document(target)['success']
report['status']='NATIVE_ACTUAL_SURFACE_GAP_PASS'
path.write_text(json.dumps(report,indent=2))
