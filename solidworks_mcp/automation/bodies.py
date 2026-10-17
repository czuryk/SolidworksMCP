"""
SolidWorks Body Operations
--------------------------
List bodies, control visibility/transparency/colour, rename bodies,
mass properties, clearance/interference checks.
body.HideBody(bool) and body.Name = ... are verified working on SW2026
and do not pollute the feature tree.

CRITICAL SW2026 fact: writing Body2.MaterialPropertyValues2 with a raw
Python tuple/list through dynamic dispatch marshals the array CORRUPTED -
the call "succeeds" but RGB reads back as zeros (black body) or garbage
(1.95e-310). Every write must go through VARIANT(VT_ARRAY | VT_R8).
"""

import math
import logging
import traceback
from typing import Dict, List, Optional

import pythoncom
from win32com.client import VARIANT

from ..constants import (SwErrors, SwBodyType, SwDocumentTypes,
                         SwBodyOperationType)
from .com_utils import com_get, null_dispatch, typed, create_select_data

logger = logging.getLogger(__name__)

# Default SW material property array (neutral grey):
# [R, G, B, ambient, diffuse, specular, shininess, transparency, emission]
DEFAULT_MATERIAL_PROPS = [0.79, 0.79, 0.79, 1.0, 1.0, 0.5, 0.4, 0.0, 0.0]


class BodyOperations:
    """
    Mixin class for solid body operations.

    Requires parent class to have:
    - get_active_doc(): Document access method
    - _result(): Result factory method
    - _units: UnitConverter instance
    """

    # ========================================================================
    # Helpers
    # ========================================================================

    def _get_solid_bodies(self, doc, include_hidden: bool = True):
        """Get solid bodies of a part document (list, possibly empty)."""
        try:
            bodies = doc.GetBodies2(int(SwBodyType.swSolidBody),
                                    not include_hidden)
            return list(bodies) if bodies else []
        except Exception as e:
            logger.debug(f"GetBodies2 failed: {e}")
            return []

    def _find_body(self, doc, name: str):
        """Find a solid body by exact name (hidden bodies included)."""
        for body in self._get_solid_bodies(doc, include_hidden=True):
            if com_get(body, "Name", default="") == name:
                return body
        return None

    def _require_part(self):
        """Get active doc and ensure it is a part document."""
        doc, err = self.get_active_doc()
        if err:
            return None, err
        doc_type = com_get(doc, "GetType", default=-1)
        if doc_type != int(SwDocumentTypes.swDocPART):
            return None, self._result(False,
                "Body operations require a Part document",
                SwErrors.swInvalidInput)
        return doc, None

    def _body_info(self, body, unit: Optional[str] = None) -> Dict:
        """Collect body info: name, visibility, bbox (display units), faces."""
        info = {
            "name": com_get(body, "Name", default="<unknown>"),
            "visible": bool(com_get(body, "Visible", default=True)),
            "face_count": None,
            "bbox": None,
        }
        try:
            info["face_count"] = com_get(body, "GetFaceCount", default=None)
        except Exception:
            pass
        try:
            box = com_get(body, "GetBodyBox", default=None)
            if box and len(box) >= 6:
                conv = self._units.from_meters
                info["bbox"] = {
                    "min": [round(conv(box[0], unit), 4),
                            round(conv(box[1], unit), 4),
                            round(conv(box[2], unit), 4)],
                    "max": [round(conv(box[3], unit), 4),
                            round(conv(box[4], unit), 4),
                            round(conv(box[5], unit), 4)],
                }
        except Exception:
            pass
        return info

    # ========================================================================
    # Tools
    # ========================================================================

    def list_bodies(self, include_hidden: bool = True,
                    unit: str = None) -> Dict:
        """
        List solid bodies with names, visibility, bbox and face count.

        Args:
            include_hidden: Include hidden bodies
            unit: Unit for bbox coordinates (default unit if None)
        """
        try:
            doc, err = self._require_part()
            if err:
                return err

            bodies = self._get_solid_bodies(doc, include_hidden)
            items = [self._body_info(b, unit) for b in bodies]
            unit_str = unit or self._units.default_unit.value

            return self._result(True, f"{len(items)} solid body(ies)",
                              SwErrors.swSuccess,
                              {"bodies": items, "count": len(items),
                               "bbox_unit": unit_str})
        except Exception as e:
            logger.error(f"List bodies error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def set_body_visibility(self, name: str, visible: bool) -> Dict:
        """
        Show or hide a body by name (body.HideBody).

        Args:
            name: Body name (see list_bodies)
            visible: True to show, False to hide
        """
        try:
            doc, err = self._require_part()
            if err:
                return err

            body = self._find_body(doc, name)
            if body is None:
                names = [com_get(b, "Name", default="?")
                         for b in self._get_solid_bodies(doc)]
                return self._result(False,
                    f"Body '{name}' not found. Existing: {names}",
                    SwErrors.swSelectionError, {"existing_bodies": names})

            # HideBody(True) hides the body, HideBody(False) shows it
            body.HideBody(not visible)
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass

            state = "shown" if visible else "hidden"
            return self._result(True, f"Body '{name}' {state}",
                              SwErrors.swSuccess,
                              {"body": name, "visible": visible})
        except Exception as e:
            logger.error(f"Body visibility error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def rename_body(self, old_name: str, new_name: str) -> Dict:
        """Rename a solid body (body.Name = ...)."""
        try:
            doc, err = self._require_part()
            if err:
                return err

            body = self._find_body(doc, old_name)
            if body is None:
                names = [com_get(b, "Name", default="?")
                         for b in self._get_solid_bodies(doc)]
                return self._result(False,
                    f"Body '{old_name}' not found. Existing: {names}",
                    SwErrors.swSelectionError, {"existing_bodies": names})

            body.Name = new_name

            # Verify
            renamed = self._find_body(doc, new_name)
            if renamed is None:
                return self._result(False,
                    f"Rename did not stick ('{old_name}' -> '{new_name}')",
                    SwErrors.swUnknownError)

            return self._result(True,
                f"Body renamed: '{old_name}' -> '{new_name}'",
                SwErrors.swSuccess,
                {"old_name": old_name, "new_name": new_name})
        except Exception as e:
            logger.error(f"Rename body error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    # ------------------------------------------------------------------
    # Material properties (colour / transparency)
    # ------------------------------------------------------------------

    def _read_material_props(self, body) -> Optional[List[float]]:
        """Read MaterialPropertyValues2 as a 9-float list, or None."""
        props = com_get(body, "MaterialPropertyValues2", default=None)
        if not props:
            return None
        try:
            lst = [float(v) for v in props]
        except Exception:
            return None
        if len(lst) < 9:
            lst += [0.0] * (9 - len(lst))
        return lst[:9]

    def _write_material_props(self, doc, body, props: List[float]) -> Dict:
        """
        Write MaterialPropertyValues2 through VARIANT(VT_ARRAY | VT_R8) and
        verify by reading the array back. A raw tuple/list write marshals
        corrupted on SW2026 (RGB zeroed or garbage) while reporting success.
        Returns {"verified": bool, "written": [...], "read_back": [...]}.
        """
        vals = [float(v) for v in props][:9]
        arr = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, vals)
        body.MaterialPropertyValues2 = arr
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass

        read_back = self._read_material_props(body)
        # SW quantizes colour channels to 8 bits (0.79 reads back as
        # 201/255 = 0.788235), so the tolerance must exceed the 1/255
        # quantization step (0.0039) - while still catching the real
        # corruption bug (RGB zeroed, delta ~0.79).
        verified = (read_back is not None and
                    all(abs(read_back[i] - vals[i]) < 0.006 for i in range(9)))
        return {"verified": verified, "written": vals, "read_back": read_back}

    def set_body_transparency(self, name: str, transparency: float) -> Dict:
        """
        Set ONLY the transparency of a body (index [7] of
        MaterialPropertyValues2), preserving its colour and other material
        properties. 0.0 = opaque, 1.0 = invisible.

        The write goes through VARIANT(VT_ARRAY|VT_R8) and is verified by
        reading the array back - a raw-list write corrupts the appearance
        on SW2026 (body turns black) while reporting success.
        """
        try:
            doc, err = self._require_part()
            if err:
                return err

            transparency = max(0.0, min(1.0, float(transparency)))

            body = self._find_body(doc, name)
            if body is None:
                names = [com_get(b, "Name", default="?")
                         for b in self._get_solid_bodies(doc)]
                return self._result(False,
                    f"Body '{name}' not found. Existing: {names}",
                    SwErrors.swSelectionError, {"existing_bodies": names})

            # Change ONLY index [7]; keep the current appearance. If the
            # body has no material array yet, start from neutral grey -
            # NOT from zeros (zeros = black body).
            props = self._read_material_props(body)
            if props is None:
                props = list(DEFAULT_MATERIAL_PROPS)
            props[7] = transparency

            write_info = self._write_material_props(doc, body, props)
            if not write_info["verified"]:
                return self._result(False,
                    f"Transparency write did NOT verify on body '{name}': "
                    f"read-back mismatch (COM marshalling issue?)",
                    SwErrors.swUnknownError,
                    {"body": name, **write_info})

            return self._result(True,
                f"Body '{name}' transparency set to {transparency} "
                f"(colour preserved, write verified)",
                SwErrors.swSuccess,
                {"body": name, "transparency": transparency, **write_info})
        except Exception as e:
            logger.error(f"Transparency error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    def set_body_color(self, name: str, rgb: List[float],
                       transparency: float = None) -> Dict:
        """
        Set body colour (and optionally transparency) via
        MaterialPropertyValues2, preserving the remaining properties.

        Args:
            name: Body name
            rgb: [r, g, b] - values 0..255 (or 0..1 if all components <= 1)
            transparency: Optional 0.0-1.0 (unchanged if None)
        """
        try:
            doc, err = self._require_part()
            if err:
                return err

            if not rgb or len(rgb) < 3:
                return self._result(False, "rgb must be [r, g, b]",
                                  SwErrors.swInvalidInput)
            r, g, b = (float(c) for c in rgb[:3])
            if max(r, g, b) > 1.0:
                r, g, b = r / 255.0, g / 255.0, b / 255.0
            r = max(0.0, min(1.0, r))
            g = max(0.0, min(1.0, g))
            b = max(0.0, min(1.0, b))

            body = self._find_body(doc, name)
            if body is None:
                names = [com_get(bd, "Name", default="?")
                         for bd in self._get_solid_bodies(doc)]
                return self._result(False,
                    f"Body '{name}' not found. Existing: {names}",
                    SwErrors.swSelectionError, {"existing_bodies": names})

            props = self._read_material_props(body)
            if props is None:
                props = list(DEFAULT_MATERIAL_PROPS)
            props[0], props[1], props[2] = r, g, b
            if transparency is not None:
                props[7] = max(0.0, min(1.0, float(transparency)))

            write_info = self._write_material_props(doc, body, props)
            if not write_info["verified"]:
                return self._result(False,
                    f"Colour write did NOT verify on body '{name}': "
                    f"read-back mismatch",
                    SwErrors.swUnknownError,
                    {"body": name, **write_info})

            return self._result(True,
                f"Body '{name}' colour set to RGB({round(r,3)}, {round(g,3)}, "
                f"{round(b,3)})"
                + (f", transparency {props[7]}" if transparency is not None
                   else "") + " (write verified)",
                SwErrors.swSuccess,
                {"body": name, "rgb": [round(r, 4), round(g, 4), round(b, 4)],
                 "transparency": props[7], **write_info})
        except Exception as e:
            logger.error(f"Body color error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    # ------------------------------------------------------------------
    # Mass properties / volume
    # ------------------------------------------------------------------

    def _body_mass_props(self, body, unit: Optional[str]) -> Optional[Dict]:
        """
        IBody2::GetMassProperties(density) -> [CoMx, CoMy, CoMz, volume,
        area, mass, ...]. Returns volume/area/CoM in user units, or None.
        """
        mp = None
        try:
            mp = com_get(body, "GetMassProperties", 1000.0, default=None)
        except Exception:
            mp = None
        if mp is None:
            b_t = typed(body, "IBody2")
            if b_t is not None:
                try:
                    mp = b_t.GetMassProperties(1000.0)
                except Exception:
                    mp = None
        if not mp or len(mp) < 5:
            return None
        k = self._units.from_meters(1.0, unit)  # linear meters -> user units
        volume_exact = float(mp[3]) * k ** 3
        return {
            "volume": round(volume_exact, 4),
            "volume_exact": volume_exact,
            "surface_area": round(float(mp[4]) * k ** 2, 4),
            "center_of_mass": [round(float(mp[0]) * k, 4),
                               round(float(mp[1]) * k, 4),
                               round(float(mp[2]) * k, 4)],
        }

    def body_volume(self, name: str = None, unit: str = None) -> Dict:
        """
        Volume / surface area / centre of mass of one body (or all bodies
        if name is omitted) via IBody2::GetMassProperties. Instant sanity
        check that a part is not a sliver and that a feature did not
        unexpectedly change a body.
        """
        try:
            doc, err = self._require_part()
            if err:
                return err

            if name:
                body = self._find_body(doc, name)
                if body is None:
                    names = [com_get(b, "Name", default="?")
                             for b in self._get_solid_bodies(doc)]
                    return self._result(False,
                        f"Body '{name}' not found. Existing: {names}",
                        SwErrors.swSelectionError, {"existing_bodies": names})
                bodies = [body]
            else:
                bodies = self._get_solid_bodies(doc)

            unit_str = unit or self._units.default_unit.value
            items = []
            for b in bodies:
                nm = com_get(b, "Name", default="?")
                props = self._body_mass_props(b, unit)
                if props is None:
                    items.append({"name": nm, "error":
                                  "GetMassProperties unavailable"})
                else:
                    items.append({"name": nm, **props})

            vols = [i["volume"] for i in items if "volume" in i]
            return self._result(True,
                f"{len(items)} body(ies), volumes: {vols} {unit_str}^3",
                SwErrors.swSuccess,
                {"bodies": items, "unit": unit_str,
                 "volume_unit": f"{unit_str}^3"})
        except Exception as e:
            logger.error(f"Body volume error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Error: {e}", SwErrors.swUnknownError)

    # ------------------------------------------------------------------
    # Clearance / interference
    # ------------------------------------------------------------------

    def check_clearance(self, body_a: str, body_b: str,
                        min_clearance: float = None, unit: str = None) -> Dict:
        """Measure native boundary distance and overlap without changing selection."""
        from .body_relations import measure_pair, validate_pairs
        from .com_utils import command_scope
        from .shape_observation import geometry_state, ensure_unchanged
        try:
            doc, err = self._require_part()
            if err:
                return err
            if body_a == body_b:
                return self._error('BODY_PAIR_SCOPE', 'Choose two distinct bodies')
            ba, bb = self._find_body(doc, body_a), self._find_body(doc, body_b)
            if ba is None or bb is None:
                return self._error('BODY_NOT_FOUND', 'Both named bodies must exist')
            k = self._units.from_meters(1.0, unit)
            query = {'id': 'legacy', 'a': {'body': body_a}, 'b': {'body': body_b}}
            if min_clearance is not None:
                query['min_clearance_mm'] = float(min_clearance)*1000/k
            validate_pairs([query])
            state = geometry_state(doc)
            dirty = bool(com_get(doc, 'GetSaveFlag'))
            with command_scope(self.app):
                evidence = measure_pair(doc, ba, bb, query)
            ensure_unchanged(self, doc, state)
            if bool(com_get(doc, 'GetSaveFlag')) != dirty:
                return self._error('INSPECTION_STATE_CHANGED', 'Measurement changed dirty state')
            factor = k/1000
            distance = evidence['clearance_mm']
            volume = evidence['intersection_volume_mm3']
            data = {'body_a': body_a, 'body_b': body_b,
                    'unit': unit or self._units.default_unit.value,
                    'min_distance': None if distance is None else distance*factor,
                    'interference': evidence['interference'],
                    'intersection_volume': None if volume is None else volume*factor**3,
                    'intersection_bbox': None,
                    'measure_ok': evidence['boundary_distance_mm'] is not None,
                    'intersect_check_ok': volume is not None,
                    'evidence': evidence}
            passed = evidence['relation'] != 'UNKNOWN'
            if min_clearance is not None:
                data.update(required_clearance=float(min_clearance),
                            clearance_ok=evidence['clearance_check']['passed'])
                passed = data['clearance_ok'] is True
            return self._result(passed, 'Native body relation: '+evidence['relation'],
                                SwErrors.swSuccess if passed else SwErrors.swFeatureError, data)
        except Exception as exc:
            return self._error(getattr(exc, 'code', 'CLEARANCE_UNKNOWN'), str(exc))

    def _copy_body(self, body):
        """Return a temporary native copy without touching document geometry."""
        return typed(body, 'IBody2').Copy2(False)

    def _intersect_bodies(self, body_a, body_b, k: float) -> Optional[Dict]:
        """Compatibility wrapper; failed native checks return unknown, never zero."""
        from .body_relations import native_intersection
        try:
            result = native_intersection(body_a, body_b)
            return {'volume': result['volume_mm3']*(k/1000)**3, 'bbox': None}
        except Exception as exc:
            logger.debug('Native intersection unavailable: %s', exc)
            return None
