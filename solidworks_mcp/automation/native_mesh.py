"""Tessellation of native solid bodies as plain triangles in mm."""

from __future__ import annotations

import numpy as np

from ..shape_program import ProgramError
from .com_utils import com_get, typed


def body_triangles(body):
    """(N, 3, 3) triangles of one body in mm, wound counter-clockwise seen from outside.

    Reads the display tessellation face by face: two COM calls per face. The
    stored vertex normals decide the winding, so volumes and inward rays are
    reliable whatever sense the face has.
    """
    pieces = []
    for face in com_get(body, 'GetFaces') or []:
        face = typed(face, 'IFace2')
        points = np.asarray(face.GetTessTriangles(True) or (), dtype=float).reshape(-1, 3, 3)
        if not len(points):
            continue
        normals = np.asarray(face.GetTessNorms() or (), dtype=float).reshape(-1, 3, 3)
        if normals.shape == points.shape:
            wound = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
            flipped = np.einsum('ij,ij->i', wound, normals.sum(axis=1)) < 0
            points[flipped] = points[flipped][:, ::-1]
        pieces.append(points)
    if not pieces:
        raise ProgramError('MESH_EMPTY', 'The body has no tessellation: ' + str(com_get(body, 'Name')))
    return np.vstack(pieces) * 1000.


def document_meshes(automation, doc, names=None):
    """{body name: triangles} of the solid bodies of a part; names limits the set."""
    result = {}
    for body in automation._get_solid_bodies(doc):
        name = str(com_get(body, 'Name'))
        if names is None or name in names:
            result[name] = body_triangles(body)
    if not result:
        raise ProgramError('MESH_EMPTY', 'The part has no solid body to tessellate')
    return result
