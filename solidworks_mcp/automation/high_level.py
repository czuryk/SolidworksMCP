"""Verified high-level CAD patterns and atomic export bundles."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import pythoncom
import win32com.client

from ..constants import SwErrors
from .com_utils import (com_get, create_select_data, feature_face_count,
                        resolve_solidworks_constant, select_by_id2, typed)
from .runtime import atomic_json_write, structured_error


logger = logging.getLogger(__name__)


class HighLevelOperations:
    def _rename_new_body(self, before_names, requested):
        bodies = self.list_bodies(include_hidden=True)
        if not bodies.get("success"):
            return bodies
        current = [item["name"] for item in bodies["data"]["bodies"]]
        new_names = [name for name in current if name not in before_names]
        if len(new_names) != 1:
            return self._error(
                "UNEXPECTED_BODY_MERGE",
                f"Expected one new body, found {new_names}",
                details={"before": before_names, "after": current})
        renamed = self.rename_body(new_names[0], requested)
        if not renamed.get("success"):
            return renamed
        return self._result(True, f"Body renamed to '{requested}'",
                            SwErrors.swSuccess,
                            {"old_name": new_names[0], "body_name": requested})

    def rename_new_body(self, before_names, body_name):
        """Transaction-safe wrapper for naming exactly one newly created body."""
        return self._rename_new_body(list(before_names or []), body_name)

    def verify_named_body(self, body_name: str, unit: str = None,
                          min_volume: float = 1e-12) -> Dict:
        """Strictly verify that a named solid has faces, bbox and volume."""
        listing = self.list_bodies(include_hidden=True, unit=unit)
        if not listing.get("success"):
            return listing
        record = next((item for item in listing["data"]["bodies"]
                       if item.get("name") == body_name), None)
        if record is None:
            return self._error(
                "INVARIANT_FAILED", f"Body '{body_name}' was not found",
                details={"existing_bodies": [
                    item.get("name") for item in listing["data"]["bodies"]]})
        face_count = int(record.get("face_count") or 0)
        bbox = record.get("bbox")
        if face_count <= 0 or not bbox:
            return self._error(
                "FEATURE_DEAD", f"Body '{body_name}' has no verified faces/bbox",
                details={"body": record})
        bounds = [*bbox.get("min", []), *bbox.get("max", [])]
        if len(bounds) != 6 or not all(math.isfinite(float(v)) for v in bounds):
            return self._error(
                "FEATURE_DEAD", f"Body '{body_name}' bbox is invalid",
                details={"body": record})
        mass = self.body_volume(body_name, unit=unit)
        mass_items = ((mass.get("data") or {}).get("bodies") or [])
        mass_record = mass_items[0] if mass.get("success") and mass_items else {}
        volume = mass_record.get("volume_exact", mass_record.get("volume"))
        if volume is None or not math.isfinite(float(volume)) or float(
                volume) <= float(min_volume):
            return self._error(
                "FEATURE_DEAD", f"Body '{body_name}' has no positive volume",
                details={"body": record, "mass_properties": mass.get("data")})
        return self._result(
            True, f"Body '{body_name}' passed faces/bbox/volume verification",
            SwErrors.swSuccess,
            {"body_name": body_name, "body": record,
             "mass_properties": mass_record, "unit": unit or
             self._units.default_unit.value})

    def create_revolved_body(self, sketch: Dict[str, Any],
                             revolve: Dict[str, Any],
                             body_name: str,
                             feature_name: str = None,
                             checkpoint: bool = True,
                             save_path: str = None,
                             allow_unsaved_document: bool = False,
                             idempotency_key: str = None,
                             budget: Dict[str, Any] = None,
                             ui_guard: Dict[str, Any] = None,
                             recovery: Dict[str, Any] = None) -> Dict:
        before = self.list_bodies(include_hidden=True)
        if not before.get("success"):
            return before
        before_names = [item["name"] for item in before["data"]["bodies"]]
        if body_name in before_names:
            return self._error(
                "INVARIANT_FAILED",
                f"Body name '{body_name}' already exists; use a unique name")
        sketch_args = dict(sketch or {})
        revolve_args = dict(revolve or {})
        if revolve_args.get("merge") is True:
            return self._error(
                "INVALID_PLAN",
                "create_revolved_body creates a distinct named body and "
                "therefore requires revolve.merge=false")
        revolve_args["merge"] = False
        if feature_name:
            revolve_args["feature_name"] = feature_name
        else:
            revolve_args.setdefault("feature_name", f"{body_name}_Revolve")
        revolve_args.setdefault("sketch_name", "$steps.0.data.sketch_name")
        operations = [
            {"op": "create_parametric_sketch", "args": sketch_args},
            {"op": "revolve_boss", "args": revolve_args},
            {"op": "rename_new_body", "args": {
                "before_names": before_names, "body_name": body_name}},
            {"op": "verify_named_body", "args": {
                "body_name": body_name,
                "unit": sketch_args.get("unit", "mm")}},
        ]
        result = self.run_transaction(
            name=idempotency_key or f"create_revolved_body:{body_name}",
            operations=operations,
            checkpoint={"mode": "save_copy" if checkpoint else "none"},
            invariants={"solid_body_count": len(before_names) + 1,
                        "required_bodies": [body_name],
                        "no_modal_dialog": True},
            idempotency_key=idempotency_key,
            budget=budget,
            save_path=save_path,
            allow_unsaved_document=allow_unsaved_document)
        if not result.get("success"):
            return result
        steps = result["data"].get("steps", [])
        verification = ((steps[3].get("data") or {})
                        if len(steps) > 3 else None)
        result["data"].update({
            "body_name": body_name,
            "feature_name": ((steps[1].get("data") or {}).get(
                "feature_name") if len(steps) > 1 else None),
            "body_verification": verification,
            "api_contract": {"merge": False, "atomic_naming": True,
                             "checkpoint": bool(checkpoint)}})
        return result

    @staticmethod
    def _polyline_minimum_radius(points):
        minimum = None
        for index in range(1, len(points) - 1):
            a, b, c = points[index - 1:index + 2]
            ab, bc, ac = math.dist(a, b), math.dist(b, c), math.dist(a, c)
            cross = abs((b[0] - a[0]) * (c[1] - a[1]) -
                        (b[1] - a[1]) * (c[0] - a[0]))
            if min(ab, bc, ac) <= 1e-12 or cross <= 1e-12:
                continue
            radius = ab * bc * ac / (2.0 * cross)
            minimum = radius if minimum is None else min(minimum, radius)
        return minimum

    def _path_quality(self, path_sketch, min_bend_radius, unit,
                      allow_sharp_corners=False):
        result, geometry = self._load_geometry_payload(
            path_sketch, unit, include={
                "relations": False, "dimensions": False,
                "equations": False, "topology": True})
        if geometry is None:
            return result, None
        return result, self._path_quality_from_geometry(
            geometry, min_bend_radius, allow_sharp_corners)

    def _path_quality_from_geometry(self, geometry, min_bend_radius,
                                    allow_sharp_corners=False):
        """Validate declared geometry without a redundant SOLIDWORKS read-back."""
        entities = [entity for entity in geometry["entities"]
                    if not entity.get("construction")]
        if not entities:
            return {"pass": False, "reason": "empty_path"}
        sample_step = max(0.01, min(0.25,
            float(min_bend_radius or 2.0) / 40.0))
        linework, endpoint_vectors = [], {}
        radii, spline_radii = [], []
        logger.info(
            "Sweep path quality stage=sample begin entities=%s step=%s",
            len(entities), sample_step)
        for entity_index, entity in enumerate(entities):
            points, _ = self._sample_geometry(
                {"entities": [entity]}, sample_step)
            compact = []
            for point in points:
                xy = [float(point[0]), float(point[1])]
                if not compact or math.dist(compact[-1], xy) > 1e-10:
                    compact.append(xy)
            if len(compact) < 2:
                continue
            linework.append((entity_index, compact))
            if entity.get("type") == "arc" and entity.get("radius") is not None:
                radii.append(float(entity["radius"]))
            elif entity.get("type") in {"spline", "b_spline"}:
                radius = self._polyline_minimum_radius(compact)
                if radius is not None:
                    spline_radii.append(radius)
            tolerance = max(1e-6, sample_step * 0.01)
            for point, vector in (
                    (compact[0], [compact[1][0] - compact[0][0],
                                  compact[1][1] - compact[0][1]]),
                    (compact[-1], [compact[-2][0] - compact[-1][0],
                                   compact[-2][1] - compact[-1][1]])):
                length = math.hypot(*vector)
                if length > 1e-12:
                    key = tuple(round(value / tolerance) for value in point)
                    endpoint_vectors.setdefault(key, []).append(
                        [vector[0] / length, vector[1] / length])
        if not linework:
            return {"pass": False, "reason": "unsampled_path"}
        logger.info(
            "Sweep path quality stage=sample complete paths=%s",
            len(linework))

        # GEOS/Shapely can deadlock when lazily loaded into the same Windows
        # process after SOLIDWORKS COM and its native modules are active.
        # A uniform-grid segment test is deterministic, dependency-free and
        # preserves exact endpoint/branch semantics for tessellated paths.
        segments = []
        for entity_index, points in linework:
            closed = math.dist(points[0], points[-1]) <= 1e-10
            for segment_index, (start, end) in enumerate(
                    zip(points, points[1:])):
                segments.append({
                    "entity": entity_index,
                    "index": segment_index,
                    "count": len(points) - 1,
                    "closed": closed,
                    "path_start": points[0],
                    "path_end": points[-1],
                    "start": start,
                    "end": end,
                })

        def close_point(first, second, tolerance):
            return (abs(first[0] - second[0]) <= tolerance and
                    abs(first[1] - second[1]) <= tolerance)

        def cross(first, second, third):
            return ((second[0] - first[0]) * (third[1] - first[1]) -
                    (second[1] - first[1]) * (third[0] - first[0]))

        def forbidden_intersection(first, second):
            points = [first["start"], first["end"],
                      second["start"], second["end"]]
            scale = max(1.0, *(abs(value) for point in points
                               for value in point))
            tolerance = 1e-10 * scale
            a, b, c, d = points
            if (max(a[0], b[0]) + tolerance < min(c[0], d[0]) or
                    max(c[0], d[0]) + tolerance < min(a[0], b[0]) or
                    max(a[1], b[1]) + tolerance < min(c[1], d[1]) or
                    max(c[1], d[1]) + tolerance < min(a[1], b[1])):
                return False
            values = [cross(a, b, c), cross(a, b, d),
                      cross(c, d, a), cross(c, d, b)]
            collinear = all(abs(value) <= tolerance for value in values)
            if collinear:
                axis = 0 if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else 1
                overlap = (min(max(a[axis], b[axis]), max(c[axis], d[axis])) -
                           max(min(a[axis], b[axis]), min(c[axis], d[axis])))
                if overlap > tolerance:
                    return True
            proper = (values[0] * values[1] < -(tolerance * tolerance) and
                      values[2] * values[3] < -(tolerance * tolerance))
            if proper:
                return True

            common_endpoint = any(
                close_point(left, right, tolerance)
                for left in (a, b) for right in (c, d))
            touches = (
                (abs(values[0]) <= tolerance and
                 min(a[0], b[0]) - tolerance <= c[0] <=
                 max(a[0], b[0]) + tolerance and
                 min(a[1], b[1]) - tolerance <= c[1] <=
                 max(a[1], b[1]) + tolerance) or
                (abs(values[1]) <= tolerance and
                 min(a[0], b[0]) - tolerance <= d[0] <=
                 max(a[0], b[0]) + tolerance and
                 min(a[1], b[1]) - tolerance <= d[1] <=
                 max(a[1], b[1]) + tolerance) or
                (abs(values[2]) <= tolerance and
                 min(c[0], d[0]) - tolerance <= a[0] <=
                 max(c[0], d[0]) + tolerance and
                 min(c[1], d[1]) - tolerance <= a[1] <=
                 max(c[1], d[1]) + tolerance) or
                (abs(values[3]) <= tolerance and
                 min(c[0], d[0]) - tolerance <= b[0] <=
                 max(c[0], d[0]) + tolerance and
                 min(c[1], d[1]) - tolerance <= b[1] <=
                 max(c[1], d[1]) + tolerance))
            if not touches:
                return False
            if not common_endpoint:
                return True
            if first["entity"] != second["entity"]:
                source_common_endpoint = any(
                    close_point(left, right, tolerance)
                    for left in (first["path_start"], first["path_end"])
                    for right in (second["path_start"], second["path_end"]))
                return not source_common_endpoint
            adjacent = abs(first["index"] - second["index"]) == 1
            closes_path = bool(
                first["closed"] and second["closed"] and
                {first["index"], second["index"]} ==
                {0, first["count"] - 1})
            return not (adjacent or closes_path)

        cell_size = max(1e-6, sample_step * 2.0)
        buckets, candidate_pairs = {}, set()
        for segment_index, segment in enumerate(segments):
            start, end = segment["start"], segment["end"]
            min_x = math.floor(min(start[0], end[0]) / cell_size)
            max_x = math.floor(max(start[0], end[0]) / cell_size)
            min_y = math.floor(min(start[1], end[1]) / cell_size)
            max_y = math.floor(max(start[1], end[1]) / cell_size)
            for cell_x in range(min_x, max_x + 1):
                for cell_y in range(min_y, max_y + 1):
                    key = (cell_x, cell_y)
                    for other_index in buckets.get(key, []):
                        candidate_pairs.add((other_index, segment_index))
                    buckets.setdefault(key, []).append(segment_index)
        logger.info(
            "Sweep path quality stage=intersections begin segments=%s pairs=%s",
            len(segments), len(candidate_pairs))
        forbidden_count = sum(
            1 for first, second in candidate_pairs
            if forbidden_intersection(segments[first], segments[second]))
        is_simple = forbidden_count == 0
        logger.info(
            "Sweep path quality stage=intersections complete forbidden=%s",
            forbidden_count)
        branch_points = sum(1 for vectors in endpoint_vectors.values()
                            if len(vectors) > 2)
        corner_angles = []
        for vectors in endpoint_vectors.values():
            if len(vectors) != 2:
                continue
            dot = max(-1.0, min(1.0,
                vectors[0][0] * vectors[1][0] +
                vectors[0][1] * vectors[1][1]))
            deflection = math.degrees(math.pi - math.acos(dot))
            if deflection > 1.0:
                corner_angles.append(deflection)
        minimum = min([*radii, *spline_radii], default=None)
        radius_ok = (min_bend_radius is None or minimum is None or
                     minimum >= float(min_bend_radius))
        corners_ok = bool(allow_sharp_corners or not corner_angles)
        passed = bool(is_simple and branch_points == 0 and radius_ok and
                      corners_ok)
        return {
            "pass": passed, "is_simple": is_simple,
            "branch_point_count": branch_points,
            "sharp_corner_count": len(corner_angles),
            "maximum_corner_deflection_deg": max(corner_angles, default=0.0),
            "sharp_corners_allowed": bool(allow_sharp_corners),
            "minimum_arc_radius": min(radii, default=None),
            "minimum_spline_radius_estimate": min(spline_radii, default=None),
            "minimum_bend_radius": minimum,
            "required_min_bend_radius": min_bend_radius,
            "spline_curvature_checked": True,
            "sample_step": sample_step,
            "entity_count": len(entities),
            "contour_count": len(geometry.get("contours") or []),
            "intersection_count": forbidden_count,
            "quality_engine": "native_segment_grid",
        }

    def validate_sweep_path(self, path_sketch: str,
                            min_bend_radius: float = None,
                            unit: str = None,
                            allow_sharp_corners: bool = False) -> Dict:
        source, quality = self._path_quality(
            path_sketch, min_bend_radius,
            unit or self._units.default_unit.value, allow_sharp_corners)
        if quality is None:
            return source
        if not quality.get("pass"):
            return self._error(
                "FEATURE_DEAD", "Sweep path quality gate failed",
                details=quality)
        return self._result(
            True, f"Sweep path '{path_sketch}' passed quality validation",
            SwErrors.swSuccess, {"path_sketch": path_sketch,
                                 "path_quality": quality})

    def validate_sweep_profile(self, profile_sketch: str,
                               unit: str = None) -> Dict:
        source, geometry = self._load_geometry_payload(
            profile_sketch, unit or self._units.default_unit.value,
            include={"relations": False, "dimensions": False,
                     "equations": False, "topology": True})
        if geometry is None:
            return source
        entities = [entity for entity in geometry["entities"]
                    if not entity.get("construction")]
        contours = geometry.get("contours") or []
        entity = entities[0] if len(entities) == 1 else {}
        start, end = entity.get("start"), entity.get("end")
        coincident_ends = bool(
            start and end and len(start) >= 2 and len(end) >= 2 and
            math.dist(start[:2], end[:2]) <= 1e-6)
        intrinsic_closed = bool(len(entities) == 1 and (
            entity.get("type") in {"ellipse", "circle"} or
            (entity.get("nurbs") or {}).get("closed", False) or
            (entity.get("type") == "arc" and
             (coincident_ends or not start or not end))))
        closed_count = sum(1 for contour in contours if contour.get("closed"))
        if intrinsic_closed:
            closed_count = 1
        if len(entities) < 1 or closed_count != 1 or len(contours) > 1:
            return self._error(
                "SKETCH_OPEN_CONTOUR",
                "Sweep sketch profile must contain exactly one closed contour",
                details={"profile_sketch": profile_sketch,
                         "entity_count": len(entities),
                         "contours": contours,
                         "intrinsic_closed": intrinsic_closed})
        return self._result(
            True, f"Sweep profile '{profile_sketch}' is one closed contour",
            SwErrors.swSuccess,
            {"profile_sketch": profile_sketch,
             "entity_count": len(entities), "closed_contours": 1})

    @staticmethod
    def _ellipse_profile_sketch(profile, body_name, unit):
        major = float(profile.get("major_radius", 0.0))
        minor = float(profile.get("minor_radius", 0.0))
        if (not math.isfinite(major) or not math.isfinite(minor) or
                major <= 0.0 or minor <= 0.0):
            raise ValueError(
                "Elliptical profile requires positive major_radius and "
                "minor_radius")
        return {
            "name": profile.get("name", f"{body_name}_Profile"),
            "plane": profile.get("plane", "Top"),
            "unit": unit,
            "entities": [{
                "id": "ellipse", "type": "ellipse",
                "center": profile.get("center", [0.0, 0.0]),
                "major_radius": major, "minor_radius": minor,
                "rotation_deg": float(profile.get("rotation_deg", 0.0))}],
            "constraints": [], "dimensions": [], "equations": [],
            "solve": {},
            "validation": {"require_closed": True,
                           "closed_contours": 1},
            "transaction": {"rollback_on_failure": True},
        }

    @staticmethod
    def _circle_profile_sketch(profile, body_name, unit):
        diameter = float(profile.get(
            "diameter", float(profile.get("radius", 1.0)) * 2.0))
        if not math.isfinite(diameter) or diameter <= 0.0:
            raise ValueError("Circular profile diameter must be positive")
        return {
            "name": profile.get("name", f"{body_name}_Profile"),
            "plane": profile.get("plane", "Top"),
            "unit": unit,
            "entities": [{
                "id": "circle", "type": "circle",
                "center": profile.get("center", [0.0, 0.0]),
                "radius": diameter / 2.0}],
            "constraints": [], "dimensions": [], "equations": [],
            "solve": {},
            "validation": {"require_closed": True,
                           "closed_contours": 1},
            "transaction": {"rollback_on_failure": True},
        }

    @staticmethod
    def _standard_plane_model_point(plane, point):
        name = str(plane or "").strip().lower()
        x, y = float(point[0]), float(point[1])
        if name.startswith("front"):
            return [x, y, 0.0]
        if name.startswith("top"):
            return [x, 0.0, y]
        if name.startswith("right"):
            return [0.0, x, y]
        return None

    @classmethod
    def _declared_path_profile_contact(cls, path_args, profile_args,
                                       tolerance=1e-4):
        """Preflight that a declared path touches a standard profile plane."""
        profile_plane = str(profile_args.get("plane", ""))
        plane_axis = None
        lowered = profile_plane.strip().lower()
        if lowered.startswith("front"):
            plane_axis = 2
        elif lowered.startswith("top"):
            plane_axis = 1
        elif lowered.startswith("right"):
            plane_axis = 0
        if plane_axis is None:
            return {"checked": False, "reason": "nonstandard_profile_plane"}
        candidates = []
        for entity in path_args.get("entities") or []:
            if entity.get("construction"):
                continue
            for key in ("start", "end"):
                point = entity.get(key)
                if point and len(point) >= 2:
                    model = cls._standard_plane_model_point(
                        path_args.get("plane"), point)
                    if model is not None:
                        candidates.append({"entity_id": entity.get("id"),
                                           "endpoint": key,
                                           "model_point": model})
        if not candidates:
            return {"checked": False, "reason": "nonstandard_or_unsampled_path"}
        nearest = min(candidates,
                      key=lambda item: abs(item["model_point"][plane_axis]))
        distance = abs(float(nearest["model_point"][plane_axis]))
        return {"checked": True, "pass": distance <= float(tolerance),
                "profile_plane": profile_plane,
                "distance_to_profile_plane": distance,
                "tolerance": float(tolerance),
                "contact_endpoint": nearest}

    @staticmethod
    def _select_sweep_path_segments(doc, path_feature, append=False):
        """Select real path curves with Mark=4, never the sketch container."""
        sketch = com_get(path_feature, "GetSpecificFeature2", default=None)
        segments = (com_get(sketch, "GetSketchSegments", default=None)
                    if sketch is not None else None)
        if not segments:
            return 0
        selection_data = create_select_data(doc, 4)
        selected = 0
        for segment in segments:
            if bool(com_get(segment, "ConstructionGeometry", default=False)):
                continue
            try:
                ok = bool(segment.Select4(bool(append or selected),
                                          selection_data))
            except Exception:
                ok = False
            if ok:
                selected += 1
        return selected

    def create_sweep_feature(self, path_sketch: str,
                             profile_type: str, body_name: str,
                             profile_sketch: str = None,
                             diameter: float = None,
                             feature_name: str = None,
                             merge: bool = False,
                             unit: str = None,
                             auto_verify: bool = True,
                             advanced_smoothing: bool = True) -> Dict:
        doc, err = self.get_active_doc()
        if err:
            return err
        self.ensure_features_not_frozen(doc)
        try:
            if doc.SketchManager.ActiveSketch is not None:
                doc.SketchManager.InsertSketch(True)
        except Exception:
            pass
        path_feature = self._find_sketch_feature(doc, path_sketch)
        if path_feature is None:
            return self._error(
                "SKETCH_OPEN_CONTOUR", f"Path sketch '{path_sketch}' not found")
        circular = (profile_type in {"circle", "circular"} and
                    not profile_sketch)
        if circular:
            return self._error(
                "CAPABILITY_UNAVAILABLE",
                "Native circular-profile sweep is disabled on SW2026 because "
                "both legacy and ISweepFeatureData creation can block the COM "
                "server; create_swept_member materializes a sketch circle "
                "instead")
        profile_feature = None
        profile_feature = self._find_sketch_feature(doc, profile_sketch)
        if profile_feature is None:
            return self._error(
                "SKETCH_OPEN_CONTOUR",
                f"Profile sketch '{profile_sketch}' not found")
        diameter_m = 0.0
        before = self.list_bodies(include_hidden=True, unit=unit)
        if not before.get("success"):
            return before
        before_names = [item["name"] for item in before["data"]["bodies"]]
        doc.ClearSelection2(True)
        try:
            logger.info(
                "Sweep stage=select profile=%s path=%s profile_type=%s",
                profile_sketch, path_sketch, profile_type)
            selected_profile = bool(profile_feature.Select2(False, 1))
            if not selected_profile:
                selected_profile = select_by_id2(
                    doc, profile_sketch, "SKETCH", mark=1)
            try:
                selected_path = bool(path_feature.Select2(True, 4))
            except Exception:
                selected_path = False
            if not selected_path:
                selected_path = select_by_id2(
                    doc, path_sketch, "SKETCH", append=True, mark=4)
            selected = bool(selected_profile and selected_path)
            selection_contract = {
                "path_mark": 4, "profile_mark": 1,
                "path_selection": "sketch_feature",
                "path_segments_selected": None,
                "profile_strategy": "materialized_sketch"}
            logger.info(
                "Sweep stage=selected profile_ok=%s path_ok=%s",
                selected_profile, selected_path)
            if not selected:
                return self._error(
                    "SELECTION_FAILED", "Sweep profile/path selection failed",
                    details={"path_sketch": path_sketch,
                             "profile_sketch": profile_sketch,
                             "selection_contract": selection_contract})
            fm = typed(doc.FeatureManager, "IFeatureManager")
            if fm is None:
                return self._error(
                    "CAPABILITY_UNAVAILABLE",
                    "Typed IFeatureManager is unavailable")
            try:
                logger.info("Sweep stage=create_definition begin")
                sweep_data = fm.CreateDefinition(
                    int(resolve_solidworks_constant("swFmSweep")))
                if sweep_data is None:
                    return self._error(
                        "CAPABILITY_UNAVAILABLE",
                        "CreateDefinition(swFmSweep) returned no feature data")
                logger.info("Sweep stage=create_definition complete")
                sweep_data.TangentPropagation = False
                sweep_data.AlignWithEndFaces = False
                sweep_data.TwistControlType = 0
                sweep_data.MaintainTangency = False
                sweep_data.AdvancedSmoothing = bool(advanced_smoothing)
                sweep_data.StartTangencyType = 0
                sweep_data.EndTangencyType = 0
                sweep_data.ThinFeature = False
                sweep_data.SetWallThickness(True, 0.0)
                sweep_data.SetWallThickness(False, 0.0)
                sweep_data.ThinWallType = 0
                sweep_data.PathAlignmentType = 0
                sweep_data.Merge = bool(merge)
                sweep_data.FeatureScope = True
                sweep_data.AutoSelect = True
                sweep_data.SetTwistAngle(0.0)
                sweep_data.MergeSmoothFaces = True
                sweep_data.CircularProfile = bool(circular)
                sweep_data.CircularProfileDiameter = diameter_m
                sweep_data.Direction = 0
                logger.info("Sweep stage=configure_definition complete")
                logger.info("Sweep stage=create_feature begin")
                feat = fm.CreateFeature(sweep_data)
                logger.info("Sweep stage=create_feature complete result=%s",
                            bool(feat is not None))
            except Exception as exc:
                return self._error(
                    "COM_MEMBER_MISMATCH",
                    f"ISweepFeatureData/CreateFeature failed: {exc}",
                    com_hresult=getattr(exc, "hresult", None),
                    details={"selection_contract": selection_contract})
        finally:
            try:
                doc.ClearSelection2(True)
            except Exception:
                pass
        if feat is None or (auto_verify and feature_face_count(feat) <= 0):
            if feat is not None:
                self._delete_feature_object(
                    doc, feat, str(com_get(feat, "Name", default="Sweep")), True)
            return self._error(
                "FEATURE_DEAD", "SolidWorks could not create a live sweep",
                details={"selection_contract": selection_contract})
        if feature_name:
            actual_name, warning = self._rename_feature_safe(
                doc, feat, feature_name)
        else:
            actual_name = str(com_get(feat, "Name", default="Sweep"))
            warning = None
        if not merge:
            renamed = self._rename_new_body(before_names, body_name)
            if not renamed.get("success"):
                self._delete_feature_object(doc, feat, actual_name, True)
                return renamed
        verification = self.verify_named_body(body_name, unit=unit)
        if not verification.get("success"):
            self._delete_feature_object(doc, feat, actual_name, True)
            return self._error(
                "FEATURE_DEAD", "Sweep body verification failed",
                details={"verification": verification})
        return self._result(
            True, f"Swept member '{body_name}' created and verified",
            SwErrors.swSuccess,
            {"feature_name": actual_name, "body_name": body_name,
             "profile_type": profile_type,
             "profile_sketch": profile_sketch,
             "diameter": diameter if circular else None,
             "face_count": feature_face_count(feat),
             "rename_warning": warning,
             "selection_contract": selection_contract,
             "creation_api": "ISweepFeatureData+CreateFeature",
             "profile_strategy": "materialized_sketch",
             "body_verification": verification.get("data")})

    def create_swept_member(self, path_sketch: str = None,
                            profile: Dict[str, Any] = None,
                            body_name: str = None,
                            feature_name: str = None,
                            merge: bool = False,
                            min_bend_radius: float = None,
                            unit: str = None,
                            auto_verify: bool = True,
                            path: Dict[str, Any] = None,
                            allow_sharp_corners: bool = False,
                            checkpoint: bool = True,
                            save_path: str = None,
                            allow_unsaved_document: bool = False,
                            idempotency_key: str = None,
                            budget: Dict[str, Any] = None,
                            ui_guard: Dict[str, Any] = None,
                            recovery: Dict[str, Any] = None) -> Dict:
        logger.info(
            "create_swept_member stage=enter body=%s path_created=%s checkpoint=%s",
            body_name, bool(path), bool(checkpoint))
        profile = dict(profile or {})
        unit = unit or self._units.default_unit.value
        profile_type = str(profile.get("type", "circle")).lower()
        if profile_type not in {
                "circle", "circular", "ellipse", "elliptical",
                "custom", "sketch"}:
            return self._error(
                "INVALID_PLAN", f"Unsupported sweep profile '{profile_type}'")
        if not body_name:
            return self._error("INVALID_PLAN", "body_name is required")
        if path_sketch and path:
            return self._error(
                "INVALID_PLAN", "Provide path_sketch or path, not both")
        if not path_sketch and not path:
            return self._error(
                "INVALID_PLAN", "path_sketch or path is required")
        logger.info("create_swept_member stage=list_bodies_before begin")
        before = self.list_bodies(include_hidden=True, unit=unit)
        logger.info("create_swept_member stage=list_bodies_before complete success=%s",
                    bool(before.get("success")))
        if not before.get("success"):
            return before
        before_names = [item["name"] for item in before["data"]["bodies"]]
        if not merge and body_name in before_names:
            return self._error(
                "INVARIANT_FAILED", f"Body name '{body_name}' already exists")
        if merge and body_name not in before_names:
            return self._error(
                "INVARIANT_FAILED",
                "For merge=true, body_name must name the existing target body")
        operations = []
        path_args = None
        path_created = False
        profile_created = False
        declared_path_quality = None
        if path:
            path_args = dict(path)
            if not path_args.get("name"):
                path_args["name"] = f"{body_name}_Path"
            path_args.setdefault("unit", unit)
            if str(path_args.get("unit")).lower() != str(unit).lower():
                return self._error(
                    "INVALID_PLAN",
                    "Created sweep path and profile must use the same unit")
            operations.append({"op": "create_parametric_sketch",
                               "args": path_args})
            path_created = True
            path_ref = f"$steps.{len(operations) - 1}.data.sketch_name"
        else:
            path_ref = path_sketch
        profile_ref = profile.get("sketch_name")
        profile_radius = None
        diameter = None
        profile_sketch_args = None
        if profile_type in {"circle", "circular"}:
            diameter = float(profile.get(
                "diameter", float(profile.get("radius", 1.0)) * 2.0))
            if not math.isfinite(diameter) or diameter <= 0.0:
                return self._error(
                    "INVALID_PLAN", "Circular profile diameter must be positive")
            profile_radius = diameter / 2.0
            profile_sketch_args = profile.get("sketch")
            if profile_sketch_args:
                profile_sketch_args = dict(profile_sketch_args)
            elif not profile_ref:
                try:
                    profile_sketch_args = self._circle_profile_sketch(
                        profile, body_name, unit)
                except ValueError as exc:
                    return self._error("INVALID_PLAN", str(exc))
            if profile_sketch_args:
                profile_sketch_args.setdefault("unit", unit)
                if str(profile_sketch_args.get("unit")).lower() != str(
                        unit).lower():
                    return self._error(
                        "INVALID_PLAN",
                        "Created sweep path and profile must use the same unit")
                profile_validation = dict(
                    profile_sketch_args.get("validation") or {})
                profile_validation.update({"require_closed": True,
                                           "closed_contours": 1})
                profile_sketch_args["validation"] = profile_validation
                operations.append({"op": "create_parametric_sketch",
                                   "args": profile_sketch_args})
                profile_created = True
                profile_ref = f"$steps.{len(operations) - 1}.data.sketch_name"
            if not profile_ref:
                return self._error(
                    "INVALID_PLAN",
                    "Circular profile requires sketch_name or a generated "
                    "circle profile")
            if not profile_created:
                operations.append({"op": "validate_sweep_profile", "args": {
                    "profile_sketch": profile_ref, "unit": unit}})
        else:
            profile_sketch_args = profile.get("sketch")
            if profile_sketch_args:
                profile_sketch_args = dict(profile_sketch_args)
            elif profile_type in {"ellipse", "elliptical"} and not profile_ref:
                try:
                    profile_sketch_args = self._ellipse_profile_sketch(
                        profile, body_name, unit)
                except ValueError as exc:
                    return self._error("INVALID_PLAN", str(exc))
            if profile_sketch_args:
                profile_sketch_args.setdefault("unit", unit)
                if str(profile_sketch_args.get("unit")).lower() != str(
                        unit).lower():
                    return self._error(
                        "INVALID_PLAN",
                        "Created sweep path and profile must use the same unit")
                profile_validation = dict(
                    profile_sketch_args.get("validation") or {})
                profile_validation.update({"require_closed": True,
                                           "closed_contours": 1})
                profile_sketch_args["validation"] = profile_validation
                operations.append({"op": "create_parametric_sketch",
                                   "args": profile_sketch_args})
                profile_created = True
                profile_ref = f"$steps.{len(operations) - 1}.data.sketch_name"
            if not profile_ref:
                return self._error(
                    "INVALID_PLAN",
                    "Elliptical/custom profile requires sketch_name, sketch, "
                    "or ellipse radii")
            if not profile_created:
                operations.append({"op": "validate_sweep_profile", "args": {
                    "profile_sketch": profile_ref, "unit": unit}})
            if profile_type in {"ellipse", "elliptical"}:
                profile_radius = max(float(profile.get("major_radius", 0.0)),
                                     float(profile.get("minor_radius", 0.0)))
            else:
                profile_radius = profile.get("max_radius")
                if min_bend_radius is None and profile_radius is None:
                    return self._error(
                        "INVALID_PLAN",
                        "Custom profile requires min_bend_radius or "
                        "profile.max_radius for curvature validation")
        if min_bend_radius is None and profile_radius is not None:
            factor = float(profile.get("bend_radius_factor", 1.05))
            min_bend_radius = float(profile_radius) * factor
        contact_preflight = None
        if path_created and profile_created:
            contact_preflight = self._declared_path_profile_contact(
                path_args, profile_sketch_args,
                tolerance=float(profile.get("contact_tolerance", 1e-4)))
            if (contact_preflight.get("checked") and
                    not contact_preflight.get("pass")):
                return self._error(
                    "INVALID_PLAN",
                    "Declared sweep path does not touch the profile plane",
                    details=contact_preflight)
        if path_created:
            logger.info("create_swept_member stage=declared_path_quality begin")
            declared_path_quality = self._path_quality_from_geometry(
                {"entities": list(path_args.get("entities") or []),
                 "contours": []},
                min_bend_radius, bool(allow_sharp_corners))
            if not declared_path_quality.get("pass"):
                code = ("CAPABILITY_UNAVAILABLE" if
                        declared_path_quality.get("reason") ==
                        "shapely_unavailable" else "FEATURE_DEAD")
                return self._error(
                    code, "Declared sweep path quality gate failed",
                    details={**declared_path_quality,
                             "validation_source": "declared_geometry"})
            logger.info(
                "create_swept_member stage=declared_path_quality complete pass=%s",
                bool(declared_path_quality.get("pass")))
        else:
            operations.append({"op": "validate_sweep_path", "args": {
                "path_sketch": path_ref,
                "min_bend_radius": min_bend_radius,
                "unit": unit,
                "allow_sharp_corners": bool(allow_sharp_corners)}})
        operations.append({"op": "create_sweep_feature", "args": {
            "path_sketch": path_ref,
            "profile_type": profile_type,
            "profile_sketch": profile_ref,
            "diameter": diameter,
            "body_name": body_name,
            "feature_name": feature_name or f"{body_name}_Sweep",
            "merge": bool(merge), "unit": unit,
            "auto_verify": bool(auto_verify),
            "advanced_smoothing": bool(profile.get(
                "advanced_smoothing", True))}})
        expected_count = len(before_names) if merge else len(before_names) + 1
        logger.info(
            "create_swept_member stage=transaction_dispatch operations=%s",
            [operation["op"] for operation in operations])
        result = self.run_transaction(
            name=idempotency_key or f"create_swept_member:{body_name}",
            operations=operations,
            checkpoint={"mode": "save_copy" if checkpoint else "none"},
            invariants={"solid_body_count": expected_count,
                        "required_bodies": [body_name],
                        "no_modal_dialog": True},
            idempotency_key=idempotency_key, budget=budget,
            save_path=save_path,
            allow_unsaved_document=allow_unsaved_document)
        if not result.get("success"):
            return result
        steps = result["data"].get("steps", [])
        path_quality = declared_path_quality or next((
            (step.get("data") or {}).get("path_quality")
            for step in steps
            if (step.get("data") or {}).get("operation") ==
            "validate_sweep_path"), None)
        feature_data = next(((step.get("data") or {}) for step in steps
                             if (step.get("data") or {}).get("operation") ==
                             "create_sweep_feature"), {})
        result["data"].update({
            "body_name": body_name,
            "feature_name": feature_data.get("feature_name"),
            "profile_type": profile_type,
            "path_quality": path_quality,
            "path_validation_source": (
                "declared_geometry" if path_created else "solidworks_readback"),
            "profile_validation_source": (
                "create_parametric_sketch" if profile_created else
                "solidworks_readback"),
            "path_profile_contact": contact_preflight,
            "body_verification": feature_data.get("body_verification"),
            "selection_contract": feature_data.get("selection_contract")})
        return result

    @staticmethod
    def _offset_profile_entities(entities, clearance):
        points = []
        previous_end = None
        tolerance = 1e-8
        for entity in entities:
            if entity.get("type", "line") != "line":
                raise ValueError(
                    "Automatic generic clearance offset supports line polygons; "
                    "provide pocket_entities for arc/spline profiles")
            start = list(entity["start"][:2])
            end = list(entity["end"][:2])
            if previous_end is not None and math.dist(
                    previous_end, start) > tolerance:
                raise ValueError(
                    "Insert profile line entities must form one contiguous loop")
            if not points:
                points.append(start)
            points.append(end)
            previous_end = end
        if len(points) < 4 or math.dist(points[0], points[-1]) > tolerance:
            raise ValueError("Insert profile must be a closed line polygon")

        # GEOS remains the robust choice for concave polygon offsets, but its
        # DLL can deadlock when first loaded after SOLIDWORKS COM. Run this
        # bounded pure-geometry step in an isolated process with no COM state.
        worker = r"""
import json
import sys
from shapely.geometry import Polygon

payload = json.loads(sys.stdin.read())
polygon = Polygon(payload["points"])
if not polygon.is_valid or polygon.area <= 0:
    raise ValueError("Insert profile is not a valid polygon")
offset = polygon.buffer(float(payload["clearance"]), join_style=2)
if offset.geom_type != "Polygon" or offset.is_empty:
    raise ValueError("Clearance offset produced multiple regions")
print(json.dumps({"coordinates": list(offset.exterior.coords),
                  "area": float(offset.area)}))
"""
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", worker],
                input=json.dumps({"points": points,
                                  "clearance": float(clearance)}),
                capture_output=True, text=True, timeout=20,
                check=False, creationflags=creation_flags)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                "Clearance offset worker exceeded 20 seconds") from exc
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout or
                     "unknown isolated geometry error").strip()
            raise ValueError(
                f"Clearance offset worker failed: {error[-500:]}")
        try:
            payload = json.loads(completed.stdout)
            coords = payload["coordinates"]
        except Exception as exc:
            raise ValueError(
                "Clearance offset worker returned invalid JSON") from exc
        if len(coords) < 4:
            raise ValueError("Clearance offset returned a degenerate polygon")
        return [{"id": f"pocket_{i + 1:03d}", "type": "line",
                 "start": list(coords[i]), "end": list(coords[i + 1])}
                for i in range(len(coords) - 1)]

    def create_multibody_insert(self, insert_sketch: Dict[str, Any],
                                insert_extrude: Dict[str, Any],
                                host_body: str, insert_body: str,
                                clearance: float,
                                pocket_sketch: Dict[str, Any] = None,
                                pocket_cut: Dict[str, Any] = None,
                                clearance_tolerance: float = None,
                                save_path: str = None,
                                allow_unsaved_document: bool = False,
                                idempotency_key: str = None,
                                checkpoint: bool = True,
                                budget: Dict[str, Any] = None,
                                ui_guard: Dict[str, Any] = None,
                                recovery: Dict[str, Any] = None) -> Dict:
        insert_sketch = dict(insert_sketch or {})
        pocket_sketch = dict(pocket_sketch or {})
        clearance = float(clearance)
        if not math.isfinite(clearance) or clearance <= 0.0:
            return self._error(
                "INVALID_PLAN", "clearance must be finite and positive")
        if clearance_tolerance is None:
            clearance_tolerance = max(1e-4, abs(clearance) * 1e-4)
        clearance_tolerance = float(clearance_tolerance)
        if (not math.isfinite(clearance_tolerance) or
                clearance_tolerance < 0.0 or
                clearance_tolerance >= clearance):
            return self._error(
                "INVALID_PLAN",
                "clearance_tolerance must be finite, non-negative and less "
                "than clearance")
        unit = insert_sketch.get("unit", "mm")
        insert_sketch.setdefault("unit", unit)
        insert_validation = dict(insert_sketch.get("validation") or {})
        insert_validation.update({"require_closed": True,
                                  "closed_contours": 1})
        insert_sketch["validation"] = insert_validation
        before = self.list_bodies(include_hidden=True,
                                  unit=insert_sketch.get("unit", "mm"))
        if not before.get("success"):
            return before
        before_names = [item["name"] for item in before["data"]["bodies"]]
        if host_body not in before_names:
            return self._error(
                "INVARIANT_FAILED", f"Host body '{host_body}' was not found",
                details={"existing_bodies": before_names})
        if insert_body in before_names:
            return self._error(
                "INVARIANT_FAILED",
                f"Insert body name '{insert_body}' already exists")
        if not pocket_sketch:
            try:
                pocket_entities = self._offset_profile_entities(
                    insert_sketch.get("entities", []), clearance)
            except Exception as exc:
                return self._error("SKETCH_OPEN_CONTOUR", str(exc))
            pocket_sketch = {
                "name": insert_sketch.get("name", "Insert") + "_Pocket",
                "plane": insert_sketch.get("plane", "Front"),
                "unit": insert_sketch.get("unit", "mm"),
                "entities": pocket_entities,
                "constraints": [], "dimensions": [], "equations": [],
                "validation": {"require_closed": True, "closed_contours": 1},
                "solve": {}, "transaction": {"rollback_on_failure": True}}
        else:
            pocket_sketch.setdefault("unit", unit)
            pocket_validation = dict(pocket_sketch.get("validation") or {})
            pocket_validation.update({"require_closed": True,
                                      "closed_contours": 1})
            pocket_sketch["validation"] = pocket_validation
        if str(pocket_sketch.get("unit", unit)).lower() != str(unit).lower():
            return self._error(
                "INVALID_PLAN",
                "Insert and pocket sketches must use the same unit")
        insert_extrude = dict(insert_extrude or {})
        insert_extrude.update({"sketch_name": "$steps.0.data.sketch_name",
                               "merge": False,
                               "auto_verify": True,
                               "feature_name": insert_extrude.get(
                                   "feature_name", insert_body + "_Boss")})
        insert_extrude.setdefault("unit", unit)
        if str(insert_extrude.get("unit")).lower() != str(unit).lower():
            return self._error(
                "INVALID_PLAN", "Insert sketch and extrusion must use the same unit")
        pocket_cut = dict(pocket_cut or {})
        pocket_cut.update({"sketch_name": "$steps.3.data.sketch_name",
                           "scope_bodies": [host_body],
                           "auto_verify": True,
                           "feature_name": pocket_cut.get(
                               "feature_name", insert_body + "_PocketCut")})
        pocket_cut.setdefault("unit", unit)
        if str(pocket_cut.get("unit")).lower() != str(unit).lower():
            return self._error(
                "INVALID_PLAN", "Pocket sketch and cut must use the same unit")
        pocket_cut.setdefault("end_condition", "through_all")
        pocket_cut.setdefault("auto_flags", True)
        operations = [
            {"op": "create_parametric_sketch", "args": insert_sketch},
            {"op": "advanced_extrude", "args": insert_extrude},
            {"op": "rename_body", "args": {
                "old_name": "$steps.1.data.new_bodies.0",
                "new_name": insert_body}},
            {"op": "create_parametric_sketch", "args": pocket_sketch},
            {"op": "advanced_cut", "args": pocket_cut},
            {"op": "verify_named_body", "args": {
                "body_name": host_body,
                "unit": insert_sketch.get("unit", "mm")}},
            {"op": "verify_named_body", "args": {
                "body_name": insert_body,
                "unit": insert_sketch.get("unit", "mm")}},
            {"op": "check_clearance", "args": {
                "body_a": host_body, "body_b": insert_body,
                "min_clearance": max(
                    0.0, clearance - clearance_tolerance),
                "unit": unit}},
        ]
        result = self.run_transaction(
            idempotency_key or f"multibody_insert:{insert_body}", operations,
            checkpoint={"mode": "save_copy" if checkpoint else "none"},
            invariants={"solid_body_count": len(before_names) + 1,
                        "required_bodies": [host_body, insert_body],
                        "no_modal_dialog": True},
            idempotency_key=idempotency_key,
            budget=budget,
            save_path=save_path,
            allow_unsaved_document=allow_unsaved_document)
        if result.get("success"):
            steps = result["data"].get("steps", [])
            host_verification = ((steps[5].get("data") or {})
                                 if len(steps) > 5 else None)
            insert_verification = ((steps[6].get("data") or {})
                                   if len(steps) > 6 else None)
            clearance_data = ((steps[7].get("data") or {})
                              if len(steps) > 7 else None)
            insert_sketch_data = ((steps[0].get("data") or {})
                                  if len(steps) > 0 else {})
            insert_feature_data = ((steps[1].get("data") or {})
                                   if len(steps) > 1 else {})
            pocket_sketch_data = ((steps[3].get("data") or {})
                                  if len(steps) > 3 else {})
            pocket_feature_data = ((steps[4].get("data") or {})
                                   if len(steps) > 4 else {})
            result["data"].update({
                "host_body": host_body, "insert_body": insert_body,
                "clearance": clearance,
                "clearance_tolerance": clearance_tolerance,
                "verified_min_clearance": max(
                    0.0, clearance - clearance_tolerance),
                "mating_sides": {
                    "host": {
                        "body_name": host_body,
                        "pocket_sketch": pocket_sketch_data.get(
                            "sketch_name", pocket_sketch.get("name")),
                        "cut_feature": pocket_feature_data.get(
                            "feature_name", pocket_cut.get("feature_name")),
                        "verification": host_verification},
                    "insert": {
                        "body_name": insert_body,
                        "profile_sketch": insert_sketch_data.get(
                            "sketch_name", insert_sketch.get("name")),
                        "boss_feature": insert_feature_data.get(
                            "feature_name", insert_extrude.get("feature_name")),
                        "verification": insert_verification}},
                "clearance_verification": clearance_data,
                "scope_verified": pocket_feature_data.get(
                    "scope_bodies", [host_body]),
                "checkpoint": bool(checkpoint)})
        return result

    def create_semantic_primitive(self, kind: str,
                                  parameters: Dict[str, Any]) -> Dict:
        parameters = dict(parameters or {})
        dispatch = {
            "revolved_shell": self.create_revolved_body,
            "tubular_member": self.create_swept_member,
            "clearance_insert": self.create_multibody_insert,
            "clearance_pocket": self.create_multibody_insert,
            "cap_and_opening": self.create_multibody_insert,
            "uniform_shell": self.shell,
            "symmetric_pair": self.mirror_feature,
            "linear_feature_array": self.linear_pattern,
            "circular_feature_array": self.circular_pattern,
        }
        if kind == "hole_or_rib_array":
            pattern = parameters.pop("pattern", "linear")
            operation = (self.circular_pattern if pattern == "circular"
                         else self.linear_pattern)
            return operation(**parameters)
        if kind == "printable_hinge":
            operations = parameters.pop("operations", None)
            if not operations:
                return self._error(
                    "INVALID_PLAN",
                    "printable_hinge requires an explicit named operations plan; "
                    "implicit project-specific hinge geometry is prohibited")
            return self.execute_cad_plan(
                parameters.pop("plan_id", "semantic:printable_hinge"),
                operations, transaction={"checkpoint_before": True,
                                         "rollback_on_failure": True},
                invariants=parameters.pop("invariants", None), **parameters)
        if kind == "capsule_profile":
            width = float(parameters.pop("width"))
            height = float(parameters.pop("height"))
            radius = width / 2.0
            if height < width:
                return self._error("SKETCH_OPEN_CONTOUR",
                                   "Capsule height must be >= width")
            half_straight = (height - width) / 2.0
            name = parameters.pop("name", "Capsule_Profile")
            entities = [
                {"id": "right", "type": "line",
                 "start": [radius, -half_straight],
                 "end": [radius, half_straight]},
                {"id": "top", "type": "arc", "center": [0, half_straight],
                 "start": [radius, half_straight],
                 "end": [-radius, half_straight], "radius": radius,
                 "direction": 1},
                {"id": "left", "type": "line",
                 "start": [-radius, half_straight],
                 "end": [-radius, -half_straight]},
                {"id": "bottom", "type": "arc", "center": [0, -half_straight],
                 "start": [-radius, -half_straight],
                 "end": [radius, -half_straight], "radius": radius,
                 "direction": 1},
            ]
            return self.create_parametric_sketch(
                name=name, plane=parameters.pop("plane", "Front"),
                unit=parameters.pop("unit", "mm"), entities=entities,
                constraints=[], dimensions=[], equations=[], solve={},
                validation={"require_closed": True, "closed_contours": 1},
                transaction={"rollback_on_failure": True})
        operation = dispatch.get(kind)
        if operation is None:
            return self._error(
                "CAPABILITY_UNAVAILABLE", f"Unknown semantic primitive '{kind}'",
                details={"supported": sorted(list(dispatch) + [
                    "capsule_profile", "hole_or_rib_array", "printable_hinge"])})
        return operation(**parameters)

    @staticmethod
    def _file_hash(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _inspect_stl(path):
        size = os.path.getsize(path)
        if size < 84:
            raise ValueError("STL is too small")
        mins = [float("inf")] * 3; maxs = [float("-inf")] * 3
        triangles = 0
        with open(path, "rb") as handle:
            header = handle.read(80)
            count_raw = handle.read(4)
            count = struct.unpack("<I", count_raw)[0]
            if 84 + count * 50 == size:
                triangles = count
                for _ in range(count):
                    record = handle.read(50)
                    values = struct.unpack("<12fH", record)
                    for vertex in (values[3:6], values[6:9], values[9:12]):
                        for axis in range(3):
                            mins[axis] = min(mins[axis], vertex[axis])
                            maxs[axis] = max(maxs[axis], vertex[axis])
            else:
                handle.seek(0)
                text = handle.read().decode("ascii", errors="ignore")
                for line in text.splitlines():
                    fields = line.strip().split()
                    if len(fields) == 4 and fields[0].lower() == "vertex":
                        vertex = [float(v) for v in fields[1:]]
                        triangles += 1 / 3
                        for axis in range(3):
                            mins[axis] = min(mins[axis], vertex[axis])
                            maxs[axis] = max(maxs[axis], vertex[axis])
                triangles = int(triangles)
        if triangles <= 0 or mins[0] == float("inf"):
            raise ValueError("STL contains no triangles")
        return {"triangles": triangles, "bbox": {"min": mins, "max": maxs}}

    @staticmethod
    def _inspect_sldprt(path, timeout_sec=2.0, poll_interval_sec=0.05):
        """Validate a native/compound part after its asynchronous disk flush."""
        compound_signature = bytes.fromhex("D0CF11E0A1B11AE1")
        native_version_marker = bytes.fromhex("00000004")
        # SW2026 changes bytes following the stable native record prefix even
        # between consecutive Save As operations.  The version word and the
        # seven-byte record prefix are stable across the observed variants;
        # requiring either alone would be too weak, so validate both.
        native_record_prefix = bytes.fromhex("140006000800DF")
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        size, signature, marker_offset = 0, b"", -1
        while True:
            try:
                size = os.path.getsize(path)
                if size >= 512:
                    with open(path, "rb") as handle:
                        header = handle.read(min(4096, size))
                    signature = header[:8]
                    marker_offset = header.find(native_record_prefix, 8)
                    if signature == compound_signature:
                        return {
                            "container": "CFBF",
                            "signature_hex": signature.hex().upper(),
                            "size_bytes": size,
                        }
                    if (signature[4:8] == native_version_marker and
                            marker_offset >= 0):
                        return {
                            "container": "SOLIDWORKS_NATIVE",
                            "signature_hex": signature.hex().upper(),
                            "native_record_offset": marker_offset,
                            "size_bytes": size,
                        }
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.001, float(poll_interval_sec)))
        if size < 512:
            raise ValueError("SLDPRT is too small")
        raise ValueError("SLDPRT has no recognized SolidWorks file signature")

    @staticmethod
    def _inspect_step(path):
        size = os.path.getsize(path)
        if size < 128:
            raise ValueError("STEP is too small")
        entity_records = 0
        solid_body_count = 0
        closed_shell_count = 0
        header_seen = False
        footer_seen = False
        with open(path, "rb") as handle:
            for raw_line in handle:
                line = raw_line.strip().upper()
                header_seen = header_seen or line.startswith(b"ISO-10303-21")
                footer_seen = footer_seen or line.startswith(b"END-ISO-10303-21")
                if line.startswith(b"#") and b"=" in line:
                    entity_records += 1
                    compact = line.replace(b" ", b"").replace(b"\t", b"")
                    if any(marker in compact for marker in (
                            b"=MANIFOLD_SOLID_BREP",
                            b"=BREP_WITH_VOIDS",
                            b"=FACETED_BREP")):
                        solid_body_count += 1
                    if b"=CLOSED_SHELL" in compact:
                        closed_shell_count += 1
        if not header_seen or not footer_seen or entity_records <= 0:
            raise ValueError("STEP Part 21 structure is incomplete")
        return {"format": "ISO-10303-21", "entity_records": entity_records,
                "solid_body_count": solid_body_count,
                "closed_shell_count": closed_shell_count,
                "size_bytes": size}

    @staticmethod
    def _safe_export_filename(value):
        filename = str(value or "").strip()
        if (not filename or filename in {".", ".."} or
                filename != os.path.basename(filename) or
                any(separator in filename for separator in ("/", "\\"))):
            raise ValueError("Export filenames must be non-empty leaf names")
        return filename

    def _commit_export_targets(self, statuses, overwrite):
        """Replace all verified targets with rollback of every prior replacement."""
        normalized = [os.path.normcase(os.path.abspath(item["target"]))
                      for item in statuses]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Export targets must be unique")
        backups, installed, rollback_errors = {}, [], []
        try:
            for item in statuses:
                staged, target = item["staged"], item["target"]
                if not os.path.isfile(staged):
                    raise FileNotFoundError(f"Verified staged file is missing: {staged}")
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                if os.path.exists(target):
                    if not overwrite:
                        raise FileExistsError(
                            f"Export target exists and overwrite=false: {target}")
                    backup = target + f".solidworks-mcp-backup-{uuid.uuid4().hex}"
                    os.replace(target, backup)
                    backups[target] = backup
                os.replace(staged, target)
                installed.append(target)
                if (os.path.getsize(target) != item["size_bytes"] or
                        self._file_hash(target) != item["sha256"]):
                    raise RuntimeError(f"Committed export hash mismatch: {target}")
                item["committed"] = True
            for backup in backups.values():
                try:
                    os.remove(backup)
                except OSError as exc:
                    logger.warning("Could not remove export backup %s: %s", backup, exc)
            return
        except Exception as exc:
            for target in reversed(installed):
                try:
                    if os.path.exists(target):
                        os.remove(target)
                except OSError as rollback_exc:
                    rollback_errors.append(f"remove {target}: {rollback_exc}")
            for target, backup in backups.items():
                try:
                    if os.path.exists(backup):
                        os.replace(backup, target)
                except OSError as rollback_exc:
                    rollback_errors.append(f"restore {target}: {rollback_exc}")
            for item in statuses:
                item["committed"] = False
            if rollback_errors:
                raise RuntimeError(
                    f"Export commit failed: {exc}; rollback errors: {rollback_errors}") from exc
            raise RuntimeError(
                f"Export commit failed and all prior targets were restored: {exc}") from exc

    def _resolve_stl_settings(self, options, unit):
        """Resolve deterministic body-mesh settings without global SW prefs."""
        options = dict(options or {})
        unit_key = str(unit or self._units.default_unit.value).lower()
        unit_aliases = {
            "mm": ("mm", 1000.0),
            "millimeter": ("mm", 1000.0),
            "millimeters": ("mm", 1000.0),
            "cm": ("cm", 100.0),
            "m": ("m", 1.0),
            "meter": ("m", 1.0),
            "meters": ("m", 1.0),
            "in": ("in", 1.0 / 0.0254),
            "inch": ("in", 1.0 / 0.0254),
            "inches": ("in", 1.0 / 0.0254),
            "ft": ("ft", 1.0 / 0.3048),
            "feet": ("ft", 1.0 / 0.3048),
        }
        if unit_key not in unit_aliases:
            raise ValueError(
                "STL unit must be mm, cm, m, in/inches, or ft/feet")
        normalized_unit, meters_to_unit = unit_aliases[unit_key]
        quality = str(options.get("quality", "fine")).lower()
        quality_defaults = {
            "coarse": (0.1, 20.0),
            "fine": (0.02, 10.0),
            "custom": (0.02, 10.0),
        }
        if quality not in quality_defaults:
            raise ValueError("stl.quality must be coarse, fine, or custom")
        custom_keys = {"deviation_mm", "angle_tolerance_deg"}
        if quality != "custom" and custom_keys.intersection(options):
            raise ValueError(
                "stl.deviation_mm and angle_tolerance_deg require quality=custom")
        default_deviation, default_angle = quality_defaults[quality]
        deviation_mm = float(options.get("deviation_mm", default_deviation))
        angle_deg = float(options.get("angle_tolerance_deg", default_angle))
        if not math.isfinite(deviation_mm) or deviation_mm <= 0.0:
            raise ValueError("stl.deviation_mm must be finite and positive")
        if not math.isfinite(angle_deg) or not 0.5 <= angle_deg <= 30.0:
            raise ValueError(
                "stl.angle_tolerance_deg must be between 0.5 and 30")
        max_triangles = int(options.get("max_triangles", 5_000_000))
        if max_triangles <= 0 or max_triangles > 20_000_000:
            raise ValueError(
                "stl.max_triangles must be between 1 and 20000000")
        return {
            "backend": "solidworks_itessellation",
            "unit": normalized_unit,
            "meters_to_unit": meters_to_unit,
            "quality": quality,
            "binary": bool(options.get("binary", True)),
            "preserve_origin": bool(options.get("preserve_origin", True)),
            "deviation_mm": deviation_mm,
            "angle_tolerance_deg": angle_deg,
            "max_triangles": max_triangles,
            "improved_quality": True,
            "preferences_mutated": False,
            "modal_preview_disabled": True,
        }

    @staticmethod
    def _ordered_facet_vertices(tessellation, fin_ids):
        edges = []
        for fin_id in list(fin_ids or []):
            vertices = list(com_get(
                tessellation, "GetFinVertices", int(fin_id), default=None) or [])
            if len(vertices) != 2:
                raise RuntimeError(
                    f"Tessellation fin {fin_id} does not have two vertices")
            edges.append((int(vertices[0]), int(vertices[1])))
        if len(edges) != 3:
            raise RuntimeError(
                f"Tessellation facet must contain three fins, got {len(edges)}")
        first, second = edges[0]
        third_candidates = sorted({value for edge in edges for value in edge}
                                  - {first, second})
        if len(third_candidates) != 1:
            raise RuntimeError("Tessellation facet is not a three-vertex loop")
        return [first, second, third_candidates[0]]

    def _tessellate_body(self, body, settings):
        """Return oriented triangles from the official IBody2 tessellator."""
        body_typed = typed(body, "IBody2")
        tessellation = body_typed.GetTessellation(pythoncom.Empty)
        if tessellation is None:
            raise RuntimeError("IBody2.GetTessellation returned no object")
        deviation_m = float(settings["deviation_mm"]) * 0.001
        angle_rad = math.radians(float(settings["angle_tolerance_deg"]))
        tessellation.ImprovedQuality = True
        tessellation.NeedVertexNormal = True
        tessellation.SurfacePlaneTolerance = deviation_m
        tessellation.SurfacePlaneAngleTolerance = angle_rad
        tessellation.CurveChordTolerance = deviation_m
        tessellation.CurveChordAngleTolerance = angle_rad
        if not bool(com_get(tessellation, "Tessellate", default=False)):
            raise RuntimeError("ITessellation.Tessellate returned false")
        facet_count = int(com_get(
            tessellation, "GetFacetCount", default=0) or 0)
        if facet_count <= 0:
            raise RuntimeError("SolidWorks tessellation contains no facets")
        if facet_count > int(settings["max_triangles"]):
            raise RuntimeError(
                f"Tessellation has {facet_count} facets, exceeding "
                f"max_triangles={settings['max_triangles']}")

        scale = float(settings["meters_to_unit"])
        triangles = []
        for facet_id in range(facet_count):
            fin_ids = com_get(
                tessellation, "GetFacetFins", facet_id, default=None)
            vertex_ids = self._ordered_facet_vertices(tessellation, fin_ids)
            points_m = [list(com_get(
                tessellation, "GetVertexPoint", vertex_id, default=None) or [])
                for vertex_id in vertex_ids]
            if any(len(point) != 3 for point in points_m):
                raise RuntimeError(
                    f"Facet {facet_id} has an invalid vertex coordinate")
            p0, p1, p2 = [tuple(float(value) * scale for value in point)
                          for point in points_m]
            edge_a = tuple(p1[axis] - p0[axis] for axis in range(3))
            edge_b = tuple(p2[axis] - p0[axis] for axis in range(3))
            cross = (
                edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
                edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
                edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
            )
            magnitude = math.sqrt(sum(value * value for value in cross))
            if not math.isfinite(magnitude) or magnitude <= 1e-15:
                raise RuntimeError(f"Facet {facet_id} is degenerate")
            normal = tuple(value / magnitude for value in cross)
            vertex_normals = []
            for vertex_id in vertex_ids:
                raw = list(com_get(
                    tessellation, "GetVertexNormal", vertex_id,
                    default=None) or [])
                if len(raw) == 3:
                    vertex_normals.append(tuple(float(value) for value in raw))
            if vertex_normals:
                average = tuple(sum(normal_value[axis]
                                    for normal_value in vertex_normals)
                                for axis in range(3))
                if sum(normal[axis] * average[axis]
                       for axis in range(3)) < 0.0:
                    p1, p2 = p2, p1
                    normal = tuple(-value for value in normal)
            triangles.append((normal, p0, p1, p2))

        translation = [0.0, 0.0, 0.0]
        if not settings["preserve_origin"]:
            mins = [min(vertex[axis]
                        for triangle in triangles
                        for vertex in triangle[1:])
                    for axis in range(3)]
            translation = [-value for value in mins]
            triangles = [
                (normal, *(tuple(vertex[axis] + translation[axis]
                                 for axis in range(3))
                           for vertex in (p0, p1, p2)))
                for normal, p0, p1, p2 in triangles]
        return triangles, {
            "facet_count": facet_count,
            "translation": translation,
            "deviation_m": deviation_m,
            "angle_tolerance_rad": angle_rad,
        }

    @staticmethod
    def _write_stl(path, triangles, binary, body_name):
        if binary:
            header_text = (
                f"solidworks-mcp ITessellation body={body_name}").encode(
                    "ascii", errors="replace")[:80]
            with open(path, "wb") as handle:
                handle.write(header_text.ljust(80, b"\0"))
                handle.write(struct.pack("<I", len(triangles)))
                for normal, p0, p1, p2 in triangles:
                    handle.write(struct.pack(
                        "<12fH", *normal, *p0, *p1, *p2, 0))
        else:
            safe_name = "".join(
                char if char.isalnum() or char in "_-" else "_"
                for char in str(body_name or "body"))
            with open(path, "w", encoding="ascii", newline="\n") as handle:
                handle.write(f"solid {safe_name}\n")
                for normal, p0, p1, p2 in triangles:
                    handle.write(
                        "  facet normal {:.9g} {:.9g} {:.9g}\n".format(*normal))
                    handle.write("    outer loop\n")
                    for vertex in (p0, p1, p2):
                        handle.write(
                            "      vertex {:.9g} {:.9g} {:.9g}\n".format(
                                *vertex))
                    handle.write("    endloop\n  endfacet\n")
                handle.write(f"endsolid {safe_name}\n")

    def _export_body_stl(self, doc, body, path, stl_settings=None):
        """Export exactly one body through ITessellation, without selection."""
        settings = dict(stl_settings or self._resolve_stl_settings({}, "mm"))
        triangles, tessellation = self._tessellate_body(body, settings)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        body_name = str(com_get(body, "Name", default="body"))
        self._write_stl(
            path, triangles, bool(settings["binary"]), body_name)
        if not os.path.isfile(path):
            raise RuntimeError("Native tessellation STL writer produced no file")
        return {
            "ok": True,
            "errors": 0,
            "warnings": 0,
            "backend": settings["backend"],
            "triangles": len(triangles),
            "tessellation": tessellation,
        }

    @contextmanager
    def _stl_export_preferences(self, options, unit):
        """Compatibility wrapper; native tessellation mutates no preferences."""
        yield self._resolve_stl_settings(options, unit)

    @staticmethod
    def _verify_stl_bbox(inspection, body_record, preserve_origin):
        stl_bbox = inspection["bbox"]
        cad_bbox = body_record["bbox"]
        stl_span = [stl_bbox["max"][axis] - stl_bbox["min"][axis]
                    for axis in range(3)]
        cad_span = [cad_bbox["max"][axis] - cad_bbox["min"][axis]
                    for axis in range(3)]
        extent_delta = [stl_span[axis] - cad_span[axis]
                        for axis in range(3)]
        tolerance = max(0.001, max(cad_span) * 0.0025)
        if any(abs(value) > tolerance for value in extent_delta):
            raise ValueError(
                f"STL bbox extents differ from CAD body by {extent_delta}; "
                f"tolerance={tolerance}")
        translation = [
            ((stl_bbox["min"][axis] - cad_bbox["min"][axis]) +
             (stl_bbox["max"][axis] - cad_bbox["max"][axis])) / 2.0
            for axis in range(3)]
        if preserve_origin and any(abs(value) > tolerance
                                   for value in translation):
            raise ValueError(
                f"STL did not preserve the CAD origin; translation={translation}")
        inspection["bbox_verification"] = {
            "cad_bbox": cad_bbox, "extent_delta": extent_delta,
            "coordinate_translation": translation,
            "tolerance": tolerance, "preserve_origin": bool(preserve_origin),
            "passed": True,
        }
        return inspection

    def export_bundle(self, sldprt_path: str, step_path: str,
                      stl_directory: str, bodies: List[str] = None,
                      naming: Dict[str, Any] = None,
                      stl: Dict[str, Any] = None,
                      report: bool = True, overwrite: bool = False,
                      unit: str = None) -> Dict:
        doc, err = self.get_active_doc()
        if err:
            return err
        source_path = self._get_doc_path(doc)
        if not source_path:
            return self._error("DOCUMENT_UNSAVED",
                               "export_bundle requires a saved active document")
        targets = [os.path.abspath(sldprt_path), os.path.abspath(step_path)]
        stl_directory = os.path.abspath(stl_directory)
        naming = naming or {}
        stl = stl or {}
        requested = [str(name) for name in (bodies or [])]
        try:
            for body_name in requested:
                filename = self._safe_export_filename(
                    naming.get(body_name, body_name))
                if not filename.lower().endswith(".stl"):
                    filename += ".stl"
                targets.append(os.path.join(stl_directory, filename))
        except ValueError as exc:
            return self._error("INVALID_PLAN", str(exc))
        if len({os.path.normcase(path) for path in targets}) != len(targets):
            return self._error("INVALID_PLAN", "Export targets must be unique")
        existing = [path for path in targets if os.path.exists(path)]
        if existing and not overwrite:
            return self._error(
                "INVALID_PLAN", "Export targets already exist; set overwrite=true",
                details={"existing": existing})
        staging_by_parent = {}

        def stage_path(target, filename):
            parent = os.path.dirname(target)
            Path(parent).mkdir(parents=True, exist_ok=True)
            key = os.path.normcase(os.path.abspath(parent))
            if key not in staging_by_parent:
                staging_by_parent[key] = tempfile.mkdtemp(
                    prefix=".solidworks-mcp-export-", dir=parent)
            return os.path.join(staging_by_parent[key], filename)

        statuses = []
        planned_files = []
        active_export = None
        try:
            rebuild_started = time.perf_counter()
            rebuild_ok = bool(com_get(doc, "EditRebuild3", default=False))
            self.record_rebuild(time.perf_counter() - rebuild_started)
            if not rebuild_ok:
                raise RuntimeError("SolidWorks rebuild failed before export")
            body_objects = {str(com_get(body, "Name", default="?")): body
                            for body in doc.GetBodies2(0, False) or []}
            if not requested:
                requested = sorted(body_objects)
                for body_name in requested:
                    filename = self._safe_export_filename(
                        naming.get(body_name, body_name))
                    if not filename.lower().endswith(".stl"):
                        filename += ".stl"
                    targets.append(os.path.join(stl_directory, filename))
            missing = [name for name in requested if name not in body_objects]
            if missing:
                raise RuntimeError(f"Bodies not found after rebuild: {missing}")
            if not body_objects:
                raise RuntimeError("Document has no solid bodies")
            if len({os.path.normcase(path) for path in targets}) != len(targets):
                raise ValueError("Export targets must be unique")
            existing = [path for path in targets if os.path.exists(path)]
            if existing and not overwrite:
                raise ValueError(
                    f"Export targets already exist; set overwrite=true: {existing}")
            planned_files = [
                {"kind": "sldprt", "target": targets[0]},
                {"kind": "step", "target": targets[1]},
            ] + [
                {"kind": "stl", "body": body_name,
                 "target": targets[index + 2]}
                for index, body_name in enumerate(requested)]
            body_listing = self.list_bodies(include_hidden=True, unit=unit)
            listed = {item["name"]: item for item in
                      ((body_listing.get("data") or {}).get("bodies") or [])}
            invalid_bodies = [name for name in requested if
                              not listed.get(name, {}).get("bbox") or
                              int(listed.get(name, {}).get("face_count") or 0) <= 0]
            if invalid_bodies:
                raise RuntimeError(
                    f"Bodies have no verified faces/bbox: {invalid_bodies}")
            active_export = planned_files[0]
            staged_part = stage_path(targets[0], "model.sldprt")
            part_saved = self._save_copy(doc, staged_part)
            if not part_saved.get("success"):
                raise RuntimeError("SLDPRT save-copy failed")
            part_inspection = self._inspect_sldprt(staged_part)
            statuses.append({"kind": "sldprt", "staged": staged_part,
                             "target": targets[0], "verified": True,
                             "inspection": part_inspection})
            active_export = planned_files[1]
            staged_step = stage_path(targets[1], "model.step")
            exported = self.export_file(staged_step)
            if not exported.get("success") or not os.path.isfile(staged_step):
                raise RuntimeError("STEP verification failed")
            step_inspection = self._inspect_step(staged_step)
            expected_step_bodies = len(body_objects)
            if step_inspection["solid_body_count"] != expected_step_bodies:
                raise RuntimeError(
                    "STEP solid-body count mismatch: "
                    f"expected {expected_step_bodies}, got "
                    f"{step_inspection['solid_body_count']}")
            step_inspection["expected_solid_body_count"] = expected_step_bodies
            statuses.append({"kind": "step", "staged": staged_step,
                             "target": targets[1], "verified": True,
                             "inspection": step_inspection})
            active_export = {"kind": "stl_settings", "target": None}
            stl_settings = self._resolve_stl_settings(stl, unit)
            for index, body_name in enumerate(requested):
                active_export = planned_files[index + 2]
                staged_stl = stage_path(
                    targets[index + 2], f"body-{index:03d}.stl")
                export_result = self._export_body_stl(
                    doc, body_objects[body_name], staged_stl, stl_settings)
                inspection = self._inspect_stl(staged_stl)
                inspection["native_export"] = export_result
                self._verify_stl_bbox(
                    inspection, listed[body_name],
                    stl_settings["preserve_origin"])
                statuses.append({"kind": "stl", "body": body_name,
                                 "staged": staged_stl,
                                 "target": targets[index + 2],
                                 "verified": True,
                                 "inspection": inspection})
            active_export = None
            for item in statuses:
                item["size_bytes"] = os.path.getsize(item["staged"])
                item["sha256"] = self._file_hash(item["staged"])
            public_statuses = [{key: value for key, value in item.items()
                                if key != "staged"} for item in statuses]
            manifest = {
                "schema": "solidworks-mcp/export-bundle/v1",
                "source_document": source_path,
                "solidworks_version": str(com_get(
                    self._sw_app, "RevisionNumber", default="unknown")),
                "mcp_version": "6.5.31", "unit": unit or self._units.default_unit.value,
                "created_at": time.time(), "files": public_statuses,
                "bodies": [listed[name] for name in requested],
                "stl_settings": stl_settings,
                "rebuild_verified": True, "committed": True}
            manifest_path = os.path.join(
                os.path.dirname(targets[0]), "manifest.json")
            if os.path.exists(manifest_path) and not overwrite:
                manifest_path = os.path.join(
                    os.path.dirname(targets[0]),
                    f"manifest-{int(time.time())}.json")
            if report:
                manifest_plan = {"kind": "manifest", "target": manifest_path}
                planned_files.append(manifest_plan)
                active_export = manifest_plan
                staged_manifest = stage_path(manifest_path, "manifest.json")
                atomic_json_write(staged_manifest, manifest)
                statuses.append({
                    "kind": "manifest", "staged": staged_manifest,
                    "target": manifest_path, "verified": True,
                    "size_bytes": os.path.getsize(staged_manifest),
                    "sha256": self._file_hash(staged_manifest),
                })
                manifest["manifest"] = manifest_path
            active_export = {"kind": "atomic_commit", "target": None}
            self._commit_export_targets(statuses, overwrite=overwrite)
            active_export = None
            self._runtime.increment("cad_result_files", len(statuses))
            self._runtime.increment("files_saved", len(statuses))
            self._runtime.last_saved_body_at = time.time()
            return self._result(True, "Export bundle committed",
                                SwErrors.swSuccess, manifest)
        except Exception as exc:
            public_statuses = [{key: value for key, value in item.items()
                                if key != "staged"} for item in statuses]
            verified_targets = {
                os.path.normcase(item["target"]) for item in statuses}
            commit_failed = bool(
                active_export and active_export.get("kind") == "atomic_commit")
            active_target = (os.path.normcase(active_export["target"])
                             if active_export and active_export.get("target")
                             else None)
            file_outcomes = []
            for item in planned_files:
                target_key = os.path.normcase(item["target"])
                if commit_failed and target_key in verified_targets:
                    outcome = "verified_then_commit_rolled_back"
                elif target_key in verified_targets:
                    outcome = "verified_not_committed"
                elif target_key == active_target:
                    outcome = "failed"
                else:
                    outcome = "not_attempted"
                file_outcomes.append({**item, "outcome": outcome})
            return self._error(
                "INVARIANT_FAILED", f"Export bundle failed: {exc}",
                document_restored=True,
                details={
                    "committed": False, "files": public_statuses,
                    "file_outcomes": file_outcomes,
                    "failed_operation": active_export,
                    "failure": str(exc),
                    "staging": list(staging_by_parent.values()),
                    "staging_cleaned_in_finally": True,
                })
        finally:
            for staging in staging_by_parent.values():
                if os.path.isdir(staging):
                    shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _mask_deviation_zones(reference, candidate, scale_mm_per_px,
                              max_zones=8):
        import cv2
        import numpy as np
        from scipy.ndimage import distance_transform_edt

        kernel = np.ones((3, 3), np.uint8)
        ref_edge = cv2.morphologyEx(
            (reference > 0).astype(np.uint8), cv2.MORPH_GRADIENT, kernel) > 0
        cand_edge = cv2.morphologyEx(
            (candidate > 0).astype(np.uint8), cv2.MORPH_GRADIENT, kernel) > 0
        ref_field = distance_transform_edt(~ref_edge)
        cand_field = distance_transform_edt(~cand_edge)
        candidates = []
        for direction, edge, field in (
                ("candidate_to_reference", cand_edge, ref_field),
                ("reference_to_candidate", ref_edge, cand_field)):
            ys, xs = np.nonzero(edge)
            for y, x in zip(ys, xs):
                candidates.append((float(field[y, x]) * scale_mm_per_px,
                                   float(x), float(y), direction))
        zones = []
        suppression_px = max(4.0, 2.0 / max(scale_mm_per_px, 1e-9))
        for deviation, x, y, direction in sorted(candidates, reverse=True):
            if deviation <= 0.0:
                break
            if any(math.hypot(x - zone["pixel"][0], y - zone["pixel"][1]) <
                   suppression_px for zone in zones):
                continue
            zones.append({"pixel": [round(x, 3), round(y, 3)],
                          "deviation_mm": round(deviation, 6),
                          "direction": direction})
            if len(zones) >= max(1, int(max_zones)):
                break
        return zones

    @staticmethod
    def _load_stl_triangles(path, max_triangles=5_000_000):
        """Load binary or ASCII STL triangles in the file's configured units."""
        import numpy as np

        size = os.path.getsize(path)
        if size < 84:
            raise ValueError(f"STL is too small: {path}")
        triangles = []
        with open(path, "rb") as handle:
            handle.read(80)
            count_raw = handle.read(4)
            count = struct.unpack("<I", count_raw)[0]
            if 84 + count * 50 == size:
                if count > int(max_triangles):
                    raise ValueError(
                        f"STL triangle count {count} exceeds {max_triangles}")
                for _ in range(count):
                    values = struct.unpack("<12fH", handle.read(50))
                    triangles.append((values[3:6], values[6:9], values[9:12]))
            else:
                handle.seek(0)
                vertices = []
                for raw_line in handle:
                    fields = raw_line.decode("ascii", errors="ignore").strip().split()
                    if len(fields) == 4 and fields[0].lower() == "vertex":
                        vertices.append(tuple(float(value) for value in fields[1:]))
                        if len(vertices) == 3:
                            triangles.append(tuple(vertices))
                            vertices = []
                            if len(triangles) > int(max_triangles):
                                raise ValueError(
                                    "ASCII STL triangle count exceeds "
                                    f"{max_triangles}")
                if vertices:
                    raise ValueError(f"ASCII STL has an incomplete triangle: {path}")
        if not triangles:
            raise ValueError(f"STL contains no triangles: {path}")
        result = np.asarray(triangles, dtype=np.float64)
        if result.ndim != 3 or result.shape[1:] != (3, 3) or not np.isfinite(
                result).all():
            raise ValueError(f"STL contains invalid triangle coordinates: {path}")
        return result

    @staticmethod
    def _silhouette_projection(orientation):
        """Return a stable model-mm to orthographic plane convention."""
        import numpy as np

        name = str(orientation or "front").lower()
        projections = {
            "front": ([[1, 0, 0], [0, 1, 0]], ["+x", "+y"]),
            "back": ([[-1, 0, 0], [0, 1, 0]], ["-x", "+y"]),
            "right": ([[0, 0, -1], [0, 1, 0]], ["-z", "+y"]),
            "left": ([[0, 0, 1], [0, 1, 0]], ["+z", "+y"]),
            "top": ([[1, 0, 0], [0, 0, -1]], ["+x", "-z"]),
            "bottom": ([[1, 0, 0], [0, 0, 1]], ["+x", "+z"]),
        }
        if name not in projections:
            raise ValueError(
                "orientation must be front, back, right, left, top, or bottom")
        basis, axes = projections[name]
        return name, np.asarray(basis, dtype=np.float64), axes

    def _load_projected_mesh(self, mesh_paths, orientation,
                             max_triangles=5_000_000):
        """Load selected-body STL meshes and project every native triangle."""
        import numpy as np

        if not mesh_paths:
            raise ValueError("native_mesh candidate requires exported mesh_paths")
        orientation, basis, axes = self._silhouette_projection(orientation)
        projected_batches, files = [], []
        total = 0
        for path in mesh_paths:
            absolute = os.path.abspath(str(path))
            if not os.path.isfile(absolute):
                raise ValueError(f"Exported body mesh not found: {absolute}")
            remaining = int(max_triangles) - total
            if remaining <= 0:
                raise ValueError(
                    f"Combined STL triangle count exceeds {max_triangles}")
            triangles = self._load_stl_triangles(absolute, remaining)
            projected = triangles @ basis.T
            projected_batches.append(projected)
            count = int(projected.shape[0])
            total += count
            files.append({
                "temporary_name": os.path.basename(absolute),
                "size_bytes": os.path.getsize(absolute),
                "sha256": self._file_hash(absolute),
                "triangles": count,
            })
        projected = np.concatenate(projected_batches, axis=0)
        flat = projected.reshape(-1, 2)
        return projected, {
            "source": "native_stl_triangle_union",
            "orientation": orientation,
            "model_plane_axes": axes,
            "triangle_count": total,
            "projected_bbox_model_mm": {
                "min": [float(value) for value in flat.min(axis=0)],
                "max": [float(value) for value in flat.max(axis=0)],
            },
            "meshes": files,
        }

    @staticmethod
    def _rasterize_projected_mesh(projected, matrix, shape):
        """Rasterize the union of projected triangles in reference pixels."""
        import cv2
        import numpy as np

        height, width = int(shape[0]), int(shape[1])
        flat = projected.reshape(-1, 2)
        homogeneous = np.column_stack(
            [flat, np.ones((flat.shape[0],), dtype=np.float64)])
        mapped = homogeneous @ matrix.T
        denominator = mapped[:, 2]
        if np.any(np.abs(denominator) <= 1e-12):
            raise ValueError("candidate_to_reference maps mesh through infinity")
        pixels = mapped[:, :2] / denominator[:, None]
        if not np.isfinite(pixels).all():
            raise ValueError("candidate_to_reference produced invalid pixels")
        pixels = np.clip(np.rint(pixels), -(2 ** 20), 2 ** 20).astype(np.int32)
        polygons = pixels.reshape(-1, 3, 2)
        mask = np.zeros((height, width), dtype=np.uint8)
        # fillPoly applies an even-odd rule across a contour collection.  A
        # closed STL normally projects coincident front/back faces, so passing
        # a whole batch can cancel valid interiors.  Incremental convex fills
        # are idempotent writes and therefore implement a true pixel union.
        for polygon in polygons:
            cv2.fillConvexPoly(mask, polygon, 255)
        ys, xs = np.nonzero(mask)
        if not len(xs):
            raise ValueError(
                "Projected native mesh is empty inside the reference frame")
        return mask, {
            "rasterizer": "opencv_incremental_triangle_union",
            "foreground_pixels": int(np.count_nonzero(mask)),
            "projected_bbox_reference_px": {
                "min": [int(xs.min()), int(ys.min())],
                "max": [int(xs.max()), int(ys.max())],
            },
        }

    def compare_body_silhouette_to_image(self, reference_image: str,
                                         screenshot_path: str,
                                         orientation: str = "front",
                                         bodies: List[str] = None,
                                         transform: Dict[str, Any] = None,
                                         tolerance: Dict[str, Any] = None,
                                         outputs: Dict[str, Any] = None,
                                         candidate_source: str = None,
                                         mesh_paths: List[str] = None,
                                         mesh_settings: Dict[str, Any] = None,
                                         reference_mode: str = "filled_silhouette",
                                         contour_selection: Dict[str, Any] = None,
                                         capture_screenshot: bool = True,
                                         screenshot_data: Dict[str, Any] = None) -> Dict:
        import cv2
        import numpy as np
        transform, tolerance, outputs = transform or {}, tolerance or {}, outputs or {}
        contour_selection = contour_selection or {}
        mesh_settings = mesh_settings or {}
        candidate_source = str(candidate_source or (
            "native_mesh" if mesh_paths else "screenshot_segmentation")).lower()
        if candidate_source not in {"native_mesh", "screenshot_segmentation"}:
            return self._error(
                "INVALID_PLAN",
                "candidate_source must be native_mesh or screenshot_segmentation")
        if capture_screenshot:
            shot = self.take_screenshot(
                screenshot_path, orientation=orientation,
                zoom_to_bodies=bodies, zoom_to_fit=not bool(bodies), compress=False)
        else:
            if not os.path.isfile(screenshot_path):
                return self._error(
                    "IMAGE_LOW_CONFIDENCE",
                    f"Pre-captured body screenshot not found: {screenshot_path}")
            shot = self._result(
                True, "Using pre-captured body screenshot", SwErrors.swSuccess,
                dict(screenshot_data or {"path": screenshot_path}))
        if not shot.get("success") or (shot.get("data") or {}).get(
                "frame_unreadable"):
            return self._error("IMAGE_LOW_CONFIDENCE",
                               "Body silhouette screenshot is unreadable",
                               details={"screenshot": shot})
        ref_rgb, ref_mask, ref_confidence, _ = self._reference_mask(
            reference_image, contour_selection, reference_mode)
        matrix = transform.get("candidate_to_reference")
        projected = None
        mesh_provenance = None
        if candidate_source == "native_mesh":
            try:
                projected, mesh_provenance = self._load_projected_mesh(
                    mesh_paths, orientation,
                    max_triangles=int(mesh_settings.get(
                        "max_triangles", 5_000_000)))
            except (OSError, ValueError, struct.error) as exc:
                return self._error(
                    "INVARIANT_FAILED",
                    f"Native body mesh is invalid: {exc}",
                    details={"mesh_paths": mesh_paths or []})
        if matrix is None:
            if not transform.get("allow_bbox_fit", False):
                return self._error(
                    "IMAGE_LOW_CONFIDENCE",
                    "Explicit candidate_to_reference transform is required; "
                    "bbox best-fit is disabled because it can hide scale errors")
            ref_contour = self._largest_contour(ref_mask)
            rx, ry, rw, rh = cv2.boundingRect(ref_contour)
            if candidate_source == "native_mesh":
                bounds = mesh_provenance["projected_bbox_model_mm"]
                cx, cy = bounds["min"]
                cw = bounds["max"][0] - cx
                ch = bounds["max"][1] - cy
            else:
                _, candidate_for_fit, _, _ = self._reference_mask(
                    screenshot_path, {}, "filled_silhouette")
                cand_contour = self._largest_contour(candidate_for_fit)
                cx, cy, cw, ch = cv2.boundingRect(cand_contour)
            scale = min(rw / max(cw, 1e-12), rh / max(ch, 1e-12))
            matrix = [[scale, 0, rx - cx * scale],
                      [0, scale, ry - cy * scale], [0, 0, 1]]
        matrix = np.asarray(matrix, dtype=float)
        if (matrix.shape != (3, 3) or not np.isfinite(matrix).all() or
                abs(float(np.linalg.det(matrix))) <= 1e-12):
            return self._error("IMAGE_LOW_CONFIDENCE",
                               "candidate_to_reference must be an invertible 3x3 matrix")
        if "mm_per_pixel" not in transform:
            return self._error(
                "IMAGE_LOW_CONFIDENCE",
                "transform.mm_per_pixel is required for metric silhouette tolerances")
        mm_per_px = float(transform["mm_per_pixel"])
        if not math.isfinite(mm_per_px) or mm_per_px <= 0.0:
            return self._error("IMAGE_LOW_CONFIDENCE",
                               "transform.mm_per_pixel must be finite and positive")
        if candidate_source == "native_mesh":
            if not np.allclose(matrix[2], [0.0, 0.0, 1.0], atol=1e-12):
                return self._error(
                    "IMAGE_LOW_CONFIDENCE",
                    "native_mesh candidate_to_reference must be affine")
            calibrated_mm_per_px = 1.0 / math.sqrt(
                abs(float(np.linalg.det(matrix[:2, :2]))))
            scale_relative_error = abs(
                calibrated_mm_per_px - mm_per_px) / mm_per_px
            allowed_scale_error = float(transform.get(
                "max_scale_relative_error", 0.02))
            if (not transform.get("allow_bbox_fit", False) and
                    scale_relative_error > allowed_scale_error):
                return self._error(
                    "IMAGE_LOW_CONFIDENCE",
                    "candidate_to_reference scale conflicts with mm_per_pixel",
                    details={
                        "matrix_mm_per_pixel": calibrated_mm_per_px,
                        "declared_mm_per_pixel": mm_per_px,
                        "relative_error": scale_relative_error,
                        "allowed_relative_error": allowed_scale_error,
                    })
            try:
                aligned, mesh_rasterization = self._rasterize_projected_mesh(
                    projected, matrix, ref_mask.shape)
            except ValueError as exc:
                return self._error(
                    "INVARIANT_FAILED",
                    f"Native mesh projection failed: {exc}")
            cand_confidence = 1.0
            candidate_details = {
                **mesh_provenance,
                **mesh_rasterization,
                "mesh_settings": mesh_settings,
                "bodies": list(mesh_settings.get("body_names") or bodies or []),
                "coordinate_space": "projected_model_mm",
                "matrix_mm_per_pixel": calibrated_mm_per_px,
                "scale_relative_error": scale_relative_error,
            }
        else:
            _, cand_mask, cand_confidence, _ = self._reference_mask(
                screenshot_path, {}, "filled_silhouette")
            aligned = cv2.warpPerspective(
                cand_mask, matrix, (ref_mask.shape[1], ref_mask.shape[0]))
            candidate_details = {
                "source": "screenshot_segmentation",
                "coordinate_space": "candidate_screenshot_pixels",
            }
        metrics = self._mask_metrics(ref_mask, aligned, mm_per_px)
        profile_name = str(tolerance.get("profile", "balanced")).lower()
        profiles = {
            "draft": {"min_iou": 0.95, "max_hausdorff_mm": 1.0},
            "balanced": {"min_iou": 0.985, "max_hausdorff_mm": 0.3},
            "strict": {"min_iou": 0.995, "max_hausdorff_mm": 0.15},
        }
        if profile_name not in profiles:
            return self._error("INVALID_PLAN",
                               "tolerance.profile must be draft, balanced, or strict")
        thresholds = {
            "min_iou": float(tolerance.get(
                "min_iou", profiles[profile_name]["min_iou"])),
            "max_hausdorff_mm": float(tolerance.get(
                "max_hausdorff_mm", max(
                    profiles[profile_name]["max_hausdorff_mm"],
                    2.0 * mm_per_px))),
            "min_segmentation_confidence": float(tolerance.get(
                "min_segmentation_confidence", 0.75)),
        }
        passed = bool(
            ref_confidence >= thresholds["min_segmentation_confidence"] and
            cand_confidence >= thresholds["min_segmentation_confidence"] and
            metrics["iou"] >= thresholds["min_iou"] and
            metrics["hausdorff_mm"] <= thresholds["max_hausdorff_mm"])
        zones = self._mask_deviation_zones(
            ref_mask, aligned, mm_per_px,
            max_zones=int(tolerance.get("max_deviation_zones", 8)))
        overlay = outputs.get("overlay")
        if overlay:
            self._save_overlay(ref_rgb, ref_mask, aligned, overlay)
            self._runtime.increment("verification_artifacts")
        data = {"pass": passed, "metrics": metrics, "overlay": overlay,
                "screenshot": screenshot_path,
                "candidate_source": candidate_source,
                "candidate_geometry": candidate_details,
                "candidate_to_reference": matrix.tolist(),
                "mm_per_pixel": mm_per_px,
                "quality_profile": profile_name, "thresholds": thresholds,
                "reference_mode": reference_mode,
                "reference_segmentation_confidence": ref_confidence,
                "candidate_segmentation_confidence": cand_confidence,
                "maximum_deviation_zones": zones,
                "framing": (shot.get("data") or {})}
        if outputs.get("report"):
            atomic_json_write(outputs["report"], data)
            data["report"] = outputs["report"]
        result = self._result(
            passed, f"Body silhouette comparison {'PASS' if passed else 'FAIL'}",
            SwErrors.swSuccess if passed else SwErrors.swFeatureError, data)
        if not passed:
            result["data"]["error"] = structured_error(
                "REFERENCE_MISMATCH",
                "Body silhouette does not satisfy the reference tolerances",
                recommended_actions=[
                    "Inspect maximum_deviation_zones and the overlay",
                    "Correct the body or explicitly relax calibrated tolerances",
                ],
                debug_artifacts=[path for path in (
                    overlay, outputs.get("report"), screenshot_path) if path],
                details={"metrics": metrics, "thresholds": thresholds})
        return result
