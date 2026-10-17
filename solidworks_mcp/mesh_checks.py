"""COM-free checks on the tessellation of native bodies.

The triangles come from SolidWorks (mm, wound counter-clockwise seen from
outside), so every number here describes the body that was actually built:
its exact silhouette, whether the assembled model stands on its base, and
how thin its walls are.
"""

from __future__ import annotations

import math

import numpy as np

from .shape_draft import VIEWS, project, to_pixels
from .shape_program import AXES, ProgramError

CONTACT_TOLERANCE_MM = .05      # A face this close to the lowest point rests on the plate.
DEFAULT_MIN_WALL_MM = 1.2       # Three perimeters of a 0.4 mm nozzle.
MAX_WALL_SAMPLES = 4000
RAY_CHUNK = 256
OPPOSING_COS = .819             # Faces within 35 degrees of parallel bound a wall; blunter ones meet at an edge.
MAX_THIN_SPOTS = 3


def as_triangles(triangles):
    result = np.asarray(triangles, dtype=float).reshape(-1, 3, 3)
    if result.shape[0] == 0:
        raise ProgramError('MESH_EMPTY', 'The body has no tessellation triangles')
    return result


class MeshSilhouette:
    """Exact projected outline of tessellated bodies; the silhouette source of compare_reference."""

    method = ('native tessellation of the saved bodies against the filled image foreground; x is measured '
              'from the alignment axis, positive to the right of the image; through holes are filled '
              'like the reference')
    kind = 'native'

    def __init__(self, triangles):
        self.triangles = as_triangles(triangles)

    def bounds(self, view):
        flat = project(self.triangles.reshape(-1, 3), view)
        # Keep derived comparisons and counters JSON-native for the MCP writer.
        return tuple(float(value) for value in (*flat.min(axis=0), *flat.max(axis=0)))

    def mask(self, view, frame, size):
        from PIL import Image, ImageDraw
        from scipy.ndimage import binary_fill_holes
        image = Image.new('L', (int(size[0]), int(size[1])), 0)
        draw = ImageDraw.Draw(image)
        flat = project(self.triangles.reshape(-1, 3), view)
        pixels = np.asarray(to_pixels(flat, frame)).reshape(-1, 3, 2)
        for triangle in pixels:
            draw.polygon([tuple(p) for p in triangle], fill=255, outline=255)
        return binary_fill_holes(np.asarray(image) > 0)


def _normals(triangles):
    """Unnormalised outward normals; the length is twice the triangle area."""
    return np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])


def volume_and_centroid(triangles):
    """Volume (mm3) and centre of mass (mm) of a closed, outward-wound mesh of uniform density."""
    triangles = as_triangles(triangles)
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    signed = np.einsum('ij,ij->i', a, np.cross(b, c)) / 6
    volume = float(signed.sum())
    if abs(volume) < 1e-9:
        raise ProgramError('MESH_NOT_CLOSED', 'The tessellation encloses no volume')
    centroid = ((a + b + c) / 4 * signed[:, None]).sum(axis=0) / volume
    return volume, centroid


def support(bodies, up='Y', tolerance=CONTACT_TOLERANCE_MM):
    """Does the assembled model stand on its lowest flat faces?

    bodies: {name: triangles}. The plate is the plane through the lowest point,
    normal to `up`. The model is stable when the centre of mass projects inside
    the convex hull of the faces resting on the plate.
    """
    from shapely.geometry import MultiPoint, Point
    if up not in AXES:
        raise ProgramError('INVALID_ARGUMENT', 'up is X, Y or Z')
    axis = AXES.index(up)
    plane = [i for i in range(3) if i != axis]
    meshes = {name: as_triangles(t) for name, t in bodies.items()}
    ground = min(float(t[:, :, axis].min()) for t in meshes.values())
    total, moment, contact_points, contact_area, resting = 0., np.zeros(3), [], 0., []
    for name, triangles in meshes.items():
        volume, centroid = volume_and_centroid(triangles)
        total += volume
        moment += centroid * volume
        on_plate = np.all(triangles[:, :, axis] <= ground + tolerance, axis=1)
        if on_plate.any():
            area = float(np.linalg.norm(_normals(triangles[on_plate]), axis=1).sum() / 2)
            if area > 0:
                resting.append(name)
                contact_area += area
                contact_points.append(triangles[on_plate].reshape(-1, 3)[:, plane])
    center = moment / total
    result = {'up': up, 'plate_mm': round(ground, 3), 'center_of_mass_mm': [round(float(v), 2) for v in center],
              'center_height_mm': round(float(center[axis] - ground), 2),
              'contact_area_mm2': round(contact_area, 2), 'resting_bodies': resting}
    hull = MultiPoint(np.vstack(contact_points)).convex_hull if contact_points else None
    if hull is None or hull.geom_type != 'Polygon' or hull.area <= 0:
        return {**result, 'stable': False, 'reason': 'NO_FLAT_SUPPORT',
                'note': 'no flat face rests on the plate normal to ' + up}
    low_a, low_b, high_a, high_b = hull.bounds
    point = Point(float(center[plane[0]]), float(center[plane[1]]))
    margin = hull.exterior.distance(point) * (1 if hull.contains(point) else -1)
    height = max(float(center[axis] - ground), 1e-9)
    result.update(
        footprint_mm={AXES[plane[0]]: [round(low_a, 2), round(high_a, 2)],
                      AXES[plane[1]]: [round(low_b, 2), round(high_b, 2)]},
        footprint_area_mm2=round(float(hull.area), 2), margin_mm=round(float(margin), 2),
        # Tilt of the plate at which the centre of mass passes over the nearest footprint edge.
        tip_angle_deg=round(math.degrees(math.atan2(margin, height)), 1) if margin > 0 else 0.,
        stable=bool(margin > 0))
    if margin <= 0:
        result['reason'] = 'CENTER_OF_MASS_OUTSIDE_FOOTPRINT'
    return result


def _ray_hits(origins, directions, triangles):
    """Nearest hit of each ray with a triangle soup (Moller-Trumbore): (distance, triangle); inf when none."""
    a = triangles[:, 0]
    e1, e2 = triangles[:, 1] - a, triangles[:, 2] - a
    best = np.full(len(origins), np.inf)
    which = np.zeros(len(origins), dtype=int)
    for start in range(0, len(origins), RAY_CHUNK):
        o, d = origins[start:start + RAY_CHUNK, None, :], directions[start:start + RAY_CHUNK, None, :]
        p = np.cross(d, e2[None])
        det = np.einsum('rtk,tk->rt', p, e1)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1 / np.where(ok, det, 1), 0)
        s = o - a[None]
        u = np.einsum('rtk,rtk->rt', s, p) * inv
        q = np.cross(s, e1[None])
        v = np.einsum('rtk,rtk->rt', np.broadcast_to(d, q.shape), q) * inv
        t = np.einsum('rtk,tk->rt', q, e2) * inv
        hit = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > 1e-6)
        distance = np.where(hit, t, np.inf)
        nearest = distance.argmin(axis=1)
        which[start:start + RAY_CHUNK] = nearest
        best[start:start + RAY_CHUNK] = distance[np.arange(len(nearest)), nearest]
    return best, which


def wall_thickness(triangles, min_wall_mm=DEFAULT_MIN_WALL_MM, max_samples=MAX_WALL_SAMPLES):
    """Material thickness behind the surface, sampled at triangle centres.

    From each sample a ray goes straight into the material. Where the surface
    it leaves through faces back along the ray (within 35 degrees), the two
    faces bound a wall and the distance is its thickness. Where it stands
    across the ray the faces meet at an edge: thin there by geometry, reported
    separately and never a failure.
    """
    triangles = as_triangles(triangles)
    normals = _normals(triangles)
    doubled = np.linalg.norm(normals, axis=1)
    keep = doubled > 1e-9
    triangles, normals, doubled = triangles[keep], normals[keep], doubled[keep]
    unit = normals / doubled[:, None]
    count = len(triangles)
    step = max(1, math.ceil(count / max_samples))
    picked = np.arange(0, count, step)
    inward = -unit[picked]
    centers = triangles[picked].mean(axis=1)
    depth, hit = _ray_hits(centers + inward * 1e-4, inward, triangles)
    finite = np.isfinite(depth)
    if not finite.any():
        raise ProgramError('MESH_NOT_CLOSED', 'No wall could be measured: the tessellation is not closed')
    total_area = float(doubled.sum() / 2)
    areas = doubled[picked] / 2 * (doubled.sum() / doubled[picked].sum())    # Scaled to the whole surface.
    facing = np.einsum('ij,ij->i', unit[hit], inward)
    wall = finite & (facing >= OPPOSING_COS)
    thin = wall & (depth < min_wall_mm)
    edge = finite & ~wall & (depth < min_wall_mm)
    thin_area = float(areas[thin].sum())
    result = {'min_thickness_mm': None, 'at_mm': None, 'thin_area_mm2': round(thin_area, 2),
              'thin_area_percent': round(100 * thin_area / total_area, 2), 'samples': int(len(picked))}
    if wall.any():
        thinnest = int(np.argmin(np.where(wall, depth, np.inf)))
        result.update(min_thickness_mm=round(float(depth[thinnest]), 3),
                      at_mm=[round(float(v), 2) for v in centers[thinnest]])
    # The thin places one by one: a single location cannot be acted on when there are several.
    spots, left = [], np.flatnonzero(thin)
    while len(left) and len(spots) < 64:
        seed = left[np.argmin(depth[left])]
        near = np.linalg.norm(centers[left] - centers[seed], axis=1) <= 3 * min_wall_mm
        spots.append({'at_mm': [round(float(v), 2) for v in centers[seed]],
                      'thickness_mm': round(float(depth[seed]), 3),
                      'area_mm2': round(float(areas[left[near]].sum()), 2)})
        left = left[~near]
    if spots:
        result['thin_spots'] = spots[:MAX_THIN_SPOTS]
    if edge.any():
        result['sharp_edge_area_mm2'] = round(float(areas[edge].sum()), 2)
    # A place fails as a patch two walls wide, or as a smaller one under half the wall: the feathered
    # rim of a hole in a sloping face stays a remark (null), a sliver does not.
    failed = any(spot['area_mm2'] >= 4 * min_wall_mm**2 or
                 (spot['thickness_mm'] < min_wall_mm / 2 and spot['area_mm2'] >= min_wall_mm**2) for spot in spots)
    result['passed'] = False if failed else (None if spots else True)
    return result


def printability(bodies, min_wall_mm=DEFAULT_MIN_WALL_MM, up='Y'):
    """Support of the assembled model and wall thickness of every body."""
    if isinstance(min_wall_mm, bool) or not isinstance(min_wall_mm, (int, float)) or not 0 < min_wall_mm <= 50:
        raise ProgramError('INVALID_ARGUMENT', 'min_wall_mm must be a number in (0, 50]')
    walls = []
    for name, triangles in bodies.items():
        try:
            walls.append({'body': name, **wall_thickness(triangles, min_wall_mm)})
        except ProgramError as exc:
            walls.append({'body': name, 'passed': None, 'error': exc.code})
    verdicts = [wall['passed'] for wall in walls]
    return {'support': support(bodies, up), 'min_wall_mm': min_wall_mm, 'walls': walls,
            'walls_passed': False if False in verdicts else (None if None in verdicts else True),
            'method': ('tessellated native bodies, uniform density; support: centre of mass over the convex hull '
                       'of the faces on the plate; wall: ray from each sampled triangle centre into the material, '
                       'counted where the far face is within 35 degrees of parallel; thin_spots lists the thin '
                       'places, sharp_edge_area_mm2 the acute edges (informational); a place fails as a patch two '
                       'walls wide or as a smaller one under half the wall, passed=null marks lesser thin places')}
