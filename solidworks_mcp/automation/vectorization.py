"""Verified raster-to-CAD vectorization and comparison.

The production path uses a semantic model, boundary matting, independent image
evidence, perturbation stability, sub-pixel topology, robust primitive fits,
and a mandatory supersampled reverse-rasterized quality gate.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..constants import SwErrors
from .runtime import atomic_json_write, structured_error


class ImageSketchOperations:
    _APPROXIMATION_PRESETS = {
        "coarse": {
            "max_error_mm": 0.5, "max_entities": 40,
            "min_feature_mm": 0.8, "min_segment_length_mm": 1.0,
            "target_segment_length_mm": 8.0, "max_segment_length_mm": 32.0,
            "corner_angle_deg": 20.0, "max_spline_fit_points": 48,
            "spline_fit_tolerance_ratio": 0.9,
            "max_total_fit_points": 240, "max_total_control_points": 256,
            "max_control_points_per_spline": 64,
            "curve_strategy": "auto", "smoothing": 0.4,
        },
        "balanced": {
            "max_error_mm": 0.15, "max_entities": 80,
            "min_feature_mm": 0.4, "min_segment_length_mm": 0.4,
            "target_segment_length_mm": 4.0, "max_segment_length_mm": 20.0,
            "corner_angle_deg": 12.0, "max_spline_fit_points": 96,
            "spline_fit_tolerance_ratio": 0.75,
            "max_total_fit_points": 480, "max_total_control_points": 384,
            "max_control_points_per_spline": 64,
            "curve_strategy": "auto", "smoothing": 0.25,
        },
        "fine": {
            "max_error_mm": 0.08, "max_entities": 160,
            "min_feature_mm": 0.2, "min_segment_length_mm": 0.2,
            "target_segment_length_mm": 2.0, "max_segment_length_mm": 10.0,
            "corner_angle_deg": 8.0, "max_spline_fit_points": 160,
            "spline_fit_tolerance_ratio": 0.5,
            "max_total_fit_points": 720, "max_total_control_points": 512,
            "max_control_points_per_spline": 64,
            "curve_strategy": "auto", "smoothing": 0.15,
        },
        "ultra": {
            "max_error_mm": 0.04, "max_entities": 240,
            "min_feature_mm": 0.1, "min_segment_length_mm": 0.1,
            "target_segment_length_mm": 1.0, "max_segment_length_mm": 5.0,
            "corner_angle_deg": 5.0, "max_spline_fit_points": 256,
            "spline_fit_tolerance_ratio": 0.35,
            "max_total_fit_points": 900, "max_total_control_points": 512,
            "max_control_points_per_spline": 64,
            "curve_strategy": "auto", "smoothing": 0.2,
        },
    }

    @staticmethod
    def _worker_progress(stage, **details):
        path = os.environ.get("SOLIDWORKS_MCP_WORKER_PROGRESS")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"stage": stage, "pid": os.getpid(), **details},
                    ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            pass

    @staticmethod
    def _commit_document_restored(sketch_result):
        """Read the verified rollback state from a nested sketch result."""
        error = ((sketch_result or {}).get("data") or {}).get("error") or {}
        return bool(error.get("document_restored", False))

    def _rollback_named_sketch(self, sketch_name):
        """Delete one committed sketch and verify that it left the tree."""
        doc, err = self.get_active_doc()
        feature = (self._find_sketch_feature(doc, sketch_name)
                   if err is None else None)
        return bool(feature is not None and self._rollback_created_sketch(
            feature, sketch_name))

    @classmethod
    def _resolve_approximation(cls, geometry, approximation):
        explicit = {**(geometry or {}), **(approximation or {})}
        # Quality is the safe default for CAD reconstruction.  Callers that
        # explicitly trade accuracy for speed may select another preset.
        preset = explicit.get("preset", "ultra")
        if preset not in cls._APPROXIMATION_PRESETS:
            raise ValueError(
                "approximation.preset must be coarse, balanced, fine, or ultra")
        resolved = dict(cls._APPROXIMATION_PRESETS[preset])
        resolved.update(explicit)
        resolved["preset"] = preset
        resolved.setdefault("prefer", ["line", "arc", "circle", "spline"])
        resolved.setdefault("output_mode", "locked_trace")
        resolved.setdefault("simplification_tolerance_mm",
                            float(resolved["max_error_mm"]))
        resolved.setdefault("entity_complexity_weight", 24.0)
        numeric_positive = (
            "max_error_mm", "max_entities", "min_feature_mm",
            "min_segment_length_mm", "target_segment_length_mm",
            "max_segment_length_mm", "corner_angle_deg",
            "max_spline_fit_points", "max_total_fit_points",
            "max_total_control_points", "max_control_points_per_spline",
            "simplification_tolerance_mm",
            "entity_complexity_weight")
        if any(float(resolved[key]) <= 0 for key in numeric_positive):
            raise ValueError("Approximation lengths, limits, and angles must be > 0")
        if not 0.0 <= float(resolved["smoothing"]) <= 1.0:
            raise ValueError("approximation.smoothing must be within [0, 1]")
        if not 0.05 <= float(
                resolved["spline_fit_tolerance_ratio"]) <= 1.0:
            raise ValueError(
                "approximation.spline_fit_tolerance_ratio must be within [0.05, 1]")
        if (float(resolved["simplification_tolerance_mm"]) >
                float(resolved["max_error_mm"])):
            raise ValueError(
                "approximation.simplification_tolerance_mm cannot exceed "
                "max_error_mm")
        strategies = {"auto", "periodic_bspline", "hybrid_primitives"}
        if resolved["curve_strategy"] not in strategies:
            raise ValueError(
                "approximation.curve_strategy must be auto, periodic_bspline, "
                "or hybrid_primitives")
        output_modes = {
            "locked_trace", "minimal_parametric", "reference_spline",
            "construction_reference",
        }
        output_mode = str(resolved["output_mode"])
        if output_mode not in output_modes:
            raise ValueError(
                "approximation.output_mode must be locked_trace, "
                "minimal_parametric, reference_spline, or "
                "construction_reference")
        # Output modes are behavioral contracts, not labels. A reference
        # spline must not silently become dozens of primitives, while a
        # minimal parametric sketch must prefer editable CAD primitives.
        if output_mode == "reference_spline":
            resolved["prefer"] = ["spline"]
            resolved["curve_strategy"] = "periodic_bspline"
        elif output_mode == "minimal_parametric":
            resolved["curve_strategy"] = "hybrid_primitives"
        if (float(resolved["min_segment_length_mm"]) >
                float(resolved["target_segment_length_mm"]) or
                float(resolved["target_segment_length_mm"]) >
                float(resolved["max_segment_length_mm"])):
            raise ValueError(
                "Segment lengths must satisfy min <= target <= max")
        supported = {"line", "arc", "circle", "spline"}
        if not resolved["prefer"] or any(
                item not in supported for item in resolved["prefer"]):
            raise ValueError(
                "approximation.prefer may contain line, arc, circle, and spline")
        return resolved

    @staticmethod
    def _apply_projection_policy(rgb, alpha, image_mode, projection=None,
                                 require_orthographic=False):
        """Apply only an explicit, numerically verified projection policy."""
        import cv2
        import numpy as np

        policy = dict(projection or {})
        mode = policy.get("mode")
        if mode is None:
            mode = ("trace_as_is" if image_mode == "trace_as_is" else
                    "orthographic_unspecified")
        allowed = {
            "orthographic", "orthographic_unspecified", "homography",
            "trace_as_is",
        }
        if mode not in allowed:
            raise ValueError(
                "projection.mode must be orthographic, homography, or "
                "trace_as_is")
        if require_orthographic and mode not in {"orthographic", "homography"}:
            raise ValueError(
                "require_orthographic=true requires projection.mode="
                "orthographic or an explicit homography")

        identity = np.eye(3, dtype=float)
        report = {
            "mode": mode,
            "require_orthographic": bool(require_orthographic),
            "source_to_working_pixels": identity.tolist(),
            "warnings": [],
            "confidence_cap": 1.0,
        }
        if mode == "trace_as_is":
            report["warnings"].append(
                "Perspective was explicitly preserved; the resulting sketch "
                "is a trace, not an orthographic reconstruction")
            confidence_cap = float(policy.get("confidence_cap", 0.75))
            if not 0.0 <= confidence_cap <= 1.0:
                raise ValueError(
                    "projection.confidence_cap must be within [0,1]")
            report["confidence_cap"] = confidence_cap
            return rgb, alpha, report, identity
        if mode in {"orthographic", "orthographic_unspecified"}:
            report["orthographic_confirmed"] = mode == "orthographic"
            return rgb, alpha, report, identity

        source = np.asarray(policy.get("source_quad_px"), dtype=np.float64)
        if source.shape != (4, 2) or not np.isfinite(source).all():
            raise ValueError(
                "projection.source_quad_px must contain four finite [x,y] "
                "points ordered top-left, top-right, bottom-right, bottom-left")
        height_source, width_source = alpha.shape[:2]
        if (np.any(source[:, 0] < 0.0) or
                np.any(source[:, 0] > width_source - 1.0) or
                np.any(source[:, 1] < 0.0) or
                np.any(source[:, 1] > height_source - 1.0)):
            raise ValueError(
                "projection.source_quad_px must lie inside the source image")
        polygon = np.round(source).astype(np.int32).reshape(-1, 1, 2)
        if not cv2.isContourConvex(polygon):
            raise ValueError(
                "projection.source_quad_px must be a convex ordered quadrilateral")
        source_area = abs(float(cv2.contourArea(source.astype(np.float32))))
        if source_area < 64.0:
            raise ValueError("projection.source_quad_px area is too small")
        requested_size = policy.get("output_size_px")
        if requested_size is None:
            top = float(np.linalg.norm(source[1] - source[0]))
            bottom = float(np.linalg.norm(source[2] - source[3]))
            right = float(np.linalg.norm(source[2] - source[1]))
            left = float(np.linalg.norm(source[3] - source[0]))
            width = int(round(max(top, bottom)))
            height = int(round(max(left, right)))
        else:
            if (not isinstance(requested_size, (list, tuple)) or
                    len(requested_size) != 2):
                raise ValueError(
                    "projection.output_size_px must be [width,height]")
            width, height = [int(round(float(value)))
                             for value in requested_size]
        if width < 64 or height < 64 or width > 16384 or height > 16384:
            raise ValueError(
                "projection.output_size_px must be within 64..16384 pixels")
        destination = np.asarray([
            [0.0, 0.0], [width - 1.0, 0.0],
            [width - 1.0, height - 1.0], [0.0, height - 1.0],
        ], dtype=np.float64)
        transform = cv2.getPerspectiveTransform(
            source.astype(np.float32), destination.astype(np.float32))
        condition = float(np.linalg.cond(transform))
        if (not np.isfinite(transform).all() or not np.isfinite(condition) or
                condition > 1e10):
            raise ValueError(
                "projection homography is numerically ill-conditioned")
        rectified_rgb = cv2.warpPerspective(
            rgb, transform, (width, height), flags=cv2.INTER_LANCZOS4,
            borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
        alpha_border = 255 if int(alpha.max()) == int(alpha.min()) == 255 else 0
        rectified_alpha = cv2.warpPerspective(
            alpha, transform, (width, height), flags=cv2.INTER_LANCZOS4,
            borderMode=cv2.BORDER_CONSTANT, borderValue=alpha_border)
        report.update({
            "orthographic_confirmed": True,
            "source_quad_px": source.tolist(),
            "destination_quad_px": destination.tolist(),
            "output_size_px": [width, height],
            "source_area_px2": source_area,
            "condition_number": condition,
            "source_to_working_pixels": transform.tolist(),
        })
        return rectified_rgb, rectified_alpha, report, transform

    @staticmethod
    def _prepare_output_entities(entities, output_mode):
        """Apply material output-mode semantics to fitted entity payloads."""
        if output_mode == "construction_reference":
            for entity in entities:
                entity["construction"] = True
        return entities

    @staticmethod
    def _output_commit_policy(output_mode, max_entities, closed_contours,
                              require_closed):
        locked = output_mode == "locked_trace"
        solve = {
            "mode": "locked_trace" if locked else output_mode,
            "target": "fully_defined" if locked else None,
        }
        sketch_validation = {"max_entities": int(max_entities)}
        if output_mode != "construction_reference":
            sketch_validation.update({
                "require_closed": bool(require_closed),
                "closed_contours": int(closed_contours),
            })
        return solve, sketch_validation

    @staticmethod
    def _primitive_as_cubic_bspline(entity):
        """Convert a supported primitive to an open cubic B-spline exactly or safely."""
        entity_type = str(entity.get("type", ""))
        if entity_type == "b_spline":
            controls = [list(map(float, point[:2]))
                        for point in entity.get("control_points", [])]
            knots = [float(value) for value in entity.get("knots", [])]
            if (int(entity.get("order", 4)) != 4 or
                    bool(entity.get("periodic", False)) or
                    len(controls) < 4 or len(knots) != len(controls) + 4):
                return None
            return controls, knots
        if entity_type == "line":
            start = list(map(float, entity.get("start", [])[:2]))
            end = list(map(float, entity.get("end", [])[:2]))
            if len(start) != 2 or len(end) != 2:
                return None
            delta = [end[index] - start[index] for index in range(2)]
            controls = [start,
                        [start[i] + delta[i] / 3.0 for i in range(2)],
                        [start[i] + 2.0 * delta[i] / 3.0 for i in range(2)],
                        end]
            return controls, [0.0] * 4 + [1.0] * 4
        if entity_type not in {"arc", "circle"}:
            return None
        center = list(map(float, entity.get("center", [])[:2]))
        if len(center) != 2:
            return None
        if entity_type == "circle":
            radius = float(entity.get("radius", 0.0))
            if not radius > 0.0:
                return None
            start_angle, sweep = 0.0, 2.0 * math.pi
            start = [center[0] + radius, center[1]]
            end = list(start)
        else:
            start = list(map(float, entity.get("start", [])[:2]))
            end = list(map(float, entity.get("end", [])[:2]))
            if len(start) != 2 or len(end) != 2:
                return None
            start_angle = math.atan2(
                start[1] - center[1], start[0] - center[0])
            end_angle = math.atan2(
                end[1] - center[1], end[0] - center[0])
            if int(entity.get("direction", 1)) >= 0:
                while end_angle <= start_angle:
                    end_angle += 2.0 * math.pi
            else:
                while end_angle >= start_angle:
                    end_angle -= 2.0 * math.pi
            sweep = end_angle - start_angle
            radius = 0.5 * (
                math.dist(center, start) + math.dist(center, end))
        if not radius > 0.0 or abs(sweep) <= 1e-12:
            return None
        piece_count = max(
            1, int(math.ceil(abs(sweep) / (math.pi / 4.0))))
        pieces = []
        for index in range(piece_count):
            angle0 = start_angle + sweep * index / piece_count
            angle1 = start_angle + sweep * (index + 1) / piece_count
            delta = angle1 - angle0
            factor = 4.0 / 3.0 * math.tan(delta / 4.0)
            point0 = [center[0] + radius * math.cos(angle0),
                      center[1] + radius * math.sin(angle0)]
            point3 = [center[0] + radius * math.cos(angle1),
                      center[1] + radius * math.sin(angle1)]
            if index == 0:
                point0 = list(start)
            if index == piece_count - 1:
                point3 = list(end)
            radius0 = math.dist(center, point0)
            radius1 = math.dist(center, point3)
            tangent0 = [-math.sin(angle0), math.cos(angle0)]
            tangent1 = [-math.sin(angle1), math.cos(angle1)]
            point1 = [point0[i] + radius0 * factor * tangent0[i]
                      for i in range(2)]
            point2 = [point3[i] - radius1 * factor * tangent1[i]
                      for i in range(2)]
            pieces.append([point0, point1, point2, point3])
        controls = [list(point) for point in pieces[0]]
        knots = [0.0] * 4
        for index, piece in enumerate(pieces):
            if index:
                controls.extend([list(point) for point in piece[1:]])
                knots.extend([float(index)] * 3)
        knots.extend([float(piece_count)] * 4)
        knots = [value / float(piece_count) for value in knots]
        return controls, knots

    @classmethod
    def _construction_nurbs_commit_plan(cls, loops, output_mode,
                                        max_error_mm,
                                        max_total_control_points):
        """Batch each construction loop into one equation-NURBS COM call."""
        original = [entity for loop in (loops or [])
                    for entity in (loop.get("entities") or [])]
        if output_mode != "construction_reference":
            return original, {"applied": False, "reason": "output_mode"}
        cad_entities = []
        reports = []
        total_transport_controls = 0
        for loop_index, loop in enumerate(loops or []):
            source_entities = list(loop.get("entities") or [])
            if len(source_entities) < 2:
                cad_entities.extend(source_entities)
                continue
            converted = []
            for source in source_entities:
                curve = cls._primitive_as_cubic_bspline(source)
                if curve is None:
                    return original, {
                        "applied": False,
                        "reason": "unsupported_or_non_cubic_entity",
                        "entity_id": source.get("id"),
                    }
                controls, knots = curve
                converted.append({
                    "id": str(source["id"]),
                    "type": "b_spline",
                    "order": 4,
                    "periodic": False,
                    "closed": False,
                    "construction": True,
                    "control_points": controls,
                    "knots": knots,
                    "commit_conversion": "batched_composite_nurbs",
                    "original_type": str(source.get("type", "")),
                })
            join_gaps = []
            pair_count = (len(converted) if bool(loop.get("closed", True))
                          else len(converted) - 1)
            for index in range(max(0, pair_count)):
                following = (index + 1) % len(converted)
                left = converted[index]["control_points"][-1]
                right = converted[following]["control_points"][0]
                gap = math.dist(left, right)
                join_gaps.append(gap)
                if gap / 2.0 > float(max_error_mm) + 1e-9:
                    return original, {
                        "applied": False,
                        "reason": "join_adjustment_exceeds_tolerance",
                        "join_gap_mm": gap,
                        "max_error_mm": float(max_error_mm),
                    }
                midpoint = [(left[i] + right[i]) / 2.0 for i in range(2)]
                converted[index]["control_points"][-1] = list(midpoint)
                converted[following]["control_points"][0] = list(midpoint)

            def transformed_knots(entity, offset):
                values = entity["knots"]
                start, end = float(values[3]), float(values[-4])
                if not end > start:
                    raise ValueError("Degenerate cubic B-spline knot domain")
                return [offset + (float(value) - start) / (end - start)
                        for value in values]

            controls = [list(point)
                        for point in converted[0]["control_points"]]
            knots = transformed_knots(converted[0], 0.0)
            for index, entity in enumerate(converted[1:], 1):
                controls.extend([list(point) for point in
                                 entity["control_points"][1:]])
                local_knots = transformed_knots(entity, float(index))
                knots = knots[:-1] + local_knots[4:]
            scale = float(len(converted))
            knots = [value / scale for value in knots]
            if len(knots) != len(controls) + 4:
                raise ValueError("Composite cubic B-spline is inconsistent")
            total_transport_controls += len(controls)
            cad_entities.append({
                "id": f"__construction_chain_{loop_index + 1:03d}",
                "type": "b_spline_chain",
                "order": 4,
                "periodic": False,
                "closed": bool(loop.get("closed", True)),
                "construction": True,
                "control_points": controls,
                "knots": knots,
                "segments": converted,
                "endpoint_match_tolerance_mm": 0.002,
            })
            reports.append({
                "loop": loop_index + 1,
                "source_entities": len(source_entities),
                "transport_control_points": len(controls),
                "max_join_gap_mm": max(join_gaps or [0.0]),
                "max_endpoint_adjustment_mm": max(join_gaps or [0.0]) / 2.0,
            })
        if total_transport_controls > int(max_total_control_points):
            return original, {
                "applied": False,
                "reason": "transport_control_budget_exceeded",
                "transport_control_points": total_transport_controls,
                "max_total_control_points": int(max_total_control_points),
            }
        return cad_entities, {
            "applied": bool(reports),
            "method": "single_ISplineParamData_call_per_loop",
            "source_entities": len(original),
            "cad_calls": len(reports) + sum(
                entity.get("type") != "b_spline_chain"
                for entity in cad_entities),
            "expected_segments": sum(
                len(entity.get("segments") or [entity])
                for entity in cad_entities),
            "transport_control_points": total_transport_controls,
            "loops": reports,
        }

    @staticmethod
    def _batched_commit_profile(plan, output_mode):
        controls = int(plan.get("transport_control_points", 0))
        calls = int(plan.get("cad_calls", 0))
        segments = int(plan.get("expected_segments", 0))
        creation = 6.0 + calls * 1.5 + controls * 0.02
        verification = 4.0 + segments * 0.12 + controls * 0.004
        if output_mode == "locked_trace":
            creation += segments * 0.5
        return {
            "estimated_sec": round(creation + verification, 3),
            "creation_estimated_sec": round(creation, 3),
            "verification_estimated_sec": round(verification, 3),
            "cad_calls": calls,
            "expected_segments": segments,
            "transport_control_points": controls,
            "calibration": "SW2026_SP2.1_batched_composite_nurbs",
        }

    @staticmethod
    def _topology_constraints(loops, output_mode):
        """Build editable contour relations without constraining references."""
        if output_mode == "construction_reference":
            return []
        constraints = []
        for loop in loops:
            entities = loop.get("entities") or []
            pair_count = (len(entities) if loop.get("closed", True)
                          else max(0, len(entities) - 1))
            for index in range(pair_count):
                left = entities[index]
                right = entities[(index + 1) % len(entities)]
                if (left["type"] not in {"circle", "b_spline"} and
                        right["type"] not in {"circle", "b_spline"}):
                    constraints.append({
                        "type": "coincident",
                        "entities": [
                            f"{left['id']}.end", f"{right['id']}.start"],
                    })
        return constraints

    @staticmethod
    def _parameterization_report(entities, output_mode):
        counts = _count_types(entities)
        primitive_count = sum(
            int(counts.get(name, 0)) for name in ("line", "arc", "circle"))
        total = max(1, len(entities))
        return {
            "output_mode": output_mode,
            "primitive_entities": primitive_count,
            "freeform_entities": len(entities) - primitive_count,
            "primitive_fraction": primitive_count / total,
            "construction_entities": sum(
                bool(entity.get("construction")) for entity in entities),
            "explicitly_locked": output_mode == "locked_trace",
            "auxiliary_reference": output_mode == "construction_reference",
            "editable_parametric": output_mode == "minimal_parametric",
        }

    def _image_dependencies(self):
        try:
            import importlib.util
            required = ("cv2", "numpy", "scipy", "skimage", "shapely", "PIL")
            missing = [name for name in required
                       if importlib.util.find_spec(name) is None]
            if missing:
                raise ImportError(
                    "missing Python packages: " + ", ".join(missing))
            return None
        except Exception as exc:
            return self._error(
                "CAPABILITY_UNAVAILABLE",
                f"Image vectorization backend unavailable: {exc}")

    @staticmethod
    def _load_image(path):
        import cv2
        import numpy as np
        from PIL import Image, ImageOps
        with Image.open(path) as source:
            source = ImageOps.exif_transpose(source)
            rgba = source.convert("RGBA")
            array = np.asarray(rgba)
        rgb = cv2.cvtColor(array[:, :, :3], cv2.COLOR_RGB2BGR)
        alpha = array[:, :, 3]
        return rgb, alpha

    @staticmethod
    def _largest_contour(mask):
        import cv2
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_NONE)
        return max(contours, key=cv2.contourArea) if contours else None

    @staticmethod
    def _clean_mask(mask, min_area_px, close_radius_px):
        import cv2
        import numpy as np
        mask = (mask > 0).astype(np.uint8) * 255
        radius = max(1, int(round(close_radius_px)))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                           (2 * radius + 1, 2 * radius + 1))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        cleaned = np.zeros_like(mask)
        for label in range(1, count):
            if stats[label, cv2.CC_STAT_AREA] >= min_area_px:
                cleaned[labels == label] = 255
        return cleaned

    def _score_mask(self, mask, ignore_border_touching, min_area_px):
        import cv2
        import numpy as np
        contour = self._largest_contour(mask)
        if contour is None:
            return -1e9, {"reason": "no contour"}
        area = float(cv2.contourArea(contour))
        height, width = mask.shape
        ratio = area / max(1.0, width * height)
        x, y, w, h = cv2.boundingRect(contour)
        border = x <= 0 or y <= 0 or x + w >= width or y + h >= height
        hull_area = max(float(cv2.contourArea(cv2.convexHull(contour))), 1.0)
        solidity = area / hull_area
        components = max(0, cv2.connectedComponents(mask)[0] - 1)
        score = 0.0
        score += min(1.0, area / max(float(min_area_px), 1.0)) * 0.2
        score += (1.0 - abs(ratio - 0.25)) * 0.25
        score += min(solidity, 1.0) * 0.15
        score += max(0.0, 1.0 - (components - 1) * 0.05) * 0.1
        if ratio < 0.0001 or ratio > 0.98:
            score -= 2.0
        if border and ignore_border_touching:
            score -= 1.5
        return score, {"area_px": area, "area_ratio": ratio,
                       "bbox_px": [x, y, x + w, y + h],
                       "border_touching": border, "solidity": solidity,
                       "components": components}

    def _segment_image(self, rgb, alpha, mode, selection):
        import cv2
        import numpy as np
        height, width = alpha.shape
        roi = selection.get("roi_px")
        roi_mask = np.ones((height, width), dtype=np.uint8) * 255
        if roi:
            x0, y0, x1, y1 = [int(round(v)) for v in roi]
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(width, x1), min(height, y1)
            if x1 <= x0 or y1 <= y0:
                raise ValueError("roi_px is outside the image")
            roi_mask[:] = 0
            roi_mask[y0:y1, x0:x1] = 255
        min_area = max(1, int(selection.get("min_area_px", 100)))
        ignore_border = bool(selection.get("ignore_border_touching", True))
        candidates = []
        if int(alpha.max()) - int(alpha.min()) > 16:
            threshold = max(1, int(cv2.threshold(
                alpha, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0]))
            candidates.append(("alpha", (alpha > threshold).astype(np.uint8) * 255))
        lab = cv2.cvtColor(rgb, cv2.COLOR_BGR2LAB).astype(np.float32)
        border_pixels = np.concatenate((lab[0], lab[-1], lab[:, 0], lab[:, -1]))
        background = np.median(border_pixels, axis=0)
        distance = np.linalg.norm(lab - background, axis=2)
        border_distance = np.concatenate((distance[0], distance[-1],
                                          distance[:, 0], distance[:, -1]))
        adaptive_floor = max(3.0, float(np.percentile(border_distance, 99.7)) + 1.5)
        candidates.append(("border_lab_noise_model",
                           (distance > adaptive_floor).astype(np.uint8) * 255))
        # Mahalanobis distance handles subtle blue/grey silhouettes on a white
        # antialiased background better than a single luminance threshold.
        covariance = np.cov(border_pixels.T) + np.eye(3) * 1.0
        inverse_covariance = np.linalg.pinv(covariance)
        delta = lab - background
        mahalanobis = np.sqrt(np.einsum(
            "...i,ij,...j->...", delta, inverse_covariance, delta))
        border_mahalanobis = np.concatenate((
            mahalanobis[0], mahalanobis[-1],
            mahalanobis[:, 0], mahalanobis[:, -1]))
        mahalanobis_floor = max(
            3.5, float(np.percentile(border_mahalanobis, 99.7)) + 0.75)
        candidates.append(("border_lab_mahalanobis",
                           (mahalanobis > mahalanobis_floor).astype(np.uint8) * 255))
        distance_u8 = np.clip(distance / max(distance.max(), 1e-9) * 255,
                              0, 255).astype(np.uint8)
        _, lab_mask = cv2.threshold(distance_u8, 0, 255,
                                    cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        candidates.append(("border_lab", lab_mask))
        gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
        _, dark = cv2.threshold(gray, 0, 255,
                                cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        candidates.extend((("otsu_dark", dark), ("otsu_light", 255 - dark)))
        if mode in {"technical_drawing", "line_drawing"}:
            adaptive = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY_INV, 31, 5)
            from skimage.morphology import skeletonize
            skeleton = skeletonize(adaptive > 0).astype(np.uint8) * 255
            candidates.append(("adaptive_skeleton", skeleton))
        best = None
        diagnostics = []
        close_radius = max(1.0, min(height, width) * 0.0015)
        for name, candidate in candidates:
            candidate = cv2.bitwise_and(candidate, roi_mask)
            cleaned = self._clean_mask(candidate, min_area, close_radius)
            score, details = self._score_mask(
                cleaned, ignore_border, min_area)
            details.update({"name": name, "score": score})
            diagnostics.append(details)
            if best is None or score > best[0]:
                best = (score, cleaned, details)
        if best is None or best[0] < -0.25:
            raise ValueError("No unambiguous foreground mask was found")
        confidence = max(0.0, min(1.0, 0.55 + best[0] * 0.35))
        return best[1], confidence, diagnostics

    @staticmethod
    def _signed_area(points):
        return 0.5 * sum(points[i][0] * points[(i + 1) % len(points)][1] -
                         points[(i + 1) % len(points)][0] * points[i][1]
                         for i in range(len(points)))

    def _extract_topology(self, mask, selection, min_feature_px, level=0.5):
        import numpy as np
        from shapely.geometry import Polygon, Point
        from skimage import measure
        level = float(level)
        if not 0.0 < level < 1.0:
            raise ValueError("Topology contour level must be within (0, 1)")
        raw = measure.find_contours(mask.astype(float) / 255.0, level,
                                    fully_connected="high")
        loops = []
        for contour in raw:
            points = [[float(col), float(row)] for row, col in contour]
            if len(points) < 8:
                continue
            if math.dist(points[0], points[-1]) > 2.0:
                continue
            points[-1] = points[0]
            polygon = Polygon(points)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.is_empty or polygon.area < min_feature_px ** 2:
                continue
            representative = polygon.representative_point()
            loops.append({"points": points[:-1], "polygon": polygon,
                          "area": float(abs(polygon.area)),
                          "representative": representative})
        loops.sort(key=lambda item: item["area"], reverse=True)
        for index, loop in enumerate(loops):
            parents = [candidate for candidate in loops[:index]
                       if candidate["polygon"].contains(loop["representative"])]
            parent = min(parents, key=lambda item: item["area"]) if parents else None
            loop["parent"] = loops.index(parent) if parent else None
            loop["depth"] = 0 if parent is None else parent["depth"] + 1
            loop["role"] = "outer" if loop["depth"] % 2 == 0 else "hole"
            desired_positive = loop["role"] == "outer"
            if (self._signed_area(loop["points"]) > 0) != desired_positive:
                loop["points"].reverse()
        mode = selection.get("mode", "largest_external_with_holes")
        if mode in {"largest_external_with_holes", "largest_external_only"}:
            roots = [i for i, loop in enumerate(loops) if loop["parent"] is None]
            if not roots:
                raise ValueError("No external contour found")
            root = max(roots, key=lambda i: loops[i]["area"])

            def descends(index, ancestor):
                current = index
                while loops[current]["parent"] is not None:
                    current = loops[current]["parent"]
                    if current == ancestor:
                        return True
                return index == ancestor

            loops = [loop for index, loop in enumerate(loops)
                     if descends(index, root)]
            if mode == "largest_external_only":
                loops = [loop for loop in loops if loop["depth"] == 0]
        if not loops:
            raise ValueError("Topology recovery produced no valid contours")
        return [{"points": loop["points"], "area_px": loop["area"],
                 "role": loop["role"], "depth": loop["depth"]}
                for loop in loops]

    @staticmethod
    def _resample_closed(points, step):
        import numpy as np
        points = np.asarray(points, dtype=float)
        points = np.vstack([points, points[0]])
        lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
        cumulative = np.concatenate([[0.0], np.cumsum(lengths)])
        count = max(16, int(math.ceil(cumulative[-1] / max(step, 1e-9))))
        targets = np.linspace(0.0, cumulative[-1], count, endpoint=False)
        x = np.interp(targets, cumulative, points[:, 0])
        y = np.interp(targets, cumulative, points[:, 1])
        return np.column_stack([x, y])

    @staticmethod
    def _line_fit(points):
        import numpy as np
        points = np.asarray(points, dtype=float)
        a, b = points[0], points[-1]
        vector = b - a
        length = np.linalg.norm(vector)
        if length < 1e-12:
            return None
        offsets = points - a
        distance = np.abs(vector[0] * offsets[:, 1] -
                          vector[1] * offsets[:, 0]) / length
        return {"max_error": float(distance.max()),
                "mean_error": float(distance.mean()),
                "start": a.tolist(), "end": b.tolist()}

    @staticmethod
    def _circle_fit(points):
        import numpy as np
        from scipy.optimize import least_squares
        points = np.asarray(points, dtype=float)
        if len(points) < 5:
            return None
        x, y = points[:, 0], points[:, 1]
        matrix = np.column_stack([2 * x, 2 * y, np.ones(len(points))])
        target = x * x + y * y
        try:
            cx, cy, constant = np.linalg.lstsq(matrix, target, rcond=None)[0]
            radius = math.sqrt(max(0.0, constant + cx * cx + cy * cy))
            fitted = least_squares(
                lambda params: np.hypot(x - params[0], y - params[1]) - params[2],
                [cx, cy, radius], loss="soft_l1",
                f_scale=max(radius * 1e-4, 1e-6))
            cx, cy, radius = fitted.x
            if radius <= 0 or not np.isfinite(radius):
                return None
            errors = np.abs(np.hypot(x - cx, y - cy) - radius)
            angles = np.unwrap(np.arctan2(y - cy, x - cx))
            diffs = np.diff(angles)
            direction = 1 if np.median(diffs) >= 0 else -1
            monotonic = float(np.mean(diffs * direction >= -1e-3))
            sweep = float(abs(angles[-1] - angles[0]))
            return {"center": [float(cx), float(cy)], "radius": float(radius),
                    "max_error": float(errors.max()),
                    "mean_error": float(errors.mean()),
                    "direction": direction, "sweep": sweep,
                    "monotonic": monotonic,
                    "start": points[0].tolist(), "end": points[-1].tolist()}
        except Exception:
            return None

    @staticmethod
    def _curvature_corners(points, tolerance, corner_angle_deg=12.0,
                           smoothing=0.15, min_segment_length=0.4):
        import numpy as np
        from scipy.signal import find_peaks, savgol_filter
        points = np.asarray(points, dtype=float)
        count = len(points)
        requested_window = max(5, int(round(11 + float(smoothing) * 100)))
        if requested_window % 2 == 0:
            requested_window += 1
        window = min(requested_window,
                     count - 1 if count % 2 == 0 else count)
        window = max(5, window if window % 2 else window - 1)
        if count < window:
            return [0]
        x = savgol_filter(points[:, 0], window, 3, mode="wrap")
        y = savgol_filter(points[:, 1], window, 3, mode="wrap")
        tangent = np.unwrap(np.arctan2(np.gradient(y), np.gradient(x)))
        curvature = np.abs(np.gradient(tangent))
        threshold = max(0.08,
                        math.radians(float(corner_angle_deg)) * 0.25,
                        float(np.percentile(curvature, 90)))
        median_step = max(float(np.median(np.linalg.norm(
            np.roll(points, -1, axis=0) - points, axis=1))), 1e-9)
        requested_separation = max(
            4, int(round(float(min_segment_length) / median_step)))
        peaks, properties = find_peaks(
            curvature, height=threshold,
            distance=max(requested_separation, count // 100))
        ranked = sorted(peaks.tolist(), key=lambda i: curvature[i], reverse=True)
        # Keep only stable, well-separated corner candidates.
        selected = []
        min_sep = max(requested_separation, count // 200)
        for index in ranked:
            if all(min((index - other) % count, (other - index) % count) >= min_sep
                   for other in selected):
                selected.append(index)
        return sorted(selected[:64]) or [int(np.argmax(curvature))]

    @staticmethod
    def _regularized_spline(points, tolerance, max_fit_points=32,
                            closed=False, target_segment_length=None,
                            max_segment_length=None):
        import numpy as np
        from scipy.interpolate import splprep, splev
        from scipy.spatial import cKDTree
        points = np.asarray(points, dtype=float)
        if closed:
            if np.linalg.norm(points[0] - points[-1]) <= 1e-12:
                points = points[:-1]
        if len(points) < 4:
            return points.tolist()
        # SolidWorks creates an interpolating spline through the supplied fit
        # points.  Select those points adaptively against that same curve,
        # checking both source-to-spline and spline-to-source distances.  A
        # one-way error check can miss overshoot at sharp silhouette details.
        limit = max(4, min(int(max_fit_points), len(points)))
        source_length = float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())
        desired_length = float(target_segment_length or
                               max(source_length / 12.0, tolerance))
        if max_segment_length is not None:
            desired_length = min(desired_length, float(max_segment_length))
        initial = min(limit, max(4, int(math.ceil(
            source_length / max(desired_length, 1e-9))) + 1))
        selected = set(np.linspace(
            0, len(points) - 1, initial, dtype=int).tolist())
        selected.update([0, len(points) - 1])
        for _ in range(limit):
            indices = np.asarray(sorted(selected), dtype=int)
            fit_points = points[indices]
            try:
                tck, _ = splprep(
                    [fit_points[:, 0], fit_points[:, 1]], s=0,
                    per=closed, k=min(3, len(fit_points) - 1))
                dense = np.column_stack(splev(
                    np.linspace(0.0, 1.0, max(1000, len(points) * 4),
                                endpoint=not closed), tck))
            except Exception:
                break
            source_error = cKDTree(dense).query(points)[0]
            dense_error, dense_owner = cKDTree(points).query(dense)
            source_index = int(np.argmax(source_error))
            dense_index = int(np.argmax(dense_error))
            worst = max(float(source_error[source_index]),
                        float(dense_error[dense_index]))
            if worst <= tolerance:
                result = fit_points.tolist()
                if closed:
                    result.append(result[0])
                return result
            selected.add(source_index)
            selected.add(int(dense_owner[dense_index]))
            if len(selected) >= limit:
                break
        indices = np.asarray(sorted(selected), dtype=int)
        result = points[indices].tolist()
        if closed:
            result.append(result[0])
        return result

    @staticmethod
    def _symmetric_curve_error(source, candidate):
        import numpy as np
        from scipy.spatial import cKDTree
        source = np.asarray(source, dtype=float)
        candidate = np.asarray(candidate, dtype=float)
        if not len(source) or not len(candidate):
            return float("inf")
        return max(float(cKDTree(candidate).query(source)[0].max()),
                   float(cKDTree(source).query(candidate)[0].max()))

    def _fit_loop_hybrid_primitives(self, points_mm, max_error, prefer,
                                    max_entities, approximation=None):
        import numpy as np
        approximation = approximation or {}
        corner_angle = float(approximation.get("corner_angle_deg", 12.0))
        smoothing = float(approximation.get("smoothing", 0.15))
        min_segment = float(approximation.get("min_segment_length_mm", 0.4))
        target_segment = float(approximation.get("target_segment_length_mm", 4.0))
        max_segment = float(approximation.get("max_segment_length_mm", 20.0))
        max_spline_points = int(approximation.get("max_spline_fit_points", 96))
        max_control_points = int(approximation.get(
            "max_control_points_per_spline", 64))
        explicit_tolerance = max(1e-4, min(
            float(max_error), float(approximation.get(
                "explicit_spline_tolerance_mm", float(max_error) * 0.8))))
        spline_tolerance = max_error * float(
            approximation.get("spline_fit_tolerance_ratio", 0.75))
        dense = self._resample_closed(points_mm, max(max_error * 0.35, 0.01))
        # A near-circle is represented as a true circle, never a many-sided arc.
        circle = self._circle_fit(np.vstack([dense, dense[0]]))
        if (circle and circle["max_error"] <= max_error and
                circle["sweep"] > math.radians(300)):
            return ([{"type": "circle", "center": circle["center"],
                      "radius": circle["radius"],
                      "fit_reason": "closed contour passed robust circle fit",
                      "fit_error_mm": circle["max_error"]}],
                    circle["max_error"])
        corners = self._curvature_corners(
            dense, max_error, corner_angle_deg=corner_angle,
            smoothing=smoothing, min_segment_length=min_segment)
        if len(corners) == 1:
            rotated = np.vstack([dense[corners[0]:], dense[:corners[0] + 1]])
            fit_points = self._regularized_spline(
                rotated, spline_tolerance,
                max_fit_points=max_spline_points, closed=True,
                target_segment_length=target_segment,
                max_segment_length=max_segment)
            entity = {"type": "spline", "fit_points": fit_points,
                      "closed": True,
                      "fit_reason": "single closed spline preserved the contour"}
            error = self._symmetric_curve_error(
                rotated, self._sample_fitted_entity(entity))
            entity["fit_error_mm"] = error
            return [entity], error
        entities, worst = [], 0.0
        ordered = corners + [corners[0] + len(dense)]
        extended = np.vstack([dense, dense])
        for start, end in zip(ordered, ordered[1:]):
            segment_points = extended[start:end + 1]
            if len(segment_points) < 2:
                continue
            line = self._line_fit(segment_points)
            arc = self._circle_fit(segment_points)
            candidates = []
            if "line" in prefer and line and line["max_error"] <= max_error:
                candidates.append((line["mean_error"] + max_error * 0.02,
                                   {"type": "line", "start": line["start"],
                                    "end": line["end"],
                                    "fit_reason": "robust line fit met tolerance",
                                    "fit_error_mm": line["max_error"]},
                                   line["max_error"]))
            if ("arc" in prefer and arc and arc["max_error"] <= max_error and
                    arc["monotonic"] >= 0.97 and
                    math.radians(3) <= arc["sweep"] <= math.radians(330)):
                candidates.append((arc["mean_error"] + max_error * 0.04,
                                   {"type": "arc", "center": arc["center"],
                                    "start": arc["start"], "end": arc["end"],
                                    "direction": arc["direction"],
                                    "fit_reason": "robust monotonic arc fit met tolerance",
                                    "fit_error_mm": arc["max_error"]},
                                   arc["max_error"]))
            if candidates:
                _, entity, error = min(candidates, key=lambda item: item[0])
            else:
                explicit, explicit_report = self._fit_open_bspline(
                    segment_points, explicit_tolerance, max_control_points)
                if (explicit is not None and
                        float(explicit.get("fit_error_mm", float("inf"))) <=
                        float(max_error)):
                    entity = explicit
                    entity["fit_reason"] = (
                        "line and arc fits could not meet tolerance; direct "
                        "bounded explicit cubic NURBS fit accepted")
                    entity["explicit_spline_fit"] = explicit_report
                    error = float(entity["fit_error_mm"])
                else:
                    fit_points = self._regularized_spline(
                        segment_points, spline_tolerance,
                        max_fit_points=max_spline_points, closed=False,
                        target_segment_length=target_segment,
                        max_segment_length=max_segment)
                    entity = {"type": "spline", "fit_points": fit_points,
                              "closed": False,
                              "fit_reason": (
                                  "direct explicit NURBS, line, and arc fits "
                                  "could not meet tolerance; bounded "
                                  "interpolating candidate retained for "
                                  "mandatory materialization"),
                              "explicit_spline_fit": explicit_report}
                    error = self._symmetric_curve_error(
                        segment_points, self._sample_fitted_entity(entity))
                    entity["fit_error_mm"] = error
            entities.append(entity)
            worst = max(worst, error)
        if len(entities) > max_entities:
            fit_points = self._regularized_spline(
                np.vstack([dense, dense[0]]), spline_tolerance,
                max_fit_points=min(max_spline_points,
                                   max(24, max_entities * 8)), closed=True,
                target_segment_length=target_segment,
                max_segment_length=max_segment)
            entity = {"type": "spline", "fit_points": fit_points,
                      "closed": True,
                      "fit_reason": "entity limit required one closed spline"}
            error = self._symmetric_curve_error(
                np.vstack([dense, dense[0]]),
                self._sample_fitted_entity(entity))
            entity["fit_error_mm"] = error
            return [entity], error
        return entities, worst

    def _fit_periodic_bspline(self, points_mm, tolerance, max_control_points):
        """Fit the simplest periodic cubic B-spline that meets a hard error."""
        import numpy as np
        from scipy.interpolate import splprep

        points = np.asarray(points_mm, dtype=float)
        if len(points) > 1 and np.linalg.norm(points[0] - points[-1]) <= 1e-12:
            points = points[:-1]
        if len(points) < 8:
            return None, {"reason": "fewer_than_8_source_points"}
        segment_lengths = np.linalg.norm(
            np.roll(points, -1, axis=0) - points, axis=1)
        perimeter = float(segment_lengths.sum())
        if perimeter <= 1e-9:
            return None, {"reason": "degenerate_closed_contour"}
        u = np.concatenate([[0.0], np.cumsum(segment_lengths[:-1])]) / perimeter
        minimum_rms = max(float(tolerance) / 80.0, 0.0005)
        maximum_rms = max(float(tolerance) * 1.5, minimum_rms * 2.0)
        rms_candidates = np.geomspace(minimum_rms, maximum_rms, 28)
        accepted = []
        evaluated = []
        for rms_target in rms_candidates:
            try:
                tck, _ = splprep(
                    [points[:, 0], points[:, 1]], u=u, per=True, k=3,
                    s=len(points) * float(rms_target) ** 2)
                scipy_knots = np.asarray(tck[0], dtype=float)
                scipy_controls = np.column_stack(tck[1]).astype(float)
                degree = 3
                if (len(scipy_controls) <= degree or
                        not np.allclose(scipy_controls[-degree:],
                                        scipy_controls[:degree],
                                        rtol=0.0, atol=1e-10)):
                    raise ValueError(
                        "SciPy periodic spline did not repeat its seam controls")
                # SolidWorks stores a periodic spline without SciPy's repeated
                # seam controls and accepts exactly control_points + 1 knots.
                controls = scipy_controls[:-degree]
                knots = scipy_knots[degree:-degree]
                if len(knots) != len(controls) + 1:
                    raise ValueError(
                        "Converted periodic B-spline has an invalid knot count")
                entity = {
                    "type": "b_spline",
                    "order": 4,
                    "periodic": True,
                    "knots": knots.tolist(),
                    "control_points": controls.tolist(),
                    "closed": True,
                    "fit_reason": (
                        "periodic cubic B-spline minimized CAD complexity "
                        "under a symmetric geometric error bound"),
                    "smoothing_rms_target_mm": float(rms_target),
                }
                sampled = self._sample_fitted_entity(
                    entity, step=max(float(tolerance) * 0.2, 0.01))
                error = self._symmetric_curve_error(points, sampled)
                entity["fit_error_mm"] = error
                control_count = len(controls)
                evaluated.append({
                    "control_points": control_count,
                    "fit_error_mm": error,
                    "rms_target_mm": float(rms_target),
                })
                if error <= tolerance and control_count <= max_control_points:
                    accepted.append(entity)
            except Exception as exc:
                evaluated.append({"error": str(exc),
                                  "rms_target_mm": float(rms_target)})
        if not accepted:
            geometric_passes = [item for item in evaluated
                                if item.get("fit_error_mm", float("inf")) <= tolerance]
            return None, {
                "reason": "accuracy_and_complexity_could_not_both_pass",
                "tolerance_mm": float(tolerance),
                "max_control_points": int(max_control_points),
                "best_geometric_pass": (min(
                    geometric_passes,
                    key=lambda item: item["control_points"])
                    if geometric_passes else None),
                "evaluated_candidates": len(evaluated),
            }
        chosen = min(
            accepted,
            key=lambda item: (len(item["control_points"]),
                              float(item["fit_error_mm"])))
        return chosen, {
            "reason": "accepted",
            "evaluated_candidates": len(evaluated),
            "accepted_candidates": len(accepted),
            "control_points": len(chosen["control_points"]),
            "fit_error_mm": float(chosen["fit_error_mm"]),
        }

    def _fit_open_bspline(self, points_mm, tolerance, max_control_points):
        """Fit the simplest non-periodic cubic B-spline under a hard error."""
        import numpy as np
        from scipy.interpolate import splprep

        points = np.asarray(points_mm, dtype=float)
        if len(points) < 4:
            return None, {"reason": "fewer_than_4_source_points"}
        lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
        total = float(lengths.sum())
        if total <= 1e-9:
            return None, {"reason": "degenerate_open_path"}
        u = np.concatenate([[0.0], np.cumsum(lengths)]) / total
        minimum_rms = max(float(tolerance) / 80.0, 0.0005)
        maximum_rms = max(float(tolerance) * 1.5, minimum_rms * 2.0)
        accepted = []
        evaluated = []
        for rms_target in np.geomspace(minimum_rms, maximum_rms, 28):
            try:
                tck, _ = splprep(
                    [points[:, 0], points[:, 1]], u=u, per=False, k=3,
                    s=len(points) * float(rms_target) ** 2)
                knots = np.asarray(tck[0], dtype=float)
                controls = np.column_stack(tck[1]).astype(float)
                if len(knots) != len(controls) + 4:
                    raise ValueError("SciPy returned an invalid open knot vector")
                entity = {
                    "type": "b_spline", "order": 4, "periodic": False,
                    "knots": knots.tolist(),
                    "control_points": controls.tolist(), "closed": False,
                    "fit_reason": (
                        "non-periodic cubic B-spline minimized CAD complexity "
                        "under a symmetric geometric error bound"),
                    "smoothing_rms_target_mm": float(rms_target),
                }
                sampled = self._sample_fitted_entity(
                    entity, step=max(float(tolerance) * 0.2, 0.01))
                error = self._symmetric_curve_error(points, sampled)
                entity["fit_error_mm"] = error
                evaluated.append({
                    "control_points": len(controls),
                    "fit_error_mm": error,
                    "rms_target_mm": float(rms_target),
                })
                if error <= tolerance and len(controls) <= max_control_points:
                    accepted.append(entity)
            except Exception as exc:
                evaluated.append({"error": str(exc),
                                  "rms_target_mm": float(rms_target)})
        if not accepted:
            return None, {
                "reason": "accuracy_and_complexity_could_not_both_pass",
                "tolerance_mm": float(tolerance),
                "max_control_points": int(max_control_points),
                "evaluated_candidates": len(evaluated),
            }
        chosen = min(
            accepted,
            key=lambda item: (len(item["control_points"]),
                              float(item["fit_error_mm"])))
        return chosen, {
            "reason": "accepted", "evaluated_candidates": len(evaluated),
            "accepted_candidates": len(accepted),
            "control_points": len(chosen["control_points"]),
            "fit_error_mm": float(chosen["fit_error_mm"]),
        }

    @staticmethod
    def _curve_complexity(entities, entity_weight=24.0):
        fit_points = sum(len(entity.get("fit_points", []))
                         for entity in entities)
        control_points = sum(len(entity.get("control_points", []))
                             for entity in entities)
        return {
            "entities": len(entities),
            "fit_points": fit_points,
            "control_points": control_points,
            "score": (len(entities) * float(entity_weight) +
                      fit_points + control_points),
        }

    def _materialize_explicit_splines(self, entities, source_points,
                                      max_error, approximation):
        """Replace interpolating splines with bounded explicit cubic NURBS."""
        import numpy as np

        tolerance = float(approximation.get(
            "explicit_spline_tolerance_mm", float(max_error) * 0.8))
        tolerance = max(1e-4, min(float(max_error), tolerance))
        per_spline_cap = int(approximation.get(
            "max_control_points_per_spline", 64))
        total_cap = int(approximation.get(
            "max_total_control_points", 1000000))
        converted = []
        diagnostics = []
        for index, entity in enumerate(entities):
            if entity.get("type") != "spline":
                converted.append(dict(entity))
                continue
            dense = self._sample_fitted_entity(
                entity, step=max(0.002, min(0.01, tolerance * 0.08)))
            if bool(entity.get("closed", False)):
                fitted, report = self._fit_periodic_bspline(
                    dense, tolerance, per_spline_cap)
            else:
                fitted, report = self._fit_open_bspline(
                    dense, tolerance, per_spline_cap)
            if fitted is None:
                return None, {
                    "reason": "explicit_spline_fit_failed",
                    "entity_index": index,
                    "tolerance_mm": tolerance,
                    "fit": report,
                }
            fitted.update({
                key: value for key, value in entity.items()
                if key not in {"type", "fit_points", "closed",
                               "fit_error_mm"}
            })
            fitted["closed"] = bool(entity.get("closed", False))
            fitted["source_fit_point_count"] = len(
                entity.get("fit_points", []))
            fitted["explicit_spline_fit"] = report
            fitted["fit_reason"] = (
                f"{entity.get('fit_reason', 'free-form curve')}; "
                "materialized as an explicit bounded cubic NURBS")
            converted.append(fitted)
            diagnostics.append({
                "entity_index": index,
                "control_points": len(fitted.get("control_points", [])),
                "fit_error_mm": float(fitted.get("fit_error_mm", 0.0)),
            })
        total_controls = sum(len(entity.get("control_points", []))
                             for entity in converted)
        if total_controls > total_cap:
            return None, {
                "reason": "explicit_spline_total_control_budget_exceeded",
                "total_control_points": total_controls,
                "max_total_control_points": total_cap,
                "tolerance_mm": tolerance,
            }
        if not diagnostics:
            explicit_entities = [entity for entity in converted
                                 if entity.get("type") == "b_spline"]
            return converted, {
                "reason": "already_explicit",
                "tolerance_mm": tolerance,
                "converted_splines": len(explicit_entities),
                "total_control_points": total_controls,
                "max_control_points_per_spline": max([
                    len(entity.get("control_points", []))
                    for entity in explicit_entities] or [0]),
                "max_local_fit_error_mm": max([
                    float(entity.get("fit_error_mm", 0.0))
                    for entity in explicit_entities] or [0.0]),
                "global_fit_error_mm": 0.0,
                "entities": [],
            }
        source_dense = self._resample_closed(
            source_points, max(0.002, min(0.01, tolerance * 0.08)))
        samples = [self._sample_fitted_entity(
            entity, step=max(0.002, min(0.01, tolerance * 0.08)))
            for entity in converted]
        candidate = np.vstack([points for points in samples if len(points)])
        global_error = self._symmetric_curve_error(source_dense, candidate)
        if global_error > float(max_error):
            return None, {
                "reason": "explicit_spline_global_error_exceeded",
                "global_fit_error_mm": global_error,
                "max_error_mm": float(max_error),
                "tolerance_mm": tolerance,
            }
        return converted, {
            "reason": "accepted",
            "tolerance_mm": tolerance,
            "converted_splines": len(diagnostics),
            "total_control_points": total_controls,
            "max_control_points_per_spline": max([
                item["control_points"] for item in diagnostics] or [0]),
            "max_local_fit_error_mm": max([
                item["fit_error_mm"] for item in diagnostics] or [0.0]),
            "global_fit_error_mm": global_error,
            "entities": diagnostics,
        }

    @staticmethod
    def _cad_commit_profile(entities, output_mode):
        """Estimate creation plus mandatory post-COM verification cost."""
        complexity = ImageSketchOperations._curve_complexity(entities)
        fit_counts = [len(entity.get("fit_points", []))
                      for entity in entities]
        control_counts = [len(entity.get("control_points", []))
                          for entity in entities]
        fit_square_sum = sum(count * count for count in fit_counts)
        control_square_sum = sum(count * count for count in control_counts)
        # Live SW2026 SP2.1 measurements: one 372-control equation spline
        # consumed 565.9 s, while 367 controls split across 46 splines were
        # practical. Model the equation-param API per entity, not by total.
        creation_estimate = (5.0 + complexity["entities"] * 0.15 +
                             control_square_sum * 0.0042 +
                             fit_square_sum * 0.0005)
        if output_mode == "locked_trace":
            creation_estimate += (
                complexity["control_points"] * 0.02 +
                complexity["fit_points"] * 0.01 +
                complexity["entities"] * 0.5)
        # Explicit NURBS are already validated before COM. Post-COM work reads
        # persistent IDs, types, construction flags, and endpoints only; exact
        # submitted parameters are sampled locally for reverse rasterization.
        verification_estimate = (
            4.0 + complexity["entities"] * 0.05 +
            complexity["control_points"] * 0.001)
        estimate = creation_estimate + verification_estimate
        return {
            "estimated_sec": round(estimate, 3),
            "creation_estimated_sec": round(creation_estimate, 3),
            "verification_estimated_sec": round(
                verification_estimate, 3),
            "fit_point_square_sum": fit_square_sum,
            "control_point_square_sum": control_square_sum,
            "max_fit_points_per_entity": max(fit_counts, default=0),
            "max_control_points_per_entity": max(control_counts, default=0),
            "calibration": (
                "SW2026_SP2.1_deterministic_nurbs_bounded_readback"),
        }

    @staticmethod
    def _estimate_vector_commit_seconds(entities, output_mode):
        """Return the conservative synchronous COM estimate in seconds."""
        return ImageSketchOperations._cad_commit_profile(
            entities, output_mode)["estimated_sec"]

    def _fit_loop_hybrid(self, points_mm, max_error, prefer, max_entities,
                         approximation=None):
        approximation = approximation or {}
        strategy = approximation.get("curve_strategy", "hybrid_primitives")
        simplification_tolerance = float(
            approximation.get("simplification_tolerance_mm", max_error))
        max_fit_points = int(
            approximation.get("max_total_fit_points", 1000000))
        max_control_points = int(
            approximation.get("max_total_control_points", 1000000))
        max_control_points_per_spline = int(
            approximation.get("max_control_points_per_spline", 64))
        entity_weight = float(
            approximation.get("entity_complexity_weight", 24.0))

        hybrid_entities, hybrid_error = self._fit_loop_hybrid_primitives(
            points_mm, max_error, prefer, max_entities,
            approximation=approximation)
        explicit_entities, explicit_diagnostics = (
            self._materialize_explicit_splines(
                hybrid_entities, points_mm, max_error, approximation))
        if explicit_entities is not None:
            hybrid_entities = explicit_entities
            hybrid_error = max(
                float(hybrid_error),
                float(explicit_diagnostics["global_fit_error_mm"]))
            for entity in hybrid_entities:
                entity["explicit_spline_materialization"] = {
                    key: value for key, value in explicit_diagnostics.items()
                    if key != "entities"}
        hybrid_complexity = self._curve_complexity(
            hybrid_entities, entity_weight)
        hybrid_valid = (
            explicit_entities is not None and
            hybrid_error <= max_error and
            hybrid_complexity["entities"] <= max_entities and
            hybrid_complexity["fit_points"] <= max_fit_points and
            hybrid_complexity["control_points"] <= max_control_points)

        if strategy == "hybrid_primitives" or "spline" not in prefer:
            if not hybrid_valid:
                raise ValueError(
                    "Hybrid primitive fit exceeded the accuracy or complexity "
                    f"budget: {hybrid_complexity}, fit_error_mm={hybrid_error}, "
                    f"explicit_splines={explicit_diagnostics}")
            for entity in hybrid_entities:
                entity["curve_strategy"] = "hybrid_primitives"
            return hybrid_entities, hybrid_error

        dense = self._resample_closed(
            points_mm, max(float(simplification_tolerance) * 0.2, 0.01))
        b_spline, b_spline_diagnostics = self._fit_periodic_bspline(
            dense, simplification_tolerance,
            min(max_control_points, max_control_points_per_spline))
        if b_spline is not None:
            b_spline["optimizer_diagnostics"] = b_spline_diagnostics
            b_spline["curve_strategy"] = "periodic_bspline"
            b_entities = [b_spline]
            b_complexity = self._curve_complexity(b_entities, entity_weight)
            b_valid = (b_spline["fit_error_mm"] <= max_error and
                       b_complexity["control_points"] <= max_control_points and
                       max(len(entity.get("control_points", []))
                           for entity in b_entities) <=
                       max_control_points_per_spline)
        else:
            b_entities = []
            b_complexity = None
            b_valid = False

        if strategy == "periodic_bspline":
            if not b_valid:
                raise ValueError(
                    "Periodic B-spline could not satisfy the accuracy and "
                    f"complexity budgets: {b_spline_diagnostics}")
            return b_entities, float(b_spline["fit_error_mm"])

        candidates = []
        output_mode = approximation.get("output_mode", "locked_trace")
        if hybrid_valid:
            candidates.append((self._estimate_vector_commit_seconds(
                                   hybrid_entities, output_mode),
                               hybrid_complexity["score"],
                               hybrid_entities, hybrid_error,
                               "hybrid_primitives", hybrid_complexity))
        if b_valid:
            candidates.append((self._estimate_vector_commit_seconds(
                                   b_entities, output_mode),
                               b_complexity["score"], b_entities,
                               float(b_spline["fit_error_mm"]),
                               "periodic_bspline", b_complexity))
        if not candidates:
            raise ValueError(
                "No curve representation satisfied both geometric accuracy "
                "and CAD complexity budgets; "
                f"hybrid={hybrid_complexity}, "
                f"explicit_splines={explicit_diagnostics}, "
                f"b_spline={b_spline_diagnostics}")
        estimated_commit_sec, _, chosen_entities, chosen_error, chosen_strategy, complexity = min(
            candidates, key=lambda item: (item[0], item[1], item[3]))
        for entity in chosen_entities:
            entity["curve_strategy"] = chosen_strategy
            entity["curve_complexity"] = complexity
            entity["estimated_commit_sec"] = estimated_commit_sec
        return chosen_entities, chosen_error

    @staticmethod
    def _resample_open(points, step):
        import numpy as np
        points = np.asarray(points, dtype=float)
        if len(points) < 2:
            return points
        lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
        cumulative = np.concatenate([[0.0], np.cumsum(lengths)])
        count = max(2, int(math.ceil(cumulative[-1] /
                                     max(step, 1e-9))) + 1)
        targets = np.linspace(0.0, cumulative[-1], count)
        return np.column_stack([
            np.interp(targets, cumulative, points[:, 0]),
            np.interp(targets, cumulative, points[:, 1]),
        ])

    def _fit_open_path_hybrid(self, points_mm, max_error, prefer,
                              max_fit_points, max_control_points,
                              approximation=None):
        """Fit one graph edge without manufacturing extra CAD entities."""
        import numpy as np

        approximation = approximation or {}
        dense = self._resample_open(
            points_mm, max(float(max_error) * 0.25, 0.01))
        if len(dense) < 2:
            raise ValueError("Open path has fewer than two distinct points")
        line = self._line_fit(dense)
        if "line" in prefer and line and line["max_error"] <= max_error:
            return [{
                "type": "line", "start": line["start"], "end": line["end"],
                "fit_reason": "whole graph edge passed robust line fit",
                "fit_error_mm": line["max_error"],
                "curve_strategy": "open_path_single_entity",
            }], float(line["max_error"])
        arc = self._circle_fit(dense)
        if ("arc" in prefer and arc and arc["max_error"] <= max_error and
                arc["monotonic"] >= 0.97 and
                math.radians(3) <= arc["sweep"] <= math.radians(330)):
            return [{
                "type": "arc", "center": arc["center"],
                "start": arc["start"], "end": arc["end"],
                "direction": arc["direction"],
                "fit_reason": "whole graph edge passed robust arc fit",
                "fit_error_mm": arc["max_error"],
                "curve_strategy": "open_path_single_entity",
            }], float(arc["max_error"])
        if "spline" not in prefer:
            raise ValueError(
                "Open path is neither a line nor an arc within tolerance and "
                "approximation.prefer excludes spline")
        tolerance = max_error * float(
            approximation.get("spline_fit_tolerance_ratio", 0.75))
        b_spline_tolerance = float(
            approximation.get("simplification_tolerance_mm", max_error))
        b_spline, diagnostics = self._fit_open_bspline(
            dense, b_spline_tolerance, max(4, int(max_control_points)))
        if b_spline is not None:
            b_spline["optimizer_diagnostics"] = diagnostics
            b_spline["curve_strategy"] = "open_path_single_entity"
            if float(b_spline["fit_error_mm"]) <= max_error:
                return [b_spline], float(b_spline["fit_error_mm"])
        fit_points = self._regularized_spline(
            dense, tolerance,
            max_fit_points=max(4, int(max_fit_points)), closed=False,
            target_segment_length=approximation.get("target_segment_length_mm"),
            max_segment_length=approximation.get("max_segment_length_mm"))
        entity = {
            "type": "spline", "fit_points": fit_points, "closed": False,
            "fit_reason": (
                "line, arc, and bounded-control B-spline could not satisfy the "
                "hard tolerance; one adaptive open spline preserved the edge"),
            "curve_strategy": "open_path_single_entity",
            "optimizer_diagnostics": diagnostics,
        }
        sampled = self._sample_fitted_entity(
            entity, step=max(float(max_error) * 0.2, 0.01))
        error = self._symmetric_curve_error(dense, sampled)
        entity["fit_error_mm"] = error
        if error > max_error:
            raise ValueError(
                f"Open spline fit_error_mm={error:.6g} exceeds "
                f"max_error_mm={max_error:.6g}")
        return [entity], float(error)

    @staticmethod
    def _calibration_scale(loops, calibration):
        all_points = [point for loop in loops for point in loop["points"]]
        min_x = min(p[0] for p in all_points); max_x = max(p[0] for p in all_points)
        min_y = min(p[1] for p in all_points); max_y = max(p[1] for p in all_points)
        mode = calibration.get("mode")
        if mode == "bbox_height":
            pixels = max_y - min_y
            value = float(calibration["value"])
        elif mode == "bbox_width":
            pixels = max_x - min_x
            value = float(calibration["value"])
        elif mode == "two_points":
            a, b = calibration["pixel_points"]
            pixels = math.dist(a, b)
            value = float(calibration.get("distance", calibration.get("value")))
        elif mode in {"mm_per_pixel", "scale"}:
            return float(calibration.get("value", calibration.get("mm_per_pixel")))
        else:
            raise ValueError("Calibration mode must be bbox_height, bbox_width, "
                             "two_points, or mm_per_pixel")
        if pixels <= 0 or value <= 0:
            raise ValueError("Calibration distance must be positive")
        return value / pixels

    @staticmethod
    def _image_anchor(loops, placement, image_shape):
        all_points = [point for loop in loops if loop["role"] == "outer"
                      for point in loop["points"]]
        if not all_points:
            all_points = [point for loop in loops
                          for point in loop["points"]]
        min_x = min(p[0] for p in all_points); max_x = max(p[0] for p in all_points)
        min_y = min(p[1] for p in all_points); max_y = max(p[1] for p in all_points)
        anchor = placement.get("image_anchor", "silhouette_bottom_center")
        if isinstance(anchor, (list, tuple)):
            return [float(anchor[0]), float(anchor[1])]
        if anchor == "silhouette_bottom_center":
            return [(min_x + max_x) / 2.0, max_y]
        if anchor == "silhouette_bbox_center":
            return [(min_x + max_x) / 2.0, (min_y + max_y) / 2.0]
        if anchor == "image_center":
            return [image_shape[1] / 2.0, image_shape[0] / 2.0]
        if anchor == "centroid":
            return [sum(p[0] for p in all_points) / len(all_points),
                    sum(p[1] for p in all_points) / len(all_points)]
        if anchor == "pixel_point":
            return list(map(float, placement["pixel_point"]))
        raise ValueError(f"Unknown image_anchor '{anchor}'")

    @staticmethod
    def _pixel_to_sketch_matrix(scale, anchor_px, placement):
        import numpy as np
        angle = math.radians(float(placement.get("rotation_deg", 0.0)))
        sx = -scale if placement.get("mirror_x", False) else scale
        # Pixel Y points down; sketch Y points up before optional mirroring.
        sy = scale if placement.get("mirror_y", False) else -scale
        linear = np.array([[math.cos(angle), -math.sin(angle)],
                           [math.sin(angle), math.cos(angle)]]) @ np.diag([sx, sy])
        model_anchor = placement.get("model_anchor", [0.0, 0.0, 0.0])
        target = np.asarray(model_anchor[:2], dtype=float)
        translation = target - linear @ np.asarray(anchor_px, dtype=float)
        matrix = np.array([[linear[0, 0], linear[0, 1], translation[0]],
                           [linear[1, 0], linear[1, 1], translation[1]],
                           [0.0, 0.0, 1.0]])
        return matrix

    @staticmethod
    def _apply_matrix(points, matrix):
        import numpy as np
        points = np.asarray(points, dtype=float)
        homogeneous = np.column_stack([points, np.ones(len(points))])
        return (homogeneous @ matrix.T)[:, :2]

    @staticmethod
    def _sample_fitted_entity(entity, step=0.05):
        import numpy as np
        if entity["type"] == "line":
            a, b = np.asarray(entity["start"]), np.asarray(entity["end"])
            count = max(2, int(math.ceil(np.linalg.norm(b - a) / step)) + 1)
            return np.linspace(a, b, count)
        if entity["type"] == "circle":
            radius = float(entity["radius"])
            count = max(32, int(math.ceil(2 * math.pi * radius / step)))
            angles = np.linspace(0, 2 * math.pi, count, endpoint=False)
            center = np.asarray(entity["center"])
            return center + np.column_stack([np.cos(angles), np.sin(angles)]) * radius
        if entity["type"] == "arc":
            center = np.asarray(entity["center"])
            start = np.asarray(entity["start"]); end = np.asarray(entity["end"])
            radius = np.linalg.norm(start - center)
            a0 = math.atan2(*(start - center)[::-1])
            a1 = math.atan2(*(end - center)[::-1])
            if entity.get("direction", 1) > 0:
                delta = (a1 - a0) % (2 * math.pi)
            else:
                delta = -((a0 - a1) % (2 * math.pi))
            count = max(4, int(math.ceil(abs(delta) * radius / step)) + 1)
            angles = np.linspace(a0, a0 + delta, count)
            return center + np.column_stack([np.cos(angles), np.sin(angles)]) * radius
        if entity["type"] == "b_spline":
            try:
                from scipy.interpolate import splev
                knots = np.asarray(entity["knots"], dtype=float)
                controls = np.asarray(entity["control_points"], dtype=float)
                order = int(entity.get("order", 4))
                degree = order - 1
                periodic = bool(entity.get("periodic", False))
                expected_knots = (len(controls) + 1 if periodic
                                  else len(controls) + order)
                if len(controls) < order or len(knots) != expected_knots:
                    return controls
                if periodic:
                    period = float(knots[-1] - knots[0])
                    if period <= 0.0:
                        return controls
                    scipy_knots = np.concatenate([
                        knots[-(degree + 1):-1] - period,
                        knots,
                        knots[1:degree + 1] + period,
                    ])
                    scipy_controls = np.vstack(
                        [controls, controls[:degree]])
                else:
                    scipy_knots = knots
                    scipy_controls = controls
                domain_start = float(scipy_knots[degree])
                domain_end = float(scipy_knots[-degree - 1])
                probe = np.column_stack(splev(
                    np.linspace(domain_start, domain_end, 1024,
                                endpoint=False),
                    (scipy_knots,
                     [scipy_controls[:, 0], scipy_controls[:, 1]], degree)))
                estimated_length = float(np.linalg.norm(
                    np.roll(probe, -1, axis=0) - probe, axis=1).sum())
                count = max(
                    256, len(controls) * 8,
                    int(math.ceil(estimated_length / max(step, 1e-9))))
                return np.column_stack(splev(
                    np.linspace(domain_start, domain_end, count,
                                endpoint=not periodic),
                    (scipy_knots,
                     [scipy_controls[:, 0], scipy_controls[:, 1]], degree)))
            except Exception:
                return np.asarray(entity.get("control_points", []), dtype=float)
        points = np.asarray(entity.get("fit_points", []), dtype=float)
        if len(points) < 4:
            return points
        try:
            from scipy.interpolate import splprep, splev
            closed = bool(entity.get(
                "closed", np.linalg.norm(points[0] - points[-1]) <= 1e-12))
            if closed:
                points = points[:-1]
            tck, _ = splprep([points[:, 0], points[:, 1]], s=0,
                             per=closed, k=min(3, len(points) - 1))
            estimated_length = float(np.linalg.norm(
                np.diff(points, axis=0), axis=1).sum())
            if closed:
                estimated_length += float(np.linalg.norm(
                    points[0] - points[-1]))
            count = max(
                50, len(points) * 12,
                int(math.ceil(estimated_length / max(step, 1e-9))))
            return np.column_stack(splev(
                np.linspace(0, 1, count, endpoint=not closed), tck))
        except Exception:
            return points

    def _rasterize_fitted(self, fitted_loops, matrix_px_to_mm, shape):
        import cv2
        import numpy as np
        inverse = np.linalg.inv(matrix_px_to_mm)
        # Supersampling prevents integer polygon rounding from dominating a
        # sub-pixel vector fit and makes the reverse-raster gate reproducible.
        supersample = 4
        raster_high = np.zeros(
            (shape[0] * supersample, shape[1] * supersample), dtype=np.uint8)
        loop_pixels = []
        for loop in fitted_loops:
            samples = []
            for entity in loop["entities"]:
                part = self._sample_fitted_entity(entity)
                if len(part):
                    if samples and np.linalg.norm(samples[-1][-1] - part[0]) < 1e-6:
                        part = part[1:]
                    samples.append(part)
            if not samples:
                continue
            samples_mm = np.vstack(samples)
            pixels = self._apply_matrix(samples_mm, inverse)
            polygon = np.round(pixels * supersample).astype(np.int32)
            if len(polygon) >= 3:
                color = 255 if loop["role"] == "outer" else 0
                cv2.fillPoly(raster_high, [polygon], color)
                loop_pixels.append({"role": loop["role"], "points": pixels})
        coverage = cv2.resize(
            raster_high, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
        raster = (coverage >= 128).astype(np.uint8) * 255
        return raster, loop_pixels

    def _rasterize_fitted_paths(self, fitted_paths, matrix_px_to_mm, shape):
        import cv2
        import numpy as np

        inverse = np.linalg.inv(matrix_px_to_mm)
        supersample = 4
        raster_high = np.zeros(
            (shape[0] * supersample, shape[1] * supersample), dtype=np.uint8)
        path_pixels = []
        for path in fitted_paths:
            samples = []
            for entity in path["entities"]:
                part = self._sample_fitted_entity(entity)
                if len(part):
                    samples.append(part)
            if not samples:
                continue
            pixels = self._apply_matrix(np.vstack(samples), inverse)
            polyline = np.round(pixels * supersample).astype(np.int32)
            if len(polyline) >= 2:
                cv2.polylines(
                    raster_high, [polyline], bool(path.get("closed")), 255,
                    supersample, lineType=cv2.LINE_AA)
                path_pixels.append({
                    "role": path["role"], "closed": bool(path.get("closed")),
                    "points": pixels,
                })
        coverage = cv2.resize(
            raster_high, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
        return (coverage >= 96).astype(np.uint8) * 255, path_pixels

    def _sample_committed_entity(self, actual, source, step=0.025):
        """Sample exported CAD while preserving the source arc traversal."""
        import numpy as np

        source_type = source.get("type")
        tessellation = np.asarray(
            actual.get("evaluation_points") or
            actual.get("tessellation_points") or [], dtype=float)
        if len(tessellation) >= 2:
            if (actual.get("commit_conversion") ==
                    "batched_composite_nurbs" and bool(
                        (actual.get("curve_evaluation") or {}).get(
                            "orientation_reversed", False))):
                tessellation = tessellation[::-1]
            source_points = source.get("fit_points") or source.get("points") or []
            if source_points:
                source_start = np.asarray(source_points[0], dtype=float)[:2]
                if (np.linalg.norm(tessellation[-1] - source_start) <
                        np.linalg.norm(tessellation[0] - source_start)):
                    tessellation = tessellation[::-1]
            return tessellation
        if source_type == "arc":
            center = np.asarray(actual.get("center"), dtype=float)
            start = np.asarray(actual.get("start"), dtype=float)
            end = np.asarray(actual.get("end"), dtype=float)
            if center.shape != (2,) or start.shape != (2,) or end.shape != (2,):
                return np.empty((0, 2), dtype=float)
            radius = float(np.linalg.norm(start - center))
            start_angle = math.atan2(*(start - center)[::-1])
            end_angle = math.atan2(*(end - center)[::-1])
            if int(source.get("direction", 1)) > 0:
                delta = (end_angle - start_angle) % (2 * math.pi)
            else:
                delta = -((start_angle - end_angle) % (2 * math.pi))
            count = max(
                8, int(math.ceil(abs(delta) * radius / max(step, 1e-9))) + 1)
            angles = np.linspace(start_angle, start_angle + delta, count)
            return center + np.column_stack([
                np.cos(angles), np.sin(angles)]) * radius
        if source_type == "circle":
            center = np.asarray(actual.get("center"), dtype=float)
            radius = float(actual.get("radius", source.get("radius", 0.0)))
            if center.shape != (2,) or radius <= 0.0:
                return np.empty((0, 2), dtype=float)
            count = max(
                32, int(math.ceil(2 * math.pi * radius / max(step, 1e-9))))
            angles = np.linspace(0.0, 2 * math.pi, count, endpoint=False)
            return center + np.column_stack([
                np.cos(angles), np.sin(angles)]) * radius
        # Global geometry comparisons intentionally omit construction curves.
        # Post-COM validation must still sample them as real CAD geometry.
        sampled_actual = dict(actual)
        sampled_actual["construction"] = False
        points, _ = self._sample_geometry(
            {"entities": [sampled_actual]}, max(step, 1e-9))
        return np.asarray(points, dtype=float)

    def _rasterize_committed_geometry(self, geometry, source_loops,
                                      matrix_px_to_mm, shape, line_mode,
                                      deadline_monotonic=None):
        """Reverse-rasterize exact exported SolidWorks primitives."""
        import cv2
        import numpy as np

        actual_by_id = {
            str(entity.get("id")): entity
            for entity in geometry.get("entities", [])
        }
        expected_ids = [
            str(entity.get("id"))
            for loop in source_loops
            for entity in (loop.get("entities") or [])
        ]
        missing = sorted(set(expected_ids) - set(actual_by_id))
        extra = sorted(set(actual_by_id) - set(expected_ids))
        metadata_mismatches = []
        expected_type_map = {
            "b_spline": "spline", "spline": "spline",
            "circle": "arc", "arc": "arc", "line": "line",
        }
        for loop in source_loops:
            for source in loop.get("entities") or []:
                entity_id = str(source.get("id"))
                actual = actual_by_id.get(entity_id)
                if actual is None:
                    continue
                expected_type = expected_type_map.get(source.get("type"))
                if (actual.get("commit_conversion") ==
                        "batched_composite_nurbs"):
                    expected_type = "spline"
                if (expected_type is not None and
                        actual.get("type") != expected_type):
                    metadata_mismatches.append({
                        "id": entity_id, "field": "type",
                        "expected": expected_type,
                        "actual": actual.get("type"),
                    })
                if bool(actual.get("construction")) != bool(
                        source.get("construction")):
                    metadata_mismatches.append({
                        "id": entity_id, "field": "construction",
                        "expected": bool(source.get("construction")),
                        "actual": bool(actual.get("construction")),
                    })
        if missing:
            raise ValueError(
                f"Committed sketch is missing entity IDs: {missing[:8]}")
        inverse = np.linalg.inv(np.asarray(matrix_px_to_mm, dtype=float))
        supersample = 4
        raster_high = np.zeros(
            (shape[0] * supersample, shape[1] * supersample),
            dtype=np.uint8)
        sampled_entities = 0
        for loop in source_loops:
            samples = []
            for source in loop.get("entities") or []:
                if (deadline_monotonic is not None and
                        time.monotonic() >= float(deadline_monotonic)):
                    raise TimeoutError(
                        "Committed CAD rasterization exceeded its deadline")
                actual = actual_by_id[str(source.get("id"))]
                part = self._sample_committed_entity(actual, source)
                if len(part) < 2:
                    continue
                if (samples and np.linalg.norm(
                        samples[-1][-1] - part[0]) < 1e-8):
                    part = part[1:]
                if len(part):
                    samples.append(part)
                    sampled_entities += 1
            if not samples:
                continue
            pixels = self._apply_matrix(np.vstack(samples), inverse)
            polyline = np.round(pixels * supersample).astype(np.int32)
            if line_mode:
                cv2.polylines(
                    raster_high, [polyline], bool(loop.get("closed")), 255,
                    supersample, lineType=cv2.LINE_AA)
            elif len(polyline) >= 3:
                color = 255 if loop.get("role") == "outer" else 0
                cv2.fillPoly(raster_high, [polyline], color)
        coverage = cv2.resize(
            raster_high, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
        threshold = 96 if line_mode else 128
        candidate = (coverage >= threshold).astype(np.uint8) * 255
        return candidate, {
            "expected_entities": len(expected_ids),
            "exported_entities": len(actual_by_id),
            "sampled_entities": sampled_entities,
            "missing_entity_ids": missing,
            "extra_entity_ids": extra,
            "metadata_mismatches": metadata_mismatches,
        }

    def _validate_committed_geometry(self, sketch_name, payload,
                                     vector_path, validation,
                                     deadline_monotonic=None):
        """Run the mandatory post-COM reverse-raster quality gate."""
        import cv2
        import numpy as np

        reference_path = payload.get("reference_raster")
        if not reference_path or not os.path.isfile(reference_path):
            return {
                "pass": False,
                "error": "Offline analysis did not preserve a reference raster",
                "reference_raster": reference_path,
            }
        reference = cv2.imread(reference_path, cv2.IMREAD_GRAYSCALE)
        expected_shape = tuple(int(value) for value in payload.get(
            "image_shape", []))
        if (reference is None or len(expected_shape) != 2 or
                reference.shape != expected_shape):
            return {
                "pass": False,
                "error": "Reference raster is unreadable or has the wrong shape",
                "reference_raster": reference_path,
            }
        if (deadline_monotonic is not None and
                time.monotonic() >= float(deadline_monotonic)):
            return {
                "pass": False, "budget_exceeded": True,
                "error": "Post-COM validation deadline expired before export",
            }
        approximation = payload.get("approximation") or {}
        max_error = float(approximation.get("max_error_mm", 0.15))
        chord_tolerance = float(validation.get(
            "cad_tessellation_chord_tolerance_mm",
            min(0.025, max_error / 4.0)))
        try:
            export_result, geometry = self._load_geometry_payload(
                sketch_name, "mm", include={
                    "relations": False, "dimensions": False,
                    "equations": False, "topology": False,
                    "constraint_status": False,
                    "spline_export_mode": "deterministic_source_nurbs",
                    "spline_fit_points": False,
                    "spline_chord_tolerance_mm": chord_tolerance,
                    "spline_endpoint_tolerance_mm": float(validation.get(
                        "cad_endpoint_tolerance_mm", 0.002)),
                    "deadline_monotonic": deadline_monotonic,
                })
        except TimeoutError as exc:
            return {
                "pass": False, "budget_exceeded": True,
                "error": str(exc),
            }
        except Exception as exc:
            return {
                "pass": False,
                "error": f"Deterministic spline verification failed: {exc}",
            }
        if geometry is None:
            return {
                "pass": False,
                "error": "Committed CAD geometry could not be exported",
                "export_result": export_result,
            }
        trace_mode = str((payload.get("trace") or {}).get("mode", ""))
        line_mode = trace_mode in {
            "all_visible_edges", "stroke_centerlines", "stroke_edges"}
        try:
            candidate, roundtrip = self._rasterize_committed_geometry(
                geometry, payload.get("loops") or [],
                payload.get("pixel_to_sketch"), reference.shape, line_mode,
                deadline_monotonic=deadline_monotonic)
        except TimeoutError as exc:
            return {
                "pass": False, "budget_exceeded": True,
                "error": str(exc),
            }
        except Exception as exc:
            return {
                "pass": False,
                "error": f"Committed CAD rasterization failed: {exc}",
            }
        roundtrip_pass = bool(
            roundtrip["expected_entities"] == roundtrip["exported_entities"] ==
            roundtrip["sampled_entities"] and
            not roundtrip["missing_entity_ids"] and
            not roundtrip["extra_entity_ids"] and
            not roundtrip["metadata_mismatches"])
        curve_reports = [
            entity.get("curve_evaluation") or {}
            for entity in geometry.get("entities", [])
            if entity.get("type") == "spline"]
        curve_sampling = {
            "source": (
                "ISplineParamData deterministic commit parameters + "
                "local_adaptive_de_boor"),
            "spline_count": len(curve_reports),
            "evaluation_count": sum(int(report.get(
                "evaluation_count", 0)) for report in curve_reports),
            "output_point_count": sum(int(report.get(
                "output_point_count", 0)) for report in curve_reports),
            "accepted_max_chord_error_mm": max([
                float(report.get("accepted_max_chord_error_mm", 0.0))
                for report in curve_reports] or [0.0]),
            "endpoint_max_error_mm": max([
                float(report.get("endpoint_max_error_mm", 0.0))
                for report in curve_reports] or [0.0]),
        }
        scale = float(payload.get("scale_mm_per_px", 0.0))
        thresholds = dict(payload.get("thresholds") or {})
        if line_mode:
            support_radius = float(validation.get(
                "reverse_line_support_radius_px", 1.5))
            metrics = self._line_metrics(
                reference, candidate, scale,
                support_radius_px=support_radius)
            passed = bool(roundtrip_pass and
                metrics["balanced_support"] >= float(
                    thresholds.get("min_line_support", 0.95)) and
                metrics["hausdorff_mm"] <= float(
                    thresholds.get("max_hausdorff_mm", 0.5)))
        else:
            metrics = self._mask_metrics(reference, candidate, scale)
            passed = bool(roundtrip_pass and
                metrics["iou"] >= float(thresholds.get("min_iou", 0.985)) and
                metrics["hausdorff_mm"] <= float(
                    thresholds.get("max_hausdorff_mm", 0.3)))
        artifact_dir = Path(vector_path).parent
        overlay_path = str(artifact_dir / f"{sketch_name}.cad-overlay.png")
        candidate_path = str(artifact_dir / f"{sketch_name}.cad-raster.png")
        rgb, _ = self._load_image(
            payload.get("verification_image") or payload.get("source"))
        self._save_overlay(rgb, reference, candidate, overlay_path)
        if not cv2.imwrite(candidate_path, candidate):
            return {
                "pass": False,
                "error": "Could not save the committed CAD raster",
                "metrics": metrics,
                "roundtrip": roundtrip,
            }
        self._runtime.increment("verification_artifacts", 2)
        return {
            "pass": passed,
            "stage": "post_commit_cad_reverse_raster",
            "verification_method": (
                "deterministic ISplineParamData commit + exact persistent-ID, "
                "type, construction, and endpoint read-back + local adaptive de Boor"),
            "cad_tessellation_chord_tolerance_mm": chord_tolerance,
            "curve_sampling": curve_sampling,
            "line_mode": line_mode,
            "metrics": metrics,
            "thresholds": thresholds,
            "roundtrip": roundtrip,
            "roundtrip_pass": roundtrip_pass,
            "reference_raster": reference_path,
            "cad_raster": candidate_path,
            "overlay": overlay_path,
        }

    @staticmethod
    def _line_metrics(reference, candidate, scale_mm_per_px,
                      support_radius_px=1.5):
        import numpy as np
        from scipy.ndimage import distance_transform_edt

        ref = reference > 0
        cand = candidate > 0
        if not ref.any() or not cand.any():
            return {
                "iou": 0.0, "reference_support": 0.0,
                "candidate_support": 0.0, "balanced_support": 0.0,
                "mean_mm": float("inf"), "p95_mm": float("inf"),
                "hausdorff_mm": float("inf"), "max_mm": float("inf"),
            }
        ref_field = distance_transform_edt(~ref)
        cand_field = distance_transform_edt(~cand)
        cand_to_ref = ref_field[cand]
        ref_to_cand = cand_field[ref]
        distances_px = np.concatenate([cand_to_ref, ref_to_cand])
        intersection = int(np.logical_and(ref, cand).sum())
        union = int(np.logical_or(ref, cand).sum())
        reference_support = float((ref_to_cand <= support_radius_px).mean())
        candidate_support = float((cand_to_ref <= support_radius_px).mean())
        return {
            "iou": float(intersection / max(1, union)),
            "reference_support": reference_support,
            "candidate_support": candidate_support,
            "balanced_support": min(reference_support, candidate_support),
            "support_radius_px": float(support_radius_px),
            "mean_mm": float(distances_px.mean() * scale_mm_per_px),
            "p95_mm": float(np.percentile(distances_px, 95) * scale_mm_per_px),
            "hausdorff_mm": float(distances_px.max() * scale_mm_per_px),
            "max_mm": float(distances_px.max() * scale_mm_per_px),
        }

    @staticmethod
    def _mask_metrics(reference, candidate, scale_mm_per_px):
        import cv2
        import numpy as np
        from scipy.ndimage import distance_transform_edt
        ref = reference > 0; cand = candidate > 0
        intersection = int(np.logical_and(ref, cand).sum())
        union = int(np.logical_or(ref, cand).sum())
        iou = intersection / max(1, union)
        ref_edge = cv2.morphologyEx(ref.astype(np.uint8), cv2.MORPH_GRADIENT,
                                    np.ones((3, 3), np.uint8)) > 0
        cand_edge = cv2.morphologyEx(cand.astype(np.uint8), cv2.MORPH_GRADIENT,
                                     np.ones((3, 3), np.uint8)) > 0
        ref_field = distance_transform_edt(~ref_edge)
        cand_field = distance_transform_edt(~cand_edge)
        cand_to_ref = ref_field[cand_edge]
        ref_to_cand = cand_field[ref_edge]
        distances = np.concatenate([cand_to_ref, ref_to_cand]) * scale_mm_per_px
        if not len(distances):
            distances = np.asarray([float("inf")])
        ref_area = float(ref.sum()) * scale_mm_per_px ** 2
        cand_area = float(cand.sum()) * scale_mm_per_px ** 2
        ref_contours, _ = cv2.findContours(
            ref.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        cand_contours, _ = cv2.findContours(
            cand.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        ref_perimeter = sum(cv2.arcLength(item, True)
                            for item in ref_contours) * scale_mm_per_px
        cand_perimeter = sum(cv2.arcLength(item, True)
                             for item in cand_contours) * scale_mm_per_px
        return {"iou": float(iou), "mean_mm": float(np.mean(distances)),
                "p95_mm": float(np.percentile(distances, 95)),
                "hausdorff_mm": float(np.max(distances)),
                "max_mm": float(np.max(distances)),
                "area_reference_mm2": ref_area,
                "area_candidate_mm2": cand_area,
                "area_delta_percent": (cand_area - ref_area) /
                max(ref_area, 1e-12) * 100.0,
                "perimeter_reference_mm": float(ref_perimeter),
                "perimeter_candidate_mm": float(cand_perimeter),
                "perimeter_delta_percent": (cand_perimeter - ref_perimeter) /
                max(ref_perimeter, 1e-12) * 100.0}

    @staticmethod
    def _save_overlay(rgb, reference, candidate, path):
        import cv2
        import numpy as np
        overlay = rgb.copy()
        ref_edge = cv2.morphologyEx((reference > 0).astype(np.uint8) * 255,
                                    cv2.MORPH_GRADIENT,
                                    np.ones((3, 3), np.uint8)) > 0
        cand_edge = cv2.morphologyEx((candidate > 0).astype(np.uint8) * 255,
                                     cv2.MORPH_GRADIENT,
                                     np.ones((3, 3), np.uint8)) > 0
        disagreement = np.logical_xor(reference > 0, candidate > 0)
        overlay[ref_edge] = [255, 120, 0]
        overlay[cand_edge] = [0, 255, 0]
        overlay[disagreement] = (
            overlay[disagreement].astype(float) * 0.35 +
            np.array([0, 0, 255]) * 0.65).astype(np.uint8)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(path, overlay)

    @staticmethod
    def _save_boundary_map(shape, loops, path):
        import cv2
        import numpy as np

        canvas = np.full((shape[0], shape[1], 3), 255, dtype=np.uint8)
        colors = {
            "outer": (0, 170, 0), "hole": (200, 0, 160),
            "visible_edge": (0, 150, 0),
            "stroke_centerline": (0, 0, 220),
            "outer_edge": (220, 80, 0), "inner_edge": (0, 140, 220),
        }
        for index, loop in enumerate(loops, start=1):
            points = np.round(loop["points"]).astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(canvas, [points], bool(loop.get("closed", True)),
                          colors.get(loop["role"], (255, 0, 0)), 2,
                          lineType=cv2.LINE_AA)
            anchor = tuple(points[0, 0].tolist())
            cv2.putText(canvas, f"{loop['role']}:{index}", anchor,
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 40, 40), 1,
                        cv2.LINE_AA)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(path, canvas)

    def _image_to_sketch_lineart_loaded(
            self, rgb, image_path, sketch_name, plane, unit, trace,
            calibration, placement, geometry, validation, commit, debug,
            models, idempotency_key, started, projection_report=None,
            source_to_working=None, verification_image=None):
        import numpy as np
        from .lineart_vectorization import vectorize_line_art

        explicit_backend = trace.get("backend")
        if explicit_backend not in {
                None, "line_art", "deep_line_art", "dexined_teed"}:
            raise NotImplementedError(
                f"Trace mode '{trace.get('mode')}' requires backend=line_art; "
                f"explicit backend '{explicit_backend}' cannot substitute")
        line_result = vectorize_line_art(
            rgb, trace=trace, validation=validation, model_config=models)
        reference = line_result.reference
        paths_px = line_result.paths
        self._worker_progress(
            "line_art_topology_complete", paths=len(paths_px),
            confidence=float(line_result.confidence))
        scale = self._calibration_scale(paths_px, calibration)
        anchor = self._image_anchor(paths_px, placement, reference.shape)
        matrix = self._pixel_to_sketch_matrix(scale, anchor, placement)
        projection_report = dict(projection_report or {})
        source_to_working = np.asarray(
            source_to_working if source_to_working is not None else np.eye(3),
            dtype=float)
        source_pixel_to_sketch = np.asarray(matrix) @ source_to_working
        max_error = float(geometry["max_error_mm"])
        max_entities = int(geometry["max_entities"])
        prefer = geometry["prefer"]
        remaining_entities = max_entities
        remaining_fit_points = int(geometry["max_total_fit_points"])
        remaining_control_points = int(geometry["max_total_control_points"])
        fitted_paths = []
        all_entities = []
        fit_worst = 0.0
        for path_index, path in enumerate(paths_px):
            points_mm = self._apply_matrix(path["points"], matrix)
            if len(points_mm) > 1 and np.linalg.norm(
                    points_mm[0] - points_mm[-1]) <= 1e-12:
                points_mm = points_mm[:-1]
            closed = bool(path.get("closed"))
            try:
                if closed and len(points_mm) >= 8:
                    path_approximation = dict(geometry)
                    path_approximation["max_total_fit_points"] = max(
                        4, remaining_fit_points)
                    path_approximation["max_total_control_points"] = max(
                        4, remaining_control_points)
                    entities, fit_error = self._fit_loop_hybrid(
                        points_mm, max_error, prefer,
                        max(1, remaining_entities),
                        approximation=path_approximation)
                else:
                    entities, fit_error = self._fit_open_path_hybrid(
                        points_mm, max_error, prefer,
                        max(4, min(int(geometry["max_spline_fit_points"]),
                                   remaining_fit_points)),
                        max(4, min(
                            remaining_control_points,
                            int(geometry["max_control_points_per_spline"]))),
                        approximation=geometry)
            except Exception as exc:
                raise ValueError(
                    f"Line-art path {path_index + 1} "
                    f"(role={path.get('role')}, closed={closed}, "
                    f"length_px={path.get('length_px')}) failed: {exc}") from exc
            if len(entities) > remaining_entities:
                raise ValueError(
                    "Line-art paths exceed approximation.max_entities")
            path_entities = []
            for entity_index, entity in enumerate(entities):
                entity = dict(entity)
                entity["id"] = (
                    f"path_{path_index + 1:03d}_entity_{entity_index + 1:03d}")
                path_entities.append(entity)
                all_entities.append(entity)
            path_complexity = self._curve_complexity(entities)
            remaining_entities -= len(entities)
            remaining_fit_points -= path_complexity["fit_points"]
            remaining_control_points -= path_complexity["control_points"]
            if remaining_fit_points < 0 or remaining_control_points < 0:
                raise ValueError(
                    "Line-art fit exceeds total fit/control-point budget at "
                    f"path {path_index + 1}: remaining_fit_points="
                    f"{remaining_fit_points}, remaining_control_points="
                    f"{remaining_control_points}")
            fitted_paths.append({
                "role": path["role"], "closed": closed,
                "length_px": float(path["length_px"]),
                "entities": path_entities,
            })
            fit_worst = max(fit_worst, fit_error)
        self._worker_progress(
            "line_art_curve_fit_complete", entities=len(all_entities),
            fit_error_mm=float(fit_worst))
        output_mode = geometry.get("output_mode", "locked_trace")
        self._prepare_output_entities(all_entities, output_mode)
        parameterization = self._parameterization_report(
            all_entities, output_mode)
        complexity = self._curve_complexity(
            all_entities, geometry["entity_complexity_weight"])
        complexity.update({
            "max_entities": max_entities,
            "max_total_fit_points": int(geometry["max_total_fit_points"]),
            "max_total_control_points": int(
                geometry["max_total_control_points"]),
            "max_control_points_per_spline": int(
                geometry["max_control_points_per_spline"]),
        })
        complexity["pass"] = bool(
            complexity["entities"] <= complexity["max_entities"] and
            complexity["fit_points"] <= complexity["max_total_fit_points"] and
            complexity["control_points"] <=
            complexity["max_total_control_points"] and
            max((len(entity.get("control_points", []))
                 for entity in all_entities), default=0) <=
            complexity["max_control_points_per_spline"])
        complexity["cad_commit_profile"] = self._cad_commit_profile(
            all_entities, output_mode)
        candidate, fitted_pixels = self._rasterize_fitted_paths(
            fitted_paths, matrix, reference.shape)
        support_radius = float(validation.get(
            "reverse_line_support_radius_px",
            max(1.5, max_error / max(scale, 1e-12))))
        metrics = self._line_metrics(
            reference, candidate, scale, support_radius_px=support_radius)
        thresholds = {
            "min_line_support": float(
                validation.get("min_reverse_line_support", 0.95)),
            "max_hausdorff_mm": float(
                validation.get("max_hausdorff_mm", 0.5)),
        }
        reverse_pass = bool(
            metrics["balanced_support"] >= thresholds["min_line_support"] and
            metrics["hausdorff_mm"] <= thresholds["max_hausdorff_mm"])
        passed = bool(
            line_result.quality.get("pass") and complexity["pass"] and
            reverse_pass and fit_worst <= max_error)
        confidence = max(0.0, min(1.0,
            line_result.confidence * 0.55 +
            metrics["balanced_support"] * 0.45))
        confidence = min(
            confidence, float(projection_report.get("confidence_cap", 1.0)))
        debug_dir = Path(debug.get("directory") or Path(image_path).parent)
        debug_dir.mkdir(parents=True, exist_ok=True)
        overlay_path = str(debug_dir / f"{sketch_name}.vector-overlay.png")
        vector_path = str(debug_dir / f"{sketch_name}.vector.json")
        boundary_map_path = str(
            debug_dir / f"{sketch_name}.selected-boundaries.png")
        reference_raster_path = str(
            debug_dir / f"{sketch_name}.reference-mask.png")
        if debug.get("save_overlay", True):
            self._save_overlay(rgb, reference, candidate, overlay_path)
            self._runtime.increment("verification_artifacts")
        else:
            overlay_path = None
        if debug.get("save_boundary_map", True):
            self._save_boundary_map(reference.shape, paths_px, boundary_map_path)
            self._runtime.increment("verification_artifacts")
        else:
            boundary_map_path = None
        if debug.get("save_reference_raster", True):
            import cv2
            if not cv2.imwrite(reference_raster_path, reference):
                raise RuntimeError("Could not save the line-art reference raster")
            self._runtime.increment("verification_artifacts")
        else:
            reference_raster_path = None
        vector_payload = {
            "schema": "solidworks-mcp/image-vector/v1",
            "source": os.path.abspath(image_path),
            "verification_image": verification_image or os.path.abspath(image_path),
            "image_shape": list(reference.shape),
            "scale_mm_per_px": scale,
            "pixel_to_sketch": matrix.tolist(),
            "source_pixel_to_sketch": source_pixel_to_sketch.tolist(),
            "projection": projection_report, "anchor_px": anchor,
            "loops": fitted_paths,
            "metrics": metrics, "confidence": confidence,
            "validation_pass": passed, "thresholds": thresholds,
            "segmentation_candidates": line_result.diagnostics,
            "segmentation_quality": line_result.quality,
            "trace": trace, "approximation": geometry,
            "fit_worst_mm": fit_worst, "complexity": complexity,
            "parameterization": parameterization,
            "selected_boundary_map": boundary_map_path,
            "reference_raster": reference_raster_path,
            "topology": [{
                "role": path["role"], "closed": bool(path.get("closed")),
                "length_px": float(path["length_px"]),
            } for path in paths_px],
        }
        if debug.get("save_vector_json", True):
            atomic_json_write(vector_path, vector_payload)
        else:
            vector_path = None
        mode = commit.get("mode", "commit_if_confident")
        min_confidence = float(commit.get("min_confidence", 0.9))
        should_commit = mode not in {"analyze_only", "preview"}
        if mode == "commit_if_confident":
            should_commit = passed and confidence >= min_confidence
        if (mode == "force_commit" and not passed and not commit.get(
                "acknowledge_validation_failure", False)):
            should_commit = False
        sketch_result = None
        committed = False
        cad_entities, commit_optimization = (
            self._construction_nurbs_commit_plan(
                fitted_paths, output_mode, max_error,
                geometry.get("max_total_control_points", 1000000)))
        if should_commit:
            constraints = self._topology_constraints(
                fitted_paths, output_mode)
            solve_policy, sketch_validation = self._output_commit_policy(
                output_mode, max_entities,
                sum(1 for path in fitted_paths if path["closed"]),
                validation.get("require_closed", False))
            sketch_result = self.create_parametric_sketch(
                name=sketch_name, plane=plane, unit=unit,
                entities=cad_entities, constraints=constraints,
                dimensions=[], equations=[],
                solve=solve_policy,
                validation=sketch_validation,
                transaction={"rollback_on_failure": commit.get(
                    "rollback_on_failure", True)},
                output_mode=output_mode,
                idempotency_key=idempotency_key)
            committed = bool(sketch_result.get("success"))
            if committed:
                self._runtime.calibrations[sketch_name] = {
                    "pixel_to_sketch": matrix.tolist(),
                    "image_path": os.path.abspath(image_path),
                    "mask_shape": list(reference.shape),
                    "scale_mm_per_px": scale,
                }
        data = {
            "committed": committed, "sketch": sketch_name,
            "confidence": round(confidence, 6),
            "validation_pass": passed, "metrics": metrics,
            "thresholds": thresholds, "entities": len(all_entities),
            "entity_types": dict(_count_types(all_entities)),
            "complexity": complexity, "paths": len(fitted_paths),
            "closed_paths": sum(1 for path in fitted_paths if path["closed"]),
            "path_roles": dict(_count_types([
                {"type": path["role"]} for path in fitted_paths])),
            "pixel_to_sketch": matrix.tolist(),
            "source_pixel_to_sketch": source_pixel_to_sketch.tolist(),
            "projection": projection_report,
            "scale_mm_per_px": scale, "anchor_px": anchor,
            "overlay": overlay_path, "vector_json": vector_path,
            "selected_boundary_map": boundary_map_path,
            "reference_raster": reference_raster_path,
            "segmentation_quality": line_result.quality,
            "parameterization": parameterization,
            "commit_optimization": commit_optimization,
            "sketch_result": sketch_result,
            "ambiguities": list(projection_report.get("warnings", [])) +
            ([] if passed else [
                key for key, value in {
                    **line_result.quality.get("checks", {}),
                    "reverse_projection": reverse_pass,
                    "primitive_fit": fit_worst <= max_error,
                    "curve_complexity": complexity["pass"],
                }.items() if not value]),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        artifacts = [path for path in [
            overlay_path, boundary_map_path, reference_raster_path,
            vector_path] if path]
        if should_commit and not committed:
            return self._error(
                "IMAGE_LOW_CONFIDENCE",
                "Line-art analysis passed, but SolidWorks sketch commit failed",
                document_restored=self._commit_document_restored(
                    sketch_result),
                debug_artifacts=artifacts, details=data)
        if mode == "commit_if_confident" and not should_commit:
            return self._error(
                "IMAGE_LOW_CONFIDENCE",
                "Line-art candidate was not committed because confidence or "
                "geometry validation is below threshold",
                debug_artifacts=artifacts, details=data)
        return self._result(
            True, "Line-art vectorization completed", SwErrors.swSuccess, data)

    def image_to_sketch(self, image_path: str, sketch_name: str,
                        plane: str = "Front", unit: str = "mm",
                        image_mode: str = "filled_silhouette",
                        contour_selection: Dict[str, Any] = None,
                        trace: Dict[str, Any] = None,
                        calibration: Dict[str, Any] = None,
                        placement: Dict[str, Any] = None,
                        geometry: Dict[str, Any] = None,
                        approximation: Dict[str, Any] = None,
                        validation: Dict[str, Any] = None,
                        commit: Dict[str, Any] = None,
                        debug: Dict[str, Any] = None,
                        models: Dict[str, Any] = None,
                        projection: Dict[str, Any] = None,
                        require_orthographic: bool = False,
                        idempotency_key: str = None) -> Dict:
        missing = self._image_dependencies()
        if missing:
            return missing
        if not image_path or not os.path.isfile(image_path):
            return self._error("IMAGE_LOW_CONFIDENCE",
                               f"Image not found: {image_path}")
        contour_selection = contour_selection or {}
        trace = dict(trace or {})
        calibration = calibration or {}
        placement = placement or {}
        explicit_approximation = {**(geometry or {}), **(approximation or {})}
        geometry = self._resolve_approximation(geometry, approximation)
        if (trace.get("mode") in {
                "stroke_centerlines", "stroke_edges", "all_visible_edges"} and
                "max_total_control_points" not in explicit_approximation):
            # A technical drawing contains many independent paths.  Keep the
            # entity ceiling unchanged, but give their compact B-splines a
            # separate finite aggregate budget instead of starving later paths
            # with the single-silhouette default.
            geometry["max_total_control_points"] = 1024
        validation = validation or {}
        commit = commit or {}
        debug = debug or {}
        models = models or {}
        started = time.perf_counter()
        try:
            rgb, alpha = self._load_image(image_path)
            rgb, alpha, projection_report, source_to_working = (
                self._apply_projection_policy(
                    rgb, alpha, image_mode, projection,
                    require_orthographic=require_orthographic))
            debug_dir = Path(debug.get("directory") or Path(image_path).parent)
            debug_dir.mkdir(parents=True, exist_ok=True)
            verification_image = os.path.abspath(image_path)
            if projection_report.get("mode") == "homography":
                import cv2
                rectified_path = str(
                    debug_dir / f"{sketch_name}.rectified-input.png")
                if not cv2.imwrite(rectified_path, rgb):
                    raise RuntimeError(
                        "Could not save the rectified verification image")
                verification_image = rectified_path
                projection_report["rectified_image"] = rectified_path
                self._runtime.increment("verification_artifacts")
            self._worker_progress("image_loaded", shape=list(rgb.shape[:2]))
            if min(rgb.shape[:2]) < 64:
                raise ValueError("Image resolution is too low (<64 px)")
            if float(rgb.std()) < 1.0 and int(alpha.max()) == int(alpha.min()):
                raise ValueError("Image is empty or nearly uniform")
            trace_mode = trace.get("mode", "outer_silhouette")
            if trace_mode in {
                    "stroke_centerlines", "stroke_edges", "all_visible_edges"}:
                return self._image_to_sketch_lineart_loaded(
                    rgb, image_path, sketch_name, plane, unit, trace,
                    calibration, placement, geometry, validation, commit,
                    debug, models, idempotency_key, started,
                    projection_report=projection_report,
                    source_to_working=source_to_working,
                    verification_image=verification_image)
            backend = trace.get("backend", "deep_matting")
            segmentation_quality = {
                "backend": backend, "pass": True, "checks": {}}
            topology_field = None
            topology_level = 0.5
            if backend in {"deep", "deep_matting", "sam2_vitmatte"}:
                from .deep_vectorization import vectorize_region
                for key in ("roi_px", "min_area_px"):
                    if key not in trace and key in contour_selection:
                        trace[key] = contour_selection[key]
                deep_result = vectorize_region(
                    rgb, alpha, trace=trace, validation=validation,
                    model_config=models)
                mask = deep_result.mask
                topology_field = deep_result.topology_field
                topology_level = float(deep_result.topology_level)
                segmentation_confidence = deep_result.confidence
                candidates = deep_result.diagnostics
                segmentation_quality = deep_result.quality
                self._worker_progress(
                    "segmentation_complete",
                    confidence=float(segmentation_confidence))
            elif backend == "classical":
                if (commit.get("mode", "commit_if_confident") not in
                        {"analyze_only", "preview"} and not commit.get(
                            "acknowledge_classical_backend", False)):
                    raise PermissionError(
                        "The classical segmentation backend is diagnostic-only; "
                        "use analyze_only or explicitly acknowledge its limitations")
                mask, segmentation_confidence, candidates = self._segment_image(
                    rgb, alpha, image_mode, contour_selection)
                topology_field = mask
                topology_level = 0.5
                segmentation_quality.update({
                    "pass": False,
                    "topology_level": topology_level,
                    "checks": {"independent_boundary_evidence": False},
                    "known_limitation": (
                        "Classical masks can lock onto antialiasing/compression halos")})
            else:
                raise ValueError(f"Unknown trace.backend '{backend}'")
            preliminary = self._largest_contour(mask)
            if preliminary is None:
                raise ValueError("Segmentation produced no foreground")
            _, _, bbox_width, bbox_height = __import__("cv2").boundingRect(preliminary)
            scale = self._calibration_scale(
                [{"points": [[0, 0], [bbox_width, bbox_height]]}],
                calibration) if calibration.get("mode") in {"two_points", "mm_per_pixel", "scale"} else None
            # bbox modes must use the recovered silhouette, not the local bbox
            # placeholder used for calibration types independent of topology.
            provisional_scale = (scale or
                float(calibration.get("value", 1.0)) /
                (bbox_height if calibration.get("mode") == "bbox_height"
                 else bbox_width))
            min_feature_px = max(1.0, float(geometry.get("min_feature_mm", 0.4)) /
                                 max(provisional_scale, 1e-12))
            topology_selection = dict(contour_selection)
            trace_mode = trace.get("mode", "outer_silhouette")
            if trace_mode == "outer_silhouette":
                topology_selection["mode"] = "largest_external_only"
            elif trace_mode == "silhouette_with_holes":
                topology_selection["mode"] = "largest_external_with_holes"
            elif trace_mode in {"all_region_boundaries", "guided_components"}:
                topology_selection["mode"] = "all_region_boundaries"
            loops_px = self._extract_topology(
                topology_field, topology_selection, min_feature_px,
                level=topology_level)
            self._worker_progress("topology_complete", loops=len(loops_px))
            scale = self._calibration_scale(loops_px, calibration)
            anchor = self._image_anchor(loops_px, placement, mask.shape)
            matrix = self._pixel_to_sketch_matrix(scale, anchor, placement)
            source_pixel_to_sketch = (
                __import__("numpy").asarray(matrix) @
                __import__("numpy").asarray(source_to_working))
            max_error = float(geometry.get("max_error_mm", 0.15))
            max_entities = int(geometry.get("max_entities", 80))
            prefer = geometry.get("prefer", ["line", "arc", "circle", "spline"])
            fitted_loops, all_entities, fit_worst = [], [], 0.0
            remaining_budget = max_entities
            remaining_fit_points = int(geometry["max_total_fit_points"])
            remaining_control_points = int(
                geometry["max_total_control_points"])
            for loop_index, loop in enumerate(loops_px):
                points_mm = self._apply_matrix(loop["points"], matrix)
                loop_approximation = dict(geometry)
                loop_approximation["max_total_fit_points"] = max(
                    1, remaining_fit_points)
                loop_approximation["max_total_control_points"] = max(
                    1, remaining_control_points)
                entities, fit_error = self._fit_loop_hybrid(
                    points_mm, max_error, prefer, max(1, remaining_budget),
                    approximation=loop_approximation)
                loop_entities = []
                for entity_index, entity in enumerate(entities):
                    entity = dict(entity)
                    entity["id"] = f"loop_{loop_index + 1:03d}_entity_{entity_index + 1:03d}"
                    loop_entities.append(entity)
                    all_entities.append(entity)
                remaining_budget -= len(entities)
                loop_complexity = self._curve_complexity(entities)
                remaining_fit_points -= loop_complexity["fit_points"]
                remaining_control_points -= loop_complexity["control_points"]
                fitted_loops.append({"role": loop["role"],
                                     "entities": loop_entities})
                fit_worst = max(fit_worst, fit_error)
            self._worker_progress(
                "curve_fit_complete", entities=len(all_entities),
                fit_error_mm=float(fit_worst))
            output_mode = geometry.get("output_mode", "locked_trace")
            self._prepare_output_entities(all_entities, output_mode)
            parameterization = self._parameterization_report(
                all_entities, output_mode)
            complexity = self._curve_complexity(
                all_entities, geometry["entity_complexity_weight"])
            complexity.update({
                "max_entities": max_entities,
                "max_total_fit_points": int(
                    geometry["max_total_fit_points"]),
                "max_total_control_points": int(
                    geometry["max_total_control_points"]),
                "max_control_points_per_spline": int(
                    geometry["max_control_points_per_spline"]),
            })
            complexity["pass"] = bool(
                complexity["entities"] <= complexity["max_entities"] and
                complexity["fit_points"] <=
                complexity["max_total_fit_points"] and
                complexity["control_points"] <=
                complexity["max_total_control_points"] and
                max((len(entity.get("control_points", []))
                     for entity in all_entities), default=0) <=
                complexity["max_control_points_per_spline"])
            complexity["cad_commit_profile"] = self._cad_commit_profile(
                all_entities, output_mode)
            fit_violations = [
                {"entity_id": entity["id"],
                 "fit_error_mm": entity.get("fit_error_mm"),
                 "max_error_mm": max_error}
                for entity in all_entities
                if float(entity.get("fit_error_mm", 0.0)) > max_error]
            if len(all_entities) > max_entities:
                raise ValueError(
                    f"Primitive fit needs {len(all_entities)} entities; limit={max_entities}")
            candidate_mask, fitted_pixels = self._rasterize_fitted(
                fitted_loops, matrix, mask.shape)
            metrics = self._mask_metrics(mask, candidate_mask, scale)
            self._worker_progress(
                "reverse_validation_complete", iou=float(metrics["iou"]),
                hausdorff_mm=float(metrics["hausdorff_mm"]))
            topology_ok = (len(loops_px) > 0 and all(
                len(loop["entities"]) > 0 for loop in fitted_loops))
            thresholds = {
                "min_iou": float(validation.get("min_iou", 0.985)),
                "max_hausdorff_mm": float(validation.get("max_hausdorff_mm", 0.3)),
            }
            passed = (bool(segmentation_quality.get("pass")) and topology_ok and
                      complexity["pass"] and
                      metrics["iou"] >= thresholds["min_iou"] and
                      metrics["hausdorff_mm"] <= thresholds["max_hausdorff_mm"] and
                      fit_worst <= max_error)
            confidence = max(0.0, min(1.0,
                segmentation_confidence * 0.35 +
                min(1.0, metrics["iou"] / max(thresholds["min_iou"], 1e-9)) * 0.4 +
                min(1.0, thresholds["max_hausdorff_mm"] /
                    max(metrics["hausdorff_mm"], 1e-9)) * 0.25))
            confidence = min(
                confidence, float(projection_report.get("confidence_cap", 1.0)))
            overlay_path = str(debug_dir / f"{sketch_name}.vector-overlay.png")
            vector_path = str(debug_dir / f"{sketch_name}.vector.json")
            boundary_map_path = str(
                debug_dir / f"{sketch_name}.selected-boundaries.png")
            reference_raster_path = str(
                debug_dir / f"{sketch_name}.reference-mask.png")
            alpha_path = None
            trimap_path = None
            if segmentation_quality.get("backend") == "sam2_vitmatte":
                import cv2
                import numpy as np
                matte = segmentation_quality.pop("alpha")
                trimap = segmentation_quality.pop("trimap")
                if debug.get("save_segmentation_artifacts", True):
                    alpha_path = str(debug_dir / f"{sketch_name}.alpha.png")
                    trimap_path = str(debug_dir / f"{sketch_name}.trimap.png")
                    cv2.imwrite(alpha_path, np.round(matte * 255).astype(np.uint8))
                    cv2.imwrite(trimap_path, trimap)
                    self._runtime.increment("verification_artifacts", 2)
            if debug.get("save_overlay", True):
                self._save_overlay(rgb, mask, candidate_mask, overlay_path)
                self._runtime.increment("verification_artifacts")
            else:
                overlay_path = None
            if debug.get("save_boundary_map", True):
                self._save_boundary_map(mask.shape, loops_px, boundary_map_path)
                self._runtime.increment("verification_artifacts")
            else:
                boundary_map_path = None
            if debug.get("save_reference_raster", True):
                import cv2
                import numpy as np
                reference_bytes = np.asarray(mask, dtype=np.uint8)
                if (reference_bytes.max() if reference_bytes.size else 0) <= 1:
                    reference_bytes = reference_bytes * 255
                if not cv2.imwrite(reference_raster_path, reference_bytes):
                    raise RuntimeError("Could not save the region reference raster")
                self._runtime.increment("verification_artifacts")
            else:
                reference_raster_path = None
            vector_payload = {
                "schema": "solidworks-mcp/image-vector/v1",
                "source": os.path.abspath(image_path),
                "verification_image": verification_image,
                "image_shape": list(mask.shape),
                "scale_mm_per_px": scale, "pixel_to_sketch": matrix.tolist(),
                "source_pixel_to_sketch": source_pixel_to_sketch.tolist(),
                "projection": projection_report,
                "anchor_px": anchor, "loops": fitted_loops,
                "metrics": metrics, "confidence": confidence,
                "validation_pass": passed, "thresholds": thresholds,
                "segmentation_candidates": candidates,
                "segmentation_quality": segmentation_quality,
                "trace": trace,
                "approximation": geometry,
                "removed_components": [
                    item for item in segmentation_quality.get(
                        "components", {}).get("components", [])
                    if not item.get("selected")],
                "fit_worst_mm": fit_worst,
                "fit_violations": fit_violations,
                "complexity": complexity,
                "parameterization": parameterization,
                "selected_boundary_map": boundary_map_path,
                "reference_raster": reference_raster_path,
                "topology": [{"role": loop["role"],
                              "area_px": loop["area_px"]} for loop in loops_px],
            }
            if debug.get("save_vector_json", True):
                atomic_json_write(vector_path, vector_payload)
            else:
                vector_path = None
            mode = commit.get("mode", "commit_if_confident")
            min_confidence = float(commit.get("min_confidence", 0.9))
            should_commit = mode not in {"analyze_only", "preview"}
            if mode == "commit_if_confident":
                should_commit = passed and confidence >= min_confidence
            if mode == "force_commit" and not commit.get(
                    "acknowledge_validation_failure", False) and not passed:
                should_commit = False
            sketch_result = None
            committed = False
            cad_entities, commit_optimization = (
                self._construction_nurbs_commit_plan(
                    fitted_loops, output_mode, max_error,
                    geometry.get("max_total_control_points", 1000000)))
            if should_commit:
                constraints = self._topology_constraints(
                    fitted_loops, output_mode)
                solve_policy, sketch_validation = self._output_commit_policy(
                    output_mode, max_entities, len(fitted_loops),
                    validation.get("require_closed", True))
                sketch_result = self.create_parametric_sketch(
                    name=sketch_name, plane=plane, unit=unit,
                    entities=cad_entities, constraints=constraints,
                    dimensions=[], equations=[],
                    solve=solve_policy,
                    validation=sketch_validation,
                    transaction={"rollback_on_failure": commit.get(
                        "rollback_on_failure", True)},
                    output_mode=output_mode,
                    idempotency_key=idempotency_key)
                committed = bool(sketch_result.get("success"))
                if committed:
                    self._runtime.calibrations[sketch_name] = {
                        "pixel_to_sketch": matrix.tolist(),
                        "image_path": os.path.abspath(image_path),
                        "mask_shape": list(mask.shape), "scale_mm_per_px": scale}
            data = {
                "committed": committed, "sketch": sketch_name,
                "confidence": round(confidence, 6), "validation_pass": passed,
                "metrics": metrics, "thresholds": thresholds,
                "entities": len(all_entities),
                "entity_types": dict(_count_types(all_entities)),
                "complexity": complexity,
                "external_contours": sum(1 for loop in loops_px
                                         if loop["role"] == "outer"),
                "holes": sum(1 for loop in loops_px if loop["role"] == "hole"),
                "pixel_to_sketch": matrix.tolist(),
                "source_pixel_to_sketch": source_pixel_to_sketch.tolist(),
                "projection": projection_report,
                "scale_mm_per_px": scale, "anchor_px": anchor,
                "overlay": overlay_path, "vector_json": vector_path,
                "selected_boundary_map": boundary_map_path,
                "reference_raster": reference_raster_path,
                "alpha_matte": alpha_path, "trimap": trimap_path,
                "segmentation_quality": segmentation_quality,
                "parameterization": parameterization,
                "commit_optimization": commit_optimization,
                "sketch_result": sketch_result,
                "ambiguities": list(projection_report.get("warnings", [])) +
                ([] if passed else [
                    key for key, value in {
                        **segmentation_quality.get("checks", {}),
                        "reverse_projection": (
                            metrics["iou"] >= thresholds["min_iou"] and
                            metrics["hausdorff_mm"] <=
                            thresholds["max_hausdorff_mm"]),
                        "primitive_fit": fit_worst <= max_error,
                        "curve_complexity": complexity["pass"],
                    }.items() if not value]),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            if should_commit and not committed:
                return self._error(
                    "IMAGE_LOW_CONFIDENCE",
                    "Vector analysis passed, but SolidWorks sketch commit failed",
                    document_restored=self._commit_document_restored(
                        sketch_result),
                    debug_artifacts=[p for p in [
                        overlay_path, boundary_map_path, vector_path,
                        alpha_path, trimap_path] if p],
                    details=data)
            if mode == "commit_if_confident" and not should_commit:
                return self._error(
                    "IMAGE_LOW_CONFIDENCE",
                    "Vector candidate was not committed because confidence or "
                    "geometry validation is below threshold",
                    debug_artifacts=[p for p in [
                        overlay_path, boundary_map_path, vector_path,
                        alpha_path, trimap_path] if p],
                    details=data,
                    recommended_actions=[
                        "Inspect the overlay and provide ROI/calibration or adjust "
                        "a tolerance only when the measured deviation is acceptable."])
            return self._result(True, "Image vectorization completed",
                                SwErrors.swSuccess, data)
        except (NotImplementedError, PermissionError) as exc:
            return self._error(
                "CAPABILITY_UNAVAILABLE", str(exc),
                recommended_actions=[
                    "Choose a supported region trace mode or run the requested "
                    "line-art mode only after its dedicated backend is available."])
        except RuntimeError as exc:
            if ("Deep vector" in str(exc) or "CUDA" in str(exc) or
                    "Line-art ensemble is unavailable" in str(exc)):
                return self._error(
                    "CAPABILITY_UNAVAILABLE", str(exc),
                    recommended_actions=[
                        "Install/cache the configured SAM 2.1 and ViTMatte models "
                        "and verify CUDA availability with get_capabilities."])
            return self._error(
                "IMAGE_LOW_CONFIDENCE", f"Image vectorization failed: {exc}")
        except Exception as exc:
            return self._error(
                "IMAGE_LOW_CONFIDENCE", f"Image vectorization failed: {exc}",
                com_hresult=getattr(exc, "hresult", None),
                recommended_actions=[
                    "Provide a clean orthographic projection, explicit ROI, "
                    "unambiguous calibration, and named anchor."])

    def commit_vector_analysis(self, analysis_result: Dict[str, Any],
                               sketch_name: str, plane: str = "Front",
                               unit: str = "mm",
                               commit: Dict[str, Any] = None,
                               validation: Dict[str, Any] = None,
                               budget: Dict[str, Any] = None,
                               idempotency_key: str = None) -> Dict:
        """Commit a verified offline vector payload without loading ML models."""
        cached = self._runtime.idempotent_get(idempotency_key)
        if cached is not None:
            cached.setdefault("data", {})["idempotent_replay"] = True
            return cached
        commit = dict(commit or {})
        validation = dict(validation or {})
        data = dict((analysis_result or {}).get("data") or {})
        if not analysis_result or not analysis_result.get("success"):
            return analysis_result or self._error(
                "IMAGE_LOW_CONFIDENCE", "Offline vector analysis returned no result")
        vector_path = data.get("vector_json")
        if not vector_path or not os.path.isfile(vector_path):
            return self._error(
                "IMAGE_LOW_CONFIDENCE",
                "Offline analysis did not produce a readable vector JSON",
                details={"vector_json": vector_path})
        try:
            with open(vector_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            return self._error(
                "IMAGE_LOW_CONFIDENCE", f"Cannot read vector JSON: {exc}",
                details={"vector_json": vector_path})
        if payload.get("schema") != "solidworks-mcp/image-vector/v1":
            return self._error(
                "IMAGE_LOW_CONFIDENCE", "Unsupported vector payload schema",
                details={"schema": payload.get("schema")})

        passed = bool(data.get(
            "validation_pass", payload.get("validation_pass", False)))
        confidence = float(data.get(
            "confidence", payload.get("confidence", 0.0)))
        mode = commit.get("mode", "commit_if_confident")
        min_confidence = float(commit.get("min_confidence", 0.9))
        if mode in {"analyze_only", "preview"}:
            return analysis_result
        if mode == "commit_if_confident" and (
                not passed or confidence < min_confidence):
            return self._error(
                "IMAGE_LOW_CONFIDENCE",
                "Offline vector candidate did not pass the commit gate",
                debug_artifacts=[path for path in [
                    data.get("overlay"), data.get("selected_boundary_map"),
                    vector_path] if path],
                details=data)
        if (mode == "force_commit" and not passed and not commit.get(
                "acknowledge_validation_failure", False)):
            return self._error(
                "IMAGE_LOW_CONFIDENCE",
                "force_commit requires acknowledge_validation_failure=true")

        loops = payload.get("loops") or []
        entities = [entity for loop in loops
                    for entity in (loop.get("entities") or [])]
        approximation = payload.get("approximation") or {}
        output_mode = approximation.get("output_mode", "locked_trace")
        self._prepare_output_entities(entities, output_mode)
        max_entities = int(approximation.get("max_entities", 80))
        max_fit_points = int(approximation.get(
            "max_total_fit_points", 1000000))
        max_control_points = int(approximation.get(
            "max_total_control_points", 1000000))
        max_control_points_per_spline = int(approximation.get(
            "max_control_points_per_spline", 64))
        complexity = self._curve_complexity(entities)
        if not loops or not entities or len(entities) > max_entities:
            return self._error(
                "IMAGE_LOW_CONFIDENCE", "Vector payload topology is invalid",
                details={"loops": len(loops), "entities": len(entities),
                         "max_entities": max_entities})
        oversized_splines = [
            {"entity_id": entity.get("id"),
             "control_points": len(entity.get("control_points", []))}
            for entity in entities
            if (entity.get("type") == "b_spline" and
                len(entity.get("control_points", [])) >
                max_control_points_per_spline)]
        if (complexity["fit_points"] > max_fit_points or
                complexity["control_points"] > max_control_points or
                oversized_splines):
            return self._error(
                "IMAGE_LOW_CONFIDENCE",
                "Vector payload exceeds the hard CAD complexity budget",
                details={**complexity,
                         "max_total_fit_points": max_fit_points,
                         "max_total_control_points": max_control_points,
                         "max_control_points_per_spline":
                             max_control_points_per_spline,
                         "oversized_splines": oversized_splines})
        allowed_types = {"line", "arc", "circle", "spline", "b_spline"}
        entity_ids = [str(entity.get("id", "")) for entity in entities]
        if (any(entity.get("type") not in allowed_types for entity in entities)
                or any(not entity_id for entity_id in entity_ids)
                or len(entity_ids) != len(set(entity_ids))):
            return self._error(
                "IMAGE_LOW_CONFIDENCE",
                "Vector payload contains invalid or duplicate entities")
        for entity in entities:
            if entity.get("type") != "b_spline":
                continue
            order = int(entity.get("order", 4))
            knots = entity.get("knots") or []
            controls = entity.get("control_points") or []
            periodic = bool(entity.get("periodic", False))
            expected_knots = (len(controls) + 1 if periodic
                              else len(controls) + order)
            if (order < 2 or len(controls) < order or
                    len(knots) != expected_knots):
                return self._error(
                    "IMAGE_LOW_CONFIDENCE",
                    "Vector payload contains an invalid B-spline",
                    details={"entity_id": entity.get("id"), "order": order,
                             "knots": len(knots),
                             "control_points": len(controls)})

        cad_entities, commit_optimization = (
            self._construction_nurbs_commit_plan(
                loops, output_mode,
                float(approximation.get("max_error_mm", 0.15)),
                max_control_points))
        if commit_optimization.get("applied"):
            commit_profile = self._batched_commit_profile(
                commit_optimization, output_mode)
            estimated_commit_sec = commit_profile["estimated_sec"]
        else:
            estimated_commit_sec = self._estimate_vector_commit_seconds(
                entities, output_mode)
            commit_profile = self._cad_commit_profile(entities, output_mode)
        commit_budget = dict(budget or {})
        allowed_commit_sec = float(commit_budget.get(
            "max_elapsed_sec", float("inf")))
        if (not math.isfinite(allowed_commit_sec) and
                allowed_commit_sec != float("inf")):
            return self._error(
                "BUDGET_EXCEEDED", "Commit budget must be finite and positive")
        if allowed_commit_sec <= 0.0 or estimated_commit_sec > allowed_commit_sec:
            self._runtime.increment("budget_exceeded")
            return self._error(
                "BUDGET_EXCEEDED",
                "Estimated synchronous SolidWorks commit exceeds the remaining budget",
                details={
                    "limit": "max_elapsed_sec",
                    "allowed": allowed_commit_sec,
                    "estimated_commit_sec": estimated_commit_sec,
                    "commit_profile": commit_profile,
                    "output_mode": output_mode,
                    "complexity": complexity,
                    "mutation_started": False,
                })

        constraints = self._topology_constraints(loops, output_mode)
        solve_policy, sketch_validation = self._output_commit_policy(
            output_mode, max_entities,
            sum(1 for loop in loops if loop.get("closed", True)),
            validation.get("require_closed", all(
                loop.get("closed", True) for loop in loops)))
        commit_started = time.monotonic()
        sketch_result = self.create_parametric_sketch(
            name=sketch_name, plane=plane, unit=unit,
            entities=cad_entities, constraints=constraints,
            dimensions=[], equations=[],
            solve=solve_policy,
            validation=sketch_validation,
            transaction={"rollback_on_failure": commit.get(
                "rollback_on_failure", True)},
            output_mode=output_mode,
            idempotency_key=None)
        committed = bool(sketch_result.get("success"))
        data.update({"committed": committed, "sketch": sketch_name,
                     "sketch_result": sketch_result,
                     "estimated_commit_sec": estimated_commit_sec,
                     "commit_profile": commit_profile,
                     "commit_optimization": commit_optimization,
                     "commit_elapsed_sec": round(
                         time.monotonic() - commit_started, 3),
                     "parameterization": self._parameterization_report(
                         entities, output_mode),
                     "analysis_process": "isolated_worker",
                     "commit_process": "solidworks_com"})
        if (committed and math.isfinite(allowed_commit_sec) and
                data["commit_elapsed_sec"] > allowed_commit_sec):
            restored = self._rollback_named_sketch(sketch_name)
            self._runtime.increment("budget_exceeded")
            data["committed"] = False
            return self._error(
                "BUDGET_EXCEEDED",
                "SolidWorks sketch creation exceeded the commit budget",
                document_restored=restored,
                details={**data, "allowed_commit_sec": allowed_commit_sec,
                         "mutation_started": True})
        if committed:
            validation_deadline = (
                commit_started + allowed_commit_sec
                if math.isfinite(allowed_commit_sec) else None)
            cad_validation = self._validate_committed_geometry(
                sketch_name, payload, vector_path, validation,
                deadline_monotonic=validation_deadline)
            data["cad_validation"] = cad_validation
            if not cad_validation.get("pass"):
                restored = False
                if commit.get("rollback_on_failure", True):
                    restored = self._rollback_named_sketch(sketch_name)
                data["committed"] = False
                if cad_validation.get("budget_exceeded"):
                    self._runtime.increment("budget_exceeded")
                    return self._error(
                        "BUDGET_EXCEEDED",
                        "Post-COM validation exceeded the remaining budget",
                        document_restored=restored,
                        debug_artifacts=[path for path in [
                            cad_validation.get("overlay"),
                            cad_validation.get("cad_raster"),
                            cad_validation.get("reference_raster"),
                            data.get("overlay"), vector_path] if path],
                        details={**data,
                                 "allowed_commit_sec": allowed_commit_sec,
                                 "mutation_started": True})
                return self._error(
                    "IMAGE_LOW_CONFIDENCE",
                    "Committed SolidWorks geometry failed post-COM reverse-raster validation",
                    document_restored=restored,
                    debug_artifacts=[path for path in [
                        cad_validation.get("overlay"),
                        cad_validation.get("cad_raster"),
                        cad_validation.get("reference_raster"),
                        data.get("overlay"), vector_path] if path],
                    details=data)
            data["commit_elapsed_sec"] = round(
                time.monotonic() - commit_started, 3)
            if (math.isfinite(allowed_commit_sec) and
                    data["commit_elapsed_sec"] > allowed_commit_sec):
                restored = self._rollback_named_sketch(sketch_name)
                self._runtime.increment("budget_exceeded")
                data["committed"] = False
                return self._error(
                    "BUDGET_EXCEEDED",
                    "Verified SolidWorks commit exceeded the remaining budget",
                    document_restored=restored,
                    debug_artifacts=[path for path in [
                        cad_validation.get("overlay"),
                        cad_validation.get("cad_raster"), vector_path] if path],
                    details={**data, "allowed_commit_sec": allowed_commit_sec,
                             "mutation_started": True})
            self._runtime.calibrations[sketch_name] = {
                "pixel_to_sketch": payload.get("pixel_to_sketch"),
                "image_path": payload.get("source"),
                "mask_shape": payload.get("image_shape"),
                "scale_mm_per_px": payload.get("scale_mm_per_px"),
            }
            result = self._result(
                True, "Image vectorization analyzed offline and committed",
                SwErrors.swSuccess, data)
            self._runtime.idempotent_put(idempotency_key, result)
            return result
        return self._error(
            "IMAGE_LOW_CONFIDENCE",
            "Offline vector analysis passed, but SolidWorks sketch commit failed",
            document_restored=self._commit_document_restored(sketch_result),
            debug_artifacts=[path for path in [
                data.get("overlay"), data.get("selected_boundary_map"),
                vector_path] if path],
            details=data)

    def _reference_mask(self, image_path, contour_selection, image_mode):
        rgb, alpha = self._load_image(image_path)
        mask, confidence, candidates = self._segment_image(
            rgb, alpha, image_mode, contour_selection or {})
        return rgb, mask, confidence, candidates

    @staticmethod
    def _order_sampled_contour(records, close_tolerance):
        """Order sampled sketch entities by coincident endpoints."""
        import numpy as np
        remaining = [{**record,
                      "points": np.asarray(record["points"], dtype=float)}
                     for record in records]
        if not remaining:
            return [], [], False
        ordered = [remaining.pop(0)]
        while remaining:
            endpoint = ordered[-1]["points"][-1]
            candidates = []
            for index, record in enumerate(remaining):
                points = record["points"]
                candidates.append((float(np.linalg.norm(endpoint - points[0])),
                                   index, False))
                candidates.append((float(np.linalg.norm(endpoint - points[-1])),
                                   index, True))
            distance, index, reverse = min(candidates)
            if distance > close_tolerance:
                break
            record = remaining.pop(index)
            if reverse:
                record["points"] = record["points"][::-1]
            ordered.append(record)
        points, owners = [], []
        for record in ordered:
            part = record["points"]
            if points and np.linalg.norm(np.asarray(points[-1]) - part[0]) <= close_tolerance:
                part = part[1:]
            points.extend(part.tolist())
            owners.extend([record["entity_id"]] * len(part))
        connected = not remaining
        return points, owners, connected

    def _rasterize_sketch_geometry(self, geometry, matrix_mm_to_px, shape,
                                   sample_step_mm, line_mode=False,
                                   supersample=4):
        """Reverse-rasterize CAD entities while preserving contours and ownership."""
        import cv2
        import numpy as np

        sampled = self._sample_geometry_entities(geometry, sample_step_mm)
        sampled_by_id = {item["entity_id"]: item for item in sampled}
        raster_high = np.zeros(
            (int(shape[0]) * supersample, int(shape[1]) * supersample),
            dtype=np.uint8)
        entity_pixels = []
        for item in sampled:
            pixels = self._apply_matrix(item["points"], matrix_mm_to_px)
            entity_pixels.append({**item, "pixels": pixels.tolist()})
            if line_mode and len(pixels) >= 2:
                polyline = np.round(pixels * supersample).astype(np.int32)
                cv2.polylines(raster_high, [polyline], False, 255,
                              supersample, lineType=cv2.LINE_AA)

        contour_source = geometry.get("contours") or []
        if not contour_source:
            contour_source = self._contours_from_entities(
                geometry.get("entities") or [])
        contour_records, disconnected = [], []
        close_tolerance = max(1e-6, float(sample_step_mm) * 2.0)
        for contour in contour_source:
            members = [sampled_by_id[entity_id]
                       for entity_id in contour.get("entities", [])
                       if entity_id in sampled_by_id]
            points, owners, connected = self._order_sampled_contour(
                members, close_tolerance)
            declared_closed = bool(contour.get("closed"))
            geometrically_closed = bool(
                len(points) >= 3 and
                math.dist(points[0], points[-1]) <= close_tolerance)
            closed = declared_closed and connected and geometrically_closed
            if declared_closed and not closed:
                disconnected.append(str(contour.get("id", "unknown")))
            if len(points) < 2:
                continue
            pixels = self._apply_matrix(points, matrix_mm_to_px)
            contour_records.append({
                "id": str(contour.get("id", "")), "closed": closed,
                "points": points, "pixels": pixels,
                "owners": owners,
            })
            if line_mode:
                continue

        degenerate = []
        if not line_mode:
            # Apply the even-odd fill rule explicitly. Each independent closed
            # loop toggles material in its interior, so holes and nested islands
            # are preserved without loading GEOS/Shapely in the COM process.
            contour_mask = np.zeros_like(raster_high)
            for contour in contour_records:
                if not contour["closed"] or len(contour["pixels"]) < 3:
                    continue
                polyline = np.round(
                    contour["pixels"] * supersample).astype(np.int32)
                if abs(float(cv2.contourArea(polyline))) < 0.5:
                    degenerate.append(contour["id"])
                    continue
                contour_mask.fill(0)
                cv2.fillPoly(contour_mask, [polyline], 255,
                             lineType=cv2.LINE_8)
                cv2.bitwise_xor(raster_high, contour_mask, dst=raster_high)

        coverage = cv2.resize(raster_high, (int(shape[1]), int(shape[0])),
                              interpolation=cv2.INTER_AREA)
        candidate = (coverage >= (96 if line_mode else 128)).astype(np.uint8) * 255
        return candidate, entity_pixels, {
            "sampled_entities": len(sampled),
            "contours": len(contour_records),
            "closed_contours": sum(1 for item in contour_records
                                   if item["closed"]),
            "disconnected_closed_contours": disconnected,
            "degenerate_closed_contours": degenerate,
            "fill_rule": "even_odd_xor" if not line_mode else None,
            "supersample": int(supersample),
            "line_mode": bool(line_mode),
        }

    @staticmethod
    def _deviation_attribution(reference_mask, entity_pixels, scale_mm_per_px,
                               max_zones=8):
        """Attribute symmetric edge deviations to the nearest CAD entity."""
        import cv2
        import numpy as np
        from scipy.spatial import cKDTree

        reference_edge = cv2.morphologyEx(
            (reference_mask > 0).astype(np.uint8), cv2.MORPH_GRADIENT,
            np.ones((3, 3), np.uint8)) > 0
        reference_yx = np.column_stack(np.nonzero(reference_edge))
        reference_xy = reference_yx[:, ::-1].astype(float)
        candidate_xy, owners = [], []
        for entity in entity_pixels:
            points = np.asarray(entity.get("pixels") or [], dtype=float)
            if not len(points):
                continue
            candidate_xy.extend(points.tolist())
            owners.extend([entity["entity_id"]] * len(points))
        if not len(reference_xy) or not candidate_xy:
            return [], [], {"candidate_samples": len(candidate_xy),
                            "reference_edge_pixels": len(reference_xy)}
        candidate_xy = np.asarray(candidate_xy, dtype=float)
        candidate_to_ref = cKDTree(reference_xy).query(candidate_xy)[0]
        ref_to_candidate, nearest_candidate = cKDTree(candidate_xy).query(reference_xy)
        by_entity = defaultdict(list)
        zone_candidates = []
        for index, distance_px in enumerate(candidate_to_ref):
            distance_mm = float(distance_px) * scale_mm_per_px
            by_entity[owners[index]].append(distance_mm)
            zone_candidates.append((distance_mm, candidate_xy[index], owners[index],
                                    "candidate_to_reference"))
        for index, distance_px in enumerate(ref_to_candidate):
            owner = owners[int(nearest_candidate[index])]
            distance_mm = float(distance_px) * scale_mm_per_px
            by_entity[owner].append(distance_mm)
            zone_candidates.append((distance_mm, reference_xy[index], owner,
                                    "reference_to_candidate"))
        entity_errors = sorted(({
            "entity_id": owner,
            "mean_mm": float(np.mean(values)),
            "p95_mm": float(np.percentile(values, 95)),
            "max_mm": float(np.max(values)),
            "sample_count": len(values),
        } for owner, values in by_entity.items()),
            key=lambda item: item["max_mm"], reverse=True)
        zones, suppression_px = [], max(4.0, 2.0 / max(scale_mm_per_px, 1e-9))
        for distance_mm, point, owner, direction in sorted(
                zone_candidates, key=lambda item: item[0], reverse=True):
            if distance_mm <= 0.0:
                break
            if any(float(np.linalg.norm(point - np.asarray(zone["pixel"]))) <
                   suppression_px for zone in zones):
                continue
            zones.append({"pixel": [round(float(point[0]), 3),
                                     round(float(point[1]), 3)],
                          "deviation_mm": round(float(distance_mm), 6),
                          "entity_id": owner, "direction": direction})
            if len(zones) >= max(1, int(max_zones)):
                break
        return entity_errors, zones, {
            "candidate_samples": len(candidate_xy),
            "reference_edge_pixels": len(reference_xy),
        }

    @staticmethod
    def _save_sketch_vector_overlay(reference_image, shape, entity_pixels,
                                    zones, path):
        """Write a self-contained, parse-verified SVG diagnostic overlay."""
        import base64
        import mimetypes
        import xml.etree.ElementTree as ET

        mime = mimetypes.guess_type(reference_image)[0] or "image/png"
        encoded = base64.b64encode(Path(reference_image).read_bytes()).decode("ascii")
        svg = ET.Element("svg", xmlns="http://www.w3.org/2000/svg",
                         version="1.1", width=str(int(shape[1])),
                         height=str(int(shape[0])),
                         viewBox=f"0 0 {int(shape[1])} {int(shape[0])}")
        ET.SubElement(svg, "image", x="0", y="0", width=str(int(shape[1])),
                      height=str(int(shape[0])), preserveAspectRatio="none",
                      href=f"data:{mime};base64,{encoded}")
        geometry_layer = ET.SubElement(svg, "g", id="cad-geometry",
                                       fill="none", stroke="#00c853",
                                       **{"stroke-width": "1.5"})
        for entity in entity_pixels:
            points = entity.get("pixels") or []
            if len(points) < 2:
                continue
            path_data = "M " + " L ".join(
                f"{float(point[0]):.3f} {float(point[1]):.3f}"
                for point in points)
            ET.SubElement(geometry_layer, "path", id=str(entity["entity_id"]),
                          d=path_data, **{"vector-effect": "non-scaling-stroke"})
        zone_layer = ET.SubElement(svg, "g", id="maximum-deviation-zones",
                                   fill="none", stroke="#ff1744",
                                   **{"stroke-width": "2"})
        for index, zone in enumerate(zones, start=1):
            ET.SubElement(zone_layer, "circle", id=f"zone-{index:02d}",
                          cx=str(zone["pixel"][0]), cy=str(zone["pixel"][1]),
                          r="5", **{"data-entity-id": str(zone["entity_id"]),
                                    "data-deviation-mm": str(zone["deviation_mm"])})
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(svg).write(path, encoding="utf-8", xml_declaration=True)
        ET.parse(path)

    def compare_sketch_to_reference(self, sketch_name: str,
                                    reference_image: str,
                                    transform: Dict[str, Any] = None,
                                    tolerance: Dict[str, Any] = None,
                                    outputs: Dict[str, Any] = None,
                                    contour_selection: Dict[str, Any] = None,
                                    image_mode: str = "filled_silhouette",
                                    geometry_payload: Dict[str, Any] = None) -> Dict:
        transform, tolerance, outputs = transform or {}, tolerance or {}, outputs or {}
        if not os.path.isfile(reference_image):
            return self._error("IMAGE_LOW_CONFIDENCE",
                               f"Reference image not found: {reference_image}")
        if geometry_payload is None:
            result, geometry = self._load_geometry_payload(sketch_name, "mm")
            if geometry is None:
                return result
        else:
            geometry = geometry_payload
            if not isinstance(geometry, dict) or not isinstance(
                    geometry.get("entities"), list):
                return self._error(
                    "INVALID_PLAN", "Invalid exported sketch geometry payload")

        # Native image/scientific libraries must load only in the isolated
        # worker when this tool is dispatched by the MCP server. Keeping the
        # imports after the COM export boundary prevents a first-use DLL load
        # from freezing the connected SOLIDWORKS process.
        import cv2
        import numpy as np
        saved = self._runtime.calibrations.get(sketch_name)
        matrix_value = transform.get("matrix")
        if transform.get("mode", "saved_calibration") == "saved_calibration":
            matrix_value = (saved or {}).get("pixel_to_sketch")
        if matrix_value is None:
            return self._error(
                "IMAGE_LOW_CONFIDENCE",
                "No saved pixel-to-sketch calibration; provide transform.matrix")
        matrix = np.asarray(matrix_value, dtype=float)
        rgb, reference_mask, segmentation_confidence, _ = self._reference_mask(
            reference_image, contour_selection, image_mode)
        sample_step = max(0.005, float(tolerance.get("sample_step_mm", 0.025)))
        inverse = np.linalg.inv(matrix)
        line_mode = image_mode != "filled_silhouette"
        candidate, entity_pixels, rasterization = self._rasterize_sketch_geometry(
            geometry, inverse, reference_mask.shape, sample_step,
            line_mode=line_mode,
            supersample=max(2, int(tolerance.get("supersample", 4))))
        if not entity_pixels:
            return self._error("SKETCH_OPEN_CONTOUR", "Sketch has no contour samples")
        scale = math.sqrt(abs(np.linalg.det(matrix[:2, :2])))
        if not math.isfinite(scale) or scale <= 0.0:
            return self._error("IMAGE_LOW_CONFIDENCE",
                               "Transform has a singular or invalid metric scale")
        metrics = (self._line_metrics(reference_mask, candidate, scale,
                   support_radius_px=float(tolerance.get(
                       "line_support_radius_px", 1.5))) if line_mode else
                   self._mask_metrics(reference_mask, candidate, scale))
        profile_name = str(tolerance.get("profile", "balanced")).lower()
        profiles = {
            "draft": {"mean_mm": 0.25, "p95_mm": 0.5, "max_mm": 1.0},
            "balanced": {"mean_mm": 0.1, "p95_mm": 0.25, "max_mm": 0.5},
            "strict": {"mean_mm": 0.05, "p95_mm": 0.1, "max_mm": 0.25},
        }
        if profile_name not in profiles:
            return self._error("INVALID_PLAN",
                               "tolerance.profile must be draft, balanced, or strict")
        defaults = profiles[profile_name]
        thresholds = {
            "mean_mm": float(tolerance.get(
                "mean_mm", max(defaults["mean_mm"], scale))),
            "p95_mm": float(tolerance.get(
                "p95_mm", max(defaults["p95_mm"], scale * 2.0))),
            "max_mm": float(tolerance.get(
                "max_mm", max(defaults["max_mm"], scale * 4.0))),
            "min_segmentation_confidence": float(tolerance.get(
                "min_segmentation_confidence", 0.75)),
        }
        if line_mode:
            thresholds["min_line_support"] = float(tolerance.get(
                "min_line_support", 0.95))
        else:
            thresholds["min_iou"] = float(tolerance.get("min_iou", 0.985))
        entity_errors, maximum_deviation_zones, attribution = (
            self._deviation_attribution(
                reference_mask, entity_pixels, scale,
                max_zones=int(tolerance.get("max_deviation_zones", 8))))
        passed = bool(
            not rasterization["disconnected_closed_contours"] and
            segmentation_confidence >= thresholds["min_segmentation_confidence"] and
            all(metrics[key] <= thresholds[key]
                for key in ("mean_mm", "p95_mm", "max_mm")) and
            (metrics["balanced_support"] >= thresholds["min_line_support"]
             if line_mode else metrics["iou"] >= thresholds["min_iou"]))
        overlay_path = outputs.get("png_preview") or outputs.get("overlay")
        if overlay_path:
            self._save_overlay(rgb, reference_mask, candidate, overlay_path)
            self._runtime.increment("verification_artifacts")
        svg_overlay = outputs.get("svg_overlay")
        if svg_overlay:
            self._save_sketch_vector_overlay(
                reference_image, reference_mask.shape, entity_pixels,
                maximum_deviation_zones, svg_overlay)
            self._runtime.increment("verification_artifacts")
        report = {"schema": "solidworks-mcp/sketch-reference-comparison/v1",
                  "sketch": sketch_name,
                  "reference": os.path.abspath(reference_image),
                  "metrics": metrics, "pass": passed,
                  "quality_profile": profile_name,
                  "thresholds": thresholds,
                  "segmentation_confidence": segmentation_confidence,
                  "transform": {"pixel_to_sketch": matrix.tolist(),
                                "source": ("saved_calibration" if
                                           transform.get("mode", "saved_calibration") ==
                                           "saved_calibration" else "explicit")},
                  "rasterization": rasterization,
                  "attribution": attribution,
                  "maximum_deviation_zones": maximum_deviation_zones,
                  "problem_entities": entity_errors[:10] if not passed else []}
        if outputs.get("report"):
            atomic_json_write(outputs["report"], report)
        result = self._result(
            passed, f"Sketch/reference comparison {'PASS' if passed else 'FAIL'}",
            SwErrors.swSuccess if passed else SwErrors.swSketchError,
            {**report, "overlay": overlay_path, "svg_overlay": svg_overlay,
             "report": outputs.get("report")})
        if not passed:
            result["data"]["error"] = structured_error(
                "REFERENCE_MISMATCH",
                "Sketch geometry does not satisfy the reference tolerances",
                conflicting_entities=[
                    item.get("entity_id") for item in entity_errors[:10]
                    if item.get("entity_id")],
                recommended_actions=[
                    "Inspect maximum_deviation_zones and problem_entities",
                    "Correct the attributed CAD entities or explicitly relax tolerances",
                ],
                debug_artifacts=[path for path in (
                    overlay_path, svg_overlay, outputs.get("report")) if path],
                details={"metrics": metrics, "thresholds": thresholds})
        return result

    def compare_sketch_to_image(self, **kwargs):
        if "image_path" in kwargs and "reference_image" not in kwargs:
            kwargs["reference_image"] = kwargs.pop("image_path")
        return self.compare_sketch_to_reference(**kwargs)


def _count_types(entities):
    counts = defaultdict(int)
    for entity in entities:
        counts[entity.get("type", "unknown")] += 1
    return counts
