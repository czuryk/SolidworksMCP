"""Parameter growth, deletion of dimensional features, full replacement and external-edit guards."""
import argparse,hashlib,json,logging,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import com_get,typed
from solidworks_mcp.automation.shape_parametric import read_record,validate_feature_health
from solidworks_mcp.automation.session_lease import SessionLease

BASE='''
H=model.parameter('H',100)
model.tube('Pin',[(0,0,0),(0,H/5,0)],H/25)
model.rounded_prism('Box',H/10,H/8,H/20,H/100,center=(H/3,0,H/10))
'''
GROW='''
H=model.parameter('H',100)
Gap=model.parameter('Gap',.25)
model.tube('Pin',[(0,0,0),(0,H/5,0)],H/25)
model.tube('Mate',[(2*H/25+Gap,0,0),(2*H/25+Gap,H/5,0)],H/25)
'''

def acceptance(path):
    sw=SolidWorksAutomation();r=sw.execute_model_code(BASE,mode='create',output_path=str(path),observe=False)
    assert r['success'],r
    doc=sw.app.ActiveDoc;original=read_record(doc);pin=original['native']['feature_refs']['Pin']
    def edit(code):
        r=sw.execute_model_code(code,mode='edit',document_path=str(path),
            expected_revision=sw.get_model_context()['data']['revision'],observe=False)
        assert r['success'],r
        print(r['data'],flush=True);return r['data']
    grown=edit(GROW)
    assert grown['added_parameters']==['Gap']
    assert read_record(doc)['native']['feature_refs']['Pin']==pin
    assert all(sw._find_feature(doc,n) is None for n in ['Box','Box_Volume','Box_Profile'])
    record=read_record(doc);eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    equations=[eq.Equation(i) for i in range(com_get(eq,'GetCount'))]
    assert not any('@Box' in e for e in equations),equations
    measured=[]
    for h in [100,110,100]:
        eq.SetEquation(record['native']['parameter_bindings']['H']['global_index'],f'"H"={h}mm')
        com_get(eq,'EvaluateAll');assert doc.ForceRebuild3(False)
        validate_feature_health(sw,doc,list(record['native']['feature_refs'])+record['native']['support_features'])
        r=sw.check_clearance('Pin','Mate',unit='mm');assert r['success'],r
        assert abs(r['data']['min_distance']-.25)<.01,r
        measured.append({'H':h,'gap_mm':r['data']['min_distance']})
    assert sw.save_document(str(path))['success']
    reordered='\n'.join(GROW.strip().splitlines()[:2]+list(reversed(GROW.strip().splitlines()[2:])))
    edit(reordered)
    doc.SketchManager.Insert3DSketch(True)
    assert doc.SketchManager.CreateLine(0,0,0,.01,0,0) is not None
    doc.SketchManager.Insert3DSketch(True)
    extra=com_get(doc,'FeatureByPositionReverse',0);extra.Name='UserGeometry'
    assert sw.save_document(str(path))['success'];before=path.read_bytes()
    rejected=sw.execute_model_code(GROW+"\nmodel.dome('Extra',2,1)",mode='edit',document_path=str(path),
        expected_revision=sw.get_model_context()['data']['revision'],observe=False)
    assert not rejected['success'] and rejected['data']['error']['code']=='EXTERNAL_FEATURE_CHANGE',rejected
    assert path.read_bytes()==before
    doc=sw.app.ActiveDoc;extra=sw._find_feature(doc,'UserGeometry')
    assert extra is not None and extra.Select2(False,0)
    assert typed(doc.Extension,'IModelDocExtension').DeleteSelection2(0)
    assert sw.save_document(str(path))['success']
    replaced=edit("H=model.parameter('H',100)\nGap=model.parameter('Gap',.25)\nmodel.ellipsoid('Replacement',(H/10,H/8,H/12))")
    assert set(read_record(doc)['native']['components'])=={'Replacement'}
    assert len(sw._get_solid_bodies(doc))==1
    sw.app.CloseDoc(sw._get_doc_title(doc));assert sw.open_document(str(path))['success']
    doc=sw.app.ActiveDoc;assert doc.ForceRebuild3(False);assert sw.save_document(str(path))['success']
    return {'status':'NATIVE_RECONCILE_PARAMETER_GROWTH_PASS','constant_gap':measured,
            'old_dimension_equations_removed':True,'independent_reorder_preserved':True,
            'external_feature_preserved':True,'full_replacement':replaced,'append':grown}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--report',required=True)
    args=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    with SessionLease():r=acceptance(Path(args.output).resolve())
    Path(args.report).write_text(json.dumps(r,indent=2),encoding='utf-8')
