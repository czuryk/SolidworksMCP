"""Curved host coverage for dome/ellipsoid and compound clipping masks."""
import argparse,json,logging,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import com_get,typed
from solidworks_mcp.automation.shape_parametric import read_record,validate_feature_health
from solidworks_mcp.automation.session_lease import SessionLease

PROGRAM='''
H=model.parameter('H',100)
Gap=model.parameter('Gap',.25)
ball=model.ellipsoid('Ball',(.15*H,.2*H,.1*H),center=(0,.2*H,0))
outer=model.rounded_prism('Outer',.12*H,.15*H,.1*H,.025*H,center=(0,.2*H,.1*H))
inner=model.rounded_prism('Inner',.08*H,.11*H,.12*H,.015*H,center=(0,.2*H,.1*H))
mask=model.combine('RingMask',[outer,inner],operation='difference')
model.surface_patch('BallRim',ball,mask,1,offset=Gap)
cap=model.dome('Cap',.15*H,.1*H,center=(.45*H,0,0))
mask2=model.rounded_prism('CapMask',.08*H,.06*H,.1*H,.01*H,center=(.45*H,.05*H,.11*H))
model.surface_patch('CapPatch',cap,mask2,1,offset=Gap)
'''

def acceptance(path):
    sw=SolidWorksAutomation();r=sw.execute_model_code(PROGRAM,mode='create',output_path=str(path),observe=False)
    assert r['success'],r
    doc=sw.app.ActiveDoc;record=read_record(doc)
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr');states=[]
    for h in [100,110,100]:
        eq.SetEquation(record['native']['parameter_bindings']['H']['global_index'],f'"H"={h}mm')
        com_get(eq,'EvaluateAll');assert doc.ForceRebuild3(False)
        validate_feature_health(sw,doc,list(record['native']['feature_refs'])+record['native']['support_features'])
        assert len(sw._get_solid_bodies(doc))==4
        gaps={}
        for host,patch in [('Ball','BallRim'),('Cap','CapPatch')]:
            r=sw.check_clearance(host,patch,unit='mm');assert r['success'],r
            assert abs(r['data']['min_distance']-.25)<.01,r
            gaps[host]=r['data']['min_distance']
        states.append({'H':h,'gaps':gaps})
    assert sw.save_document(str(path))['success']
    sw.app.CloseDoc(sw._get_doc_title(doc));assert sw.open_document(str(path))['success']
    assert sw.app.ActiveDoc.ForceRebuild3(False)
    observed=sw.observe_model(str(path.parent/'surface_curved_views'),views=['front','right','isometric'],width=850)
    assert observed['success'],observed
    return {'status':'NATIVE_CURVED_PATCH_PASS','states':states,'compound_mask':True,'reopen':True}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--report',required=True)
    args=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    with SessionLease():report=acceptance(Path(args.output).resolve())
    Path(args.report).write_text(json.dumps(report,indent=2),encoding='utf-8')
