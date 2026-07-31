"""
SolidWorks Feature Operations
-----------------------------
Create 3D features: extrude, cut, fillet, chamfer, etc.

Version: 4.0.0 (Fixed for SolidWorks 2025 - v33)
Author: Samsaam Ali Baig

Fixes v4.0.0:
- FeatureExtrusion2 now uses correct 23 parameters for SW 2025
- Proper sketch close + select before extrude (fixes multi-profile sketches)
- Property vs method fixes (FirstFeature, GetNextFeature, GetTypeName2)
- Added _find_last_sketch helper for reliable sketch selection
- Added _get_sketch_info helper for better error diagnostics
- Better error messages with sketch profile count
"""

import logging
import traceback
from typing import Optional, Dict

import win32com.client
import pythoncom

from ..constants import SwErrors, SwEndConditions
from .com_utils import com_get

logger = logging.getLogger(__name__)


class FeatureOperations:
    """
    Mixin class for feature operations

    Requires parent class to have:
    - get_active_doc(): Document access method
    - _result(): Result factory method
    - _units: UnitConverter instance
    """

    # ========================================================================
    # Helper Methods
    # ========================================================================

    def _find_last_sketch(self, doc) -> Optional[str]:
        """
        Find the name of the last sketch in the feature tree.
        Uses properties (not method calls) for SW 2025 compatibility.

        Returns:
            Sketch name string or None
        """
        last_sketch = None
        try:
            feat = com_get(doc, "FirstFeature", default=None)
            while feat is not None:
                try:
                    feat_type = com_get(feat, "GetTypeName2", default=None)
                    if feat_type == "ProfileFeature":
                        last_sketch = com_get(feat, "Name", default=None)
                except:
                    pass
                feat = com_get(feat, "GetNextFeature", default=None)
        except Exception as e:
            logger.debug(f"_find_last_sketch error: {e}")
        return last_sketch

    def _get_sketch_info(self, doc) -> Dict:
        """
        Get diagnostic info about sketches in the document.
        Useful for error messages.

        Returns:
            Dict with sketch_count, sketch_names, has_active_sketch
        """
        info = {
            "sketch_count": 0,
            "sketch_names": [],
            "has_active_sketch": False,
            "feature_count": 0
        }
        try:
            # Check if sketch is active
            try:
                active_sketch = doc.SketchManager.ActiveSketch
                info["has_active_sketch"] = active_sketch is not None
            except:
                pass

            # Count sketches and features
            feat = com_get(doc, "FirstFeature", default=None)
            while feat is not None:
                try:
                    feat_type = com_get(feat, "GetTypeName2", default=None)
                    info["feature_count"] += 1
                    if feat_type == "ProfileFeature":
                        info["sketch_count"] += 1
                        info["sketch_names"].append(
                            com_get(feat, "Name", default="?"))
                except:
                    pass
                feat = com_get(feat, "GetNextFeature", default=None)
        except Exception as e:
            logger.debug(f"_get_sketch_info error: {e}")
        return info

    def _close_and_select_sketch(self, doc) -> tuple:
        """
        Close active sketch if open, find and select the last sketch.

        Returns:
            Tuple of (success: bool, sketch_name: str, error_msg: str)
        """
        try:
            # Step 1: Close active sketch if one is open
            try:
                active_sketch = doc.SketchManager.ActiveSketch
                if active_sketch is not None:
                    doc.SketchManager.InsertSketch(True)
                    logger.debug("Closed active sketch")
            except:
                # Try closing anyway
                try:
                    doc.InsertSketch2(True)
                except:
                    pass

            # Step 2: Clear selection
            doc.ClearSelection2(True)

            # Step 3: Find the last sketch
            sketch_name = self._find_last_sketch(doc)
            if not sketch_name:
                return False, "", "No sketch found in feature tree"

            # Step 4: Select the sketch
            empty_callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            selected = doc.Extension.SelectByID2(
                sketch_name, "SKETCH", 0, 0, 0, False, 0, empty_callout, 0
            )

            if not selected:
                # Fallback: try pythoncom.Nothing
                selected = doc.Extension.SelectByID2(
                    sketch_name, "SKETCH", 0, 0, 0, False, 0, pythoncom.Nothing, 0
                )

            if not selected:
                return False, sketch_name, f"Could not select sketch '{sketch_name}'"

            return True, sketch_name, ""

        except Exception as e:
            return False, "", f"Error in sketch selection: {e}"

    # ========================================================================
    # Extrude
    # ========================================================================

    def extrude_sketch(self, depth: float = 10, both_directions: bool = False,
                       unit: str = None) -> Dict:
        """
        Extrude the active sketch (Boss-Extrude)
        FIXED v4.0: Properly closes sketch, selects it, uses 23-param FeatureExtrusion2

        Args:
            depth: Extrusion depth
            both_directions: Extrude in both directions (mid-plane)
            unit: Unit for depth

        Returns:
            Result dictionary
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            # Convert depth to meters
            depth_m = self._units.to_meters(depth, unit)
            unit_str = unit or self._units.default_unit.value

            # Step 1: Close sketch and select it
            success, sketch_name, error_msg = self._close_and_select_sketch(doc)
            if not success:
                sketch_info = self._get_sketch_info(doc)
                return self._result(False,
                    f"Extrusion failed: {error_msg}. "
                    f"Sketches found: {sketch_info['sketch_count']} {sketch_info['sketch_names']}. "
                    f"Active sketch: {sketch_info['has_active_sketch']}. "
                    f"Try: create_sketch → draw geometry → extrude_sketch",
                    SwErrors.swFeatureError,
                    {"diagnostics": sketch_info})

            # Step 2: Determine end condition
            end_cond = 6 if both_directions else 0  # 6=MidPlane, 0=Blind

            # Step 3: Try extrusion methods
            feat = None
            method_used = ""

            # Method 1: FeatureExtrusion2 with 23 params (SW 2025 / v33)
            try:
                feat = doc.FeatureManager.FeatureExtrusion2(
                    True,           # Sd - single direction
                    False,          # Flip
                    False,          # Dir - direction
                    end_cond,       # T1 - end condition (0=Blind, 6=MidPlane)
                    0,              # T2 - end condition 2
                    depth_m,        # D1 - depth
                    depth_m,        # D2 - depth 2
                    False,          # Dchk1 - draft on/off
                    False,          # Dchk2 - draft on/off 2
                    False,          # Ddir1 - draft outward
                    False,          # Ddir2 - draft outward 2
                    0.0,            # Dang1 - draft angle (radians)
                    0.0,            # Dang2 - draft angle 2
                    False,          # OffsetReverse1
                    False,          # OffsetReverse2
                    False,          # TranslateSurface1
                    False,          # TranslateSurface2
                    True,           # Merge - merge result
                    True,           # UseFeatScope
                    True,           # UseAutoSelect
                    0,              # T0 - start condition
                    0.0,            # StartOffset
                    False           # FlipStartOffset
                )
                if feat:
                    method_used = "FeatureExtrusion2_23p"
            except Exception as e:
                logger.debug(f"FeatureExtrusion2 (23p) failed: {e}")

            # Method 2: FeatureExtrusion2 with 20 params (older SW versions)
            if feat is None:
                try:
                    feat = doc.FeatureManager.FeatureExtrusion2(
                        True, False, False, end_cond, 0,
                        depth_m, depth_m,
                        False, False, False, False,
                        0.0, 0.0,
                        False, False, False,
                        True, True, True, True
                    )
                    if feat:
                        method_used = "FeatureExtrusion2_20p"
                except Exception as e:
                    logger.debug(f"FeatureExtrusion2 (20p) failed: {e}")

            # Method 3: FeatureExtrusion3 (some SW versions)
            if feat is None:
                try:
                    feat = doc.FeatureManager.FeatureExtrusion3(
                        True, False, False, end_cond, 0,
                        depth_m, 0,
                        False, False, False, False,
                        0.0, 0.0,
                        False, False, False,
                        True, True, True,
                        0, 0.0, False
                    )
                    if feat:
                        method_used = "FeatureExtrusion3"
                except Exception as e:
                    logger.debug(f"FeatureExtrusion3 failed: {e}")

            if feat is None:
                sketch_info = self._get_sketch_info(doc)
                return self._result(False,
                    f"Extrusion failed on sketch '{sketch_name}'. "
                    f"Ensure sketch has a closed profile (circle, rectangle, etc). "
                    f"Sketches in model: {sketch_info['sketch_names']}",
                    SwErrors.swFeatureError,
                    {"sketch_name": sketch_name, "diagnostics": sketch_info})

            direction = "both directions (mid-plane)" if both_directions else "one direction"

            return self._result(True,
                f"Extruded {depth}{unit_str} ({direction}) [{method_used}]",
                SwErrors.swSuccess,
                {"depth": depth, "unit": unit_str,
                 "both_directions": both_directions,
                 "sketch_name": sketch_name,
                 "api_method": method_used})

        except Exception as e:
            logger.error(f"Extrude error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Cut Extrude
    # ========================================================================

    def cut_extrude(self, depth: float = 10, through_all: bool = False,
                    both_directions: bool = False, unit: str = None) -> Dict:
        """
        Cut extrude (remove material)
        FIXED v4.0: Proper sketch handling and parameter counts

        Args:
            depth: Cut depth (ignored if through_all=True)
            through_all: Cut through entire model
            both_directions: Cut in both directions
            unit: Unit for depth

        Returns:
            Result dictionary
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            # Convert depth to meters
            depth_m = self._units.to_meters(depth, unit)
            unit_str = unit or self._units.default_unit.value

            # Step 1: Close sketch and select it
            success, sketch_name, error_msg = self._close_and_select_sketch(doc)
            if not success:
                sketch_info = self._get_sketch_info(doc)
                return self._result(False,
                    f"Cut failed: {error_msg}. "
                    f"Sketches: {sketch_info['sketch_names']}. "
                    f"Ensure sketch is on an existing face.",
                    SwErrors.swFeatureError,
                    {"diagnostics": sketch_info})

            # Determine end condition
            if through_all:
                if both_directions:
                    end_cond = 2  # swEndCondThroughAllBoth
                else:
                    end_cond = 1  # swEndCondThroughAll
                cut_depth = 0
            else:
                if both_directions:
                    end_cond = 6  # swEndCondMidPlane
                else:
                    end_cond = 0  # swEndCondBlind
                cut_depth = depth_m

            feat = None
            method_used = ""

            # Method 1: FeatureCut3 (26 params - most reliable for SW 2025)
            # Verified working parameter signature from SolidWorks API testing
            try:
                feat = doc.FeatureManager.FeatureCut3(
                    True,           # Sd - single direction
                    False,          # Flip
                    False,          # Dir
                    end_cond,       # T1 - end condition
                    0,              # T2 - end condition 2
                    cut_depth,      # D1 - depth
                    0,              # D2 - depth 2
                    False, False,   # Dchk1, Dchk2 - draft on/off
                    False, False,   # Ddir1, Ddir2 - draft direction
                    0.0, 0.0,       # Dang1, Dang2 - draft angle
                    False, False,   # OffsetReverse1, OffsetReverse2
                    False, False,   # TranslateSurface1, TranslateSurface2
                    False, False, False,  # NormalCut, UseFeatScope, UseAutoSelect
                    False, False, False,  # AssemblyFeatureScope, AutoSelectComponents, PropagateFeatureToParts
                    0,              # T0 - start condition
                    0.0,            # StartOffset
                    False           # FlipStartOffset
                )
                if feat:
                    method_used = "FeatureCut3"
            except Exception as e:
                logger.debug(f"FeatureCut3 (26p) failed: {e}")

            # Method 2: FeatureCut4 (SW 2014+ - may need different param count)
            if feat is None:
                try:
                    feat = doc.FeatureManager.FeatureCut4(
                        True,           # Sd
                        False,          # Flip
                        False,          # Dir
                        end_cond,       # T1
                        0,              # T2
                        cut_depth,      # D1
                        0,              # D2
                        False, False,   # Dchk1, Dchk2
                        False, False,   # Ddir1, Ddir2
                        0.0, 0.0,       # Dang1, Dang2
                        False, False,   # OffsetReverse1, OffsetReverse2
                        False, False,   # TranslateSurface1, TranslateSurface2
                        False, False, False,  # NormalCut, UseFeatScope, UseAutoSelect
                        False, False, False,  # AssemblyFeatureScope, AutoSelectComponents, PropagateFeatureToParts
                        0,              # T0
                        0.0,            # StartOffset
                        False,          # FlipStartOffset
                        False           # OptimizeGeometry (extra param in Cut4)
                    )
                    if feat:
                        method_used = "FeatureCut4"
                except Exception as e:
                    logger.debug(f"FeatureCut4 failed: {e}")

            if feat is None:
                return self._result(False,
                    f"Cut failed on sketch '{sketch_name}'. "
                    f"Ensure sketch is drawn on an existing face with a closed profile.",
                    SwErrors.swFeatureError,
                    {"sketch_name": sketch_name})

            cut_type = "through all" if through_all else f"{depth}{unit_str}"

            return self._result(True, f"Cut extrude: {cut_type} [{method_used}]",
                              SwErrors.swSuccess,
                              {"depth": depth, "through_all": through_all,
                               "sketch_name": sketch_name,
                               "api_method": method_used})

        except Exception as e:
            logger.error(f"Cut extrude error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Fillet
    # ========================================================================

    def fillet_edges(self, radius: float = 2, unit: str = None) -> Dict:
        """
        Add fillet to selected edges

        Args:
            radius: Fillet radius
            unit: Unit for radius

        Returns:
            Result dictionary

        Note: Select edges first using execute_python or manual selection
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            radius_m = self._units.to_meters(radius, unit)

            feat = None
            method_used = ""

            # Method 1: FeatureFillet3
            try:
                feat = doc.FeatureManager.FeatureFillet3(
                    195, radius_m, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
                )
                if feat:
                    method_used = "FeatureFillet3"
            except Exception as e:
                logger.debug(f"FeatureFillet3 failed: {e}")

            # Method 2: SimpleFillet
            if feat is None:
                try:
                    feat = doc.FeatureManager.SimpleFillet(radius_m, True, True, True)
                    if feat:
                        method_used = "SimpleFillet"
                except Exception as e:
                    logger.debug(f"SimpleFillet failed: {e}")

            if feat is None:
                return self._result(False,
                    "Fillet failed - select edges first (use execute_python to select edges programmatically)",
                    SwErrors.swFeatureError)

            unit_str = unit or self._units.default_unit.value

            return self._result(True, f"Fillet: r={radius}{unit_str} [{method_used}]",
                              SwErrors.swSuccess,
                              {"radius": radius, "unit": unit_str})

        except Exception as e:
            logger.error(f"Fillet error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # Chamfer
    # ========================================================================

    def chamfer_edges(self, distance: float = 2, angle: float = 45,
                      unit: str = None) -> Dict:
        """
        Add chamfer to selected edges
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            import math
            dist_m = self._units.to_meters(distance, unit)
            angle_rad = math.radians(angle)

            feat = None

            try:
                feat = doc.FeatureManager.InsertFeatureChamfer(
                    1, dist_m, angle_rad, dist_m, 0, False, False
                )
            except Exception as e:
                logger.debug(f"InsertFeatureChamfer failed: {e}")

            if feat is None:
                return self._result(False,
                    "Chamfer failed - select edges first",
                    SwErrors.swFeatureError)

            unit_str = unit or self._units.default_unit.value

            return self._result(True, f"Chamfer: {distance}{unit_str} x {angle}\u00b0",
                              SwErrors.swSuccess,
                              {"distance": distance, "angle": angle, "unit": unit_str})

        except Exception as e:
            logger.error(f"Chamfer error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swFeatureError)

    # ========================================================================
    # List Features (FIXED: properties not methods)
    # ========================================================================

    def list_features(self) -> Dict:
        """
        List all features in the active document
        FIXED v4.0: Uses properties (FirstFeature, GetNextFeature, GetTypeName2)
        instead of method calls for SW 2025 compatibility.
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            features = []

            # com_get: works whether members are dynamic properties or
            # typed makepy methods
            feat = com_get(doc, "FirstFeature", default=None)

            while feat is not None:
                try:
                    name = com_get(feat, "Name", default="<unknown>")
                    feat_type = com_get(feat, "GetTypeName2", default="<unknown>")
                    features.append({
                        "name": name,
                        "type": feat_type,
                    })
                except:
                    pass

                feat = com_get(feat, "GetNextFeature", default=None)

            return self._result(True, f"{len(features)} features found",
                              SwErrors.swSuccess,
                              {"features": features, "count": len(features)})

        except Exception as e:
            logger.error(f"List features error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    # ========================================================================
    # Edge Selection Helper
    # ========================================================================

    def select_edge(self, edge_index: int = 1) -> Dict:
        """
        Select an edge by index
        Note: Use execute_python for precise edge selection
        """
        try:
            doc, err = self.get_active_doc()
            if err:
                return err

            doc.ClearSelection2(True)

            return self._result(True, "Use execute_python for edge selection",
                              SwErrors.swSuccess)

        except Exception as e:
            return self._result(False, f"Error: {e}", SwErrors.swSelectionError)
