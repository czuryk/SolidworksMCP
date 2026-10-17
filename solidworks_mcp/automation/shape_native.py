"""Native solid recipes for the small, model-space shape program."""

from __future__ import annotations

import math

from .com_utils import (com_get, typed, array_r8, create_select_data,
                        feature_face_count)
from ..shape_program import ProgramError
from ..constants import SwPlanes


def unit(v):
    length = math.sqrt(sum(x*x for x in v))
    if length < 1e-12:
        raise ProgramError("DEGENERATE_FRAME", "Cannot normalize a zero vector")
    return tuple(x/length for x in v)


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2],
            a[0]*b[1]-a[1]*b[0])


def normal_frame(normal):
    n = unit(normal)
    seed = (1, 0, 0) if abs(n[0]) < 0.8 else (0, 0, 1)
    u = unit(cross(n, seed))
    return u, unit(cross(n, u))


class NativeCompiler:
    """Compile already-validated geometry into a new, owned part document."""

    def __init__(self, automation, doc):
        self.a, self.doc = automation, doc
        self.components = {}
        self.support = []
        self.stage = "initialize"
        self.component = None

    def require(self, condition, message, code="NATIVE_RECIPE_FAILED", **details):
        if not condition:
            raise ProgramError(code, message, component=self.component,
                               stage=self.stage, **details)

    def standard_plane(self, which):
        """The Front, Top or Right reference plane, found by position in the tree.

        A localized SolidWorks names them differently, so the English name is
        only the fallback. The three default planes are the first reference
        planes of every part, in this order.
        """
        planes = getattr(self, '_standard_planes', None)
        if planes is None:
            found = []
            for feature in self.a._walk_features(self.doc, include_sub=False):
                if str(com_get(feature, 'GetTypeName2', default='')) == 'RefPlane':
                    found.append(feature)
                    if len(found) == 3:
                        break
            planes = self._standard_planes = dict(zip(('Front', 'Top', 'Right'), found)) if len(found) == 3 else {}
        feature = planes.get(which) or self.a._find_feature(self.doc, which + ' Plane')
        self.require(feature is not None, 'Cannot find the standard ' + which + ' plane')
        return feature

    def select_standard_plane(self, which):
        self.doc.ClearSelection2(True)
        self.require(bool(self.standard_plane(which).Select2(False, 0)), 'Cannot select the ' + which + ' plane')

    def last_feature(self):
        feature = com_get(self.doc, "FeatureByPositionReverse", 0, default=None)
        self.require(feature is not None, "No native feature was returned")
        return feature

    def finish_sketch(self, name, is3d=False):
        sm = self.doc.SketchManager
        if is3d:
            sm.Insert3DSketch(True)
        else:
            sm.InsertSketch(True)
        feature = self.last_feature()
        feature.Name = name
        self.support.append(name)
        self.doc.ClearSelection2(True)
        return feature

    def frame_plane(self, name, origin, normal):
        """Use three exact native points; do not infer plane orientation by name."""
        self.stage = "create_frame"
        d, sm = self.doc, self.doc.SketchManager
        u, v = normal_frame(normal)
        d.ClearSelection2(True)
        sm.Insert3DSketch(True)
        points = []
        old_add = sm.AddToDB
        try:
            sm.AddToDB = True
            for direction in [(0, 0, 0), u, v]:
                p = [(origin[i] + 10.0*direction[i])*0.001 for i in range(3)]
                point = sm.CreatePoint(*p)
                self.require(point is not None, "Cannot create a frame point")
                points.append(point)
        finally:
            sm.AddToDB = old_add
        self.finish_sketch(name+"_Points", True)
        data = create_select_data(d, 0)
        for i, point in enumerate(points):
            self.require(bool(point.Select4(i > 0, data)), "Cannot select frame points")
        # The three-point API avoids undocumented selection/constraint guesses.
        plane = typed(d, "IModelDoc2").CreatePlaneThru3Points3(True)
        self.require(plane is not None, "Cannot create the profile frame")
        feature = self.last_feature()
        feature.Name = name
        self.support.append(name)
        d.ClearSelection2(True)
        return feature

    def begin_profile(self, plane):
        self.doc.ClearSelection2(True)
        self.require(bool(plane.Select2(False, 0)), "Cannot select the profile plane")
        sm = self.doc.SketchManager
        sm.InsertSketch(True)
        self.require(sm.ActiveSketch is not None, "Cannot enter the profile sketch")
        return sm

    def local(self, point):
        result = self.a._model_to_sketch_point(
            self.doc, [float(x)*0.001 for x in point])
        self.require(abs(result[2]) < 1e-7,
                     "A profile point is outside its declared plane",
                     "PROFILE_FRAME_MISMATCH")
        return (result[0], result[1], 0.0)

    def ellipse_profile(self, name, center, rx, rz):
        plane = self.frame_plane(name+"_Frame", center, (0, 1, 0))
        sm = self.begin_profile(plane)
        self.stage = "create_ellipse"
        c = self.local(center)
        x = self.local((center[0]+rx, center[1], center[2]))
        z = self.local((center[0], center[1], center[2]+rz))
        major, minor = (x, z) if rx >= rz else (z, x)
        old_add = sm.AddToDB
        try:
            sm.AddToDB = True
            if abs(rx-rz) < 1e-9:
                curve = sm.CreateCircle(*c, *major)
            else:
                curve = sm.CreateEllipse(*c, *major, *minor)
            self.require(curve is not None, "Ellipse construction failed")
        finally:
            sm.AddToDB = old_add
        return self.finish_sketch(name)

    def bodies(self):
        return list(self.a._get_solid_bodies(self.doc))

    def body_names(self):
        return {str(com_get(b, "Name", default="")) for b in self.bodies()}

    def finish_solid(self, name, feature, before, expected=1):
        self.require(feature is not None and feature_face_count(feature) > 0,
                     "The recipe did not create a feature with real faces")
        feature.Name = name
        new = [b for b in self.bodies()
               if str(com_get(b, "Name", default="")) not in before]
        self.require(len(new) == expected,
                     f"Expected {expected} new solid bodies, found {len(new)}")
        names = []
        for i, body in enumerate(new):
            body.Name = name if expected == 1 else f"{name}_{i+1}"
            names.append(str(com_get(body, "Name", default="")))
        self.components[name] = {
            "feature": str(com_get(feature, "Name", default=name)),
            "bodies": names, "face_count": feature_face_count(feature)}
        self.doc.ClearSelection2(True)
        return feature

    def sectioned_solid(self, name, geometry):
        profiles = []
        cx, cz = geometry["center_xz"]
        for i, (height, rx, rz) in enumerate(geometry["sections"]):
            profiles.append(self.ellipse_profile(
                f"{name}_Section{i+1}", (cx, height, cz), rx, rz))
        before = self.body_names()
        self.stage = "loft"
        self.doc.ClearSelection2(True)
        for i, profile in enumerate(profiles):
            self.require(bool(profile.Select2(i > 0, 1)), "Cannot select loft profiles")
        fm = typed(self.doc.FeatureManager, "IFeatureManager")
        feature = fm.InsertProtrusionBlend2(
            False, True, False, 1.0, 0, 0, 1.0, 1.0, True, True,
            False, 0.0, 0.0, 0, False, False, False, 0)
        return self.finish_solid(name, feature, before)

    def path(self, name, points):
        self.stage = "create_spatial_path"
        d, sm = self.doc, self.doc.SketchManager
        d.ClearSelection2(True)
        sm.Insert3DSketch(True)
        coordinates = [[v*0.001 for v in p] for p in points]
        old_add = sm.AddToDB
        try:
            sm.AddToDB = True
            if len(points) == 2:
                segment = sm.CreateLine(*coordinates[0], *coordinates[1])
            else:
                segment = self.a._create_spline(sm, coordinates)
            self.require(segment is not None, "Cannot create a spatial spline")
        finally:
            sm.AddToDB = old_add
        feature = self.finish_sketch(name, True)
        if len(points) == 2:
            # A line's underlying ICurve is untrimmed. Its parameter range
            # does not describe the finite sketch segment; read its vertices.
            line = typed(segment, "ISketchLine")
            native_points = [com_get(line, member) for member in
                             ("GetStartPoint2", "GetEndPoint2")]
            actual = [tuple(float(com_get(p, axis)) for axis in ("X", "Y", "Z"))
                      for p in native_points]
            if math.dist(actual[1], coordinates[0]) < math.dist(actual[0], coordinates[0]):
                actual.reverse()
            self.require(all(math.dist(a, b) < 1e-6 for a, b in zip(actual, coordinates)),
                         "Native line endpoints do not match the requested path")
            return feature, tuple(v*1000 for v in actual[0]), unit(
                tuple(b-a for a, b in zip(*actual)))
        curve = typed(com_get(segment, "GetCurve"), "ICurve")
        self.require(curve is not None, "Cannot read the actual path curve")
        ends = curve.GetEndParams()
        # The makepy wrapper returns (retval, start, end, closed, periodic).
        self.require(isinstance(ends, (list, tuple)) and len(ends) == 5
                     and bool(ends[0]), "Cannot read the native path parameter range")
        candidates = [list(curve.Evaluate2(float(t), 1)) for t in ends[1:3]]
        first = min(candidates, key=lambda p: math.dist(p[:3], coordinates[0]))
        self.require(math.dist(first[:3], coordinates[0]) < 1e-6,
                     "Native spline endpoint does not match the requested path")
        return feature, tuple(v*1000 for v in first[:3]), unit(first[3:6])

    def tube(self, name, geometry):
        path, start, tangent = self.path(name+"_Path", geometry["points"])
        plane = self.frame_plane(name+"_Frame", start, tangent)
        sm = self.begin_profile(plane)
        self.stage = "create_tube_section"
        center = self.local(start)
        radius = geometry["radius"]*0.001
        circle = sm.CreateCircle(*center, center[0]+radius, center[1], 0.0)
        self.require(circle is not None, "Cannot create the circular tube section")
        profile = self.finish_sketch(name+"_Section")
        self.stage = "sweep"
        # Use the verified materialized-profile sweep, never the blocking
        # SW2026 circular-profile shortcut.
        result = self.a.create_sweep_feature(
            path_sketch=str(path.Name), profile_sketch=str(profile.Name),
            profile_type="sketch", body_name=name, feature_name=name,
            merge=False, unit="mm")
        self.require(result.get("success"), result.get("message", "Sweep failed"))
        self.components[name] = {
            "feature": result["data"]["feature_name"], "bodies": [name],
            "face_count": result["data"]["face_count"],
            "start": list(start), "start_tangent": list(tangent)}

    def revolved_profile(self, name, center_xz, points, arc=None):
        cx, cz = center_xz
        base, top = points[0][0], points[-1][0]
        plane = self.frame_plane(name+"_Frame", (cx, base, cz), (0, 0, 1))
        sm = self.begin_profile(plane)
        self.stage = "create_revolve_profile"
        def p(y, r):
            return self.local((cx+r, y, cz))
        outline = [(base, 0)] + points + [(top, 0)]
        segments = []
        for a, b in zip(outline, outline[1:]):
            if a == b:
                continue
            if arc and a == points[0] and b == points[-1]:
                segments.append(sm.Create3PointArc(*p(*a), *p(*b), *p(*arc)))
            else:
                segments.append(sm.CreateLine(*p(*a), *p(*b)))
        segments.append(sm.CreateLine(*p(top, 0), *p(base, 0)))
        self.require(all(s is not None for s in segments),
                     "Cannot create the closed revolve profile")
        axis = sm.CreateCenterLine(*p(base-1, 0), *p(top+1, 0))
        self.require(axis is not None, "Cannot create the revolve axis")
        profile = self.finish_sketch(name+"_Profile")
        before = self.body_names()
        self.stage = "revolve"
        result = self.a.revolve_boss(sketch_name=str(profile.Name), merge=False,
                                    feature_name=name, unit="mm")
        self.require(result.get("success"), result.get("message", "Revolve failed"))
        feature = self.a._find_feature(self.doc, name)
        return self.finish_solid(name, feature, before)

    def dome(self, name, geometry):
        radius, height = geometry["radius"], geometry["height"]
        x, base, z = geometry["center"]
        sphere_radius = (radius*radius+height*height)/(2*height)
        center_y = base+height-sphere_radius
        theta = math.atan2(radius, base-center_y)
        mid = (center_y+sphere_radius*math.cos(theta/2),
               sphere_radius*math.sin(theta/2))
        return self.revolved_profile(
            name, (x, z), [(base, radius), (base+height, 0)], arc=mid)

    def revolve(self, name, geometry):
        return self.revolved_profile(
            name, geometry["center_xz"], geometry["profile"])

    def mirror(self, name, geometry):
        seeds = [body for key in geometry["seeds"]
                 for body in self.components[key]["bodies"]]
        before = self.body_names()
        self.stage = "mirror"
        plane_name = str(com_get(self.standard_plane(geometry["plane"]), "Name"))
        result = self.a.mirror_feature(
            mirror_plane=plane_name, mirror_bodies=seeds, merge=False,
            feature_name=name, unit="mm")
        self.require(result.get("success"), result.get("message", "Mirror failed"))
        return self.finish_solid(
            name, self.a._find_feature(self.doc, name), before, expected=len(seeds))

    def compile(self, program):
        self.require(self.doc.SketchManager.ActiveSketch is None,
                     "Close the active sketch before compiling")
        for node in program["nodes"]:
            self.component = node["id"]
            getattr(self, node["kind"])(node["id"], node["geometry"])
            self.components[node["id"]]["kind"] = node["kind"]
        self.stage = "rebuild"
        self.require(bool(com_get(self.doc, "EditRebuild3")), "Native rebuild reported failure")
        for name in self.support:
            f = self.a._find_feature(self.doc, name)
            if f is None:
                continue
            self.doc.ClearSelection2(True)
            if bool(f.Select2(False, 0)):
                kind = str(com_get(f, "GetTypeName2", default=""))
                if kind == "RefPlane":
                    com_get(self.doc, "BlankRefGeom")
                elif kind in {"ProfileFeature", "3DProfileFeature"}:
                    com_get(self.doc, "BlankSketch")
        self.doc.ClearSelection2(True)
        return {"components": self.components, "support_features": self.support,
                "native_parameter_bindings": False,
                "status": "NATIVE_GEOMETRY_CREATED"}
