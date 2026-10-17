"""Structural checks on the actual exported triangles, without mesh repair."""
import numpy as np


def inspect_mesh(triangles, expected_volume, native_bbox, deviation_mm):
    triangles = np.asarray(triangles, dtype=np.float64)
    if triangles.ndim != 3 or triangles.shape[1:] != (3,3) or not len(triangles) or not np.isfinite(triangles).all():
        raise ValueError('Invalid exported triangles')
    # Float32 STL coordinates have finite precision. Quantization is only an
    # adjacency test; coordinates/files are never welded or rewritten.
    extent = float(np.max(np.ptp(triangles.reshape(-1,3), axis=0)))
    tolerance = max(1e-6, float(np.max(np.abs(triangles)))*2e-7)
    vertices, inverse = np.unique(np.rint(triangles.reshape(-1,3)/tolerance).astype(np.int64), axis=0, return_inverse=True)
    faces = inverse.reshape(-1,3)
    cross = np.cross(triangles[:,1]-triangles[:,0], triangles[:,2]-triangles[:,0])
    area2 = np.linalg.norm(cross, axis=1)
    degenerate = int(np.count_nonzero((faces[:,0]==faces[:,1]) | (faces[:,1]==faces[:,2]) |
                                    (faces[:,2]==faces[:,0]) | (area2<=1e-14)))
    ordered = np.concatenate((faces[:,[0,1]], faces[:,[1,2]], faces[:,[2,0]]))
    edges, edge_map, counts = np.unique(np.sort(ordered, axis=1), axis=0, return_inverse=True, return_counts=True)
    balance = np.bincount(edge_map, weights=np.where(ordered[:,0]<ordered[:,1],1,-1))
    boundary = int(np.count_nonzero(counts==1)); nonmanifold = int(np.count_nonzero(counts>2))
    winding = int(np.count_nonzero((counts==2)&(balance!=0)))
    duplicate = len(faces)-len(np.unique(np.sort(faces,axis=1),axis=0))
    # A closed edge graph can still contain a bow-tie vertex. Its link must be
    # one cycle; multiple cycles indicate disconnected local surface fans.
    links = [[] for _ in vertices]
    for a,b,c in faces:
        links[a].append((int(b),int(c))); links[b].append((int(c),int(a))); links[c].append((int(a),int(b)))
    bad_vertices = 0
    for link in links:
        adjacency = {}
        for a,b in link:
            adjacency.setdefault(a,set()).add(b); adjacency.setdefault(b,set()).add(a)
        if not adjacency: continue
        seen=set(); pending=[next(iter(adjacency))]
        while pending:
            v=pending.pop()
            if v in seen:continue
            seen.add(v);pending.extend(adjacency[v]-seen)
        if len(seen)!=len(adjacency) or any(len(n)!=2 for n in adjacency.values()):bad_vertices+=1
    origin = triangles.reshape(-1,3).mean(axis=0)
    shifted = triangles-origin
    volume = float(np.einsum('ij,ij->i', shifted[:,0],np.cross(shifted[:,1],shifted[:,2])).sum()/6)
    volume_error = abs(volume-expected_volume)/expected_volume if expected_volume>0 else float('inf')
    lo=triangles.reshape(-1,3).min(axis=0);hi=triangles.reshape(-1,3).max(axis=0)
    bbox_delta = max(float(np.max(np.abs(lo-native_bbox['min']))),float(np.max(np.abs(hi-native_bbox['max']))))
    bbox_tolerance = max(2*deviation_mm, 4*tolerance)
    topology_ok = not (degenerate or boundary or nonmanifold or winding or duplicate or bad_vertices)
    passed = topology_ok and volume>0 and volume_error<=.01 and bbox_delta<=bbox_tolerance
    return {'passed':bool(passed), 'triangles':len(faces), 'vertices':len(vertices),
            'adjacency_tolerance_mm':tolerance, 'boundary_edges':boundary,'nonmanifold_edges':nonmanifold,
            'inconsistent_edges':winding,'nonmanifold_vertices':bad_vertices,
            'degenerate_triangles':degenerate,'duplicate_triangles':int(duplicate),
            'signed_volume_mm3':volume,'native_volume_mm3':expected_volume,
            'relative_volume_error':volume_error,'relative_volume_tolerance':.01,
            'bbox_max_delta_mm':bbox_delta,'bbox_tolerance_mm':bbox_tolerance,
            'self_intersections':'NOT_CHECKED', 'repair_performed':False}
