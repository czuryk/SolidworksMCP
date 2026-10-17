"""Opt-in, bounded STL seam cleanup. Never fill holes or repair native geometry."""
from pathlib import Path
import struct
import numpy as np

from .mesh_acceptance import inspect_mesh
from .shape_program import ProgramError

MAX_DISPLACEMENT_MM = 0.00006
MAX_REMOVED_FACETS = 10
MAX_REORIENTED_FACETS = 64


def indexed_mesh(triangles):
    tri = np.asarray(triangles, dtype=np.float64)
    if tri.ndim != 3 or tri.shape[1:] != (3, 3) or not 0 < len(tri) <= 1000000 or not np.isfinite(tri).all():
        raise ProgramError('MESH_CLEANUP_REFUSED', 'Expected 1–1000000 finite triangles')
    tolerance = max(1e-6, float(np.abs(tri).max())*2e-7)
    _, first, inverse = np.unique(np.rint(tri.reshape(-1, 3)/tolerance).astype(np.int64),
                                  axis=0, return_index=True, return_inverse=True)
    return tri, tri.reshape(-1, 3)[first], inverse.reshape(-1, 3), tolerance


def edge_graph(faces):
    ordered = np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    edges, mapping, counts = np.unique(np.sort(ordered, axis=1), axis=0,
                                       return_inverse=True, return_counts=True)
    directions = ordered[:, 0] < ordered[:, 1]
    balance = np.bincount(mapping, weights=np.where(directions, 1, -1))
    return edges, mapping, counts, directions, balance


def mesh_diagnostics(triangles, limit=20):
    """Bounded facet/edge samples refer to the unchanged raw STL facet order."""
    tri, vertices, faces, tolerance = indexed_mesh(triangles)
    collapsed = ((faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) |
                 (faces[:, 2] == faces[:, 0]))
    area2 = np.linalg.norm(np.cross(tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0]), axis=1)
    edges, mapping, counts, _, balance = edge_graph(faces)
    bad_edges = np.flatnonzero((counts != 2) | (balance != 0))[:limit]
    return {'facet_indices_base':0, 'sample_limit':limit, 'adjacency_tolerance_mm':tolerance,
            'degenerate_facets':np.flatnonzero(collapsed | (area2 <= 1e-14))[:limit].tolist(),
            'edge_samples':[{'vertices_mm':vertices[edges[i]].tolist(), 'incident_count':int(counts[i]),
                             'facet_indices':(np.flatnonzero(mapping == i) % len(faces))[:limit].tolist(),
                             'winding_balance':int(balance[i])} for i in bad_edges]}


def bounded_cleanup(triangles, expected_volume, native_bbox, deviation_mm):
    """Return candidate bytes' triangles plus an audit, or refuse explicitly.

    A single connected surface is required only for cleanup. Valid raw hollow
    bodies bypass this function and retain their native inner-shell orientation.
    """
    tri, vertices, faces, tolerance = indexed_mesh(triangles)
    displacement = float(np.linalg.norm(vertices[faces]-tri, axis=2).max())
    if displacement > MAX_DISPLACEMENT_MM:
        raise ProgramError('MESH_CLEANUP_REFUSED', 'Seam displacement exceeds the fixed limit',
                           maximum_vertex_displacement_mm=displacement, limit_mm=MAX_DISPLACEMENT_MM)
    keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 2] != faces[:, 0])
    removed_ids = np.flatnonzero(~keep)
    removed_limit = min(MAX_REMOVED_FACETS, max(1, len(tri)//10000))
    if len(removed_ids) > removed_limit or not keep.any():
        raise ProgramError('MESH_CLEANUP_REFUSED', 'Too many collapsed facets',
                           removed_facets=int(len(removed_ids)), limit=removed_limit)
    faces = faces[keep].copy()
    if len(np.unique(np.sort(faces, axis=1), axis=0)) != len(faces):
        raise ProgramError('MESH_CLEANUP_REFUSED', 'Duplicate facets require separate investigation')
    _, mapping, counts, directions, _ = edge_graph(faces)
    if np.any(counts != 2):
        raise ProgramError('MESH_CLEANUP_REFUSED', 'Open or nonmanifold edges remain; no hole filling is permitted')
    pairs = np.argsort(mapping, kind='stable').reshape(-1, 2)
    a, b = pairs[:, 0], pairs[:, 1]
    n = len(faces)
    neighbors = np.empty((n, 3), dtype=np.int64)
    parity = np.empty((n, 3), dtype=np.int8)
    neighbors[a % n, a // n] = b % n
    neighbors[b % n, b // n] = a % n
    parity[a % n, a // n] = parity[b % n, b // n] = directions[a] == directions[b]
    flips = np.full(n, -1, dtype=np.int8)
    flips[0] = 0
    pending = [0]
    while pending:
        face = pending.pop()
        for neighbor, change in zip(neighbors[face], parity[face]):
            expected = flips[face] ^ change
            if flips[neighbor] < 0:
                flips[neighbor] = expected
                pending.append(int(neighbor))
            elif flips[neighbor] != expected:
                raise ProgramError('MESH_CLEANUP_REFUSED', 'Surface is not orientable')
    if np.any(flips < 0):
        raise ProgramError('MESH_CLEANUP_REFUSED', 'Cleanup requires one connected surface; inner shells are not reoriented')
    # Keep the majority native orientation. Do not globally invert a bad shell.
    if np.count_nonzero(flips) > n/2:
        flips = 1-flips
    flip_limit = min(MAX_REORIENTED_FACETS, max(1, n//1000))
    flip_count = int(np.count_nonzero(flips))
    if flip_count > flip_limit:
        raise ProgramError('MESH_CLEANUP_REFUSED', 'Too many inconsistent facets',
                           reoriented_facets=flip_count, limit=flip_limit)
    selected = flips.astype(bool)
    faces[selected] = faces[selected][:, [0, 2, 1]]
    # Validate the exact float32 coordinates that will be written to STL.
    output = vertices[faces].astype(np.float32).astype(np.float64)
    accepted = inspect_mesh(output, expected_volume, native_bbox, deviation_mm)
    if not accepted['passed']:
        raise ProgramError('MESH_CLEANUP_REFUSED', 'Bounded cleanup still fails the unchanged validator', inspection=accepted)
    retained_ids = np.flatnonzero(keep)
    audit = {'policy':'bounded_seams_v1', 'repair_performed':bool(displacement or len(removed_ids) or flip_count),
             'source_triangles':len(tri), 'output_triangles':len(output),
             'removed_collapsed_facet_indices':removed_ids.tolist(),
             'reoriented_facet_indices':retained_ids[selected].tolist(),
             'maximum_vertex_displacement_mm':displacement,
             'maximum_allowed_displacement_mm':MAX_DISPLACEMENT_MM,
             'removed_facet_limit':removed_limit, 'reoriented_facet_limit':flip_limit,
             'adjacency_tolerance_mm':tolerance, 'connected_surface_count':1}
    return output, audit


def write_binary_stl(path, triangles):
    tri = np.asarray(triangles, dtype=np.float32)
    dtype = np.dtype([('normal', '<f4', (3,)), ('vertices', '<f4', (3, 3)), ('attribute', '<u2')])
    records = np.zeros(len(tri), dtype=dtype)
    normals = np.cross(tri[:, 1].astype(float)-tri[:, 0], tri[:, 2].astype(float)-tri[:, 0])
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    records['normal'] = normals
    records['vertices'] = tri
    Path(path).write_bytes(b'SolidWorks MCP bounded seam cleanup'.ljust(80, b' ') +
                          struct.pack('<I', len(records)) + records.tobytes())
