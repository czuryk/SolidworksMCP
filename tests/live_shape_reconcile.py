"""Incremental append, replacement, deletion and dependent rebuild acceptance."""
import argparse,hashlib,json,logging,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import com_get
from solidworks_mcp.automation.shape_parametric import read_record,persist,restore
from solidworks_mcp.automation.session_lease import SessionLease

BASE='''
H=model.parameter('H',100)
model.dome('Witness',.06*H,.04*H,center=(-.3*H,0,0))
outer=model.tube('Outer',[(.2*H,0,0),(.2*H,.2*H,0)],.07*H)
tool=model.tube('Tool',[(.2*H,-.01*H,0),(.2*H,.21*H,0)],.03*H)
'''
APPEND="\nsleeve=model.combine('Sleeve',[outer,tool],operation='difference')\nmodel.mirror('Pair',[sleeve])\n"
REPLACED=BASE.replace("outer=model.tube('Outer',[(.2*H,0,0),(.2*H,.2*H,0)],.07*H)",
    "outer=model.sectioned_solid('Outer',[(0,.07*H,.07*H),(.2*H,.08*H,.08*H)],center_xz=(.2*H,0))")

def acceptance(path):
    sw=SolidWorksAutomation();r=sw.execute_model_code(BASE,mode='create',output_path=str(path),observe=False)
    assert r['success'],r
    doc=sw.app.ActiveDoc;baseline=read_record(doc);witness=baseline['native']['feature_refs']['Witness']
    stages=[]
    def edit(code,body_names):
        revision=sw.get_model_context()['data']['revision']
        r=sw.execute_model_code(code,mode='edit',document_path=str(path),expected_revision=revision,observe=False)
        assert r['success'],r
        doc=sw.app.ActiveDoc;record=read_record(doc)
        assert record['native']['feature_refs']['Witness']==witness
        assert persist(doc,restore(doc,witness))==witness
        names={str(com_get(b,'Name')) for b in sw._get_solid_bodies(doc)}
        assert names==set(body_names),(names,body_names)
        stages.append(r['data']);print(r['data'],flush=True)
    edit(BASE+APPEND,['Witness','Sleeve','Pair'])
    edit(REPLACED+APPEND,['Witness','Sleeve','Pair'])
    edit(REPLACED,['Witness','Outer','Tool'])
    assert sw.save_document(str(path))['success']
    sw.app.CloseDoc(sw._get_doc_title(doc));assert sw.open_document(str(path))['success']
    edit(BASE+APPEND,['Witness','Sleeve','Pair'])
    before=path.read_bytes()
    broken=(BASE+APPEND).replace("model.mirror('Pair',[sleeve])","model.combine('Pair',[sleeve,'Witness'])")
    r=sw.execute_model_code(broken,mode='edit',document_path=str(path),
        expected_revision=sw.get_model_context()['data']['revision'],observe=False)
    assert not r['success'] and r['data']['error']['document_restored'],r
    assert path.read_bytes()==before
    assert sw.observe_model(str(path.parent/'reconcile_views'))['success']
    return {'status':'NATIVE_DEPENDENCY_RECONCILE_PASS','stages':stages,'reopen_verified':True,
            'failed_replacement_rolled_back':True,'preserved_witness_ref':witness,
            'rollback_sha256':hashlib.sha256(before).hexdigest()}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--report',required=True)
    args=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    with SessionLease():r=acceptance(Path(args.output).resolve())
    Path(args.report).write_text(json.dumps(r,indent=2),encoding='utf-8')
