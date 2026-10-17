"""Surface-following solids with native offset/thicken/Boolean dependencies."""
from .com_utils import com_get,typed,create_select_data,resolve_solidworks_constant
from .shape_parametric import persist,short_dimension
from ..shape_program import ProgramError


def build_surface_patch(compiler,spec):
    c=compiler;doc=c.doc;a=c.a;name=spec['id'];surface=spec['surface']
    c.stage='native_surface_patch'
    host,mask=spec['seeds']
    host_bodies=c.components[host]['bodies'];mask_bodies=c.components[mask]['bodies']
    for role,key,owned in (('host',host,host_bodies),('mask',mask,mask_bodies)):
        consumer=c.components[key].get('consumed_by')
        if not owned and consumer:
            # Name the component that absorbed the body instead of a bare scope error.
            raise ProgramError('COMPONENT_CONSUMED',
                f'Surface patch {role} {key} has no live body: it is consumed by {consumer}',
                component=key,consumed_by=consumer,role=role,patch=name,stage=c.stage)
    c.require(len(host_bodies)==len(mask_bodies)==1,'Host and mask must each own one solid')
    host_body=a._find_body(doc,host_bodies[0]);before=c.body_names()
    faces=[f for f in com_get(host_body,'GetFaces')
           if not bool(com_get(com_get(f,'GetSurface'),'IsPlane'))]
    c.require(bool(faces),'Host has no curved faces')
    existing_sheets={persist(doc,b) for b in (typed(doc,'IPartDoc').GetBodies2(1,False) or [])}
    doc.ClearSelection2(True)
    for i,face in enumerate(faces):
        c.require(bool(typed(face,'IEntity').Select4(i>0,create_select_data(doc,0))),
                  'Cannot select explicit host faces')
    typed(doc,'IModelDoc2').InsertOffsetSurface(surface['offset']*.001,surface['reverse'])
    offset=c.last_feature()
    c.require(com_get(offset,'GetTypeName2')=='OffsetRefSurface','Native offset surface failed')
    offset.Name=name+'_Surface'
    c.feature_refs[str(offset.Name)]=persist(doc,offset)
    sheets=[b for b in (typed(doc,'IPartDoc').GetBodies2(1,False) or [])
            if persist(doc,b) not in existing_sheets]
    c.require(len(sheets)==1,'Host faces must form one connected offset sheet')
    doc.ClearSelection2(True)
    c.require(bool(typed(sheets[0],'IBody2').Select2(False,create_select_data(doc,1))),
              'Cannot select the owned offset sheet')
    thicken=typed(doc.FeatureManager,'IFeatureManager').FeatureBossThicken(
        surface['thickness']*.001,1 if surface['inward'] else 0,0,False,False,False,True)
    c.require(thicken is not None,'Native surface thickening failed')
    thicken.Name=name+'_Thicken'
    c.feature_refs[str(thicken.Name)]=persist(doc,thicken)
    for feature,key in [(offset,'offset_expression'),(thicken,'thickness_expression')]:
        if feature is offset and not surface['offset']:continue
        display=com_get(feature,'GetFirstDisplayDimension')
        c.require(display is not None,'Surface feature has no native driving dimension')
        dimension=short_dimension(com_get(display,'GetDimension2',0))
        c.require(dimension=='D1@'+str(feature.Name),'Unexpected surface driving dimension')
        c.add_equation(dimension,surface[key])
    solids=[b for b in c.bodies() if str(com_get(b,'Name')) not in before]
    c.require(len(solids)==1,'Surface recipe must produce one skin solid')
    doc.ClearSelection2(True)
    for i,body in enumerate([solids[0],a._find_body(doc,mask_bodies[0])]):
        c.require(bool(typed(body,'IBody2').Select2(i>0,create_select_data(doc,2))),
                  'Cannot select skin and clipping mask')
    feature=typed(doc.FeatureManager,'IFeatureManager').InsertCombineFeature(
        resolve_solidworks_constant('SWBODYINTERSECT'),None,None)
    c.require(feature is not None,'Mask does not produce a valid surface patch')
    feature.Name=name
    untouched=before-set(mask_bodies)
    bodies=c.bodies()
    c.require(untouched.issubset({str(com_get(b,'Name')) for b in bodies}),
              'Surface patch modified an unrelated body')
    patches=[b for b in bodies if str(com_get(b,'Name')) not in untouched]
    c.require(len(patches)==1,'Mask must intersect exactly one connected patch')
    patch=patches[0];patch.Name=name
    c.require(int(com_get(patch,'GetFaceCount'))>0,'Patch has no faces')
    c.components[mask]['bodies']=[];c.components[mask]['face_count']=0
    c.components[mask]['consumed_by']=name
    c.components[name]={'feature':name,'bodies':[name],
                        'face_count':int(com_get(patch,'GetFaceCount')),
                        'dependencies':[host,mask],'consumes':[mask]}
    # The thicken consumes the sheet; no visible auxiliary surface is delivered.
    remaining={persist(doc,b) for b in (typed(doc,'IPartDoc').GetBodies2(1,False) or [])}
    c.require(remaining==existing_sheets,'Surface recipe left an unconsumed sheet')
    doc.ClearSelection2(True)
