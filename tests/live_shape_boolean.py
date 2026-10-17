"""Native rounded prisms, explicit Boolean scope, and failed dependency edit rollback."""
import argparse,hashlib,json,logging,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import com_get,typed
from solidworks_mcp.automation.shape_parametric import read_record,validate_feature_health
from solidworks_mcp.automation.session_lease import SessionLease

PROGRAM='''
H=model.parameter('H',100)
def block(name,x):
    return model.rounded_prism(name,.1*H,.08*H,.06*H,.01*H,center=(x,0,.08*H))
a=block('UnionA',.28*H)
b=block('UnionB',.32*H)
model.combine('Union',[a,b])
c=block('CommonA',-.32*H)
d=block('CommonB',-.28*H)
model.combine('Common',[c,d],operation='intersection')
model.ellipsoid('Witness',(.05*H,.05*H,.05*H),center=(0,.2*H,0))
'''

def values(sw,doc):
    return {str(com_get(b,'Name')):sw._body_mass_props(b,'mm') for b in sw._get_solid_bodies(doc)}

def acceptance(path):
    sw=SolidWorksAutomation()
    r=sw.execute_model_code(PROGRAM,mode='create',output_path=str(path),observe=False)
    assert r['success'],r
    print('BOOLEAN_CREATE_PASS',flush=True)
    doc=sw.app.ActiveDoc;sw.app.CloseDoc(sw._get_doc_title(doc))
    assert sw.open_document(str(path))['success'];doc=sw.app.ActiveDoc
    record=read_record(doc);refs=record['native']['feature_refs']
    expected={'Union':(14*8-(4-math.pi))*6,'Common':(6*8-(4-math.pi))*6,'Witness':4*math.pi*5**3/3}
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr');measured=[]
    for h in [100,110,100]:
        eq.SetEquation(record['native']['parameter_bindings']['H']['global_index'],f'"H"={h}mm')
        com_get(eq,'EvaluateAll');assert doc.ForceRebuild3(False)
        validate_feature_health(sw,doc,list(refs)+record['native']['support_features'])
        props=values(sw,doc);assert set(props)==set(expected),props
        for name,v in expected.items():assert math.isclose(props[name]['volume_exact'],v*(h/100)**3,rel_tol=.001),(name,props[name],v)
        assert math.dist(props['Union']['center_of_mass'],[.3*h,0,.08*h])<.001
        assert math.dist(props['Common']['center_of_mass'],[-.3*h,0,.08*h])<.001
        measured.append({'H':h,'volumes':{k:p['volume_exact'] for k,p in props.items()}})
        print(measured[-1],flush=True)
    assert sw.save_document(str(path))['success']
    original=path.read_bytes();revision=sw.get_model_context()['data']['revision']
    broken=PROGRAM.replace("'UnionB',.32*H","'UnionB',.5*H")
    failed=sw.execute_model_code(broken,mode='edit',document_path=str(path),expected_revision=revision,observe=False)
    assert not failed['success'] and failed['data']['error']['document_restored'],failed
    assert path.read_bytes()==original
    doc=sw.app.ActiveDoc
    assert read_record(doc)['native']['feature_refs']==refs
    assert sw.observe_model(str(path.parent/'boolean_views'))['success']
    return {'status':'EXPLICIT_BOOLEAN_SCOPE_ROLLBACK_PASS','measured':measured,
            'unrelated_body_preserved':True,'disjoint_union_edit_rolled_back':True,
            'checkpoint_sha256':hashlib.sha256(original).hexdigest()}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--report',required=True)
    args=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    with SessionLease(): result=acceptance(Path(args.output).resolve())
    Path(args.report).write_text(json.dumps(result,indent=2),encoding='utf-8')
