"""
SolidWorks Advanced Feature Operations
--------------------------------------
Feature management (delete/rename/status) and full-featured
extrude/cut via typed makepy IFeatureManager.

Hard-won SW2026 facts encoded here:
- FeatureExtrusion3/FeatureCut4 via dynamic dispatch raise COM 61704
  "Internal application error" - typed makepy wrappers are mandatory.
- SolidWorks silently creates dead features (0 faces) or "ICE" features
  for infeasible parameters - every feature is verified via GetFaces and
  deleted on failure (auto_verify).
- Freeze Bar at the end of the tree freezes every new API feature
  (no faces, no error) - checked/fixed before each operation.
- Cut feature scope: body must be selected with Mark=8 + UseFeatScope=True
  + UseAutoSelect=False (Mark=2 -> feature is not created at all).
- Topology-changing cuts invalidate pre-cut Body2 wrappers. Scoped bodies are
  reacquired from post-cut geometry before their names are restored.
- Feature and body display names share a collision rule after a cut. A source
  feature that blocks restoration is renamed explicitly and reported.
- FeatureCut4/FeatureExtrusion3 require Sd=False plus two ThroughAll end
  conditions for a real double-ended through-all operation.
- Boss vs Cut flag semantics differ: Boss offset-from-surface works with
  Dir=False/OffsetReverse=False, the equivalent Cut needs Dir=True;
  Cut with outside start-offset: FlipStart=False, Dir=False, OffsetReverse=True.
"""

import logging
import traceback
from typing import Dict, List, Optional

from ..constants import (SwErrors, SwEndConditions, SwStartConditions,
                         SwFeatureMarks, SwSelectTypeCode)
from .com_utils import (com_get, feature_face_count, select_by_id2,
                        select_by_ray, create_select_data, typed,
                        detect_modal_dialog)

logger = logging.getLogger(__name__)


END_CONDITION_MAP = {
    "blind": int(SwEndConditions.swEndCondBlind),
    "through_all": int(SwEndConditions.swEndCondThroughAll),
    "through_all_both": int(SwEndConditions.swEndCondThroughAllBoth),
    "through_next": int(SwEndConditions.swEndCondThroughNext),
    "up_to_vertex": int(SwEndConditions.swEndCondUpToVertex),
    "up_to_surface": int(SwEndConditions.swEndCondUpToSurface),
    "mid_plane": int(SwEndConditions.swEndCondMidPlane),
    "offset_from_surface": int(SwEndConditions.swEndCondOffsetFromSurface),
}

START_CONDITION_MAP = {
    "sketch_plane": int(SwStartConditions.swStartSketchPlane),
    "surface": int(SwStartConditions.swStartSurface),
    "vertex": int(SwStartConditions.swStartVertex),
    "offset": int(SwStartConditions.swStartOffset),
}

# End conditions that need a reference face selected with Mark=1
REF_FACE_CONDITIONS = ("up_to_surface", "offset_from_surface")


class AdvancedFeatureOperations:
    """
    Mixin class for advanced feature operations.

    Requires parent class to have:
    - get_active_doc(): Document access method
    - ensure_features_not_frozen(): Freeze Bar protection
    - _result(): Result factory method
    - _units: UnitConverter instance
    - _find_last_sketch(), _get_sketch_info() from FeatureOperations
    - _find_body() from BodyOperations
    """

    # ========================================================================
    # Feature tree helpers
    # ========================================================================

    def _walk_features(self, doc, include_sub: bool = True):
        """Yield features of the tree (property access, SW2025/2026-safe)."""
        feat = com_get(doc, "FirstFeature", default=None)
        while feat is not None:
            yield feat
            if include_sub:
                sub = com_get(feat, "GetFirstSubFeature", default=None)
                while sub is not None:
                    yield sub
                    sub = com_get(sub, "GetNextSubFeature", default=None)
            feat = com_get(feat, "GetNextFeature", default=None)

    def _find_feature(self, doc, name: str):
        """Find a feature (or subfeature) by exact name."""
        for feat in self._walk_features(doc):
            if com_get(feat, "Name", default="") == name:
                return feat
        return None

    def _feature_names(self, doc) -> List[str]:
        """All feature names (top level + subfeatures)."""
        return [com_get(f, "Name", default="?")
                for f in self._walk_features(doc)]

    def _body_names(self, doc) -> List[str]:
        """Names of all solid bodies (hidden included)."""
        return [com_get(b, "Name", default="?")
                for b in self._get_solid_bodies(doc)]

    def _feature_bbox_m(self, feat):
        """
        Union bbox of the feature's faces in METERS, as (mins, maxs) lists.
        Returns None if the feature has no faces / boxes.
        """
        faces = com_get(feat, "GetFaces", default=None)
        if not faces:
            return None
        mins = [float("inf")] * 3
        maxs = [float("-inf")] * 3
        got = False
        for face in faces:
            try:
                box = com_get(face, "GetBox", default=None)
            except Exception:
                box = None
            if box and len(box) >= 6:
                got = True
                for i in range(3):
                    mins[i] = min(mins[i], float(box[i]))
                    maxs[i] = max(maxs[i], float(box[i + 3]))
        return (mins, maxs) if got else None

    def _feature_bbox(self, feat, unit=None):
        """
        Union bbox of the feature's faces in USER units:
        {"min": [x,y,z], "max": [x,y,z]} or None. This is the single most
        valuable piece of diagnostics after extrude/cut - it tells WHERE
        the geometry actually ended up (flag semantics are treacherous).
        """
        mm = self._feature_bbox_m(feat)
        if mm is None:
            return None
        conv = self._units.from_meters
        return {"min": [round(conv(v, unit), 4) for v in mm[0]],
                "max": [round(conv(v, unit), 4) for v in mm[1]]}

    def _rename_feature_safe(self, doc, feat, requested: str):
        """
        Rename a feature reading back the ACTUAL name. SolidWorks silently
        keeps the default name (e.g. 'Cut-Extrude1') when the requested name
        is already taken - so on collision an auto-suffixed name (_2, _3...)
        is applied and a warning is returned.

        Returns (actual_name, warning_or_None).
        """
        current = com_get(feat, "Name", default="")
        if not requested or requested == current:
            return current, None

        existing = set(self._feature_names(doc))
        existing.discard(current)

        target = requested
        if target in existing:
            i = 2
            while f"{requested}_{i}" in existing and i < 1000:
                i += 1
            target = f"{requested}_{i}"

        try:
            feat.Name = target
        except Exception as e:
            logger.debug(f"Feature rename failed: {e}")

        actual = com_get(feat, "Name", default=current)
        warning = None
        if target != requested and actual == target:
            warning = (f"feature_name '{requested}' already exists - "
                       f"feature was named '{actual}' instead")
        elif actual != target:
            warning = (f"Rename to '{requested}' did not stick; actual "
                       f"feature name is '{actual}'")
        return actual, warning

    def _select_feature(self, doc, feat, name: str) -> bool:
        """Select a feature object (Select2 with SelectByID2 fallback)."""
        try:
            doc.ClearSelection2(True)
        except Exception:
            pass
        try:
            if bool(feat.Select2(False, 0)):
                return True
        except Exception as e:
            logger.debug(f"feat.Select2 failed: {e}")
        for sel_type in ("BODYFEATURE", "SKETCH", "REFPLANE", "REFAXIS"):
            if select_by_id2(doc, name, sel_type):
                return True
        return False

    def _delete_feature_object(self, doc, feat, name: str,
                               delete_absorbed: bool = False) -> bool:
        """Delete a feature object. Returns True on success.

        When delete_absorbed is set, absorbed sub-features (typically the
        profile sketch) are collected by name first and deleted explicitly
        afterwards - DeleteSelection2's swDelete_Absorbed flag leaves the
        sketch orphaned on SW2026 rather than removing it.
        """
        absorbed_names = []
        if delete_absorbed:
            sub = com_get(feat, "GetFirstSubFeature", default=None)
            while sub is not None:
                nm = com_get(sub, "Name", default=None)
                if nm:
                    absorbed_names.append(nm)
                sub = com_get(sub, "GetNextSubFeature", default=None)

        if not self._select_feature(doc, feat, name):
            return False
        # swDelete_Absorbed=1 | swDelete_Children=2
        option = 3 if delete_absorbed else 0
        deleted = False
        try:
            deleted = bool(doc.Extension.DeleteSelection2(option))
        except Exception as e:
            logger.debug(f"DeleteSelection2 failed: {e}")
        if not deleted:
            try:
                doc.EditDelete()
                deleted = self._find_feature(doc, name) is None
            except Exception as e:
                logger.debug(f"EditDelete failed: {e}")
                return False

        # Explicitly remove any absorbed sketches left orphaned
        for nm in absorbed_names:
            orphan = self._find_feature(doc, nm)
            if orphan is not None:
                try:
                    doc.ClearSelection2(True)
                    if orphan.Select2(False, 0):
                        doc.Extension.DeleteSelection2(0)
                except Exception as e:
                    logger.debug(f"Absorbed delete '{nm}' failed: {e}")

        return deleted

    # ========================================================================
    # Feature management tools
    # ========================================================================

    def delete_feature(self, name: str, delete_absorbed: bool = False) -> Dict:
        """
        Delete a feature by name.

        Args:
            name: Feature name (see list_features)
            delete_absorbed: Also delete absorbed features/sketches
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            feat = self._find_feature(doc, name)
            if feat is None:
                return self._result(False,
                    f"Feature '{name}' not found",
                    SwErrors.swSelectionError,
                    {"existing_features": self._feature_names(doc)})

            if not self._delete_feature_object(doc, feat, name,
                                               delete_absorbed):
                return self._result(False,
                    f"Could not delete feature '{name}'",
                    SwErrors.swFeatureError)

            return self._result(True, f"Feature '{name}' deleted",
                              SwErrors.swSuccess,
                              {"deleted": name,
                               "delete_absorbed": delete_absorbed})
        except Exception as e:
            logger.error(f"Delete feature error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    def rename_feature(self, old_name: str, new_name: str) -> Dict:
        """
        Rename a feature. On name collision the feature gets an
        auto-suffixed name (_2, _3...) and a warning is returned - the
        previous implementation reported success with the REQUESTED name
        while SolidWorks silently kept the default one.
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            feat = self._find_feature(doc, old_name)
            if feat is None:
                return self._result(False,
                    f"Feature '{old_name}' not found",
                    SwErrors.swSelectionError,
                    {"existing_features": self._feature_names(doc)})

            actual, warning = self._rename_feature_safe(doc, feat, new_name)

            if actual == old_name and new_name != old_name:
                return self._result(False,
                    f"Rename did not stick ('{old_name}' -> '{new_name}')",
                    SwErrors.swUnknownError,
                    {"old_name": old_name, "requested_name": new_name,
                     "actual_name": actual})

            return self._result(True,
                f"Feature renamed: '{old_name}' -> '{actual}'"
                + (f" WARNING: {warning}" if warning else ""),
                SwErrors.swSuccess,
                {"old_name": old_name, "requested_name": new_name,
                 "actual_name": actual, "rename_warning": warning})
        except Exception as e:
            logger.error(f"Rename feature error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    def get_feature_status(self, name: str) -> Dict:
        """
        Get feature health status. The key check is GetFaces:
        0 faces = dead feature (SolidWorks creates them silently for
        infeasible parameters). GetTypeName2 is reported but unreliable
        ("ICE" even for healthy extrusions via dynamic access).
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            feat = self._find_feature(doc, name)
            if feat is None:
                return self._result(False,
                    f"Feature '{name}' not found",
                    SwErrors.swSelectionError,
                    {"existing_features": self._feature_names(doc)})

            face_count = feature_face_count(feat)
            type_name = com_get(feat, "GetTypeName2", default=None)
            suppressed = bool(com_get(feat, "IsSuppressed", default=False))

            # Sketches and reference geometry legitimately have no faces
            no_face_types = ("ProfileFeature", "RefPlane", "RefAxis",
                             "OriginProfileFeature", "CommentsFolder",
                             "FeatureFolder", "MaterialFolder",
                             "HistoryFolder", "SensorFolder",
                             "DetailCabinet", "SelectionSetFolder",
                             "SolidBodyFolder", "SurfaceBodyFolder",
                             "EnvFolder", "AmbientLight", "DirectionLight",
                             "OriginFeature", "3DProfileFeature")
            geometry_feature = type_name not in no_face_types

            alive = (face_count > 0) if geometry_feature else True

            status = "OK" if alive else "DEAD (0 faces)"
            if suppressed:
                status = "SUPPRESSED"

            return self._result(True,
                f"Feature '{name}': {status}, {face_count} face(s), "
                f"type={type_name}",
                SwErrors.swSuccess,
                {"name": name,
                 "type": type_name,
                 "type_note": "GetTypeName2 unreliable for health checks "
                              "(may report ICE for healthy features)",
                 "suppressed": suppressed,
                 "face_count": face_count,
                 "alive": alive})
        except Exception as e:
            logger.error(f"Feature status error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Advanced extrude / cut (typed IFeatureManager)
    # ========================================================================

    def _prepare_profile_selection(self, doc, sketch_name: Optional[str]):
        """
        Close active sketch, select the profile sketch (Mark=0).
        Returns (sketch_name, error_result_or_None).
        """
        try:
            if doc.SketchManager.ActiveSketch is not None:
                doc.SketchManager.InsertSketch(True)
        except Exception:
            pass

        try:
            doc.ClearSelection2(True)
        except Exception:
            pass

        if not sketch_name:
            sketch_name = self._find_last_sketch(doc)
        if not sketch_name:
            return None, self._result(False,
                "No sketch found in feature tree",
                SwErrors.swSketchError,
                {"diagnostics": self._get_sketch_info(doc)})

        if not select_by_id2(doc, sketch_name, "SKETCH",
                             mark=int(SwFeatureMarks.PROFILE)):
            return sketch_name, self._result(False,
                f"Could not select sketch '{sketch_name}'",
                SwErrors.swSelectionError,
                {"diagnostics": self._get_sketch_info(doc)})

        return sketch_name, None

    def _select_ray_reference(self, doc, ray: Dict, mark: int,
                              unit: Optional[str]):
        """
        Select a face by ray with a given mark (append to selection).
        ray = {"origin": [x,y,z], "direction": [dx,dy,dz]} in user units.
        Returns error_result or None.
        """
        try:
            origin_m = tuple(self._units.to_meters(c, unit)
                             for c in ray["origin"])
            direction = tuple(float(c) for c in ray["direction"])
        except Exception as e:
            return self._result(False,
                f"Invalid ray specification: {e}. Expected "
                "{{'origin': [x,y,z], 'direction': [dx,dy,dz]}}",
                SwErrors.swInvalidInput)

        if not select_by_ray(doc, origin_m, direction,
                             sel_type=int(SwSelectTypeCode.swSelFACES),
                             append=True, mark=mark):
            return self._result(False,
                f"Reference face ray missed (origin={ray['origin']}, "
                f"direction={ray['direction']}, mark={mark})",
                SwErrors.swSelectionError)
        return None

    def _select_scope_bodies(self, doc, scope_bodies: List[str]):
        """
        Select bodies for cut feature scope with Mark=8.
        Returns error_result or None.
        """
        for body_name in scope_bodies:
            body = self._find_body(doc, body_name)
            if body is None:
                return self._result(False,
                    f"Scope body '{body_name}' not found",
                    SwErrors.swSelectionError)
            sel_data = create_select_data(
                doc, mark=int(SwFeatureMarks.CUT_SCOPE_BODY))
            try:
                ok = bool(body.Select2(True, sel_data))
            except Exception as e:
                logger.debug(f"body.Select2 failed: {e}")
                ok = False
            if not ok:
                return self._result(False,
                    f"Could not select scope body '{body_name}' (Mark=8)",
                    SwErrors.swSelectionError)
        return None

    def _flag_candidates(self, params):
        """
        Ordered list of (direction_flip, offset_reverse, flip_start_offset)
        combinations to try in auto_flags mode. The user-supplied combo is
        first; only flags relevant to the conditions are varied.
        """
        end_c = params["end_condition"]
        start_c = params["start_condition"]
        d0 = bool(params["direction_flip"])
        o0 = bool(params["offset_reverse"])
        f0 = bool(params["flip_start_offset"])

        dflips = [d0, not d0]
        orevs = [o0, not o0] if end_c in REF_FACE_CONDITIONS else [o0]
        fstarts = [f0, not f0] if start_c == "offset" else [f0]

        combos = []
        for d in dflips:
            for o in orevs:
                for f in fstarts:
                    combo = (d, o, f)
                    if combo not in combos:
                        combos.append(combo)
        return combos

    def _create_with_flag_search(self, is_cut, sketch_name, params, unit):
        """Try flag combinations, return the first that yields a live feature."""
        attempts = []
        for (d, o, f) in self._flag_candidates(params):
            trial = dict(params)
            trial["direction_flip"] = d
            trial["offset_reverse"] = o
            trial["flip_start_offset"] = f
            trial["auto_flags"] = False
            trial["auto_verify"] = True
            res = self._create_extrusion_feature(is_cut, sketch_name, trial, unit)
            attempt = {"flags": {"direction_flip": d,
                                 "offset_reverse": o,
                                 "flip_start_offset": f},
                       "success": res["success"],
                       "message": res["message"][:160]}
            # Record WHERE each variant landed (available for successful
            # variants and for expected_bbox/expected_merge rollbacks)
            res_data = res.get("data") or {}
            if res_data.get("feature_bbox"):
                attempt["feature_bbox"] = res_data["feature_bbox"]
            if res_data.get("merged_bodies"):
                attempt["merged_bodies"] = res_data["merged_bodies"]
            attempts.append(attempt)
            if res["success"]:
                res.setdefault("data", {})["auto_flags_attempts"] = attempts
                res["data"]["chosen_flags"] = {"direction_flip": d,
                                               "offset_reverse": o,
                                               "flip_start_offset": f}
                return res
        op = "Cut" if is_cut else "Extrude"
        return self._result(False,
            f"{op}: no flag combination produced a valid feature "
            f"({len(attempts)} tried). Check geometry/conditions.",
            SwErrors.swFeatureError, {"auto_flags_attempts": attempts})

    def _create_extrusion_feature(self, is_cut: bool, sketch_name, params,
                                  unit) -> Dict:
        """Shared implementation for advanced_extrude / advanced_cut."""
        if params.get("auto_flags"):
            return self._create_with_flag_search(is_cut, sketch_name,
                                                 params, unit)

        doc, err = self.get_active_doc()
        if err:
            return err

        # Freeze Bar check is mandatory before any feature-creating call
        freeze_info = self.ensure_features_not_frozen(doc)

        end_condition = params["end_condition"]
        if end_condition not in END_CONDITION_MAP:
            return self._result(False,
                f"Unknown end_condition '{end_condition}'. "
                f"Valid: {sorted(END_CONDITION_MAP)}",
                SwErrors.swInvalidInput)
        start_condition = params["start_condition"]
        if start_condition not in START_CONDITION_MAP:
            return self._result(False,
                f"Unknown start_condition '{start_condition}'. "
                f"Valid: {sorted(START_CONDITION_MAP)}",
                SwErrors.swInvalidInput)

        double_ended = end_condition == "through_all_both"
        if double_ended:
            t1 = END_CONDITION_MAP["through_all"]
            t2 = END_CONDITION_MAP["through_all"]
        else:
            t1 = END_CONDITION_MAP[end_condition]
            t2 = 0
        t0 = START_CONDITION_MAP[start_condition]
        depth_m = self._units.to_meters(params["depth"], unit)
        start_offset_m = self._units.to_meters(params["start_offset"], unit)

        # Profile selection (Mark=0)
        sketch_name, sel_err = self._prepare_profile_selection(doc, sketch_name)
        if sel_err:
            return sel_err

        # End-condition reference face (Mark=1)
        if end_condition in REF_FACE_CONDITIONS:
            if not params.get("ref_face_ray"):
                return self._result(False,
                    f"end_condition '{end_condition}' requires ref_face_ray",
                    SwErrors.swInvalidInput)
            ray_err = self._select_ray_reference(
                doc, params["ref_face_ray"],
                int(SwFeatureMarks.END_REFERENCE), unit)
            if ray_err:
                return ray_err

        # Start-condition reference face (Mark=32)
        if start_condition == "surface":
            if not params.get("start_face_ray"):
                return self._result(False,
                    "start_condition 'surface' requires start_face_ray",
                    SwErrors.swInvalidInput)
            ray_err = self._select_ray_reference(
                doc, params["start_face_ray"],
                int(SwFeatureMarks.START_REFERENCE), unit)
            if ray_err:
                return ray_err

        # Cut feature scope bodies (Mark=8)
        use_feat_scope = False
        use_auto_select = True
        scope_bodies = params.get("scope_bodies") or []
        scoped_body_records = {}
        if is_cut and scope_bodies:
            scope_err = self._select_scope_bodies(doc, scope_bodies)
            if scope_err:
                return scope_err
            for body_name in scope_bodies:
                body = self._find_body(doc, body_name)
                bbox = com_get(body, "GetBodyBox", default=None)
                scoped_body_records[body_name] = {
                    "body": body,
                    "bbox_m": list(bbox) if bbox and len(bbox) >= 6 else None,
                }
            use_feat_scope = True
            use_auto_select = False

        body_names_before = self._body_names(doc)
        bodies_before = len(body_names_before)

        # Typed IFeatureManager is mandatory: dynamic dispatch raises
        # COM 61704 "Internal application error" on FeatureExtrusion3
        fm = typed(doc.FeatureManager, "IFeatureManager")
        fm_typed = fm is not None
        if fm is None:
            logger.warning("Typed IFeatureManager unavailable, "
                           "falling back to dynamic dispatch (may fail)")
            fm = doc.FeatureManager

        direction_flip = bool(params["direction_flip"])
        offset_reverse = bool(params["offset_reverse"])
        translate_surface = bool(params["translate_surface"])
        flip_start = bool(params["flip_start_offset"])

        feat = None
        api_error = None
        try:
            if is_cut:
                feat = fm.FeatureCut4(
                    not double_ended,    # Sd - false for double ended
                    False,               # Flip - flip side to cut
                    direction_flip,      # Dir
                    t1, t2,              # T1, T2 end conditions
                    depth_m, 0.0,        # D1, D2
                    False, False,        # Dchk1, Dchk2 (draft)
                    False, False,        # Ddir1, Ddir2
                    0.0, 0.0,            # Dang1, Dang2
                    offset_reverse, False,      # OffsetReverse1, 2
                    translate_surface, False,   # TranslateSurface1, 2
                    bool(params.get("normal_cut", False)),  # NormalCut
                    use_feat_scope,      # UseFeatScope
                    use_auto_select,     # UseAutoSelect
                    False, False, False,  # Assembly scope params
                    t0,                  # T0 start condition
                    start_offset_m,      # StartOffset
                    flip_start,          # FlipStartOffset
                    bool(params.get("optimize_geometry", False)))
            else:
                feat = fm.FeatureExtrusion3(
                    not double_ended,    # Sd - false for double ended
                    False,               # Flip
                    direction_flip,      # Dir
                    t1, t2,              # T1, T2
                    depth_m, 0.0,        # D1, D2
                    False, False,        # Dchk1, Dchk2
                    False, False,        # Ddir1, Ddir2
                    0.0, 0.0,            # Dang1, Dang2
                    offset_reverse, False,      # OffsetReverse1, 2
                    translate_surface, False,   # TranslateSurface1, 2
                    bool(params["merge"]),      # Merge
                    True,                # UseFeatScope
                    True,                # UseAutoSelect
                    t0,                  # T0
                    start_offset_m,      # StartOffset
                    flip_start)          # FlipStartOffset
        except Exception as e:
            api_error = str(e)
            logger.error(f"Feature API call failed: {e}")

        op = "Cut" if is_cut else "Extrude"

        if feat is None:
            modal_info = detect_modal_dialog()
            return self._result(False,
                f"{op} failed on sketch '{sketch_name}'"
                + (f": {api_error}" if api_error else "")
                + (". A modal dialog is blocking SolidWorks!"
                   if modal_info.get("modal") else ""),
                SwErrors.swFeatureError,
                {"sketch_name": sketch_name,
                 "api_error": api_error,
                 "typed_feature_manager": fm_typed,
                 "freeze_bar": freeze_info,
                 "modal_dialog": modal_info,
                 "flags": {"direction_flip": direction_flip,
                           "offset_reverse": offset_reverse,
                           "flip_start_offset": flip_start}})

        feat_name = com_get(feat, "Name", default="<unknown>")
        face_count = feature_face_count(feat)

        scope_feature_renames = []
        scope_feature_rename_objects = []

        def rollback_scope_feature_renames():
            failures = []
            for feature, old_name, new_name in reversed(
                    scope_feature_rename_objects):
                try:
                    feature.Name = old_name
                except Exception as exc:
                    failures.append({
                        "from": new_name, "to": old_name,
                        "reason": str(exc)})
                    continue
                actual = com_get(feature, "Name", default=new_name)
                if actual != old_name:
                    failures.append({
                        "from": new_name, "to": old_name,
                        "actual_name": actual,
                        "reason": "rename_did_not_stick"})
            return failures

        def _bbox_match_distance(reference, candidate):
            if not reference or not candidate or len(candidate) < 6:
                return None
            reference = [float(value) for value in reference[:6]]
            candidate = [float(value) for value in candidate[:6]]
            scale = max(
                1e-12,
                sum(abs(reference[index + 3] - reference[index])
                    for index in range(3)))
            return sum(abs(reference[index] - candidate[index])
                       for index in range(6)) / scale

        def find_current_scoped_body(requested_name, claimed_names):
            direct = self._find_body(doc, requested_name)
            if direct is not None and requested_name not in claimed_names:
                return direct, None
            untouched_names = set(body_names_before) - set(scope_bodies)
            candidates = []
            for candidate in self._get_solid_bodies(doc):
                candidate_name = com_get(candidate, "Name", default="?")
                if (candidate_name in untouched_names or
                        candidate_name in claimed_names):
                    continue
                candidate_box = com_get(
                    candidate, "GetBodyBox", default=None)
                score = _bbox_match_distance(
                    scoped_body_records[requested_name]["bbox_m"],
                    candidate_box)
                candidates.append((score, candidate_name, candidate))
            if not candidates:
                return None, "post_cut_body_not_found"
            if len(candidates) == 1:
                return candidates[0][2], None
            scored = [item for item in candidates if item[0] is not None]
            if not scored:
                return None, "post_cut_body_match_is_ambiguous"
            scored.sort(key=lambda item: item[0])
            if (len(scored) > 1 and
                    abs(scored[0][0] - scored[1][0]) <= 1e-9):
                return None, "post_cut_body_match_is_ambiguous"
            return scored[0][2], None

        def restore_scoped_body_names():
            restorations, failures = [], []
            claimed_names = set()
            for requested_name in scope_bodies:
                body, match_error = find_current_scoped_body(
                    requested_name, claimed_names)
                if body is None:
                    failures.append({
                        "requested_name": requested_name,
                        "reason": match_error or "body_reference_missing"})
                    continue
                current_name = com_get(body, "Name", default=None)
                if current_name == requested_name:
                    claimed_names.add(requested_name)
                    continue
                try:
                    body.Name = requested_name
                except Exception as exc:
                    failures.append({
                        "requested_name": requested_name,
                        "actual_name": current_name,
                        "reason": str(exc)})
                    continue
                actual_name = com_get(body, "Name", default=current_name)
                if actual_name != requested_name:
                    conflict = self._find_feature(doc, requested_name)
                    if conflict is None:
                        failures.append({
                            "requested_name": requested_name,
                            "actual_name": actual_name,
                            "reason": "rename_did_not_stick"})
                        continue
                    conflict_name = com_get(
                        conflict, "Name", default=requested_name)
                    replacement, warning = self._rename_feature_safe(
                        doc, conflict, f"{requested_name}_SourceFeature")
                    if replacement == conflict_name:
                        failures.append({
                            "requested_name": requested_name,
                            "actual_name": actual_name,
                            "reason": "blocking_feature_rename_failed",
                            "blocking_feature": conflict_name,
                            "rename_warning": warning})
                        continue
                    scope_feature_renames.append({
                        "from": conflict_name,
                        "to": replacement,
                        "reason": "body_name_namespace_collision",
                        "rename_warning": warning})
                    scope_feature_rename_objects.append(
                        (conflict, conflict_name, replacement))
                    body, match_error = find_current_scoped_body(
                        requested_name, claimed_names)
                    if body is None:
                        failures.append({
                            "requested_name": requested_name,
                            "reason": match_error or
                            "post_feature_rename_body_not_found"})
                        continue
                    current_name = com_get(body, "Name", default=actual_name)
                    try:
                        body.Name = requested_name
                    except Exception as exc:
                        failures.append({
                            "requested_name": requested_name,
                            "actual_name": current_name,
                            "reason": str(exc)})
                        continue
                    actual_name = com_get(
                        body, "Name", default=current_name)
                    if actual_name != requested_name:
                        failures.append({
                            "requested_name": requested_name,
                            "actual_name": actual_name,
                            "reason": "rename_did_not_stick_after_"
                            "feature_collision_resolution"})
                        continue
                restorations.append({
                    "from": current_name, "to": requested_name})
                claimed_names.add(requested_name)
            return restorations, failures

        scope_name_restorations = []

        # Dead-feature verification: SW silently creates empty features
        if params.get("auto_verify", True) and face_count == 0:
            deleted = self._delete_feature_object(doc, feat, feat_name)
            modal_info = detect_modal_dialog()
            bodies_after_dead = len(self._body_names(doc))
            return self._result(False,
                f"{op} created a DEAD feature '{feat_name}' (0 faces) - "
                f"{'deleted' if deleted else 'DELETE FAILED, remove manually'}."
                f" Check flags (Boss vs Cut semantics differ: Cut "
                f"offset-from-surface usually needs direction_flip=True) "
                f"and geometry feasibility.",
                SwErrors.swFeatureError,
                {"feature_name": feat_name,
                 "dead_feature_deleted": deleted,
                 "bodies_before": bodies_before,
                 "bodies_after": bodies_after_dead,
                 "freeze_bar": freeze_info,
                 "modal_dialog": modal_info,
                 "flags": {"direction_flip": direction_flip,
                           "offset_reverse": offset_reverse,
                           "flip_start_offset": flip_start}})

        unit_str = unit or self._units.default_unit.value

        # WHERE did the geometry end up - the single most valuable piece
        # of diagnostics (auto_flags can pick a live-but-wrong side, and
        # flip_start_offset semantics are treacherous).
        feature_bbox = self._feature_bbox(feat, unit)

        # Guard: expected_bbox - roll back if the feature landed outside
        # the expected zone (user units; optional per-call tolerance).
        expected_bbox = params.get("expected_bbox")
        if expected_bbox and feature_bbox:
            tol = float(expected_bbox.get("tolerance", 0.5))
            exp_min = expected_bbox.get("min")
            exp_max = expected_bbox.get("max")
            if exp_min and exp_max:
                outside = []
                for i, ax in enumerate("xyz"):
                    if (feature_bbox["min"][i] < float(exp_min[i]) - tol or
                            feature_bbox["max"][i] > float(exp_max[i]) + tol):
                        outside.append(ax)
                if outside:
                    deleted = self._delete_feature_object(doc, feat, feat_name)
                    return self._result(False,
                        f"{op} '{feat_name}' landed OUTSIDE the expected "
                        f"zone (axes: {','.join(outside)}): actual bbox "
                        f"{feature_bbox['min']}..{feature_bbox['max']}, "
                        f"expected {exp_min}..{exp_max} (tol {tol}). Feature "
                        f"{'rolled back' if deleted else 'ROLLBACK FAILED'}. "
                        f"Try flipping flip_start_offset/direction_flip or "
                        f"auto_flags=true.",
                        SwErrors.swFeatureError,
                        {"feature_name": feat_name,
                         "feature_deleted": deleted,
                         "feature_bbox": feature_bbox,
                         "expected_bbox": expected_bbox,
                         "bbox_unit": unit_str})

        # Optional rename - reads back the ACTUAL name, auto-suffixes on
        # collision (SW silently keeps the default name otherwise).
        rename_warning = None
        if params.get("feature_name"):
            feat_name, rename_warning = self._rename_feature_safe(
                doc, feat, params["feature_name"])

        if scoped_body_records:
            restored, restore_failures = restore_scoped_body_names()
            scope_name_restorations.extend(restored)
            if restore_failures:
                deleted = self._delete_feature_object(
                    doc, feat, feat_name, delete_absorbed=False)
                feature_rollback_failures = (
                    rollback_scope_feature_renames())
                return self._result(
                    False,
                    f"Cut '{feat_name}' could not preserve scoped body names",
                    SwErrors.swFeatureError,
                    {"feature_name": feat_name,
                     "feature_deleted": deleted,
                     "scope_bodies": list(scope_bodies),
                     "scope_name_restore_failures": restore_failures,
                     "scope_feature_renames": scope_feature_renames,
                     "scope_feature_rename_rollback_failures":
                         feature_rollback_failures})
            feat_name = com_get(feat, "Name", default=feat_name)

        # SOLIDWORKS renames a body's display name together with its owning
        # feature. Re-read the complete body contract only after scoped-name
        # restoration, so a topology-preserving cut is not reported as one
        # removed body plus one new body.
        body_names_after = self._body_names(doc)
        bodies_after = len(body_names_after)
        merged_bodies = [name for name in body_names_before
                         if name not in body_names_after]
        new_bodies = [name for name in body_names_after
                      if name not in body_names_before]
        merge_data = {
            "body_names_before": body_names_before,
            "body_names_after": body_names_after,
            "merged_bodies": merged_bodies,
            "new_bodies": new_bodies,
        }

        # Guard: expected_merge_bodies - roll back if the merge swallowed
        # bodies the caller did not expect to be touched.
        expected_merge = params.get("expected_merge_bodies")
        if expected_merge is not None and merged_bodies:
            unexpected = [n for n in merged_bodies if n not in expected_merge]
            if unexpected:
                deleted = self._delete_feature_object(doc, feat, feat_name)
                feature_rollback_failures = (
                    rollback_scope_feature_renames())
                rollback = ("rolled back" if deleted
                            else "ROLLBACK FAILED, delete manually")
                return self._result(False,
                    f"{op} '{feat_name}' merged UNEXPECTED bodies "
                    f"{unexpected} (allowed: {expected_merge}) - feature "
                    f"{rollback}. Bodies with coincident faces merge "
                    f"silently; add a clearance or use merge=false.",
                    SwErrors.swFeatureError,
                    {"feature_name": feat_name,
                     "feature_deleted": deleted,
                     "unexpected_merged": unexpected,
                     "expected_merge_bodies": expected_merge,
                     "feature_bbox": feature_bbox, "bbox_unit": unit_str,
                     "scope_feature_rename_rollback_failures":
                         feature_rollback_failures,
                     **merge_data})

        merge_note = (f", merged: {merged_bodies}" if merged_bodies else "")
        bbox_note = ""
        if feature_bbox:
            bbox_note = (f", bbox {feature_bbox['min']}.."
                         f"{feature_bbox['max']} {unit_str}")
        warn_note = f" WARNING: {rename_warning}" if rename_warning else ""
        return self._result(True,
            f"{op} '{feat_name}': {face_count} face(s), "
            f"bodies {bodies_before} -> {bodies_after}{merge_note}{bbox_note}"
            f" [{end_condition}, typed={fm_typed}]{warn_note}",
            SwErrors.swSuccess,
            {"feature_name": feat_name,
             "requested_name": params.get("feature_name"),
             "rename_warning": rename_warning,
             "face_count": face_count,
             "feature_bbox": feature_bbox,
             "bbox_unit": unit_str,
             "bodies_before": bodies_before,
             "bodies_after": bodies_after,
             **merge_data,
             "sketch_name": sketch_name,
             "end_condition": end_condition,
             "start_condition": params["start_condition"],
             "depth": params["depth"],
             "unit": unit_str,
             "scope_bodies": list(scope_bodies),
             "scope_name_restorations": scope_name_restorations,
             "scope_feature_renames": scope_feature_renames,
             "double_ended": bool(double_ended),
             "typed_feature_manager": fm_typed})

    def advanced_extrude(self, sketch_name: str = None,
                         end_condition: str = "blind",
                         depth: float = 10.0,
                         direction_flip: bool = False,
                         offset_reverse: bool = False,
                         translate_surface: bool = False,
                         merge: bool = True,
                         start_condition: str = "sketch_plane",
                         start_offset: float = 0.0,
                         flip_start_offset: bool = False,
                         ref_face_ray: Dict = None,
                         start_face_ray: Dict = None,
                         feature_name: str = None,
                         auto_verify: bool = True,
                         auto_flags: bool = False,
                         expected_bbox: Dict = None,
                         expected_merge_bodies: List[str] = None,
                         unit: str = None) -> Dict:
        """
        Full-featured Boss-Extrude via typed IFeatureManager.FeatureExtrusion3.
        See advanced_cut for the cut variant. depth is used as the offset
        distance for end_condition='offset_from_surface'.
        Verified SW2026 combo - Boss offset-from-surface:
        direction_flip=False, offset_reverse=False.

        Diagnostics returned in data: feature_bbox (WHERE the geometry
        landed), body names before/after, merged_bodies. Guards:
        - expected_bbox={"min":[x,y,z],"max":[x,y,z],"tolerance":0.5}:
          roll back + error if the feature lands outside the zone;
        - expected_merge_bodies=["Name"]: roll back + error if the merge
          swallowed any body not in the list.
        """
        try:
            params = {
                "end_condition": end_condition,
                "depth": depth,
                "direction_flip": direction_flip,
                "offset_reverse": offset_reverse,
                "translate_surface": translate_surface,
                "merge": merge,
                "start_condition": start_condition,
                "start_offset": start_offset,
                "flip_start_offset": flip_start_offset,
                "ref_face_ray": ref_face_ray,
                "start_face_ray": start_face_ray,
                "feature_name": feature_name,
                "auto_verify": auto_verify,
                "auto_flags": auto_flags,
                "expected_bbox": expected_bbox,
                "expected_merge_bodies": expected_merge_bodies,
            }
            return self._create_extrusion_feature(False, sketch_name,
                                                  params, unit)
        except Exception as e:
            logger.error(f"Advanced extrude error: {e}\n"
                         f"{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    def advanced_cut(self, sketch_name: str = None,
                     end_condition: str = "blind",
                     depth: float = 10.0,
                     direction_flip: bool = False,
                     offset_reverse: bool = False,
                     translate_surface: bool = False,
                     start_condition: str = "sketch_plane",
                     start_offset: float = 0.0,
                     flip_start_offset: bool = False,
                     ref_face_ray: Dict = None,
                     start_face_ray: Dict = None,
                     scope_bodies: List[str] = None,
                     normal_cut: bool = False,
                     optimize_geometry: bool = False,
                     feature_name: str = None,
                     auto_verify: bool = True,
                     auto_flags: bool = False,
                     expected_bbox: Dict = None,
                     expected_merge_bodies: List[str] = None,
                     unit: str = None) -> Dict:
        """
        Full-featured Cut-Extrude via typed IFeatureManager.FeatureCut4.
        scope_bodies limits the cut to the named bodies (Mark=8 selection) -
        without it a cut in a multibody part damages other bodies.
        Verified SW2026 combos:
        - Cut offset-from-surface (same side as the equivalent Boss):
          direction_flip=True, offset_reverse=False;
        - Cut with outside start-offset: flip_start_offset=False,
          direction_flip=False, offset_reverse=True.

        Same diagnostics/guards as advanced_extrude: feature_bbox, body
        names before/after, merged_bodies, expected_bbox and
        expected_merge_bodies (roll back on violation). Combine
        auto_flags=true + expected_bbox to search for the flag combination
        that puts the cut in the right zone, not just a live one.
        """
        try:
            params = {
                "end_condition": end_condition,
                "depth": depth,
                "direction_flip": direction_flip,
                "offset_reverse": offset_reverse,
                "translate_surface": translate_surface,
                "merge": True,
                "start_condition": start_condition,
                "start_offset": start_offset,
                "flip_start_offset": flip_start_offset,
                "ref_face_ray": ref_face_ray,
                "start_face_ray": start_face_ray,
                "scope_bodies": scope_bodies,
                "normal_cut": normal_cut,
                "optimize_geometry": optimize_geometry,
                "feature_name": feature_name,
                "auto_verify": auto_verify,
                "auto_flags": auto_flags,
                "expected_bbox": expected_bbox,
                "expected_merge_bodies": expected_merge_bodies,
            }
            return self._create_extrusion_feature(True, sketch_name,
                                                  params, unit)
        except Exception as e:
            logger.error(f"Advanced cut error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)
