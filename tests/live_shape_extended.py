"""Opt-in native extended-family acceptance; creates only an explicit new file."""
import argparse,json,logging,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import com_get,typed
from solidworks_mcp.automation.shape_parametric import read_record,validate_feature_health
from solidworks_mcp.automation.session_lease import SessionLease

PROGRAM='''
H=model.parameter('H',100)
model.ellipsoid('Oval',(.07*H,.1*H,.04*H),center=(.3*H,.15*H,.02*H))
model.variable_tube('Member',[(0,0,0),(.05*H,.2*H,.02*H),(0,.4*H,0)], [.03*H,.04*H,.02*H])
outer=model.tube('Outer',[(-.3*H,0,0),(-.3*H,.2*H,0)],.08*H)
tool=model.tube('Tool',[(-.3*H,-.01*H,0),(-.3*H,.21*H,0)],.04*H)
model.combine('Sleeve',[outer,tool],operation='difference')
'''

def masses(sw,doc):
    return {str(com_get(b,'Name')):sw._body_mass_props(b,'mm') for b in sw._get_solid_bodies(doc)}

def acceptance(path,existing=False):
    sw=SolidWorksAutomation()
    if existing:
        assert sw.connect()['success']
        assert sw.open_document(str(path))['success']
    else:
        r=sw.execute_model_code(PROGRAM,mode='create',output_path=str(path),observe=False)
        assert r['success'],r
    print('EXTENDED_CREATE_PASS',flush=True)
    doc=sw.app.ActiveDoc;sw.app.CloseDoc(sw._get_doc_title(doc))
    assert sw.open_document(str(path))['success'];doc=sw.app.ActiveDoc
    record=read_record(doc);refs=record['native']['feature_refs']
    expected={'Oval':4*math.pi*7*10*4/3,'Sleeve':math.pi*(8**2-4**2)*20}
    measured=[]
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    for h in [100,110,100]:
        eq.SetEquation(record['native']['parameter_bindings']['H']['global_index'],f'"H"={h}mm')
        com_get(eq,'EvaluateAll');assert doc.ForceRebuild3(False)
        validate_feature_health(sw,doc,list(refs)+record['native']['support_features'])
        values=masses(sw,doc);assert set(values)=={'Oval','Member','Sleeve'},values
        for name,v in expected.items():assert math.isclose(values[name]['volume_exact'],v*(h/100)**3,rel_tol=.001),(name,values[name],v)
        assert math.dist(values['Oval']['center_of_mass'],[.3*h,.15*h,.02*h])<.001
        measured.append({'H':h,'volumes':{k:v['volume_exact'] for k,v in values.items()}})
        print(measured[-1],flush=True)
    assert math.isclose(measured[1]['volumes']['Member']/measured[0]['volumes']['Member'],1.331,rel_tol=.002)
    assert sw.save_document(str(path))['success']
    revision=sw.get_model_context()['data']['revision']
    edited=PROGRAM.replace('.04*H),center','.05*H),center').replace('[.03*H,.04*H,.02*H]','[.03*H,.045*H,.02*H]').replace("],.04*H)\nmodel.combine", "],.045*H)\nmodel.combine").replace('(.05*H,.2*H,.02*H)','(.06*H,.2*H,.02*H)')
    result=sw.execute_model_code(edited,mode='edit',document_path=str(path),expected_revision=revision,observe=False)
    assert result['success'],result
    assert read_record(doc)['native']['feature_refs']==refs
    final=masses(sw,doc)
    assert final['Oval']['volume_exact']>measured[0]['volumes']['Oval']*1.2
    assert final['Sleeve']['volume_exact']<measured[0]['volumes']['Sleeve']
    assert final['Member']['volume_exact']>measured[0]['volumes']['Member']
    assert sw.save_document(str(path))['success']
    sw.app.CloseDoc(sw._get_doc_title(doc));assert sw.open_document(str(path))['success']
    doc=sw.app.ActiveDoc;assert doc.ForceRebuild3(False)
    validate_feature_health(sw,doc,list(refs)+record['native']['support_features'])
    reopened=masses(sw,doc)
    for name in final:assert math.isclose(final[name]['volume_exact'],reopened[name]['volume_exact'],rel_tol=.0001)
    assert sw.save_document(str(path))['success']
    observed=sw.observe_model(str(path.parent/'extended_views'),document_path=str(path),
                              expected_revision=sw.get_model_context()['data']['revision'])
    assert observed['success'],observed
    return {'status':'EXTENDED_NATIVE_FAMILIES_PASS','measured':measured,
            'definition_edit':result['data'],'final_volumes':{k:v['volume_exact'] for k,v in final.items()}}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--report',required=True)
    p.add_argument('--verify-existing',action='store_true')
    args=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    with SessionLease(): result=acceptance(Path(args.output).resolve(),args.verify_existing)
    Path(args.report).write_text(json.dumps(result,indent=2),encoding='utf-8')
