"""COM-free checks of a compiled shape program that predict native failures.

Nothing here blocks a build: a saved part with a tight bend must stay editable.
The warnings travel with analyze, create and edit answers, and with the error
of a failed rebuild, where they are the likely cause. They stay short: the
reasons are in the pitfalls topic of the API lookup.
"""

from __future__ import annotations

import numpy as np
from .shape_limits import expansion_warnings

BEND_MARGIN = 1.1       # At a bend radius equal to the tube radius the swept surface folds onto itself.
SAMPLES = 48
WIDE_MASK = .7          # Share of the host width beyond which the mask walls leave the surface at a shallow angle.
ROUND_HOSTS = {'sectioned_solid', 'revolve', 'dome', 'ellipsoid'}


def bend_radii(points):
    """Smallest radius of curvature of every segment of a tube path.

    The path is the same cubic Hermite interpolation the native curves use:
    the tangent at an interior point is half the chord between its neighbours.
    """
    points = [np.asarray(p, float) for p in points]
    if len(points) < 3:
        return [float('inf')] * max(len(points) - 1, 0)
    tangents = ([points[1] - points[0]] + [(points[i+1] - points[i-1]) / 2 for i in range(1, len(points) - 1)] +
                [points[-1] - points[-2]])
    result = []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        u, v = tangents[i], tangents[i+1]
        smallest = float('inf')
        for t in np.linspace(0, 1, SAMPLES):
            first = a*(6*t*t - 6*t) + b*(-6*t*t + 6*t) + u*(3*t*t - 4*t + 1) + v*(3*t*t - 2*t)
            second = a*(12*t - 6) + b*(-12*t + 6) + u*(6*t - 4) + v*(6*t - 2)
            turn = np.linalg.norm(np.cross(first, second))
            if turn > 1e-12:
                smallest = min(smallest, float(np.linalg.norm(first)**3 / turn))
        result.append(smallest)
    return result


def tube_bends(program):
    """Tubes whose path bends tighter than the tube is thick: the native sweep self-intersects."""
    warnings = []
    for node in program['nodes']:
        if node['kind'] not in {'tube', 'variable_tube'}:
            continue
        geometry = node['geometry']
        points = geometry['points']
        if len(points) < 3:
            continue
        radii = list(geometry['radii']) if node['kind'] == 'variable_tube' else [geometry['radius']] * len(points)
        worst = None
        for index, bend in enumerate(bend_radii(points)):
            tube = max(radii[index], radii[index + 1])
            if bend < tube * BEND_MARGIN and (worst is None or bend / tube < worst[0]):
                worst = (bend / tube, index, bend, tube)
        if worst:
            _, index, bend, tube = worst
            warnings.append({
                'code': 'TUBE_BEND_TOO_TIGHT', 'component': node['id'],
                'min_bend_radius_mm': round(bend, 2), 'tube_radius_mm': round(tube, 2),
                'between_points': [index + 1, index + 2],
                'action': 'spread these path points or reduce the radius'})
    return warnings


def wide_masks(program):
    """Surface patches whose mask is almost as wide as the curved host it is projected onto.

    The mask walls then leave the host surface at a shallow angle: the pocket breaks out in
    slivers, and pocket and insert both end at the back plane of the mask.
    """
    from .shape_draft import _bounds, draft_components
    from .shape_program import AXES
    drafted = None
    warnings = []
    for node in program['nodes']:
        if node['kind'] != 'surface_patch':
            continue
        drafted = drafted or draft_components(program)
        host_name, mask_name = node['geometry']['seeds'][:2]
        host_node = next(n for n in program['nodes'] if n['id'] == host_name)
        if host_node['kind'] not in ROUND_HOSTS:
            continue
        host, mask = _bounds(drafted[host_name]['pieces']), _bounds(drafted[mask_name]['pieces'])
        if not (host and mask):
            continue
        axis = AXES.index(str(host_node['geometry'].get('axis', 'Y'))[-1])
        cross = [i for i in range(3) if i != axis]
        offset = [abs((mask[0][i] + mask[1][i]) / 2 - (host[0][i] + host[1][i]) / 2) for i in cross]
        if abs(offset[0] - offset[1]) < 1e-6:
            continue                    # The side the mask reaches through is not evident.
        lateral = cross[0] if offset[1] > offset[0] else cross[1]
        share = (mask[1][lateral] - mask[0][lateral]) / max(host[1][lateral] - host[0][lateral], 1e-9)
        if share > WIDE_MASK:
            warnings.append({
                'code': 'SURFACE_PATCH_WIDE_MASK', 'component': node['id'], 'mask': mask_name,
                'mask_width_share': round(float(share), 2),
                'action': 'narrow the mask or use template curved_inlay'})
    return warnings


def program_warnings(program):
    found = []
    for check in (tube_bends, wide_masks, expansion_warnings):
        try:
            found.extend(check(program))
        except Exception:
            # A check comments on a build; it must never break one that is already committed.
            continue
    return found
