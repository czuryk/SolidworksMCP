"""
SolidWorks Geometry Probing & Precise Sketching
-----------------------------------------------
Ray probing (the single most-used capability for reverse-engineering
reference geometry) and precise closed-contour sketching in model
coordinates.

IModeler.GetRayIntersections is missing from the SW2026 typelib, so ray
hits are found via Extension.SelectByRay + SelectionManager.GetSelectionPoint2.
"""

import math
import logging
import traceback
from typing import Dict, List, Optional

from ..constants import (SwErrors, SwSelectTypeCode, SwPlanes)
from .com_utils import (com_get, select_by_ray, normalize,
                        transform_point, select_by_id2)

logger = logging.getLogger(__name__)


class GeometryProbeOperations:
    """
    Mixin class for ray probing and precise sketching.

    Requires parent class to have:
    - get_active_doc(): Document access method
    - _result(): Result factory method
    - _units: UnitConverter instance
    """

    # ========================================================================
    # Ray probing
    # ========================================================================

    def _probe_single_ray(self, doc, origin_m, direction, radius_m,
                          sel_type) -> Optional[Dict]:
        """
        Cast one ray, return hit info or None on miss. Coordinates in meters.
        Uses SelectByRay + GetSelectionPoint2 (IModeler.GetRayIntersections
        is absent from the SW2026 typelib).
        """
        try:
            doc.ClearSelection2(True)
        except Exception:
            pass

        if not select_by_ray(doc, origin_m, direction,
                             sel_type=sel_type, radius_m=radius_m):
            return None

        sel_mgr = doc.SelectionManager
        hit = {"entity_type": None, "entity_name": None, "body_name": None,
               "point_m": None}

        # Hit point
        try:
            pt = com_get(sel_mgr, "GetSelectionPoint2", 1, -1, default=None)
            if pt and len(pt) >= 3:
                hit["point_m"] = (pt[0], pt[1], pt[2])
        except Exception as e:
            logger.debug(f"GetSelectionPoint2 failed: {e}")

        # Selected entity / body
        try:
            ent = com_get(sel_mgr, "GetSelectedObject6", 1, -1, default=None)
            if ent is not None:
                hit["entity_name"] = com_get(ent, "GetName", default=None) \
                    or com_get(ent, "Name", default=None)
                body = com_get(ent, "GetBody", default=None)
                if body is not None:
                    hit["body_name"] = com_get(body, "Name", default=None)
        except Exception as e:
            logger.debug(f"GetSelectedObject6 failed: {e}")

        try:
            hit["entity_type"] = com_get(sel_mgr, "GetSelectedObjectType3",
                                        1, -1, default=None)
        except Exception:
            pass

        if hit["point_m"] is None and hit["body_name"] is None:
            return None
        return hit

    def _format_hit(self, hit, origin, direction, unit) -> Dict:
        """Convert a raw hit (meters) to a user-facing dict (display units)."""
        conv = self._units.from_meters
        result = {
            "hit": True,
            "origin": list(origin),
            "direction": list(direction),
            "body_name": hit.get("body_name"),
            "entity_name": hit.get("entity_name"),
            "entity_type": hit.get("entity_type"),
        }
        if hit.get("point_m"):
            p = hit["point_m"]
            result["point"] = [round(conv(p[0], unit), 5),
                               round(conv(p[1], unit), 5),
                               round(conv(p[2], unit), 5)]
            result["distance"] = round(conv(math.sqrt(
                sum((p[i] - self._units.to_meters(origin[i], unit)) ** 2
                    for i in range(3))), unit), 5)
        return result

    def probe_ray(self, origin: List[float], direction: List[float],
                  sel_type: str = "face", radius: float = 0.01,
                  unit: str = None) -> Dict:
        """
        Cast a ray and return the hit point (x,y,z) + body/entity name.
        The workhorse for reverse-engineering reference geometry.

        Args:
            origin: Ray origin [x,y,z] in user units
            direction: Ray direction vector [dx,dy,dz] (need not be normalized)
            sel_type: "face", "edge", "vertex" or "body"
            radius: Ray radius in user units (tolerance)
            unit: Unit for coordinates
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            sel_map = {"face": SwSelectTypeCode.swSelFACES,
                       "edge": SwSelectTypeCode.swSelEDGES,
                       "vertex": SwSelectTypeCode.swSelVERTICES,
                       "body": SwSelectTypeCode.swSelSOLIDBODIES}
            sel_code = int(sel_map.get(sel_type.lower(),
                                       SwSelectTypeCode.swSelFACES))

            origin_m = tuple(self._units.to_meters(c, unit) for c in origin)
            radius_m = self._units.to_meters(radius, unit)

            hit = self._probe_single_ray(doc, origin_m, normalize(direction),
                                         radius_m, sel_code)
            if hit is None:
                return self._result(True,
                    f"Ray missed (origin={origin}, direction={direction})",
                    SwErrors.swSuccess,
                    {"hit": False, "origin": origin, "direction": direction})

            data = self._format_hit(hit, origin, direction, unit)
            return self._result(True,
                f"Hit {data.get('body_name')} at {data.get('point')}",
                SwErrors.swSuccess, data)
        except Exception as e:
            logger.error(f"Probe ray error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def probe_rays(self, rays: List[Dict], sel_type: str = "face",
                   radius: float = 0.01, unit: str = None) -> Dict:
        """
        Batch ray probing (one call, many rays) - the session cast ~300 rays.

        Args:
            rays: List of {"origin": [x,y,z], "direction": [dx,dy,dz]}
            sel_type: "face", "edge", "vertex" or "body" (applies to all)
            radius: Ray radius in user units
            unit: Unit for coordinates
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            sel_map = {"face": SwSelectTypeCode.swSelFACES,
                       "edge": SwSelectTypeCode.swSelEDGES,
                       "vertex": SwSelectTypeCode.swSelVERTICES,
                       "body": SwSelectTypeCode.swSelSOLIDBODIES}
            sel_code = int(sel_map.get(sel_type.lower(),
                                       SwSelectTypeCode.swSelFACES))
            radius_m = self._units.to_meters(radius, unit)

            results = []
            hit_count = 0
            for ray in rays:
                try:
                    origin = ray["origin"]
                    direction = ray["direction"]
                    origin_m = tuple(self._units.to_meters(c, unit)
                                     for c in origin)
                    hit = self._probe_single_ray(
                        doc, origin_m, normalize(direction),
                        radius_m, sel_code)
                    if hit is None:
                        results.append({"hit": False, "origin": origin,
                                        "direction": direction})
                    else:
                        results.append(
                            self._format_hit(hit, origin, direction, unit))
                        hit_count += 1
                except Exception as e:
                    results.append({"hit": False, "error": str(e),
                                    "origin": ray.get("origin")})

            return self._result(True,
                f"{hit_count}/{len(rays)} rays hit",
                SwErrors.swSuccess,
                {"results": results, "total": len(rays), "hits": hit_count})
        except Exception as e:
            logger.error(f"Probe rays error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def probe_section(self, axis: str = "z", value: float = 0.0,
                      origin: List[float] = None, n_rays: int = 36,
                      angle_start: float = 0.0, angle_end: float = 360.0,
                      sel_type: str = "face", radius: float = 0.01,
                      unit: str = None) -> Dict:
        """
        Radar-style section probe: cast a fan of rays inside the section
        plane axis=value and return the r(theta) profile per body. One call
        replaces dozens of hand-built probe_rays for "who is where" section
        analysis.

        Args:
            axis: Section plane normal - "x" (plane YZ), "y" (XZ), "z" (XY)
            value: Plane coordinate along axis (user units)
            origin: Fan centre [x,y,z]; the axis component is overridden by
                    value. Default [0,0,0].
            n_rays: Number of rays (default 36 = every 10 deg for 0..360)
            angle_start, angle_end: Fan range in degrees. theta=0 along the
                    first in-plane axis (z->+X, x->+Y, y->+Z), growing
                    towards the second (z->+Y, x->+Z, y->+X).
            sel_type: "face" | "edge" | "vertex" | "body"
            radius: Ray radius / tolerance (user units)
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            axes = {"z": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), 2),
                    "x": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), 0),
                    "y": ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), 1)}
            axis = (axis or "z").lower()
            if axis not in axes:
                return self._result(False,
                    f"Unknown axis '{axis}' (use x, y or z)",
                    SwErrors.swInvalidInput)
            u, v, ax_idx = axes[axis]

            sel_map = {"face": SwSelectTypeCode.swSelFACES,
                       "edge": SwSelectTypeCode.swSelEDGES,
                       "vertex": SwSelectTypeCode.swSelVERTICES,
                       "body": SwSelectTypeCode.swSelSOLIDBODIES}
            sel_code = int(sel_map.get(sel_type.lower(),
                                       SwSelectTypeCode.swSelFACES))

            origin = list(origin) if origin else [0.0, 0.0, 0.0]
            origin[ax_idx] = float(value)
            origin_m = tuple(self._units.to_meters(c, unit) for c in origin)
            radius_m = self._units.to_meters(radius, unit)

            n_rays = max(1, int(n_rays))
            full_circle = abs((angle_end - angle_start) - 360.0) < 1e-9
            if n_rays == 1:
                step = 0.0
            elif full_circle:
                step = (angle_end - angle_start) / n_rays
            else:
                step = (angle_end - angle_start) / (n_rays - 1)

            conv = self._units.from_meters
            rays_out = []
            per_body: Dict = {}
            hits = 0
            for i in range(n_rays):
                theta = math.radians(angle_start + i * step)
                ct, st = math.cos(theta), math.sin(theta)
                direction = (u[0] * ct + v[0] * st,
                             u[1] * ct + v[1] * st,
                             u[2] * ct + v[2] * st)
                theta_deg = round(angle_start + i * step, 2)
                hit = self._probe_single_ray(doc, origin_m, direction,
                                             radius_m, sel_code)
                if hit is None or not hit.get("point_m"):
                    rays_out.append({"theta": theta_deg, "hit": False})
                    continue
                p = hit["point_m"]
                r_m = math.sqrt(sum((p[i2] - origin_m[i2]) ** 2
                                    for i2 in range(3)))
                body = hit.get("body_name")
                entry = {"theta": theta_deg, "hit": True, "body": body,
                         "point": [round(conv(p[0], unit), 4),
                                   round(conv(p[1], unit), 4),
                                   round(conv(p[2], unit), 4)],
                         "r": round(conv(r_m, unit), 4)}
                rays_out.append(entry)
                hits += 1
                if body:
                    b = per_body.setdefault(body, {"hits": 0,
                                                   "r_min": float("inf"),
                                                   "r_max": 0.0})
                    b["hits"] += 1
                    b["r_min"] = min(b["r_min"], entry["r"])
                    b["r_max"] = max(b["r_max"], entry["r"])
            for b in per_body.values():
                b["r_min"] = round(b["r_min"], 4)
                b["r_max"] = round(b["r_max"], 4)

            unit_str = unit or self._units.default_unit.value
            return self._result(True,
                f"Section {axis}={value}{unit_str}: {hits}/{n_rays} rays "
                f"hit, bodies: {sorted(per_body)}",
                SwErrors.swSuccess,
                {"plane": {"axis": axis, "value": value},
                 "origin": origin, "rays": rays_out,
                 "bodies": per_body, "unit": unit_str,
                 "note": "Rays hit only VISIBLE bodies; first hit per ray"})
        except Exception as e:
            logger.error(f"Probe section error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def select_face_by_ray(self, origin: List[float], direction: List[float],
                           mark: int = 0, append: bool = False,
                           radius: float = 0.01, unit: str = None) -> Dict:
        """
        Select a face by ray with a given Mark, for reference end-conditions.
        SelectByID2 by coordinates is unreliable (tolerance ~0.01mm), so
        ray selection is preferred for offset/up-to-surface references.

        Args:
            origin: Ray origin [x,y,z]
            direction: Ray direction [dx,dy,dz]
            mark: Selection mark (1 = end-condition ref, 32 = start ref, ...)
            append: Append to current selection
            radius: Ray radius in user units
            unit: Unit for coordinates
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            origin_m = tuple(self._units.to_meters(c, unit) for c in origin)
            radius_m = self._units.to_meters(radius, unit)

            if not append:
                try:
                    doc.ClearSelection2(True)
                except Exception:
                    pass

            ok = select_by_ray(doc, origin_m, normalize(direction),
                               sel_type=int(SwSelectTypeCode.swSelFACES),
                               radius_m=radius_m, append=append, mark=int(mark))
            if not ok:
                return self._result(False,
                    f"Ray missed - no face selected (origin={origin}, "
                    f"direction={direction})",
                    SwErrors.swSelectionError,
                    {"origin": origin, "direction": direction, "mark": mark})

            # Report what got selected
            body_name = None
            try:
                sel_mgr = doc.SelectionManager
                ent = com_get(sel_mgr, "GetSelectedObject6", 1, mark,
                              default=None)
                if ent is not None:
                    body = com_get(ent, "GetBody", default=None)
                    if body is not None:
                        body_name = com_get(body, "Name", default=None)
            except Exception:
                pass

            return self._result(True,
                f"Face selected with mark={mark}"
                + (f" on body '{body_name}'" if body_name else ""),
                SwErrors.swSuccess,
                {"mark": mark, "append": append, "body_name": body_name})
        except Exception as e:
            logger.error(f"Select face by ray error: {e}\n"
                         f"{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swSelectionError)

    # ========================================================================
    # Precise contour sketching
    # ========================================================================

    def _get_sketch_transform_data(self, doc):
        """
        Get ModelToSketchTransform ArrayData of the active sketch.
        Returns list of 16 doubles or None.
        """
        try:
            sketch = doc.SketchManager.ActiveSketch
            if sketch is None:
                return None
            xform = com_get(sketch, "ModelToSketchTransform", default=None)
            if xform is None:
                return None
            data = com_get(xform, "ArrayData", default=None)
            return list(data) if data else None
        except Exception as e:
            logger.debug(f"ModelToSketchTransform failed: {e}")
            return None

    def _segments_endpoint_check(self, segments: List[Dict]):
        """
        Pure-math closure check on the requested segments (model coords):
        in a closed contour every endpoint is shared by exactly two
        segments. Centerlines are construction geometry and are excluded.

        Returns (open_endpoints, contour_bbox, contour_segment_count):
        open_endpoints - points used an odd number of times (chain breaks);
        contour_bbox - {"min":[...], "max":[...]} over segment endpoints
        (arc bulges not included - approximate);
        """
        from collections import Counter
        counter = Counter()
        mins = [float("inf")] * 3
        maxs = [float("-inf")] * 3
        n_contour = 0
        for seg in segments or []:
            stype = seg.get("type", "line").lower()
            if stype == "centerline":
                continue
            n_contour += 1
            for key in ("start", "end"):
                p = seg.get(key)
                if not p or len(p) < 3:
                    continue
                # 1e-3 user units (0.001 mm) tolerance via rounding
                counter[tuple(round(float(c), 3) for c in p[:3])] += 1
                for i in range(3):
                    mins[i] = min(mins[i], float(p[i]))
                    maxs[i] = max(maxs[i], float(p[i]))
        open_pts = sorted([list(pt) for pt, n in counter.items() if n % 2 == 1])
        bbox = None
        if n_contour > 0 and mins[0] != float("inf"):
            bbox = {"min": [round(v, 4) for v in mins],
                    "max": [round(v, 4) for v in maxs]}
        return open_pts, bbox, n_contour

    def _count_closed_contours(self, sk_mgr):
        """
        Ask SolidWorks how many closed contours the active sketch has
        (ISketch::GetSketchContours). Returns int or None if the API is
        unavailable. A connected endpoint chain with 0 closed contours
        almost always means a SELF-INTERSECTING contour (arc drawn to the
        wrong side - flip its 'direction' +-1).
        """
        try:
            sketch = sk_mgr.ActiveSketch
            if sketch is None:
                return None
            contours = com_get(sketch, "GetSketchContours", default=None)
            if not contours:
                return 0
            closed = 0
            for c in contours:
                try:
                    if bool(com_get(c, "IsClosed", default=True)):
                        closed += 1
                except Exception:
                    closed += 1
            return closed
        except Exception as e:
            logger.debug(f"GetSketchContours failed: {e}")
            return None

    def sketch_contour(self, plane: str = None, segments: List[Dict] = None,
                       face_ray: Dict = None, add_to_db: bool = True,
                       close: bool = True, unit: str = None) -> Dict:
        """
        Draw a closed contour (lines + arcs) from exact MODEL coordinates,
        transformed into the active sketch's coordinate system via
        ModelToSketchTransform.

        Solves the limitations of draw_line/draw_arc: no AddToDB control
        (risk of snapping to inferences), only Front/Top/Right, no control
        over the sketch coordinate system.

        Args:
            plane: "Front"/"Top"/"Right" to create the sketch on, OR
            face_ray: {"origin": [...], "direction": [...]} to sketch on a face
                      (one of plane/face_ray required if no sketch is active)
            segments: List of segments, each in MODEL coordinates (user units):
                - {"type": "line", "start": [x,y,z], "end": [x,y,z]}
                - {"type": "arc", "center": [x,y,z], "start": [x,y,z],
                   "end": [x,y,z], "direction": 1 or -1}
            add_to_db: SketchManager.AddToDB (True = exact coords, no snapping)
            close: Informational flag (contour is expected pre-closed)
            unit: Unit for coordinates
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err
            if not segments:
                return self._result(False, "segments is required",
                                  SwErrors.swInvalidInput)

            sk_mgr = doc.SketchManager

            # Create a sketch if none active
            created_sketch = False
            if sk_mgr.ActiveSketch is None:
                if plane:
                    plane_name = SwPlanes.get(plane)
                    if not select_by_id2(doc, plane_name, "PLANE"):
                        return self._result(False,
                            f"Could not select {plane_name}",
                            SwErrors.swSelectionError)
                    sk_mgr.InsertSketch(True)
                    created_sketch = True
                elif face_ray:
                    origin_m = tuple(self._units.to_meters(c, unit)
                                     for c in face_ray["origin"])
                    if not select_by_ray(doc, origin_m,
                                        normalize(face_ray["direction"]),
                                        sel_type=int(SwSelectTypeCode.swSelFACES)):
                        return self._result(False,
                            "Face ray for sketch plane missed",
                            SwErrors.swSelectionError)
                    sk_mgr.InsertSketch(True)
                    created_sketch = True
                else:
                    return self._result(False,
                        "No active sketch: provide plane or face_ray",
                        SwErrors.swInvalidInput)

            orientation_before = self._auto_normal_to(
                doc, zoom_to_fit=False)
            if not orientation_before.get("success"):
                return self._result(
                    False,
                    "Contour geometry was not created because Normal To "
                    "verification failed",
                    SwErrors.swSketchError,
                    {"created_sketch": created_sketch,
                     "orientation": orientation_before.get(
                         "data", orientation_before)})

            # Model -> sketch transform
            xform = self._get_sketch_transform_data(doc)
            if xform is None:
                return self._result(False,
                    "Could not read ModelToSketchTransform of active sketch",
                    SwErrors.swSketchError)

            def to_sketch(pt_user):
                pt_m = tuple(self._units.to_meters(c, unit) for c in pt_user)
                return transform_point(xform, pt_m)

            # AddToDB: place exact geometry without snapping to inferences.
            # NOTE: do NOT disable DisplayWhenAdded - AddToDB already bypasses
            # the display update, so entities exist in the DB but are not drawn
            # until a rebuild. A forced rebuild + redraw is issued afterwards.
            old_add_to_db = None
            try:
                old_add_to_db = sk_mgr.AddToDB
                sk_mgr.AddToDB = bool(add_to_db)
            except Exception:
                pass

            created = 0
            errors = []
            try:
                for i, seg in enumerate(segments):
                    stype = seg.get("type", "line").lower()
                    try:
                        if stype == "line":
                            s = to_sketch(seg["start"])
                            e = to_sketch(seg["end"])
                            obj = sk_mgr.CreateLine(s[0], s[1], s[2],
                                                    e[0], e[1], e[2])
                        elif stype == "arc":
                            c = to_sketch(seg["center"])
                            s = to_sketch(seg["start"])
                            e = to_sketch(seg["end"])
                            direction = int(seg.get("direction", 1))
                            obj = sk_mgr.CreateArc(c[0], c[1], c[2],
                                                   s[0], s[1], s[2],
                                                   e[0], e[1], e[2],
                                                   direction)
                        elif stype == "centerline":
                            # Construction line (e.g. revolve axis)
                            s = to_sketch(seg["start"])
                            e = to_sketch(seg["end"])
                            obj = sk_mgr.CreateCenterLine(s[0], s[1], s[2],
                                                          e[0], e[1], e[2])
                        else:
                            errors.append(f"seg[{i}]: unknown type '{stype}'")
                            continue
                        if obj is None:
                            errors.append(f"seg[{i}] ({stype}): create failed")
                        else:
                            created += 1
                    except Exception as se:
                        errors.append(f"seg[{i}] ({stype}): {se}")
            finally:
                if old_add_to_db is not None:
                    try:
                        sk_mgr.AddToDB = old_add_to_db
                    except Exception:
                        pass

            try:
                doc.ClearSelection2(True)
            except Exception:
                pass

            if created == 0:
                return self._result(False,
                    f"No segments created. Errors: {errors}",
                    SwErrors.swSketchError,
                    {"errors": errors, "created_sketch": created_sketch})

            # Redraw so the geometry renders immediately. The real cause of
            # the "invisible lines" bug was DisplayWhenAdded=False (removed);
            # with it enabled a plain GraphicsRedraw2 renders AddToDB geometry,
            # and - unlike EditRebuild3 - it does NOT close the sketch, so the
            # sketch stays open and its name is returned correctly.
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass

            # Re-verify orientation and pixel occupancy after the geometry is
            # visible; this is where Fit to Screen becomes measurable.
            orientation_after = self._auto_normal_to(
                doc, zoom_to_fit=True)

            sketch_name = None
            try:
                sketch_name = com_get(sk_mgr.ActiveSketch, "Name", default=None)
            except Exception:
                pass

            # ---- Contour validation ----
            # A self-intersecting / open contour used to surface only as a
            # cryptic fail-103 on the extrude. Validate right here instead.
            open_pts, contour_bbox, n_contour_segs = \
                self._segments_endpoint_check(segments)
            closed_contours = self._count_closed_contours(sk_mgr)

            data = {"segments_created": created,
                    "segments_total": len(segments),
                    "errors": errors,
                    "sketch_name": sketch_name,
                    "created_sketch": created_sketch,
                    "add_to_db": bool(add_to_db),
                    "closed_contours": closed_contours,
                    "open_endpoints": open_pts,
                    "contour_bbox": contour_bbox,
                    "orientation": {
                        "before_geometry": orientation_before.get("data", {}),
                        "after_geometry": orientation_after.get("data", {}),
                    }}

            # Strict closure check only when the caller expects a finished
            # closed contour (close=true, the default). Pass close=false
            # when drawing a partial chain over several calls.
            if close and n_contour_segs > 0 and not errors:
                if open_pts:
                    return self._result(False,
                        f"Contour drawn ({created} segments) but NOT "
                        f"closed: {len(open_pts)} open endpoint(s) at "
                        f"{open_pts[:4]}. Fix the segment chain (or pass "
                        f"close=false for an intentional partial chain).",
                        SwErrors.swSketchError, data)
                if closed_contours == 0:
                    return self._result(False,
                        f"Contour drawn ({created} segments), endpoint "
                        f"chain is connected, but SolidWorks reports 0 "
                        f"closed contours - the contour is almost certainly "
                        f"SELF-INTERSECTING. Check arc 'direction' (+-1: "
                        f"sign selects which side the arc bulges to).",
                        SwErrors.swSketchError, data)

            if not orientation_after.get("success"):
                data["geometry_created"] = True
                return self._result(
                    False,
                    "Contour was created, but Normal To / Fit to Screen "
                    "read-back verification failed",
                    SwErrors.swSketchError, data)

            closure_note = ""
            if closed_contours is not None:
                closure_note = f", {closed_contours} closed contour(s)"

            return self._result(
                len(errors) == 0,
                f"Contour: {created}/{len(segments)} segments created"
                + (f", {len(errors)} error(s)" if errors else "")
                + closure_note,
                SwErrors.swSuccess if not errors else SwErrors.swSketchError,
                data)
        except Exception as e:
            logger.error(f"Sketch contour error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swSketchError)
