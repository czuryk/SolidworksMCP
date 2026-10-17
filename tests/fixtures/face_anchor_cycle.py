"""Reopen the compiled model and change only native globals, including ray origin."""
import json,math
from pathlib import Path
from solidworks_mcp.automation.com_utils import com_get,typed,command_scope
from solidworks_mcp.automation.shape_parametric import read_record,persist,validate_feature_health
from solidworks_mcp.automation.shape_anchor import validate_anchors,point_xyz
from solidworks_mcp.automation.model_inspection import exact_bbox,kernel_health
target='__OUTPUT_DIRECTORY__/Anchor_Release.SLDPRT'
out=Path('__OUTPUT_DIRECTORY__/native_anchor_cycle.json')
app=typed(sw,'ISldWorks')
assert str(com_get(doc,'GetPathName')).replace('\\','/')==target
assert not com_get(doc,'GetSaveFlag')
app.CloseDoc(com_get(doc,'GetTitle'))
assert automation.open_document(target)['success']
doc=app.ActiveDoc
native=read_record(doc)['native']
names=list(native['feature_refs'])+native['support_features']
ids={n:persist(doc,automation._find_feature(doc,n)) for n in names}
report={'status':'IN_PROGRESS','cycles':[],'method':'Reopen and native equations only; no curve regeneration'}
with command_scope(app):
    eq=typed(com_get(doc,'GetEquationMgr'),'IEquationMgr')
    for values in [(12,17,8,33,2.5,3),(8,17,12,31,1.5,2.5),(10,20,6,32,2,2)]:
        for name,value in zip(['Rx','Ry','Rz','X','Radius','Depth'],values):
            eq.SetEquation(native['parameter_bindings'][name]['global_index'],f'"{name}" = {value}mm')
        com_get(eq,'EvaluateAll')
        rebuilt=bool(doc.ForceRebuild3(False))
        row={'values':values,'rebuilt':rebuilt}
        report['cycles'].append(row)
        out.write_text(json.dumps(report,indent=2))
        validate_feature_health(automation,doc,names)
        validate_anchors(automation,doc,native['components'])
        xyz=point_xyz(automation._find_feature(doc,'Boss_Anchor'))
        body=automation._find_body(doc,'Boss')
        bbox=exact_bbox(body)
        mass=automation._body_mass_props(body,'mm')
        row.update(anchor_mm=xyz,bbox=bbox,kernel=kernel_health(body),volume_mm3=mass['volume_exact'],
                   same_ids=ids=={n:persist(doc,automation._find_feature(doc,n)) for n in names})
        assert rebuilt and row['same_ids'] and row['kernel']['passed']
        assert abs(xyz[0]-values[3])<1e-5 and abs(xyz[1]-30)<1e-5
        assert abs(bbox['min'][2]-xyz[2])<1e-5 and abs(bbox['max'][2]-xyz[2]-values[5])<1e-5
        assert abs(bbox['max'][0]-bbox['min'][0]-2*values[4])<1e-5
        assert abs(mass['volume_exact']/(math.pi*values[4]**2*values[5])-1)<1e-6
        out.write_text(json.dumps(report,indent=2))
        print(json.dumps(row))
    assert automation.save_document(target)['success']
report['status']='COMPILED_NATIVE_ANCHOR_CYCLE_PASS'
out.write_text(json.dumps(report,indent=2))
