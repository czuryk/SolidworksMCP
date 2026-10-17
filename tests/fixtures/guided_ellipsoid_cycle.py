"""Drive only saved native global variables and inspect the rebuilt B-rep."""
import json
import math
from pathlib import Path
from solidworks_mcp.automation.com_utils import com_get, typed, command_scope
from solidworks_mcp.automation.shape_parametric import read_record, persist, validate_feature_health
from solidworks_mcp.automation.model_inspection import exact_bbox, kernel_health
from solidworks_mcp.automation.shape_sections import measure_sections, section_directions

target = '__OUTPUT_DIRECTORY__/Guided_A.SLDPRT'
output = Path('__OUTPUT_DIRECTORY__/integrated_cycle.json')
app = typed(sw, 'ISldWorks')
doc = app.ActiveDoc
assert str(com_get(doc, 'GetPathName')).replace('\\','/') == target
app.CloseDoc(com_get(doc, 'GetTitle'))
assert automation.open_document(target)['success']
doc = app.ActiveDoc
binding = read_record(doc)['native']['parameter_bindings']
features = ['Oval_Half','Oval_OtherHalf','Oval_Volume','Oval','Oval_Equator','Oval_Axis']+[f'Oval_Meridian{i}' for i in range(1,5)]
original_ids = {name: persist(doc, automation._find_feature(doc, name)) for name in features}
report = {'status': 'IN_PROGRESS', 'cycles': [], 'feature_ids': original_ids,
          'method': 'Native global equations + EvaluateAll/ForceRebuild3; no program or curve regeneration'}
output.write_text(json.dumps(report, indent=2))
with command_scope(app):
    eq = typed(com_get(doc, 'GetEquationMgr'), 'IEquationMgr')
    for radii in [(12,17,8), (8,17,12), (15,25,4), (10,20,10), (12,12,12), (10,20,6)]:
        for name, value in zip(['Rx', 'Ry', 'Rz'], radii):
            eq.SetEquation(binding[name]['global_index'], f'"{name}" = {value}mm')
        com_get(eq, 'EvaluateAll')
        rebuilt = bool(doc.ForceRebuild3(False))
        report['pending'] = {'radii_mm': radii, 'rebuilt': rebuilt,
            'health': {name: typed(automation._find_feature(doc, name), 'IFeature').GetErrorCode2()
                       for name in features}}
        output.write_text(json.dumps(report, indent=2))
        validate_feature_health(automation, doc, features)
        body = automation._find_body(doc, 'Oval')
        assert body is not None
        row = {'radii_mm': radii, 'rebuilt': rebuilt, 'bbox': exact_bbox(body),
               'mass': automation._body_mass_props(body, 'mm'), 'kernel': kernel_health(body)}
        faces = list(com_get(body, 'GetFaces') or [])
        surface_errors = []
        for latitude in range(-8, 9):
            phi = latitude * math.pi / 18
            for longitude in range(36):
                theta = longitude * 2 * math.pi / 36
                p = [40+radii[0]*math.cos(phi)*math.cos(theta), 35+radii[1]*math.sin(phi),
                     12+radii[2]*math.cos(phi)*math.sin(theta)]
                distances = []
                for face in faces:
                    q = typed(face, 'IFace2').GetClosestPointOn(*[v/1000 for v in p])
                    assert q is not None
                    distances.append(math.dist(p, [1000*v for v in q[:3]]))
                surface_errors.append(min(distances))
        row['sampled_surface_error_mm'] = max(surface_errors)
        row['surface_sample_count'] = len(surface_errors)
        queries = [{'component':'Oval', 'center':[40.017,35.013,12.019], 'normal':n, 'samples':64}
                   for n in [[0,1,0],[0,0,1],[1,0,0],[1,1,1],[1,2,-1]]]
        record = {'native':{'components':{'Oval':{'bodies':['Oval']}}}}
        sections = measure_sections(automation, doc, record, queries)
        errors = []
        for query, section in zip(queries, sections):
            expected = []
            for d in section_directions(query['normal'], 64):
                a = sum((v/r)**2 for v,r in zip(d,radii))
                b = 2*sum(x*v/r**2 for x,v,r in zip([.017,.013,.019],d,radii))
                k = sum((x/r)**2 for x,r in zip([.017,.013,.019],radii))-1
                expected.append((-b+math.sqrt(b*b-4*a*k))/(2*a))
            errors.append(max(abs(v-e) for v,e in zip(section['radii_mm'],expected))
                          if section['status'] == 'SAMPLED' else None)
        row['section_statuses'] = [s['status'] for s in sections]
        row['section_errors_mm'] = errors
        row['volume_relative_error'] = abs(row['mass']['volume_exact']/(4*math.pi*math.prod(radii)/3)-1)
        row['same_feature_ids'] = original_ids == {name:persist(doc,automation._find_feature(doc,name)) for name in features}
        report['cycles'].append(row)
        output.write_text(json.dumps(report, indent=2))
        print(json.dumps(row))
        assert rebuilt and row['kernel']['passed'] and row['same_feature_ids']
        assert row['sampled_surface_error_mm'] < .02, row
        assert max(abs(row['bbox'][side][i]-[40,35,12][i]-sign*radii[i]) for side,sign in [('min',-1),('max',1)]
                   for i in range(3)) < 1e-5
        assert row['volume_relative_error'] < .001
    assert automation.save_document(target)['success']
report['status'] = 'NATIVE_SWEEP_CYCLE_PASS'
report.pop('pending', None)
output.write_text(json.dumps(report, indent=2))
