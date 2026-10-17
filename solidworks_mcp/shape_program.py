"""Composable symbolic shape programs independent of COM and image tracing."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass


class ProgramError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


@dataclass(frozen=True)
class Expr:
    op: str
    args: tuple

    def _binary(self, op, other):
        return Expr(op, (self, scalar(other)))

    def __add__(self, other):
        return self._binary("add", other)

    __radd__ = __add__

    def __sub__(self, other):
        return self._binary("sub", other)

    def __rsub__(self, other):
        return scalar(other)._binary("sub", self)

    def __mul__(self, other):
        return self._binary("mul", other)

    __rmul__ = __mul__

    def __truediv__(self, other):
        return self._binary("div", other)

    def __rtruediv__(self, other):
        return scalar(other)._binary("div", self)

    def __neg__(self):
        return Expr("neg", (self,))

    def __bool__(self):
        raise ProgramError("SYMBOLIC_CONDITION", "Use model.value() in conditions")

    def evaluate(self, values):
        if self.op == "literal":
            result = self.args[0]
        elif self.op == "parameter":
            result = values[self.args[0]]
        else:
            a = [v.evaluate(values) for v in self.args]
            operations = {
                "add": lambda: a[0] + a[1], "sub": lambda: a[0] - a[1],
                "mul": lambda: a[0] * a[1], "div": lambda: a[0] / a[1],
                "neg": lambda: -a[0], "sin": lambda: math.sin(a[0]),
                "cos": lambda: math.cos(a[0]), "sqrt": lambda: math.sqrt(a[0]),
            }
            result = operations[self.op]()
        if not math.isfinite(result):
            raise ProgramError("INVALID_NUMBER", "A parameter expression is not finite")
        return result

    def serialize(self):
        return {"op": self.op, "args": [
            v.serialize() if isinstance(v, Expr) else v for v in self.args]}


def scalar(value):
    if isinstance(value, Expr):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProgramError("INVALID_NUMBER", "Expected a number or parameter expression")
    value = float(value)
    if not math.isfinite(value):
        raise ProgramError("INVALID_NUMBER", "Numbers must be finite")
    return Expr("literal", (value,))


def vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ProgramError("INVALID_POINT", "Model points must contain x, y, z in mm")
    return tuple(scalar(v) for v in value)


def encode(value):
    if isinstance(value, Expr):
        return value.serialize()
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    return value


def resolve(value, parameters):
    if isinstance(value, Expr):
        return value.evaluate(parameters)
    if isinstance(value, (tuple, list)):
        return [resolve(v, parameters) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, parameters) for k, v in value.items()}
    return value


AXES = ('X', 'Y', 'Z')
# Model axes of the two cross-section directions for a build axis (alphabetical order).
CROSS_AXES = {'X': ('Y', 'Z'), 'Y': ('X', 'Z'), 'Z': ('X', 'Y')}
# Section-curve recipes of sectioned_solid; a recipe id of another kind under the same name is ignored.
SECTION_RECIPES = {'rational_quarters_v2', 'rational_quarters', 'trig_halves'}


def build_axis(value, *, signed=False):
    """Validate a construction axis; a signed axis may point along the negative direction."""
    allowed = AXES + (('-X', '-Y', '-Z') if signed else ())
    if value not in allowed:
        raise ProgramError('INVALID_AXIS', 'axis must be one of ' + ', '.join(allowed))
    return value


def from_axis_frame(axis, along, a, b):
    """Model (x, y, z) of a point given along the build axis and in its two cross directions."""
    values = {axis: along, CROSS_AXES[axis][0]: a, CROSS_AXES[axis][1]: b}
    return tuple(values[k] for k in AXES)


def identifier(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", name):
        raise ProgramError("INVALID_NAME", "Names must use 1-48 ASCII letters, digits or underscores")
    return name


@dataclass(frozen=True)
class Component:
    name: str
    start: tuple | None = None
    end: tuple | None = None


@dataclass(frozen=True)
class FaceAnchor:
    """Deferred native ray/face attachment, never a sampled coordinate tuple."""
    host: str
    origin: tuple
    direction: tuple


def trig_pair(angle):
    """Keep exact cardinal directions free from floating-point residue."""
    a = scalar(angle)
    if a.op == 'literal':
        quarter = round(a.args[0] / (math.pi / 2))
        if abs(a.args[0] - quarter * math.pi / 2) < 1e-12:
            return scalar((0, 1, 0, -1)[quarter % 4]), scalar((1, 0, -1, 0)[quarter % 4])
    return Expr('sin', (a,)), Expr('cos', (a,))


@dataclass(frozen=True)
class Frame:
    """Right-handed extrinsic X, then Y, then Z rotation in radians."""
    origin: tuple
    rotation: tuple

    def direction(self, value):
        x, y, z = vector(value)
        sx, cx = trig_pair(self.rotation[0])
        sy, cy = trig_pair(self.rotation[1])
        sz, cz = trig_pair(self.rotation[2])
        y, z = cx*y-sx*z, sx*y+cx*z
        x, z = cy*x+sy*z, -sy*x+cy*z
        return (cz*x-sz*y, sz*x+cz*y, z)

    def point(self, value):
        return tuple(a+b for a, b in zip(self.origin, self.direction(value)))


class ShapeProgram:
    MAX_COMPONENTS = 512

    def __init__(self, overrides=None, recipe_versions=None):
        self.parameters = {}
        self.overrides = dict(overrides or {})
        self.nodes = []
        self._names = set()
        self._consumed = {}  # consumed component -> the component that consumed it
        self.evaluated_parameters = set()
        self._recipe_versions = dict(recipe_versions or {})

    def parameter(self, name, value, *, unit="mm"):
        identifier(name)
        if name in self.parameters:
            raise ProgramError("DUPLICATE_PARAMETER", name)
        if unit not in {"mm", "ratio"}:
            raise ProgramError("UNSUPPORTED_UNIT", "Parameters use mm or ratio")
        original = scalar(value).evaluate({})
        actual = scalar(self.overrides.get(name, original)).evaluate({})
        self.parameters[name] = {"value": actual, "default": original, "unit": unit}
        return Expr("parameter", (name,))

    def value(self, expression):
        def record(expr):
            if not isinstance(expr,Expr): return
            if expr.op=='parameter': self.evaluated_parameters.add(expr.args[0])
            else:
                for arg in expr.args: record(arg)
        record(expression)
        return scalar(expression).evaluate(self.values)

    @property
    def values(self):
        return {k: v["value"] for k, v in self.parameters.items()}

    def sin(self, value):
        return Expr("sin", (scalar(value),))

    def cos(self, value):
        return Expr("cos", (scalar(value),))

    def sqrt(self, value):
        return Expr("sqrt", (scalar(value),))

    def _add(self, name, kind, **geometry):
        identifier(name)
        if name in self._names:
            raise ProgramError("DUPLICATE_COMPONENT", name)
        if len(self.nodes) >= self.MAX_COMPONENTS:
            raise ProgramError(
                "PROGRAM_LIMIT", f"Shape component limit exceeded: attempted {len(self.nodes) + 1}, "
                f"maximum {self.MAX_COMPONENTS}.", limit_kind="shape_components",
                existing_components=len(self.nodes), attempted_components=len(self.nodes) + 1,
                max_components=self.MAX_COMPONENTS, component=name, component_kind=kind)
        self._names.add(name)
        self.nodes.append({"id": name, "kind": kind, "geometry": geometry})
        return Component(name)

    def _axis_options(self, axis, origin, center_xz, *, signed=False):
        """Optional construction axis and origin; defaults add nothing to the stored geometry."""
        options = {}
        if build_axis(axis, signed=signed) != 'Y':
            options['axis'] = axis
        if origin is not None:
            if any(scalar(v).op != 'literal' or scalar(v).args[0] != 0 for v in center_xz):
                raise ProgramError('INVALID_POINT', 'Use either origin or center_xz, not both')
            options['origin'] = vector(origin)
        elif axis != 'Y' and any(scalar(v).op != 'literal' or scalar(v).args[0] != 0 for v in center_xz):
            raise ProgramError('INVALID_POINT', 'center_xz belongs to the default Y axis; use origin with axis')
        return options

    def _recipe(self, name, prefix, default):
        """A saved component keeps its native recipe; recipe ids are namespaced by kind."""
        stored = self._recipe_versions.get(name)
        return stored if isinstance(stored, str) and stored.startswith(prefix) else default

    def sectioned_solid(self, name, sections, *, center_xz=(0, 0), axis='Y', origin=None, spans='smooth'):
        """Create an elliptical solid from (height, radius, radius) stations along an axis.

        Default axis Y: sections are (y, rx, rz). Axis X: (x, ry, rz). Axis Z: (z, rx, ry).
        spans='linear' joins neighbouring sections with ruled faces instead of one smooth loft.
        """
        if not 2 <= len(sections) <= 24 or any(len(s) != 3 for s in sections):
            raise ProgramError("INVALID_SECTIONS", "Use 2-24 (height, radius, radius) sections")
        if len(center_xz) != 2:
            raise ProgramError("INVALID_POINT", "center_xz must contain two numbers")
        if spans not in {'smooth', 'linear'}:
            raise ProgramError('INVALID_SPANS', "spans must be 'smooth' or 'linear'")
        stored = self._recipe_versions.get(name)
        options = self._axis_options(axis, origin, center_xz)
        # Two sections are one ruled span already; the flag would only add native features.
        if spans == 'linear' and len(sections) > 2:
            options['spans'] = 'linear'
        return self._add(name, "sectioned_solid",
                         sections=[tuple(scalar(v) for v in s) for s in sections],
                         center_xz=tuple(scalar(v) for v in center_xz),
                         profile_representation=(stored if stored in SECTION_RECIPES
                                                 else 'rational_quarters_v2'), **options)

    def tube(self, name, points, radius):
        """Create a circular native sweep along a spatial interpolating path."""
        if not 2 <= len(points) <= 24:
            raise ProgramError("INVALID_PATH", "Use 2-24 spatial path points")
        points = tuple(vector(p) for p in points)
        options = {}
        if len(points) == 2:
            # A straight tube is an analytic cylinder. Parts saved before this recipe keep the sweep.
            recipe = self._recipe(name, 'tube_', 'tube_cylinder_v3')
            if recipe != 'tube_sweep_v1':
                options['recipe'] = recipe
        else:
            options.update(self._path_recipe(name))
        self._add(name, "tube", points=points, radius=scalar(radius), **options)
        return Component(name, points[0], points[-1])

    def _path_recipe(self, name):
        """New interpolated paths draw each segment in the direction SolidWorks solves without a warning."""
        recipe = self._recipe(name, 'path_', 'path_chord_v2')
        return {} if recipe == 'path_v1' else {'recipe': recipe}

    def dome(self, name, radius, height, *, center=(0, 0, 0), axis='Y'):
        """Create a spherical cap; center is the center of its flat base, axis points to its apex."""
        options = {'axis': axis} if build_axis(axis, signed=True) != 'Y' else {}
        return self._add(name, "dome", radius=scalar(radius),
                         height=scalar(height), center=vector(center), **options)

    def ellipsoid(self, name, radii, *, center=(0, 0, 0), approximate=False):
        """Use exact constant aspect, or opt into a native guided approximation."""
        if not isinstance(approximate, bool):
            raise ProgramError('INVALID_APPROXIMATION', 'approximate must be a boolean')
        options = {'approximate': True} if approximate else {}
        return self._add(name, 'ellipsoid', radii=vector(radii), center=vector(center), **options)

    def rounded_prism(self, name, width, height, depth, corner_radius, *, center=(0, 0, 0), axis='Z'):
        """Extrude a rounded rectangle symmetrically along an axis.

        Axis Z (default): width along X, height along Y. Axis Y: width X, height Z.
        Axis X: width Z, height Y. corner_radius = min(width, height)/2 gives a slot
        (stadium) with true cylindrical ends, or a cylinder when width == height.
        """
        options = {'axis': axis} if build_axis(axis) != 'Z' else {}
        options.update(self._prism_recipe(name))
        return self._add(name,'rounded_prism',width=scalar(width),height=scalar(height),
                         depth=scalar(depth),corner_radius=scalar(corner_radius),center=vector(center),
                         **options)

    def _prism_recipe(self, name):
        """New prisms are swept profiles without equation-manager entries; saved ones stay extrusions."""
        recipe = self._recipe(name, 'prism_', 'prism_sweep_v2')
        return {} if recipe == 'prism_extrude_v1' else {'recipe': recipe}

    def prism(self, name, points, depth, *, center=(0, 0, 0), axis='Z'):
        """Extrude a closed polygon symmetrically along an axis.

        points are (a, b) pairs relative to center in the profile plane; the outline closes
        itself. Axis Z (default): a along X, b along Y. Axis Y: a along X, b along Z.
        Axis X: a along Z, b along Y.
        """
        if not isinstance(points, (list, tuple)) or not 3 <= len(points) <= 64 or any(
                not isinstance(q, (list, tuple)) or len(q) != 2 for q in points):
            raise ProgramError('INVALID_PROFILE', 'Use 3-64 (a, b) outline points')
        options = {'axis': axis} if build_axis(axis) != 'Z' else {}
        options.update(self._prism_recipe(name))
        return self._add(name, 'prism', points=[tuple(scalar(v) for v in q) for q in points],
                         depth=scalar(depth), center=vector(center), **options)

    def _edge_selector(self, edges):
        """Edge sets that do not depend on native face numbering."""
        if edges == 'all':
            return {'all': True}
        if isinstance(edges, dict) and set(edges) == {'planar_loop'}:
            loop = edges['planar_loop']
            if (not isinstance(loop, dict) or set(loop) != {'axis', 'side'} or
                    loop['axis'] not in AXES or loop['side'] not in {'min', 'max'}):
                raise ProgramError('INVALID_EDGES', "planar_loop is {'axis': 'X'|'Y'|'Z', 'side': 'min'|'max'}")
            return {'planar_loop': {'axis': loop['axis'], 'side': loop['side']}}
        if isinstance(edges, dict) and set(edges) == {'between'}:
            pair = edges['between']
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ProgramError('INVALID_EDGES', 'between names two components')
            names = [c.name if isinstance(c, Component) else c for c in pair]
            if names[0] == names[1] or any(n not in self._names for n in names):
                raise ProgramError('INVALID_EDGES', 'between needs two distinct existing components')
            return {'between': names}
        raise ProgramError('INVALID_EDGES',
            "edges is 'all', {'planar_loop': {'axis', 'side'}} or {'between': [A, B]}")

    def _edge_feature(self, kind, name, component, size, edges):
        seeds = self._seeds([component])
        selector = self._edge_selector(edges)
        for owner in selector.get('between', []):
            # The joined faces must have reached this body through its dependencies.
            if owner != seeds[0] and not self._descends(seeds[0], owner):
                raise ProgramError('INVALID_EDGES', f'{owner} is not part of {seeds[0]}')
        result = self._add(name, kind, seeds=seeds, size=scalar(size), edges=selector)
        self._consumed[seeds[0]] = name
        return result

    def _descends(self, component, ancestor):
        node = next(n for n in self.nodes if n['id'] == component)
        seeds = node['geometry'].get('seeds') or []
        return ancestor in seeds or any(self._descends(seed, ancestor) for seed in seeds)

    def fillet(self, name, component, radius, *, edges='all'):
        """Round selected edges with a native constant-radius Fillet; consumes the component."""
        return self._edge_feature('fillet', name, component, radius, edges)

    def chamfer(self, name, component, distance, *, edges='all'):
        """Bevel selected edges with a native equal-distance Chamfer; consumes the component."""
        return self._edge_feature('chamfer', name, component, distance, edges)

    def face_anchor(self, host, *, origin, direction):
        """First positive ray hit; a native plane follows the actual host face."""
        key = self._seeds([host])[0]
        direction = vector(direction)
        if any(v.op != 'literal' for v in direction):
            raise ProgramError('ANCHOR_DIRECTION_DOMAIN', 'Anchor direction must be a constant vector')
        length = math.hypot(*resolve(direction, {}))
        if length < 1e-10:
            raise ProgramError('ANCHOR_DIRECTION_DOMAIN', 'Anchor direction must be nonzero')
        return FaceAnchor(key, vector(origin), tuple(scalar(v.args[0]/length) for v in direction))

    def anchored_extrude(self, name, anchor, profile, depth):
        """Extrude a circle/ellipse or rounded rectangle in a native anchor plane."""
        if not isinstance(anchor, FaceAnchor):
            raise ProgramError('INVALID_FACE_ANCHOR', 'Use model.face_anchor()')
        seeds = self._seeds([anchor.host])
        if not isinstance(profile, dict) or len(profile) != 1:
            raise ProgramError('INVALID_ANCHOR_PROFILE', 'Use {circle: radius}, {ellipse: (rx,ry)} or {rounded_rectangle: (width,height,radius)}')
        kind, values = next(iter(profile.items()))
        if kind == 'circle':
            values = [values]
        elif kind not in {'ellipse', 'rounded_rectangle'} or not isinstance(values, (list, tuple)) or len(values) != (2 if kind == 'ellipse' else 3):
            raise ProgramError('INVALID_ANCHOR_PROFILE', 'Unsupported profile or wrong profile dimensions')
        return self._add(name, 'anchored_extrude', seeds=seeds, origin=anchor.origin,
                         direction=anchor.direction, profile_kind=kind,
                         profile_values=[scalar(v) for v in values], depth=scalar(depth))

    def variable_tube(self, name, points, radii):
        """Loft circular stations normal to a native spatial centerline."""
        if not 2 <= len(points) <= 24 or len(points) != len(radii):
            raise ProgramError('INVALID_SECTIONS', 'Use 2-24 points with one radius per point')
        points = tuple(vector(p) for p in points)
        self._add(name, 'variable_tube', points=points, radii=[scalar(r) for r in radii],
                  **(self._path_recipe(name) if len(points) > 2 else {}))
        return Component(name, points[0], points[-1])

    def frame(self, *, origin=(0, 0, 0), rotation=(0, 0, 0)):
        """Map local points/directions or place bodies; angles are radians."""
        return Frame(vector(origin), vector(rotation))

    def place(self, name, components, *, frame):
        """Consume explicit components with native rotation and translation."""
        if not isinstance(frame, Frame):
            raise ProgramError('INVALID_FRAME', 'Use model.frame()')
        items = [components] if isinstance(components, (str, Component)) else components
        seeds = self._seeds(items)
        self._add(name, 'place', seeds=seeds, origin=frame.origin, rotation=frame.rotation)
        self._consumed.update(dict.fromkeys(seeds, name))
        if len(items) == 1 and isinstance(items[0], Component) and items[0].start is not None:
            return Component(name, frame.point(items[0].start), frame.point(items[0].end))
        return Component(name)

    def _seeds(self, components):
        if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
            raise ProgramError('INVALID_SEEDS',
                'components must be a sequence of components or names; use [seed] for one component',
                argument='components')
        for index, component in enumerate(components):
            if not isinstance(component, (Component, str)):
                raise ProgramError('INVALID_SEEDS',
                    f'components[{index}] must be a component or its name; for example [seed, "Other"]',
                    argument='components', index=index)
        seeds = [c.name if isinstance(c, Component) else c for c in components]
        if not seeds or len(seeds) != len(set(seeds)):
            raise ProgramError('INVALID_SEEDS', 'Specify distinct existing components')
        if any(s not in self._names for s in seeds):
            raise ProgramError('UNKNOWN_COMPONENT', 'Dependencies must already exist')
        for seed in seeds:
            if seed in self._consumed:
                raise ProgramError('COMPONENT_CONSUMED',
                    f'{seed} is consumed by {self._consumed[seed]}; use that resulting component',
                    component=seed, consumed_by=self._consumed[seed])
        return seeds

    def combine(self, name, components, *, operation='union', expected_bodies=1):
        """Consume explicit bodies; difference uses the first component as target."""
        if operation not in {'union', 'difference', 'intersection'}:
            raise ProgramError('INVALID_BOOLEAN', 'Use union, difference or intersection')
        seeds = self._seeds(components)
        if len(seeds) < 2:
            raise ProgramError('INVALID_SEEDS', 'Boolean operations need at least two components')
        if isinstance(expected_bodies, bool) or not isinstance(expected_bodies, int) or not 1 <= expected_bodies <= 32:
            raise ProgramError('INVALID_BODY_COUNT', 'Expected result count must be 1-32')
        result = self._add(name, 'combine', seeds=seeds, operation=operation,
                           expected_bodies=expected_bodies)
        self._consumed.update(dict.fromkeys(seeds, name))
        return result

    def surface_point(self, host, *, fraction=.5, angle=0):
        """Symbolic analytic surface point; fraction is height and angle 0 is +Z."""
        key=self._seeds([host])[0]
        return self._surface_point(key, scalar(fraction), scalar(angle))

    def _surface_point(self, key, t, a):
        node=next(n for n in self.nodes if n['id']==key);g=node['geometry']
        if not 0<=t.evaluate(self.values)<=1:
            raise ProgramError('INVALID_SURFACE_ANCHOR','fraction must be between 0 and 1')
        if node['kind']=='place' and len(g['seeds'])==1:
            return Frame(g['origin'],g['rotation']).point(self._surface_point(g['seeds'][0],t,a))
        sine,cosine=trig_pair(a)
        if node['kind']=='ellipsoid':
            if g.get('approximate'):
                raise ProgramError('APPROXIMATE_SURFACE_ANCHOR',
                    'A guided ellipsoid has no exact analytic surface anchor; use native surface_patch for a fitted detail')
            rx,ry,rz=g['radii'];cx,cy,cz=g['center'];v=2*t-1
            radial=self.sqrt(1-v*v)
            return (cx+rx*radial*sine,cy+ry*v,cz+rz*radial*cosine)
        if g.get('axis') or g.get('origin') is not None:
            raise ProgramError('UNSUPPORTED_SURFACE_ANCHOR',
                'surface_point supports the default Y axis; use surface_patch or explicit coordinates')
        if node['kind']=='dome':
            r,h=g['radius'],g['height'];cx,base,cz=g['center']
            R=(r*r+h*h)/(2*h);dy=h*(t-1)
            radial=self.sqrt(-dy*(2*R+dy))
            return (cx+radial*sine,base+h*t,cz+radial*cosine)
        if node['kind']=='sectioned_solid' and len(g['sections'])==2:
            (y0,rx0,rz0),(y1,rx1,rz1)=g['sections']
        elif node['kind']=='revolve' and len(g['profile'])==2:
            (y0,rx0),(y1,rx1)=g['profile'];rz0,rz1=rx0,rx1
        else:
            raise ProgramError('UNSUPPORTED_SURFACE_ANCHOR',
                'Use a two-station sectioned_solid/revolve, dome, ellipsoid, or their single-seed placement')
        cx,cz=g['center_xz'];rx=rx0+(rx1-rx0)*t;rz=rz0+(rz1-rz0)*t
        return (cx+rx*sine,y0+(y1-y0)*t,cz+rz*cosine)

    def path_point(self, host, *, segment=0, fraction=.5):
        """Point on the actual declared Hermite centerline, not its polygon."""
        return self._path_anchor(self._seeds([host])[0], segment, scalar(fraction), False)

    def path_tangent(self, host, *, segment=0, fraction=.5):
        """Unit tangent of the declared Hermite centerline."""
        return self._path_anchor(self._seeds([host])[0], segment, scalar(fraction), True)

    def _path_anchor(self, key, segment, t, tangent):
        node=next(n for n in self.nodes if n['id']==key);g=node['geometry']
        if node['kind']=='place' and len(g['seeds'])==1:
            f=Frame(g['origin'],g['rotation'])
            p=self._path_anchor(g['seeds'][0],segment,t,tangent)
            return f.direction(p) if tangent else f.point(p)
        if node['kind'] not in {'tube','variable_tube'}:
            raise ProgramError('UNSUPPORTED_PATH_ANCHOR','Use tube, variable_tube or their single-seed placement')
        p=g['points']
        if isinstance(segment,bool) or not isinstance(segment,int) or not 0<=segment<len(p)-1 or not 0<=t.evaluate(self.values)<=1:
            raise ProgramError('INVALID_PATH_ANCHOR','Use an existing integer segment and fraction in [0,1]')
        i=segment;a,b=p[i],p[i+1]
        u=tuple(y-x for x,y in zip(p[0],p[1])) if i==0 else tuple((y-x)/2 for x,y in zip(p[i-1],p[i+1]))
        v=tuple(y-x for x,y in zip(p[-2],p[-1])) if i==len(p)-2 else tuple((y-x)/2 for x,y in zip(p[i],p[i+2]))
        if tangent:
            result=tuple(x*(6*t*t-6*t)+y*(-6*t*t+6*t)+du*(3*t*t-4*t+1)+dv*(3*t*t-2*t) for x,y,du,dv in zip(a,b,u,v))
            length=self.sqrt(sum(x*x for x in result))
            if length.evaluate(self.values)<1e-10:
                raise ProgramError('DEGENERATE_PATH_ANCHOR','The requested path tangent is zero')
            return tuple(x/length for x in result)
        return tuple(x*(2*t*t*t-3*t*t+1)+y*(-2*t*t*t+3*t*t)+du*(t*t*t-2*t*t+t)+dv*(t*t*t-t*t) for x,y,du,dv in zip(a,b,u,v))

    def surface_patch(self, name, host, mask, thickness, *, offset=0, inward=False):
        """Offset native curved host faces, thicken, and clip to a solid mask."""
        seeds=self._seeds([host,mask])
        host_node=next(n for n in self.nodes if n['id']==seeds[0])
        if host_node['kind'] not in {'sectioned_solid','revolve','dome','ellipsoid'}:
            raise ProgramError('UNSUPPORTED_SURFACE_HOST',
                'Use a sectioned_solid, revolve, dome or ellipsoid host before Boolean consumption')
        if not isinstance(inward,bool):
            raise ProgramError('INVALID_DIRECTION','inward must be a boolean')
        result=self._add(name,'surface_patch',seeds=seeds,thickness=scalar(thickness),
                         offset=scalar(offset),inward=inward)
        self._consumed[seeds[1]] = name
        return result

    def revolve(self, name, profile, *, center_xz=(0, 0), axis='Y', origin=None, closed=False):
        """Revolve a sparse (height, radius) profile about an axis through origin.

        Heights run along the axis from origin. The default is the model Y axis at center_xz.
        closed=True treats the profile as a closed outline that need not touch the axis:
        a ring, a bushing, a torus-like band.
        """
        if not 2 <= len(profile) <= 32 or any(len(p) != 2 for p in profile):
            raise ProgramError("INVALID_PROFILE", "Use 2-32 (height, radius) stations")
        if len(center_xz) != 2:
            raise ProgramError("INVALID_POINT", "center_xz must contain two numbers")
        if not isinstance(closed, bool):
            raise ProgramError('INVALID_PROFILE', 'closed must be a boolean')
        if closed and len(profile) < 3:
            raise ProgramError('INVALID_PROFILE', 'A closed profile needs at least three points')
        return self._add(name, "revolve",
                         profile=[tuple(scalar(v) for v in p) for p in profile],
                         center_xz=tuple(scalar(v) for v in center_xz),
                         **self._axis_options(axis, origin, center_xz),
                         **({'closed': True} if closed else {}))

    def mirror(self, name, components, *, plane="Right"):
        if plane not in {"Front", "Top", "Right"}:
            raise ProgramError("INVALID_PLANE", "Mirror uses Front, Top or Right")
        seeds = self._seeds(components)
        return self._add(name, "mirror", seeds=seeds, plane=plane)

    def pattern(self, name, components, *, count, step=None, axis=None, center=(0, 0, 0)):
        """One native body pattern; like mirror it copies its input, so the component holds the copies."""
        if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 100:
            raise ProgramError('INVALID_PATTERN', 'count is a constant integer from 2 to 100, the seed included')
        if (step is None) == (axis is None):
            raise ProgramError('INVALID_PATTERN',
                               'Give step=(dx, dy, dz) for a row or axis= (with center=) for a full circle')
        seeds = self._seeds(components)
        if step is not None:
            return self._add(name, 'pattern', seeds=seeds, count=count, step=vector(step))
        if axis not in AXES:
            raise ProgramError('INVALID_PATTERN', 'axis is X, Y or Z')
        return self._add(name, 'pattern', seeds=seeds, count=count, axis=axis, center=vector(center))

    def compile(self):
        unknown = sorted(set(self.overrides) - set(self.parameters))
        if unknown:
            raise ProgramError("UNKNOWN_PARAMETER", "Unknown parameter overrides", names=unknown)
        if not self.nodes:
            raise ProgramError("EMPTY_PROGRAM", "The program creates no shapes")
        resolved = []
        body_counts = {}
        for node in self.nodes:
            g = resolve(node["geometry"], self.values)
            kind = node["kind"]
            count = 1
            if kind in {'mirror','place'}: count = sum(body_counts[s] for s in g['seeds'])
            elif kind == 'pattern':
                count = sum(body_counts[s] for s in g['seeds']) * (g['count'] - 1)
                if 'step' in g and sum(v*v for v in g['step']) < 1e-12:
                    raise ProgramError('INVALID_PATTERN', 'The pattern step has no length', component=node['id'])
            elif kind in {'fillet','chamfer'}:
                if body_counts[g['seeds'][0]] != 1:
                    raise ProgramError('INVALID_EDGE_SCOPE', 'fillet and chamfer need a component with one body')
                if g['size'] <= 0:
                    raise ProgramError('INVALID_RADIUS', node['id'])
            elif kind == 'combine':
                if g['operation']=='difference' and body_counts[g['seeds'][0]]!=1:
                    raise ProgramError('INVALID_BOOLEAN_TARGET','Difference requires one target body')
                count=g['expected_bodies']
            elif kind=='surface_patch':
                if any(body_counts[s]!=1 for s in g['seeds']):
                    raise ProgramError('INVALID_SURFACE_SCOPE','Host and mask must each have one body')
                if g['thickness']<=0:
                    raise ProgramError('INVALID_THICKNESS','Patch thickness must be positive')
            elif kind == 'anchored_extrude':
                if body_counts[g['seeds'][0]] != 1:
                    raise ProgramError('ANCHOR_BODY_SCOPE', 'An anchor host must own exactly one body')
                if min(g['profile_values']) <= 0 or abs(g['depth']) < 1e-5:
                    raise ProgramError('INVALID_ANCHOR_PROFILE', 'Profile sizes must be positive and depth must be nonzero')
                if g['profile_kind'] == 'rounded_rectangle' and g['profile_values'][2] >= min(g['profile_values'][:2])/2:
                    raise ProgramError('INVALID_ANCHOR_PROFILE',
                        'Corner radius must be less than half the smaller size; for a full-round end use a circle '
                        'profile or a rounded_prism slot')
            body_counts[node['id']]=count
            if kind == "sectioned_solid":
                if any(rx <= 0 or rz <= 0 for _, rx, rz in g["sections"]):
                    raise ProgramError("INVALID_RADIUS", node["id"])
                self._ordered([s[0] for s in g["sections"]])
            elif kind == "revolve":
                if g.get('closed'):
                    simple_outline([(r, h) for h, r in g['profile']], node['id'])
                else:
                    self._ordered([s[0] for s in g["profile"]])
                if any(r < 0 for _, r in g["profile"]) or not any(r > 0 for _, r in g["profile"]):
                    raise ProgramError("INVALID_RADIUS", node["id"])
            elif kind == 'prism':
                if g['depth'] <= 0:
                    raise ProgramError('INVALID_PROFILE', 'Prism depth must be positive')
                simple_outline(g['points'], node['id'])
            elif kind in {'tube', 'variable_tube'}:
                radii = [g['radius']] if kind == 'tube' else g['radii']
                if any(r <= 0 for r in radii):
                    raise ProgramError("INVALID_RADIUS", node["id"])
                if any(math.dist(a, b) < 1e-5 for a, b in zip(g["points"], g["points"][1:])):
                    raise ProgramError("DEGENERATE_PATH", node["id"])
                for a,b,c in zip(g['points'],g['points'][1:],g['points'][2:]):
                    if math.dist(a,c) < 1e-5:
                        raise ProgramError('DEGENERATE_PATH', 'Path doubles back with a zero tangent')
            elif kind == 'ellipsoid':
                if any(r <= 0 for r in g['radii']):
                    raise ProgramError('INVALID_RADIUS', node['id'])
            elif kind == 'rounded_prism':
                limit = min(g['width'],g['height'])/2
                # Equality is the slot (stadium) profile; allow for float noise in the expressions.
                if min(g['width'],g['height'],g['depth']) <= 0 or not 0 <= g['corner_radius'] <= limit*(1+1e-9):
                    raise ProgramError('INVALID_ROUNDED_PRISM',
                        'Sizes must be positive and 0 <= corner_radius <= min(width,height)/2')
            elif kind == "dome":
                if not (0 < g["height"] <= g["radius"]):
                    raise ProgramError("INVALID_DOME", "Spherical cap requires 0 < height <= radius")
            numbers = []
            def collect(v):
                if isinstance(v, (int, float)):
                    numbers.append(abs(v))
                elif isinstance(v, (list, tuple)):
                    for item in v:
                        collect(item)
            collect(list(g.values()))
            if numbers and max(numbers) > 1e6:
                raise ProgramError("MODEL_SIZE_LIMIT", "Model coordinates exceed 1 km")
            resolved.append({**node, "geometry": g})
        source_graph = encode({"parameters": self.parameters, "nodes": self.nodes})
        digest = hashlib.sha256(json.dumps(
            source_graph, sort_keys=True, allow_nan=False).encode()).hexdigest()
        return {"schema_version": 1, "unit": "mm", "up_axis": "Y",
                "parameters": self.parameters, "nodes": resolved,
                "source_graph": source_graph, "program_hash": digest,
                "native_parameter_bindings": False,
                "evaluated_parameters":sorted(self.evaluated_parameters)}

    @staticmethod
    def _ordered(values):
        if any(b <= a for a, b in zip(values, values[1:])):
            raise ProgramError("SECTION_ORDER", "Heights must increase strictly")


def apply_patch(source, patch):
    """Apply ordered {old, new} replacements; every old must match exactly once."""
    if not isinstance(source, str):
        raise ProgramError('INVALID_PATCH', 'There is no embedded program text to patch')
    if not isinstance(patch, (list, tuple)) or not 1 <= len(patch) <= 64:
        raise ProgramError('INVALID_PATCH', 'patch must be a list of 1-64 {old, new} replacements')
    for index, item in enumerate(patch):
        if (not isinstance(item, dict) or set(item) != {'old', 'new'} or
                not isinstance(item['old'], str) or not isinstance(item['new'], str) or not item['old']):
            raise ProgramError('INVALID_PATCH', 'Each replacement needs a non-empty string old and a string new',
                               index=index)
        found = source.count(item['old'])
        if found == 0:
            raise ProgramError('PATCH_NOT_FOUND', 'old text does not occur in the program at this step',
                               index=index, old=item['old'][:200])
        if found > 1:
            raise ProgramError('PATCH_NOT_UNIQUE', 'old text occurs more than once; add surrounding context',
                               index=index, old=item['old'][:200], matches=found)
        source = source.replace(item['old'], item['new'], 1)
    return source


def changed_line_count(before, after):
    """Lines of the new text that differ from the old one, deletions included."""
    import difflib
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != 'equal')


def simple_outline(points, name):
    """Reject a closed outline with repeated points, crossing edges or no area; before any CAD call."""
    count = len(points)
    edges = [(points[i], points[(i+1) % count]) for i in range(count)]
    if any(math.dist(a, b) < 1e-6 for a, b in edges):
        raise ProgramError('INVALID_PROFILE', 'Outline points must be distinct', component=name)
    area = sum(a[0]*b[1] - b[0]*a[1] for a, b in edges) / 2
    if abs(area) < 1e-9:
        raise ProgramError('INVALID_PROFILE', 'The outline encloses no area', component=name)

    def side(p, q, r):
        return (q[0]-p[0])*(r[1]-p[1]) - (q[1]-p[1])*(r[0]-p[0])
    for i in range(count):
        for j in range(i+1, count):
            if j == i or (j+1) % count == i or (i+1) % count == j:
                continue        # Neighbouring edges share a vertex by construction.
            (a, b), (c, d) = edges[i], edges[j]
            if side(a, b, c)*side(a, b, d) < 0 and side(c, d, a)*side(c, d, b) < 0:
                raise ProgramError('INVALID_PROFILE', 'The outline crosses itself', component=name)


def bounded_range(*args):
    result = range(*args)
    if len(result) > 10000:
        raise ProgramError("PROGRAM_LIMIT", "range() is limited to 10000 entries")
    return result


def run_program(code, parameters=None, *, recipe_versions=None):
    """Run trusted local modeling code with accidental-I/O and loop guards.

    This is not an adversarial security sandbox. No COM object is exposed.
    Compilation and all validation finish before any CAD mutation begins.
    """
    if not isinstance(code, str) or len(code) > 150000:
        raise ProgramError("PROGRAM_LIMIT", "Code must be a string below 150000 characters")
    tree = ast.parse(code, filename="<shape_program>")
    forbidden = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.While,
                 ast.Global, ast.Nonlocal, ast.With, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, forbidden):
            raise ProgramError("PROGRAM_SYNTAX", f"{type(node).__name__} is not supported")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ProgramError("PROGRAM_SYNTAX", "Private attributes are not accessible")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ProgramError("PROGRAM_SYNTAX", "Private names are not accessible")
    model = ShapeProgram(parameters, recipe_versions)
    stream = io.StringIO()
    def program_print(*args, **kwargs):
        if "file" in kwargs:
            raise ProgramError("PROGRAM_SYNTAX", "print cannot write to files")
        # Do not redirect process-wide stdout: it carries the MCP transport
        # and may also be used by another worker thread.
        if stream.tell() < 8000:
            print(*args, file=stream, **kwargs)
    safe = dict(range=bounded_range, len=len, min=min, max=max, abs=abs,
                round=round, enumerate=enumerate, zip=zip, sum=sum,
                float=float, int=int, bool=bool, str=str, list=list,
                tuple=tuple, dict=dict, print=program_print)
    namespace = {"__builtins__": safe, "model": model, "pi": math.pi,
                 "sin": model.sin, "cos": model.cos, "sqrt": model.sqrt}
    remaining = 50000
    def trace(frame, event, arg):
        nonlocal remaining
        if frame.f_code.co_filename == "<shape_program>" and event == "line":
            remaining -= 1
            if remaining <= 0:
                raise ProgramError("PROGRAM_LIMIT", "Program exceeded 50000 line steps")
        return trace
    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        exec(compile(tree, "<shape_program>", "exec"), namespace)
    finally:
        sys.settrace(previous)
    result = model.compile()
    result["stdout"] = stream.getvalue()[:8000]
    return result
