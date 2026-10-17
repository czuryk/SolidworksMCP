"""Sample real B-rep cross-sections without selection or construction entities."""
import math
import pythoncom
from win32com.client import VARIANT

from ..shape_program import ProgramError
from .com_utils import typed, com_get, array_r8, resolve_solidworks_constant


def validate_sections(queries):
    if queries is None: return []
    if not isinstance(queries, list) or not 1 <= len(queries) <= 4:
        raise ProgramError('INVALID_SECTIONS', 'Use one to four targeted section queries')
    allowed={'component','center','normal','samples','expected_radius','tolerance_mm'}
    for q in queries:
        if not isinstance(q,dict) or set(q)-allowed or not {'component','center','normal'} <= set(q):
            raise ProgramError('INVALID_SECTIONS','Each section needs component, center and normal')
        if not isinstance(q['component'],str) or not q['component']:
            raise ProgramError('INVALID_SECTIONS','component must be a name')
        for field in ('center','normal'):
            if (not isinstance(q[field],list) or len(q[field])!=3 or
                    any(not isinstance(x,(int,float)) or isinstance(x,bool) or not math.isfinite(x) for x in q[field])):
                raise ProgramError('INVALID_SECTIONS',f'{field} must contain three finite coordinates')
        if math.hypot(*q['normal']) < 1e-12:
            raise ProgramError('INVALID_SECTIONS','normal must be nonzero')
        n=q.get('samples',16)
        if not isinstance(n,int) or isinstance(n,bool) or not 8 <= n <= 64:
            raise ProgramError('INVALID_SECTIONS','samples must be between 8 and 64')
        if ('expected_radius' in q) != ('tolerance_mm' in q):
            raise ProgramError('INVALID_SECTIONS','Provide both expected_radius and tolerance_mm for a radius check')
        for key in ('expected_radius','tolerance_mm'):
            if key in q and (not isinstance(q[key],(int,float)) or isinstance(q[key],bool) or not math.isfinite(q[key]) or q[key]<=0):
                raise ProgramError('INVALID_SECTIONS',f'{key} must be finite and positive')
    return queries


def section_directions(normal, samples):
    length=math.hypot(*normal);normal=[x/length for x in normal]
    helper=[1.,0.,0.] if abs(normal[0])<.9 else [0.,1.,0.]
    dot=sum(a*b for a,b in zip(helper,normal))
    u=[a-dot*b for a,b in zip(helper,normal)]; length=math.hypot(*u);u=[x/length for x in u]
    v=[normal[1]*u[2]-normal[2]*u[1],normal[2]*u[0]-normal[0]*u[2],normal[0]*u[1]-normal[1]*u[0]]
    return [[math.cos(i*math.tau/samples)*a+math.sin(i*math.tau/samples)*b for a,b in zip(u,v)] for i in range(samples)]


def radial_results(points, count, directions, center_m, exit_flag, enter_flag):
    if count < 0 or len(points)!=count*9:
        raise ProgramError('SECTION_READ_FAILED','Unexpected native ray intersection layout')
    hits=[[] for _ in directions]
    for j in range(count):
        row=points[j*9:(j+1)*9];body_index,ray_index,kind=map(int,row[:3])
        if body_index!=0 or not 0<=ray_index<len(directions):
            raise ProgramError('SECTION_READ_FAILED','Unexpected native body or ray index')
        delta=[a-b for a,b in zip(row[3:6],center_m)]
        distance=sum(a*b for a,b in zip(delta,directions[ray_index]))*1000
        if distance>1e-5:hits[ray_index].append((distance,kind))
    radii=[];unknown=[]
    for index,ray in enumerate(hits):
        ray.sort()
        # Duplicate seam/vertex hits at the same position do not create a
        # second section boundary. Keep all flags at that nearest position.
        if not ray:
            radii.append(None);unknown.append({'ray':index,'reason':'NO_HIT'});continue
        nearest=ray[0][0];flags=0
        for distance,kind in ray:
            if abs(distance-nearest)<.001:flags|=kind
        if not flags&exit_flag or flags&enter_flag:
            radii.append(None);unknown.append({'ray':index,'reason':'CENTER_NOT_INSIDE_OR_AMBIGUOUS'});continue
        radii.append(nearest)
    valid=[r for r in radii if r is not None]
    return {'status':'SAMPLED' if not unknown else 'UNKNOWN',
            'radii_mm':[round(x,5) if x is not None else None for x in radii],
            'radius_min_mm':round(min(valid),5) if valid else None,
            'radius_max_mm':round(max(valid),5) if valid else None,
            'unknown_rays':unknown}


def measure_sections(automation, doc, record, queries):
    if not queries:return []
    if not record:raise ProgramError('COMPONENT_MAPPING_REQUIRED','Sections require native component mapping')
    results=[]
    for q in queries:
        item=record['native']['components'].get(q['component'])
        if not item:raise ProgramError('UNKNOWN_COMPONENT',q['component'])
        if item.get('consumed_by'):
            raise ProgramError('COMPONENT_CONSUMED',
                f"{q['component']} is consumed by {item['consumed_by']}; address that resulting component",
                component=q['component'],consumed_by=item['consumed_by'])
        if len(item.get('bodies',[]))!=1:raise ProgramError('SECTION_BODY_SCOPE','Section requires one component body')
        body=automation._find_body(doc,item['bodies'][0])
        if body is None:raise ProgramError('NATIVE_MAPPING_STALE','Section body missing')
        n=q.get('samples',16);directions=section_directions(q['normal'],n)
        center_m=[x/1000 for x in q['center']]
        options=resolve_solidworks_constant('swRayPtsOptsENTRY_EXIT')|resolve_solidworks_constant('swRayPtsOptsNORMALS')
        count=typed(doc.Extension,'IModelDocExtension').RayIntersections(
            VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_DISPATCH,[body]),array_r8(center_m*n),
            array_r8([x for direction in directions for x in direction]),options,1e-8,1e-7,True)
        points=list(com_get(doc,'GetRayIntersectionsPoints') or [])
        result=radial_results(points,count,directions,center_m,
                             resolve_solidworks_constant('swRayPtsResultsEXIT'),
                             resolve_solidworks_constant('swRayPtsResultsENTER'))
        result.update(component=q['component'],center_mm=q['center'],normal=q['normal'],samples=n,
                      method='native_body_scoped_RayIntersections',
                      coverage='Sampled first exits from a supplied interior center; not a continuous circularity certificate')
        if 'expected_radius' in q:
            error=max(abs(r-q['expected_radius']) for r in result['radii_mm'] if r is not None) if result['status']=='SAMPLED' else None
            result['radius_check']={'expected_mm':q['expected_radius'],'tolerance_mm':q['tolerance_mm'],
                                    'max_sample_error_mm':error,'passed':None if error is None else error<=q['tolerance_mm']}
        results.append(result)
    return results
