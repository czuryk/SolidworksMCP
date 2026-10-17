"""Native independent clearance and ratio parameters across save/reopen."""
import argparse,json,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import com_get,typed
from solidworks_mcp.automation.shape_parametric import read_record,validate_feature_health
from solidworks_mcp.automation.session_lease import SessionLease
PROGRAM='''
H=model.parameter('H',150)
Gap=model.parameter('Gap',.25)
Ratio=model.parameter('Ratio',.03,unit='ratio')
r=H*Ratio
model.tube('PinA',[(0,0,0),(0,H/5,0)],r)
model.tube('PinB',[(2*r+Gap,0,0),(2*r+Gap,H/5,0)],r)
model.revolve('Vessel',[(0,H/20),(H/10,H/20),(H/8,H/30)],center_xz=(-H/5,H/10))
'''


def acceptance(path):
    sw=SolidWorksAutomation()
    result=sw.execute_model_code(PROGRAM,mode='create',output_path=str(path),observe=False)
    assert result['success'],result
    doc=sw.app.ActiveDoc;sw.app.CloseDoc(sw._get_doc_title(doc))
    assert sw.open_document(str(path))['success'];doc=sw.app.ActiveDoc
    record=read_record(doc);eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    measured=[]
    for h in [150,165,150]:
        eq.SetEquation(record['native']['parameter_bindings']['H']['global_index'],f'"H"={h}mm')
        com_get(eq,'EvaluateAll');assert doc.ForceRebuild3(False)
        validate_feature_health(sw,doc,list(record['native']['feature_refs'])+record['native']['support_features'])
        value=sw.check_clearance('PinA','PinB',unit='mm')
        assert value['success'],value
        d=value['data'];assert d['measure_ok'] and abs(d['min_distance']-.25)<.01,d
        assert not d['interference'],d
        props=sw._body_mass_props(sw._find_body(doc,'Vessel'),'mm')
        assert math.dist([props['center_of_mass'][0],props['center_of_mass'][2]],[-h/5,h/10])<.001,props
        measured.append({'H':h,'gap_mm':d['min_distance'],'placed_center_xz':[props['center_of_mass'][0],props['center_of_mass'][2]]})
        print(measured[-1],flush=True)
    assert sw.save_document(str(path))['success']
    sw.app.CloseDoc(sw._get_doc_title(doc));assert sw.open_document(str(path))['success']
    return {'status':'NATIVE_CONSTANT_CLEARANCE_PASS','measured':measured,'ratio_parameter_verified':True,'negative_placement_verified':True}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--report',required=True);args=p.parse_args()
    with SessionLease():report=acceptance(Path(args.output).resolve())
    Path(args.report).write_text(json.dumps(report,indent=2),encoding='utf-8')
