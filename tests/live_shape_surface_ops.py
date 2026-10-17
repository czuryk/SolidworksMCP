"""Surface patch booleans, inward direction, zero offset and host replacement."""
import argparse,hashlib,json,logging,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import com_get,typed
from solidworks_mcp.automation.shape_parametric import read_record,validate_feature_health
from solidworks_mcp.automation.session_lease import SessionLease

BASE='''
H=model.parameter('H',100)
Depth=model.parameter('Depth',.8)
host=model.revolve('Host',[(0,.2*H),(.5*H,.2*H)])
p=model.surface_point(host,fraction=.5)
mask=model.rounded_prism('Mask',.14*H,.2*H,.15*H,.025*H,center=p)
model.dome('Witness',4,2,center=(-35,0,0))
'''
CUT="patch=model.surface_patch('Patch',host,mask,Depth,offset=0,inward=True)\nmodel.combine('Result',[host,patch],operation='difference')"
EMBOSS="patch=model.surface_patch('Patch',host,mask,Depth,offset=-Depth/4)\nmodel.combine('Result',[host,patch],operation='union')"

def acceptance(path):
    sw=SolidWorksAutomation();r=sw.execute_model_code(BASE+CUT,mode='create',output_path=str(path),observe=False)
    assert r['success'],r
    doc=sw.app.ActiveDoc;original=read_record(doc);witness=original['native']['feature_refs']['Witness']
    def volume():
        r=sw.body_volume('Result',unit='mm');assert r['success'],r
        return r['data']['bodies'][0]['volume_exact']
    import math
    base_volume=math.pi*20**2*50
    cut_volume=volume();assert 0<cut_volume<base_volume
    def edit(code):
        r=sw.execute_model_code(code,mode='edit',document_path=str(path),
            expected_revision=sw.get_model_context()['data']['revision'],observe=False)
        assert r['success'],r
        assert read_record(sw.app.ActiveDoc)['native']['feature_refs']['Witness']==witness
        return r['data']
    raised=edit(BASE+EMBOSS);emboss_volume=volume();assert emboss_volume>base_volume
    replaced=edit(BASE.replace("model.revolve('Host',[(0,.2*H),(.5*H,.2*H)])",
        "model.sectioned_solid('Host',[(0,.2*H,.14*H),(.5*H,.2*H,.14*H)])")+EMBOSS)
    assert set(replaced['rebuilt_components'])=={'Host','Patch','Result'},replaced
    doc=sw.app.ActiveDoc;record=read_record(doc)
    assert len(sw._get_solid_bodies(doc))==2
    validate_feature_health(sw,doc,list(record['native']['feature_refs'])+record['native']['support_features'])
    before=path.read_bytes()
    bad=sw.execute_model_code(record['source'].replace('center=p','center=(0,250,0)'),
        mode='edit',document_path=str(path),expected_revision=sw.get_model_context()['data']['revision'],observe=False)
    assert not bad['success'],bad
    assert path.read_bytes()==before
    doc=sw.app.ActiveDoc;assert len(sw._get_solid_bodies(doc))==2
    sw.app.CloseDoc(sw._get_doc_title(doc));assert sw.open_document(str(path))['success']
    doc=sw.app.ActiveDoc;record=read_record(doc)
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    states=[]
    for h in [100,110,100]:
        eq.SetEquation(record['native']['parameter_bindings']['H']['global_index'],f'"H"={h}mm')
        com_get(eq,'EvaluateAll');assert doc.ForceRebuild3(False)
        validate_feature_health(sw,doc,list(record['native']['feature_refs'])+record['native']['support_features'])
        assert len(sw._get_solid_bodies(doc))==2
        states.append({'H':h,'volume':volume()})
    assert sw.save_document(str(path))['success']
    observed=sw.observe_model(str(path.parent/'surface_ops_views'),views=['isometric'],width=800)
    assert observed['success'],observed
    return {'status':'NATIVE_SURFACE_BOOLEAN_PASS','cylinder_volume':base_volume,
            'cut_volume':cut_volume,'emboss_volume':emboss_volume,'emboss_edit':raised,
            'host_family_replacement':replaced,'failed_patch_byte_exact_rollback':True,
            'reopened_native_H':states}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--report',required=True)
    args=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    with SessionLease():report=acceptance(Path(args.output).resolve())
    Path(args.report).write_text(json.dumps(report,indent=2),encoding='utf-8')
