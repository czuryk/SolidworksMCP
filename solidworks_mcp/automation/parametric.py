"""Atomic parametric sketch backend and vector-native inspection tools."""

from __future__ import annotations

import base64
import bisect
import json
import math
import os
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pythoncom
import win32com.client

from ..constants import SwErrors, SwPlanes
from .com_utils import (ComAdapter, array_r8, com_get, create_select_data,
                        resolve_solidworks_constant, select_by_id2,
                        transform_point, typed)
from .runtime import atomic_json_write, structured_error


RELATION_CODES = {
    "coincident": "sgCOINCIDENT",
    "horizontal": "sgHORIZONTAL2D",
    "vertical": "sgVERTICAL2D",
    "tangent": "sgTANGENT",
    "concentric": "sgCONCENTRIC",
    "equal": "sgSAMELENGTH",
    "midpoint": "sgATMIDDLE",
    "symmetric": "sgSYMMETRIC",
    "parallel": "sgPARALLEL",
    "perpendicular": "sgPERPENDICULAR",
    "collinear": "sgCOLINEAR",
    "fixed": "sgFIXED",
}

CONSTRAINED_STATUS = {
    1: "unknown", 2: "under_defined", 3: "fully_defined",
    4: "over_defined", 5: "no_solution", 6: "invalid_solution",
    7: "autosolve_off",
}

SEGMENT_TYPE = {0: "line", 1: "arc", 2: "ellipse", 3: "spline",
                4: "text", 5: "parabola"}


class ParametricSketchOperations:
    """Mixin implementing one-call sketch construction and diagnostics."""

    def _wait_for_active_sketch(self, doc, timeout_sec=2.0):
        """Wait for SW to publish ActiveSketch after InsertSketch2.

        SolidWorks 2026 can create the ProfileFeature synchronously while
        exposing SketchManager.ActiveSketch a few UI ticks later.  Reacquiring
        the active document avoids retaining a stale generated COM wrapper.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        current_doc = doc
        while True:
            manager = com_get(current_doc, "SketchManager", default=None)
            sketch = com_get(manager, "ActiveSketch", default=None)
            if sketch is not None:
                return current_doc, sketch
            if time.monotonic() >= deadline:
                return current_doc, None
            time.sleep(0.05)
            refreshed, err = self.get_active_doc()
            if err is None and refreshed is not None:
                current_doc = refreshed

    def _snapshot_feature_names_parametric(self, doc):
        names = set()
        feature = com_get(doc, "FirstFeature", default=None)
        guard = 0
        while feature is not None and guard < 10000:
            guard += 1
            names.add(str(com_get(feature, "Name", default="")))
            feature = com_get(feature, "GetNextFeature", default=None)
        return names

    def _find_new_sketch_feature(self, doc, previous_names):
        """Return the newest ProfileFeature created after a snapshot."""
        candidate = None
        feature = com_get(doc, "FirstFeature", default=None)
        guard = 0
        while feature is not None and guard < 10000:
            guard += 1
            name = str(com_get(feature, "Name", default=""))
            feature_type = str(com_get(
                feature, "GetTypeName2", default="")).lower()
            if name not in previous_names and feature_type in {
                    "profilefeature", "3dprofilefeature"}:
                candidate = feature
            feature = com_get(feature, "GetNextFeature", default=None)
        return candidate

    def _rollback_created_sketch(self, created_feature, requested_name):
        if created_feature is None:
            return False
        actual_name = str(com_get(
            created_feature, "Name", default=requested_name) or requested_name)
        doc, err = self.get_active_doc()
        if err is not None:
            return False
        # A failed relation can leave SolidWorks inside the invalid sketch for
        # a few UI ticks. The active sketch feature is not always published
        # through GetFeature after that failure, so close any active sketch
        # owned by this atomic operation before attempting deletion.
        for _ in range(3):
            manager = com_get(doc, "SketchManager", default=None)
            active = com_get(manager, "ActiveSketch", default=None)
            if active is None:
                break
            try:
                doc.ClearSelection2(True)
            except Exception:
                pass
            try:
                manager.InsertSketch(True)
            except Exception:
                try:
                    doc.InsertSketch2(True)
                except Exception:
                    pass
            time.sleep(0.1)
            doc, err = self.get_active_doc()
            if err is not None:
                return False
        target_name = actual_name
        if self._find_sketch_feature(doc, target_name) is None:
            if (requested_name != target_name and
                    self._find_sketch_feature(doc, requested_name) is not None):
                target_name = requested_name
            else:
                self._runtime.increment("rollbacks")
                return True
        if com_get(com_get(doc, "SketchManager", default=None),
                   "ActiveSketch", default=None) is not None:
            return False
        if self._find_sketch_feature(doc, target_name) is None:
            self._runtime.increment("rollbacks")
            return True
        self.delete_feature(target_name, delete_absorbed=True)
        # Never report a successful rollback from an optimistic delete result;
        # verify the feature tree after reacquiring the active document.
        doc, err = self.get_active_doc()
        restored = bool(err is None and self._find_sketch_feature(
            doc, target_name) is None)
        if restored:
            self._runtime.increment("rollbacks")
        return restored

    def _document_key(self, doc):
        return self._get_doc_path(doc) or self._get_doc_title(doc)

    def _find_sketch_feature(self, doc, name):
        if hasattr(self, "_find_feature"):
            feat = self._find_feature(doc, name)
            if feat is not None:
                return feat
        feat = com_get(doc, "FirstFeature", default=None)
        while feat is not None:
            if str(com_get(feat, "Name", default="")) == name:
                return feat
            feat = com_get(feat, "GetNextFeature", default=None)
        return None

    def _sketch_specific(self, feature):
        return com_get(feature, "GetSpecificFeature2", default=None)

    def _activate_sketch_feature(self, doc, feature):
        self._last_sketch_activation_orientation = None
        try:
            current = doc.SketchManager.ActiveSketch
            if current is not None:
                current_feature = com_get(current, "GetFeature", default=None)
                if current_feature is not None and str(com_get(
                        current_feature, "Name", default="")) == str(com_get(
                            feature, "Name", default="")):
                    orientation = self._auto_normal_to(
                        doc, zoom_to_fit=True)
                    self._last_sketch_activation_orientation = orientation
                    return bool(orientation.get("success"))
                doc.SketchManager.InsertSketch(True)
        except Exception:
            pass
        doc.ClearSelection2(True)
        try:
            selected = bool(feature.Select2(False, 0))
        except Exception:
            selected = select_by_id2(doc, str(com_get(
                feature, "Name", default="")), "SKETCH")
        if not selected:
            return False
        doc.SketchManager.InsertSketch(True)
        if doc.SketchManager.ActiveSketch is None:
            return False
        orientation = self._auto_normal_to(doc, zoom_to_fit=True)
        self._last_sketch_activation_orientation = orientation
        return bool(orientation.get("success"))

    def _model_to_sketch_point(self, doc, point):
        if len(point) == 2:
            return float(point[0]), float(point[1]), 0.0
        sketch = doc.SketchManager.ActiveSketch
        transform = com_get(sketch, "ModelToSketchTransform", default=None)
        data = com_get(transform, "ArrayData", default=None) if transform else None
        if not data:
            return tuple(float(v) for v in point[:3])
        return transform_point(list(data), point[:3])

    def _sketch_to_model_point(self, doc, point):
        sketch = doc.SketchManager.ActiveSketch
        transform = com_get(sketch, "ModelToSketchTransform", default=None)
        inverse = com_get(transform, "Inverse", default=None) if transform else None
        data = com_get(inverse, "ArrayData", default=None) if inverse else None
        p3 = (float(point[0]), float(point[1]),
              float(point[2]) if len(point) > 2 else 0.0)
        return transform_point(list(data), p3) if data else p3

    def _to_sketch_m(self, doc, point, unit):
        point_m = [self._units.to_meters(float(v), unit) for v in point]
        if len(point_m) == 2:
            return point_m[0], point_m[1], 0.0
        return self._model_to_sketch_point(doc, point_m)

    def _persist(self, doc, obj):
        try:
            raw = doc.Extension.GetPersistReference3(obj)
            if raw:
                return base64.b64encode(bytes(raw)).decode("ascii")
        except Exception:
            pass
        return None

    def _create_spline(self, sketch_manager, points_m):
        flat = [coordinate for point in points_m for coordinate in point]
        # CreateSpline2 is live-supported by both SW2025/2026 and avoids the
        # ambiguous out parameter of CreateSpline3.  Natural ends are chosen
        # because image-fit points are already curvature-regularized.
        try:
            return sketch_manager.CreateSpline2(array_r8(flat), True)
        except Exception:
            try:
                status = win32com.client.VARIANT(
                    pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                return sketch_manager.CreateSpline3(
                    array_r8(flat), None, None, True, status)
            except Exception:
                return None

    @staticmethod
    def _create_b_spline_segments(sketch_manager, control_points_m, knots,
                                  order, periodic):
        order = int(order)
        controls = list(control_points_m)
        knots = [float(value) for value in knots]
        if order < 2 or len(controls) < order:
            raise ValueError("B-spline requires control_points >= order >= 2")
        expected_knots = (len(controls) + 1 if periodic
                          else len(controls) + order)
        if len(knots) != expected_knots:
            raise ValueError(
                "B-spline knot count must equal control_points + 1 for "
                "periodic curves or control_points + order for open curves")
        coordinates = [coordinate for point in controls
                       for coordinate in (point[0], point[1], 0.0)]
        if not all(math.isfinite(float(value))
                   for value in [*knots, *coordinates]):
            raise ValueError("B-spline parameters must be finite")
        # The legacy packed-array API throws server exceptions on SW2026.
        # ISplineParamData is the current typed API and also avoids ambiguous
        # integer packing in a SAFEARRAY of doubles.
        manager = (typed(sketch_manager, "ISketchManager")
                   if getattr(sketch_manager, "_oleobj_", None) is not None
                   else sketch_manager)
        parameter_data = manager.CreateSplineParamData()
        parameter_data.Dimension = 3
        parameter_data.Order = order
        parameter_data.Periodic = 1 if periodic else 0
        parameter_data.ControlPointsCount = len(controls)
        if not parameter_data.SetControlPoints(array_r8(coordinates)):
            raise RuntimeError("SolidWorks rejected B-spline control points")
        if not parameter_data.SetKnotPoints(array_r8(knots)):
            raise RuntimeError("SolidWorks rejected B-spline knot points")
        segments = manager.CreateSplinesByEqnParams2(parameter_data)
        if segments is None:
            return []
        if isinstance(segments, (list, tuple)):
            return [segment for segment in segments if segment is not None]
        return [segments]

    @staticmethod
    def _create_b_spline(sketch_manager, control_points_m, knots, order,
                         periodic):
        segments = ParametricSketchOperations._create_b_spline_segments(
            sketch_manager, control_points_m, knots, order, periodic)
        return segments[0] if segments else None

    @staticmethod
    def _spline_sketch_points(segment):
        values = com_get(segment, "GetPoints2", default=None)
        if values is None:
            return []
        if isinstance(values, (list, tuple)):
            return [point for point in values if point is not None]
        return [values]

    def _match_b_spline_chain_segments(self, segments, sources, unit,
                                       tolerance_mm):
        """Match SW split segments to deterministic source spans by endpoints."""
        if len(segments) != len(sources):
            raise RuntimeError(
                "SolidWorks B-spline chain segment count does not match source")
        actual = []
        for segment in segments:
            points = self._spline_sketch_points(segment)
            if len(points) < 2:
                raise RuntimeError(
                    "SolidWorks B-spline chain endpoints are unavailable")
            start = self._point_coords(points[0], unit)
            end = self._point_coords(points[-1], unit)
            if start is None or end is None:
                raise RuntimeError(
                    "SolidWorks B-spline chain endpoints are unreadable")
            actual.append((segment, start[:2], end[:2]))
        ranked = []
        for actual_index, (_, start, end) in enumerate(actual):
            for source_index, source in enumerate(sources):
                controls = source.get("control_points") or []
                if len(controls) < 2:
                    raise ValueError(
                        "B-spline chain source requires control points")
                source_start = list(map(float, controls[0][:2]))
                source_end = list(map(float, controls[-1][:2]))
                forward = max(math.dist(start, source_start),
                              math.dist(end, source_end))
                reverse = max(math.dist(start, source_end),
                              math.dist(end, source_start))
                ranked.append((min(forward, reverse), actual_index,
                               source_index, reverse < forward))
        assignments = []
        used_actual, used_source = set(), set()
        for error, actual_index, source_index, reversed_order in sorted(ranked):
            if actual_index in used_actual or source_index in used_source:
                continue
            used_actual.add(actual_index)
            used_source.add(source_index)
            assignments.append((source_index, actual_index, error,
                                reversed_order))
        if len(assignments) != len(sources):
            raise RuntimeError(
                "SolidWorks B-spline chain matching is incomplete")
        worst = max((item[2] for item in assignments), default=0.0)
        if worst > max(1e-5, float(tolerance_mm)):
            raise RuntimeError(
                "SolidWorks B-spline chain endpoints do not match source")
        output = []
        for source_index, actual_index, error, reversed_order in sorted(
                assignments):
            source = dict(sources[source_index])
            source["actual_orientation_reversed"] = bool(reversed_order)
            output.append({
                "id": str(source["id"]),
                "segment": actual[actual_index][0],
                "source": source,
                "orientation_reversed": reversed_order,
                "endpoint_error_mm": error,
            })
        return output

    def _create_entity(self, doc, entity, unit):
        manager = doc.SketchManager
        entity_type = str(entity.get("type", "line")).lower()
        construction = bool(entity.get("construction", False) or
                            entity_type == "centerline")
        if entity_type in {"line", "centerline"}:
            start = self._to_sketch_m(doc, entity["start"], unit)
            end = self._to_sketch_m(doc, entity["end"], unit)
            segment = (manager.CreateCenterLine(*start, *end) if construction
                       else manager.CreateLine(*start, *end))
        elif entity_type in {"arc", "arc_center"}:
            center = self._to_sketch_m(doc, entity["center"], unit)
            start = self._to_sketch_m(doc, entity["start"], unit)
            end = self._to_sketch_m(doc, entity["end"], unit)
            segment = manager.CreateArc(*center, *start, *end,
                                        int(entity.get("direction", 1)))
        elif entity_type in {"arc_3pt", "three_point_arc"}:
            start = self._to_sketch_m(doc, entity["start"], unit)
            end = self._to_sketch_m(doc, entity["end"], unit)
            point = self._to_sketch_m(doc, entity.get(
                "point", entity.get("mid")), unit)
            segment = manager.Create3PointArc(*start, *end, *point)
        elif entity_type == "circle":
            center = self._to_sketch_m(doc, entity["center"], unit)
            radius = self._units.to_meters(float(entity["radius"]), unit)
            if not math.isfinite(radius) or radius <= 0.0:
                raise ValueError("Circle radius must be finite and positive")
            segment = manager.CreateCircle(
                center[0], center[1], center[2],
                center[0] + radius, center[1], center[2])
        elif entity_type == "ellipse":
            center_source = entity["center"]
            center = self._to_sketch_m(doc, center_source, unit)
            major_source = entity.get("major_point")
            minor_source = entity.get("minor_point")
            if major_source is None or minor_source is None:
                major_radius = float(entity.get("major_radius", 0.0))
                minor_radius = float(entity.get("minor_radius", 0.0))
                rotation = math.radians(float(entity.get(
                    "rotation_deg", entity.get("angle", 0.0))))
                if (not math.isfinite(major_radius) or
                        not math.isfinite(minor_radius) or
                        major_radius <= 0.0 or minor_radius <= 0.0):
                    raise ValueError(
                        "Ellipse major_radius and minor_radius must be "
                        "finite and positive")
                cx, cy = map(float, center_source[:2])
                major_source = [
                    cx + major_radius * math.cos(rotation),
                    cy + major_radius * math.sin(rotation)]
                minor_source = [
                    cx - minor_radius * math.sin(rotation),
                    cy + minor_radius * math.cos(rotation)]
            major = self._to_sketch_m(doc, major_source, unit)
            minor = self._to_sketch_m(doc, minor_source, unit)
            if (math.dist(center, major) <= 1e-12 or
                    math.dist(center, minor) <= 1e-12):
                raise ValueError("Ellipse axes must be non-zero")
            segment = manager.CreateEllipse(
                *center, *major, *minor)
        elif entity_type == "spline":
            points = entity.get("fit_points") or entity.get("points")
            if not points or len(points) < 2:
                raise ValueError("Spline requires at least two fit_points")
            segment = self._create_spline(
                manager, [self._to_sketch_m(doc, point, unit)
                          for point in points])
        elif entity_type == "b_spline":
            controls = entity.get("control_points") or []
            segment = self._create_b_spline(
                manager,
                [self._to_sketch_m(doc, point, unit) for point in controls],
                entity.get("knots") or [], int(entity.get("order", 4)),
                bool(entity.get("periodic", False)))
        elif entity_type == "b_spline_chain":
            controls = entity.get("control_points") or []
            segments = self._create_b_spline_segments(
                manager,
                [self._to_sketch_m(doc, point, unit) for point in controls],
                entity.get("knots") or [], int(entity.get("order", 4)),
                bool(entity.get("periodic", False)))
            if not segments:
                raise RuntimeError(
                    "SolidWorks failed to create 'b_spline_chain'")
            if construction:
                for item in segments:
                    item.ConstructionGeometry = True
            return {
                "entity_group": True,
                "items": self._match_b_spline_chain_segments(
                    segments, entity.get("segments") or [], unit,
                    float(entity.get("endpoint_match_tolerance_mm", 0.002))),
            }
        else:
            raise ValueError(f"Unsupported sketch entity type '{entity_type}'")
        if segment is None:
            raise RuntimeError(f"SolidWorks failed to create '{entity_type}'")
        if construction and entity_type != "centerline":
            try:
                segment.ConstructionGeometry = True
            except Exception:
                pass
        return segment

    def _entity_points(self, segment):
        result = {}
        for label, member in (("start", "GetStartPoint2"),
                              ("end", "GetEndPoint2"),
                              ("center", "GetCenterPoint2")):
            point = com_get(segment, member, default=None)
            if point is not None:
                result[label] = point
        if "start" not in result or "end" not in result:
            points = self._spline_sketch_points(segment)
            if len(points) >= 2:
                result.setdefault("start", points[0])
                result.setdefault("end", points[-1])
        return result

    @staticmethod
    def _select_com_object(obj, append, doc):
        if obj is None:
            return False
        selection_data = create_select_data(doc, 0)
        for method, args in (("Select4", (append, selection_data)),
                             ("Select2", (append, 0)),
                             ("Select", (append,))):
            try:
                return bool(com_get(obj, method, *args))
            except Exception:
                continue
        return False

    def _resolve_entity_ref(self, records, reference):
        if reference in {"origin", "sketch_origin"}:
            return ("origin", None)
        if not isinstance(reference, str):
            return (None, None)
        entity_id, _, suffix = reference.partition(".")
        record = records.get(entity_id)
        if not record:
            return (None, None)
        if suffix in {"start", "end", "center"}:
            return (suffix, record["points"].get(suffix))
        return ("entity", record["object"])

    def _select_reference(self, doc, records, reference, append=False):
        kind, obj = self._resolve_entity_ref(records, reference)
        if kind == "origin":
            return select_by_id2(doc, "Origin", "EXTSKETCHPOINT",
                                 append=append)
        return self._select_com_object(obj, append, doc)

    def _validate_constraint_graph(self, entities, constraints):
        ids = {str(entity.get("id")) for entity in entities}
        errors, seen = [], set()
        for index, constraint in enumerate(constraints or []):
            ctype = str(constraint.get("type", "")).lower()
            if ctype not in RELATION_CODES:
                errors.append(f"constraint[{index}]: unsupported type '{ctype}'")
            refs = list(constraint.get("entities", []))
            if constraint.get("about"):
                refs.append(constraint["about"])
            for ref in refs:
                base = str(ref).split(".", 1)[0]
                if base not in ids and base not in {"origin", "sketch_origin",
                                                    "axis_x", "axis_y"}:
                    errors.append(f"constraint[{index}]: unknown ref '{ref}'")
            canonical = (ctype, tuple(sorted(map(str, refs))))
            if canonical in seen:
                errors.append(f"constraint[{index}]: duplicate relation")
            seen.add(canonical)
        return errors

    def _apply_constraint(self, doc, records, constraint,
                          allow_redundant=False):
        ctype = str(constraint.get("type", "")).lower()
        refs = list(constraint.get("entities", []))
        about = constraint.get("about")
        if about:
            refs.append(about)
        doc.ClearSelection2(True)
        selected = 0
        for ref in refs:
            if ref in {"axis_x", "axis_y"}:
                # Explicit construction axes should be supplied as entities;
                # hidden implicit axes are not stable selection targets.
                raise ValueError(
                    f"'{ref}' must be declared as a centerline entity")
            if self._select_reference(doc, records, ref, append=selected > 0):
                selected += 1
        if selected != len(refs):
            raise ValueError(
                f"Could not select all refs for {ctype}: {refs}")
        before = self._active_relation_count(doc)
        relation = doc.SketchAddConstraints(RELATION_CODES[ctype])
        after = self._active_relation_count(doc)
        if relation is False or (before is not None and after is not None and
                                 after <= before):
            if allow_redundant:
                return None
            raise RuntimeError(f"SolidWorks rejected {ctype} relation")
        return {"type": ctype, "before": before, "after": after}

    def _active_relation_count(self, doc):
        sketch = com_get(com_get(
            doc, "SketchManager", default=None), "ActiveSketch", default=None)
        if sketch is None:
            return None
        return len(self._sketch_relations(sketch))

    @staticmethod
    def _sketch_relations(sketch):
        """Read relations through the manager when the legacy accessor is empty."""
        direct = com_get(sketch, "GetRelations", default=None)
        try:
            relations = list(direct or [])
        except TypeError:
            relations = []
        if relations:
            return relations
        manager = com_get(sketch, "RelationManager", default=None)
        managed = com_get(manager, "GetRelations", 0, default=None)
        try:
            return list(managed or [])
        except TypeError:
            return []

    def _dimension_position_model_m(self, doc, position, unit):
        if not position:
            position = [0.0, 0.0]
        sketch_m = [self._units.to_meters(float(v), unit) for v in position]
        return self._sketch_to_model_point(doc, sketch_m)

    def _dimension_name(self, display_dimension, dimension):
        for obj, member in ((dimension, "FullName"), (dimension, "Name"),
                            (display_dimension, "GetNameForSelection")):
            value = com_get(obj, member, default=None)
            if value:
                return str(value)
        return None

    def _create_dimension(self, doc, records, request, default_unit):
        unit = request.get("unit") or default_unit
        dim_type = str(request.get("type", "distance")).lower()
        refs = request.get("entities") or []
        if not refs:
            refs = [request.get("from"), request.get("to")]
            refs = [ref for ref in refs if ref]
        if dim_type in {"radius", "diameter", "length"} and not refs:
            candidate = request.get("entity") or request.get("id")
            if candidate in records:
                refs = [candidate]
        doc.ClearSelection2(True)
        for index, ref in enumerate(refs):
            if not self._select_reference(doc, records, ref, append=index > 0):
                raise ValueError(f"Could not select dimension ref '{ref}'")
        position = self._dimension_position_model_m(
            doc, request.get("text_position"), unit)
        method_name = {
            "horizontal": "AddHorizontalDimension2",
            "vertical": "AddVerticalDimension2",
            "radius": "AddRadialDimension2",
            "diameter": "AddDiameterDimension2",
        }.get(dim_type, "AddDimension2")
        display = com_get(doc, method_name, *position, default=None)
        if display is None:
            raise RuntimeError(f"{method_name} returned no dimension")
        dimension = com_get(display, "GetDimension2", 0, default=None)
        if dimension is None:
            raise RuntimeError("Could not obtain IDimension from display dimension")
        value = request.get("value")
        if value is not None:
            value_m = (math.radians(float(value)) if dim_type == "angle" else
                       self._units.to_meters(float(value), unit))
            dimension.SystemValue = value_m
            actual = float(com_get(dimension, "SystemValue", default=value_m))
            tolerance = max(1e-10, abs(value_m) * 1e-8)
            if abs(actual - value_m) > tolerance:
                raise RuntimeError(
                    f"Dimension read-back mismatch: {actual} != {value_m}")
        actual_name = self._dimension_name(display, dimension)
        driven_state = com_get(dimension, "DrivenState", default=None)
        try:
            driving_value = resolve_solidworks_constant("swDimensionDriving")
            driven_value = resolve_solidworks_constant("swDimensionDriven")
            unknown_value = resolve_solidworks_constant(
                "swDimensionDrivenUnknown")
        except LookupError:
            # Documented swDimensionDrivenState_e values are stable, but the
            # symbolic constants remain the primary source when available.
            driving_value, driven_value, unknown_value = 2, 1, 0
        state_name = ({driving_value: "driving", driven_value: "driven",
                       unknown_value: "unknown"}.get(driven_state, "unknown"))
        return {"id": request.get("id"), "name": actual_name,
                "type": dim_type, "value": value, "unit": unit,
                "driving": driven_state == driving_value,
                "driven_state": driven_state,
                "driven_state_name": state_name,
                "display_object": display, "dimension_object": dimension}

    def _delete_objects(self, doc, objects):
        deleted = 0
        for obj in reversed(objects):
            doc.ClearSelection2(True)
            if self._select_com_object(obj, False, doc):
                try:
                    if doc.Extension.DeleteSelection2(0):
                        deleted += 1
                except Exception:
                    pass
        return deleted

    def add_dimensions_batch(self, sketch_name: str,
                             dimensions: List[Dict[str, Any]],
                             suppress_modify_dialog: bool = True,
                             rebuild: str = "once",
                             rollback_on_failure: bool = True,
                             guard_policy: str = "operation_scoped",
                             unit: str = None) -> Dict:
        doc, err = self.get_active_doc()
        if err:
            return err
        feature = self._find_sketch_feature(doc, sketch_name)
        if feature is None or not self._activate_sketch_feature(doc, feature):
            return self._error("SKETCH_UNDERDEFINED",
                               f"Sketch '{sketch_name}' was not found/activated",
                               details={"orientation": getattr(
                                   self,
                                   "_last_sketch_activation_orientation",
                                   None)})
        activation_orientation = getattr(
            self, "_last_sketch_activation_orientation", None)
        records = self._runtime.get_entities(
            self._document_key(doc), sketch_name)
        created_objects, results = [], []
        started = time.perf_counter()
        try:
            guard = (self.dimension_input_guard(guard_policy)
                     if suppress_modify_dialog else _NullContext())
            with guard:
                for request in dimensions or []:
                    item = self._create_dimension(doc, records, request, unit)
                    created_objects.append(item["display_object"])
                    results.append({k: v for k, v in item.items()
                                    if not k.endswith("_object")})
            rebuild_count = 0
            solver_elapsed = 0.0
            if rebuild != "none":
                solve_start = time.perf_counter()
                com_get(doc, "EditRebuild3", default=False)
                solver_elapsed = time.perf_counter() - solve_start
                rebuild_count = 1
                self.record_rebuild(solver_elapsed)
            ui = __import__(
                "solidworks_mcp.automation.com_utils",
                fromlist=["detect_modal_dialog"]).detect_modal_dialog()
            if ui.get("modal"):
                raise RuntimeError("A modal dialog appeared during dimension batch")
            return self._result(
                True, f"Created {len(results)} dimension(s)", SwErrors.swSuccess,
                {"sketch": sketch_name, "dimensions": results,
                 "rebuild_count": rebuild_count,
                 "solver_time_sec": round(solver_elapsed, 6),
                 "modify_dialog_suppressed": suppress_modify_dialog,
                 "guard_policy": guard_policy,
                 "orientation": ((activation_orientation or {}).get(
                     "data", activation_orientation)),
                 "dimension_input_guard": {
                     "preference_original": getattr(guard, "original", None),
                     "disabled_verified": bool(getattr(
                         guard, "disabled_verified", False)),
                     "preference_restored": getattr(guard, "restored", None),
                 },
                 "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
        except Exception as exc:
            deleted = self._delete_objects(doc, created_objects) if rollback_on_failure else 0
            return self._error(
                "SKETCH_OVERDEFINED", f"Dimension batch failed: {exc}",
                stage="add_dimensions", document_restored=(
                    deleted == len(created_objects) if rollback_on_failure else False),
                details={"created_before_failure": len(created_objects),
                         "deleted_on_rollback": deleted, "dimensions": results})

    def _sketch_status(self, sketch):
        status = int(com_get(sketch, "GetConstrainedStatus", default=1) or 1)
        return CONSTRAINED_STATUS.get(status, f"unknown_{status}"), status

    def _geometry_topology(self, records, tolerance_m=1e-8):
        endpoint_map = defaultdict(list)
        for entity_id, record in records.items():
            if record.get("construction"):
                continue
            for end_name in ("start", "end"):
                point = record["points"].get(end_name)
                if point is None:
                    continue
                coords = [float(com_get(point, axis, default=0.0))
                          for axis in ("X", "Y", "Z")]
                key = tuple(round(value / tolerance_m) for value in coords)
                endpoint_map[key].append(f"{entity_id}.{end_name}")
        open_endpoints = [refs[0] for refs in endpoint_map.values()
                          if len(refs) % 2 == 1]
        return endpoint_map, open_endpoints

    @staticmethod
    def _topology_validation_message(open_endpoints, closed_contours,
                                     validation):
        """Validate closed-loop count independently from allowed open paths."""
        if "closed_contours" in validation:
            expected = int(validation["closed_contours"])
            if closed_contours != expected:
                return (f"Expected {expected} closed contour(s); got "
                        f"{closed_contours}")
        if validation.get("require_closed", False) and open_endpoints:
            return ("Sketch must be fully closed; open endpoints="
                    f"{open_endpoints[:8]}")
        return None

    @staticmethod
    def _locked_trace_constraints(records, constraints):
        """Build non-redundant Fix relations for exact trace geometry."""
        result = []
        already_fixed = {
            str(item.get("entities", [""])[0])
            for item in constraints
            if str(item.get("type", "")).lower() == "fixed"
            and len(item.get("entities", [])) == 1
        }
        point_refs = set()
        point_objects = {}
        sliding_types = {
            "line", "centerline", "arc", "arc_center",
            "arc_3pt", "three_point_arc",
        }
        for entity_id, record in records.items():
            if entity_id not in already_fixed:
                result.append({"type": "fixed", "entities": [entity_id]})
            if record.get("type") in sliding_types:
                for suffix in ("start", "end"):
                    if record.get("points", {}).get(suffix) is not None:
                        reference = f"{entity_id}.{suffix}"
                        point_refs.add(reference)
                        point_objects[reference] = record["points"][suffix]

        # Coincident endpoints form one geometric vertex. Fixing every member
        # would be redundant; fixing one representative locks the whole class.
        parent = {reference: reference for reference in point_refs}

        def find(reference):
            while parent[reference] != reference:
                parent[reference] = parent[parent[reference]]
                reference = parent[reference]
            return reference

        def union(left, right):
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for constraint in constraints:
            if str(constraint.get("type", "")).lower() != "coincident":
                continue
            refs = [str(reference) for reference in constraint.get(
                "entities", []) if str(reference) in point_refs]
            for reference in refs[1:]:
                union(refs[0], reference)

        # AddToDB suppresses inferred relations, but SolidWorks may still reuse
        # one SketchPoint for graph edges created at exactly the same vertex.
        # Group such endpoints by sub-nanometre coordinates so a shared point
        # receives one Fix relation instead of one Fix per incident segment.
        coordinate_groups = defaultdict(list)
        tolerance_m = 1e-10
        for reference in sorted(point_refs):
            point = point_objects[reference]
            coords = tuple(float(com_get(
                point, axis, default=0.0)) for axis in ("X", "Y", "Z"))
            key = tuple(round(value / tolerance_m) for value in coords)
            coordinate_groups[key].append(reference)
        for members in coordinate_groups.values():
            for reference in members[1:]:
                union(members[0], reference)

        classes = defaultdict(list)
        for reference in sorted(point_refs):
            classes[find(reference)].append(reference)
        for members in sorted(classes.values(), key=lambda value: min(value)):
            if any(member in already_fixed for member in members):
                continue
            result.append({"type": "fixed",
                           "entities": [min(members)]})
        return result

    @staticmethod
    def _directed_arc_extrema(center, radius, start_angle, end_angle,
                              clockwise=False, full_circle=False):
        """Return endpoints and in-sweep cardinal extrema for a 2D arc."""
        center = [float(center[0]), float(center[1])]
        radius = float(radius)
        start_angle = float(start_angle)
        end_angle = float(end_angle)
        if radius <= 0.0:
            return []
        two_pi = 2.0 * math.pi
        if full_circle:
            angles = [0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0]
        else:
            total = ((start_angle - end_angle) if clockwise else
                     (end_angle - start_angle)) % two_pi
            angles = [start_angle, end_angle]
            for candidate in (
                    0.0, math.pi / 2.0, math.pi,
                    3.0 * math.pi / 2.0):
                travel = ((start_angle - candidate) if clockwise else
                          (candidate - start_angle)) % two_pi
                if travel <= total + 1e-12:
                    angles.append(candidate)
        return [[center[0] + radius * math.cos(angle),
                 center[1] + radius * math.sin(angle)]
                for angle in angles]

    def _entity_bbox(self, entities, unit):
        points = []
        for entity in entities:
            for key in ("start", "end", "center", "point", "mid",
                        "major_point", "minor_point"):
                value = entity.get(key)
                if value:
                    points.append(value[:2])
            points.extend([p[:2] for p in entity.get("fit_points", [])])
            points.extend([p[:2] for p in entity.get("control_points", [])])
            entity_type = str(entity.get("type", "")).lower()
            center = entity.get("center")
            radius = float(entity.get("radius", 0.0) or 0.0)
            if entity_type in {"arc_3pt", "three_point_arc"}:
                parameters = self._arc_three_point_parameters(entity)
                if parameters:
                    arc_center, arc_radius, start, direction, sweep = parameters
                    points.extend(self._directed_arc_extrema(
                        arc_center, arc_radius, start,
                        start + direction * sweep,
                        clockwise=direction < 0))
            elif entity_type in {"arc", "circle"} and center and radius > 0.0:
                start_point, end_point = entity.get("start"), entity.get("end")
                full_circle = bool(
                    entity_type == "circle" or not start_point or
                    not end_point or
                    math.dist(start_point[:2], end_point[:2]) <= 1e-9)
                start = float(entity.get(
                    "start_angle", math.atan2(
                        start_point[1] - center[1],
                        start_point[0] - center[0])
                    if start_point else 0.0))
                end = float(entity.get(
                    "end_angle", math.atan2(
                        end_point[1] - center[1],
                        end_point[0] - center[0])
                    if end_point else start))
                points.extend(self._directed_arc_extrema(
                    center, radius, start, end,
                    clockwise=bool(entity.get("clockwise", False)),
                    full_circle=full_circle))
            elif entity_type == "ellipse" and center:
                if entity.get("major_point") and entity.get("minor_point"):
                    major = [float(entity["major_point"][axis]) -
                             float(center[axis]) for axis in range(2)]
                    minor = [float(entity["minor_point"][axis]) -
                             float(center[axis]) for axis in range(2)]
                else:
                    angle = math.radians(float(
                        entity.get("rotation_deg", 0.0)))
                    major_radius = float(
                        entity.get("major_radius", 0.0) or 0.0)
                    minor_radius = float(
                        entity.get("minor_radius", 0.0) or 0.0)
                    major = [major_radius * math.cos(angle),
                             major_radius * math.sin(angle)]
                    minor = [-minor_radius * math.sin(angle),
                             minor_radius * math.cos(angle)]
                extent = [math.hypot(major[axis], minor[axis])
                          for axis in range(2)]
                points.extend([
                    [float(center[0]) - extent[0],
                     float(center[1]) - extent[1]],
                    [float(center[0]) + extent[0],
                     float(center[1]) + extent[1]]])
        if not points:
            return None
        return {"min": [min(p[i] for p in points) for i in range(2)],
                "max": [max(p[i] for p in points) for i in range(2)],
                "unit": unit or self._units.default_unit.value}

    def create_parametric_sketch(self, name: str, plane: str = "Front",
                                 entities: List[Dict[str, Any]] = None,
                                 constraints: List[Dict[str, Any]] = None,
                                 dimensions: List[Dict[str, Any]] = None,
                                 equations: List[Dict[str, Any]] = None,
                                 solve: Dict[str, Any] = None,
                                 validation: Dict[str, Any] = None,
                                 transaction: Dict[str, Any] = None,
                                 unit: str = None,
                                 idempotency_key: str = None,
                                 output_mode: str = None) -> Dict:
        cached = self._runtime.idempotent_get(idempotency_key)
        if cached is not None:
            cached.setdefault("data", {})["idempotent_replay"] = True
            return cached
        entities = list(entities or [])
        constraints = list(constraints or [])
        dimensions = list(dimensions or [])
        equations = list(equations or [])
        solve = solve or {}
        validation = validation or {}
        transaction = transaction or {}
        if not name or not entities:
            return self._error("SKETCH_OPEN_CONTOUR",
                               "Sketch name and entities are required")
        requested_entities = [
            source
            for entity in entities
            for source in (entity.get("segments") or [entity]
                           if entity.get("type") == "b_spline_chain"
                           else [entity])
        ]
        if len(requested_entities) > int(validation.get("max_entities", 500)):
            return self._error("BUDGET_EXCEEDED", "max_entities exceeded",
                               details={"entities": len(requested_entities)})
        ids = [str(entity.get("id", "")) for entity in requested_entities]
        if any(not entity_id for entity_id in ids) or len(ids) != len(set(ids)):
            return self._error("SKETCH_OVERDEFINED",
                               "Entity IDs must be present and unique")
        graph_errors = self._validate_constraint_graph(entities, constraints)
        if graph_errors:
            return self._error("SKETCH_OVERDEFINED",
                               "Constraint graph validation failed",
                               conflicting_entities=graph_errors)
        doc, err = self.get_active_doc()
        if err:
            return err
        if self._find_sketch_feature(doc, name) is not None:
            return self._error("SKETCH_OVERDEFINED",
                               f"A feature named '{name}' already exists",
                               recommended_actions=[
                                   "Use an idempotency_key or a unique sketch name."])
        created_feature = None
        records = {}
        relations_created = []
        locked_trace_relations_skipped = 0
        started = time.perf_counter()
        phase_timings = {}
        entity_timings = []
        rebuild_count = 0
        try:
            previous_feature_names = self._snapshot_feature_names_parametric(doc)
            created = self.create_sketch(plane)
            if not created.get("success"):
                return created
            doc, sketch = self._wait_for_active_sketch(doc)
            created_feature = com_get(
                sketch, "GetFeature", default=None) if sketch is not None else None
            if created_feature is None:
                created_feature = self._find_new_sketch_feature(
                    doc, previous_feature_names)
            if sketch is None and created_feature is not None:
                if self._activate_sketch_feature(doc, created_feature):
                    doc, sketch = self._wait_for_active_sketch(doc, 1.0)
            if sketch is None:
                raise RuntimeError("Active sketch is unavailable after bounded wait")
            if created_feature is None:
                raise RuntimeError("Active sketch feature is unavailable")
            if hasattr(self, "_rename_feature_safe"):
                name, warning = self._rename_feature_safe(doc, created_feature, name)
            else:
                created_feature.Name = name
                name = str(com_get(created_feature, "Name", default=name))
                warning = None
            phase_timings["sketch_setup"] = round(
                time.perf_counter() - started, 6)
            manager = doc.SketchManager
            old_add_to_db = bool(com_get(manager, "AddToDB", default=False))
            geometry_started = time.perf_counter()
            try:
                manager.AddToDB = True
                for entity in entities:
                    entity_started = time.perf_counter()
                    created_entity = self._create_entity(doc, entity, unit)
                    if (isinstance(created_entity, dict) and
                            created_entity.get("entity_group")):
                        created_items = created_entity.get("items") or []
                    else:
                        created_items = [{
                            "id": str(entity["id"]),
                            "segment": created_entity,
                            "source": entity,
                            "orientation_reversed": False,
                            "endpoint_error_mm": 0.0,
                        }]
                    for item in created_items:
                        segment = item["segment"]
                        source = item["source"]
                        entity_id = str(item["id"])
                        records[entity_id] = {
                            "object": segment,
                            "type": str(source.get(
                                "type", "line")).lower(),
                            "construction": bool(source.get(
                                "construction", False) or
                                source.get("type") == "centerline"),
                            "points": self._entity_points(segment),
                            "persistent_id": self._persist(doc, segment),
                            "source": source,
                            "orientation_reversed": bool(item.get(
                                "orientation_reversed", False)),
                            "endpoint_error_mm": float(item.get(
                                "endpoint_error_mm", 0.0)),
                        }
                    entity_timings.append({
                        "id": entity_id,
                        "type": str(entity.get("type", "line")).lower(),
                        "fit_points": len(entity.get("fit_points", [])),
                        "control_points": len(
                            entity.get("control_points", [])),
                        "created_segments": len(created_items),
                        "elapsed_sec": round(
                            time.perf_counter() - entity_started, 6),
                    })
            finally:
                try:
                    manager.AddToDB = old_add_to_db
                except Exception:
                    pass
            phase_timings["geometry_creation"] = round(
                time.perf_counter() - geometry_started, 6)
            relation_started = time.perf_counter()
            for constraint in constraints:
                relations_created.append(self._apply_constraint(
                    doc, records, constraint))
            if output_mode == "locked_trace" or solve.get(
                    "mode") == "locked_trace":
                for constraint in self._locked_trace_constraints(
                        records, constraints):
                    relation = self._apply_constraint(
                        doc, records, constraint, allow_redundant=True)
                    if relation is None:
                        locked_trace_relations_skipped += 1
                    else:
                        relations_created.append(relation)
            phase_timings["relations"] = round(
                time.perf_counter() - relation_started, 6)
            parameter_started = time.perf_counter()
            dimension_results = []
            dimension_guard = (self.dimension_input_guard(
                solve.get("dimension_guard_policy", "operation_scoped"))
                if dimensions else _NullContext())
            with dimension_guard:
                for request in dimensions:
                    item = self._create_dimension(doc, records, request, unit)
                    dimension_results.append({k: v for k, v in item.items()
                                              if not k.endswith("_object")})
            equation_results = []
            if equations:
                manager_eq = com_get(doc, "GetEquationMgr", default=None)
                if manager_eq is None:
                    raise RuntimeError("Equation manager is unavailable")
                dimension_by_id = {item.get("id"): item.get("name")
                                   for item in dimension_results}
                for equation in equations:
                    target = dimension_by_id.get(
                        equation.get("dimension"), equation.get("dimension"))
                    expression = str(equation.get("expression", ""))
                    equation_text = f'"{target}" = {expression}'
                    index = com_get(manager_eq, "Add2", -1, equation_text,
                                    True, default=-1)
                    if int(index) < 0:
                        raise RuntimeError(
                            f"Equation rejected: {equation_text}")
                    equation_results.append({"index": int(index),
                                             "equation": equation_text})
            phase_timings["dimensions_and_equations"] = round(
                time.perf_counter() - parameter_started, 6)
            solve_started = time.perf_counter()
            com_get(doc, "EditRebuild3", default=False)
            solver_elapsed = time.perf_counter() - solve_started
            rebuild_count = 1
            self.record_rebuild(solver_elapsed)
            verification_started = time.perf_counter()
            construction_only = bool(records) and all(
                bool(record.get("construction"))
                for record in records.values())
            construction_reference_fast_path = (
                output_mode == "construction_reference" and construction_only)
            constraint_status_evaluation_skipped = (
                construction_reference_fast_path)
            if constraint_status_evaluation_skipped:
                # GetConstrainedStatus invokes the sketch solver even though a
                # construction-reference sketch has no solve target.  SW2026
                # can block for minutes on a composite equation-NURBS here.
                # Exact endpoint metadata and the mandatory reverse-raster gate
                # validate the committed reference geometry instead.
                status = "not_evaluated_construction_reference"
                status_code = None
            else:
                status, status_code = self._sketch_status(sketch)
            endpoint_map, open_endpoints = self._geometry_topology(records)
            contour_enumeration_skipped = construction_reference_fast_path
            if contour_enumeration_skipped:
                # Construction geometry cannot form a SolidWorks sketch
                # contour.  Asking SW2026 to enumerate contours for a large
                # equation-NURBS chain can block the COM apartment for minutes,
                # while the result is necessarily empty.  The reverse-raster
                # gate validates this reference geometry after the sketch is
                # committed, so skipping the inapplicable contour query does
                # not weaken geometric verification.
                closed_contours = 0
            else:
                contours = com_get(
                    sketch, "GetSketchContours", default=[]) or []
                closed_contours = sum(
                    1 for contour in contours if bool(com_get(
                        contour, "IsClosed", default=False)))
            topology_error = self._topology_validation_message(
                open_endpoints, closed_contours, validation)
            phase_timings["verification"] = round(
                time.perf_counter() - verification_started, 6)
            if topology_error:
                raise _SketchValidationError(
                    "SKETCH_OPEN_CONTOUR", topology_error)
            orientation_after = self._auto_normal_to(
                doc, zoom_to_fit=True)
            if not orientation_after.get("success"):
                raise RuntimeError(
                    "Normal To / Fit to Screen verification failed after "
                    f"parametric geometry creation: {orientation_after}")
            target = solve.get("target")
            if target == "fully_defined" and status != "fully_defined":
                if solve.get("allow_fix_fallback", False):
                    raise _SketchValidationError(
                        "SKETCH_UNDERDEFINED",
                        "Hidden Fix fallback is prohibited; choose locked_trace explicitly")
                analysis = self._analyze_sketch_objects(
                    doc, created_feature, sketch, include_recommendations=True)
                raise _SketchValidationError(
                    "SKETCH_UNDERDEFINED", "Sketch did not reach fully_defined",
                    details=analysis)
            serializable_records = {
                entity_id: {k: v for k, v in record.items()
                            if k not in {"object", "points"}}
                for entity_id, record in records.items()}
            # Keep live COM objects in session memory and persist stable IDs in
            # exported results for recovery after a server restart.
            self._runtime.register_entities(
                self._document_key(doc), name, records)
            data = {
                "sketch": name, "sketch_name": name, "status": status,
                "status_code": status_code,
                "constraint_status_evaluation_skipped":
                    constraint_status_evaluation_skipped,
                "entities_created": len(records),
                "relations_created": len(relations_created),
                "locked_trace_relations_skipped":
                    locked_trace_relations_skipped,
                "dimensions_created": len(dimension_results),
                "equations_created": len(equation_results),
                "dimensions": dimension_results, "equations": equation_results,
                "dimension_input_guard": {
                    "preference_original": getattr(
                        dimension_guard, "original", None),
                    "disabled_verified": bool(getattr(
                        dimension_guard, "disabled_verified", False)),
                    "preference_restored": getattr(
                        dimension_guard, "restored", None),
                },
                "rebuild_count": rebuild_count,
                "solver_time_sec": round(solver_elapsed, 6),
                "phase_timings_sec": phase_timings,
                "slowest_entities": sorted(
                    entity_timings,
                    key=lambda item: item["elapsed_sec"], reverse=True)[:10],
                "closed_contours": closed_contours,
                "contour_enumeration_skipped": contour_enumeration_skipped,
                "open_endpoints": open_endpoints,
                "self_intersections": [],
                "bbox": self._entity_bbox(entities, unit),
                "entity_ids": serializable_records,
                "rename_warning": warning,
                "orientation": {
                    "sketch_setup": created.get("data", {}).get(
                        "orientation"),
                    "after_geometry": orientation_after.get("data", {}),
                },
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            result = self._result(True, f"Parametric sketch '{name}' created",
                                  SwErrors.swSuccess, data)
            self._runtime.idempotent_put(idempotency_key, result)
            return result
        except _SketchValidationError as exc:
            restored = False
            if transaction.get("rollback_on_failure", True) and created_feature is not None:
                restored = self._rollback_created_sketch(created_feature, name)
            return self._error(exc.code, str(exc),
                               document_restored=restored,
                               details=exc.details)
        except Exception as exc:
            restored = False
            if transaction.get("rollback_on_failure", True) and created_feature is not None:
                restored = self._rollback_created_sketch(created_feature, name)
            return self._error(
                "SKETCH_OVERDEFINED", f"Atomic sketch failed: {exc}",
                document_restored=restored,
                com_hresult=getattr(exc, "hresult", None),
                details={"entities_created": len(records),
                         "relations_created": len(relations_created),
                         "phase_timings_sec": phase_timings,
                         "slowest_entities": sorted(
                             entity_timings,
                             key=lambda item: item["elapsed_sec"],
                             reverse=True)[:10],
                         "locked_trace_relations_skipped":
                             locked_trace_relations_skipped,
                         "rebuild_count": rebuild_count})

    def _point_coords(self, point, unit):
        if point is None:
            return None
        values = [float(com_get(point, axis, default=0.0))
                  for axis in ("X", "Y", "Z")]
        return [self._units.from_meters(value, unit) for value in values]

    def _segment_type(self, segment):
        raw_code = com_get(segment, "GetType", default=-1)
        code = int(-1 if raw_code is None else raw_code)
        return SEGMENT_TYPE.get(code, f"unknown_{code}"), code

    def _sketch_segments(self, sketch):
        return list(com_get(sketch, "GetSketchSegments", default=[]) or [])

    def _analyze_sketch_objects(self, doc, feature, sketch,
                                include_recommendations=True):
        segments = self._sketch_segments(sketch)
        status, status_code = self._sketch_status(sketch)
        document_key = self._document_key(doc)
        sketch_name = str(com_get(feature, "Name", default="Sketch"))
        registered = self._runtime.get_entities(document_key, sketch_name)
        pid_to_id = {record.get("persistent_id"): entity_id
                     for entity_id, record in registered.items()
                     if record.get("persistent_id")}
        items, graph = [], defaultdict(set)
        endpoint_keys = defaultdict(list)
        for index, segment in enumerate(segments):
            persistent_id = self._persist(doc, segment)
            entity_id = pid_to_id.get(persistent_id, f"seg_{index + 1:03d}")
            kind, kind_code = self._segment_type(segment)
            constrained = int(com_get(
                segment, "GetConstrainedStatus", default=status_code) or status_code)
            coords = {}
            for point_name, member in (("start", "GetStartPoint2"),
                                       ("end", "GetEndPoint2"),
                                       ("center", "GetCenterPoint2")):
                point = com_get(segment, member, default=None)
                value = self._point_coords(point, self._units.default_unit.value)
                if value is not None:
                    coords[point_name] = value[:2]
                    if point_name in {"start", "end"}:
                        key = tuple(round(v, 6) for v in value[:2])
                        endpoint_keys[key].append((entity_id, point_name))
            free = []
            if constrained == 2:
                if kind == "line":
                    free = ["x", "y", "rotation", "length"]
                elif kind in {"arc", "circle"}:
                    free = ["center_x", "center_y", "radius"]
                elif kind == "spline":
                    free = ["control_points"]
                else:
                    free = ["position", "shape"]
            recommendation = None
            if include_recommendations and free:
                recommendation = (
                    {"type": "coincident", "target": "origin",
                     "reason": "anchor one connected component"}
                    if index == 0 else
                    {"type": "dimension", "dimension": free[-1],
                     "reason": "remove one independent degree of freedom"})
            items.append({
                "entity": entity_id, "type": kind,
                "persistent_id": persistent_id, "coordinates": coords,
                "status": CONSTRAINED_STATUS.get(constrained,
                                                  f"unknown_{constrained}"),
                "free": free, "relations": [], "dimensions": [],
                "component": None, "recommendation": recommendation,
            })
        for refs in endpoint_keys.values():
            for left, _ in refs:
                for right, _ in refs:
                    if left != right:
                        graph[left].add(right)
        component_by_id, component = {}, 0
        all_ids = [item["entity"] for item in items]
        for entity_id in all_ids:
            if entity_id in component_by_id:
                continue
            component += 1
            queue = deque([entity_id])
            component_by_id[entity_id] = component
            while queue:
                current = queue.popleft()
                for neighbor in graph[current]:
                    if neighbor not in component_by_id:
                        component_by_id[neighbor] = component
                        queue.append(neighbor)
        for item in items:
            item["component"] = component_by_id[item["entity"]]
        remaining = sum(len(item["free"]) for item in items)
        conflicts = []
        if status in {"over_defined", "no_solution", "invalid_solution"}:
            relations = self._sketch_relations(sketch)
            for index, relation in enumerate(relations[:8]):
                conflicts.append({
                    "relation": f"relation_{index + 1}",
                    "type": str(com_get(relation, "GetRelationType", default="?")),
                    "status": str(com_get(relation, "GetStatus", default="?")),
                })
        fixed_count = sum(1 for relation in self._sketch_relations(sketch)
            if int(com_get(relation, "GetRelationType", default=-1) or -1) == 17)
        quality = max(0.0, 1.0 - fixed_count / max(1, len(segments)))
        return {"sketch": sketch_name, "status": status,
                "status_code": status_code, "remaining_dof": remaining,
                "items": items, "connectivity_components": component,
                "conflict_candidates": conflicts,
                "false_parameterization": {
                    "fixed_relations": fixed_count,
                    "parametric_quality": round(quality, 4)}}

    def analyze_sketch_dof(self, sketch_name: str,
                           include_recommendations: bool = True) -> Dict:
        doc, err = self.get_active_doc()
        if err:
            return err
        feature = self._find_sketch_feature(doc, sketch_name)
        if feature is None:
            return self._error("SKETCH_UNDERDEFINED",
                               f"Sketch '{sketch_name}' not found")
        sketch = self._sketch_specific(feature)
        if sketch is None:
            return self._error("SKETCH_UNDERDEFINED",
                               f"Feature '{sketch_name}' is not a sketch")
        data = self._analyze_sketch_objects(
            doc, feature, sketch, include_recommendations)
        return self._result(True, f"DOF analysis: {data['status']}",
                            SwErrors.swSuccess, data)

    @staticmethod
    def _com_out_array(result):
        """Normalize pywin32 return values for COM methods with out arrays."""
        if result is None:
            return []
        payload = result
        if (isinstance(result, tuple) and len(result) == 2 and
                isinstance(result[0], (bool, int))):
            if not bool(result[0]):
                return []
            payload = result[1]
        if payload is None:
            return []
        if isinstance(payload, (list, tuple)):
            return list(payload)
        try:
            return list(payload)
        except TypeError:
            return [payload]

    @staticmethod
    def _sample_nurbs(nurbs, step):
        """Sample an exact non-periodic NURBS representation with de Boor."""
        control_points = [list(point)
                          for point in nurbs.get("control_points", [])]
        knots = [float(value) for value in nurbs.get("knots", [])]
        degree = int(nurbs.get("degree", 3))
        weights = [float(value) for value in nurbs.get("weights", [])]
        if (degree < 1 or len(control_points) <= degree or
                len(knots) != len(control_points) + degree + 1):
            return []
        rational = len(weights) == len(control_points)
        if rational:
            homogeneous = [[point[0] * weight, point[1] * weight, weight]
                           for point, weight in zip(control_points, weights)]
        else:
            homogeneous = [[point[0], point[1], 1.0]
                           for point in control_points]
        parameter_range = nurbs.get("parameter_range") or [
            knots[degree], knots[-degree - 1]]
        start, end = map(float, parameter_range[:2])
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            return []
        length = float(nurbs.get("curve_length", 0.0) or 0.0)
        count = max(len(control_points) * 4, 64)
        if length > 0.0 and step > 0.0:
            count = max(count, int(math.ceil(length / step)) + 1)
        count = min(count, 200000)
        last_index = len(control_points) - 1

        def evaluate(parameter):
            if parameter >= knots[last_index + 1]:
                span = last_index
            else:
                span = bisect.bisect_right(knots, parameter) - 1
                span = max(degree, min(span, last_index))
            work = [homogeneous[span - degree + index][:]
                    for index in range(degree + 1)]
            for level in range(1, degree + 1):
                for index in range(degree, level - 1, -1):
                    knot_index = span - degree + index
                    denominator = (knots[knot_index + degree - level + 1] -
                                   knots[knot_index])
                    alpha = ((parameter - knots[knot_index]) / denominator
                             if abs(denominator) > 1e-15 else 0.0)
                    work[index] = [
                        (1.0 - alpha) * work[index - 1][axis] +
                        alpha * work[index][axis]
                        for axis in range(3)]
            point = work[degree]
            if rational and abs(point[2]) > 1e-15:
                return [point[0] / point[2], point[1] / point[2]]
            return point[:2]

        return [evaluate(start + (end - start) * index / (count - 1))
                for index in range(count)]

    @staticmethod
    def _unpack_packed_double(value):
        """Decode the two signed 32-bit integers stored in a COM double."""
        return struct.unpack("<ii", struct.pack("<d", float(value)))

    def _parse_bulk_spline_params(self, values, unit):
        """Parse the exact all-spline payload returned by GetSplineParams3."""
        raw = [float(value) for value in self._com_out_array(values)]
        records = []
        offset = 0
        while offset < len(raw):
            if len(raw) - offset < 5:
                raise RuntimeError(
                    "ISketch.GetSplineParams3 returned a truncated header")
            dimension, order = self._unpack_packed_double(raw[offset])
            control_count, periodic_value = self._unpack_packed_double(
                raw[offset + 1])
            if dimension not in (3, 4):
                raise RuntimeError(
                    "ISketch.GetSplineParams3 returned an invalid dimension")
            if order < 2 or order > 16 or control_count < order:
                raise RuntimeError(
                    "ISketch.GetSplineParams3 returned invalid spline sizes")
            if periodic_value not in (0, 1):
                raise RuntimeError(
                    "ISketch.GetSplineParams3 returned an invalid periodic flag")
            periodic = bool(periodic_value)
            knot_count = control_count + 1 if periodic else control_count + order
            record_size = 2 + control_count * dimension + knot_count + 3
            if offset + record_size > len(raw):
                raise RuntimeError(
                    "ISketch.GetSplineParams3 returned truncated spline data")
            cursor = offset + 2
            controls = []
            weights = []
            for _ in range(control_count):
                point = raw[cursor:cursor + dimension]
                cursor += dimension
                weight = float(point[3]) if dimension == 4 else None
                if (weight is not None and
                        (not math.isfinite(weight) or weight <= 0.0)):
                    raise RuntimeError(
                        "ISketch.GetSplineParams3 returned invalid rational weights")
                # ForceNonPeriodic returns open rational controls in
                # homogeneous form: (w*x, w*y, w*z, w).
                x_m = point[0] / weight if weight is not None else point[0]
                y_m = point[1] / weight if weight is not None else point[1]
                controls.append([
                    self._units.from_meters(x_m, unit),
                    self._units.from_meters(y_m, unit),
                ])
                if weight is not None:
                    weights.append(weight)
            knots = raw[cursor:cursor + knot_count]
            cursor += knot_count
            cursor += 3
            if periodic:
                raise RuntimeError(
                    "ForceNonPeriodic spline export returned a periodic spline")
            degree = order - 1
            parameter_range = [knots[degree], knots[-degree - 1]]
            if (len(knots) != control_count + degree + 1 or
                    not all(math.isfinite(value) for value in knots) or
                    parameter_range[1] <= parameter_range[0]):
                raise RuntimeError(
                    "ISketch.GetSplineParams3 returned invalid knot data")
            records.append({
                "degree": degree,
                "order": order,
                "dimension": dimension,
                "periodic": False,
                "closed": False,
                "parameterization_periodic": False,
                "parameter_range": parameter_range,
                "knots": knots,
                "control_points": controls,
                "weights": weights,
            })
            offset = cursor
        return records

    @staticmethod
    def _adaptive_nurbs_points(nurbs, chord_tolerance_mm,
                               max_evaluations=200000):
        """Sample exact read-back NURBS locally with a bounded chord error."""
        control_points = [list(point[:2])
                          for point in nurbs.get("control_points", [])]
        knots = [float(value) for value in nurbs.get("knots", [])]
        degree = int(nurbs.get("degree", 3))
        weights = [float(value) for value in nurbs.get("weights", [])]
        if (degree < 1 or len(control_points) <= degree or
                len(knots) != len(control_points) + degree + 1):
            raise RuntimeError("Bulk NURBS payload is not locally evaluable")
        rational = len(weights) == len(control_points)
        if weights and not rational:
            raise RuntimeError("Bulk NURBS rational weights are incomplete")
        homogeneous = (
            [[point[0] * weight, point[1] * weight, weight]
             for point, weight in zip(control_points, weights)]
            if rational else
            [[point[0], point[1], 1.0] for point in control_points])
        parameter_range = nurbs.get("parameter_range") or [
            knots[degree], knots[-degree - 1]]
        parameter_start, parameter_end = map(float, parameter_range[:2])
        tolerance = max(1e-6, float(chord_tolerance_mm))
        max_evaluations = max(64, min(int(max_evaluations), 1000000))
        last_index = len(control_points) - 1
        cache = {}
        accepted_error = 0.0
        max_depth_seen = 0

        def evaluate(parameter):
            key = float(parameter)
            if key in cache:
                return cache[key]
            if len(cache) >= max_evaluations:
                raise RuntimeError(
                    "Local adaptive NURBS evaluation exceeded its point budget")
            if key >= knots[last_index + 1]:
                span = last_index
            else:
                span = bisect.bisect_right(knots, key) - 1
                span = max(degree, min(span, last_index))
            work = [homogeneous[span - degree + index][:]
                    for index in range(degree + 1)]
            for level in range(1, degree + 1):
                for index in range(degree, level - 1, -1):
                    knot_index = span - degree + index
                    denominator = (
                        knots[knot_index + degree - level + 1] -
                        knots[knot_index])
                    alpha = ((key - knots[knot_index]) / denominator
                             if abs(denominator) > 1e-15 else 0.0)
                    work[index] = [
                        (1.0 - alpha) * work[index - 1][axis] +
                        alpha * work[index][axis]
                        for axis in range(3)]
            point = work[degree]
            if rational:
                if abs(point[2]) <= 1e-15:
                    raise RuntimeError("Bulk NURBS evaluated to zero weight")
                result = [point[0] / point[2], point[1] / point[2]]
            else:
                result = point[:2]
            if not all(math.isfinite(value) for value in result):
                raise RuntimeError("Bulk NURBS evaluated to a non-finite point")
            cache[key] = result
            return result

        def chord_distance(point, start, end):
            direction = [end[index] - start[index] for index in range(2)]
            denominator = sum(value * value for value in direction)
            if denominator <= 1e-24:
                return math.dist(point, start)
            projection = sum(
                (point[index] - start[index]) * direction[index]
                for index in range(2)) / denominator
            projection = max(0.0, min(1.0, projection))
            nearest = [start[index] + projection * direction[index]
                       for index in range(2)]
            return math.dist(point, nearest)

        output = []

        def refine(t0, p0, t1, p1, depth):
            nonlocal accepted_error, max_depth_seen
            max_depth_seen = max(max_depth_seen, depth)
            probes_t = [t0 + (t1 - t0) * fraction
                        for fraction in (0.25, 0.5, 0.75)]
            probes = [evaluate(value) for value in probes_t]
            error = max(chord_distance(point, p0, p1) for point in probes)
            if error <= tolerance:
                accepted_error = max(accepted_error, error)
                output.append(p1)
                return
            if depth >= 24:
                raise RuntimeError(
                    "Local adaptive NURBS evaluation exceeded refinement depth")
            refine(t0, p0, probes_t[1], probes[1], depth + 1)
            refine(probes_t[1], probes[1], t1, p1, depth + 1)

        # Seed every non-zero knot span. A uniform parameter grid can miss a
        # narrow, high-curvature span when knots are strongly non-uniform.
        knot_breaks = sorted({parameter_start, parameter_end, *[
            value for value in knots
            if parameter_start < value < parameter_end]})
        subdivisions = max(4, degree + 1)
        parameters = [knot_breaks[0]]
        for span_start, span_end in zip(knot_breaks, knot_breaks[1:]):
            if span_end - span_start <= 1e-15:
                continue
            parameters.extend([
                span_start + (span_end - span_start) * index / subdivisions
                for index in range(1, subdivisions + 1)])
        initial_intervals = len(parameters) - 1
        if initial_intervals < 1:
            raise RuntimeError("Bulk NURBS contains no evaluable knot span")
        grid = [evaluate(value) for value in parameters]
        output.append(grid[0])
        for index in range(initial_intervals):
            refine(parameters[index], grid[index], parameters[index + 1],
                   grid[index + 1], 0)
        return output, {
            "source": "ISketch.GetSplineParams3+local_adaptive_de_boor",
            "chord_tolerance_mm": tolerance,
            "accepted_max_chord_error_mm": accepted_error,
            "evaluation_count": len(cache),
            "output_point_count": len(output),
            "initial_intervals": initial_intervals,
            "max_refinement_depth": max_depth_seen,
        }

    def _bulk_spline_records(self, sketch, unit, chord_tolerance_mm,
                             deadline_monotonic=None):
        """Read every exact sketch spline in one COM call, then sample locally."""
        if (deadline_monotonic is not None and
                time.monotonic() >= float(deadline_monotonic)):
            raise TimeoutError("Bulk spline export deadline expired")
        com_started = time.monotonic()
        values = com_get(sketch, "GetSplineParams3", True, default=None)
        com_elapsed = time.monotonic() - com_started
        if (deadline_monotonic is not None and
                time.monotonic() >= float(deadline_monotonic)):
            raise TimeoutError("Bulk spline export exceeded its deadline")
        records = self._parse_bulk_spline_params(values, unit)
        expected_count = int(com_get(
            sketch, "GetSplineCount", default=len(records)) or 0)
        if len(records) != expected_count:
            raise RuntimeError(
                "Bulk spline count does not match ISketch.GetSplineCount")
        result = []
        local_started = time.monotonic()
        for nurbs in records:
            points, diagnostics = self._adaptive_nurbs_points(
                nurbs, chord_tolerance_mm)
            nurbs["closed"] = math.dist(points[0], points[-1]) <= max(
                1e-6, float(chord_tolerance_mm))
            result.append({"nurbs": nurbs, "evaluation_points": points,
                           "curve_evaluation": diagnostics})
        local_elapsed = time.monotonic() - local_started
        for record in result:
            record["curve_evaluation"].update({
                "bulk_com_read_elapsed_sec": com_elapsed,
                "bulk_local_sampling_elapsed_sec": local_elapsed,
                "bulk_payload_double_count": len(self._com_out_array(values)),
                "bulk_spline_count": len(result),
            })
        return result

    @staticmethod
    def _take_matching_bulk_spline(records, start, end, tolerance_mm):
        """Match an exact bulk NURBS record to a persistent sketch segment."""
        if not records:
            raise RuntimeError("Bulk spline payload contains too few records")
        if start is None or end is None:
            raise RuntimeError("Spline segment endpoints are unavailable")
        ranked = []
        for index, record in enumerate(records):
            points = record["evaluation_points"]
            forward = max(math.dist(start, points[0]),
                          math.dist(end, points[-1]))
            reverse = max(math.dist(start, points[-1]),
                          math.dist(end, points[0]))
            ranked.append((min(forward, reverse), index, reverse < forward))
        error, index, reversed_order = min(ranked)
        if error > max(1e-5, float(tolerance_mm)):
            raise RuntimeError(
                "Bulk NURBS endpoints do not match the sketch segment")
        record = records.pop(index)
        if reversed_order:
            record["evaluation_points"] = list(reversed(
                record["evaluation_points"]))
        record["curve_evaluation"]["endpoint_max_error_mm"] = error
        record["curve_evaluation"]["orientation_reversed"] = reversed_order
        return record

    def _export_segment(self, doc, segment, entity_id, unit, include=None):
        include = include or {}
        kind, kind_code = self._segment_type(segment)
        item = {"id": entity_id, "type": kind,
                "construction": bool(com_get(
                    segment, "ConstructionGeometry", default=False)),
                "persistent_id": self._persist(doc, segment)}
        if include.get("constraint_status", True):
            item["status"] = CONSTRAINED_STATUS.get(int(com_get(
                segment, "GetConstrainedStatus", default=1) or 1), "unknown")
        else:
            # Geometry-only verification does not use solver status. SW2026
            # can block here on the first segment of a composite equation-NURBS.
            item["status"] = "not_evaluated"
        for key, member in (("start", "GetStartPoint2"),
                            ("end", "GetEndPoint2"),
                            ("center", "GetCenterPoint2")):
            point = com_get(segment, member, default=None)
            coords = self._point_coords(point, unit)
            if coords is not None:
                item[key] = coords[:2]
        if kind == "spline" and (
                item.get("start") is None or item.get("end") is None):
            spline_points = self._spline_sketch_points(segment)
            if len(spline_points) >= 2:
                start = self._point_coords(spline_points[0], unit)
                end = self._point_coords(spline_points[-1], unit)
                if start is not None and end is not None:
                    item["start"] = start[:2]
                    item["end"] = end[:2]
        if kind == "arc":
            radius = com_get(segment, "GetRadius", default=None)
            if radius is not None:
                item["radius"] = self._units.from_meters(float(radius), unit)
            rotation_direction = com_get(
                segment, "GetRotationDir", default=None)
            if rotation_direction is None:
                item["clockwise"] = bool(com_get(
                    segment, "IsClockwise", default=False))
            else:
                rotation_direction = int(rotation_direction)
                item["rotation_direction"] = rotation_direction
                item["clockwise"] = rotation_direction < 0
            if item.get("start") and item.get("end") and item.get("center"):
                item["start_angle"] = math.atan2(
                    item["start"][1] - item["center"][1],
                    item["start"][0] - item["center"][0])
                item["end_angle"] = math.atan2(
                    item["end"][1] - item["center"][1],
                    item["end"][0] - item["center"][0])
        elif kind == "ellipse":
            major = self._point_coords(com_get(
                segment, "GetMajorPoint2", default=None), unit)
            minor = self._point_coords(com_get(
                segment, "GetMinorPoint2", default=None), unit)
            if major is not None:
                item["major_point"] = major[:2]
            if minor is not None:
                item["minor_point"] = minor[:2]
            center = item.get("center")
            if center and item.get("major_point") and item.get("minor_point"):
                major_vector = [item["major_point"][axis] - center[axis]
                                for axis in range(2)]
                minor_vector = [item["minor_point"][axis] - center[axis]
                                for axis in range(2)]
                item["major_radius"] = math.hypot(*major_vector)
                item["minor_radius"] = math.hypot(*minor_vector)
                item["rotation_deg"] = math.degrees(math.atan2(
                    major_vector[1], major_vector[0]))
        elif kind == "spline":
            spline_export_mode = str(include.get(
                "spline_export_mode", "nurbs"))
            bulk_records = include.get("_bulk_spline_records")
            if spline_export_mode == "deterministic_source_nurbs":
                source = include.get("source_entity") or {}
                if source.get("type") != "b_spline":
                    raise RuntimeError(
                        "Deterministic validation requires explicit B-spline source data")
                order = int(source.get("order", 4))
                nurbs = {
                    "degree": order - 1,
                    "order": order,
                    "dimension": 3,
                    "periodic": bool(source.get("periodic", False)),
                    "closed": bool(source.get("closed", False)),
                    "parameterization_periodic": bool(
                        source.get("periodic", False)),
                    "knots": [float(value) for value in
                              source.get("knots", [])],
                    "control_points": [list(map(float, point[:2]))
                                       for point in source.get(
                                           "control_points", [])],
                    "weights": [],
                }
                if nurbs["periodic"]:
                    raise RuntimeError(
                        "Deterministic source validation requires bounded open B-splines")
                degree = nurbs["degree"]
                knots = nurbs["knots"]
                if len(knots) != len(nurbs["control_points"]) + degree + 1:
                    raise RuntimeError(
                        "Deterministic B-spline source has an invalid knot vector")
                nurbs["parameter_range"] = [
                    knots[degree], knots[-degree - 1]]
                points, diagnostics = self._adaptive_nurbs_points(
                    nurbs, max(1e-5, float(include.get(
                        "spline_chord_tolerance_mm", 0.025))))
                diagnostics["source"] = (
                    "ISplineParamData deterministic commit parameters + "
                    "local_adaptive_de_boor")
                record = self._take_matching_bulk_spline(
                    [{"nurbs": nurbs, "evaluation_points": points,
                      "curve_evaluation": diagnostics}],
                    item.get("start"), item.get("end"),
                    max(1e-5, float(include.get(
                        "spline_endpoint_tolerance_mm", 0.002))))
                item.update(record)
                if source.get("commit_conversion"):
                    item["commit_conversion"] = str(
                        source["commit_conversion"])
                    item["original_type"] = str(source.get(
                        "original_type", ""))
                curve_typed = None
            elif spline_export_mode == "bulk_exact_nurbs":
                record = self._take_matching_bulk_spline(
                    bulk_records, item.get("start"), item.get("end"),
                    max(0.001, float(include.get(
                        "spline_chord_tolerance_mm", 0.025)) * 2.0))
                item.update(record)
                curve_typed = None
            else:
                curve = com_get(segment, "GetCurve", default=None)
                curve_typed = typed(
                    curve, "ICurve") if curve is not None else None
            if (curve_typed is not None and
                    spline_export_mode == "adaptive_evaluate"):
                try:
                    chord_mm = max(1e-5, float(include.get(
                        "spline_chord_tolerance_mm", 0.025)))
                    source_entity = include.get("source_entity") or {}
                    source_fit_count = len(
                        source_entity.get("fit_points") or
                        source_entity.get("points") or [])
                    points, diagnostics = self._adaptive_curve_points(
                        curve_typed, unit, chord_mm,
                        deadline_monotonic=include.get("deadline_monotonic"),
                        source_fit_count=source_fit_count,
                        max_evaluations=int(include.get(
                            "spline_max_evaluations", 2048)))
                    item["evaluation_points"] = points
                    item["curve_evaluation"] = diagnostics
                except TimeoutError:
                    raise
                except Exception as exc:
                    item["curve_evaluation_error"] = str(exc)
            elif curve_typed is not None and spline_export_mode == "tessellation":
                try:
                    chord_mm = max(1e-5, float(include.get(
                        "spline_chord_tolerance_mm", 0.025)))
                    length_mm = max(0.0, float(include.get(
                        "spline_length_tolerance_mm", 0.0)))
                    start = list(item.get("start") or [0.0, 0.0])[:2]
                    end = list(item.get("end") or start)[:2]
                    start_m = [self._units.to_meters(value, unit)
                               for value in start] + [0.0]
                    end_m = [self._units.to_meters(value, unit)
                             for value in end] + [0.0]
                    values = list(curve_typed.GetTessPts(
                        self._units.to_meters(chord_mm, unit),
                        self._units.to_meters(length_mm, unit),
                        start_m, end_m) or [])
                    points = [
                        [self._units.from_meters(float(values[index]), unit),
                         self._units.from_meters(
                             float(values[index + 1]), unit)]
                        for index in range(0, len(values) - 2, 3)]
                    if len(points) < 2:
                        raise RuntimeError(
                            "SolidWorks returned fewer than two tessellation points")
                    item["tessellation_points"] = points
                    item["tessellation"] = {
                        "source": "ICurve.GetTessPts",
                        "chord_tolerance_mm": chord_mm,
                        "length_tolerance_mm": length_mm,
                        "point_count": len(points),
                    }
                except Exception as exc:
                    item["tessellation_error"] = str(exc)
            elif curve_typed is not None:
                try:
                    end_params = com_get(
                        curve_typed, "GetEndParams", default=None)
                    if (isinstance(end_params, tuple) and
                            len(end_params) >= 5):
                        parameter_start = float(end_params[1])
                        parameter_end = float(end_params[2])
                        is_closed = bool(end_params[3])
                        is_periodic = bool(end_params[4])
                    else:
                        parameter_start = parameter_end = None
                        is_closed = is_periodic = False
                    # Force a non-periodic parameterization so the exported
                    # knots and controls form a standard, directly evaluable
                    # NURBS while preserving the curve's original topology.
                    bcurve = curve_typed.GetBCurveParams5(
                        False, False, True, is_closed)
                    dimension = int(com_get(
                        bcurve, "Dimension", default=3))
                    order = int(com_get(bcurve, "Order", default=4))
                    control_values = self._com_out_array(com_get(
                        bcurve, "GetControlPoints", default=None))
                    knot_values = self._com_out_array(com_get(
                        bcurve, "GetKnotPoints", default=None))
                    control_count = int(com_get(
                        bcurve, "ControlPointsCount", default=0) or 0)
                    available_count = (len(control_values) // dimension
                                       if dimension > 0 else 0)
                    control_count = min(control_count or available_count,
                                        available_count)
                    controls, weights = [], []
                    for index in range(control_count):
                        offset = index * dimension
                        controls.append([
                            self._units.from_meters(
                                float(control_values[offset]), unit),
                            self._units.from_meters(
                                float(control_values[offset + 1]), unit),
                        ])
                        if dimension >= 4:
                            weights.append(float(
                                control_values[offset + dimension - 1]))
                    curve_length_m = (curve_typed.GetLength3(
                        parameter_start, parameter_end)
                        if parameter_start is not None else None)
                    item["nurbs"] = {
                        "degree": max(1, order - 1),
                        "order": order,
                        "dimension": dimension,
                        "periodic": is_periodic,
                        "closed": is_closed,
                        "parameterization_periodic": bool(com_get(
                            bcurve, "Periodic", default=False)),
                        "parameter_range": ([parameter_start, parameter_end]
                                            if parameter_start is not None
                                            else None),
                        "curve_length": (self._units.from_meters(
                            float(curve_length_m), unit)
                            if curve_length_m is not None else None),
                        "knots": [float(value) for value in knot_values],
                        "control_points": controls,
                        "weights": weights,
                    }
                except Exception:
                    pass
            points = None
            if include.get("spline_fit_points", True):
                points = (com_get(segment, "GetSplinePoints", default=None) or
                          com_get(segment, "GetInterpolationPoints", default=None))
            if points:
                values = list(points)
                if values and not isinstance(values[0], (float, int)):
                    item["fit_points"] = [self._point_coords(p, unit)[:2]
                                          for p in values]
                else:
                    item["fit_points"] = [
                        [self._units.from_meters(float(values[i]), unit),
                         self._units.from_meters(float(values[i + 1]), unit)]
                        for i in range(0, len(values) - 2, 3)]
        elif kind == "ellipse":
            for key, member in (("major_point", "GetMajorPoint2"),
                                ("minor_point", "GetMinorPoint2")):
                point = com_get(segment, member, default=None)
                coords = self._point_coords(point, unit)
                if coords is not None:
                    item[key] = coords[:2]
            if item.get("center") and item.get("major_point"):
                item["major_radius"] = math.dist(
                    item["center"], item["major_point"])
            if item.get("center") and item.get("minor_point"):
                item["minor_radius"] = math.dist(
                    item["center"], item["minor_point"])
        return item

    def _adaptive_curve_points(self, curve, unit, chord_tolerance_mm,
                               deadline_monotonic=None, source_fit_count=0,
                               max_evaluations=2048):
        """Evaluate a SolidWorks curve with bounded adaptive chord control."""
        end_params = com_get(curve, "GetEndParams", default=None)
        if not isinstance(end_params, tuple) or len(end_params) < 5:
            raise RuntimeError("ICurve.GetEndParams returned an invalid payload")
        parameter_start = float(end_params[1])
        parameter_end = float(end_params[2])
        if (not math.isfinite(parameter_start) or
                not math.isfinite(parameter_end) or
                parameter_start == parameter_end):
            raise RuntimeError("ICurve parameter range is invalid")
        max_evaluations = max(32, min(int(max_evaluations), 16384))
        initial_intervals = max(
            4, min(64, int(math.ceil(max(1, source_fit_count) / 8.0))))
        cache = {}
        accepted_error = 0.0
        max_depth_seen = 0

        def deadline_check():
            if (deadline_monotonic is not None and
                    time.monotonic() >= float(deadline_monotonic)):
                raise TimeoutError(
                    "Adaptive curve evaluation exceeded its deadline")

        def evaluate(parameter):
            deadline_check()
            key = float(parameter)
            if key in cache:
                return cache[key]
            if len(cache) >= max_evaluations:
                raise RuntimeError(
                    "Adaptive curve evaluation exceeded its point budget")
            values = list(curve.Evaluate2(key, 0) or [])
            deadline_check()
            if len(values) < 3:
                raise RuntimeError("ICurve.Evaluate2 returned no point")
            point = [self._units.from_meters(float(values[index]), unit)
                     for index in range(3)]
            if not all(math.isfinite(value) for value in point):
                raise RuntimeError("ICurve.Evaluate2 returned a non-finite point")
            cache[key] = point
            return point

        def chord_distance(point, start, end):
            direction = [end[index] - start[index] for index in range(3)]
            denominator = sum(value * value for value in direction)
            if denominator <= 1e-24:
                return math.dist(point, start)
            projection = sum(
                (point[index] - start[index]) * direction[index]
                for index in range(3)) / denominator
            projection = max(0.0, min(1.0, projection))
            nearest = [start[index] + projection * direction[index]
                       for index in range(3)]
            return math.dist(point, nearest)

        output = []

        def refine(t0, p0, t1, p1, depth):
            nonlocal accepted_error, max_depth_seen
            max_depth_seen = max(max_depth_seen, depth)
            quarter_parameters = [
                t0 + (t1 - t0) * fraction
                for fraction in (0.25, 0.5, 0.75)]
            quarter_points = [evaluate(value)
                              for value in quarter_parameters]
            error = max(chord_distance(point, p0, p1)
                        for point in quarter_points)
            if error <= float(chord_tolerance_mm):
                accepted_error = max(accepted_error, error)
                output.append(p1)
                return
            if depth >= 20:
                raise RuntimeError(
                    "Adaptive curve evaluation exceeded its refinement depth")
            middle_parameter = quarter_parameters[1]
            middle_point = quarter_points[1]
            refine(t0, p0, middle_parameter, middle_point, depth + 1)
            refine(middle_parameter, middle_point, t1, p1, depth + 1)

        parameters = [
            parameter_start + (parameter_end - parameter_start) *
            index / initial_intervals
            for index in range(initial_intervals + 1)]
        grid = [evaluate(value) for value in parameters]
        output.append(grid[0])
        for index in range(initial_intervals):
            refine(parameters[index], grid[index], parameters[index + 1],
                   grid[index + 1], 0)
        if len(output) < 2:
            raise RuntimeError("Adaptive curve evaluation produced no polyline")
        return [[point[0], point[1]] for point in output], {
            "source": "ICurve.GetEndParams+Evaluate2",
            "chord_tolerance_mm": float(chord_tolerance_mm),
            "accepted_max_chord_error_mm": accepted_error,
            "initial_intervals": initial_intervals,
            "evaluation_count": len(cache),
            "output_point_count": len(output),
            "max_refinement_depth": max_depth_seen,
            "source_fit_point_count": int(source_fit_count),
        }

    @staticmethod
    def _contours_from_entities(entities, tolerance=1e-6):
        entities_by_id = {entity["id"]: entity for entity in entities}
        endpoint_to_entities = defaultdict(list)
        for entity in entities:
            if entity.get("construction"):
                continue
            if entity["type"] == "arc" and not entity.get("start"):
                continue
            for key in ("start", "end"):
                point = entity.get(key)
                if point:
                    endpoint_to_entities[tuple(round(v / tolerance)
                                               for v in point)].append(entity["id"])
        adjacency = defaultdict(set)
        for ids in endpoint_to_entities.values():
            for left in ids:
                adjacency[left].update(right for right in ids if right != left)
        seen, contours = set(), []
        for entity in entities:
            entity_id = entity["id"]
            if entity_id in seen or entity.get("construction"):
                continue
            stack, component = [entity_id], []
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                component.append(current)
                stack.extend(adjacency[current] - seen)
            member = entities_by_id[component[0]]
            start, end = member.get("start"), member.get("end")
            coincident_ends = bool(
                start and end and len(start) >= 2 and len(end) >= 2 and
                math.dist(start[:2], end[:2]) <= tolerance)
            intrinsic_closed = (len(component) == 1 and bool(
                (member.get("nurbs") or {}).get("closed", False) or
                member.get("type") in {"circle", "ellipse"} or
                (member.get("type") == "arc" and
                 (coincident_ends or not start or not end))))
            degree_closed = intrinsic_closed or (bool(component) and all(
                len(adjacency[member]) >= 1 for member in component))
            contours.append({"id": f"contour_{len(contours) + 1:03d}",
                             "entities": component, "closed": degree_closed})
        return contours

    def export_sketch_geometry(self, sketch_name: str,
                               coordinate_system: str = "sketch_2d",
                               unit: str = None, include: Dict[str, Any] = None,
                               output: Dict[str, Any] = None) -> Dict:
        unit = unit or self._units.default_unit.value
        include = include or {}
        output = output or {"mode": "summary"}
        doc, err = self.get_active_doc()
        if err:
            return err
        feature = self._find_sketch_feature(doc, sketch_name)
        sketch = self._sketch_specific(feature) if feature else None
        if sketch is None:
            return self._error("SKETCH_UNDERDEFINED",
                               f"Sketch '{sketch_name}' not found")
        registered = self._runtime.get_entities(
            self._document_key(doc), sketch_name)
        pid_to_id = {record.get("persistent_id"): entity_id
                     for entity_id, record in registered.items()
                     if record.get("persistent_id")}
        entities = []
        deadline = include.get("deadline_monotonic")
        bulk_spline_records = None
        if str(include.get("spline_export_mode", "")) == "bulk_exact_nurbs":
            chord_mm = max(1e-5, float(include.get(
                "spline_chord_tolerance_mm", 0.025)))
            bulk_spline_records = self._bulk_spline_records(
                sketch, unit, chord_mm, deadline_monotonic=deadline)
        for index, segment in enumerate(self._sketch_segments(sketch)):
            if deadline is not None and time.monotonic() >= float(deadline):
                raise TimeoutError(
                    "Sketch export exceeded the post-COM validation deadline")
            pid = self._persist(doc, segment)
            entity_id = pid_to_id.get(pid, f"seg_{index + 1:03d}")
            segment_include = {
                **include,
                "_bulk_spline_records": bulk_spline_records,
                "source_entity": (
                    registered.get(entity_id, {}).get("source") or {}),
            }
            item = self._export_segment(
                doc, segment, entity_id, unit, include=segment_include)
            if include.get("construction", True) or not item["construction"]:
                entities.append(item)
        if bulk_spline_records:
            raise RuntimeError(
                "Bulk spline payload contains unmatched records")
        contours = self._contours_from_entities(entities) if include.get(
            "topology", True) else []
        transform = com_get(sketch, "ModelToSketchTransform", default=None)
        transform_data = com_get(transform, "ArrayData", default=None) if transform else None
        parents = com_get(feature, "GetParents", default=None) or []
        plane_name = next((
            str(com_get(parent, "Name", default=""))
            for parent in parents
            if str(com_get(parent, "GetTypeName2", default="")).lower() ==
            "refplane"), None)
        configuration_manager = com_get(
            doc, "ConfigurationManager", default=None)
        active_configuration = com_get(
            configuration_manager, "ActiveConfiguration", default=None)
        configuration_name = com_get(
            active_configuration, "Name", default=None)
        constraint_status_evaluation_skipped = not include.get(
            "constraint_status", True)
        if constraint_status_evaluation_skipped:
            status = "not_evaluated"
        else:
            status, _ = self._sketch_status(sketch)
        all_points = [point for entity in entities for key in
                      ("start", "end", "center")
                      for point in [entity.get(key)] if point]
        all_points.extend(point for entity in entities
                          for point in entity.get("fit_points", []))
        all_points.extend(point for entity in entities
                          for point in entity.get(
                              "tessellation_points", []))
        all_points.extend(point for entity in entities
                          for point in entity.get(
                              "evaluation_points", []))
        for entity in entities:
            center = entity.get("center")
            if not center:
                continue
            if entity.get("type") == "arc" and float(
                    entity.get("radius", 0.0) or 0.0) > 0.0:
                radius = float(entity["radius"])
                start, end = entity.get("start"), entity.get("end")
                full_circle = bool(
                    not start or not end or
                    math.dist(start[:2], end[:2]) <= 1e-9)
                start_angle = float(entity.get(
                    "start_angle", math.atan2(
                        start[1] - center[1], start[0] - center[0])
                    if start else 0.0))
                end_angle = float(entity.get(
                    "end_angle", math.atan2(
                        end[1] - center[1], end[0] - center[0])
                    if end else start_angle))
                all_points.extend(self._directed_arc_extrema(
                    center, radius, start_angle, end_angle,
                    clockwise=bool(entity.get("clockwise", False)),
                    full_circle=full_circle))
            elif entity.get("type") == "ellipse":
                major_point = entity.get("major_point")
                minor_point = entity.get("minor_point")
                if major_point and minor_point:
                    major = [major_point[axis] - center[axis]
                             for axis in range(2)]
                    minor = [minor_point[axis] - center[axis]
                             for axis in range(2)]
                    extent = [math.hypot(major[axis], minor[axis])
                              for axis in range(2)]
                    all_points.extend([
                        [center[0] - extent[0], center[1] - extent[1]],
                        [center[0] + extent[0], center[1] + extent[1]]])
        for entity in entities:
            nurbs = entity.get("nurbs") or {}
            if not nurbs.get("control_points"):
                continue
            length = float(nurbs.get("curve_length", 0.0) or 0.0)
            sample_step = max(length / 4096.0, 0.01)
            all_points.extend(self._sample_nurbs(nurbs, sample_step))
        bbox = ({"min": [min(p[i] for p in all_points) for i in range(2)],
                 "max": [max(p[i] for p in all_points) for i in range(2)]}
                if all_points else None)
        relations = []
        if include.get("relations", True):
            for index, relation in enumerate(self._sketch_relations(sketch)):
                relations.append({
                    "id": f"rel_{index + 1:03d}",
                    "type": com_get(relation, "GetRelationType", default=None),
                    "status": com_get(relation, "GetStatus", default=None)})
        dimensions = []
        if include.get("dimensions", True):
            display = com_get(feature, "GetFirstDisplayDimension", default=None)
            guard = 0
            while display is not None and guard < 10000:
                guard += 1
                dimension = com_get(display, "GetDimension2", 0, default=None)
                if dimension is not None:
                    value_m = com_get(dimension, "SystemValue", default=None)
                    dimensions.append({
                        "name": self._dimension_name(display, dimension),
                        "system_value_m": float(value_m) if value_m is not None else None,
                        "value": (self._units.from_meters(float(value_m), unit)
                                  if value_m is not None else None),
                        "unit": unit,
                        "driven_state": com_get(
                            dimension, "DrivenState", default=None),
                    })
                display = com_get(feature, "GetNextDisplayDimension", display,
                                  default=None)
        equations = []
        if include.get("equations", True):
            manager_eq = com_get(doc, "GetEquationMgr", default=None)
            if manager_eq is not None:
                count = int(com_get(manager_eq, "GetCount", default=0) or 0)
                for index in range(count):
                    equation = com_get(manager_eq, "Equation", index, default=None)
                    if equation and (f"@{sketch_name}" in str(equation) or
                                     include.get("all_equations", False)):
                        equations.append({"index": index,
                                          "equation": str(equation),
                                          "value": com_get(
                                              manager_eq, "Value", index,
                                              default=None),
                                          "global_variable": bool(com_get(
                                              manager_eq, "GlobalVariable", index,
                                              default=False))})
        payload = {
            "schema": "solidworks-mcp/sketch-geometry/v1",
            "document": {"title": self._get_doc_title(doc),
                         "path": self._get_doc_path(doc),
                         "configuration": configuration_name},
            "sketch": {"name": sketch_name, "unit": unit,
                       "plane": plane_name,
                       "coordinate_system": coordinate_system,
                       "model_to_sketch_transform": list(transform_data or []),
                       "bbox": bbox, "constraint_status": status,
                       "constraint_status_evaluation_skipped":
                           constraint_status_evaluation_skipped,
                       "y_axis": "up"},
            "entities": entities, "contours": contours,
            "relations": relations, "dimensions": dimensions,
            "equations": equations}
        precision = int(output.get("numeric_precision", 6))
        payload = _round_floats(payload, precision)
        output_path = output.get("path")
        if output_path:
            atomic_json_write(output_path, payload)
        summary = {"schema": payload["schema"], "sketch": payload["sketch"],
                   "entity_count": len(entities),
                   "entity_types": dict(_count_by(entities, "type")),
                   "contour_count": len(contours),
                   "closed_contours": sum(1 for c in contours if c["closed"]),
                   "relation_count": len(relations),
                   "dimension_count": len(dimensions),
                   "equation_count": len(equations), "path": output_path}
        if output.get("mode") == "inline":
            summary["geometry"] = payload
        return self._result(True, f"Exported sketch geometry '{sketch_name}'",
                            SwErrors.swSuccess, summary)

    def _load_geometry_payload(self, sketch_name, unit, include=None):
        include = {
            "construction": True, "relations": True, "dimensions": True,
            "equations": True, "topology": True, **(include or {}),
        }
        result = self.export_sketch_geometry(
            sketch_name, unit=unit,
            include=include,
            output={"mode": "inline", "numeric_precision": 9})
        if not result.get("success"):
            return result, None
        return result, result["data"]["geometry"]

    def render_sketch_svg(self, sketch_names: List[str], path: str,
                          view: Dict[str, Any] = None,
                          style: Dict[str, Any] = None) -> Dict:
        view, style = view or {}, style or {}
        unit = view.get("unit") or self._units.default_unit.value
        layers, all_points = [], []
        for sketch_name in sketch_names or []:
            result, geometry = self._load_geometry_payload(sketch_name, unit)
            if geometry is None:
                return result
            layers.append(geometry)
            bbox = geometry["sketch"].get("bbox")
            if bbox:
                all_points.extend([bbox["min"], bbox["max"]])
        if not layers or not all_points:
            return self._error("SKETCH_OPEN_CONTOUR", "No drawable geometry")
        padding = float(view.get("padding", 5.0))
        min_x, min_y = min(p[0] for p in all_points), min(p[1] for p in all_points)
        max_x, max_y = max(p[0] for p in all_points), max(p[1] for p in all_points)
        min_x -= padding; min_y -= padding; max_x += padding; max_y += padding
        width, height = max(max_x - min_x, 1e-9), max(max_y - min_y, 1e-9)
        svg = ET.Element("svg", xmlns="http://www.w3.org/2000/svg",
                         version="1.1",
                         viewBox=f"{min_x} {min_y} {width} {height}")
        metadata = ET.SubElement(svg, "metadata")
        metadata.text = json.dumps({
            "unit": unit,
            "document": layers[0].get("document") or {},
            "sketches": [{
                "name": geometry["sketch"].get("name"),
                "plane": geometry["sketch"].get("plane"),
                "model_to_sketch_transform": geometry["sketch"].get(
                    "model_to_sketch_transform") or [],
                "bbox": geometry["sketch"].get("bbox"),
                "constraint_status": geometry["sketch"].get(
                    "constraint_status"),
            } for geometry in layers],
            "mcp_version": "6.5.31", "invert_y_for_display": bool(
                view.get("invert_y_for_display", True))})
        root_group = ET.SubElement(svg, "g")
        if view.get("invert_y_for_display", True):
            root_group.set("transform", f"translate(0 {min_y + max_y}) scale(1 -1)")
        colors = {"normal": style.get("normal", "#00d4ff"),
                  "construction": style.get("construction", "#777777"),
                  "under_defined": style.get("under_defined", "#3366ff"),
                  "over_defined": style.get("over_defined", "#ff3333")}
        for geometry in layers:
            layer = ET.SubElement(root_group, "g", id=_xml_id(
                geometry["sketch"]["name"]))
            for entity in geometry["entities"]:
                color = colors["construction"] if entity.get(
                    "construction") else colors.get(entity.get("status"), colors["normal"])
                attrs = {"id": _xml_id(entity["id"]), "fill": "none",
                         "stroke": color,
                         "stroke-width": str(style.get("stroke_width", 1)),
                         "vector-effect": "non-scaling-stroke"}
                if entity.get("construction"):
                    attrs["stroke-dasharray"] = "4 3"
                if entity["type"] == "line" and entity.get("start"):
                    attrs.update({"x1": str(entity["start"][0]),
                                  "y1": str(entity["start"][1]),
                                  "x2": str(entity["end"][0]),
                                  "y2": str(entity["end"][1])})
                    ET.SubElement(layer, "line", attrs)
                elif entity["type"] == "arc" and entity.get("center"):
                    if entity.get("start") and entity.get("end") and (
                            entity["start"] != entity["end"]):
                        sa, ea = entity.get("start_angle", 0), entity.get("end_angle", 0)
                        delta = (ea - sa) % (2 * math.pi)
                        if entity.get("clockwise"):
                            delta = (sa - ea) % (2 * math.pi)
                        attrs["d"] = (f"M {entity['start'][0]} {entity['start'][1]} "
                                      f"A {entity['radius']} {entity['radius']} 0 "
                                      f"{1 if delta > math.pi else 0} "
                                      f"{0 if entity.get('clockwise') else 1} "
                                      f"{entity['end'][0]} {entity['end'][1]}")
                        ET.SubElement(layer, "path", attrs)
                    else:
                        attrs.update({"cx": str(entity["center"][0]),
                                      "cy": str(entity["center"][1]),
                                      "r": str(entity.get("radius", 0))})
                        ET.SubElement(layer, "circle", attrs)
                elif entity["type"] == "ellipse" and entity.get("center"):
                    major_radius = float(entity.get("major_radius", 0.0))
                    minor_radius = float(entity.get("minor_radius", 0.0))
                    if major_radius > 0.0 and minor_radius > 0.0:
                        attrs.update({"cx": str(entity["center"][0]),
                                      "cy": str(entity["center"][1]),
                                      "rx": str(major_radius),
                                      "ry": str(minor_radius)})
                        rotation = float(entity.get("rotation_deg", 0.0))
                        if abs(rotation) > 1e-12:
                            attrs["transform"] = (
                                f"rotate({rotation} {entity['center'][0]} "
                                f"{entity['center'][1]})")
                        ET.SubElement(layer, "ellipse", attrs)
                elif entity["type"] in {"spline", "b_spline"}:
                    points = (entity.get("fit_points") or
                              entity.get("evaluation_points") or
                              self._sample_nurbs(
                                  entity.get("nurbs") or {},
                                  float(view.get("spline_sample_step", 0.1))))
                    if points:
                        attrs["d"] = "M " + " L ".join(
                            f"{p[0]} {p[1]}" for p in points)
                        if (entity.get("nurbs") or {}).get("closed"):
                            attrs["d"] += " Z"
                        ET.SubElement(layer, "path", attrs)
        tree = ET.ElementTree(svg)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tree.write(path, encoding="utf-8", xml_declaration=True)
        ET.parse(path)
        self._runtime.increment("verification_artifacts")
        return self._result(True, f"SVG rendered: {path}", SwErrors.swSuccess,
                            {"path": os.path.abspath(path), "unit": unit,
                             "view_box": [min_x, min_y, width, height],
                             "sketches": sketch_names,
                             "size_bytes": os.path.getsize(path)})

    @staticmethod
    def _arc_three_point_parameters(entity):
        """Return center, radius, start angle, direction and sweep for a 3-point arc."""
        start = entity.get("start")
        end = entity.get("end")
        through = entity.get("point", entity.get("mid"))
        if not start or not end or not through:
            return None
        x1, y1 = map(float, start[:2])
        x2, y2 = map(float, through[:2])
        x3, y3 = map(float, end[:2])
        determinant = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) +
                             x3 * (y1 - y2))
        if abs(determinant) <= 1e-12:
            return None
        ux = (((x1 * x1 + y1 * y1) * (y2 - y3) +
               (x2 * x2 + y2 * y2) * (y3 - y1) +
               (x3 * x3 + y3 * y3) * (y1 - y2)) / determinant)
        uy = (((x1 * x1 + y1 * y1) * (x3 - x2) +
               (x2 * x2 + y2 * y2) * (x1 - x3) +
               (x3 * x3 + y3 * y3) * (x2 - x1)) / determinant)
        angles = [math.atan2(y - uy, x - ux)
                  for x, y in ((x1, y1), (x2, y2), (x3, y3))]
        ccw_sweep = (angles[2] - angles[0]) % (2.0 * math.pi)
        ccw_to_mid = (angles[1] - angles[0]) % (2.0 * math.pi)
        if ccw_to_mid <= ccw_sweep + 1e-12:
            direction, sweep = 1, ccw_sweep
        else:
            direction = -1
            sweep = (angles[0] - angles[2]) % (2.0 * math.pi)
        return [ux, uy], math.hypot(x1 - ux, y1 - uy), angles[0], direction, sweep

    def _sample_geometry_entities(self, geometry, step):
        """Sample supported CAD primitives without discarding entity ownership."""
        safe_step = max(1e-9, float(step))
        sampled = []
        for entity in geometry.get("entities", []):
            if entity.get("construction"):
                continue
            entity_type = str(entity.get("type", "")).lower()
            points = []
            if entity_type in {"line", "centerline"} and entity.get("start"):
                a, b = entity["start"], entity["end"]
                length = math.dist(a[:2], b[:2])
                count = max(2, int(math.ceil(length / safe_step)) + 1)
                points = [[a[0] + (b[0] - a[0]) * i / (count - 1),
                           a[1] + (b[1] - a[1]) * i / (count - 1)]
                          for i in range(count)]
            elif entity_type in {"arc_3pt", "three_point_arc"}:
                parameters = self._arc_three_point_parameters(entity)
                if parameters:
                    center, radius, start, direction, delta = parameters
                    count = max(8, int(math.ceil(radius * delta /
                                                  safe_step)) + 1)
                    points = [[center[0] + radius * math.cos(
                                   start + direction * delta * i / (count - 1)),
                               center[1] + radius * math.sin(
                                   start + direction * delta * i / (count - 1))]
                              for i in range(count)]
            elif entity_type in {"arc", "circle"} and entity.get("center"):
                radius = float(entity.get("radius", 0.0))
                start_point, end_point = entity.get("start"), entity.get("end")
                full_circle = (entity_type == "circle" or not start_point or
                               not end_point or math.dist(
                                   start_point[:2], end_point[:2]) <= 1e-9)
                if full_circle:
                    start, delta, direction = 0.0, 2.0 * math.pi, 1
                else:
                    center = entity["center"]
                    start = float(entity.get(
                        "start_angle", math.atan2(
                            start_point[1] - center[1],
                            start_point[0] - center[0])))
                    end = float(entity.get(
                        "end_angle", math.atan2(
                            end_point[1] - center[1],
                            end_point[0] - center[0])))
                    delta = ((start - end) if entity.get("clockwise") else
                             (end - start)) % (2.0 * math.pi)
                    direction = -1 if entity.get("clockwise") else 1
                if radius > 0.0:
                    count = max(16 if full_circle else 8,
                                int(math.ceil(radius * delta / safe_step)) + 1)
                    points = [[entity["center"][0] + radius * math.cos(
                                   start + direction * delta * i / (count - 1)),
                               entity["center"][1] + radius * math.sin(
                                   start + direction * delta * i / (count - 1))]
                              for i in range(count)]
            elif entity_type == "ellipse" and entity.get("center"):
                center = list(map(float, entity["center"][:2]))
                if entity.get("major_point") and entity.get("minor_point"):
                    major = [float(entity["major_point"][axis]) - center[axis]
                             for axis in range(2)]
                    minor = [float(entity["minor_point"][axis]) - center[axis]
                             for axis in range(2)]
                else:
                    angle = math.radians(float(entity.get("rotation_deg", 0.0)))
                    major_radius = float(entity.get("major_radius", 0.0))
                    minor_radius = float(entity.get("minor_radius", 0.0))
                    major = [major_radius * math.cos(angle),
                             major_radius * math.sin(angle)]
                    minor = [-minor_radius * math.sin(angle),
                             minor_radius * math.cos(angle)]
                major_radius, minor_radius = math.hypot(*major), math.hypot(*minor)
                if major_radius > 0.0 and minor_radius > 0.0:
                    start = float(entity.get("start_parameter", 0.0))
                    end = float(entity.get("end_parameter", 2.0 * math.pi))
                    delta = ((start - end) if entity.get("clockwise") else
                             (end - start)) % (2.0 * math.pi)
                    if delta <= 1e-12:
                        delta = 2.0 * math.pi
                    direction = -1 if entity.get("clockwise") else 1
                    circumference = math.pi * (3.0 * (major_radius + minor_radius) -
                        math.sqrt((3.0 * major_radius + minor_radius) *
                                  (major_radius + 3.0 * minor_radius)))
                    count = max(24, int(math.ceil(
                        circumference * delta / (2.0 * math.pi) / safe_step)) + 1)
                    points = [[center[0] + major[0] * math.cos(parameter) +
                               minor[0] * math.sin(parameter),
                               center[1] + major[1] * math.cos(parameter) +
                               minor[1] * math.sin(parameter)]
                              for parameter in (start + direction * delta * i /
                                                (count - 1)
                                                for i in range(count))]
            elif entity_type in {"spline", "b_spline"}:
                points = (entity.get("evaluation_points") or
                          entity.get("tessellation_points") or [])
                nurbs = dict(entity.get("nurbs") or {})
                if entity_type == "b_spline":
                    order = int(entity.get("order", 4))
                    nurbs.update({
                        "degree": order - 1,
                        "order": order,
                        "control_points": entity.get("control_points") or [],
                        "knots": entity.get("knots") or [],
                        "weights": entity.get("weights") or [],
                        "periodic": bool(entity.get("periodic", False)),
                        "closed": bool(entity.get("closed", False)),
                    })
                if not points and nurbs.get("control_points"):
                    points = self._sample_nurbs(nurbs, safe_step)
                if not points:
                    points = (entity.get("fit_points") or
                              entity.get("points") or [])
            points = [[float(point[0]), float(point[1])]
                      for point in points if len(point) >= 2 and
                      math.isfinite(float(point[0])) and
                      math.isfinite(float(point[1]))]
            if points:
                sampled.append({"entity_id": str(entity.get("id", "")),
                                "type": entity_type, "points": points})
        return sampled

    def _sample_geometry(self, geometry, step):
        samples, owners = [], []
        for record in self._sample_geometry_entities(geometry, step):
            samples.extend(record["points"])
            owners.extend([record["entity_id"]] * len(record["points"]))
        return samples, owners

    def compare_sketches(self, reference_sketch: str, candidate_sketch: str,
                         tolerance: Dict[str, float] = None,
                         unit: str = None, report_path: str = None,
                         reference_geometry: Dict[str, Any] = None,
                         candidate_geometry: Dict[str, Any] = None) -> Dict:
        unit = unit or self._units.default_unit.value
        tolerance = tolerance or {}
        if reference_geometry is None and candidate_geometry is None:
            ref_result, reference = self._load_geometry_payload(
                reference_sketch, unit)
            if reference is None:
                return ref_result
            cand_result, candidate = self._load_geometry_payload(
                candidate_sketch, unit)
            if candidate is None:
                return cand_result
        elif isinstance(reference_geometry, dict) and isinstance(
                candidate_geometry, dict):
            reference, candidate = reference_geometry, candidate_geometry
        else:
            return self._error(
                "INVALID_PLAN",
                "reference_geometry and candidate_geometry must be provided together")
        # Scientific native libraries load only after both COM exports. The
        # MCP server dispatches this section in an isolated worker.
        import numpy as np
        from scipy.spatial import cKDTree
        step = max(1e-5, float(tolerance.get("sample_step", 0.05)))
        ref_points, _ = self._sample_geometry(reference, step)
        cand_points, owners = self._sample_geometry(candidate, step)
        if not ref_points or not cand_points:
            return self._error("SKETCH_OPEN_CONTOUR",
                               "Both sketches must contain sampled geometry")
        ref_array, cand_array = np.asarray(ref_points), np.asarray(cand_points)
        cand_to_ref, _ = cKDTree(ref_array).query(cand_array)
        ref_to_cand, ref_indices = cKDTree(cand_array).query(ref_array)
        symmetric = np.concatenate([cand_to_ref, ref_to_cand])
        worst_index = int(np.argmax(cand_to_ref))
        by_entity = defaultdict(list)
        for owner, distance in zip(owners, cand_to_ref):
            by_entity[owner].append(float(distance))
        entity_errors = sorted(({
            "entity_id": owner, "max": max(values),
            "mean": sum(values) / len(values)} for owner, values in by_entity.items()),
            key=lambda item: item["max"], reverse=True)
        metrics = {
            "mean": float(np.mean(symmetric)),
            "p95": float(np.percentile(symmetric, 95)),
            "max": float(np.max(symmetric)),
            "hausdorff": float(max(np.max(cand_to_ref), np.max(ref_to_cand))),
            "worst_candidate_entity": owners[worst_index],
        }
        passed = all(metrics[key] <= float(tolerance.get(
            f"{key}_mm", float("inf"))) for key in ("mean", "p95", "max"))
        payload = {"schema": "solidworks-mcp/sketch-comparison/v1",
                   "reference": reference_sketch, "candidate": candidate_sketch,
                   "unit": unit, "metrics": metrics,
                   "entity_errors": entity_errors[:20], "pass": passed}
        if report_path:
            atomic_json_write(report_path, payload)
        result = self._result(
            passed, f"Sketch comparison {'PASS' if passed else 'FAIL'}",
            SwErrors.swSuccess if passed else SwErrors.swSketchError,
            {**payload, "report": report_path})
        if not passed:
            result["data"]["error"] = structured_error(
                "REFERENCE_MISMATCH",
                "Candidate sketch does not satisfy reference tolerances",
                conflicting_entities=[
                    item["entity_id"] for item in entity_errors[:20]
                    if item.get("entity_id")],
                recommended_actions=[
                    "Inspect entity_errors and worst_candidate_entity",
                    "Correct the candidate sketch or explicitly relax tolerances",
                ],
                debug_artifacts=[report_path] if report_path else [],
                details={"metrics": metrics, "tolerance": tolerance})
        return result


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback_value):
        return False


class _SketchValidationError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _round_floats(value, precision):
    if isinstance(value, float):
        return round(value, precision)
    if isinstance(value, list):
        return [_round_floats(item, precision) for item in value]
    if isinstance(value, dict):
        return {key: _round_floats(item, precision)
                for key, item in value.items()}
    return value


def _count_by(items, key):
    counts = defaultdict(int)
    for item in items:
        counts[item.get(key, "unknown")] += 1
    return counts


def _xml_id(value):
    text = "".join(ch if ch.isalnum() or ch in "_-" else "_"
                   for ch in str(value))
    return text if text and not text[0].isdigit() else "id_" + text
