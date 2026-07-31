"""
SolidWorks View & Screenshot Operations
---------------------------------------
Built-in screenshot capture, named/custom camera orientation, zoom.
Replaces the external take_screenshot.py script.

Camera orientation: IModelView.Orientation3 = IMathTransform whose rotation
COLUMNS = [screenRight, screenUp, towardViewer] in model coordinates.
"""

import math
import os
import time
import logging
import traceback
from typing import Dict, List, Optional

import win32com.client
import pythoncom

from ..constants import SwErrors, SwViews, SwPlanes
from .com_utils import (
    _exe_name_for_pid, build_view_orientation_data, com_get, cross, dot,
    normalize, transform_point, typed)

logger = logging.getLogger(__name__)


class ViewOperations:
    """
    Mixin class for view and screenshot operations.

    Requires parent class to have:
    - get_active_doc(): Document access method
    - _result(): Result factory method
    """

    # Documented swCommands_e value in SOLIDWORKS 2025/2026. The Commands
    # type library is separate from swconst.tlb, so resolving it through the
    # existing swconst helper is not reliable.
    _SW_COMMAND_NORMAL_TO = 169
    _SW_COMMAND_ZOOM_TO_FIT = 332
    _NORMAL_TO_DEFAULT_TOLERANCE_DEG = 0.1

    @staticmethod
    def _orientation_basis(data):
        """Return model-space screen axes from Orientation3 ArrayData."""
        values = list(data or [])
        if len(values) < 9:
            raise ValueError("Orientation transform has fewer than 9 values")
        right = normalize((values[0], values[3], values[6]))
        up = normalize((values[1], values[4], values[7]))
        toward = normalize((values[2], values[5], values[8]))
        orthogonality = max(
            abs(dot(right, up)), abs(dot(right, toward)),
            abs(dot(up, toward)))
        handedness = dot(cross(right, up), toward)
        if orthogonality > 1e-4 or handedness < 0.999:
            raise ValueError(
                "Orientation axes are not a right-handed orthonormal basis")
        return {"right": right, "up": up, "toward_viewer": toward}

    @staticmethod
    def _rounded_axis(axis):
        return [round(float(value), 9) for value in axis]

    @staticmethod
    def _angle_deg(left, right):
        cosine = max(-1.0, min(1.0, dot(normalize(left), normalize(right))))
        return math.degrees(math.acos(cosine))

    def _active_view_state(self, doc):
        """Read and validate the current view basis from SOLIDWORKS."""
        view = com_get(doc, "ActiveView", default=None)
        if view is None:
            raise RuntimeError("No active model view")
        transform = com_get(view, "Orientation3", default=None)
        data = com_get(transform, "ArrayData", default=None)
        basis = self._orientation_basis(data)
        return view, basis

    def _active_sketch_basis(self, doc):
        """Read the model-space +X/+Y/+Z basis of the active 2D sketch."""
        manager = com_get(doc, "SketchManager", default=None)
        sketch = com_get(manager, "ActiveSketch", default=None)
        if sketch is None:
            raise RuntimeError("No active sketch to orient to")
        transform = com_get(sketch, "ModelToSketchTransform", default=None)
        data = com_get(transform, "ArrayData", default=None)
        basis = self._orientation_basis(data)
        return sketch, {
            "x": basis["right"],
            "y": basis["up"],
            "normal": basis["toward_viewer"],
        }

    def _normal_to_target(self, current, sketch_basis):
        """Choose the face and upright direction requiring least view rotation."""
        normal_sign = 1.0 if dot(
            current["toward_viewer"], sketch_basis["normal"]) >= 0.0 else -1.0
        up_sign = 1.0 if dot(current["up"], sketch_basis["y"]) >= 0.0 else -1.0
        toward = tuple(normal_sign * value
                       for value in sketch_basis["normal"])
        up = tuple(up_sign * value for value in sketch_basis["y"])
        right = cross(up, toward)
        return {
            "right": normalize(right),
            "up": normalize(up),
            "toward_viewer": normalize(toward),
            "side": "sketch_front" if normal_sign > 0 else "sketch_back",
            "up_axis": "+sketch_y" if up_sign > 0 else "-sketch_y",
        }

    def _normal_to_measurement(self, current, target,
                               tolerance_deg: float):
        errors = {
            "normal_deg": self._angle_deg(
                current["toward_viewer"], target["toward_viewer"]),
            "up_deg": self._angle_deg(current["up"], target["up"]),
            "right_deg": self._angle_deg(current["right"], target["right"]),
        }
        return {
            "verified": max(errors.values()) <= float(tolerance_deg),
            "angular_error_deg": {
                key: round(value, 9) for key, value in errors.items()},
            "actual_axes": {
                key: self._rounded_axis(current[key])
                for key in ("right", "up", "toward_viewer")},
        }

    def _assign_view_basis(self, doc, target):
        """Assign a target view basis through typed IMathUtility."""
        view = com_get(doc, "ActiveView", default=None)
        if view is None:
            raise RuntimeError("No active model view")
        math_util = com_get(self._sw_app, "GetMathUtility", default=None)
        math_util_t = typed(math_util, "IMathUtility") if math_util else None
        if math_util_t is None:
            raise RuntimeError("Typed IMathUtility unavailable")
        view_direction = [-value for value in target["toward_viewer"]]
        data = build_view_orientation_data(view_direction, target["up"])
        array = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8,
            [float(value) for value in data])
        transform = math_util_t.CreateTransform(array)
        if transform is None:
            raise RuntimeError("IMathUtility.CreateTransform returned nothing")
        view.Orientation3 = transform

    @staticmethod
    def _redraw_view(doc):
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        try:
            view = com_get(doc, "ActiveView", default=None)
            if view is not None:
                com_get(view, "Update", default=None)
        except Exception:
            pass

    @staticmethod
    def _point_coordinates(point):
        return tuple(float(com_get(point, axis, default=0.0))
                     for axis in ("X", "Y", "Z"))

    def _active_sketch_model_points(self, doc):
        """Collect bounded sketch geometry points in model coordinates."""
        manager = com_get(doc, "SketchManager", default=None)
        sketch = com_get(manager, "ActiveSketch", default=None)
        if sketch is None:
            return []
        model_to_sketch = com_get(
            sketch, "ModelToSketchTransform", default=None)
        sketch_to_model = com_get(
            model_to_sketch, "Inverse", default=None) if model_to_sketch else None
        inverse_data = com_get(
            sketch_to_model, "ArrayData", default=None) if sketch_to_model else None
        if not inverse_data:
            return []

        local_points = []
        for point in (com_get(sketch, "GetSketchPoints2", default=[]) or []):
            try:
                local_points.append(self._point_coordinates(point))
            except Exception:
                pass

        # Circle/arc extrema are not guaranteed to appear in
        # GetSketchPoints2. Add a conservative full-radius box; overestimating
        # a partial arc is preferable to clipping it during fit.
        for segment in (com_get(sketch, "GetSketchSegments", default=[]) or []):
            center = com_get(segment, "GetCenterPoint2", default=None)
            radius = com_get(segment, "GetRadius", default=None)
            if center is None or radius is None:
                continue
            try:
                cx, cy, cz = self._point_coordinates(center)
                radius = abs(float(radius))
                if radius > 0.0:
                    local_points.extend([
                        (cx - radius, cy, cz), (cx + radius, cy, cz),
                        (cx, cy - radius, cz), (cx, cy + radius, cz),
                    ])
            except Exception:
                pass

        model_points = []
        for point in local_points:
            try:
                model_points.append(transform_point(inverse_data, point))
            except Exception:
                pass
        return model_points

    @staticmethod
    def _bbox_corners(box):
        if not box or len(box) < 6:
            return []
        lo = [float(box[index]) for index in range(3)]
        hi = [float(box[index + 3]) for index in range(3)]
        return [(x, y, z) for x in (lo[0], hi[0])
                for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]

    def _visible_body_model_points(self, doc):
        points = []
        try:
            bodies = com_get(doc, "GetBodies2", 0, False, default=[]) or []
        except Exception:
            bodies = []
        for body in bodies:
            try:
                if bool(com_get(body, "Visible", default=True)):
                    points.extend(self._bbox_corners(com_get(
                        body, "GetBodyBox", default=None)))
            except Exception:
                pass
        return points

    @staticmethod
    def _points_have_extent(points, tolerance=1e-12):
        if len(points) < 2:
            return False
        return max(max(point[axis] for point in points) -
                   min(point[axis] for point in points)
                   for axis in range(3)) > tolerance

    def _fit_target_points(self, doc):
        sketch_points = self._active_sketch_model_points(doc)
        if self._points_have_extent(sketch_points):
            return sketch_points, "active_sketch"
        body_points = self._visible_body_model_points(doc)
        if self._points_have_extent(body_points):
            return body_points, "visible_bodies"
        return [], "empty_geometry"

    def _fit_measurement(self, doc, points, min_fill=0.35,
                         max_fill=0.90, max_center_offset=0.18):
        """Measure a model-space point cloud in actual screen pixels."""
        view = com_get(doc, "ActiveView", default=None)
        transform = com_get(view, "Transform", default=None) if view else None
        transform_data = com_get(
            transform, "ArrayData", default=None) if transform else None
        frame_width = int(com_get(view, "FrameWidth", default=0) or 0)
        frame_height = int(com_get(view, "FrameHeight", default=0) or 0)
        if not transform_data or frame_width <= 0 or frame_height <= 0:
            raise RuntimeError("View pixel transform/frame size unavailable")
        pixels = [transform_point(transform_data, point) for point in points]
        xs = [point[0] for point in pixels]
        ys = [point[1] for point in pixels]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        width_ratio = max(0.0, bbox[2] - bbox[0]) / frame_width
        height_ratio = max(0.0, bbox[3] - bbox[1]) / frame_height
        fill = max(width_ratio, height_ratio)
        center_x = (bbox[0] + bbox[2]) * 0.5
        center_y = (bbox[1] + bbox[3]) * 0.5
        center_offset = math.hypot(
            (center_x - frame_width * 0.5) / frame_width,
            (center_y - frame_height * 0.5) / frame_height)
        clipped = (bbox[0] < -1.0 or bbox[1] < -1.0 or
                   bbox[2] > frame_width + 1.0 or
                   bbox[3] > frame_height + 1.0)
        verified = (min_fill <= fill <= max_fill and
                    center_offset <= max_center_offset and not clipped)
        return {
            "verified": verified,
            "screen_bbox_px": [round(value, 3) for value in bbox],
            "frame_px": [frame_width, frame_height],
            "width_ratio": round(width_ratio, 6),
            "height_ratio": round(height_ratio, 6),
            "dominant_fill_ratio": round(fill, 6),
            "center_offset_ratio": round(center_offset, 6),
            "clipped": clipped,
            "scale2": float(com_get(view, "Scale2", default=0.0) or 0.0),
            "limits": {"min_fill": min_fill, "max_fill": max_fill,
                       "max_center_offset": max_center_offset},
        }

    def _zoom_to_points(self, doc, points, margin=0.12):
        mins = [min(point[axis] for point in points) for axis in range(3)]
        maxs = [max(point[axis] for point in points) for axis in range(3)]
        largest = max(maxs[axis] - mins[axis] for axis in range(3))
        padding = max(largest * float(margin), 1e-6)
        lo = [mins[axis] - padding for axis in range(3)]
        hi = [maxs[axis] + padding for axis in range(3)]
        doc.ViewZoomTo2(lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])

    def _fit_active_working_geometry(self, doc) -> Dict:
        """Fit the active sketch/body and verify occupancy in screen pixels."""
        points, scope = self._fit_target_points(doc)
        methods = []
        attempts = []
        if not points:
            try:
                doc.ViewZoomtofit2()
                methods.append("ViewZoomtofit2")
                self._redraw_view(doc)
                return {
                    "verified": True, "verification_applicable": False,
                    "reason": "empty_geometry", "scope": scope,
                    "changed": True, "methods": methods, "attempts": [],
                }
            except Exception as exc:
                return {
                    "verified": False, "verification_applicable": False,
                    "reason": f"empty_geometry_fit_failed: {exc}",
                    "scope": scope, "changed": False,
                    "methods": methods, "attempts": [],
                }

        try:
            initial = self._fit_measurement(doc, points)
        except Exception as exc:
            return {"verified": False, "verification_applicable": True,
                    "reason": str(exc), "scope": scope, "changed": False,
                    "methods": methods, "attempts": attempts}
        # A verified initial frame is still not enough for the automatic
        # contract: every working-plane operation must actively request a fit,
        # then prove the resulting frame. This also removes any dependence on
        # undocumented zoom side effects of the native Normal To command.
        final = initial
        operations = [
            ("ViewZoomTo2(active_geometry)",
             lambda: self._zoom_to_points(doc, points)),
            ("ViewZoomtofit2", lambda: doc.ViewZoomtofit2()),
            ("swCommands_ZoomToFit", lambda: com_get(
                self._sw_app, "RunCommand", self._SW_COMMAND_ZOOM_TO_FIT,
                "", default=False)),
        ]
        for name, operation in operations:
            try:
                returned = operation()
                methods.append(name)
                self._redraw_view(doc)
                final = self._fit_measurement(doc, points)
                attempts.append({"method": name, "return_value": returned,
                                 "measurement": final})
                if final["verified"]:
                    break
                # ViewZoomTo2 reliably centres planar sketch geometry on
                # SW2026, but its scale can overshoot badly for a flat 3D box.
                # Correct that scale immediately while preserving the useful
                # centring, before falling back to whole-document fit methods.
                fill = final.get("dominant_fill_ratio", 0.0)
                if fill > 0.0:
                    view = com_get(doc, "ActiveView", default=None)
                    old_scale = float(com_get(
                        view, "Scale2", default=0.0) or 0.0)
                    translation = com_get(
                        view, "Translation3", default=None)
                    factor = max(0.05, min(20.0, 0.55 / fill))
                    view.Scale2 = old_scale * factor
                    if translation is not None:
                        # Scale2 changes SOLIDWORKS' zoom pivot and silently
                        # rewrites Translation3. Restore the centring produced
                        # by ViewZoomTo2 before measuring the corrected frame.
                        view.Translation3 = translation
                    correction = f"Scale2(after {name})"
                    methods.append(correction)
                    self._redraw_view(doc)
                    final = self._fit_measurement(doc, points)
                    attempts.append({
                        "method": correction, "factor": factor,
                        "translation_preserved": translation is not None,
                        "measurement": final})
                    if final["verified"]:
                        break
            except Exception as exc:
                attempts.append({"method": name, "error": str(exc),
                                 "verified": False})

        if not final["verified"] and final.get("dominant_fill_ratio", 0.0) > 0:
            try:
                view = com_get(doc, "ActiveView", default=None)
                old_scale = float(com_get(view, "Scale2", default=0.0) or 0.0)
                translation = com_get(view, "Translation3", default=None)
                factor = max(0.1, min(
                    10.0, 0.55 / final["dominant_fill_ratio"]))
                view.Scale2 = old_scale * factor
                if translation is not None:
                    view.Translation3 = translation
                methods.append("Scale2(correction)")
                self._redraw_view(doc)
                final = self._fit_measurement(doc, points)
                attempts.append({
                    "method": "Scale2(correction)", "factor": factor,
                    "translation_preserved": translation is not None,
                    "measurement": final})
            except Exception as exc:
                attempts.append({"method": "Scale2(correction)",
                                 "error": str(exc), "verified": False})

        return {
            "verified": bool(final["verified"]),
            "verification_applicable": True,
            "scope": scope, "changed": True, "methods": methods,
            "attempts": attempts, "initial": initial, "actual": final,
        }

    def _apply_named_view(self, doc, orientation: str) -> bool:
        """Apply a named view orientation (e.g. 'isometric', 'front').

        ShowNamedView2 returns None on success in SW2026 (not a bool), so
        only an explicit False is treated as failure - otherwise valid views
        were wrongly reported as failed.
        """
        try:
            view_name, view_id = SwViews.get(orientation)
            r = doc.ShowNamedView2(view_name, view_id)
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass
            return r is not False
        except Exception as e:
            logger.debug(f"ShowNamedView2 failed: {e}")
            return False

    def set_view_orientation(self, orientation: str = "isometric",
                             view_direction: List[float] = None,
                             up_direction: List[float] = None,
                             zoom_to_fit: bool = True) -> Dict:
        """
        Set the camera orientation, by name or by custom direction vectors.

        Args:
            orientation: Named view - isometric, front, back, left, right,
                         top, bottom, trimetric, dimetric. Ignored if
                         view_direction is given.
            view_direction: Custom look direction [dx,dy,dz] in model coords
                            (camera looks along this vector into the model).
            up_direction: Optional screen-up [ux,uy,uz] in model coords.
            zoom_to_fit: Zoom to fit after orienting.
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            applied = ""
            if view_direction:
                data = build_view_orientation_data(view_direction,
                                                   up_direction)
                view = com_get(doc, "ActiveView", default=None)
                if view is None:
                    return self._result(False, "No active view",
                                      SwErrors.swUnknownError)
                math_util = com_get(self._sw_app, "GetMathUtility", default=None)
                if math_util is None:
                    return self._result(False, "IMathUtility unavailable",
                                      SwErrors.swUnknownError)
                # CreateTransform must go through the TYPED IMathUtility wrapper:
                # via dynamic dispatch it raises COM -2147417851 "server threw
                # an exception" regardless of how the array is marshaled.
                math_util_t = typed(math_util, "IMathUtility")
                if math_util_t is None:
                    return self._result(False,
                        "Typed IMathUtility unavailable for CreateTransform",
                        SwErrors.swUnknownError)
                arr = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [float(v) for v in data])
                xform = math_util_t.CreateTransform(arr)
                view.Orientation3 = xform
                applied = f"custom(view_direction={view_direction})"
            else:
                if not self._apply_named_view(doc, orientation):
                    return self._result(False,
                        f"Could not apply view '{orientation}'",
                        SwErrors.swUnknownError)
                applied = orientation

            if zoom_to_fit:
                try:
                    doc.ViewZoomtofit2()
                except Exception:
                    pass
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass

            return self._result(True, f"View set: {applied}",
                              SwErrors.swSuccess, {"orientation": applied})
        except Exception as e:
            logger.error(f"Set view error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def orient_normal_to_active_sketch(
            self, doc=None, zoom_to_fit: bool = True,
            angular_tolerance_deg: float = None,
            prefer_native: bool = True) -> Dict:
        """
        Orient the camera normal to the active sketch plane (like Ctrl+8 /
        "Normal To"). When a sketch is created via the API the view is NOT
        auto-oriented (unlike interactive sketching), which is disorienting
        when watching the process - this restores that behaviour.

        This is deliberately not a blind Ctrl+8 emulation: SOLIDWORKS toggles
        to the opposite side when Normal To is invoked on an already aligned
        view. The method first reads the current camera and sketch bases,
        chooses the nearest deterministic side/up combination, and treats an
        already aligned view as a no-op. Otherwise it runs the native command,
        verifies the resulting matrix, and falls back to a typed Orientation3
        assignment with bounded retries. Success always means the read-back
        angular postcondition passed.
        """
        try:
            if doc is None:
                doc, err = self.get_active_doc()
                if err:
                    return err

            tolerance = (self._NORMAL_TO_DEFAULT_TOLERANCE_DEG
                         if angular_tolerance_deg is None
                         else float(angular_tolerance_deg))
            if not 0.001 <= tolerance <= 5.0:
                return self._result(
                    False, "angular_tolerance_deg must be within 0.001..5.0",
                    SwErrors.swInvalidInput)

            sketch, sketch_basis = self._active_sketch_basis(doc)
            _, initial = self._active_view_state(doc)
            target = self._normal_to_target(initial, sketch_basis)
            initial_check = self._normal_to_measurement(
                initial, target, tolerance)
            methods = []
            attempts = []
            native_enabled = False
            native_return = None

            target_data = {
                "right": self._rounded_axis(target["right"]),
                "up": self._rounded_axis(target["up"]),
                "toward_viewer": self._rounded_axis(
                    target["toward_viewer"]),
                "side": target["side"], "up_axis": target["up_axis"],
            }
            sketch_data = {
                "x": self._rounded_axis(sketch_basis["x"]),
                "y": self._rounded_axis(sketch_basis["y"]),
                "normal": self._rounded_axis(sketch_basis["normal"]),
            }

            if initial_check["verified"]:
                final_check = initial_check
                changed = False
            else:
                changed = True
                final_check = initial_check
                if prefer_native:
                    try:
                        native_enabled = bool(com_get(
                            self._sw_app, "IsCommandEnabled",
                            self._SW_COMMAND_NORMAL_TO, default=False))
                    except Exception:
                        native_enabled = False
                    if native_enabled:
                        try:
                            native_return = com_get(
                                self._sw_app, "RunCommand",
                                self._SW_COMMAND_NORMAL_TO, "", default=False)
                            methods.append("swCommands_NormalTo")
                        except Exception as exc:
                            native_return = f"error: {exc}"
                        self._redraw_view(doc)
                        try:
                            _, actual = self._active_view_state(doc)
                            final_check = self._normal_to_measurement(
                                actual, target, tolerance)
                            attempts.append({
                                "method": "swCommands_NormalTo",
                                "result": native_return,
                                "verified": final_check["verified"],
                                "angular_error_deg": final_check[
                                    "angular_error_deg"],
                            })
                        except Exception as exc:
                            attempts.append({
                                "method": "swCommands_NormalTo",
                                "result": native_return,
                                "verified": False, "error": str(exc)})

                if not final_check["verified"]:
                    for matrix_attempt in range(1, 3):
                        method = f"Orientation3[{matrix_attempt}]"
                        try:
                            self._assign_view_basis(doc, target)
                            methods.append(method)
                            self._redraw_view(doc)
                            if matrix_attempt > 1:
                                time.sleep(0.02)
                            _, actual = self._active_view_state(doc)
                            final_check = self._normal_to_measurement(
                                actual, target, tolerance)
                            attempts.append({
                                "method": method,
                                "verified": final_check["verified"],
                                "angular_error_deg": final_check[
                                    "angular_error_deg"],
                            })
                            if final_check["verified"]:
                                break
                        except Exception as exc:
                            attempts.append({
                                "method": method, "verified": False,
                                "error": str(exc)})

            fit = {
                "verified": True, "verification_applicable": False,
                "reason": "disabled", "scope": None, "changed": False,
                "methods": [], "attempts": [],
            }
            if final_check["verified"] and zoom_to_fit:
                fit = self._fit_active_working_geometry(doc)
                try:
                    _, actual_after_fit = self._active_view_state(doc)
                    final_check = self._normal_to_measurement(
                        actual_after_fit, target, tolerance)
                except Exception as exc:
                    final_check = dict(final_check)
                    final_check["verified"] = False
                    attempts.append({
                        "method": "post_fit_orientation_readback",
                        "verified": False, "error": str(exc)})

            sketch_feature = com_get(sketch, "GetFeature", default=None)
            data = {
                "verified": bool(final_check["verified"] and
                                 fit.get("verified", False)),
                "normal_to_verified": bool(final_check["verified"]),
                "changed": bool(changed or fit.get("changed", False)),
                "sketch": str(com_get(
                    sketch_feature, "Name", default=com_get(
                        sketch, "Name", default="<active sketch>"))),
                "angular_tolerance_deg": tolerance,
                "initial_axes": initial_check["actual_axes"],
                "sketch_axes": sketch_data,
                "target_axes": target_data,
                "actual_axes": final_check["actual_axes"],
                "angular_error_deg": final_check["angular_error_deg"],
                "methods": methods,
                "attempts": attempts,
                "native_command": {
                    "id": self._SW_COMMAND_NORMAL_TO,
                    "enabled": native_enabled,
                    "return_value": native_return,
                },
                "fit_to_screen": fit,
            }
            if not final_check["verified"]:
                return self._result(
                    False,
                    "Normal To failed geometric read-back verification",
                    SwErrors.swUnknownError, data)
            if not fit.get("verified", False):
                return self._result(
                    False,
                    "Normal To passed, but Fit to Screen failed pixel "
                    "read-back verification",
                    SwErrors.swUnknownError, data)
            action = ("already verified" if not data["changed"]
                      else "verified")
            return self._result(
                True, f"Sketch view Normal To: {action}",
                SwErrors.swSuccess, data)
        except Exception as e:
            logger.error(f"Normal-to error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def _auto_normal_to(self, doc, zoom_to_fit: bool = True) -> Dict:
        """Apply and verify automatic Normal To; never hide a failure."""
        result = self.orient_normal_to_active_sketch(
            doc, zoom_to_fit=zoom_to_fit)
        if not result.get("success"):
            logger.error("Automatic Normal To failed: %s", result)
        return result

    # ------------------------------------------------------------------
    # Zoom to object (bodies / feature / bbox)
    # ------------------------------------------------------------------

    def _collect_zoom_bbox_m(self, doc, bodies=None, feature=None,
                             bbox=None, unit=None):
        """
        Union bbox in METERS over: named bodies (GetBodyBox), a feature
        (union of face boxes), and/or an explicit bbox in user units.
        Returns ((mins, maxs), None) or (None, error_message).
        """
        mins = [float("inf")] * 3
        maxs = [float("-inf")] * 3
        got = False

        if bbox:
            try:
                to_m = self._units.to_meters
                for i in range(3):
                    mins[i] = min(mins[i], to_m(float(bbox["min"][i]), unit))
                    maxs[i] = max(maxs[i], to_m(float(bbox["max"][i]), unit))
                got = True
            except Exception as e:
                return None, f"Invalid bbox: {e}"

        for name in (bodies or []):
            body = self._find_body(doc, name)
            if body is None:
                return None, f"Body '{name}' not found"
            box = com_get(body, "GetBodyBox", default=None)
            if box and len(box) >= 6:
                got = True
                for i in range(3):
                    mins[i] = min(mins[i], float(box[i]))
                    maxs[i] = max(maxs[i], float(box[i + 3]))

        if feature:
            feat = self._find_feature(doc, feature)
            if feat is None:
                return None, f"Feature '{feature}' not found"
            mm = self._feature_bbox_m(feat)
            if mm is None:
                return None, f"Feature '{feature}' has no faces to zoom to"
            got = True
            for i in range(3):
                mins[i] = min(mins[i], mm[0][i])
                maxs[i] = max(maxs[i], mm[1][i])

        if not got:
            return None, "Nothing to zoom to (provide bodies/feature/bbox)"
        return (mins, maxs), None

    def _apply_zoom_bbox_m(self, doc, mins, maxs, margin: float = 0.15):
        """ViewZoomTo2 over a bbox (meters) expanded by a margin fraction."""
        spans = [max(maxs[i] - mins[i], 1e-6) for i in range(3)]
        pad = max(spans) * float(margin)
        z_min = [mins[i] - pad for i in range(3)]
        z_max = [maxs[i] + pad for i in range(3)]
        doc.ViewZoomTo2(z_min[0], z_min[1], z_min[2],
                        z_max[0], z_max[1], z_max[2])
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        return z_min, z_max

    def zoom_to(self, bodies: List[str] = None, feature: str = None,
                bbox: Dict = None, margin: float = 0.15,
                unit: str = None) -> Dict:
        """
        Frame the view on the given bodies / feature / explicit bbox
        (union + margin, then ViewZoomTo2). Replaces hand-guessed
        ViewZoomTo2 coordinates that produced unreadable close-ups.

        Args:
            bodies: Body names to frame
            feature: Feature name to frame (union of its face boxes)
            bbox: Explicit {"min":[x,y,z],"max":[x,y,z]} in user units
            margin: Extra margin as a fraction of the largest span (0.15)
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            mm, msg = self._collect_zoom_bbox_m(doc, bodies, feature,
                                                bbox, unit)
            if mm is None:
                return self._result(False, f"zoom_to: {msg}",
                                  SwErrors.swSelectionError)

            self._apply_zoom_bbox_m(doc, mm[0], mm[1], margin)

            conv = self._units.from_meters
            unit_str = unit or self._units.default_unit.value
            zoomed = {"min": [round(conv(v, unit), 3) for v in mm[0]],
                      "max": [round(conv(v, unit), 3) for v in mm[1]]}
            return self._result(True,
                f"View framed on bbox {zoomed['min']}..{zoomed['max']} "
                f"{unit_str} (margin {margin})",
                SwErrors.swSuccess,
                {"bbox": zoomed, "margin": margin, "unit": unit_str})
        except Exception as e:
            logger.error(f"Zoom-to error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    # ------------------------------------------------------------------
    # Frame readability check
    # ------------------------------------------------------------------

    def _check_frame_readability(self, path: str) -> Optional[Dict]:
        """
        Detect solid-fill, black, and background-gradient-only frames.

        A histogram alone is insufficient: SOLIDWORKS SaveAs3 can export the
        normal viewport background gradient without any model geometry. The
        gradient contains many grey tones, so it used to pass the dominant
        tone check. Local edge density in the central image region catches
        that failure while ignoring the view triad and viewport controls near
        the borders.
        """
        try:
            from PIL import Image, ImageChops

            with Image.open(path) as img:
                g = img.convert("L")
                g.thumbnail((384, 384))
                hist = g.histogram()
            total = sum(hist)
            if not total:
                return None
            top2 = sum(sorted(hist, reverse=True)[:2]) / total

            w, h = g.size
            border_y = max(1, int(round(h * 0.08)))
            border_x = max(1, int(round(w * 0.08)))
            central = g.crop((border_x, border_y,
                              max(border_x + 2, w - border_x),
                              max(border_y + 2, h - border_y)))
            cw, ch = central.size
            if cw < 2 or ch < 2:
                central = g
                cw, ch = central.size

            # Pure-Pillow finite differences are intentional here. Importing
            # NumPy (or another native-DLL stack) lazily inside the connected
            # SOLIDWORKS COM process can stall the first screenshot forever.
            gx = ImageChops.difference(
                central.crop((1, 0, cw, ch)),
                central.crop((0, 0, cw - 1, ch)))
            gy = ImageChops.difference(
                central.crop((0, 1, cw, ch)),
                central.crop((0, 0, cw, ch - 1)))
            edge_map = ImageChops.lighter(
                gx.crop((0, 0, cw - 1, ch - 1)),
                gy.crop((0, 0, cw - 1, ch - 1)))
            edge_hist = edge_map.histogram()
            edge_total = sum(edge_hist)
            edge_share = (sum(edge_hist[5:]) / edge_total
                          if edge_total else 0.0)
            low_edge_content = edge_share < 0.0005
            # A sparse wireframe on a plain background can legitimately put
            # more than 95% of pixels into one tone. Local model edges are
            # therefore authoritative; the histogram is diagnostic and
            # classifies edge-free solid fills, but cannot reject by itself.
            unreadable = low_edge_content
            return {
                "dominant_tone_share": round(top2, 4),
                "central_edge_share": round(edge_share, 6),
                "readability_reason": (
                    ("dominant_tones" if top2 >= 0.95
                     else "no_central_model_edges")
                    if low_edge_content else None),
                "frame_unreadable": unreadable,
            }
        except Exception as e:
            logger.debug(f"Readability check skipped: {e}")
            return None

    def _find_sw_frame_window(self):
        """Return the largest visible top-level window owned by SLDWORKS."""
        try:
            import win32gui
            import win32process

            candidates = []

            def _collect(hwnd, _):
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    title = win32gui.GetWindowText(hwnd)
                    if "SOLIDWORKS" not in title.upper():
                        return True
                    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                    if _exe_name_for_pid(pid) != "sldworks.exe":
                        return True
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    area = max(0, r - l) * max(0, b - t)
                    candidates.append((area, hwnd))
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(_collect, None)
            return max(candidates)[1] if candidates else None
        except Exception as e:
            logger.debug(f"SOLIDWORKS frame discovery failed: {e}")
            return None

    def _sw_viewport_screen_rect(self, hwnd) -> Optional[List[int]]:
        """
        Locate the actual graphics viewport and convert its logical Win32
        coordinates to physical screen pixels using the COM frame rectangle.

        SOLIDWORKS 2026 is per-monitor DPI scaled. win32gui returns logical
        coordinates in the MCP process while PIL.ImageGrab expects physical
        pixels, so using either coordinate system alone crops the ribbon/tree
        or even a neighbouring monitor.
        """
        try:
            import win32gui

            frame = win32gui.GetWindowRect(hwnd)
            frame_w = max(1, frame[2] - frame[0])
            frame_h = max(1, frame[3] - frame[1])
            physical_left = int(com_get(
                self._sw_app, "FrameLeft", default=frame[0]))
            physical_top = int(com_get(
                self._sw_app, "FrameTop", default=frame[1]))
            physical_w = int(com_get(
                self._sw_app, "FrameWidth", default=frame_w))
            physical_h = int(com_get(
                self._sw_app, "FrameHeight", default=frame_h))
            scale_x = physical_w / frame_w
            scale_y = physical_h / frame_h
            if not (0.5 <= scale_x <= 4.0 and 0.5 <= scale_y <= 4.0):
                scale_x = scale_y = 1.0
                physical_left, physical_top = frame[0], frame[1]

            children = []
            win32gui.EnumChildWindows(
                hwnd, lambda child, items: items.append(child), children)
            details = []
            for child in children:
                try:
                    if not win32gui.IsWindowVisible(child):
                        continue
                    rect = win32gui.GetWindowRect(child)
                    details.append({
                        "hwnd": child,
                        "class": win32gui.GetClassName(child),
                        "title": win32gui.GetWindowText(child),
                        "rect": rect,
                    })
                except Exception:
                    pass

            mdi = next((item for item in details
                        if item["class"] == "swMdiClient"), None)
            if mdi is None:
                return None
            left, top, right, bottom = mdi["rect"]
            mdi_w = max(1, right - left)
            mdi_h = max(1, bottom - top)

            # The deepest large empty AFX child is the OpenGL graphics pane.
            # Its shorter bottom excludes the model tabs/status strip.
            surfaces = [
                item for item in details
                if not item["title"]
                and item["class"].startswith("Afx")
                and (item["rect"][2] - item["rect"][0]) >= 0.75 * mdi_w
                and (item["rect"][3] - item["rect"][1]) >= 0.70 * mdi_h
            ]
            if surfaces:
                surface = min(
                    surfaces,
                    key=lambda item: (
                        item["rect"][3] - item["rect"][1])
                        * (item["rect"][2] - item["rect"][0]))
                sr = surface["rect"]
                left = max(left, sr[0])
                top = max(top, sr[1])
                right = min(right, sr[2])
                bottom = min(bottom, sr[3])

            # Exclude the feature/property manager and right task pane.
            for item in details:
                rect = item["rect"]
                title = item["title"]
                if title == "Tree Container Wnd" and rect[0] < (
                        left + right) / 2:
                    left = max(left, rect[2])
                elif title == "Auto Hider" and rect[0] > (
                        left + right) / 2:
                    right = min(right, rect[0])
                elif title == "swCmdMgr":
                    top = max(top, rect[3])

            if right - left < 200 or bottom - top < 120:
                return None

            def _physical_x(value):
                return int(round(
                    physical_left + (value - frame[0]) * scale_x))

            def _physical_y(value):
                return int(round(
                    physical_top + (value - frame[1]) * scale_y))

            return [_physical_x(left), _physical_y(top),
                    _physical_x(right), _physical_y(bottom)]
        except Exception as e:
            logger.debug(f"Viewport rectangle discovery failed: {e}")
            return None

    def _capture_sw_viewport(self, path: str, compress: bool,
                             width: int, height: int) -> Dict:
        """Capture the on-screen graphics viewport as a SaveAs3 fallback."""
        try:
            from PIL import ImageGrab, Image

            hwnd = self._find_sw_frame_window()
            if hwnd is None:
                return self._result(
                    False, "Could not locate the SOLIDWORKS frame window",
                    SwErrors.swExportError)
            rect = self._sw_viewport_screen_rect(hwnd)
            if rect is None:
                return self._result(
                    False, "Could not locate the SOLIDWORKS graphics viewport",
                    SwErrors.swExportError)

            self._bring_sw_to_front(0, 0, hwnd=hwnd)
            image = ImageGrab.grab(bbox=tuple(rect), all_screens=True)
            if image.mode != "RGB":
                image = image.convert("RGB")
            if compress:
                image.thumbnail((width or 2000, height or 2000),
                                Image.Resampling.LANCZOS)

            path = os.path.abspath(path)
            out_dir = os.path.dirname(path)
            if out_dir and not os.path.exists(out_dir):
                os.makedirs(out_dir)
            if path.lower().endswith(".png"):
                image.save(path, "PNG", optimize=True)
            else:
                image.save(path, "JPEG", quality=70, optimize=True)

            info = {
                "path": path,
                "size_bytes": os.path.getsize(path),
                "capture_method": "screen_viewport_fallback",
                "viewport_rect": rect,
                "full_window": False,
            }
            readability = self._check_frame_readability(path)
            if readability:
                info.update(readability)
            return self._result(
                True, f"Viewport screenshot saved: {path}",
                SwErrors.swSuccess, info)
        except Exception as e:
            logger.error(
                f"Viewport capture error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}",
                                SwErrors.swExportError)

    # ------------------------------------------------------------------
    # Screenshot capture
    # ------------------------------------------------------------------

    def _capture_model_image(self, doc, path: str, compress: bool,
                             width: int, height: int) -> Dict:
        """
        Capture the model view to an image file (SaveAs3 / SaveBMP + PIL
        recompression + readability check). Shared by take_screenshot and
        section_screenshot.
        """
        path = os.path.abspath(path)
        out_dir = os.path.dirname(path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir)
        # Remove a stale file first: SaveAs3 sometimes returns False while
        # actually writing the image (seen live with Section View on), so
        # "file exists afterwards" is the reliable success signal.
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.debug(f"Stale screenshot remove failed: {e}")

        # SaveAs3 exports the active view to an image for image extensions
        saved = False
        capture_method = None
        try:
            saved = bool(doc.SaveAs3(path, 0, 1))
        except Exception as e:
            logger.debug(f"SaveAs3 failed: {e}")
        if not saved:
            saved = os.path.exists(path)  # False retval but file written
        if saved:
            capture_method = "save_as3"
        if not saved:
            try:
                saved = bool(doc.SaveBMP(path, width or 0, height or 0))
                if saved:
                    capture_method = "save_bmp"
            except Exception as e:
                logger.debug(f"SaveBMP failed: {e}")

        if not saved or not os.path.exists(path):
            return self._result(False,
                f"Screenshot not saved to {path}",
                SwErrors.swExportError)

        info = {"path": path, "size_bytes": os.path.getsize(path),
                "capture_method": capture_method or "unknown"}

        # Optional recompression / resize to save tokens
        if compress and path.lower().endswith((".jpg", ".jpeg", ".png")):
            try:
                from PIL import Image
                with Image.open(path) as img:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    max_w = width or 2000
                    max_h = height or 2000
                    img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                    if path.lower().endswith(".png"):
                        img.save(path, "PNG", optimize=True)
                    else:
                        img.save(path, "JPEG", quality=60, optimize=True)
                info["size_bytes"] = os.path.getsize(path)
                info["compressed"] = True
            except ImportError:
                info["compressed"] = False
            except Exception as e:
                logger.debug(f"PIL compress failed: {e}")
                info["compressed"] = False

        readability = self._check_frame_readability(path)
        if readability:
            info.update(readability)

        # SaveAs3/SaveBMP can successfully write only the SOLIDWORKS
        # background gradient. Replace that false-positive artifact with an
        # on-screen crop of the real graphics viewport.
        if readability and readability.get("frame_unreadable"):
            root, ext = os.path.splitext(path)
            fallback_path = root + ".viewport-fallback" + (ext or ".png")
            fallback = self._capture_sw_viewport(
                fallback_path, compress, width, height)
            if fallback.get("success"):
                try:
                    os.replace(fallback_path, path)
                    fallback_info = dict(fallback.get("data") or {})
                    fallback_info["path"] = path
                    fallback_info["size_bytes"] = os.path.getsize(path)
                    fallback_info["primary_capture_method"] = info.get(
                        "capture_method")
                    fallback_info["fallback_reason"] = info.get(
                        "readability_reason")
                    info = fallback_info
                    readability = {
                        key: info.get(key) for key in (
                            "dominant_tone_share", "central_edge_share",
                            "readability_reason", "frame_unreadable")
                    }
                except Exception as e:
                    logger.warning(
                        f"Viewport fallback install failed: {e}")
            else:
                info["viewport_fallback_error"] = fallback.get("message")
            try:
                if os.path.exists(fallback_path):
                    os.remove(fallback_path)
            except Exception:
                pass

        msg = f"Screenshot saved: {path}"
        if readability and readability.get("frame_unreadable"):
            msg += (" WARNING: frame looks UNREADABLE (solid/dominant tones "
                    "or no central model edges). Do NOT use it as visual "
                    "verification; fix zoom/visibility and reshoot.")
        elif info.get("capture_method") == "screen_viewport_fallback":
            msg += (" SaveAs3 returned no model geometry; replaced with a "
                    "verified on-screen viewport capture.")
        return self._result(True, msg, SwErrors.swSuccess, info)

    def take_screenshot(self, path: str, orientation: str = None,
                        view_direction: List[float] = None,
                        up_direction: List[float] = None,
                        zoom_to_fit: bool = True,
                        zoom_to_bodies: List[str] = None,
                        zoom_bbox: Dict = None,
                        width: int = None, height: int = None,
                        compress: bool = True,
                        full_window: bool = False,
                        unit: str = None) -> Dict:
        """
        Save a screenshot of the active document to an image file.
        Optionally sets the camera first. Built-in replacement for the
        external take_screenshot.py (no cp1252 print issues).

        Args:
            path: Output image path (.jpg, .png, .bmp, .tif)
            orientation: Optional named view before capture
            view_direction: Optional custom look direction
            up_direction: Optional screen-up for custom view
            zoom_to_fit: Zoom to fit before capture (default on)
            zoom_to_bodies: Frame the view on these bodies instead of
                            zoom-to-fit (uses zoom_to)
            zoom_bbox: Frame the view on this {"min","max"} bbox (user units)
            width, height: Optional target size for post-compression
            compress: Recompress via PIL if available (smaller file)
            full_window: Capture the whole SolidWorks application window
                         (ribbon, tree, status bar) instead of just the model.
                         Grabs the screen region of the SW frame, so overlapping
                         windows will appear in the image.
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            # Orient camera if requested (applies to model AND full-window)
            want_zoom_obj = bool(zoom_to_bodies or zoom_bbox)
            if orientation or view_direction:
                self.set_view_orientation(
                    orientation=orientation or "isometric",
                    view_direction=view_direction,
                    up_direction=up_direction,
                    zoom_to_fit=zoom_to_fit and not want_zoom_obj)
            elif zoom_to_fit and not want_zoom_obj:
                try:
                    doc.ViewZoomtofit2()
                except Exception:
                    pass

            if want_zoom_obj:
                mm, msg = self._collect_zoom_bbox_m(
                    doc, zoom_to_bodies, None, zoom_bbox, unit)
                if mm is None:
                    return self._result(False, f"zoom failed: {msg}",
                                      SwErrors.swSelectionError)
                self._apply_zoom_bbox_m(doc, mm[0], mm[1])

            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass

            if full_window:
                return self._capture_sw_window(path, compress, width, height)

            return self._capture_model_image(doc, path, compress,
                                             width, height)
        except Exception as e:
            logger.error(f"Screenshot error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swExportError)

    # ------------------------------------------------------------------
    # Section view screenshot
    # ------------------------------------------------------------------

    def _remove_section_view_verified(self, view_mgr, doc,
                                      max_attempts: int = 3) -> Dict:
        """
        Remove the active section view and verify it with
        GetSectionViewData(""). The RemoveSectionView boolean is unreliable
        in SW2026 and is deliberately not used as the success criterion.
        """
        import time

        cleanup = {
            "requested": True,
            "attempts": 0,
            "verified_off": False,
            "verification_error": None,
        }
        for attempt in range(1, max(1, int(max_attempts)) + 1):
            cleanup["attempts"] = attempt
            try:
                view_mgr.RemoveSectionView()
            except Exception as e:
                cleanup["verification_error"] = str(e)
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass
            time.sleep(0.05)
            try:
                active_data = view_mgr.GetSectionViewData("")
                cleanup["verified_off"] = active_data is None
                cleanup["verification_error"] = None
            except Exception as e:
                cleanup["verification_error"] = str(e)
                cleanup["verified_off"] = False
            if cleanup["verified_off"]:
                break
        return cleanup

    def section_screenshot(self, path: str, plane: str = "Front",
                           offset: float = 0.0, flip: bool = False,
                           orientation: str = None,
                           view_direction: List[float] = None,
                           up_direction: List[float] = None,
                           zoom_to_bodies: List[str] = None,
                           zoom_bbox: Dict = None,
                           zoom_to_fit: bool = True,
                           keep_section: bool = False,
                           width: int = None, height: int = None,
                           compress: bool = True,
                           unit: str = None) -> Dict:
        """
        One-call section view screenshot: enable Section View on a plane
        (+offset, +flip), frame, capture, and switch the section OFF again.
        The cheapest "X-ray" for looking inside assemblies of bodies -
        transparency overlays proved unreadable for nested internals.

        Args:
            path: Output image path
            plane: "Front" | "Top" | "Right" or any plane feature name
            offset: Section plane offset (user units)
            flip: Flip the section side
            orientation / view_direction / up_direction: optional camera
            zoom_to_bodies / zoom_bbox: frame on bodies or explicit bbox
            zoom_to_fit: zoom-to-fit when no explicit framing given
            keep_section: Leave the section view ON after the capture
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            # Resolve plane feature
            if plane and plane.lower() in ("front", "top", "right"):
                plane_name = SwPlanes.get(plane)
            else:
                plane_name = plane
            plane_feat = self._find_feature(doc, plane_name)
            if plane_feat is None:
                return self._result(False,
                    f"Section plane '{plane_name}' not found (use "
                    f"Front/Top/Right or an exact plane feature name)",
                    SwErrors.swSelectionError)

            # Section view API is typed-only on SW2026 (dynamic dispatch ->
            # "Member not found", same disease as CreateMeasure/GetModeler).
            # Live-verified member names: CreateSectionViewData,
            # ISectionViewData.FirstPlane / FirstOffset /
            # FirstReverseDirection / GraphicsOnlySection,
            # CreateSectionView(data) to show, RemoveSectionView() to hide
            # (its bool return is MEANINGLESS - False on actual success).
            view_mgr = typed(com_get(doc, "ModelViewManager", default=None),
                             "IModelViewManager")
            if view_mgr is None:
                return self._result(False,
                    "Typed IModelViewManager unavailable",
                    SwErrors.swUnknownError)

            offset_m = self._units.to_meters(offset, unit)

            # Camera first (section plane stays fixed in model space)
            if orientation or view_direction:
                self.set_view_orientation(
                    orientation=orientation or "isometric",
                    view_direction=view_direction,
                    up_direction=up_direction,
                    zoom_to_fit=False)

            section_on = False
            api_error = None
            try:
                data_obj = view_mgr.CreateSectionViewData()
                # FirstPlane accepts the plane feature (or its RefPlane)
                try:
                    data_obj.FirstPlane = plane_feat
                except Exception:
                    spec = com_get(plane_feat, "GetSpecificFeature2",
                                   default=None)
                    if spec is None:
                        raise
                    data_obj.FirstPlane = spec
                data_obj.FirstOffset = float(offset_m)
                data_obj.FirstReverseDirection = bool(flip)
                try:
                    # Purely visual section - does not touch geometry
                    data_obj.GraphicsOnlySection = True
                except Exception:
                    pass
                section_on = bool(view_mgr.CreateSectionView(data_obj))
            except Exception as e:
                api_error = str(e)
                logger.debug(f"Section view failed: {e}")

            if not section_on:
                return self._result(False,
                    f"Could not enable Section View on '{plane_name}'"
                    + (f": {api_error}" if api_error else ""),
                    SwErrors.swUnknownError,
                    {"plane": plane_name, "api_error": api_error})

            cleanup = {
                "requested": not keep_section,
                "attempts": 0,
                "verified_off": bool(keep_section),
                "verification_error": None,
            }
            try:
                # Framing
                if zoom_to_bodies or zoom_bbox:
                    mm, msg = self._collect_zoom_bbox_m(
                        doc, zoom_to_bodies, None, zoom_bbox, unit)
                    if mm is not None:
                        self._apply_zoom_bbox_m(doc, mm[0], mm[1])
                    else:
                        logger.debug(f"section zoom skipped: {msg}")
                elif zoom_to_fit:
                    try:
                        doc.ViewZoomtofit2()
                    except Exception:
                        pass
                try:
                    doc.GraphicsRedraw2()
                except Exception:
                    pass

                result = self._capture_model_image(doc, path, compress,
                                                   width, height)
            finally:
                if not keep_section:
                    # SW2026 can return False even when removal succeeds.
                    # Retry finitely and verify the actual active-view state.
                    cleanup = self._remove_section_view_verified(
                        view_mgr, doc, max_attempts=3)

            if result.get("success"):
                result["message"] = (
                    f"Section ({plane_name}, offset={offset}, flip={flip}) "
                    + result["message"]
                    + ("" if keep_section else (
                        " Section view switched off and verified."
                        if cleanup["verified_off"]
                        else " ERROR: section cleanup could not be verified.")))
                result.setdefault("data", {})
                result["data"].update({"plane": plane_name, "offset": offset,
                                       "flip": flip,
                                       "section_kept": keep_section,
                                       "section_cleanup": cleanup})
            if not keep_section and not cleanup["verified_off"]:
                data = dict(result.get("data") or {})
                data.update({"plane": plane_name, "offset": offset,
                             "flip": flip, "section_kept": False,
                             "section_cleanup": cleanup})
                return self._result(
                    False,
                    f"Section screenshot was captured, but the active "
                    f"Section View could not be verified OFF after "
                    f"{cleanup['attempts']} attempts",
                    SwErrors.swUnknownError, data)
            return result
        except Exception as e:
            logger.error(f"Section screenshot error: {e}\n"
                         f"{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swExportError)

    def _bring_sw_to_front(self, left: int, top: int, hwnd=None) -> None:
        """
        Best-effort: raise the SolidWorks frame window (identified by its
        screen origin) so it is not occluded during a full-window capture.
        """
        try:
            import time
            import win32gui
            import win32con

            target = {"hwnd": hwnd}

            def _find(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                try:
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    if abs(l - left) <= 4 and abs(t - top) <= 4:
                        target["hwnd"] = hwnd
                except Exception:
                    pass
                return True

            if target["hwnd"] is None:
                win32gui.EnumWindows(_find, None)
            hwnd = target["hwnd"]
            if hwnd:
                try:
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                except Exception as e:
                    logger.debug(f"Foreground SW failed: {e}")
                time.sleep(0.2)
        except Exception as e:
            logger.debug(f"_bring_sw_to_front skipped: {e}")

    def _capture_sw_window(self, path: str, compress: bool,
                           width: int, height: int) -> Dict:
        """
        Capture the whole SolidWorks application window via PIL ImageGrab of
        the frame rect (ISldWorks.FrameLeft/Top/Width/Height). Grabs the
        on-screen region, so overlapping windows appear in the image.
        """
        try:
            try:
                from PIL import ImageGrab, Image
            except ImportError:
                return self._result(False,
                    "Pillow (PIL) is required for full_window screenshots "
                    "(pip install pillow)", SwErrors.swExportError)

            l = int(com_get(self._sw_app, "FrameLeft", default=0))
            t = int(com_get(self._sw_app, "FrameTop", default=0))
            w = int(com_get(self._sw_app, "FrameWidth", default=0))
            h = int(com_get(self._sw_app, "FrameHeight", default=0))
            if w <= 0 or h <= 0:
                return self._result(False,
                    "Could not read SolidWorks frame rectangle",
                    SwErrors.swExportError)

            # Raise SW so it is not covered by other windows. COM frame
            # coordinates are physical pixels while win32gui may be logical
            # under per-monitor DPI scaling, so prefer the process-owned HWND.
            self._bring_sw_to_front(
                l, t, hwnd=self._find_sw_frame_window())

            path = os.path.abspath(path)
            out_dir = os.path.dirname(path)
            if out_dir and not os.path.exists(out_dir):
                os.makedirs(out_dir)

            img = ImageGrab.grab(bbox=(l, t, l + w, t + h), all_screens=True)
            if img.mode != "RGB":
                img = img.convert("RGB")
            if compress:
                img.thumbnail((width or 2000, height or 2000),
                              Image.Resampling.LANCZOS)

            if path.lower().endswith(".png"):
                img.save(path, "PNG", optimize=True)
            else:
                img.save(path, "JPEG", quality=70, optimize=True)

            return self._result(True, f"Full-window screenshot saved: {path}",
                              SwErrors.swSuccess,
                              {"path": path, "size_bytes": os.path.getsize(path),
                               "frame_rect": [l, t, w, h], "full_window": True})
        except Exception as e:
            logger.error(f"Window capture error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swExportError)
