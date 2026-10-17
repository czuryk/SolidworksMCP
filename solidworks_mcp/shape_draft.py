"""COM-free draft of a shape program: predicted extents and silhouettes.

A native build costs minutes; this costs milliseconds. Every primitive is
sampled from the same formulas the native recipes use and kept as convex
pieces, so extents come from points and a silhouette is the union of the
projected convex hulls. Cuts are not subtracted: a difference keeps the
extents of its target, and components whose shape depends on native faces
(surface patches, anchored extrusions) are flagged `estimated`.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .shape_program import AXES, CROSS_AXES, ProgramError, from_axis_frame
from .shape_limits import MAX_DRAFT_POINTS

RING = 64          # Samples per circle; includes the four axis-extreme points.
VIEWS = {          # Screen (right, up) as (model axis, sign), matching the native standard views.
    'front': (('X', 1), ('Y', 1)), 'back': (('X', -1), ('Y', 1)),
    'right': (('Z', -1), ('Y', 1)), 'left': (('Z', 1), ('Y', 1)),
    'top': (('X', 1), ('Z', -1)), 'bottom': (('X', 1), ('Z', 1)),
}
MIRROR_AXIS = {'Right': 0, 'Top': 1, 'Front': 2}
COLORS = [(126, 166, 214), (236, 171, 119), (144, 198, 149), (214, 139, 150), (176, 155, 209),
          (203, 189, 120), (124, 197, 196), (190, 190, 190)]


def _unit(vector):
    vector = np.asarray(vector, float)
    length = np.linalg.norm(vector)
    if length < 1e-12:
        raise ProgramError('DEGENERATE_PATH', 'A draft direction is zero')
    return vector / length


def _ring(center, normal, radius, count=RING):
    """Points of a circle around center, in the plane normal to `normal`."""
    normal = _unit(normal)
    seed = np.eye(3)[int(np.argmin(np.abs(normal)))]
    u = _unit(np.cross(normal, seed))
    v = np.cross(normal, u)
    angles = np.linspace(0, 2*math.pi, count, endpoint=False)
    return np.asarray(center, float) + radius*(np.outer(np.cos(angles), u) + np.outer(np.sin(angles), v))


def _ellipse(axis, along, ca, cb, ra, rb, count=RING):
    angles = np.linspace(0, 2*math.pi, count, endpoint=False)
    return np.array([from_axis_frame(axis, along, ca + ra*math.cos(t), cb + rb*math.sin(t)) for t in angles])


def _hermite(points):
    """The interpolating path of a tube: the same cubic Hermite as the native curves."""
    points = [np.asarray(p, float) for p in points]
    if len(points) == 2:
        return [points[0], points[1]], [points[1]-points[0]]*2
    tangents = ([points[1]-points[0]] + [(points[i+1]-points[i-1])/2 for i in range(1, len(points)-1)] +
                [points[-1]-points[-2]])
    path, directions = [], []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        u, v = tangents[i], tangents[i+1]
        for t in np.linspace(0, 1, 9)[(0 if i == 0 else 1):]:
            path.append(a*(2*t**3-3*t**2+1) + b*(-2*t**3+3*t**2) + u*(t**3-2*t**2+t) + v*(t**3-t**2))
            directions.append(a*(6*t**2-6*t) + b*(-6*t**2+6*t) + u*(3*t**2-4*t+1) + v*(3*t**2-2*t))
    return path, directions


def _tube(points, radii):
    path, directions = _hermite(points)
    if len(radii) == 1:
        radius = [radii[0]]*len(path)
    else:
        # Radii are given per input point; interpolate them along the sampled path.
        stations = np.linspace(0, len(radii)-1, len(path))
        radius = np.interp(stations, range(len(radii)), radii)
    rings = [_ring(p, d, r) for p, d, r in zip(path, directions, radius)]
    return [np.vstack(pair) for pair in zip(rings, rings[1:])]


def _axis_base(geometry, axis):
    """(offset along the axis, the two cross-axis centres) as the native recipes place them."""
    if geometry.get('origin') is not None:
        origin = dict(zip(AXES, geometry['origin']))
        return origin[axis], origin[CROSS_AXES[axis][0]], origin[CROSS_AXES[axis][1]]
    if axis == 'Y':
        return 0., geometry['center_xz'][0], geometry['center_xz'][1]
    return 0., 0., 0.


def _primitive(node):
    kind, g = node['kind'], node['geometry']
    if kind == 'sectioned_solid':
        axis = g.get('axis', 'Y')
        start, ca, cb = _axis_base(g, axis)
        rings = [_ellipse(axis, start + h, ca, cb, ra, rb) for h, ra, rb in g['sections']]
        return [np.vstack(pair) for pair in zip(rings, rings[1:])]
    if kind == 'revolve':
        axis = g.get('axis', 'Y')
        start, ca, cb = _axis_base(g, axis)
        rings = [_ellipse(axis, start + h, ca, cb, max(r, 0.), max(r, 0.)) for h, r in g['profile']]
        if g.get('closed'):
            rings.append(rings[0])      # The outline returns to its first point.
        return [np.vstack(pair) for pair in zip(rings, rings[1:])]
    if kind == 'tube':
        return _tube(g['points'], [g['radius']])
    if kind == 'variable_tube':
        return _tube(g['points'], list(g['radii']))
    if kind == 'dome':
        signed = g.get('axis', 'Y')
        axis, sign = signed[-1], (-1 if signed.startswith('-') else 1)
        center = dict(zip(AXES, g['center']))
        radius, height = g['radius'], g['height']
        sphere = (radius*radius + height*height) / (2*height)
        ca, cb = (center[k] for k in CROSS_AXES[axis])
        rings = []
        for z in np.linspace(0, height, 13):
            rho = math.sqrt(max(sphere*sphere - (z - (height - sphere))**2, 0.))
            rings.append(_ellipse(axis, center[axis] + sign*z, ca, cb, rho, rho))
        return [np.vstack(rings)]
    if kind == 'ellipsoid':
        rx, ry, rz = g['radii']
        cx, cy, cz = g['center']
        points = []
        for latitude in np.linspace(-math.pi/2, math.pi/2, 17):
            for longitude in np.linspace(0, 2*math.pi, 32, endpoint=False):
                points.append((cx + rx*math.cos(latitude)*math.cos(longitude), cy + ry*math.sin(latitude),
                               cz + rz*math.cos(latitude)*math.sin(longitude)))
        return [np.array(points)]
    if kind == 'rounded_prism':
        axis = g.get('axis', 'Z')
        w, h, d, r = g['width']/2, g['height']/2, g['depth']/2, min(g['corner_radius'], g['width']/2, g['height']/2)
        outline = []
        for sx, sy, start in ((1, 1, 0), (-1, 1, 90), (-1, -1, 180), (1, -1, 270)):
            for degrees in np.linspace(start, start + 90, 9):
                outline.append((sx*(w-r) + r*math.cos(math.radians(degrees)),
                                sy*(h-r) + r*math.sin(math.radians(degrees))))
        center = dict(zip(AXES, g['center']))
        # In-plane (width, height) directions per extrusion axis, as documented for the constructor.
        wide, tall = {'Z': ('X', 'Y'), 'Y': ('X', 'Z'), 'X': ('Z', 'Y')}[axis]
        points = []
        for side in (-d, d):
            for a, b in outline:
                value = {axis: center[axis] + side, wide: center[wide] + a, tall: center[tall] + b}
                points.append([value[k] for k in AXES])
        return [np.array(points)]
    if kind == 'prism':
        axis = g.get('axis', 'Z')
        center = dict(zip(AXES, g['center']))
        wide, tall = {'Z': ('X', 'Y'), 'Y': ('X', 'Z'), 'X': ('Z', 'Y')}[axis]
        points = []
        for side in (-g['depth']/2, g['depth']/2):
            for a, b in g['points']:
                value = {axis: center[axis] + side, wide: center[wide] + a, tall: center[tall] + b}
                points.append([value[k] for k in AXES])
        return [np.array(points)]
    return None


def _rotation(angles):
    rx, ry, rz = angles
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    about_x = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    about_y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    about_z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return about_z @ about_y @ about_x     # Extrinsic X, then Y, then Z.


def _bounds(pieces):
    if not pieces:
        return None
    points = np.vstack(pieces)
    return points.min(axis=0), points.max(axis=0)


def _box(low, high):
    return np.array([[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])


def _clip_piece(points, axis, bound, keep_above):
    """A convex piece cut by an axis plane: its kept vertices plus where its hull edges cross the plane."""
    side = points[:, axis] - bound
    keep = side >= -1e-9 if keep_above else side <= 1e-9
    if keep.all():
        return points
    if not keep.any():
        return None
    try:
        from scipy.spatial import ConvexHull
        hull = ConvexHull(points)
        edges = {tuple(sorted((int(s[i]), int(s[j])))) for s in hull.simplices for i, j in ((0, 1), (1, 2), (0, 2))}
        crossing = [points[a] + side[a] / (side[a] - side[b]) * (points[b] - points[a])
                    for a, b in edges if (side[a] > 0) != (side[b] > 0) and abs(side[a] - side[b]) > 1e-12]
        return np.vstack([points[keep]] + ([np.array(crossing)] if crossing else []))
    except Exception:
        # A flat or degenerate piece has no hull: move its outside points onto the plane.
        clipped = np.array(points, dtype=float)
        clipped[:, axis] = np.maximum(clipped[:, axis], bound) if keep_above else np.minimum(clipped[:, axis], bound)
        return clipped


def _clip(pieces, axis, bound, keep_above):
    clipped = (_clip_piece(piece, axis, bound, keep_above) for piece in pieces)
    return [piece for piece in clipped if piece is not None]


def _clip_box(pieces, low, high):
    """The parts of convex pieces inside an axis-aligned box."""
    for axis in range(3):
        pieces = _clip(pieces, axis, low[axis], True)
        pieces = _clip(pieces, axis, high[axis], False)
    return pieces


def _plane_cut(target, cut):
    """(axis, bound, keep_above) when a box cutter removes one whole side of the target, else None."""
    covered = [cut[0][i] <= target[0][i] + 1e-9 and cut[1][i] >= target[1][i] - 1e-9 for i in range(3)]
    if sum(covered) != 2:
        return None
    axis = covered.index(False)
    if cut[0][axis] <= target[0][axis] + 1e-9 and target[0][axis] < cut[1][axis] < target[1][axis]:
        return axis, cut[1][axis], True
    if cut[1][axis] >= target[1][axis] - 1e-9 and target[0][axis] < cut[0][axis] < target[1][axis]:
        return axis, cut[0][axis], False
    return None


def draft_components(program):
    """Convex pieces and flags for every component of a compiled program."""
    result, consumed = {}, {}
    retained_points = 0
    retained_piece_ids = set()
    point_counts = {}
    for node in program['nodes']:
        name, kind, g = node['id'], node['kind'], node['geometry']
        seeds = g.get('seeds') or []
        predicted = sum(point_counts[s] for s in seeds)
        if kind == 'pattern':
            predicted *= g['count'] - 1
        elif kind in {'fillet', 'chamfer'} or (kind == 'combine' and g['operation'] == 'union'):
            # These drafts reuse seed arrays; another list of references allocates no points.
            predicted = 0
        if retained_points + predicted > MAX_DRAFT_POINTS:
            raise ProgramError('DRAFT_COMPLEXITY_LIMIT', 'Expanded draft exceeds the point budget',
                               component=name, limit_kind='draft_points', max_draft_points=MAX_DRAFT_POINTS,
                               retained_points=retained_points, attempted_points=retained_points + predicted)
        estimated = any(result[s]['estimated'] for s in seeds)
        pieces = _primitive(node)
        if pieces is not None:
            # A concave prism outline is drawn as its hull.
            estimated = False
        elif kind == 'place':
            rotation, origin = _rotation(g['rotation']), np.asarray(g['origin'], float)
            pieces = [piece @ rotation.T + origin for s in seeds for piece in result[s]['pieces']]
        elif kind == 'mirror':
            flip = np.ones(3)
            flip[MIRROR_AXIS[g['plane']]] = -1
            pieces = [piece * flip for s in seeds for piece in result[s]['pieces']]
        elif kind == 'pattern':
            pieces = []
            for index in range(1, g['count']):
                if 'step' in g:
                    shift = index * np.asarray(g['step'], float)
                    pieces += [piece + shift for s in seeds for piece in result[s]['pieces']]
                else:
                    # Right-handed turn about the axis through center, as the native feature does.
                    e = np.eye(3)[AXES.index(g['axis'])]
                    angle = 2 * math.pi * index / g['count']
                    cross = np.array([[0, -e[2], e[1]], [e[2], 0, -e[0]], [-e[1], e[0], 0]])
                    turn = math.cos(angle)*np.eye(3) + math.sin(angle)*cross + (1 - math.cos(angle))*np.outer(e, e)
                    center = np.asarray(g['center'], float)
                    pieces += [(piece - center) @ turn.T + center for s in seeds for piece in result[s]['pieces']]
        elif kind == 'combine' and g['operation'] == 'union':
            pieces = [piece for s in seeds for piece in result[s]['pieces']]
        elif kind == 'combine' and g['operation'] == 'difference':
            pieces = list(result[seeds[0]]['pieces'])
            for tool in seeds[1:]:
                target, cut = _bounds(pieces), _bounds(result[tool]['pieces'])
                if not (target and cut):
                    continue
                plane = _plane_cut(target, cut) if result[tool]['box'] else None
                if plane is not None:
                    # A box that covers one whole side of the target is a plane cut: exact.
                    pieces = _clip(pieces, *plane)
                elif any((cut[0][i] <= target[0][i] < cut[1][i]) or
                         (cut[0][i] < target[1][i] <= cut[1][i]) for i in range(3)):
                    # Any other cutter that reaches a side of the target can shorten its extents.
                    overlap = all(cut[0][j] < target[1][j] and cut[1][j] > target[0][j] for j in range(3))
                    estimated = estimated or overlap
        elif kind == 'combine':
            items = [result[s] for s in seeds]
            bounds = [_bounds(item['pieces']) for item in items]
            pieces = []
            # Exact only when everything but one seed is a box: then the common box is a real cut.
            base = next((item for item in items if not item['box']), items[0])
            estimated = estimated or any(not item['box'] for item in items if item is not base)
            if all(bounds):
                low = np.max([b[0] for b in bounds], axis=0)
                high = np.min([b[1] for b in bounds], axis=0)
                if np.all(high > low):
                    # The pieces of the shaped seed inside the common box: tighter than the box itself.
                    pieces = _clip_box(base['pieces'], low, high) or [_box(low, high)]
        elif kind in {'fillet', 'chamfer'}:
            pieces = list(result[seeds[0]]['pieces'])
        elif kind == 'surface_patch':
            host, mask = (_bounds(result[s]['pieces']) for s in seeds[:2])
            pieces = []
            if host and mask:
                # An inward patch stays inside the host; an outward one reaches past its surface.
                inside = g['inward'] and g['offset'] <= 0
                reach = 0. if inside else abs(g['offset']) + g['thickness']
                low, high = np.maximum(host[0] - reach, mask[0]), np.minimum(host[1] + reach, mask[1])
                if np.all(high > low):
                    pieces = (_clip_box(result[seeds[0]]['pieces'], low, high) if inside else None) or [_box(low, high)]
            estimated = True
        else:
            pieces, estimated = [], True
        # Only a plain box acts as a plane cut or an exact intersection mask.
        box = kind == 'rounded_prism' and g['corner_radius'] < 1e-9
        result[name] = {'kind': kind, 'pieces': pieces, 'estimated': bool(estimated), 'box': bool(box)}
        point_counts[name] = sum(len(piece) for piece in pieces)
        # Consumed components stay in result, so shared arrays stay alive and ids cannot be reused.
        # Logical point counts remain separate: a later transform really allocates every copy.
        for piece in pieces:
            if id(piece) not in retained_piece_ids:
                retained_piece_ids.add(id(piece))
                retained_points += len(piece)
        if retained_points > MAX_DRAFT_POINTS:
            raise ProgramError('DRAFT_COMPLEXITY_LIMIT', 'Expanded draft exceeds the point budget',
                               component=name, limit_kind='draft_points', max_draft_points=MAX_DRAFT_POINTS,
                               attempted_points=retained_points)
        if kind in {'combine', 'place', 'fillet', 'chamfer'}:
            consumed.update(dict.fromkeys(seeds, name))
        elif kind == 'surface_patch':
            consumed.update(dict.fromkeys(seeds[1:], name))
    for name, item in result.items():
        item['consumed_by'] = consumed.get(name)
    return result


def _box_record(bounds):
    """[min_x, min_y, min_z, max_x, max_y, max_z] in mm; 0.01 mm is below the sampling error."""
    if bounds is None:
        return None
    return [round(float(v), 2) for v in list(bounds[0]) + list(bounds[1])]


def draft_extents(program, components=None):
    """Predicted extents: overall, per resulting body, and per requested component."""
    drafted = draft_components(program) if components is None else components
    bodies, estimated, everything = {}, [], []
    for name, item in drafted.items():
        if item['consumed_by'] is None:
            bounds = _bounds(item['pieces'])
            bodies[name] = _box_record(bounds)
            if item['estimated']:
                estimated.append(name)
            if bounds is not None:
                everything.extend(item['pieces'])
    overall = _bounds(everything)
    return {'bbox': _box_record(overall),
            'size': None if overall is None else [round(float(v), 2) for v in overall[1] - overall[0]],
            'bodies': bodies, 'estimated': estimated,
            'note': 'predicted mm [min_x,min_y,min_z,max_x,max_y,max_z]; estimated bodies are upper bounds'}


def component_extents(program, names, components=None):
    drafted = draft_components(program) if components is None else components
    result = {}
    for name in names:
        if name not in drafted:
            raise ProgramError('UNKNOWN_COMPONENT', f'Unknown component {name}')
        item = drafted[name]
        result[name] = {'bbox': _box_record(_bounds(item['pieces'])), 'kind': item['kind'],
                        **({'estimated': True} if item['estimated'] else {}),
                        **({'consumed_by': item['consumed_by']} if item['consumed_by'] else {})}
    return result


def project(points, view):
    (ax, sx), (ay, sy) = VIEWS[view]
    return np.column_stack((points[:, AXES.index(ax)]*sx, points[:, AXES.index(ay)]*sy))


def silhouettes(drafted, view):
    """Projected outline of every resulting body in one view, as shapely geometry in mm."""
    from shapely.geometry import MultiPoint
    from shapely.ops import unary_union
    if view not in VIEWS:
        raise ProgramError('INVALID_VIEWS', 'Draft views are ' + ', '.join(VIEWS))
    result = {}
    for name, item in drafted.items():
        if item['consumed_by'] is not None or not item['pieces']:
            continue
        hulls = [MultiPoint(project(piece, view)).convex_hull for piece in item['pieces']]
        shape = unary_union([hull for hull in hulls if hull.area > 0])
        if not shape.is_empty:
            result[name] = shape
    return result


def _polygons(shape):
    return [shape] if shape.geom_type == 'Polygon' else [g for g in getattr(shape, 'geoms', []) if g.geom_type == 'Polygon']


def raster_frame(bounds, size, margin=.06):
    """Pixel mapping that fits model bounds (min_x, min_y, max_x, max_y in mm) into a square image."""
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    scale = size * (1 - 2*margin) / max(width, height, 1e-9)
    left = (size - width*scale) / 2 - bounds[0]*scale
    top = (size + height*scale) / 2 + bounds[1]*scale
    return {'px_per_mm': scale, 'origin_px': [left, top]}     # Pixel of model (0, 0); image Y grows down.


def to_pixels(points, frame):
    scale, (left, top) = frame['px_per_mm'], frame['origin_px']
    return [(left + x*scale, top - y*scale) for x, y in points]


def render_view(drafted, view, path, size=800):
    """Write a draft silhouette image; returns the frame needed to convert pixels to mm."""
    from PIL import Image, ImageDraw
    from shapely.ops import unary_union
    shapes = silhouettes(drafted, view)
    if not shapes:
        raise ProgramError('DRAFT_EMPTY', 'The program has no body with a predictable shape')
    frame = raster_frame(unary_union(list(shapes.values())).bounds, size)
    image = Image.new('RGB', (size, size), 'white')
    draw = ImageDraw.Draw(image)
    for index, (name, shape) in enumerate(shapes.items()):
        for polygon in _polygons(shape):
            draw.polygon(to_pixels(polygon.exterior.coords, frame), fill=COLORS[index % len(COLORS)],
                         outline=(60, 60, 60))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return {'view': view, 'path': str(path), 'size_px': [size, size], **frame, 'kind': 'draft_silhouette',
            'bodies': list(shapes)}


def view_mask(drafted, view, frame, size):
    """Binary mask (numpy bool, rows from the top) of the model silhouette in a given pixel frame."""
    from PIL import Image, ImageDraw
    image = Image.new('L', (int(size[0]), int(size[1])), 0)
    draw = ImageDraw.Draw(image)
    for shape in silhouettes(drafted, view).values():
        for polygon in _polygons(shape):
            draw.polygon(to_pixels(polygon.exterior.coords, frame), fill=255)
    return np.asarray(image) > 0
