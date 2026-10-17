"""Native surface-following patch, clearances, anchors and dependency edits."""
import argparse,json,logging,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import com_get,typed
from solidworks_mcp.automation.shape_parametric import read_record,validate_feature_health
from solidworks_mcp.automation.session_lease import SessionLease

BASE='''
H=model.parameter('H',100)
Gap=model.parameter('Gap',.25)
Wall=model.parameter('Wall',2)
host=model.sectioned_solid('Host',[(0,.18*H,.13*H),(.5*H,.2*H,.15*H)])
p=model.surface_point(host,fraction=.5)
mask=model.rounded_prism('Mask',.14*H,.2*H,.12*H,.03*H,center=p)
model.dome('Witness',4,2,center=(-35,0,0))
'''
PATCH="patch=model.surface_patch('Patch',host,mask,Wall,offset=Gap)\n"

def verify(sw,doc,record):
    validate_feature_health(sw,doc,list(record['native']['feature_refs'])+record['native']['support_features'])
    assert len(sw._get_solid_bodies(doc))==3
    r=sw.check_clearance('Host','Patch',unit='mm')
    assert r['success'] and abs(r['data']['min_distance']-.25)<.01,r
    return r['data']['min_distance']

def acceptance(path):
    sw=SolidWorksAutomation();created=sw.execute_model_code(BASE+PATCH,mode='create',output_path=str(path),observe=False)
    assert created['success'],created
    doc=sw.app.ActiveDoc;record=read_record(doc);refs=record['native']['feature_refs'].copy()
    gaps=[];volumes=[]
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    for h in [100,110,100]:
        eq.SetEquation(record['native']['parameter_bindings']['H']['global_index'],f'"H"={h}mm')
        com_get(eq,'EvaluateAll');assert doc.ForceRebuild3(False)
        gap=verify(sw,doc,record)
        mass=sw.body_volume('Host',unit='mm');assert mass['success'],mass
        gaps.append({'H':h,'gap_mm':gap});volumes.append(mass['data'])
        assert read_record(doc)['native']['feature_refs']==refs
    assert sw.save_document(str(path))['success']
    def edit(code):
        result=sw.execute_model_code(code,mode='edit',document_path=str(path),
            expected_revision=sw.get_model_context()['data']['revision'],observe=False)
        assert result['success'],result
        return result['data']
    changed=edit((BASE+PATCH).replace('.13*H','.16*H').replace('.15*H','.18*H'))
    assert changed['native_feature_ids_preserved']
    verify(sw,doc,read_record(doc))
    assert read_record(doc)['native']['feature_refs']==refs
    removed=edit(BASE)
    assert len(sw._get_solid_bodies(doc))==3
    assert sw._find_body(doc,'Mask') is not None
    assert all(sw._find_feature(doc,n) is None for n in ['Patch','Patch_Surface','Patch_Thicken'])
    appended=edit(BASE+PATCH)
    verify(sw,doc,read_record(doc))
    assert read_record(doc)['native']['feature_refs']['Witness']==refs['Witness']
    sw.app.CloseDoc(sw._get_doc_title(doc));assert sw.open_document(str(path))['success']
    doc=sw.app.ActiveDoc;assert doc.ForceRebuild3(False);verify(sw,doc,read_record(doc))
    assert sw.save_document(str(path))['success']
    observed=sw.observe_model(str(path.parent/'surface_views'),views=['front','right','isometric'],width=850)
    assert observed['success'],observed
    return {'status':'NATIVE_SURFACE_PATCH_PASS','native_clearances':gaps,'host_volumes':volumes,
            'definition_edit':changed,'removed':removed,'appended':appended,
            'reopen_verified':True,'views':observed['data']}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--report',required=True)
    args=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    with SessionLease():report=acceptance(Path(args.output).resolve())
    Path(args.report).write_text(json.dumps(report,indent=2),encoding='utf-8')
