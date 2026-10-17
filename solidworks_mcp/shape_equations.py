"""Pure native-curve recipe planning from retained symbolic shape expressions."""

import math
import re
from decimal import Decimal
from functools import lru_cache
import sympy as sp
from .shape_program import Expr, ProgramError, scalar, resolve, AXES, CROSS_AXES, from_axis_frame


def native_parameter_name(name):
    # SW reserves Thickness for sheet-metal expressions even in a new part.
    return 'ShapeParameter_'+name if name.casefold()=='thickness' else name


def decode(value):
    if isinstance(value, dict) and set(value) == {'op', 'args'}:
        return Expr(value['op'], tuple(decode(v) for v in value['args']))
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        return {k: decode(v) for k,v in value.items()}
    return value


def emit(expr, symbols):
    value = symbolic(scalar(expr))
    t = sp.Symbol('t', real=True)
    if value.has(t) and value.is_polynomial(t):
        # Factoring an almost linear polynomial can produce a tiny common
        # multiplier and enormous integer terms. Avoid that ill-conditioned
        # representation. Horner form keeps coefficients in
        # model units and evaluates each closed numeric expression only once.
        coefficients = sp.Poly(value, t).all_coeffs()
        result = print_symbolic(sp.factor(coefficients[0]), symbols)
        for coefficient in coefficients[1:]:
            result = '(' + result + '*t+' + print_symbolic(sp.factor(coefficient), symbols) + ')'
        return result
    return print_symbolic(sp.factor(value), symbols)


def emit_dimension(expr, symbols):
    """Keep affine dimension coefficients in model units, without huge factors.

    Curve emission is deliberately separate to preserve stored native recipes.
    Non-affine expressions retain their established representation.
    """
    value = symbolic(scalar(expr))
    variables = sorted(value.free_symbols, key=str)
    if variables and value.is_polynomial(*variables):
        polynomial = sp.Poly(value, *variables)
        if polynomial.total_degree() <= 1:
            return print_symbolic(sp.expand(value), symbols)
    return emit(expr, symbols)


def reverse_native_curve(curve):
    """Reverse only the parameter, preserving quoted native dimension names."""
    a, b = curve['range']
    replacement = '(' + format(a+b, '.15g') + '-t)'
    def reverse(expression):
        pieces = re.split(r'("[^"]*")', expression)
        return ''.join(piece if i % 2 else re.sub(r'\bt\b', replacement, piece)
                       for i, piece in enumerate(pieces))
    return {**curve, 'xyz': [reverse(v) for v in curve['xyz']],
            'parameter_reversed': True}


def retained_native_curve(planned, stored):
    return reverse_native_curve(planned) if stored.get('parameter_reversed') else planned


@lru_cache(maxsize=16384)
def symbolic(expr):
    op,args=expr.op,expr.args
    if op=='literal':
        # Match the emitter's existing 15-digit numeric contract before exact
        # algebra, so binary float noise cannot become fake high-order terms.
        return sp.Rational(format(float(args[0]), '.15g'))
    if op=='parameter': return sp.Symbol(args[0],positive=True)
    if op=='t': return sp.Symbol('t',real=True)
    a=[symbolic(x) for x in args]
    if op=='add': return a[0]+a[1]
    if op=='sub': return a[0]-a[1]
    if op=='mul': return a[0]*a[1]
    if op=='div': return sp.cancel(a[0]/a[1])
    if op=='neg': return -a[0]
    if op=='sqrt': return sp.sqrt(sp.factor(a[0]))
    if op=='sin': return sp.sin(a[0])
    if op=='cos': return sp.cos(a[0])
    raise ProgramError('UNSUPPORTED_EXPRESSION',op)


def print_symbolic(expr,symbols):
    if expr.is_number:
        # The equation-curve parser rejects scientific notation such as 2e-5.
        return format(Decimal(format(float(expr),'.15g')),'f')
    if expr.is_Symbol: return 't' if str(expr)=='t' else '"'+symbols[str(expr)]+'"'
    if expr.is_Add: return '('+'+'.join(print_symbolic(a,symbols) for a in expr.args)+')'
    if expr.is_Mul: return '('+'*'.join(print_symbolic(a,symbols) for a in expr.args)+')'
    if expr.is_Pow:
        a,b=expr.args
        if b.is_negative:
            return '(1/'+print_symbolic(sp.Pow(a,-b),symbols)+')'
        if b==sp.Rational(1,2): return 'sqr('+print_symbolic(a,symbols)+')'
        return '('+print_symbolic(a,symbols)+'^('+print_symbolic(b,symbols)+'))'
    if expr.func in {sp.sin,sp.cos}:
        return str(expr.func)+'('+print_symbolic(expr.args[0],symbols)+')'
    raise ProgramError('UNSUPPORTED_EXPRESSION',str(expr))


T = Expr('t', ())


def sqrt(v): return Expr('sqrt',(scalar(v),))
def sin(v): return Expr('sin',(scalar(v),))
def cos(v): return Expr('cos',(scalar(v),))
def add(a,b): return [x+y for x,y in zip(a,b)]
def sub(a,b): return [x-y for x,y in zip(a,b)]
def scale(a,s): return [x*s for x in a]
def norm(v): return scale(v,1/sqrt(sum(x*x for x in v)))
def cross(a,b): return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]


# Sketch frames of the default planes, pinned by a native check when a sketch is created:
# sketch (sx, sy) expressed in model coordinates, as (model axis, sign) pairs.
PLANE_FRAMES = {'Front': (('X', 1), ('Y', 1)), 'Top': (('X', 1), ('Z', -1)), 'Right': (('Z', -1), ('Y', 1))}


def frame_check(plane):
    """A model point and its expected sketch coordinates; mm. Catches a mirrored plane frame."""
    (ax, sx), (ay, sy) = PLANE_FRAMES[plane]
    model = {axis: 0. for axis in AXES}
    model[ax], model[ay] = 3., 5.
    return {'model': [model[k] for k in AXES], 'sketch': [3.*sx, 5.*sy]}


def same(a, b):
    """Symbolic equality: true for every parameter value, not only the current one."""
    return sp.simplify(symbolic(scalar(a)) - symbolic(scalar(b))) == 0


def emit_length(vector, values, symbols):
    """Native expression for the length of a symbolic vector.

    Written as sqr((dx)^2+(dy)^2+(dz)^2) over the affine components: expanding
    the sum first produced 16-digit integer coefficients that the equation
    manager rejects. One nonzero component is just its magnitude, with the sign
    taken at the current values.
    """
    current = {sp.Symbol(k, positive=True): v for k, v in values.items()}
    parts = [scalar(v) for v in vector if symbolic(scalar(v)) != 0]
    if not parts:
        raise ProgramError('DEGENERATE_PATH', 'A straight tube needs two distinct points')
    if len(parts) == 1:
        negative = float(symbolic(parts[0]).subs(current)) < 0
        return emit_dimension(-parts[0] if negative else parts[0], symbols)
    return 'sqr(' + '+'.join('(' + emit_dimension(part, symbols) + ')^2' for part in parts) + ')'


def reversed_path_curve(curve):
    """The same locus drawn from its other end; the range stays 0..1."""
    xyz,a,b=curve
    def reverse(expr):
        if expr.op=='t': return 1-T
        if expr.op in {'literal','parameter'}:return expr
        return Expr(expr.op,tuple(reverse(v) for v in expr.args))
    return ([reverse(scalar(v)) for v in xyz],a,b)


def orient_path_curve(curve,values):
    """Avoid a stationary coordinate at the start of SW's equation solver."""
    xyz,a,b=curve
    t=sp.Symbol('t',real=True)
    polys=[symbolic(scalar(v)) for v in xyz]
    substitutions={sp.Symbol(k,positive=True):v for k,v in values.items()}
    derivatives=[sp.diff(p,t) for p in polys if p.has(t)]
    def stationary(endpoint):
        return sum(abs(float(d.subs(substitutions).subs(t,endpoint)))<1e-9 for d in derivatives)
    if stationary(a)<=stationary(b): return curve
    return reversed_path_curve(curve)


CHORD_PARALLEL=1e-9     # Sine of the angle below which a tangent counts as lying along the chord.


def orient_path_chord(curve,values):
    """Draw a path segment from the end whose tangent lies along its chord.

    SW2026 marks a 3D-sketch equation spline over defined when the tangent at its
    end is parallel to its chord and the tangent at its start is not. Every
    interpolated path meets that in its last segment, and wherever a segment ends
    a straight run; the same locus drawn from the other end is accepted. Measured
    on 60 segments; a tangent turned by 1e-6 degree no longer warns, so only the
    exact case matters. Every other segment keeps the stationary-start rule.
    """
    xyz,a,b=curve
    t=sp.Symbol('t',real=True)
    substitutions={sp.Symbol(k,positive=True):v for k,v in values.items()}
    polys=[symbolic(scalar(v)).subs(substitutions) for v in xyz]
    slopes=[sp.diff(p,t) for p in polys]
    def at(expressions,value): return [float(e.subs(t,value)) for e in expressions]
    chord=[q-p for p,q in zip(at(polys,a),at(polys,b))]
    def along_chord(value):
        u=at(slopes,value)
        turn=math.hypot(u[1]*chord[2]-u[2]*chord[1],u[2]*chord[0]-u[0]*chord[2],u[0]*chord[1]-u[1]*chord[0])
        return turn<=CHORD_PARALLEL*math.hypot(*u)*math.hypot(*chord)
    start,end=along_chord(a),along_chord(b)
    if end and not start: return reversed_path_curve(curve)
    if start and not end: return curve
    return orient_path_curve(curve,values)


def placement_steps(name, geometry, values, globals_map):
    """Keep rotation and translation separate: the SW API ignores one if mixed."""
    steps=[]
    for axis,angle in zip('XYZ',geometry['rotation']):
        if symbolic(angle)==0:continue
        value=resolve(angle,values)
        if abs(value)<1e-8 or abs(value)>=2*math.pi:
            raise ProgramError('PLACEMENT_ANGLE_DOMAIN','Active rotations require 0 < abs(angle) < 2*pi radians',component=name)
        direction=1 if value>0 else -1
        steps.append({'kind':'rotate','axis':axis,'direction':direction,'value':abs(value),
                      'expression':emit_dimension(angle*direction*180/math.pi,globals_map)+' * 1deg',
                      'feature':name+'_Rotate'+axis})
    axes={}
    for axis,coordinate in zip('XYZ',geometry['origin']):
        if symbolic(coordinate)==0:continue
        value=resolve(coordinate,values)
        if abs(value)<1e-8:
            raise ProgramError('NATIVE_PARAMETER_DOMAIN','A driven translation cannot start at zero; use literal zero for a fixed axis',component=name)
        direction=1 if value>0 else -1
        axes[axis]={'direction':direction,'value':value,'expression':emit_dimension(coordinate*direction,globals_map)}
    if axes:steps.append({'kind':'translate','axes':axes,'feature':name})
    if not steps:
        raise ProgramError('IDENTITY_PLACEMENT','This frame does not move the bodies; use the source component',component=name)
    steps[-1]['feature']=name
    return steps


def plan_recipes(program, dimensions):
    """Topology keys distinguish supported definition edits from reconstruction."""
    if program.get('evaluated_parameters'):
        raise ProgramError('NATIVE_PARAMETER_ESCAPE',
            'model.value() cannot convert native parameters to frozen Python numbers; retain symbolic expressions')
    values = {k:v['value'] for k,v in program['parameters'].items()}
    globals_map={k:native_parameter_name(k) for k in dimensions}
    if len({v.casefold() for v in globals_map.values()})!=len(globals_map):
        raise ProgramError('NATIVE_PARAMETER_COLLISION','Parameter names map to the same native variable')
    if any(k.lower()=='t' for k in values):
        raise ProgramError('NATIVE_PARAMETER_DOMAIN','t is reserved for native curve parameterization')
    if any(not 0 < v <= 1e6 for v in values.values()):
        raise ProgramError('NATIVE_PARAMETER_DOMAIN', 'Native parameters must be positive and at most 1e6; coordinates may be signed')
    nodes = decode(program['source_graph']['nodes'])
    specs = []
    for node in nodes:
        name,kind,g = node['id'],node['kind'],node['geometry']
        sketches = []
        def sketch(suffix, curves, mode='3d', axis=False):
            sketches.append({'name':name+suffix, 'mode':mode, 'axis':axis,
                'curves':[{'xyz':[emit(v,dimensions) for v in xyz], 'range':[a,b]}
                          for xyz,a,b in curves]})
        def line(a,b): return (add(a,scale(sub(b,a),T)),0.,1.)
        placement = {}
        scale_z = None
        extrusion = None
        surface = None
        transform = None
        guided_ellipsoid = None
        anchor = None
        variant = []      # Non-default native recipes; part of the topology key.
        driven = {}       # Named native driving dimensions: role -> expression.
        edge_feature = None
        pattern = None
        spans = None
        if kind == 'anchored_extrude':
            direction = resolve(g['direction'], values)
            origin = resolve(g['origin'], values)
            sign = 1 if resolve(g['depth'], values) > 0 else -1
            anchor = {'origin': origin, 'direction': direction, 'depth_sign': sign}
            sketch('_Ray', [line(g['origin'], add(g['origin'], g['direction']))])
            profile = g['profile_values']
            c=(1-T*T)/(1+T*T);s=2*T/(1+T*T)
            if g['profile_kind'] in {'circle','ellipse'}:
                rx, ry = (profile[0], profile[0]) if len(profile)==1 else profile
                curves=[([rx*(ux*c-uy*s),ry*(uy*c+ux*s),scalar(0)],0.,1.)
                        for ux,uy in [(1,0),(0,1),(-1,0),(0,-1)]]
                if g['profile_kind']=='circle':
                    curves=[]
                    anchor['circle']={'radius':resolve(rx,values),'diameter_expression':emit_dimension(2*rx,globals_map)}
            else:
                w,h,r=profile[0]/2,profile[1]/2,profile[2]
                a,b=w-r,h-r
                curves=[([dx+r*(ux*c-uy*s),dy+r*(uy*c+ux*s),scalar(0)],0.,1.)
                        for dx,dy,ux,uy in [(a,b,1,0),(-a,b,0,1),(-a,-b,-1,0),(a,-b,0,-1)]]
                curves.extend(line([p[0],p[1],scalar(0)],[q[0],q[1],scalar(0)])
                              for p,q in [((a,h),(-a,h)),((-w,b),(-w,-b)),((-a,-h),(a,-h)),((w,-b),(w,b))])
            sketch('_Profile', curves, mode='anchor')
            sketches[-1]['plane'] = name+'_Frame'
            extrusion={'value':abs(resolve(g['depth'], values)), 'expression':emit_dimension(g['depth']*sign,globals_map)}
        elif kind == 'ellipsoid' and g.get('approximate'):
            rx,ry,rz = g['radii']
            guided_ellipsoid = {'radii': resolve(g['radii'], values)}
            sketch('_Axis', [([scalar(0),ry*T,scalar(0)],0.,1.)])
            c=(1-T*T)/(1+T*T);s=2*T/(1+T*T)
            for i,(x,z) in enumerate([(1,0),(0,1),(-1,0),(0,-1)]):
                sketch(f'_Meridian{i+1}', [([x*rx*c,ry*s,z*rz*c],0.,1.)])
            for axis,value in zip('XYZ',g['center']):
                if symbolic(value)==0:continue
                actual=resolve(value,values)
                if abs(actual)<1e-8:
                    raise ProgramError('NATIVE_PARAMETER_DOMAIN',
                        'A driven placement cannot start at zero; use literal zero for a fixed axis',component=name)
                direction=1 if actual>0 else -1
                placement[axis]={'expression':emit_dimension(value*direction,globals_map),
                                 'value':actual,'direction':direction}
        elif kind == 'sectioned_solid':
            cx,cz = g['center_xz']
            build_axis = g.get('axis','Y')
            cross_a,cross_b = CROSS_AXES[build_axis]
            base = dict(zip(AXES, g['origin'])) if g.get('origin') is not None else None
            if build_axis != 'Y' or base is not None:
                variant.append('axis='+build_axis+('+origin' if base is not None else ''))
            if g.get('spans') == 'linear':
                spans = 'linear'
                variant.append('spans=linear')
            for i,(y,rx,rz) in enumerate(g['sections']):
                if g.get('profile_representation')=='rational_quarters_v2':
                    # Share each emitted radius across adjacent quarter endpoints.
                    # Folding 2*radius independently can leave a rounding seam.
                    if base is not None:
                        along,ca,cb = base[build_axis]+y,base[cross_a],base[cross_b]
                    elif build_axis == 'Y':
                        along,ca,cb = y,cx,cz
                    else:
                        along,ca,cb = y,scalar(0),scalar(0)
                    center = [emit_dimension(v,dimensions) for v in (ca,along,cb)]
                    radii = [emit_dimension(v,dimensions) for v in (rx,rz)]
                    c='((1-t^2)/(1+t^2))';s='((2*t)/(1+t^2))'
                    curves=[]
                    for a,b in [(c,s),('(-'+s+')',c),('(-'+c+')','(-'+s+')'),(s,'(-'+c+')')]:
                        # The default Y axis keeps the stored expression order: x, height, z.
                        curves.append({'xyz':list(from_axis_frame(build_axis,center[1],
                                              '('+center[0]+'+'+radii[0]+'*'+a+')',
                                              '('+center[2]+'+'+radii[1]+'*'+b+')')), 'range':[0.,1.]})
                    sketches.append({'name':name+f'_Section{i+1}','mode':'3d','axis':False,'curves':curves})
                    if spans and 0<i<len(g['sections'])-1:
                        # Each ruled span owns both of its sections: an interior station is drawn twice.
                        sketches.append({'name':name+f'_Section{i+1}b','mode':'3d','axis':False,
                                         'curves':[dict(curve) for curve in curves]})
                elif g.get('profile_representation')=='rational_quarters':
                    c=(1-T*T)/(1+T*T);s=2*T/(1+T*T)
                    curves=[([cx+rx*(ux*c-uz*s),y,cz+rz*(uz*c+ux*s)],0.,1.)
                            for ux,uz in [(1,0),(0,1),(-1,0),(0,-1)]]
                    sketch(f'_Section{i+1}',curves)
                else:
                    # Retain the native curve topology of previously saved parts.
                    xyz=[cx+rx*cos(T),y,cz+rz*sin(T)]
                    sketch(f'_Section{i+1}',[(xyz,0.,math.pi),(xyz,math.pi,2*math.pi)])
        elif kind in {'tube','variable_tube'}:
            points = g['points']
            curves=[]
            tangents=[sub(points[1],points[0])]+[
                scale(sub(points[i+1],points[i-1]),.5) for i in range(1,len(points)-1)
            ]+[sub(points[-1],points[-2])]
            for i,(a,b) in enumerate(zip(points,points[1:])):
                if len(points)==2: curves.append(line(a,b)); continue
                xyz=add(add(scale(a,2*T*T*T-3*T*T+1),scale(b,-2*T*T*T+3*T*T)),
                        add(scale(tangents[i],T*T*T-2*T*T+T),scale(tangents[i+1],T*T*T-T*T)))
                curves.append((xyz,0.,1.))
            # Paths saved before the chord rule keep the direction their native curves were solved in.
            orient=orient_path_curve
            if g.get('recipe')=='path_chord_v2':
                orient=orient_path_chord
                variant.append('path_chord_v2')
            for i,curve in enumerate(curves):
                sketch(f'_Path{i+1}',[orient(curve,values)])
                sketches[-1]['role']='path'
            if kind=='variable_tube':
                # Keep corresponding loft seams on the same side in model space.
                # Native per-curve planes can reverse their local X axis.
                unit_tangents=[norm(tangent) for tangent in tangents]
                directions=[resolve(tangent,values) for tangent in unit_tangents]
                seed_axis=min(range(3),key=lambda axis:max(abs(t[axis]) for t in directions))
                seed=[scalar(1 if i==seed_axis else 0) for i in range(3)]
            stations = [(0,g['radius'])] if kind=='tube' else list(enumerate(g['radii']))
            for i,radius in stations:
                suffix='_Section' if kind=='tube' else f'_Section{i+1}'
                if kind=='variable_tube':
                    n=unit_tangents[i]
                    u=norm(sub(seed,scale(n,sum(a*b for a,b in zip(seed,n)))))
                    v=cross(n,u)
                    xyz=add(points[i],add(scale(u,radius*cos(T)),scale(v,radius*sin(T))))
                    sketch(suffix,[(xyz,0.,math.pi),(xyz,math.pi,2*math.pi)])
                    sketches[-1]['role']='section'
                elif g.get('recipe') == 'tube_cylinder_v3':
                    # A true circle swept along the straight path: an analytic cylinder with no
                    # entry in the equation manager. Every manager equation costs about 0.3 s in
                    # each full rebuild; here the radius is the end of an equation-driven
                    # construction line and the circle is tied to that point, so it follows the
                    # parameters natively and the length is the path itself.
                    variant.append('tube_cylinder_v3')
                    sketch(suffix,[([radius*T,scalar(0),scalar(0)],0.,1.)],mode='normal')
                    sketches[-1].update(path=name+'_Path1',origin=resolve(points[0],values),
                                        circle_through_tip=True)
                elif g.get('recipe') == 'tube_cylinder_v2':
                    # One native circle extruded along the axis: an analytic cylinder and two planar ends.
                    variant.append('tube_cylinder_v2')
                    sketch(suffix,[],mode='normal')
                    sketches[-1].update(path=name+'_Path1',origin=resolve(points[0],values),
                                        circle={'radius':resolve(radius,values),'role':'diameter'})
                    driven['diameter']=emit_dimension(2*radius,globals_map)
                    span=sub(points[1],points[0])
                    direction=resolve(span,values)
                    driven['length']=emit_length(span,values,globals_map)
                    extrusion={'value':math.sqrt(sum(v*v for v in direction)),'expression':driven['length'],
                               'direction':direction}
                else:
                    xyz=[radius*cos(T),radius*sin(T),scalar(0)]
                    sketch(suffix,[(xyz,0.,math.pi),(xyz,math.pi,2*math.pi)],mode='normal')
                    sketches[-1].update(path=name+'_Path1',origin=resolve(points[0],values))
        elif kind in {'dome','revolve','ellipsoid','rounded_prism','prism'}:
            plane='Front'          # Sketch plane of the profile.
            axis_line='vertical'   # Direction of the revolve centerline in the sketch.
            native_shape=None      # A native slot or circle instead of equation curves.
            moves=None             # Body placement after the feature: [(model axis, expression)].
            swept=None             # (outline segments, path) of a prism swept without manager equations.

            def prism_frame(center,build_axis,depth):
                """Model-space mapper for an outline point (a, b) at one end of a prism, and its axis path."""
                wide,tall={'Z':('X','Y'),'Y':('X','Z'),'X':('Z','Y')}[build_axis]
                base=dict(zip(AXES,center))
                def at(a,b,level):
                    value={build_axis:level,wide:base[wide]+a,tall:base[tall]+b}
                    return [value[k] for k in AXES]
                low,high=base[build_axis]-depth/2,base[build_axis]+depth/2
                return (lambda a,b:at(a,b,low)),(at(scalar(0),scalar(0),low),at(scalar(0),scalar(0),high))
            if kind=='rounded_prism':
                cx,cy,cz=g['center'];w,h,r=g['width']/2,g['height']/2,g['corner_radius']
                build_axis=g.get('axis','Z')
                if build_axis=='Z': plane='Front';sx,sy=cx,cy;moves=[('Z',cz)]
                elif build_axis=='Y': plane='Top';sx,sy=cx,-cz;moves=[('Y',cy)]
                else: plane='Right';sx,sy=-cz,cy;moves=[('X',cx)]
                if build_axis!='Z': variant.append('axis='+build_axis)
                vertical=resolve(h,values)>resolve(w,values)
                smaller=h if resolve(h,values)<=resolve(w,values) else w
                full_round=same(r,smaller)
                if not full_round and abs(resolve(r,values)-resolve(smaller,values))<=1e-9*max(1.,resolve(smaller,values)):
                    raise ProgramError('INVALID_ROUNDED_PRISM',
                        'corner_radius equals half the smaller side only at the current parameter values; '
                        'write it as exactly that half so the slot survives parameter changes',component=name)
                sweep=g.get('recipe')=='prism_sweep_v2'
                if sweep and not full_round:
                    # The profile is a planar 3D sketch at one end, swept along the axis line:
                    # no depth dimension and no body move, so nothing enters the equation manager.
                    place,path=prism_frame(g['center'],build_axis,g['depth'])
                    if symbolic(r)==0:
                        variant.append('box')
                        corners=[(w,h),(-w,h),(-w,-h),(w,-h)]
                        outline=[line(place(*p),place(*q)) for p,q in zip(corners,corners[1:]+corners[:1])]
                    elif abs(resolve(r,values))<1e-9:
                        raise ProgramError('INVALID_ROUNDED_PRISM',
                            'corner_radius is zero only at the current parameter values; use a literal 0 for a box',
                            component=name)
                    else:
                        a=w-r;b=h-r
                        c=(1-T*T)/(1+T*T);s=2*T/(1+T*T)
                        outline=[(place(dx+r*(ux*c-uy*s),dy+r*(uy*c+ux*s)),0.,1.)
                                 for dx,dy,ux,uy in [(a,b,1,0),(-a,b,0,1),(-a,-b,-1,0),(a,-b,0,-1)]]
                        outline+=[line(place(*p),place(*q))
                                  for p,q in [((a,h),(-a,h)),((-w,b),(-w,-b)),((-a,-h),(a,-h)),((w,-b),(w,b))]]
                    swept=(outline,path);curves=[];moves=[]
                    variant.append('sweep')
                elif symbolic(r)==0:
                    # A plain box: four straight edges.
                    variant.append('box')
                    corners=[(w,h),(-w,h),(-w,-h),(w,-h)]
                    curves=[line([sx+p[0],sy+p[1],scalar(0)],[sx+q[0],sy+q[1],scalar(0)])
                            for p,q in zip(corners,corners[1:]+corners[:1])]
                elif abs(resolve(r,values))<1e-9:
                    raise ProgramError('INVALID_ROUNDED_PRISM',
                        'corner_radius is zero only at the current parameter values; use a literal 0 for a box',
                        component=name)
                elif full_round:
                    # A stadium is two arcs and two lines; a square one is a circle. Both are drawn at the
                    # sketch origin with native entities and moved to the centre as a body.
                    curves=[]
                    moves=[(k,v) for k,v in zip(AXES,(cx,cy,cz))]
                    if same(w,h):
                        native_shape={'circle':{'radius':resolve(r,values),'role':'diameter'}}
                        variant.append('circle')
                    else:
                        longer=w if not vertical else h
                        native_shape={'slot':{'half_length':resolve(longer-r,values),'radius':resolve(r,values),
                                              'vertical':vertical}}
                        driven['slot_length']=emit_dimension(2*(longer-r),globals_map)
                        variant.append('slot_'+('vertical' if vertical else 'horizontal'))
                    driven['diameter']=emit_dimension(2*r,globals_map)
                else:
                    a=w-r;b=h-r
                    c=(1-T*T)/(1+T*T);s=2*T/(1+T*T)
                    corners=[(a,b,1,0),(-a,b,0,1),(-a,-b,-1,0),(a,-b,0,-1)]
                    curves=[]
                    for dx,dy,ux,uy in corners:
                        curves.append(([sx+dx+r*(ux*c-uy*s),sy+dy+r*(uy*c+ux*s),scalar(0)],0.,1.))
                    for p,q in [((a,h),(-a,h)),((-w,b),(-w,-b)),((-a,-h),(a,-h)),((w,-b),(w,b))]:
                        curves.append(line([sx+p[0],sy+p[1],scalar(0)],[sx+q[0],sy+q[1],scalar(0)]))
                cx=scalar(0)
                if swept is None:
                    extrusion={'value':resolve(g['depth'],values),'expression':emit_dimension(g['depth'],globals_map)}
            elif kind == 'prism' and g.get('recipe')=='prism_sweep_v2':
                build_axis=g.get('axis','Z')
                if build_axis!='Z': variant.append('axis='+build_axis)
                place,path=prism_frame(g['center'],build_axis,g['depth'])
                corners=[place(a,b) for a,b in g['points']]
                swept=([line(p,q) for p,q in zip(corners,corners[1:]+corners[:1])],path)
                curves=[];moves=[];cx=scalar(0)
                variant.append('sweep')
            elif kind == 'prism':
                cx,cy,cz=g['center']
                build_axis=g.get('axis','Z')
                # Outline (a, b) in sketch coordinates of the plane normal to the axis, signs included.
                if build_axis=='Z': plane='Front';to_plane=lambda a,b:[cx+a,cy+b,scalar(0)];moves=[('Z',cz)]
                elif build_axis=='Y': plane='Top';to_plane=lambda a,b:[cx+a,-(cz+b),scalar(0)];moves=[('Y',cy)]
                else: plane='Right';to_plane=lambda a,b:[-(cz+a),cy+b,scalar(0)];moves=[('X',cx)]
                if build_axis!='Z': variant.append('axis='+build_axis)
                outline=[to_plane(a,b) for a,b in g['points']]
                curves=[line(p,q) for p,q in zip(outline,outline[1:]+outline[:1])]
                cx=scalar(0)
                extrusion={'value':resolve(g['depth'],values),'expression':emit_dimension(g['depth'],globals_map)}
            elif kind == 'ellipsoid':
                rx,ry,rz = g['radii'];cx,cy,cz = g['center']
                ratio=sp.cancel(symbolic(rz)/symbolic(rx))
                if ratio.free_symbols:
                    raise ProgramError('ELLIPSOID_ASPECT_PARAMETER',
                        'Exact Z/X aspect must be constant; use approximate=True for independently driven radii with a native guided Sweep')
                scale_z=float(ratio)
                # Split at the equator: SW rejects the full rational half-ellipse.
                x=2*rx*T/(1+T*T);dy=ry*(1-T*T)/(1+T*T)
                curves=[([x,cy+dy,scalar(0)],0.,1.),([x,cy-dy,scalar(0)],0.,1.),
                        line([scalar(0),cy+ry,scalar(0)],[scalar(0),cy-ry,scalar(0)])]
            elif kind == 'dome':
                radius,height = g['radius'],g['height']
                signed=g.get('axis','Y');build_axis=signed[-1];sign=-1 if signed.startswith('-') else 1
                center=dict(zip(AXES,g['center']))
                cx,base,cz = g['center']
                if signed!='Y':
                    variant.append('axis='+signed)
                    base=center[build_axis]
                    moves=[(k,center[k]) for k in CROSS_AXES[build_axis]]
                R=(radius*radius+height*height)/(2*height)
                u=height/radius*T
                rise=R*(1-u*u)/(1+u*u)
                # (radius from the axis, coordinate along the axis); the default keeps its stored form.
                top=base+height if sign>0 else base-height
                crown=base+height-R+rise if sign>0 else base-height+R-rise
                profile_curves=[((scalar(0),base),(radius,base)),
                                ((2*R*u/(1+u*u),crown),None),
                                ((scalar(0),top),(scalar(0),base))]
            else:
                cx,cz = g['center_xz'];profile=g['profile']
                build_axis=g.get('axis','Y')
                at=lambda y:y
                if g.get('origin') is not None:
                    origin=dict(zip(AXES,g['origin']))
                    at=lambda y,start=origin[build_axis]:start+y
                    moves=[(k,origin[k]) for k in CROSS_AXES[build_axis]]
                    variant.append('axis='+build_axis+'+origin')
                elif build_axis!='Y':
                    moves=[]
                    variant.append('axis='+build_axis)
                if g.get('closed'):
                    variant.append('closed')
                    outline=[(r,at(y)) for y,r in profile]
                else:
                    outline=[(scalar(0),at(profile[0][0]))]+[
                        (r,at(y)) for y,r in profile]+[(scalar(0),at(profile[-1][0]))]
                outline.append(outline[0]);profile_curves=[]
                for a,b in zip(outline,outline[1:]):
                    if math.dist(resolve(list(a),values),resolve(list(b),values))>1e-8:
                        profile_curves.append((a,b))
            if kind in {'dome','revolve'}:
                # Map (radius, along) into the sketch that contains the build axis.
                if build_axis=='Y': to_sketch=lambda rad,along:[rad,along,scalar(0)]
                elif build_axis=='X': to_sketch=lambda rad,along:[along,rad,scalar(0)];axis_line='horizontal'
                else: to_sketch=lambda rad,along:[rad,-along,scalar(0)];plane='Top'
                curves=[(to_sketch(*a),0.,1.) if b is None else line(to_sketch(*a),to_sketch(*b))
                        for a,b in profile_curves]
            if swept is not None:
                sketch('_Path1',[orient_path_curve(line(*swept[1]),values)])
                sketches[-1]['role']='path'
                sketch('_Profile',swept[0])
            else:
                sketch('_Profile',curves,mode='2d',axis=kind not in {'rounded_prism','prism'})
            if swept is not None:
                pass
            elif plane!='Front':
                sketches[-1].update(plane=plane,frame_check=frame_check(plane))
            if axis_line!='vertical':
                sketches[-1]['axis_line']=axis_line
            if native_shape:
                sketches[-1].update(native_shape)
            for axis,value in (moves if moves is not None else [('X',cx),('Z',cz)]):
                expression=emit_dimension(value,globals_map)
                if expression != '0':
                    if abs(resolve(value,values)) < 1e-8:
                        raise ProgramError('NATIVE_PARAMETER_DOMAIN', 'A driven placement cannot start at zero; use literal zero for a fixed axis', component=name)
                    actual=resolve(value,values)
                    direction=1 if actual>0 else -1
                    placement[axis]={'expression':emit_dimension(value*direction,globals_map),
                                     'value':actual,'direction':direction}
        elif kind=='surface_patch':
            offset=resolve(g['offset'],values)
            if abs(offset)<1e-8 and symbolic(g['offset'])!=0:
                raise ProgramError('NATIVE_PARAMETER_DOMAIN','A driven surface offset cannot start at zero')
            direction=-1 if offset<0 else 1
            surface={'offset':abs(offset),'reverse':offset<0,'inward':g['inward'],
                     'offset_expression':emit_dimension(g['offset']*direction,globals_map),
                     'thickness':resolve(g['thickness'],values),
                     'thickness_expression':emit_dimension(g['thickness'],globals_map)}
        elif kind=='place':
            transform=placement_steps(name,g,values,globals_map)
        elif kind in {'fillet','chamfer'}:
            edge_feature={'kind':kind,'size':resolve(g['size'],values),'edges':g['edges']}
            driven['size']=emit_dimension(g['size'],globals_map)
            variant.append(kind+':'+repr(sorted(g['edges'].items())))
        elif kind=='pattern':
            # The direction (or axis) is a native line tied to the ends of an equation curve:
            # it follows the parameters by relation. A row adds one spacing dimension.
            if 'step' in g:
                sketch('_Step',[line([scalar(0)]*3,g['step'])])
                driven['spacing']=emit_length(g['step'],values,globals_map)
                pattern={'mode':'row','count':g['count'],
                         'spacing':math.sqrt(sum(v*v for v in resolve(g['step'],values)))}
                variant.append(f"row:{g['count']}")
            else:
                tip=[scalar(10) if k==g['axis'] else scalar(0) for k in AXES]
                sketch('_Axis',[line(g['center'],add(g['center'],tip))])
                pattern={'mode':'circle','count':g['count']}
                variant.append(f"circle:{g['axis']}:{g['count']}")
            sketches[-1]['chord']=True
        elif kind not in {'mirror','combine'}:
            raise ProgramError('UNSUPPORTED_SHAPE',kind)
        specs.append({'id':name,'kind':kind,'sketches':sketches,'placement':placement,
                      'seeds':g.get('seeds'),'plane':g.get('plane'),
                      **({'scale_z':scale_z} if scale_z is not None else {}),
                      **({'guided_ellipsoid':guided_ellipsoid} if guided_ellipsoid is not None else {}),
                      **({'anchor':anchor} if anchor is not None else {}),
                      **({'extrusion':extrusion} if extrusion is not None else {}),
                      **({'surface':surface,'consumes':g['seeds'][1:]} if surface is not None else {}),
                      **({'transform':transform,'consumes':g['seeds']} if transform is not None else {}),
                      **({'edge_feature':edge_feature,'consumes':g['seeds']} if edge_feature is not None else {}),
                      **({'pattern':pattern} if pattern is not None else {}),
                      **({'variant':'|'.join(variant)} if variant else {}),
                      **({'dimensions':driven} if driven else {}),
                      **({'spans':spans} if spans else {}),
                      **({'operation':g['operation'],'expected_bodies':g['expected_bodies']} if kind=='combine' else {})})
    native_names=list(dict.fromkeys(value.split('@')[-1] for value in dimensions.values()))
    for node in specs:
        native_names.append(node['id'])
        if node['kind']=='place':
            native_names.extend(s['feature'] for s in node['transform'] if s['feature']!=node['id'])
            if any(s['kind']=='rotate' for s in node['transform']):native_names.append(node['id']+'_Axes')
        if node['kind']=='surface_patch':
            native_names.extend([node['id']+'_Surface',node['id']+'_Thicken'])
        if node['kind']=='anchored_extrude':
            native_names.extend(node['id']+s for s in ['_Axis','_Anchor','_Frame'])
        if node['placement'] or (node['kind']=='ellipsoid' and not node.get('guided_ellipsoid')):
            native_names.append(node['id']+'_Volume')
        if node.get('guided_ellipsoid'):
            native_names.extend(node['id']+s for s in ['_Equator','_Half','_OtherHalf'])
        elif node['kind']=='ellipsoid' and node['placement']:native_names.append(node['id']+'_Scaled')
        if node['kind'] in {'tube','variable_tube'} and sum(s.get('role')=='path' for s in node['sketches'])>1:
            native_names.append(node['id']+'_Path')
        if node.get('spans')=='linear':
            native_names.extend(f"{node['id']}_Span{i+1}" for i in range(len(node['sketches'])//2))
        for s in node['sketches']:
            native_names.append(s['name'])
            if s['mode']=='normal':native_names.append(s['name']+'_Frame')
            for c in s['curves']:
                if any(len(expr)>2000 for expr in c['xyz']):
                    raise ProgramError('NATIVE_EXPRESSION_LIMIT',
                        'Native curve expressions must be at most 2000 characters after simplification',component=node['id'])
    if len({name.casefold() for name in native_names})!=len(native_names):
        raise ProgramError('NATIVE_NAME_COLLISION','Component names collide with generated native support names')
    return specs


def topology(specs):
    return [{'id':s['id'],'kind':s['kind'],'sketches':[
        (k['name'],k['mode'],k['axis'],len(k['curves'])) for k in s['sketches']],
        'placement_axes':[(k,s['placement'][k]['direction']) for k in sorted(s['placement'])],
        'seeds':s['seeds'],'plane':s['plane'],
        **({'variant':s['variant']} if s.get('variant') else {}),
        **({'ellipsoid_recipe':'guided_sweep_v1'} if s.get('guided_ellipsoid') else {}),
        **({'anchor_direction':s['anchor']['direction'], 'depth_sign':s['anchor']['depth_sign']} if s.get('anchor') else {}),
        **({'transform_steps':[(k['kind'],k.get('axis'),k.get('direction'),
                 [(axis,v['direction']) for axis,v in k.get('axes',{}).items()])
                 for k in s['transform']]} if s['kind']=='place' else {}),
        **({'surface_direction':(s['surface']['reverse'],s['surface']['inward'],s['surface']['offset']>0)} if s['kind']=='surface_patch' else {}),
        **({'operation':s['operation'],'expected_bodies':s['expected_bodies']} if s['kind']=='combine' else {})}
        for s in specs]
