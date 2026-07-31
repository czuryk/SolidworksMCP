"""
SolidWorks Additional Feature Operations
----------------------------------------
Backlog features built on the same scheme as advanced_features: typed
IFeatureManager + GetFaces auto-verification + ray-based selection.

Tools: revolve_boss / revolve_cut, shell, reference_plane, reference_axis,
linear_pattern, circular_pattern, mirror_feature, fillet_edges / chamfer_edges
(edge selection by ray), export_file.

Selection marks for patterns/mirror are the SolidWorks-documented values and
were confirmed live on SW2026.
"""

import os
import logging
import traceback
from typing import Dict, List, Optional

import win32com.client
import pythoncom

from ..constants import SwErrors, SwSelectTypeCode, SwFeatureMarks
from .com_utils import (com_get, feature_face_count, select_by_id2,
                        select_by_ray, typed, detect_modal_dialog, normalize)

logger = logging.getLogger(__name__)

# swRefPlaneReferenceConstraints_e
_REFPLANE_DISTANCE = 8
_REFPLANE_ANGLE = 16


class MoreFeatureOperations:
    """
    Mixin: additional feature types. Relies on the combined automation class
    for get_active_doc, ensure_features_not_frozen, _result, _units,
    _find_feature, _find_body, _get_solid_bodies, _find_last_sketch.
    """

    # ========================================================================
    # Shared helpers
    # ========================================================================

    def _typed_fm(self, doc):
        """Typed IFeatureManager (mandatory for feature-creating calls)."""
        fm = typed(doc.FeatureManager, "IFeatureManager")
        return fm if fm is not None else doc.FeatureManager

    def _total_faces(self, doc) -> int:
        """Sum of face counts across all solid bodies."""
        return sum(com_get(b, "GetFaceCount", default=0)
                   for b in self._get_solid_bodies(doc))

    def _verify_and_finish(self, doc, feat, op, bodies_before, extra=None,
                           feature_name=None, auto_verify=True,
                           total_faces_before=None):
        """
        Shared post-op: dead-feature check, optional rename, result dict.

        Verification: a feature is alive if its own GetFaces > 0 OR the
        total model face count changed. Patterns/mirror report GetFaces
        unreliably (0 even when they added geometry, like GetTypeName2), so
        the total-face delta is the authoritative signal for them.
        """
        if feat is None:
            modal = detect_modal_dialog()
            return self._result(False,
                f"{op} failed (API returned None)"
                + (". A modal dialog is blocking SolidWorks!"
                   if modal.get("modal") else ""),
                SwErrors.swFeatureError,
                {"modal_dialog": modal, **(extra or {})})

        feat_name = com_get(feat, "Name", default="<unknown>")
        faces = feature_face_count(feat)
        bodies_after = len(self._get_solid_bodies(doc))
        total_after = (self._total_faces(doc)
                       if total_faces_before is not None else None)
        geometry_changed = (total_faces_before is not None
                            and total_after != total_faces_before)
        alive = (faces > 0) or geometry_changed

        if auto_verify and not alive:
            deleted = self._delete_feature_object(doc, feat, feat_name)
            return self._result(False,
                f"{op} created a DEAD feature '{feat_name}' (no geometry "
                f"change) - {'deleted' if deleted else 'DELETE FAILED'}. "
                f"Check selection/parameters.",
                SwErrors.swFeatureError,
                {"feature_name": feat_name, "dead_feature_deleted": deleted,
                 **(extra or {})})

        # Safe rename: reads back the ACTUAL name, auto-suffixes on
        # collision (SW silently keeps the default name otherwise).
        rename_warning = None
        if feature_name:
            feat_name, rename_warning = self._rename_feature_safe(
                doc, feat, feature_name)

        detail = f"{faces} face(s)" if faces > 0 else "geometry updated"
        warn_note = f" WARNING: {rename_warning}" if rename_warning else ""
        return self._result(True,
            f"{op} '{feat_name}': {detail}, "
            f"bodies {bodies_before} -> {bodies_after}{warn_note}",
            SwErrors.swSuccess,
            {"feature_name": feat_name, "requested_name": feature_name,
             "rename_warning": rename_warning, "face_count": faces,
             "total_faces_before": total_faces_before,
             "total_faces_after": total_after,
             "bodies_before": bodies_before, "bodies_after": bodies_after,
             **(extra or {})})

    def _select_entities_by_rays(self, doc, rays, sel_type_code, mark=0,
                                 append_first=False, unit=None):
        """
        Select multiple entities (edges/faces) by rays. Returns
        (count_selected, misses list).
        """
        selected = 0
        misses = []
        for i, ray in enumerate(rays):
            try:
                origin_m = tuple(self._units.to_meters(c, unit)
                                 for c in ray["origin"])
                direction = ray["direction"]
                radius_m = self._units.to_meters(ray.get("radius", 0.01), unit)
                append = append_first or selected > 0
                ok = select_by_ray(doc, origin_m, normalize(direction),
                                   sel_type=sel_type_code, radius_m=radius_m,
                                   append=append, mark=mark)
                if ok:
                    selected += 1
                else:
                    misses.append(i)
            except Exception as e:
                misses.append(i)
                logger.debug(f"ray[{i}] select failed: {e}")
        return selected, misses

    # ========================================================================
    # Revolve
    # ========================================================================

    def revolve_boss(self, sketch_name: str = None, angle: float = 360.0,
                     axis_name: str = None, reverse: bool = False,
                     merge: bool = True, thin: bool = False,
                     thin_thickness: float = 1.0,
                     feature_name: str = None, auto_verify: bool = True,
                     unit: str = None) -> Dict:
        """
        Revolve a profile sketch (FeatureRevolve2). The sketch must contain a
        centerline (sketch_contour type 'centerline') OR provide axis_name
        (a named axis / sketch segment) which is selected with the profile.

        Args:
            sketch_name: Profile sketch (last sketch if omitted)
            angle: Revolve angle in degrees (360 = full)
            axis_name: Optional axis entity name (else sketch centerline)
            reverse: Reverse revolve direction
            merge: Merge with existing bodies
            thin: Thin-feature revolve
            thin_thickness: Wall thickness if thin
        """
        try:
            import math
            doc, err = self.get_active_doc()
            if err:
                return err
            self.ensure_features_not_frozen(doc)

            try:
                if doc.SketchManager.ActiveSketch is not None:
                    doc.SketchManager.InsertSketch(True)
            except Exception:
                pass
            doc.ClearSelection2(True)

            if not sketch_name:
                sketch_name = self._find_last_sketch(doc)
            if not sketch_name:
                return self._result(False, "No profile sketch found",
                                  SwErrors.swSketchError)

            if not select_by_id2(doc, sketch_name, "SKETCH", mark=0):
                return self._result(False,
                    f"Could not select sketch '{sketch_name}'",
                    SwErrors.swSelectionError)

            if axis_name:
                # Append the axis (sketch segment / ref axis) with mark 0
                if not (select_by_id2(doc, axis_name, "EXTSKETCHSEGMENT",
                                      append=True, mark=4)
                        or select_by_id2(doc, axis_name, "AXIS",
                                         append=True, mark=4)):
                    logger.debug(f"axis '{axis_name}' select failed")

            angle_rad = math.radians(angle)
            thin_m = self._units.to_meters(thin_thickness, unit)
            bodies_before = len(self._get_solid_bodies(doc))
            total_before = self._total_faces(doc)
            fm = self._typed_fm(doc)

            feat = None
            api_error = None
            try:
                feat = fm.FeatureRevolve2(
                    True,               # SingleDir
                    True,               # IsSolid
                    thin,               # IsThin
                    False,              # IsCut
                    reverse,            # ReverseDir
                    False,              # BothDirectionUpToSameEntity
                    0, 0,               # Dir1Type, Dir2Type (0=blind angle)
                    angle_rad, 0.0,     # Dir1Angle, Dir2Angle
                    False, False,       # OffsetReverse1/2
                    0.0, 0.0,           # OffsetDistance1/2
                    0,                  # ThinType
                    thin_m, 0.0,        # ThinThickness1/2
                    merge,              # Merge
                    True, True)         # UseFeatScope, UseAutoSelect
            except Exception as e:
                api_error = str(e)
                logger.debug(f"FeatureRevolve2 failed: {e}")

            return self._verify_and_finish(
                doc, feat, "Revolve", bodies_before,
                extra={"sketch_name": sketch_name, "angle": angle,
                       "api_error": api_error},
                feature_name=feature_name, auto_verify=auto_verify,
                total_faces_before=total_before)
        except Exception as e:
            logger.error(f"Revolve error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Shell
    # ========================================================================

    def shell(self, thickness: float = 2.0, face_rays: List[Dict] = None,
              outward: bool = False, feature_name: str = None,
              auto_verify: bool = True, unit: str = None) -> Dict:
        """
        Hollow out the body, removing the faces hit by face_rays
        (IModelDoc2.InsertFeatureShell). With no face_rays a closed hollow
        body is created.

        Args:
            thickness: Wall thickness
            face_rays: Rays selecting faces to remove
                       [{"origin":[x,y,z],"direction":[dx,dy,dz]}]
            outward: Thicken outward instead of inward
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err
            self.ensure_features_not_frozen(doc)
            doc.ClearSelection2(True)

            removed = 0
            if face_rays:
                removed, misses = self._select_entities_by_rays(
                    doc, face_rays, int(SwSelectTypeCode.swSelFACES),
                    mark=0, unit=unit)
                if removed == 0:
                    return self._result(False,
                        "No faces selected for shell (all rays missed)",
                        SwErrors.swSelectionError)

            thick_m = self._units.to_meters(thickness, unit)
            bodies_before = len(self._get_solid_bodies(doc))
            total_before = self._total_faces(doc)

            feat = None
            api_error = None
            # InsertFeatureShell returns a BOOL (not the feature). Fetch the
            # newest feature and verify it is the Shell.
            try:
                doc.InsertFeatureShell(thick_m, bool(outward))
                feat = com_get(doc, "FeatureByPositionReverse", 0, default=None)
                if feat is not None and com_get(
                        feat, "GetTypeName2", default="") != "Shell":
                    feat = None  # shell not created
            except Exception as e:
                api_error = str(e)
                logger.debug(f"InsertFeatureShell failed: {e}")

            return self._verify_and_finish(
                doc, feat, "Shell", bodies_before,
                extra={"faces_removed": removed, "thickness": thickness,
                       "api_error": api_error},
                feature_name=feature_name, auto_verify=auto_verify,
                total_faces_before=total_before)
        except Exception as e:
            logger.error(f"Shell error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Reference geometry
    # ========================================================================

    def reference_plane(self, source: str = None, source_ray: Dict = None,
                        offset: float = 10.0, reverse: bool = False,
                        feature_name: str = None, unit: str = None) -> Dict:
        """
        Create a reference plane offset from an existing plane or planar face.

        Args:
            source: Name of the source plane (e.g. "Front Plane")
            source_ray: Ray to pick a planar face instead of a named plane
            offset: Offset distance
            reverse: Offset to the other side
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err
            self.ensure_features_not_frozen(doc)
            doc.ClearSelection2(True)

            picked = False
            if source_ray:
                origin_m = tuple(self._units.to_meters(c, unit)
                                 for c in source_ray["origin"])
                picked = select_by_ray(
                    doc, origin_m, normalize(source_ray["direction"]),
                    sel_type=int(SwSelectTypeCode.swSelFACES), mark=0)
            elif source:
                picked = select_by_id2(doc, source, "PLANE", mark=0) or \
                    select_by_id2(doc, source, "FACE", mark=0)
            if not picked:
                return self._result(False,
                    "Could not select the source plane/face",
                    SwErrors.swSelectionError)

            offset_m = self._units.to_meters(offset, unit)
            if reverse:
                offset_m = -offset_m

            fm = self._typed_fm(doc)
            feat = None
            api_error = None
            try:
                feat = fm.InsertRefPlane(_REFPLANE_DISTANCE, offset_m,
                                         0, 0.0, 0, 0.0)
            except Exception as e:
                api_error = str(e)
                logger.debug(f"InsertRefPlane failed: {e}")

            if feat is None:
                modal = detect_modal_dialog()
                return self._result(False,
                    f"Reference plane failed (offset={offset})"
                    + (f": {api_error}" if api_error else ""),
                    SwErrors.swFeatureError,
                    {"api_error": api_error, "modal_dialog": modal})

            name = com_get(feat, "Name", default="<plane>")
            rename_warning = None
            if feature_name:
                name, rename_warning = self._rename_feature_safe(
                    doc, feat, feature_name)
            return self._result(True,
                f"Reference plane '{name}' at offset {offset}{unit or self._units.default_unit.value}"
                + (f" WARNING: {rename_warning}" if rename_warning else ""),
                SwErrors.swSuccess,
                {"feature_name": name, "requested_name": feature_name,
                 "rename_warning": rename_warning, "offset": offset})
        except Exception as e:
            logger.error(f"Reference plane error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    def reference_axis(self, entity_names: List[str] = None,
                       feature_name: str = None) -> Dict:
        """
        Create a reference axis (IModelDoc2.InsertAxis2). Select the defining
        entities first by name: two planes, two points, a cylindrical face,
        or an edge.

        Args:
            entity_names: Entities defining the axis. Each is "name:type",
                          type one of PLANE/FACE/EDGE/VERTEX (default PLANE).
                          Example: ["Front Plane:PLANE", "Right Plane:PLANE"]
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err
            self.ensure_features_not_frozen(doc)
            doc.ClearSelection2(True)

            entity_names = entity_names or []
            sel = 0
            for spec in entity_names:
                if ":" in spec:
                    nm, ty = spec.rsplit(":", 1)
                else:
                    nm, ty = spec, "PLANE"
                if select_by_id2(doc, nm, ty.upper(), append=(sel > 0), mark=0):
                    sel += 1
            if sel == 0:
                return self._result(False,
                    "No defining entities selected for the axis",
                    SwErrors.swSelectionError)

            feat = None
            api_error = None
            # InsertAxis2 returns a BOOL, not the feature. Fetch the newest
            # feature and confirm it is the RefAxis.
            try:
                doc.InsertAxis2(True)
                last = com_get(doc, "FeatureByPositionReverse", 0, default=None)
                if last is not None and com_get(
                        last, "GetTypeName2", default="") == "RefAxis":
                    feat = last
            except Exception as e:
                api_error = str(e)
                logger.debug(f"InsertAxis2 failed: {e}")

            if feat is None:
                return self._result(False,
                    f"Reference axis failed"
                    + (f": {api_error}" if api_error else ""),
                    SwErrors.swFeatureError, {"api_error": api_error})

            name = com_get(feat, "Name", default="<axis>")
            rename_warning = None
            if feature_name:
                name, rename_warning = self._rename_feature_safe(
                    doc, feat, feature_name)
            return self._result(True,
                f"Reference axis '{name}' created"
                + (f" WARNING: {rename_warning}" if rename_warning else ""),
                SwErrors.swSuccess,
                {"feature_name": name, "requested_name": feature_name,
                 "rename_warning": rename_warning})
        except Exception as e:
            logger.error(f"Reference axis error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Fillet / Chamfer with ray edge selection
    # ========================================================================

    def fillet_edges(self, radius: float = 2.0, edge_rays: List[Dict] = None,
                     feature_name: str = None, auto_verify: bool = True,
                     unit: str = None) -> Dict:
        """
        Constant-radius fillet (FeatureFillet3). Edges are selected by rays
        (edge_rays) or must be pre-selected if edge_rays is omitted.
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err
            self.ensure_features_not_frozen(doc)

            if edge_rays:
                doc.ClearSelection2(True)
                n, misses = self._select_entities_by_rays(
                    doc, edge_rays, int(SwSelectTypeCode.swSelEDGES),
                    mark=0, unit=unit)
                if n == 0:
                    return self._result(False,
                        "No edges selected for fillet (all rays missed)",
                        SwErrors.swSelectionError)

            radius_m = self._units.to_meters(radius, unit)
            bodies_before = len(self._get_solid_bodies(doc))
            total_before = self._total_faces(doc)
            fm = self._typed_fm(doc)

            feat = None
            api_error = None
            # Verified SW2026: Options=195, Ftyp=0 (constant radius), array
            # params passed as 0 (NOT None - None returns a null feature).
            try:
                feat = fm.FeatureFillet3(
                    195,        # Options (propagate + defaults)
                    radius_m,   # R1
                    0.0, 0.0,   # R2, Rho
                    0,          # Ftyp = constant size
                    0, 0,       # OverflowType, ConicRhoType
                    0, 0, 0, 0, 0, 0, 0)
            except Exception as e:
                api_error = str(e)
                logger.debug(f"FeatureFillet3 failed: {e}")

            return self._verify_and_finish(
                doc, feat, "Fillet", bodies_before,
                extra={"radius": radius, "api_error": api_error},
                feature_name=feature_name, auto_verify=auto_verify,
                total_faces_before=total_before)
        except Exception as e:
            logger.error(f"Fillet error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    def chamfer_edges(self, distance: float = 2.0, angle: float = 45.0,
                      edge_rays: List[Dict] = None, feature_name: str = None,
                      auto_verify: bool = True, unit: str = None) -> Dict:
        """
        Distance-angle chamfer (InsertFeatureChamfer). Edges selected by rays
        (edge_rays) or pre-selected.
        """
        try:
            import math
            doc, err = self.get_active_doc()
            if err:
                return err
            self.ensure_features_not_frozen(doc)

            if edge_rays:
                doc.ClearSelection2(True)
                n, misses = self._select_entities_by_rays(
                    doc, edge_rays, int(SwSelectTypeCode.swSelEDGES),
                    mark=0, unit=unit)
                if n == 0:
                    return self._result(False,
                        "No edges selected for chamfer (all rays missed)",
                        SwErrors.swSelectionError)

            dist_m = self._units.to_meters(distance, unit)
            angle_rad = math.radians(angle)
            bodies_before = len(self._get_solid_bodies(doc))
            total_before = self._total_faces(doc)
            fm = self._typed_fm(doc)

            feat = None
            api_error = None
            # Verified SW2026: Options=1, ChamferType=1 (angle-distance).
            # ChamferType=0 produced a dead (0-face) feature.
            try:
                feat = fm.InsertFeatureChamfer(
                    1,          # Options
                    1,          # ChamferType: angle-distance
                    dist_m, angle_rad,
                    0.0, 0.0, 0.0, 0.0)
            except Exception as e:
                api_error = str(e)
                logger.debug(f"InsertFeatureChamfer failed: {e}")

            return self._verify_and_finish(
                doc, feat, "Chamfer", bodies_before,
                extra={"distance": distance, "angle": angle,
                       "api_error": api_error},
                feature_name=feature_name, auto_verify=auto_verify,
                total_faces_before=total_before)
        except Exception as e:
            logger.error(f"Chamfer error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Patterns
    # ========================================================================

    def linear_pattern(self, seed_features: List[str],
                       direction_edge_ray: Dict = None,
                       direction_entity: str = None,
                       count: int = 3, spacing: float = 20.0,
                       reverse: bool = False,
                       count2: int = 1, spacing2: float = 20.0,
                       direction2_edge_ray: Dict = None,
                       direction2_entity: str = None,
                       reverse2: bool = False,
                       feature_name: str = None, auto_verify: bool = True,
                       unit: str = None) -> Dict:
        """
        Linear pattern of features (FeatureLinearPattern5). Direction 1 (and
        optional direction 2) is an edge/axis selected by ray or by name.

        Selection marks (SW-documented): direction entity mark=1(/2), seed
        feature(s) mark=4.
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err
            self.ensure_features_not_frozen(doc)
            doc.ClearSelection2(True)

            # Direction 1 (mark 1)
            if not self._select_direction(doc, direction_edge_ray,
                                          direction_entity, mark=1, unit=unit):
                return self._result(False,
                    "Could not select direction 1 (provide direction_edge_ray "
                    "or direction_entity)", SwErrors.swSelectionError)

            # Direction 2 (mark 2), optional
            has_dir2 = count2 and count2 > 1 and (direction2_edge_ray
                                                  or direction2_entity)
            if has_dir2:
                self._select_direction(doc, direction2_edge_ray,
                                       direction2_entity, mark=2, unit=unit)

            # Seed features (mark 4)
            if not self._select_features(doc, seed_features, mark=4):
                return self._result(False,
                    f"Could not select seed feature(s): {seed_features}",
                    SwErrors.swSelectionError)

            spacing_m = self._units.to_meters(spacing, unit)
            spacing2_m = self._units.to_meters(spacing2, unit)
            bodies_before = len(self._get_solid_bodies(doc))
            total_before = self._total_faces(doc)
            fm = self._typed_fm(doc)

            feat = None
            api_error = None
            try:
                feat = fm.FeatureLinearPattern5(
                    int(count), spacing_m,
                    int(count2) if has_dir2 else 1, spacing2_m,
                    bool(reverse), bool(reverse2),
                    "", "",             # DName1, DName2
                    False,              # GeometryPattern (True fails on SW2026)
                    False,              # VaryInstance
                    False, False,       # HasOffset1/2
                    False, False,       # CtrlByNum1/2
                    False, False,       # FromCentroid1/2
                    False, False,       # RevOffset1/2
                    0.0, 0.0,           # Offset1/2
                    False, False)       # D2PatternSeedOnly, SyncSubAssemblies
            except Exception as e:
                api_error = str(e)
                logger.debug(f"FeatureLinearPattern5 failed: {e}")

            return self._verify_and_finish(
                doc, feat, "LinearPattern", bodies_before,
                extra={"count": count, "spacing": spacing,
                       "api_error": api_error},
                feature_name=feature_name, auto_verify=auto_verify,
                total_faces_before=total_before)
        except Exception as e:
            logger.error(f"Linear pattern error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    def circular_pattern(self, seed_features: List[str],
                         axis_entity: str = None, axis_edge_ray: Dict = None,
                         count: int = 4, angle: float = 360.0,
                         equal_spacing: bool = True, reverse: bool = False,
                         feature_name: str = None, auto_verify: bool = True,
                         unit: str = None) -> Dict:
        """
        Circular pattern of features (FeatureCircularPattern5) about an axis
        (reference axis / circular edge) selected by name or ray.
        Marks: axis mark=1, seed feature(s) mark=4.
        """
        try:
            import math
            doc, err = self.get_active_doc()
            if err:
                return err
            self.ensure_features_not_frozen(doc)
            doc.ClearSelection2(True)

            # Axis (mark 1)
            if not self._select_direction(doc, axis_edge_ray, axis_entity,
                                          mark=1, unit=unit, allow_axis=True):
                return self._result(False,
                    "Could not select the pattern axis (provide axis_entity "
                    "or axis_edge_ray)", SwErrors.swSelectionError)

            # Seed features (mark 4)
            if not self._select_features(doc, seed_features, mark=4):
                return self._result(False,
                    f"Could not select seed feature(s): {seed_features}",
                    SwErrors.swSelectionError)

            angle_rad = math.radians(angle if not equal_spacing else angle)
            bodies_before = len(self._get_solid_bodies(doc))
            total_before = self._total_faces(doc)
            fm = self._typed_fm(doc)

            feat = None
            api_error = None
            try:
                feat = fm.FeatureCircularPattern5(
                    int(count), angle_rad, bool(reverse), "",
                    True,               # GeometryPattern
                    bool(equal_spacing),
                    False,              # VaryInstance
                    False,              # SyncSubAssemblies
                    False, False,       # BDir2, BSymmetric
                    int(count), angle_rad, "", bool(equal_spacing))
            except Exception as e:
                api_error = str(e)
                logger.debug(f"FeatureCircularPattern5 failed: {e}")

            return self._verify_and_finish(
                doc, feat, "CircularPattern", bodies_before,
                extra={"count": count, "angle": angle,
                       "api_error": api_error},
                feature_name=feature_name, auto_verify=auto_verify,
                total_faces_before=total_before)
        except Exception as e:
            logger.error(f"Circular pattern error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Mirror
    # ========================================================================

    def mirror_feature(self, mirror_plane: str = None,
                       mirror_face_ray: Dict = None,
                       seed_features: List[str] = None,
                       mirror_bodies: List[str] = None,
                       merge: bool = True, feature_name: str = None,
                       auto_verify: bool = True, unit: str = None) -> Dict:
        """
        Mirror features or bodies about a plane/planar face
        (InsertMirrorFeature2). Marks: mirror plane mark=2, features/bodies
        mark=1.
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err
            self.ensure_features_not_frozen(doc)
            doc.ClearSelection2(True)

            # Mirror plane / face (mark 2)
            picked = False
            if mirror_face_ray:
                origin_m = tuple(self._units.to_meters(c, unit)
                                 for c in mirror_face_ray["origin"])
                picked = select_by_ray(
                    doc, origin_m, normalize(mirror_face_ray["direction"]),
                    sel_type=int(SwSelectTypeCode.swSelFACES), mark=2)
            elif mirror_plane:
                picked = select_by_id2(doc, mirror_plane, "PLANE", mark=2) or \
                    select_by_id2(doc, mirror_plane, "FACE", mark=2)
            if not picked:
                return self._result(False,
                    "Could not select mirror plane/face",
                    SwErrors.swSelectionError)

            mirror_body = bool(mirror_bodies)
            if mirror_bodies:
                for bn in mirror_bodies:
                    body = self._find_body(doc, bn)
                    if body is None:
                        return self._result(False,
                            f"Body '{bn}' not found", SwErrors.swSelectionError)
                    try:
                        body.Select2(True, self._mark_seldata(doc, 1))
                    except Exception:
                        pass
            else:
                if not self._select_features(doc, seed_features or [],
                                             mark=1, append=True):
                    return self._result(False,
                        f"Could not select feature(s) to mirror: {seed_features}",
                        SwErrors.swSelectionError)

            bodies_before = len(self._get_solid_bodies(doc))
            total_before = self._total_faces(doc)
            fm = self._typed_fm(doc)

            feat = None
            api_error = None
            try:
                feat = fm.InsertMirrorFeature2(
                    mirror_body,        # BMirrorBody
                    False,              # BGeometryPattern
                    bool(merge),        # BMerge
                    False,              # BKnit
                    0)                  # ScopeOptions
            except Exception as e:
                api_error = str(e)
                logger.debug(f"InsertMirrorFeature2 failed: {e}")

            return self._verify_and_finish(
                doc, feat, "Mirror", bodies_before,
                extra={"api_error": api_error},
                feature_name=feature_name, auto_verify=auto_verify,
                total_faces_before=total_before)
        except Exception as e:
            logger.error(f"Mirror error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Selection helpers for patterns/mirror
    # ========================================================================

    def _mark_seldata(self, doc, mark):
        from .com_utils import create_select_data
        return create_select_data(doc, mark)

    def _select_direction(self, doc, ray, entity, mark, unit,
                          allow_axis=False) -> bool:
        """Select a direction/axis entity by ray or by name (append)."""
        if ray:
            try:
                origin_m = tuple(self._units.to_meters(c, unit)
                                 for c in ray["origin"])
                return select_by_ray(
                    doc, origin_m, normalize(ray["direction"]),
                    sel_type=int(SwSelectTypeCode.swSelEDGES),
                    append=True, mark=mark)
            except Exception as e:
                logger.debug(f"direction ray failed: {e}")
                return False
        if entity:
            for ty in (("AXIS", "EDGE") if allow_axis else ("EDGE", "AXIS")):
                if select_by_id2(doc, entity, ty, append=True, mark=mark):
                    return True
            # planes can also act as linear direction
            if select_by_id2(doc, entity, "PLANE", append=True, mark=mark):
                return True
        return False

    def _select_features(self, doc, names, mark, append=True) -> bool:
        """Select feature(s) by name with a given mark."""
        ok_any = False
        for nm in names:
            feat = self._find_feature(doc, nm)
            if feat is None:
                continue
            try:
                sd = self._mark_seldata(doc, mark)
                if bool(feat.Select2(append, sd)):
                    ok_any = True
                    continue
            except Exception as e:
                logger.debug(f"feat.Select2 failed: {e}")
            if select_by_id2(doc, nm, "BODYFEATURE", append=append, mark=mark):
                ok_any = True
        return ok_any

    # ========================================================================
    # Export
    # ========================================================================

    def export_file(self, filepath: str) -> Dict:
        """
        Export the active document to a neutral format by extension:
        STEP (.step/.stp), STL (.stl), IGES (.igs/.iges), Parasolid (.x_t/.x_b),
        3MF (.3mf), or any format SolidWorks supports via SaveAs.

        Args:
            filepath: Output path; the extension selects the format.
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            filepath = os.path.abspath(filepath)
            out_dir = os.path.dirname(filepath)
            if out_dir and not os.path.exists(out_dir):
                os.makedirs(out_dir)

            ext = os.path.splitext(filepath)[1].lower()
            supported = (".step", ".stp", ".stl", ".igs", ".iges", ".x_t",
                         ".x_b", ".3mf", ".sat", ".wrl", ".ply", ".pdf",
                         ".dxf", ".dwg", ".parasolid")
            if ext not in supported:
                return self._result(False,
                    f"Unsupported export extension '{ext}'. "
                    f"Supported: {supported}",
                    SwErrors.swInvalidFileType)

            errors = win32com.client.VARIANT(
                pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            warnings = win32com.client.VARIANT(
                pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

            ok = False
            try:
                ok = bool(doc.Extension.SaveAs(
                    filepath, 0, 1, empty, errors, warnings))
            except Exception as e:
                logger.debug(f"Extension.SaveAs export failed: {e}")

            if not ok:
                try:
                    ok = bool(doc.SaveAs3(filepath, 0, 1))
                except Exception as e:
                    logger.debug(f"SaveAs3 export failed: {e}")

            if not ok or not os.path.exists(filepath):
                return self._result(False,
                    f"Export failed (errors={errors.value})",
                    SwErrors.swExportError, {"errors": errors.value})

            return self._result(True, f"Exported: {filepath}",
                              SwErrors.swSuccess,
                              {"path": filepath, "format": ext.lstrip("."),
                               "size_bytes": os.path.getsize(filepath)})
        except Exception as e:
            logger.error(f"Export error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swExportError)
