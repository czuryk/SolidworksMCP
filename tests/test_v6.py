import asyncio
import json
import math
import os
import struct
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.com_utils import (
    ComAdapter, _SW_MAIN_WINDOW_CACHE, _cached_sldworks_main,
    _classify_dialog, com_get, detect_modal_dialog, resolve_known_dialog,
    resolve_solidworks_constant)
from solidworks_mcp.automation.jobs import (
    JobManager, capture_ui_problem_screenshot)
from solidworks_mcp.automation import ui_watchdog_worker
from solidworks_mcp.automation.runtime import (
    RuntimeState, enrich_legacy_error, structured_error)
from solidworks_mcp.server import (
    _dispatch_offline_worker, _dispatch_two_phase_body_comparison,
    _dispatch_two_phase_sketches_comparison,
    _dispatch_two_phase_vector_commit,
    _dispatch_two_phase_sketch_comparison, _get_job_result,
    _is_effectively_mutating, list_tools)


class PropertyObject:
    Value = 42


class MethodObject:
    def Value(self, increment=0):
        return 42 + increment


class FakePoint:
    def __init__(self, x, y, z=0.0):
        self.X, self.Y, self.Z = x, y, z

    def Select4(self, append, data):
        return True


class FakeSegment:
    def __init__(self, start, end, kind=0):
        self.start = FakePoint(*start)
        self.end = FakePoint(*end)
        self.kind = kind
        self.ConstructionGeometry = False

    def GetStartPoint2(self):
        return self.start

    def GetEndPoint2(self):
        return self.end

    def GetType(self):
        return self.kind

    def GetConstrainedStatus(self):
        return 3

    def Select4(self, append, data):
        return True


class FakeArcSegment(FakeSegment):
    def __init__(self):
        super().__init__((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), kind=1)
        self.center = FakePoint(0.0, 0.0, 0.0)

    def GetCenterPoint2(self):
        return self.center

    def GetRadius(self):
        return 1.0

    def GetRotationDir(self):
        return -1

    def IsClockwise(self):
        return False


class FakeTessCurve:
    def __init__(self):
        self.calls = []

    def GetEndParams(self, *args):
        return True, 0.0, 1.0, False, False

    def Evaluate2(self, parameter, derivatives):
        self.calls.append((parameter, derivatives))
        return [0.01 * float(parameter), 0.0, 0.0, 1.0]

    def GetTessPts(self, *args):
        raise AssertionError("Monolithic tessellation must not run")

    def GetBCurveParams5(self, *args):
        raise AssertionError("NURBS conversion must not run in tessellation mode")


class FakeSplineSegment(FakeSegment):
    def __init__(self, curve):
        super().__init__((0.0, 0.0, 0.0), (0.01, 0.0, 0.0), kind=3)
        self.curve = curve

    def GetCurve(self):
        return self.curve


class FakeEquationSplineSegment:
    def __init__(self, start, end):
        self.points = [FakePoint(*start), FakePoint(*end)]
        self.ConstructionGeometry = False

    def GetPoints2(self):
        return self.points

    def GetType(self):
        return 3

    def GetConstrainedStatus(self):
        return 2


class FakeParabolaCurve(FakeTessCurve):
    def Evaluate2(self, parameter, derivatives):
        parameter = float(parameter)
        self.calls.append((parameter, derivatives))
        return [0.01 * parameter,
                0.002 * 4.0 * parameter * (1.0 - parameter),
                0.0, 1.0]


class FakeRelationManager:
    def __init__(self, relations):
        self.relations = relations

    def GetRelations(self, relation_filter):
        return self.relations


class FakeContour:
    IsClosed = True


class FakeFeature:
    def __init__(self, name="Sketch1"):
        self.Name = name
        self.sketch = None

    def GetSpecificFeature2(self):
        return self.sketch

    def Select2(self, append, mark):
        return True


class FakeSketch:
    def __init__(self, feature):
        self.feature = feature
        self.segments = []
        self.relations = []

    def GetFeature(self):
        return self.feature

    def GetConstrainedStatus(self):
        return 3

    def GetSketchContours(self):
        return [FakeContour()]

    def GetSketchSegments(self):
        return self.segments

    def GetRelations(self):
        return self.relations


class FakeSketchManager:
    def __init__(self, sketch):
        self.ActiveSketch = sketch
        self.AddToDB = False

    def CreateLine(self, x1, y1, z1, x2, y2, z2):
        segment = FakeSegment((x1, y1, z1), (x2, y2, z2))
        self.ActiveSketch.segments.append(segment)
        return segment

    CreateCenterLine = CreateLine

    def AddRelation(self, code):
        relation = object()
        self.ActiveSketch.relations.append(relation)
        return relation

    def InsertSketch(self, update):
        self.ActiveSketch = None
        return True


class DelayedSketchManager:
    def __init__(self, sketch, delayed_reads=2):
        self.sketch = sketch
        self.delayed_reads = delayed_reads
        self.reads = 0

    @property
    def ActiveSketch(self):
        self.reads += 1
        return None if self.reads <= self.delayed_reads else self.sketch


class FakeExtension:
    def __init__(self):
        self.deleted = 0

    def GetPersistReference3(self, obj):
        return bytes(str(id(obj)), "ascii")

    def SelectByID2(self, *args):
        return True


class FakeSelectionManager:
    def CreateSelectData(self):
        return type("SelectionData", (), {"Mark": 0})()

    def DeleteSelection2(self, options):
        self.deleted += 1
        return True


class FakeDimension:
    def __init__(self, name):
        self.Name = name
        self.FullName = name
        self.SystemValue = 0.0
        self.DrivenState = 2


class FakeDisplayDimension:
    def __init__(self, dimension):
        self.dimension = dimension

    def GetDimension2(self, configuration):
        return self.dimension

    def Select4(self, append, data):
        return True


class FakeDoc:
    def __init__(self):
        self.feature = FakeFeature()
        self.sketch = FakeSketch(self.feature)
        self.feature.sketch = self.sketch
        self.SketchManager = FakeSketchManager(self.sketch)
        self.Extension = FakeExtension()
        self.SelectionManager = FakeSelectionManager()
        self.rebuilds = 0
        self.dimension_count = 0

    def EditRebuild3(self):
        self.rebuilds += 1
        return True

    def ClearSelection2(self, all_items):
        return True

    def GraphicsRedraw2(self):
        return True

    def SketchAddConstraints(self, code):
        self.sketch.relations.append({"code": code})
        return True

    def AddDimension2(self, x, y, z):
        self.dimension_count += 1
        return FakeDisplayDimension(FakeDimension(f"D{self.dimension_count}"))

    AddHorizontalDimension2 = AddDimension2
    AddVerticalDimension2 = AddDimension2
    AddRadialDimension2 = AddDimension2
    AddDiameterDimension2 = AddDimension2


class FakeSW:
    def __init__(self):
        self.value = True

    def GetUserPreferenceToggle(self, enum_value):
        return self.value

    def SetUserPreferenceToggle(self, enum_value, value):
        self.value = bool(value)
        return True


class FakeAutomation(SolidWorksAutomation):
    def __init__(self):
        super().__init__()
        self.doc = FakeDoc()
        self._sw_app = FakeSW()
        self._connected = True

    def get_active_doc(self):
        return self.doc, None

    def create_sketch(self, plane="Front"):
        self.doc.SketchManager.ActiveSketch = self.doc.sketch
        return self._result(True, "created", data={
            "orientation": {"verified": True,
                            "fit_to_screen": {"verified": True}}})

    def _auto_normal_to(self, doc, zoom_to_fit=True):
        return self._result(True, "verified", data={
            "verified": True,
            "normal_to_verified": True,
            "fit_to_screen": {"verified": True,
                              "verification_applicable": True},
        })

    def _rename_feature_safe(self, doc, feature, requested):
        feature.Name = requested
        return requested, None

    def _find_sketch_feature(self, doc, name):
        return doc.feature if doc.feature.Name == name else None

    def delete_feature(self, name, delete_absorbed=False):
        return self._result(True, "deleted")

    def _document_key(self, doc):
        return "fake"


class V6Tests(unittest.TestCase):
    def test_property_method_adapter_contract(self):
        self.assertEqual(com_get(PropertyObject(), "Value"), 42)
        self.assertEqual(com_get(MethodObject(), "Value", 3), 45)
        self.assertIsInstance(ComAdapter(), ComAdapter)
        self.assertIsInstance(
            resolve_solidworks_constant("swInputDimValOnCreate"), int)

    def test_new_document_template_uses_solidworks_user_preference(self):
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            template = os.path.join(directory, "Part.prtdot")
            Path(template).write_bytes(b"template")

            class PreferenceApp:
                def GetUserPreferenceStringValue(self, preference):
                    return template

            automation._sw_app = PreferenceApp()
            automation._config.part_template = "auto"
            with patch(
                    "solidworks_mcp.automation.documents.find_template",
                    return_value=None), patch(
                    "solidworks_mcp.automation.documents."
                    "resolve_solidworks_constant", return_value=17):
                resolved, source = automation._resolve_document_template("part")
            self.assertEqual(resolved, os.path.abspath(template))
            self.assertEqual(source, "solidworks_user_preference")

    def test_structured_error_and_legacy_enrichment(self):
        error = structured_error("FEATURE_DEAD", "dead")
        self.assertEqual(error["code"], "FEATURE_DEAD")
        enriched = enrich_legacy_error({
            "success": False, "message": "created a DEAD feature (0 faces)",
            "error_code": 103, "error_name": "swFeatureError"})
        self.assertEqual(enriched["data"]["error"]["code"], "FEATURE_DEAD")

    def test_dimension_guard_restores_user_setting(self):
        automation = FakeAutomation()
        with automation.dimension_input_guard("operation_scoped") as guard:
            self.assertFalse(automation._sw_app.value)
            self.assertTrue(guard.disabled_verified)
        self.assertTrue(automation._sw_app.value)
        self.assertTrue(guard.restored)

    def test_active_sketch_publication_is_retried(self):
        automation = FakeAutomation()
        manager = DelayedSketchManager(automation.doc.sketch, delayed_reads=2)
        automation.doc.SketchManager = manager
        doc, sketch = automation._wait_for_active_sketch(
            automation.doc, timeout_sec=0.5)
        self.assertIs(doc, automation.doc)
        self.assertIs(sketch, automation.doc.sketch)
        self.assertGreaterEqual(manager.reads, 3)

    def test_created_sketch_rollback_uses_actual_feature_name(self):
        automation = FakeAutomation()
        deleted = []

        def delete_feature(name, delete_absorbed=False):
            deleted.append((name, delete_absorbed))
            automation.doc.feature.Name = "__deleted__"
            return automation._result(True, "deleted")

        automation.delete_feature = delete_feature
        automation.doc.feature.Name = "Sketch17"
        self.assertTrue(automation._rollback_created_sketch(
            automation.doc.feature, "RequestedName"))
        self.assertEqual(deleted, [("Sketch17", True)])
        self.assertEqual(automation._runtime.metrics["rollbacks"], 1)

    def test_created_sketch_rollback_rejects_optimistic_delete_result(self):
        automation = FakeAutomation()
        automation.doc.feature.Name = "Sketch18"
        automation.delete_feature = lambda *args, **kwargs: (
            automation._result(True, "claimed deletion"))
        self.assertFalse(automation._rollback_created_sketch(
            automation.doc.feature, "Sketch18"))

    def test_atomic_sketch_one_rebuild_and_ids(self):
        automation = FakeAutomation()
        result = automation.create_parametric_sketch(
            name="Rectangle", plane="Front", unit="mm",
            entities=[
                {"id": "a", "type": "line", "start": [0, 0], "end": [10, 0]},
                {"id": "b", "type": "line", "start": [10, 0], "end": [10, 5]},
                {"id": "c", "type": "line", "start": [10, 5], "end": [0, 5]},
                {"id": "d", "type": "line", "start": [0, 5], "end": [0, 0]},
            ], constraints=[{"type": "horizontal", "entities": ["a"]}],
            dimensions=[], equations=[],
            solve={"target": "fully_defined"},
            validation={"require_closed": True, "closed_contours": 1},
            transaction={"rollback_on_failure": True})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["rebuild_count"], 1)
        self.assertEqual(automation.doc.rebuilds, 1)
        self.assertEqual(automation.doc.sketch.relations[-1]["code"],
                         "sgHORIZONTAL2D")
        self.assertEqual(set(result["data"]["entity_ids"]), {"a", "b", "c", "d"})
        self.assertIn("geometry_creation",
                      result["data"]["phase_timings_sec"])
        self.assertEqual(len(result["data"]["slowest_entities"]), 4)

    def test_atomic_sketch_accepts_dynamic_rebuild_property(self):
        automation = FakeAutomation()
        automation.doc.EditRebuild3 = True
        result = automation.create_parametric_sketch(
            name="DynamicRebuild", plane="Front", unit="mm",
            entities=[{
                "id": "line", "type": "line",
                "start": [0, 0], "end": [10, 0],
            }], constraints=[], dimensions=[], equations=[],
            solve={}, validation={},
            transaction={"rollback_on_failure": True})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["rebuild_count"], 1)

    def test_construction_reference_skips_inapplicable_solver_and_contours(self):
        automation = FakeAutomation()

        def reject_constraint_status():
            raise AssertionError(
                "Construction-only reference must not invoke the sketch solver")

        def reject_contour_enumeration():
            raise AssertionError(
                "Construction-only geometry must not enumerate sketch contours")

        automation.doc.sketch.GetConstrainedStatus = reject_constraint_status
        automation.doc.sketch.GetSketchContours = reject_contour_enumeration
        result = automation.create_parametric_sketch(
            name="ConstructionReference", plane="Front", unit="mm",
            entities=[{
                "id": "reference", "type": "line", "construction": True,
                "start": [0, 0], "end": [10, 2],
            }], constraints=[], dimensions=[], equations=[],
            solve={"mode": "construction_reference"},
            validation={"max_entities": 1},
            transaction={"rollback_on_failure": True},
            output_mode="construction_reference")
        self.assertTrue(result["success"], result)
        self.assertTrue(
            result["data"]["constraint_status_evaluation_skipped"])
        self.assertEqual(
            result["data"]["status"],
            "not_evaluated_construction_reference")
        self.assertIsNone(result["data"]["status_code"])
        self.assertTrue(result["data"]["contour_enumeration_skipped"])
        self.assertEqual(result["data"]["closed_contours"], 0)

    def test_nonconstruction_sketch_still_evaluates_status_and_contours(self):
        automation = FakeAutomation()
        calls = {"status": 0, "contours": 0}
        original_status = automation.doc.sketch.GetConstrainedStatus
        original = automation.doc.sketch.GetSketchContours

        def record_constraint_status():
            calls["status"] += 1
            return original_status()

        def record_contour_enumeration():
            calls["contours"] += 1
            return original()

        automation.doc.sketch.GetConstrainedStatus = record_constraint_status
        automation.doc.sketch.GetSketchContours = record_contour_enumeration
        result = automation.create_parametric_sketch(
            name="WorkingContour", plane="Front", unit="mm",
            entities=[{
                "id": "edge", "type": "line",
                "start": [0, 0], "end": [10, 2],
            }], constraints=[], dimensions=[], equations=[],
            solve={}, validation={},
            transaction={"rollback_on_failure": True})
        self.assertTrue(result["success"], result)
        self.assertEqual(calls, {"status": 1, "contours": 1})
        self.assertFalse(
            result["data"]["constraint_status_evaluation_skipped"])
        self.assertFalse(result["data"]["contour_enumeration_skipped"])

    def test_batch_dimensions_one_rebuild_and_setting_restore(self):
        automation = FakeAutomation()
        segment = FakeSegment((0, 0, 0), (0.01, 0, 0))
        automation.doc.sketch.segments = [segment]
        automation.doc.feature.Name = "Dims"
        automation._runtime.register_entities("fake", "Dims", {
            "line": {"object": segment,
                     "points": {"start": segment.start, "end": segment.end},
                     "persistent_id": "x"}})
        dimensions = [{"id": f"length_{i}", "type": "length",
                       "entity": "line", "value": 10 + i,
                       "unit": "mm", "text_position": [5, i]}
                      for i in range(50)]
        result = automation.add_dimensions_batch("Dims", dimensions)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["rebuild_count"], 1)
        self.assertEqual(len(result["data"]["dimensions"]), 50)
        self.assertTrue(all(item["driving"]
                            for item in result["data"]["dimensions"]))
        self.assertTrue(result["data"]["dimension_input_guard"]
                        ["disabled_verified"])
        self.assertTrue(result["data"]["dimension_input_guard"]
                        ["preference_restored"])
        self.assertTrue(automation._sw_app.value)

    def test_runtime_idempotency_and_budget(self):
        runtime = RuntimeState()
        runtime.idempotent_put("key", {"success": True})
        self.assertTrue(runtime.idempotent_get("key")["success"])
        violation = runtime.budget_violation({"max_rebuilds_per_sketch": 1},
                                             rebuilds=2)
        self.assertEqual(violation["limit"], "max_rebuilds_per_sketch")
        automation = FakeAutomation()
        self.assertTrue(automation._elapsed_budget_exceeded(
            time.monotonic(), {"max_elapsed_sec": 0}))
        with self.assertRaises(ValueError):
            automation._elapsed_budget_exceeded(
                time.monotonic(), {"max_elapsed_sec": -1})
        stopped = automation.run_transaction(
            "budget_stop", [{"op": "show_body", "args": {"name": "X"}}],
            checkpoint={"mode": "none"},
            budget={"max_elapsed_sec": 0},
            save_policy={"save_after_success": False,
                         "save_checkpoint": False},
            allow_unsaved_document=True)
        self.assertFalse(stopped["success"])
        self.assertEqual(stopped["data"]["error"]["code"],
                         "BUDGET_EXCEEDED")
        self.assertEqual(automation._runtime.metrics["budget_exceeded"], 1)

    def test_model_graph_idempotency_changes_with_desired_graph(self):
        automation = FakeAutomation()
        plan_ids = []

        def execute(plan_id, operations, **kwargs):
            plan_ids.append(plan_id)
            return automation._result(
                True, "applied", data={"operations": operations})

        automation.execute_cad_plan = execute
        first = automation.sync_model_graph(
            "fixture", [{"id": "visibility", "op": "show_body",
                         "args": {"name": "BodyA"}}], mode="apply")
        second = automation.sync_model_graph(
            "fixture", [{"id": "visibility", "op": "show_body",
                         "args": {"name": "BodyB"}}], mode="apply")
        planned = automation.sync_model_graph(
            "fixture", [{"id": "visibility", "op": "show_body",
                         "args": {"name": "BodyB"}}], mode="plan")
        self.assertTrue(first["success"] and second["success"])
        self.assertNotEqual(plan_ids[0], plan_ids[1])
        self.assertTrue(first["data"]["desired_graph_sha256"].startswith(
            plan_ids[0].rsplit(":", 1)[-1]))
        self.assertEqual(planned["data"]["diff"][0]["action"], "unchanged")

    def test_dialog_classification_requires_identity(self):
        kind, confidence = _classify_dialog({
            "title": "Modify", "controls": [
                {"text": "D31@Front_Limb_Centerlines"},
                {"text": "9.80 mm"}]})
        self.assertEqual(kind, "dimension_modify")
        self.assertGreater(confidence, 0.9)
        kind, _ = _classify_dialog({"title": "Warning", "controls": []})
        self.assertEqual(kind, "unknown")
        kind, confidence = _classify_dialog({
            "title": "Modify", "controls": [],
            "context_text": (
                "SOLIDWORKS 2026 - [Watchdog_Sketch of "
                "watchdog_acceptance_v6520.SLDPRT]")})
        self.assertEqual(kind, "dimension_modify")
        self.assertGreater(confidence, 0.9)

    def test_owner_drawn_modify_requires_context_identity_before_enter(self):
        dialog = {
            "hwnd": 202, "title": "Modify", "class": "#32770",
            "classification": "dimension_modify", "text": "",
            "context_text": (
                "SOLIDWORKS 2026 - [Watchdog_Sketch of "
                "watchdog_acceptance_v6520.SLDPRT]"),
            "controls": [],
        }
        with patch("win32api.PostMessage") as post:
            mismatch = resolve_known_dialog(
                dialog, ["dimension_modify"], "Different_Sketch")
            self.assertFalse(mismatch["resolved"])
            post.assert_not_called()
            accepted = resolve_known_dialog(
                dialog, ["dimension_modify"], "Watchdog_Sketch")
        self.assertTrue(accepted["resolved"])
        self.assertEqual(accepted["method"], "dialog_enter")
        self.assertEqual(post.call_count, 2)

    def test_verified_sldworks_main_cache_requires_same_hwnd_and_pid(self):
        original = dict(_SW_MAIN_WINDOW_CACHE)
        try:
            _SW_MAIN_WINDOW_CACHE.update({"hwnd": 101, "pid": 202})
            valid = (101, "SldWorksFrame", "SOLIDWORKS 2026 - [Part]", 202)
            self.assertEqual(_cached_sldworks_main([valid]), valid)
            self.assertIsNone(_cached_sldworks_main([
                (101, "SldWorksFrame", "SOLIDWORKS 2026 - [Part]", 999)]))
            self.assertEqual(_SW_MAIN_WINDOW_CACHE, {"hwnd": 0, "pid": 0})
        finally:
            _SW_MAIN_WINDOW_CACHE.clear()
            _SW_MAIN_WINDOW_CACHE.update(original)

    def test_fast_modal_scan_does_not_read_blocked_window_text(self):
        original = dict(_SW_MAIN_WINDOW_CACHE)
        try:
            _SW_MAIN_WINDOW_CACHE.update({"hwnd": 101, "pid": 202})
            with patch("win32gui.IsWindow", return_value=True), patch(
                    "win32process.GetWindowThreadProcessId",
                    return_value=(303, 202)), patch(
                    "win32gui.IsWindowEnabled", return_value=False), patch(
                    "win32gui.GetWindowText") as get_text, patch(
                    "win32gui.EnumWindows") as enum_windows:
                info = detect_modal_dialog(include_controls=False)
            self.assertTrue(info["modal"])
            self.assertEqual(info["inspection_level"], "basic")
            self.assertEqual(info["main_window_hwnd"], 101)
            self.assertIn("detected_at", info)
            get_text.assert_not_called()
            enum_windows.assert_not_called()
        finally:
            _SW_MAIN_WINDOW_CACHE.clear()
            _SW_MAIN_WINDOW_CACHE.update(original)

    def test_vector_fit_rectangle_and_circle(self):
        import numpy as np
        automation = SolidWorksAutomation()
        rectangle = []
        rectangle.extend([[x, 0] for x in np.linspace(0, 20, 101)])
        rectangle.extend([[20, y] for y in np.linspace(0, 10, 51)[1:]])
        rectangle.extend([[x, 10] for x in np.linspace(20, 0, 101)[1:]])
        rectangle.extend([[0, y] for y in np.linspace(10, 0, 51)[1:]])
        entities, error = automation._fit_loop_hybrid(
            rectangle, 0.05, ["line", "arc", "circle", "spline"], 20)
        self.assertLessEqual(len(entities), 20)
        self.assertLessEqual(error, 0.05)
        circle = [[10 * math.cos(a), 10 * math.sin(a)]
                  for a in [i * 2 * math.pi / 720 for i in range(720)]]
        entities, error = automation._fit_loop_hybrid(
            circle, 0.02, ["line", "arc", "circle", "spline"], 10)
        self.assertEqual(entities[0]["type"], "circle")
        self.assertLessEqual(error, 0.02)

    def test_periodic_bspline_optimizer_is_accuracy_and_complexity_bounded(self):
        automation = SolidWorksAutomation()
        points = []
        for index in range(720):
            angle = index * 2 * math.pi / 720
            radius = 10.0 + 0.35 * math.sin(5 * angle)
            points.append([radius * math.cos(angle), radius * math.sin(angle)])
        approximation = automation._resolve_approximation(
            {}, {"preset": "balanced", "curve_strategy": "periodic_bspline",
                 "max_error_mm": 0.08,
                 "simplification_tolerance_mm": 0.08,
                 "max_total_control_points": 128})
        entities, error = automation._fit_loop_hybrid(
            points, 0.08, approximation["prefer"], 20,
            approximation=approximation)
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["type"], "b_spline")
        self.assertLessEqual(error, 0.08)
        self.assertLessEqual(len(entities[0]["control_points"]), 128)
        self.assertEqual(len(entities[0]["knots"]),
                         len(entities[0]["control_points"]) + 1)

    def test_open_bspline_optimizer_is_accuracy_and_complexity_bounded(self):
        import numpy as np

        automation = SolidWorksAutomation()
        x = np.linspace(0.0, 30.0, 500)
        points = np.column_stack([x, 3.0 * np.sin(x / 6.0)])
        entity, diagnostics = automation._fit_open_bspline(
            points, tolerance=0.05, max_control_points=64)
        self.assertIsNotNone(entity, diagnostics)
        self.assertFalse(entity["periodic"])
        self.assertLessEqual(entity["fit_error_mm"], 0.05)
        self.assertLessEqual(len(entity["control_points"]), 64)
        self.assertEqual(len(entity["knots"]),
                         len(entity["control_points"]) + entity["order"])

    def test_bspline_uses_typed_parameter_data(self):
        class ParameterData:
            Dimension = None
            Order = None
            Periodic = None
            ControlPointsCount = None

            def SetControlPoints(self, values):
                self.controls = list(values.value)
                return True

            def SetKnotPoints(self, values):
                self.knots = list(values.value)
                return True

        class Manager:
            parameter_data = None

            def CreateSplineParamData(self):
                self.parameter_data = ParameterData()
                return self.parameter_data

            def CreateSplinesByEqnParams2(self, parameter_data):
                self.parameter_data = parameter_data
                return ("segment",)

        manager = Manager()
        controls = [(0.0, 0.0, 0.0), (0.01, 0.0, 0.0),
                    (0.01, 0.01, 0.0), (0.0, 0.01, 0.0)]
        knots = [0.0, 0.25, 0.5, 0.75, 1.0]
        segment = SolidWorksAutomation._create_b_spline(
            manager, controls, knots, 4, True)
        self.assertEqual(segment, "segment")
        self.assertEqual(manager.parameter_data.Dimension, 3)
        self.assertEqual(manager.parameter_data.Order, 4)
        self.assertEqual(manager.parameter_data.Periodic, 1)
        self.assertEqual(manager.parameter_data.ControlPointsCount, 4)
        self.assertEqual(len(manager.parameter_data.controls), 12)
        self.assertEqual(manager.parameter_data.knots, knots)

    def test_bspline_rejects_wrong_periodic_knot_count(self):
        with self.assertRaisesRegex(ValueError, "periodic curves"):
            SolidWorksAutomation._create_b_spline(
                object(), [(0.0, 0.0, 0.0)] * 4,
                [float(index) for index in range(8)], 4, True)

    def test_construction_commit_plan_batches_a_loop_without_losing_ids(self):
        automation = SolidWorksAutomation()
        loops = [{"closed": False, "entities": [
            {"id": "curve", "type": "b_spline", "order": 4,
             "periodic": False,
             "control_points": [[0, 0], [0.3, 0], [0.7, 0], [1, 0]],
             "knots": [0, 0, 0, 0, 1, 1, 1, 1]},
            {"id": "line", "type": "line",
             "start": [1.1, 0], "end": [2, 0]},
            {"id": "arc", "type": "arc", "center": [2, 1],
             "start": [2, 0], "end": [3, 1], "direction": 1},
        ]}]
        entities, report = automation._construction_nurbs_commit_plan(
            loops, "construction_reference", 0.1, 64)
        self.assertTrue(report["applied"], report)
        self.assertEqual(len(entities), 1)
        chain = entities[0]
        self.assertEqual(chain["type"], "b_spline_chain")
        self.assertEqual([item["id"] for item in chain["segments"]],
                         ["curve", "line", "arc"])
        self.assertEqual(len(chain["knots"]),
                         len(chain["control_points"]) + 4)
        self.assertAlmostEqual(
            chain["segments"][0]["control_points"][-1][0], 1.05)
        self.assertAlmostEqual(
            chain["segments"][1]["control_points"][0][0], 1.05)
        self.assertLessEqual(
            report["loops"][0]["max_endpoint_adjustment_mm"], 0.1)

    def test_construction_commit_plan_rejects_large_join_adjustment(self):
        automation = SolidWorksAutomation()
        loops = [{"closed": False, "entities": [
            {"id": "left", "type": "line",
             "start": [0, 0], "end": [1, 0]},
            {"id": "right", "type": "line",
             "start": [2, 0], "end": [3, 0]},
        ]}]
        entities, report = automation._construction_nurbs_commit_plan(
            loops, "construction_reference", 0.1, 64)
        self.assertFalse(report["applied"])
        self.assertEqual(report["reason"],
                         "join_adjustment_exceeds_tolerance")
        self.assertEqual([item["id"] for item in entities],
                         ["left", "right"])

    def test_bspline_chain_maps_reversed_segments_and_sets_construction(self):
        automation = FakeAutomation()
        first = FakeEquationSplineSegment(
            (0.01, 0.0, 0.0), (0.0, 0.0, 0.0))
        second = FakeEquationSplineSegment(
            (0.02, 0.0, 0.0), (0.01, 0.0, 0.0))
        sources = [
            {"id": "a", "type": "b_spline", "order": 2,
             "control_points": [[0, 0], [10, 0]],
             "knots": [0, 0, 1, 1], "construction": True,
             "commit_conversion": "batched_composite_nurbs"},
            {"id": "b", "type": "b_spline", "order": 2,
             "control_points": [[10, 0], [20, 0]],
             "knots": [0, 0, 1, 1], "construction": True,
             "commit_conversion": "batched_composite_nurbs"},
        ]
        chain = {
            "id": "chain", "type": "b_spline_chain", "order": 2,
            "periodic": False, "construction": True,
            "control_points": [[0, 0], [10, 0], [20, 0]],
            "knots": [0, 0, 0.5, 1, 1], "segments": sources,
            "endpoint_match_tolerance_mm": 0.002,
        }
        with patch.object(
                automation, "_create_b_spline_segments",
                return_value=[second, first]):
            result = automation._create_entity(
                automation.doc, chain, "mm")
        self.assertTrue(first.ConstructionGeometry)
        self.assertTrue(second.ConstructionGeometry)
        self.assertEqual([item["id"] for item in result["items"]],
                         ["a", "b"])
        self.assertTrue(all(item["orientation_reversed"]
                            for item in result["items"]))

    def test_com_out_array_and_exact_nurbs_sampling(self):
        self.assertEqual(
            SolidWorksAutomation._com_out_array((True, (1.0, 2.0))),
            [1.0, 2.0])
        self.assertEqual(
            SolidWorksAutomation._com_out_array((False, (1.0, 2.0))), [])
        nurbs = {
            "degree": 3,
            "parameter_range": [0.0, 1.0],
            "curve_length": 3.0,
            "knots": [0.0, 0.0, 0.0, 0.0,
                      1.0, 1.0, 1.0, 1.0],
            "control_points": [[0.0, 0.0], [1.0, 0.0],
                               [2.0, 0.0], [3.0, 0.0]],
            "weights": [],
        }
        samples = SolidWorksAutomation._sample_nurbs(nurbs, 0.25)
        self.assertEqual(samples[0], [0.0, 0.0])
        self.assertEqual(samples[-1], [3.0, 0.0])
        self.assertTrue(all(abs(point[1]) <= 1e-12 for point in samples))
        self.assertTrue(all(samples[index][0] <= samples[index + 1][0]
                            for index in range(len(samples) - 1)))

    def test_periodic_spline_is_intrinsically_closed_in_export_topology(self):
        contours = SolidWorksAutomation._contours_from_entities([{
            "id": "curve", "type": "spline", "construction": False,
            "nurbs": {"closed": True, "periodic": True},
        }])
        self.assertEqual(len(contours), 1)
        self.assertTrue(contours[0]["closed"])

    def test_full_circle_arc_is_intrinsically_closed_in_export_topology(self):
        contours = SolidWorksAutomation._contours_from_entities([{
            "id": "circle", "type": "arc", "construction": False,
            "center": [0.0, 0.0], "radius": 3.0,
            "start": [3.0, 0.0], "end": [3.0, 0.0],
        }])
        self.assertEqual(len(contours), 1)
        self.assertTrue(contours[0]["closed"])

    def test_synthetic_image_analysis_and_artifacts(self):
        import cv2
        import numpy as np
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            image = np.full((512, 512, 3), 255, np.uint8)
            cv2.rectangle(image, (100, 130), (412, 382), (190, 205, 220), -1)
            cv2.circle(image, (256, 256), 55, (20, 20, 20), -1)
            path = os.path.join(directory, "input.png")
            cv2.imwrite(path, image)
            result = automation.image_to_sketch(
                path, "Synthetic", calibration={"mode": "bbox_width", "value": 100},
                trace={"backend": "classical"},
                placement={"image_anchor": "silhouette_bbox_center",
                           "model_anchor": [0, 0, 0]},
                geometry={"max_error_mm": 0.2, "max_entities": 20,
                          "min_feature_mm": 0.5,
                          "prefer": ["line", "arc", "circle", "spline"]},
                validation={"min_iou": 0.97, "max_hausdorff_mm": 0.8},
                commit={"mode": "analyze_only"},
                debug={"directory": directory, "save_overlay": True,
                       "save_vector_json": True})
            self.assertTrue(result["success"], result)
            self.assertTrue(Path(result["data"]["overlay"]).exists())
            self.assertTrue(Path(result["data"]["vector_json"]).exists())
            self.assertTrue(Path(
                result["data"]["selected_boundary_map"]).exists())

    def test_synthetic_output_modes_and_homography_reach_vector_payload(self):
        import cv2
        import numpy as np

        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            image = np.full((256, 256, 3), 255, np.uint8)
            cv2.rectangle(image, (48, 36), (210, 220), (40, 80, 140), -1)
            path = os.path.join(directory, "projection.png")
            cv2.imwrite(path, image)
            common = {
                "image_path": path,
                "trace": {"backend": "classical"},
                "calibration": {"mode": "bbox_height", "value": 80},
                "placement": {"image_anchor": "silhouette_bbox_center",
                              "model_anchor": [0, 0, 0]},
                "validation": {"min_iou": 0.94,
                               "max_hausdorff_mm": 1.0},
                "commit": {"mode": "analyze_only"},
                "debug": {"directory": directory},
            }
            construction = automation.image_to_sketch(
                sketch_name="ConstructionProjection",
                approximation={"output_mode": "construction_reference",
                               "max_error_mm": 0.3,
                               "max_entities": 30},
                projection={
                    "mode": "homography",
                    "source_quad_px": [[0, 0], [255, 0],
                                       [255, 255], [0, 255]],
                    "output_size_px": [192, 192],
                },
                require_orthographic=True,
                **common)
            self.assertTrue(construction["success"], construction)
            self.assertEqual(
                construction["data"]["projection"]["mode"], "homography")
            self.assertTrue(Path(
                construction["data"]["projection"]["rectified_image"]).exists())
            payload = json.loads(Path(
                construction["data"]["vector_json"]).read_text(
                    encoding="utf-8"))
            entities = [entity for loop in payload["loops"]
                        for entity in loop["entities"]]
            self.assertTrue(entities)
            self.assertTrue(all(item["construction"] for item in entities))
            self.assertEqual(
                payload["parameterization"]["construction_entities"],
                len(entities))
            self.assertIn("source_pixel_to_sketch", payload)

            reference = automation.image_to_sketch(
                sketch_name="ReferenceSplineProjection",
                approximation={"output_mode": "reference_spline",
                               "max_error_mm": 0.3,
                               "simplification_tolerance_mm": 0.3,
                               "max_entities": 30},
                **common)
            self.assertTrue(reference["success"], reference)
            self.assertEqual(
                set(reference["data"]["entity_types"]), {"b_spline"})

    def test_offline_vector_dispatch_runs_in_isolated_process(self):
        import cv2
        import numpy as np
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            image = np.full((256, 256, 3), 255, np.uint8)
            cv2.rectangle(image, (40, 50), (216, 206), (60, 90, 140), -1)
            path = os.path.join(directory, "worker.png")
            cv2.imwrite(path, image)
            arguments = {
                "image_path": path,
                "sketch_name": "WorkerPreview",
                "trace": {"backend": "classical"},
                "calibration": {"mode": "bbox_width", "value": 80},
                "placement": {"image_anchor": "silhouette_bbox_center",
                              "model_anchor": [0, 0, 0]},
                "geometry": {"max_error_mm": 0.2, "max_entities": 20},
                "validation": {"min_iou": 0.97,
                               "max_hausdorff_mm": 0.8},
                "commit": {"mode": "analyze_only"},
                "debug": {"directory": directory},
            }
            result = _dispatch_offline_worker(
                automation, "image_to_sketch", arguments)
            self.assertTrue(result["success"], result)
            self.assertFalse(result["data"]["committed"])

    def test_offline_vector_worker_enforces_zero_budget(self):
        result = _dispatch_offline_worker(
            SolidWorksAutomation(), "image_to_sketch",
            {"budget": {"max_elapsed_sec": 0}})
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error"]["code"],
                         "BUDGET_EXCEEDED")

    def test_sketch_reference_dispatch_exports_com_then_uses_worker(self):
        automation = SolidWorksAutomation()
        geometry = {
            "entities": [{"id": "edge", "type": "line",
                          "start": [0.0, 0.0], "end": [1.0, 0.0]}],
            "contours": [],
        }
        exported = []

        def load_geometry(sketch_name, unit, include=None):
            exported.append((sketch_name, unit, include))
            return {"success": True}, geometry

        automation._load_geometry_payload = load_geometry
        worker_result = {
            "success": True, "message": "PASS", "error_code": 0,
            "error_name": "swSuccess", "data": {"pass": True}}
        arguments = {
            "sketch_name": "NativeSketch", "image_path": "reference.png",
            "budget": {"max_elapsed_sec": 30},
        }
        with patch("solidworks_mcp.server._dispatch_offline_worker",
                   return_value=worker_result) as worker:
            result = _dispatch_two_phase_sketch_comparison(
                automation, arguments)

        self.assertTrue(result["success"])
        self.assertEqual(exported[0][0:2], ("NativeSketch", "mm"))
        self.assertFalse(exported[0][2]["constraint_status"])
        self.assertFalse(exported[0][2]["relations"])
        self.assertFalse(exported[0][2]["dimensions"])
        worker_args = worker.call_args.args[2]
        self.assertEqual(worker.call_args.args[1],
                         "compare_sketch_to_reference")
        self.assertIs(worker_args["geometry_payload"], geometry)
        self.assertEqual(worker_args["reference_image"], "reference.png")
        self.assertFalse(result["data"]["execution_boundary"]
                         ["native_image_libraries_in_com"])

    def test_offline_sketch_reference_worker_runs_without_com(self):
        import cv2
        import numpy as np

        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            image = np.full((64, 64, 3), 255, dtype=np.uint8)
            cv2.rectangle(image, (16, 16), (48, 48), (0, 0, 0), -1)
            reference_path = os.path.join(directory, "reference.png")
            cv2.imwrite(reference_path, image)
            geometry = {
                "entities": [
                    {"id": "bottom", "type": "line",
                     "start": [16, 16], "end": [48, 16]},
                    {"id": "right", "type": "line",
                     "start": [48, 16], "end": [48, 48]},
                    {"id": "top", "type": "line",
                     "start": [48, 48], "end": [16, 48]},
                    {"id": "left", "type": "line",
                     "start": [16, 48], "end": [16, 16]},
                ],
                "contours": [{"id": "outer", "closed": True,
                              "entities": ["bottom", "right", "top", "left"]}],
            }
            result = _dispatch_offline_worker(
                automation, "compare_sketch_to_reference", {
                    "sketch_name": "OfflineRectangle",
                    "reference_image": reference_path,
                    "geometry_payload": geometry,
                    "image_mode": "filled_silhouette",
                    "transform": {"mode": "explicit",
                                  "matrix": np.eye(3).tolist()},
                    "contour_selection": {"min_area_px": 16},
                    "tolerance": {"profile": "draft", "min_iou": 0.9,
                                  "mean_mm": 2.0, "p95_mm": 3.0,
                                  "max_mm": 4.0,
                                  "min_segmentation_confidence": 0.5},
                    "budget": {"max_elapsed_sec": 30},
                })
        self.assertTrue(result["success"], result)
        self.assertTrue(result["data"]["pass"])

    def test_body_reference_dispatch_captures_com_then_uses_worker(self):
        automation = SolidWorksAutomation()
        captured = []

        def take_screenshot(path, **kwargs):
            captured.append((path, kwargs))
            return automation._result(
                True, "shot", data={"path": path, "frame_unreadable": False})

        automation.take_screenshot = take_screenshot
        worker_result = {
            "success": True, "message": "PASS", "error_code": 0,
            "error_name": "swSuccess", "data": {"pass": True}}
        arguments = {
            "reference_image": "reference.png",
            "screenshot_path": "candidate.png", "orientation": "right",
            "bodies": ["Housing"], "budget": {"max_elapsed_sec": 30},
            "candidate_source": "screenshot_segmentation",
        }
        with patch("solidworks_mcp.server._dispatch_offline_worker",
                   return_value=worker_result) as worker:
            result = _dispatch_two_phase_body_comparison(
                automation, arguments)

        self.assertTrue(result["success"])
        self.assertEqual(captured[0][0], "candidate.png")
        self.assertEqual(captured[0][1]["orientation"], "right")
        self.assertEqual(captured[0][1]["zoom_to_bodies"], ["Housing"])
        worker_args = worker.call_args.args[2]
        self.assertFalse(worker_args["capture_screenshot"])
        self.assertEqual(worker_args["screenshot_data"]["path"],
                         "candidate.png")
        self.assertFalse(result["data"]["execution_boundary"]
                         ["native_image_libraries_in_com"])

    def test_body_reference_dispatch_exports_native_mesh_and_restores_visibility(self):
        automation = SolidWorksAutomation()

        class Body:
            def __init__(self, name, visible):
                self.Name = name
                self.Visible = visible

            def HideBody(self, hidden):
                self.Visible = not hidden

        body_a = Body("Housing", True)
        body_b = Body("Insert", False)

        class Document:
            def GetBodies2(self, body_type, visible_only):
                return [body_a, body_b]

            def GraphicsRedraw2(self):
                return True

        automation.get_active_doc = lambda: (Document(), None)
        screenshot_states = []

        def take_screenshot(path, **kwargs):
            screenshot_states.append((body_a.Visible, body_b.Visible, kwargs))
            return automation._result(
                True, "shot", data={"path": path, "frame_unreadable": False})

        automation.take_screenshot = take_screenshot
        automation._export_body_stl = lambda doc, body, path, settings: Path(
            path).write_bytes(b"temporary native mesh")
        automation._inspect_stl = lambda path: {"triangles": 2}

        def worker_side_effect(instance, name, worker_arguments):
            self.assertEqual(name, "compare_body_silhouette_to_image")
            self.assertEqual(worker_arguments["candidate_source"], "native_mesh")
            self.assertEqual(worker_arguments["bodies"], ["Housing"])
            self.assertTrue(os.path.isfile(worker_arguments["mesh_paths"][0]))
            return {"success": True, "message": "PASS", "error_code": 0,
                    "error_name": "swSuccess", "data": {"pass": True}}

        with patch("solidworks_mcp.server._dispatch_offline_worker",
                   side_effect=worker_side_effect):
            result = _dispatch_two_phase_body_comparison(automation, {
                "reference_image": "reference.png",
                "screenshot_path": "candidate.png",
                "orientation": "front", "bodies": ["Housing"],
                "budget": {"max_elapsed_sec": 30},
            })

        self.assertTrue(result["success"], result)
        self.assertEqual(screenshot_states[0][:2], (True, False))
        self.assertTrue(body_a.Visible)
        self.assertFalse(body_b.Visible)
        boundary = result["data"]["execution_boundary"]
        self.assertEqual(
            boundary["com_process"],
            "orthographic_screenshot_and_native_stl_export")
        self.assertFalse(boundary["native_image_libraries_in_com"])

    def test_body_reference_dispatch_classifies_invalid_mesh_plan_before_com(self):
        automation = SolidWorksAutomation()
        automation.take_screenshot = lambda *args, **kwargs: (
            (_ for _ in ()).throw(
                AssertionError("Invalid mesh plan must fail before COM")))
        result = _dispatch_two_phase_body_comparison(automation, {
            "reference_image": "reference.png",
            "screenshot_path": "candidate.png",
            "candidate_source": "native_mesh",
            "mesh": {"quality": "fine", "deviation_mm": 0.01},
        })
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error"]["code"], "INVALID_PLAN")
        self.assertIn("quality=custom", result["message"])

    def test_native_mesh_silhouette_uses_triangle_union_not_viewport_shading(self):
        import cv2
        import numpy as np

        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            mesh_path = os.path.join(directory, "square.stl")
            triangles = [
                ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0)),
                ((0.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 0.0)),
                # Coincident front/back projections must form a union, not
                # cancel through OpenCV's multi-contour even-odd fill rule.
                ((10.0, 10.0, 5.0), (10.0, 0.0, 5.0), (0.0, 0.0, 5.0)),
                ((0.0, 10.0, 5.0), (10.0, 10.0, 5.0), (0.0, 0.0, 5.0)),
            ]
            with open(mesh_path, "wb") as handle:
                handle.write(b"native-square".ljust(80, b"\0"))
                handle.write(struct.pack("<I", len(triangles)))
                for triangle in triangles:
                    flattened = [0.0, 0.0, 1.0]
                    flattened.extend(value for vertex in triangle for value in vertex)
                    handle.write(struct.pack("<12fH", *flattened, 0))

            reference = np.full((96, 96, 3), 255, dtype=np.uint8)
            cv2.rectangle(reference, (20, 20), (70, 70), (0, 0, 0), -1)
            reference_path = os.path.join(directory, "reference.png")
            screenshot_path = os.path.join(directory, "shaded-viewport.png")
            cv2.imwrite(reference_path, reference)
            shaded = np.full((96, 96, 3), 235, dtype=np.uint8)
            for x, value in ((20, 40), (35, 120), (50, 210), (65, 70)):
                cv2.rectangle(shaded, (x, 20), (min(x + 15, 70), 70),
                              (value, value, value), -1)
            cv2.imwrite(screenshot_path, shaded)
            result = automation.compare_body_silhouette_to_image(
                reference_path, screenshot_path, orientation="front",
                bodies=["Square"], candidate_source="native_mesh",
                mesh_paths=[mesh_path],
                mesh_settings={"body_names": ["Square"], "unit": "mm"},
                capture_screenshot=False,
                transform={
                    "candidate_to_reference": [[5.0, 0.0, 20.0],
                                                [0.0, -5.0, 70.0],
                                                [0.0, 0.0, 1.0]],
                    "mm_per_pixel": 0.2,
                },
                tolerance={"profile": "draft", "min_iou": 0.99,
                           "max_hausdorff_mm": 0.21,
                           "min_segmentation_confidence": 0.5})

            self.assertTrue(result["success"], result)
            data = result["data"]
            self.assertEqual(data["candidate_source"], "native_mesh")
            self.assertEqual(data["candidate_segmentation_confidence"], 1.0)
            self.assertEqual(data["candidate_geometry"]["triangle_count"], 4)
            self.assertEqual(
                data["candidate_geometry"]["foreground_pixels"], 2601)
            self.assertEqual(data["candidate_geometry"]["bodies"], ["Square"])
            self.assertGreaterEqual(data["metrics"]["iou"], 0.99)

            shifted = np.full((96, 96, 3), 255, dtype=np.uint8)
            cv2.rectangle(shifted, (30, 20), (80, 70), (0, 0, 0), -1)
            shifted_path = os.path.join(directory, "shifted-reference.png")
            cv2.imwrite(shifted_path, shifted)
            mismatch = automation.compare_body_silhouette_to_image(
                shifted_path, screenshot_path, orientation="front",
                candidate_source="native_mesh", mesh_paths=[mesh_path],
                mesh_settings={"body_names": ["Square"], "unit": "mm"},
                capture_screenshot=False,
                transform={
                    "candidate_to_reference": [[5.0, 0.0, 20.0],
                                                [0.0, -5.0, 70.0],
                                                [0.0, 0.0, 1.0]],
                    "mm_per_pixel": 0.2,
                },
                tolerance={"profile": "balanced", "min_iou": 0.99,
                           "max_hausdorff_mm": 0.3,
                           "min_segmentation_confidence": 0.5})
            self.assertFalse(mismatch["success"])
            self.assertEqual(mismatch["data"]["error"]["code"],
                             "REFERENCE_MISMATCH")
            self.assertTrue(mismatch["data"]["maximum_deviation_zones"])

    def test_offline_body_reference_worker_runs_without_com(self):
        import cv2
        import numpy as np

        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            image = np.full((96, 96, 3), 255, dtype=np.uint8)
            cv2.circle(image, (48, 48), 24, (0, 0, 0), -1)
            reference_path = os.path.join(directory, "reference.png")
            candidate_path = os.path.join(directory, "candidate.png")
            cv2.imwrite(reference_path, image)
            cv2.imwrite(candidate_path, image)
            result = _dispatch_offline_worker(
                automation, "compare_body_silhouette_to_image", {
                    "reference_image": reference_path,
                    "screenshot_path": candidate_path,
                    "capture_screenshot": False,
                    "screenshot_data": {"path": candidate_path},
                    "transform": {
                        "candidate_to_reference": np.eye(3).tolist(),
                        "mm_per_pixel": 0.1,
                    },
                    "tolerance": {
                        "profile": "draft", "min_iou": 0.99,
                        "max_hausdorff_mm": 0.2,
                        "min_segmentation_confidence": 0.5,
                    },
                    "budget": {"max_elapsed_sec": 30},
                })
        self.assertTrue(result["success"], result)
        self.assertTrue(result["data"]["pass"])

    def test_direct_sketch_dispatch_exports_com_then_uses_worker(self):
        automation = SolidWorksAutomation()
        exported = []

        def load_geometry(sketch_name, unit, include=None):
            exported.append((sketch_name, unit, include))
            return {"success": True}, {
                "entities": [{"id": sketch_name, "type": "line",
                              "start": [0, 0], "end": [1, 0]}]}

        automation._load_geometry_payload = load_geometry
        worker_result = {
            "success": True, "message": "PASS", "error_code": 0,
            "error_name": "swSuccess", "data": {"pass": True}}
        with patch("solidworks_mcp.server._dispatch_offline_worker",
                   return_value=worker_result) as worker:
            result = _dispatch_two_phase_sketches_comparison(
                automation, {"reference_sketch": "Reference",
                             "candidate_sketch": "Candidate",
                             "budget": {"max_elapsed_sec": 30}})
        self.assertTrue(result["success"])
        self.assertEqual([item[0] for item in exported],
                         ["Reference", "Candidate"])
        self.assertFalse(exported[0][2]["constraint_status"])
        worker_args = worker.call_args.args[2]
        self.assertEqual(worker_args["reference_geometry"]["entities"][0]["id"],
                         "Reference")
        self.assertEqual(worker_args["candidate_geometry"]["entities"][0]["id"],
                         "Candidate")
        self.assertFalse(result["data"]["execution_boundary"]
                         ["native_scientific_libraries_in_com"])

    def test_offline_direct_sketch_worker_and_mismatch_classification(self):
        reference = {"entities": [{
            "id": "edge", "type": "line", "construction": False,
            "start": [0.0, 0.0], "end": [10.0, 0.0]}]}
        candidate = {"entities": [{
            "id": "shifted_edge", "type": "line", "construction": False,
            "start": [0.0, 0.1], "end": [10.0, 0.1]}]}
        result = _dispatch_offline_worker(
            SolidWorksAutomation(), "compare_sketches", {
                "reference_sketch": "Reference",
                "candidate_sketch": "Candidate",
                "reference_geometry": reference,
                "candidate_geometry": candidate,
                "tolerance": {"sample_step": 0.02, "mean_mm": 0.05,
                              "p95_mm": 0.05, "max_mm": 0.05},
                "budget": {"max_elapsed_sec": 30},
            })
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error"]["code"],
                         "REFERENCE_MISMATCH")
        self.assertEqual(result["data"]["metrics"]["worst_candidate_entity"],
                         "shifted_edge")

    def test_svg_contains_native_layers_primitives_and_full_metadata(self):
        import xml.etree.ElementTree as ET

        automation = SolidWorksAutomation()
        geometry = {
            "document": {"title": "part.SLDPRT", "path": "part.SLDPRT",
                         "configuration": "Default"},
            "sketch": {"name": "MixedSketch", "unit": "mm",
                       "plane": "Front Plane",
                       "model_to_sketch_transform": list(range(16)),
                       "bbox": {"min": [0, 0], "max": [30, 20]},
                       "constraint_status": "under_defined"},
            "entities": [
                {"id": "line", "type": "line", "construction": False,
                 "start": [0, 0], "end": [10, 0]},
                {"id": "circle", "type": "arc", "construction": False,
                 "center": [5, 5], "radius": 2},
                {"id": "arc", "type": "arc", "construction": False,
                 "center": [15, 5], "radius": 3, "start": [12, 5],
                 "end": [15, 8], "start_angle": math.pi,
                 "end_angle": math.pi / 2, "clockwise": True},
                {"id": "ellipse", "type": "ellipse", "construction": False,
                 "center": [20, 10], "major_radius": 4,
                 "minor_radius": 2, "rotation_deg": 30},
                {"id": "spline", "type": "spline", "construction": False,
                 "fit_points": [[0, 10], [5, 15], [10, 10]]},
            ],
        }
        automation._load_geometry_payload = lambda *args, **kwargs: (
            {"success": True}, geometry)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "mixed.svg")
            result = automation.render_sketch_svg(
                ["MixedSketch"], path,
                view={"unit": "mm", "padding": 2,
                      "invert_y_for_display": True},
                style={"stroke_width": 0.25})
            self.assertTrue(result["success"], result)
            root = ET.parse(path).getroot()
            namespace = {"svg": "http://www.w3.org/2000/svg"}
            metadata = json.loads(root.find("svg:metadata", namespace).text)
            tags = {item.tag.rsplit("}", 1)[-1]
                    for item in root.findall(".//*[@id]", namespace)}
            ids = {item.attrib["id"]
                   for item in root.findall(".//*[@id]", namespace)}
        self.assertEqual(metadata["document"]["configuration"], "Default")
        self.assertEqual(metadata["sketches"][0]["plane"], "Front Plane")
        self.assertEqual(len(metadata["sketches"][0]
                             ["model_to_sketch_transform"]), 16)
        self.assertTrue({"line", "circle", "path", "ellipse"} <= tags)
        self.assertTrue({"line", "circle", "arc", "ellipse", "spline"} <= ids)

    def test_mutating_vectorization_uses_two_phase_worker(self):
        automation = SolidWorksAutomation()
        committed = []

        def commit_vector_analysis(**kwargs):
            committed.append(kwargs)
            return {"success": True, "message": "committed",
                    "error_code": 0, "error_name": "swSuccess", "data": {}}

        automation.commit_vector_analysis = commit_vector_analysis
        analysis = {
            "success": True, "message": "analyzed", "error_code": 0,
            "error_name": "swSuccess",
            "data": {"validation_pass": True, "confidence": 1.0,
                     "vector_json": "result.json"},
        }
        arguments = {
            "image_path": "input.png", "sketch_name": "TwoPhase",
            "calibration": {"mode": "bbox_width", "value": 10},
            "commit": {"mode": "commit_if_confident", "min_confidence": 0.99},
            "budget": {"max_elapsed_sec": 60},
        }
        with patch("solidworks_mcp.server._dispatch_offline_worker",
                   return_value=analysis) as worker:
            result = _dispatch_two_phase_vector_commit(automation, arguments)
        self.assertTrue(result["success"])
        worker_arguments = worker.call_args.args[2]
        self.assertEqual(worker_arguments["commit"]["mode"], "analyze_only")
        self.assertTrue(worker_arguments["debug"]["save_vector_json"])
        self.assertTrue(worker_arguments["debug"]["save_reference_raster"])
        self.assertEqual(committed[0]["sketch_name"], "TwoPhase")
        self.assertEqual(committed[0]["commit"]["mode"],
                         "commit_if_confident")
        self.assertGreater(committed[0]["budget"]["max_elapsed_sec"], 0)
        self.assertLessEqual(committed[0]["budget"]["max_elapsed_sec"], 60)

    def test_commit_vector_analysis_validates_then_uses_cad_only(self):
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            vector_path = os.path.join(directory, "candidate.vector.json")
            payload = {
                "schema": "solidworks-mcp/image-vector/v1",
                "source": "input.png", "image_shape": [100, 100],
                "pixel_to_sketch": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                "scale_mm_per_px": 1.0,
                "validation_pass": True, "confidence": 1.0,
                "approximation": {"max_entities": 10,
                                  "output_mode": "locked_trace"},
                "loops": [{"role": "outer", "entities": [{
                    "id": "edge_1", "type": "line",
                    "start": [0, 0], "end": [1, 0]}]}],
            }
            Path(vector_path).write_text(
                json.dumps(payload), encoding="utf-8")
            automation.create_parametric_sketch = lambda **kwargs: {
                "success": True, "message": "created", "error_code": 0,
                "error_name": "swSuccess", "data": {"kwargs": kwargs}}
            automation._validate_committed_geometry = lambda *args, **kwargs: {
                "pass": True,
                "stage": "post_commit_cad_reverse_raster",
                "metrics": {"balanced_support": 1.0,
                            "hausdorff_mm": 0.0},
            }
            result = automation.commit_vector_analysis(
                {"success": True, "data": {
                    "validation_pass": True, "confidence": 1.0,
                    "vector_json": vector_path}},
                sketch_name="OfflineCommit",
                commit={"mode": "commit_if_confident",
                        "min_confidence": 0.99})
            self.assertTrue(result["success"], result)
            self.assertTrue(result["data"]["committed"])
            self.assertEqual(result["data"]["analysis_process"],
                             "isolated_worker")
            self.assertTrue(result["data"]["cad_validation"]["pass"])

    def test_construction_commit_uses_one_batched_nurbs_transport(self):
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            vector_path = os.path.join(directory, "construction.vector.json")
            payload = {
                "schema": "solidworks-mcp/image-vector/v1",
                "source": "input.png", "image_shape": [100, 100],
                "pixel_to_sketch": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                "scale_mm_per_px": 1.0,
                "validation_pass": True, "confidence": 1.0,
                "approximation": {
                    "max_entities": 10, "max_error_mm": 0.1,
                    "max_total_fit_points": 10,
                    "max_total_control_points": 64,
                    "max_control_points_per_spline": 64,
                    "output_mode": "construction_reference",
                },
                "loops": [{"role": "outer_edge", "closed": False,
                           "entities": [
                    {"id": "left", "type": "line", "construction": True,
                     "start": [0, 0], "end": [1, 0]},
                    {"id": "right", "type": "line", "construction": True,
                     "start": [1, 0], "end": [2, 0]},
                ]}],
            }
            Path(vector_path).write_text(
                json.dumps(payload), encoding="utf-8")
            calls = []
            automation.create_parametric_sketch = lambda **kwargs: (
                calls.append(kwargs) or {
                    "success": True, "message": "created", "error_code": 0,
                    "error_name": "swSuccess", "data": {}})
            automation._validate_committed_geometry = lambda *args, **kwargs: {
                "pass": True,
                "stage": "post_commit_cad_reverse_raster",
                "metrics": {"balanced_support": 1.0,
                            "hausdorff_mm": 0.0},
            }
            result = automation.commit_vector_analysis(
                {"success": True, "data": {
                    "validation_pass": True, "confidence": 1.0,
                    "vector_json": vector_path}},
                sketch_name="ConstructionBatch",
                commit={"mode": "commit_if_confident"},
                budget={"max_elapsed_sec": 60})
        self.assertTrue(result["success"], result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]["entities"]), 1)
        chain = calls[0]["entities"][0]
        self.assertEqual(chain["type"], "b_spline_chain")
        self.assertEqual([item["id"] for item in chain["segments"]],
                         ["left", "right"])
        self.assertTrue(result["data"]["commit_optimization"]["applied"])
        self.assertEqual(result["data"]["commit_profile"]["cad_calls"], 1)

    def test_post_commit_cad_validation_failure_rolls_back(self):
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            vector_path = os.path.join(directory, "candidate.vector.json")
            payload = {
                "schema": "solidworks-mcp/image-vector/v1",
                "source": "input.png", "image_shape": [100, 100],
                "pixel_to_sketch": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                "scale_mm_per_px": 1.0,
                "validation_pass": True, "confidence": 1.0,
                "approximation": {"max_entities": 10,
                                  "output_mode": "locked_trace"},
                "loops": [{"role": "outer", "entities": [{
                    "id": "edge_1", "type": "line",
                    "start": [0, 0], "end": [1, 0]}]}],
            }
            Path(vector_path).write_text(
                json.dumps(payload), encoding="utf-8")
            automation.create_parametric_sketch = lambda **kwargs: {
                "success": True, "message": "created", "error_code": 0,
                "error_name": "swSuccess", "data": {}}
            automation._validate_committed_geometry = lambda *args, **kwargs: {
                "pass": False, "metrics": {"balanced_support": 0.5},
                "overlay": "cad-overlay.png"}
            feature = object()
            automation.get_active_doc = lambda: (object(), None)
            automation._find_sketch_feature = lambda *args: feature
            automation._rollback_created_sketch = lambda *args: True
            result = automation.commit_vector_analysis(
                {"success": True, "data": {
                    "validation_pass": True, "confidence": 1.0,
                    "vector_json": vector_path}},
                sketch_name="RejectedCadCommit",
                commit={"mode": "commit_if_confident",
                        "rollback_on_failure": True},
                idempotency_key="post-cad-fail")
            self.assertFalse(result["success"])
            self.assertFalse(result["data"]["error"]["details"]["committed"])
            self.assertTrue(result["data"]["error"]["document_restored"])
            self.assertIsNone(automation._runtime.idempotent_get(
                "post-cad-fail"))

    def test_commit_vector_analysis_rejects_complexity_overflow(self):
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            vector_path = os.path.join(directory, "complex.vector.json")
            controls = [[float(index), 0.0] for index in range(8)]
            payload = {
                "schema": "solidworks-mcp/image-vector/v1",
                "validation_pass": True,
                "approximation": {"max_entities": 10,
                                  "max_total_fit_points": 20,
                                  "max_total_control_points": 4},
                "loops": [{"role": "outer", "entities": [{
                    "id": "curve", "type": "b_spline", "order": 4,
                    "periodic": True, "control_points": controls,
                    "knots": [float(index) for index in range(12)]}]}],
            }
            Path(vector_path).write_text(
                json.dumps(payload), encoding="utf-8")
            result = automation.commit_vector_analysis(
                {"success": True, "data": {
                    "validation_pass": True, "confidence": 1.0,
                    "vector_json": vector_path}},
                sketch_name="RejectComplexity",
                commit={"mode": "force_commit",
                        "acknowledge_validation_failure": True})
            self.assertFalse(result["success"])
            self.assertEqual(result["data"]["error"]["code"],
                             "IMAGE_LOW_CONFIDENCE")
            self.assertIn("complexity budget", result["message"])

    def test_async_job_watchdog_does_not_break_fast_job(self):
        manager = JobManager(use_isolated_watchdog=False)
        with patch(
                "solidworks_mcp.automation.com_utils.detect_modal_dialog",
                return_value={"modal": False}), patch(
                "solidworks_mcp.automation.jobs."
                "capture_ui_problem_screenshot") as capture:
            job = manager.submit("result = 2 + 2", lambda: {},
                                 watchdog={"max_runtime_sec": 5})
            manager.wait(job.id, 3)
            job.watchdog_thread.join(1)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.result, "4")
        capture.assert_not_called()

    def test_async_watchdog_preflight_runs_before_com_worker(self):
        manager = JobManager(use_isolated_watchdog=False)
        order = []

        def context_factory():
            order.append("worker")
            return {}

        def detect(*args, **kwargs):
            order.append("watchdog")
            return {"modal": False, "window_details": []}

        with patch(
                "solidworks_mcp.automation.com_utils.detect_modal_dialog",
                side_effect=detect):
            job = manager.submit("result = 1", context_factory,
                                 watchdog={"max_runtime_sec": 5})
            manager.wait(job.id, 2)
            job.watchdog_thread.join(1)
        self.assertEqual(job.status, "done")
        self.assertEqual(order[0], "watchdog")
        self.assertIn("worker", order)

    def test_isolated_watchdog_package_root_contains_module(self):
        import solidworks_mcp.automation.jobs as jobs_module
        package_root = Path(jobs_module.__file__).resolve().parents[2]
        self.assertTrue((package_root / "solidworks_mcp" / "automation" /
                         "ui_watchdog_worker.py").is_file())

    def test_async_job_records_actual_com_call_start(self):
        manager = JobManager(use_isolated_watchdog=False)
        job = manager.submit(
            'result = com_get(None, "AddHorizontalDimension2")',
            lambda: {"com_get": lambda obj, name: 42}, watchdog=False)
        manager.wait(job.id, 2)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.result, "42")
        self.assertEqual(job.last_com_method, "AddHorizontalDimension2")
        self.assertIsNotNone(job.last_com_started_at)
        self.assertGreaterEqual(job.last_com_started_at, job.started_at)

    def test_async_watchdog_records_known_dialog_recovery(self):
        manager = JobManager(use_isolated_watchdog=False)
        modal = {
            "modal": True, "main_window_hwnd": 101,
            "window_details": [{
                "hwnd": 202, "title": "Modify",
                "classification": "dimension_modify",
                "text": "D31@Front_Limb_Centerlines\n9.80 mm",
            }],
        }
        ready = {"modal": False, "window_details": []}
        fast_modal = dict(modal)
        fast_modal.update({"inspection_level": "basic",
                           "detected_at": time.time()})
        detections = [fast_modal, modal]

        def detect(*args, **kwargs):
            return detections.pop(0) if detections else ready

        with patch(
                "solidworks_mcp.automation.com_utils.detect_modal_dialog",
                side_effect=detect), patch(
                "solidworks_mcp.automation.com_utils.resolve_known_dialog",
                return_value={"resolved": True,
                              "button": "OK"}) as resolve, patch(
                "solidworks_mcp.automation.jobs."
                "capture_ui_problem_screenshot",
                return_value={"captured": True,
                              "path": "ui-problem.png"}) as capture:
            job = manager.submit(
                "import time; time.sleep(0.25); result = 'verified'",
                lambda: {}, watchdog={
                    "interval_sec": 0.1,
                    "max_runtime_sec": 5,
                    "auto_resolve_known": ["dimension_modify"],
                    "expected_dialog_text": "D31@",
                    "capture_screenshot": True,
                    "caused_by": {
                        "tool": "add_dimensions_batch",
                        "com_method": "AddHorizontalDimension2",
                        "dimension_request_id": "left_leg_x_3",
                    },
                })
            manager.wait(job.id, 2)
            job.watchdog_thread.join(1)

        self.assertEqual(job.status, "done")
        self.assertEqual(job.result, "verified")
        self.assertEqual(job.watchdog["state"], "UI_RECOVERED")
        event = job.watchdog["last_event"]
        self.assertEqual(event["code"], "UI_RECOVERED")
        self.assertLessEqual(event["detected_within_ms"], 500)
        self.assertLessEqual(event["recovered_within_ms"], 500)
        self.assertIn("detection_reference", event)
        self.assertEqual(event["caused_by"]["com_method"],
                         "AddHorizontalDimension2")
        self.assertEqual(event["screenshot"], "ui-problem.png")
        resolve.assert_called_once()
        capture.assert_called_once()

    def test_async_watchdog_waits_for_dialog_details_race(self):
        manager = JobManager(use_isolated_watchdog=False)
        empty = {"modal": True, "main_window_hwnd": 101,
                 "window_details": []}
        known = {
            "modal": True, "main_window_hwnd": 101,
            "window_details": [{
                "hwnd": 202, "title": "Modify",
                "classification": "dimension_modify",
                "text": "D1@Watchdog_Sketch\n20.00 mm",
            }],
        }
        ready = {"modal": False, "window_details": []}
        detections = [empty, known]

        def detect(*args, **kwargs):
            return detections.pop(0) if detections else ready

        with patch(
                "solidworks_mcp.automation.com_utils.detect_modal_dialog",
                side_effect=detect), patch(
                "solidworks_mcp.automation.com_utils.resolve_known_dialog",
                return_value={"resolved": True, "button": "ENTER"}) as resolve, patch(
                "solidworks_mcp.automation.jobs.capture_ui_problem_screenshot",
                return_value={"captured": True, "path": "race.png"}):
            job = manager.submit(
                "import time; time.sleep(0.45); result = 'continued'",
                lambda: {}, watchdog={
                    "interval_sec": 0.1,
                    "max_runtime_sec": 5,
                    "auto_resolve_known": ["dimension_modify"],
                    "expected_dialog_text": "Watchdog_Sketch",
                })
            manager.wait(job.id, 2)
            job.watchdog_thread.join(1)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.result, "continued")
        self.assertEqual(job.watchdog["state"], "UI_RECOVERED")
        resolve.assert_called_once()

    def test_isolated_watchdog_waits_for_dialog_details_race(self):
        ready = {"modal": False, "window_details": []}
        empty = {"modal": True, "main_window_hwnd": 101,
                 "window_details": []}
        known = {
            "modal": True, "main_window_hwnd": 101,
            "window_details": [{
                "hwnd": 202, "title": "Modify", "class": "#32770",
                "classification": "dimension_modify",
                "text": "D1@Watchdog_Sketch\n20.00 mm",
                "context_text": "[Watchdog_Sketch of acceptance.SLDPRT]",
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            event_path = root / "event.json"
            config_path.write_text(json.dumps({
                "job_id": "isolated-race",
                "interval_sec": 0.01,
                "max_runtime_sec": 2,
                "dialog_detail_wait_sec": 0.2,
                "auto_resolve_known": ["dimension_modify"],
                "expected_dialog_text": "Watchdog_Sketch",
                "capture_screenshot": True,
                "screenshot_path": str(root / "race.png"),
                "caused_by": {"com_method": "AddHorizontalDimension2"},
                "ready_path": str(root / "ready.json"),
                "event_path": str(event_path),
                "stop_path": str(root / "stop"),
                "com_state_path": str(root / "com-state.json"),
            }), encoding="utf-8")
            (root / "com-state.json").write_text(json.dumps({
                "com_method": "AddHorizontalDimension2",
                "started_at": time.time(),
            }), encoding="utf-8")
            detections = [ready, empty, empty, known]
            with patch.object(
                    ui_watchdog_worker, "detect_modal_dialog",
                    side_effect=detections), patch.object(
                    ui_watchdog_worker, "resolve_known_dialog",
                    return_value={"resolved": True,
                                  "method": "dialog_enter"}) as resolve, patch.object(
                    ui_watchdog_worker, "capture_ui_problem_screenshot",
                    return_value={"captured": True, "path": "race.png",
                                  "_deferred_image": object()}), patch.object(
                    ui_watchdog_worker, "persist_ui_problem_screenshot",
                    return_value={"captured": True, "path": "race.png"}):
                exit_code = ui_watchdog_worker.run(str(config_path))
            event = json.loads(event_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(event["state"], "UI_RECOVERED")
        self.assertTrue(event["event"]["causal_identity_match"])
        resolve.assert_called_once()

    def test_watchdog_non_deferred_capture_persists_png(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "unknown-dialog.png"
            frame = Image.new("RGB", (64, 32), "white")
            with patch("win32gui.ShowWindow"), patch(
                    "win32gui.BringWindowToTop"), patch(
                    "win32gui.GetWindowRect",
                    return_value=(0, 0, 64, 32)), patch(
                    "PIL.ImageGrab.grab", return_value=frame), patch(
                    "solidworks_mcp.automation.jobs.time.sleep"):
                capture = capture_ui_problem_screenshot(
                    {"main_window_hwnd": 101, "window_details": []},
                    "persist-test", str(target), defer_save=False)
            self.assertTrue(capture["captured"])
            self.assertTrue(target.is_file())
            self.assertGreater(capture["size_bytes"], 0)
            self.assertNotIn("_deferred_image", capture)

    def test_async_watchdog_recovers_before_deferred_png_persistence(self):
        manager = JobManager(use_isolated_watchdog=False)
        modal = {
            "modal": True, "main_window_hwnd": 101,
            "window_details": [{
                "hwnd": 202, "title": "Modify",
                "classification": "dimension_modify",
                "text": "D1@Watchdog_Sketch\n20.00 mm",
            }],
        }
        ready = {"modal": False, "window_details": []}
        detections = [modal]
        order = []

        def detect(*args, **kwargs):
            return detections.pop(0) if detections else ready

        def capture(*args, **kwargs):
            self.assertTrue(kwargs["defer_save"])
            order.append("capture")
            return {"captured": True, "path": "deferred.png",
                    "_deferred_image": object()}

        def resolve(*args, **kwargs):
            order.append("resolve")
            return {"resolved": True, "button": "ENTER"}

        def persist(value):
            order.append("persist")
            return {"captured": True, "path": value["path"]}

        with patch(
                "solidworks_mcp.automation.com_utils.detect_modal_dialog",
                side_effect=detect), patch(
                "solidworks_mcp.automation.com_utils.resolve_known_dialog",
                side_effect=resolve), patch(
                "solidworks_mcp.automation.jobs."
                "capture_ui_problem_screenshot", side_effect=capture), patch(
                "solidworks_mcp.automation.jobs."
                "persist_ui_problem_screenshot", side_effect=persist):
            job = manager.submit(
                "import time; time.sleep(0.3); result = 'continued'",
                lambda: {}, watchdog={
                    "interval_sec": 0.1,
                    "max_runtime_sec": 5,
                    "auto_resolve_known": ["dimension_modify"],
                    "expected_dialog_text": "Watchdog_Sketch",
                    "capture_screenshot": True,
                })
            manager.wait(job.id, 2)
            job.watchdog_thread.join(1)
        self.assertEqual(job.status, "done")
        self.assertEqual(order, ["capture", "resolve", "persist"])

    def test_async_watchdog_never_resolves_unknown_dialog(self):
        manager = JobManager(use_isolated_watchdog=False)
        unknown = {
            "modal": True, "main_window_hwnd": 101,
            "window_details": [{
                "hwnd": 303, "title": "Delete confirmation",
                "classification": "unknown", "text": "Delete file?",
            }],
        }
        with patch(
                "solidworks_mcp.automation.com_utils.detect_modal_dialog",
                return_value=unknown), patch(
                "solidworks_mcp.automation.com_utils."
                "resolve_known_dialog") as resolve, patch(
                    "solidworks_mcp.automation.jobs."
                    "capture_ui_problem_screenshot",
                    return_value={"captured": True,
                                  "path": "unknown.png"}):
            job = manager.submit(
                "import time; time.sleep(0.2)", lambda: {}, watchdog={
                    "interval_sec": 0.1,
                    "auto_resolve_known": ["dimension_modify"],
                    "expected_dialog_text": "D1@",
                    "capture_screenshot": True,
                })
            manager.wait(job.id, 1)
            job.watchdog_thread.join(1)

        self.assertEqual(job.status, "blocked")
        self.assertEqual(job.watchdog["state"], "UI_RECOVERY_FAILED")
        event = job.watchdog["last_event"]
        self.assertEqual(event["code"], "MODAL_DIALOG_BLOCKING")
        self.assertFalse(event["auto_recovery_attempted"])
        self.assertEqual(event["dialog"]["classification"], "unknown")
        resolve.assert_not_called()

    def test_async_watchdog_returns_bounded_timeout(self):
        manager = JobManager(use_isolated_watchdog=False)
        with patch(
                "solidworks_mcp.automation.com_utils.detect_modal_dialog",
                return_value={"modal": False}):
            job = manager.submit(
                "import time; time.sleep(1.25)", lambda: {},
                watchdog={"interval_sec": 0.1, "max_runtime_sec": 1})
            manager.wait(job.id, 2)
            job.watchdog_thread.join(1)
        self.assertEqual(job.status, "timeout")
        self.assertEqual(job.watchdog["state"], "TIMEOUT")
        self.assertEqual(job.watchdog["last_event"]["code"],
                         "BUDGET_EXCEEDED")

    def test_blocked_and_timeout_job_results_are_failures(self):
        manager = JobManager(use_isolated_watchdog=False)
        job = manager.submit("result = 4", lambda: {}, watchdog=False)
        manager.wait(job.id, 1)
        for status in ("blocked", "timeout"):
            with self.subTest(status=status):
                job.status = status
                job.error = f"structured {status} error"
                with patch("solidworks_mcp.server.job_manager.wait",
                           return_value=job):
                    result = _get_job_result(job.id)
                self.assertFalse(result["success"])
                self.assertIn(job.error, result["message"])

    def test_tool_schemas_are_unique_and_augmented(self):
        tools = asyncio.run(list_tools())
        names = [tool.name for tool in tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 82)
        advanced = next(tool for tool in tools if tool.name == "advanced_extrude")
        self.assertIn("save_path", advanced.inputSchema["properties"])
        vector = next(tool for tool in tools if tool.name == "image_to_sketch")
        self.assertIn("calibration", vector.inputSchema["properties"])
        self.assertIn("trace", vector.inputSchema["properties"])
        self.assertIn("approximation", vector.inputSchema["properties"])
        approximation = vector.inputSchema["properties"]["approximation"]
        self.assertIn("curve_strategy", approximation["properties"])
        self.assertIn("simplification_tolerance_mm",
                      approximation["properties"])
        self.assertIn("max_total_control_points",
                      approximation["properties"])
        self.assertIn("max_control_points_per_spline",
                      approximation["properties"])
        placement = vector.inputSchema["properties"]["placement"]["properties"]
        self.assertIn("image_anchor", placement)
        trace = vector.inputSchema["properties"]["trace"]["properties"]
        self.assertIn("stroke_edge_side", trace)
        self.assertIn("line_probability_threshold", trace)
        self.assertIn("consensus_radius_px", trace)
        self.assertIn("min_branch_length_px", trace)
        self.assertIn("projection", vector.inputSchema["properties"])
        self.assertIn("require_orthographic", vector.inputSchema["properties"])
        capabilities = SolidWorksAutomation().get_capabilities()
        controls = capabilities["data"]["vectorization_controls"]
        self.assertIn("homography", controls["projection_modes"])
        self.assertIn("construction_reference", controls["output_modes"])
        self.assertEqual(controls["default_max_control_points_per_spline"], 64)

    def test_approximation_presets_and_explicit_overrides(self):
        automation = SolidWorksAutomation()
        resolved = automation._resolve_approximation(
            {}, {"preset": "ultra", "max_error_mm": 0.12,
                 "target_segment_length_mm": 2.0,
                 "max_segment_length_mm": 6.0})
        self.assertEqual(resolved["preset"], "ultra")
        self.assertEqual(resolved["max_error_mm"], 0.12)
        self.assertEqual(resolved["target_segment_length_mm"], 2.0)
        self.assertEqual(resolved["max_control_points_per_spline"], 64)
        with self.assertRaises(ValueError):
            automation._resolve_approximation(
                {}, {"min_segment_length_mm": 8,
                     "target_segment_length_mm": 4})

    def test_output_modes_enforce_geometry_and_constraint_semantics(self):
        automation = SolidWorksAutomation()
        reference = automation._resolve_approximation({}, {
            "output_mode": "reference_spline",
            "prefer": ["line", "arc"],
            "curve_strategy": "hybrid_primitives",
        })
        self.assertEqual(reference["prefer"], ["spline"])
        self.assertEqual(reference["curve_strategy"], "periodic_bspline")
        minimal = automation._resolve_approximation({}, {
            "output_mode": "minimal_parametric",
            "curve_strategy": "periodic_bspline",
        })
        self.assertEqual(minimal["curve_strategy"], "hybrid_primitives")
        entities = [{"type": "line"}, {"type": "b_spline"}]
        automation._prepare_output_entities(
            entities, "construction_reference")
        self.assertTrue(all(item["construction"] for item in entities))
        solve, validation = automation._output_commit_policy(
            "construction_reference", 20, 1, True)
        self.assertEqual(solve["mode"], "construction_reference")
        self.assertIsNone(solve["target"])
        self.assertNotIn("closed_contours", validation)
        report = automation._parameterization_report(
            entities, "construction_reference")
        self.assertEqual(report["construction_entities"], 2)
        self.assertEqual(report["primitive_entities"], 1)
        self.assertFalse(report["explicitly_locked"])
        self.assertTrue(report["auxiliary_reference"])

    def test_projection_policy_requires_explicit_safe_choice(self):
        import numpy as np

        automation = SolidWorksAutomation()
        rgb = np.full((100, 120, 3), 255, np.uint8)
        alpha = np.full((100, 120), 255, np.uint8)
        with self.assertRaisesRegex(ValueError, "require_orthographic"):
            automation._apply_projection_policy(
                rgb, alpha, "filled_silhouette", {}, True)
        _, _, trace_report, trace_matrix = (
            automation._apply_projection_policy(
                rgb, alpha, "trace_as_is", {}, False))
        self.assertEqual(trace_report["mode"], "trace_as_is")
        self.assertLess(trace_report["confidence_cap"], 0.9)
        self.assertTrue(np.allclose(trace_matrix, np.eye(3)))
        with self.assertRaisesRegex(ValueError, "inside the source image"):
            automation._apply_projection_policy(
                rgb, alpha, "filled_silhouette", {
                    "mode": "homography",
                    "source_quad_px": [[-1, 0], [119, 0],
                                       [119, 99], [0, 99]],
                    "output_size_px": [120, 100],
                }, True)

    def test_homography_rectification_is_finite_and_maps_quad(self):
        import cv2
        import numpy as np

        automation = SolidWorksAutomation()
        rgb = np.full((120, 140, 3), 255, np.uint8)
        alpha = np.full((120, 140), 255, np.uint8)
        source = [[12, 14], [124, 8], [132, 106], [7, 112]]
        rectified, rectified_alpha, report, transform = (
            automation._apply_projection_policy(
                rgb, alpha, "filled_silhouette", {
                    "mode": "homography",
                    "source_quad_px": source,
                    "output_size_px": [110, 90],
                }, True))
        self.assertEqual(rectified.shape[:2], (90, 110))
        self.assertEqual(rectified_alpha.shape, (90, 110))
        self.assertTrue(np.isfinite(transform).all())
        mapped = cv2.perspectiveTransform(
            np.asarray(source, np.float32).reshape(1, 4, 2),
            transform.astype(np.float64))[0]
        self.assertTrue(np.allclose(
            mapped, np.asarray(report["destination_quad_px"]), atol=1e-3))

    def test_region_metrics_report_area_and_perimeter_change(self):
        import cv2
        import numpy as np

        reference = np.zeros((80, 80), np.uint8)
        candidate = np.zeros_like(reference)
        cv2.rectangle(reference, (10, 10), (60, 60), 255, -1)
        cv2.rectangle(candidate, (11, 10), (60, 60), 255, -1)
        metrics = SolidWorksAutomation._mask_metrics(
            reference, candidate, 0.1)
        self.assertIn("perimeter_reference_mm", metrics)
        self.assertIn("perimeter_delta_percent", metrics)
        self.assertGreater(metrics["perimeter_reference_mm"], 0.0)

    def test_open_spline_is_never_inferred_closed_from_near_endpoints(self):
        import numpy as np
        automation = SolidWorksAutomation()
        points = np.asarray([[0, 0], [1, 0], [1, 1], [0.01, 0.01]], float)
        entity = {"type": "spline", "fit_points": points.tolist(),
                  "closed": False}
        sampled = automation._sample_fitted_entity(entity, step=0.1)
        self.assertGreater(np.linalg.norm(sampled[0] - sampled[-1]), 0.001)

    def test_component_policies_are_explicit(self):
        import numpy as np
        from solidworks_mcp.automation.deep_vectorization import _select_components
        mask = np.zeros((80, 120), dtype=bool)
        mask[5:45, 5:45] = True
        mask[20:70, 70:115] = True
        selected, report = _select_components(
            mask, {"min_area_px": 50}, "outer_silhouette")
        self.assertEqual(report["selected"], 1)
        self.assertEqual(report["discarded"], 1)
        self.assertTrue(selected[30, 80])
        selected, report = _select_components(
            mask, {"min_area_px": 50}, "all_region_boundaries")
        self.assertEqual(report["selected"], 2)
        selected, report = _select_components(
            mask, {"min_area_px": 50,
                   "positive_points_px": [[20, 20]]}, "guided_components")
        self.assertTrue(selected[20, 20])
        self.assertFalse(selected[30, 80])

    def test_topology_modes_distinguish_outer_holes_and_all_components(self):
        import cv2
        import numpy as np
        automation = SolidWorksAutomation()
        mask = np.zeros((160, 220), np.uint8)
        cv2.rectangle(mask, (10, 10), (130, 145), 255, -1)
        cv2.circle(mask, (70, 75), 20, 0, -1)
        cv2.rectangle(mask, (160, 30), (205, 90), 255, -1)
        outer = automation._extract_topology(
            mask, {"mode": "largest_external_only"}, 2)
        with_holes = automation._extract_topology(
            mask, {"mode": "largest_external_with_holes"}, 2)
        all_boundaries = automation._extract_topology(
            mask, {"mode": "all_region_boundaries"}, 2)
        self.assertEqual([item["role"] for item in outer], ["outer"])
        self.assertEqual(sum(item["role"] == "hole" for item in with_holes), 1)
        self.assertEqual(sum(item["role"] == "outer" for item in all_boundaries), 2)

    def test_topology_extraction_honors_explicit_alpha_threshold(self):
        import numpy as np
        automation = SolidWorksAutomation()
        rows, columns = np.mgrid[:120, :120]
        radius = np.sqrt((columns - 60.0) ** 2 + (rows - 60.0) ** 2)
        alpha = np.clip((42.0 - radius) / 12.0 + 0.5, 0.0, 1.0)
        field = np.round(alpha * 255.0).astype(np.uint8)
        low = automation._extract_topology(
            field, {"mode": "largest_external_only"}, 2, level=0.25)
        high = automation._extract_topology(
            field, {"mode": "largest_external_only"}, 2, level=0.75)
        self.assertEqual(len(low), 1)
        self.assertEqual(len(high), 1)
        self.assertGreater(low[0]["area_px"], high[0]["area_px"])
        low_width = max(point[0] for point in low[0]["points"]) - min(
            point[0] for point in low[0]["points"])
        high_width = max(point[0] for point in high[0]["points"]) - min(
            point[0] for point in high[0]["points"])
        self.assertGreater(low_width - high_width, 10.0)
        with self.assertRaisesRegex(ValueError, "within \\(0, 1\\)"):
            automation._extract_topology(
                field, {"mode": "largest_external_only"}, 2, level=1.0)

    def test_line_art_mode_refuses_explicit_region_backend(self):
        import cv2
        import numpy as np
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "line.png")
            image = np.full((128, 128, 3), 255, np.uint8)
            cv2.line(image, (10, 64), (118, 64), (0, 0, 0), 3)
            cv2.imwrite(path, image)
            result = automation.image_to_sketch(
                path, "LineMode",
                trace={"mode": "stroke_centerlines",
                       "backend": "sam2_vitmatte"},
                calibration={"mode": "bbox_width", "value": 10},
                commit={"mode": "analyze_only"})
            self.assertFalse(result["success"])
            self.assertEqual(result["data"]["error"]["code"],
                             "CAPABILITY_UNAVAILABLE")

    def test_line_art_graph_does_not_invent_diagonal_corner_branches(self):
        import numpy as np
        from solidworks_mcp.automation.lineart_vectorization import _graph_paths

        skeleton = np.zeros((40, 40), dtype=bool)
        for value in range(5, 30):
            skeleton[value, value] = True
        skeleton[29, 5:30] = True
        paths, report = _graph_paths(
            skeleton, min_length_px=3.0, max_paths=16)
        self.assertLessEqual(len(paths), 2)
        self.assertLessEqual(report["graph_nodes"], 3)

    def test_capability_probe_is_lightweight_and_reads_annotated_cuda_build(self):
        import inspect
        from types import SimpleNamespace
        from solidworks_mcp.automation import deep_vectorization
        from solidworks_mcp.automation import lineart_vectorization

        self.assertNotIn(
            "import torch", inspect.getsource(
                lineart_vectorization._lightweight_cuda_probe))
        self.assertNotIn(
            "scan_cache_dir", inspect.getsource(
                deep_vectorization.capability_report))
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "torch"
            package.mkdir()
            (package / "version.py").write_text(
                "cuda: Optional[str] = '12.4'\n", encoding="utf-8")
            spec = SimpleNamespace(submodule_search_locations=[str(package)])
            completed = SimpleNamespace(
                returncode=0, stdout="NVIDIA Test GPU\n")
            with patch.object(
                    lineart_vectorization.importlib.util, "find_spec",
                    return_value=spec), patch.object(
                    lineart_vectorization.subprocess, "run",
                    return_value=completed):
                report = lineart_vectorization._lightweight_cuda_probe()
        self.assertTrue(report["cuda"])
        self.assertEqual(report["torch_cuda_build"], "12.4")
        self.assertEqual(report["device"], "NVIDIA Test GPU")

    def test_topology_validation_allows_declared_open_line_art_paths(self):
        automation = SolidWorksAutomation()
        open_ends = ["edge_1.start", "edge_2.end"]
        self.assertIsNone(automation._topology_validation_message(
            open_ends, 1,
            {"require_closed": False, "closed_contours": 1}))
        self.assertIn("fully closed", automation._topology_validation_message(
            open_ends, 1,
            {"require_closed": True, "closed_contours": 1}))
        self.assertIn("Expected 2", automation._topology_validation_message(
            [], 1, {"require_closed": False, "closed_contours": 2}))

    def test_arc_export_uses_rotation_direction_not_missing_clockwise_flag(self):
        automation = FakeAutomation()
        item = automation._export_segment(
            automation.doc, FakeArcSegment(), "arc", "mm")
        self.assertEqual(item["rotation_direction"], -1)
        self.assertTrue(item["clockwise"])

    def test_relation_manager_is_used_when_legacy_accessor_is_empty(self):
        sketch = FakeSketch(FakeFeature())
        relations = [object(), object(), object()]
        sketch.RelationManager = FakeRelationManager(relations)
        self.assertEqual(
            SolidWorksAutomation()._sketch_relations(sketch), relations)

    def test_committed_arc_sampling_preserves_source_traversal(self):
        automation = SolidWorksAutomation()
        points = automation._sample_committed_entity(
            {"center": [0, 0], "start": [1, 0], "end": [0, 1]},
            {"type": "arc", "direction": -1}, step=0.05)
        self.assertLess(float(points[:, 0].min()), -0.9)
        self.assertLess(float(points[:, 1].min()), -0.9)

    def test_committed_tessellation_preserves_source_traversal(self):
        automation = SolidWorksAutomation()
        points = automation._sample_committed_entity(
            {"tessellation_points": [[2, 0], [1, 0], [0, 0]]},
            {"type": "spline",
             "fit_points": [[0, 0], [1, 0], [2, 0]]})
        self.assertEqual(points.tolist(), [[0.0, 0.0],
                                           [1.0, 0.0],
                                           [2.0, 0.0]])

    def test_verification_export_uses_curve_tessellation_contract(self):
        automation = FakeAutomation()
        curve = FakeTessCurve()
        segment = FakeSplineSegment(curve)
        with patch("solidworks_mcp.automation.parametric.typed",
                   side_effect=lambda obj, interface: obj):
            item = automation._export_segment(
                automation.doc, segment, "curve", "mm", include={
                    "spline_export_mode": "adaptive_evaluate",
                    "spline_fit_points": False,
                    "spline_chord_tolerance_mm": 0.025,
                    "source_entity": {
                        "fit_points": [[0, 0], [5, 0], [10, 0]]},
                })
        self.assertEqual(item["evaluation_points"][0], [0.0, 0.0])
        self.assertEqual(item["evaluation_points"][-1], [10.0, 0.0])
        self.assertGreater(len(curve.calls), 4)
        self.assertEqual(item["curve_evaluation"]["source"],
                         "ICurve.GetEndParams+Evaluate2")

    def test_deterministic_source_nurbs_avoids_curve_readback(self):
        automation = FakeAutomation()
        curve = FakeTessCurve()
        segment = FakeSplineSegment(curve)
        item = automation._export_segment(
            automation.doc, segment, "curve", "mm", include={
                "spline_export_mode": "deterministic_source_nurbs",
                "spline_chord_tolerance_mm": 0.025,
                "spline_endpoint_tolerance_mm": 0.002,
                "source_entity": {
                    "type": "b_spline", "order": 2,
                    "periodic": False, "closed": False,
                    "control_points": [[0.0, 0.0], [10.0, 0.0]],
                    "knots": [0.0, 0.0, 1.0, 1.0],
                },
            })
        self.assertEqual(curve.calls, [])
        self.assertEqual(item["evaluation_points"][0], [0.0, 0.0])
        self.assertEqual(item["evaluation_points"][-1], [10.0, 0.0])
        self.assertLessEqual(
            item["curve_evaluation"]["endpoint_max_error_mm"], 0.002)
        self.assertIn("ISplineParamData deterministic commit parameters",
                      item["curve_evaluation"]["source"])

    def test_deterministic_export_can_skip_segment_constraint_status(self):
        automation = FakeAutomation()
        segment = FakeEquationSplineSegment(
            (0.0, 0.0, 0.0), (0.01, 0.0, 0.0))

        def reject_constraint_status():
            raise AssertionError(
                "Geometry-only validation must not invoke the segment solver")

        segment.GetConstrainedStatus = reject_constraint_status
        item = automation._export_segment(
            automation.doc, segment, "curve", "mm", include={
                "constraint_status": False,
                "spline_export_mode": "deterministic_source_nurbs",
                "spline_chord_tolerance_mm": 0.025,
                "spline_endpoint_tolerance_mm": 0.002,
                "source_entity": {
                    "type": "b_spline", "order": 2,
                    "periodic": False, "closed": False,
                    "control_points": [[0.0, 0.0], [10.0, 0.0]],
                    "knots": [0.0, 0.0, 1.0, 1.0],
                },
            })
        self.assertEqual(item["status"], "not_evaluated")
        self.assertGreaterEqual(len(item["evaluation_points"]), 2)
        self.assertEqual(item["evaluation_points"][0], [0.0, 0.0])
        self.assertEqual(item["evaluation_points"][-1], [10.0, 0.0])

    def test_geometry_export_can_skip_sketch_constraint_status(self):
        automation = FakeAutomation()
        automation.doc.feature.Name = "StatusFreeExport"

        def reject_constraint_status():
            raise AssertionError(
                "Geometry-only export must not invoke the sketch solver")

        automation.doc.sketch.GetConstrainedStatus = reject_constraint_status
        result = automation.export_sketch_geometry(
            "StatusFreeExport", unit="mm", include={
                "constraint_status": False,
                "relations": False, "dimensions": False,
                "equations": False, "topology": False,
            }, output={"mode": "inline"})
        self.assertTrue(result["success"], result)
        sketch = result["data"]["geometry"]["sketch"]
        self.assertEqual(sketch["constraint_status"], "not_evaluated")
        self.assertTrue(sketch["constraint_status_evaluation_skipped"])

    def test_equation_spline_endpoint_readback_uses_sketch_points(self):
        automation = FakeAutomation()
        segment = FakeEquationSplineSegment(
            (0.0, 0.0, 0.0), (0.01, 0.0, 0.0))
        item = automation._export_segment(
            automation.doc, segment, "curve", "mm", include={
                "spline_export_mode": "deterministic_source_nurbs",
                "spline_chord_tolerance_mm": 0.025,
                "spline_endpoint_tolerance_mm": 0.002,
                "source_entity": {
                    "type": "b_spline", "order": 2,
                    "periodic": False, "closed": False,
                    "control_points": [[0.0, 0.0], [10.0, 0.0]],
                    "knots": [0.0, 0.0, 1.0, 1.0],
                    "commit_conversion": "batched_composite_nurbs",
                    "original_type": "line",
                },
            })
        self.assertEqual(item["start"], [0.0, 0.0])
        self.assertEqual(item["end"], [10.0, 0.0])
        self.assertEqual(item["commit_conversion"],
                         "batched_composite_nurbs")
        self.assertEqual(item["original_type"], "line")

    def test_adaptive_curve_evaluation_meets_chord_tolerance(self):
        automation = SolidWorksAutomation()
        points, diagnostics = automation._adaptive_curve_points(
            FakeParabolaCurve(), "mm", 0.025, source_fit_count=16,
            max_evaluations=2048)
        self.assertLessEqual(
            diagnostics["accepted_max_chord_error_mm"], 0.025)
        self.assertGreater(len(points), 8)
        self.assertGreater(max(point[1] for point in points), 1.9)

    def test_adaptive_curve_evaluation_honors_expired_deadline(self):
        automation = SolidWorksAutomation()
        with self.assertRaises(TimeoutError):
            automation._adaptive_curve_points(
                FakeParabolaCurve(), "mm", 0.025,
                deadline_monotonic=time.monotonic() - 1.0)

    @staticmethod
    def _packed_int_pair(low, high):
        return struct.unpack("<d", struct.pack("<ii", low, high))[0]

    def test_bulk_spline_parameter_parser_preserves_exact_geometry(self):
        automation = SolidWorksAutomation()
        payload = [
            self._packed_int_pair(3, 3),
            self._packed_int_pair(3, 0),
            0.0, 0.0, 0.0,
            0.005, 0.002, 0.0,
            0.01, 0.0, 0.0,
            0.0, 0.0, 0.0, 1.0, 1.0, 1.0,
            self._packed_int_pair(0, 0),
            self._packed_int_pair(0, 0),
            self._packed_int_pair(0, 0),
        ]
        records = automation._parse_bulk_spline_params(payload, "mm")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["degree"], 2)
        self.assertEqual(records[0]["control_points"], [
            [0.0, 0.0], [5.0, 2.0], [10.0, 0.0]])
        points, diagnostics = automation._adaptive_nurbs_points(
            records[0], 0.025)
        self.assertEqual(points[0], [0.0, 0.0])
        self.assertEqual(points[-1], [10.0, 0.0])
        self.assertGreater(max(point[1] for point in points), 0.9)
        self.assertLessEqual(
            diagnostics["accepted_max_chord_error_mm"], 0.025)
        self.assertEqual(
            diagnostics["source"],
            "ISketch.GetSplineParams3+local_adaptive_de_boor")

    def test_bulk_spline_parameter_parser_rejects_periodic_readback(self):
        automation = SolidWorksAutomation()
        payload = [
            self._packed_int_pair(3, 2),
            self._packed_int_pair(2, 1),
            0.0, 0.0, 0.0, 0.01, 0.0, 0.0,
            0.0, 0.5, 1.0,
            self._packed_int_pair(0, 0),
            self._packed_int_pair(0, 0),
            self._packed_int_pair(0, 0),
        ]
        with self.assertRaisesRegex(RuntimeError, "periodic"):
            automation._parse_bulk_spline_params(payload, "mm")

    def test_bulk_spline_parser_dehomogenizes_open_rational_controls(self):
        automation = SolidWorksAutomation()
        payload = [
            self._packed_int_pair(4, 2),
            self._packed_int_pair(2, 0),
            0.0, 0.0, 0.0, 2.0,
            0.03, 0.012, 0.0, 3.0,
            0.0, 0.0, 1.0, 1.0,
            self._packed_int_pair(0, 0),
            self._packed_int_pair(0, 0),
            self._packed_int_pair(0, 0),
        ]
        record = automation._parse_bulk_spline_params(payload, "mm")[0]
        self.assertEqual(record["control_points"], [
            [0.0, 0.0], [10.0, 4.0]])
        self.assertEqual(record["weights"], [2.0, 3.0])

    def test_bulk_spline_segment_match_is_endpoint_verified(self):
        record = {
            "nurbs": {},
            "evaluation_points": [[10.0, 0.0], [5.0, 1.0], [0.0, 0.0]],
            "curve_evaluation": {},
        }
        matched = SolidWorksAutomation._take_matching_bulk_spline(
            [record], [0.0, 0.0], [10.0, 0.0], 0.001)
        self.assertEqual(matched["evaluation_points"][0], [0.0, 0.0])
        self.assertTrue(matched["curve_evaluation"]["orientation_reversed"])
        with self.assertRaisesRegex(RuntimeError, "too few"):
            SolidWorksAutomation._take_matching_bulk_spline(
                [], [0.0, 0.0], [10.0, 0.0], 0.001)

    def test_committed_geometry_raster_reports_exact_entity_roundtrip(self):
        import numpy as np

        automation = SolidWorksAutomation()
        geometry = {"entities": [{
            "id": "edge", "type": "line", "construction": False,
            "start": [2, 2], "end": [16, 2]}]}
        loops = [{"role": "visible_edge", "closed": False,
                  "entities": [{"id": "edge", "type": "line",
                                "start": [2, 2], "end": [16, 2]}]}]
        raster, report = automation._rasterize_committed_geometry(
            geometry, loops, np.eye(3), (20, 20), line_mode=True)
        self.assertGreater(int((raster > 0).sum()), 10)
        self.assertEqual(report["sampled_entities"], 1)
        self.assertEqual(report["missing_entity_ids"], [])

    def test_committed_geometry_raster_honors_deadline(self):
        import numpy as np

        automation = SolidWorksAutomation()
        geometry = {"entities": [{
            "id": "edge", "type": "line", "construction": False,
            "start": [2, 2], "end": [16, 2]}]}
        loops = [{"role": "visible_edge", "closed": False,
                  "entities": [{"id": "edge", "type": "line",
                                "start": [2, 2], "end": [16, 2]}]}]
        with self.assertRaises(TimeoutError):
            automation._rasterize_committed_geometry(
                geometry, loops, np.eye(3), (20, 20), line_mode=True,
                deadline_monotonic=time.monotonic() - 1.0)

    def test_committed_construction_geometry_is_reverse_rasterized(self):
        import numpy as np

        automation = SolidWorksAutomation()
        geometry = {"entities": [{
            "id": "construction_edge", "type": "line", "construction": True,
            "start": [2, 2], "end": [16, 2]}]}
        loops = [{"role": "visible_edge", "closed": False,
                  "entities": [{"id": "construction_edge", "type": "line",
                                "construction": True,
                                "start": [2, 2], "end": [16, 2]}]}]
        raster, report = automation._rasterize_committed_geometry(
            geometry, loops, np.eye(3), (20, 20), line_mode=True)
        self.assertGreater(int((raster > 0).sum()), 10)
        self.assertEqual(report["sampled_entities"], 1)
        self.assertEqual(report["expected_entities"], 1)

    def test_batched_nurbs_roundtrip_preserves_original_entity_id(self):
        import numpy as np

        automation = SolidWorksAutomation()
        geometry = {"entities": [{
            "id": "source_line", "type": "spline", "construction": True,
            "commit_conversion": "batched_composite_nurbs",
            "original_type": "line",
            "evaluation_points": [[2, 2], [9, 2], [16, 2]],
            "curve_evaluation": {"orientation_reversed": False},
        }]}
        loops = [{"role": "visible_edge", "closed": False,
                  "entities": [{
                      "id": "source_line", "type": "line",
                      "construction": True,
                      "start": [2, 2], "end": [16, 2],
                  }]}]
        raster, report = automation._rasterize_committed_geometry(
            geometry, loops, np.eye(3), (20, 20), line_mode=True)
        self.assertGreater(int((raster > 0).sum()), 10)
        self.assertEqual(report["metadata_mismatches"], [])
        self.assertEqual(report["missing_entity_ids"], [])

    def test_post_commit_validation_requires_exact_entity_roundtrip(self):
        import cv2
        import numpy as np

        automation = SolidWorksAutomation()
        reference = np.zeros((20, 20), np.uint8)
        cv2.line(reference, (2, 2), (16, 2), 255, 1)
        payload = {
            "source": "input.png", "image_shape": [20, 20],
            "reference_raster": "reference.png",
            "pixel_to_sketch": np.eye(3).tolist(),
            "scale_mm_per_px": 1.0,
            "trace": {"mode": "stroke_edges"},
            "thresholds": {"min_line_support": 0.1,
                           "max_hausdorff_mm": 100.0},
            "loops": [{"role": "visible_edge", "closed": False,
                       "entities": [{"id": "edge", "type": "line",
                                     "start": [2, 2], "end": [16, 2]}]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            reference_path = os.path.join(directory, "reference.png")
            source_path = os.path.join(directory, "input.png")
            vector_path = os.path.join(directory, "candidate.vector.json")
            cv2.imwrite(reference_path, reference)
            cv2.imwrite(source_path, cv2.cvtColor(
                reference, cv2.COLOR_GRAY2BGR))
            Path(vector_path).write_text("{}", encoding="utf-8")
            payload["reference_raster"] = reference_path
            payload["source"] = source_path
            export_options = []

            def load_geometry(*args, **kwargs):
                export_options.append(kwargs.get("include"))
                return {"success": True}, {"entities": [
                    {"id": "edge", "type": "line", "construction": False,
                     "start": [2, 2], "end": [16, 2]},
                    {"id": "unexpected", "type": "line",
                     "construction": False,
                     "start": [2, 3], "end": [16, 3]},
                ]}

            automation._load_geometry_payload = load_geometry
            result = automation._validate_committed_geometry(
                "RoundtripMismatch", payload, vector_path, {})
        self.assertFalse(result["pass"])
        self.assertFalse(result["roundtrip_pass"])
        self.assertEqual(result["roundtrip"]["extra_entity_ids"],
                         ["unexpected"])
        self.assertEqual(export_options[0]["spline_export_mode"],
                         "deterministic_source_nurbs")
        self.assertFalse(export_options[0]["spline_fit_points"])

    def test_post_commit_bulk_export_failure_is_rollback_safe(self):
        import cv2
        import numpy as np

        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            reference = np.zeros((8, 8), np.uint8)
            reference_path = os.path.join(directory, "reference.png")
            source_path = os.path.join(directory, "source.png")
            vector_path = os.path.join(directory, "candidate.vector.json")
            cv2.imwrite(reference_path, reference)
            cv2.imwrite(source_path, cv2.cvtColor(
                reference, cv2.COLOR_GRAY2BGR))
            Path(vector_path).write_text("{}", encoding="utf-8")
            payload = {
                "source": source_path,
                "image_shape": [8, 8],
                "reference_raster": reference_path,
                "approximation": {"max_error_mm": 0.15},
            }

            def fail_export(*args, **kwargs):
                raise RuntimeError("malformed packed payload")

            automation._load_geometry_payload = fail_export
            result = automation._validate_committed_geometry(
                "RollbackSafe", payload, vector_path, {})
        self.assertFalse(result["pass"])
        self.assertIn("malformed packed payload", result["error"])

    def test_locked_vector_commit_is_rejected_before_com_when_over_budget(self):
        automation = SolidWorksAutomation()
        controls = [[float(index), 0.0] for index in range(100)]
        payload = {
            "schema": "solidworks-mcp/image-vector/v1",
            "validation_pass": True, "confidence": 1.0,
            "approximation": {
                "max_entities": 10, "max_total_control_points": 200,
                "max_control_points_per_spline": 128,
                "output_mode": "locked_trace"},
            "loops": [{"role": "outer", "entities": [{
                "id": "curve", "type": "b_spline", "order": 4,
                "periodic": True, "control_points": controls,
                "knots": [float(index) for index in range(101)]}]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            vector_path = os.path.join(directory, "budget.vector.json")
            Path(vector_path).write_text(json.dumps(payload), encoding="utf-8")
            called = []
            automation.create_parametric_sketch = lambda **kwargs: called.append(
                kwargs)
            result = automation.commit_vector_analysis(
                {"success": True, "data": {
                    "validation_pass": True, "confidence": 1.0,
                    "vector_json": vector_path}},
                sketch_name="BudgetGuard", budget={"max_elapsed_sec": 30})
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error"]["code"],
                         "BUDGET_EXCEEDED")
        self.assertFalse(result["data"]["error"]["details"][
            "mutation_started"])
        self.assertEqual(called, [])
        self.assertEqual(automation._runtime.metrics["budget_exceeded"], 1)

    def test_commit_rejects_oversized_equation_spline_before_com(self):
        automation = SolidWorksAutomation()
        controls = [[float(index), 0.0] for index in range(100)]
        payload = {
            "schema": "solidworks-mcp/image-vector/v1",
            "validation_pass": True, "confidence": 1.0,
            "approximation": {
                "max_entities": 10, "max_total_control_points": 200,
                "max_control_points_per_spline": 64,
                "output_mode": "construction_reference"},
            "loops": [{"role": "outer", "entities": [{
                "id": "curve", "type": "b_spline", "order": 4,
                "periodic": True, "control_points": controls,
                "knots": [float(index) for index in range(101)]}]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            vector_path = os.path.join(directory, "oversized.vector.json")
            Path(vector_path).write_text(json.dumps(payload), encoding="utf-8")
            called = []
            automation.create_parametric_sketch = lambda **kwargs: called.append(
                kwargs)
            result = automation.commit_vector_analysis(
                {"success": True, "data": {
                    "validation_pass": True, "confidence": 1.0,
                    "vector_json": vector_path}},
                sketch_name="OversizedSpline", budget={"max_elapsed_sec": 900})
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error"]["code"],
                         "IMAGE_LOW_CONFIDENCE")
        details = result["data"]["error"]["details"]
        self.assertEqual(details["oversized_splines"][0]["control_points"], 100)
        self.assertEqual(called, [])

    def test_cad_cost_model_penalizes_one_large_spline(self):
        automation = SolidWorksAutomation()
        one = [{"type": "b_spline",
                "control_points": [[float(index), 0.0]
                                   for index in range(372)]}]
        split = [{"type": "b_spline",
                  "control_points": [[float(index), 0.0]
                                     for index in range(8)]}
                 for _ in range(46)]
        one_cost = automation._estimate_vector_commit_seconds(
            one, "construction_reference")
        split_cost = automation._estimate_vector_commit_seconds(
            split, "construction_reference")
        self.assertGreater(one_cost, 580)
        self.assertLess(split_cost, 50)
        self.assertGreater(one_cost, split_cost * 10)
        profile = automation._cad_commit_profile(
            split, "construction_reference")
        self.assertGreater(profile["verification_estimated_sec"], 0)
        self.assertEqual(
            profile["estimated_sec"],
            round(profile["creation_estimated_sec"] +
                  profile["verification_estimated_sec"], 3))

    def test_auto_fit_uses_cad_safe_candidate_when_periodic_is_oversized(self):
        import numpy as np

        automation = SolidWorksAutomation()
        hybrid = [{
            "type": "b_spline", "order": 4, "periodic": False,
            "control_points": [[float(index), 0.0]
                               for index in range(20)],
            "knots": [0.0] * 4 + [float(index) for index in range(1, 17)] +
                     [17.0] * 4,
            "fit_error_mm": 0.1,
        }]
        periodic = {
            "type": "b_spline", "order": 4, "periodic": True,
            "control_points": [[float(index), 0.0]
                               for index in range(100)],
            "knots": [float(index) for index in range(101)],
            "fit_error_mm": 0.1,
        }
        points = np.column_stack([
            np.cos(np.linspace(0, 2 * math.pi, 100, endpoint=False)),
            np.sin(np.linspace(0, 2 * math.pi, 100, endpoint=False))])
        with patch.object(
                automation, "_fit_loop_hybrid_primitives",
                return_value=(hybrid, 0.1)), patch.object(
                automation, "_fit_periodic_bspline",
                return_value=(periodic, {"reason": "accepted"})):
            entities, error = automation._fit_loop_hybrid(
                points, 0.15, ["spline"], 20,
                approximation={
                    "curve_strategy": "auto",
                    "output_mode": "construction_reference",
                    "simplification_tolerance_mm": 0.15,
                    "max_total_fit_points": 1000,
                    "max_total_control_points": 200,
                    "max_control_points_per_spline": 64,
                    "entity_complexity_weight": 24.0,
                })
        self.assertEqual(entities[0]["type"], "b_spline")
        self.assertEqual(entities[0]["curve_strategy"], "hybrid_primitives")
        self.assertEqual(error, 0.1)

    def test_construction_reference_has_no_topology_relations(self):
        automation = SolidWorksAutomation()
        loops = [{"closed": True, "entities": [
            {"id": "curve", "type": "spline"},
            {"id": "edge", "type": "line"},
            {"id": "corner", "type": "arc"},
        ]}]
        self.assertEqual(automation._topology_constraints(
            loops, "construction_reference"), [])
        editable = automation._topology_constraints(
            loops, "minimal_parametric")
        self.assertEqual(len(editable), 3)
        self.assertEqual(editable[0]["entities"],
                         ["curve.end", "edge.start"])

    def test_rollback_refuses_delete_while_created_sketch_stays_active(self):
        automation = FakeAutomation()
        automation.doc.feature.Name = "ActiveFailure"
        automation.doc.SketchManager.InsertSketch = lambda update: True
        deleted = []
        automation.delete_feature = lambda *args, **kwargs: deleted.append(args)
        self.assertFalse(automation._rollback_created_sketch(
            automation.doc.feature, "ActiveFailure"))
        self.assertEqual(deleted, [])

    def test_locked_trace_fixes_sliding_endpoints_without_redundancy(self):
        automation = SolidWorksAutomation()
        left_start = FakePoint(0.0, 0.0)
        shared_left = FakePoint(1.0, 0.0)
        shared_right = FakePoint(1.0, 0.0)
        right_end = FakePoint(2.0, 0.0)
        records = {
            "left": {"type": "line",
                     "points": {"start": left_start, "end": shared_left}},
            "right": {"type": "arc",
                      "points": {"start": shared_right, "end": right_end}},
            "curve": {"type": "b_spline", "points": {}},
        }
        constraints = [{
            "type": "coincident",
            "entities": ["left.end", "right.start"],
        }]
        generated = automation._locked_trace_constraints(
            records, constraints)
        fixed = [item["entities"][0] for item in generated]
        self.assertEqual(set(fixed[:3]), {"left", "right", "curve"})
        self.assertIn("left.start", fixed)
        self.assertIn("right.end", fixed)
        self.assertEqual(
            len(set(fixed) & {"left.end", "right.start"}), 1)
        self.assertEqual(len(fixed), 6)

    def test_locked_trace_tolerates_a_redundant_auto_fix(self):
        automation = FakeAutomation()
        calls = 0
        original = automation.doc.SketchAddConstraints

        def reject_last_fix(code):
            nonlocal calls
            calls += 1
            if calls == 3:
                return False
            return original(code)

        automation.doc.SketchAddConstraints = reject_last_fix
        result = automation.create_parametric_sketch(
            name="LockedLineWithSharedPoint", plane="Front", unit="mm",
            entities=[{"id": "line", "type": "line",
                       "start": [0, 0], "end": [10, 2]}],
            constraints=[], dimensions=[], equations=[],
            solve={"mode": "locked_trace", "target": "fully_defined"},
            validation={}, transaction={"rollback_on_failure": True},
            output_mode="locked_trace")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["relations_created"], 2)
        self.assertEqual(
            result["data"]["locked_trace_relations_skipped"], 1)

    def test_outer_vector_error_uses_verified_nested_rollback_state(self):
        automation = SolidWorksAutomation()
        self.assertFalse(automation._commit_document_restored({
            "data": {"error": {"document_restored": False}}}))
        self.assertTrue(automation._commit_document_restored({
            "data": {"error": {"document_restored": True}}}))

    def test_locked_trace_relations_are_added_after_direct_db_mode(self):
        automation = FakeAutomation()
        add_to_db_states = []
        original = automation.doc.SketchAddConstraints

        def record_state(code):
            add_to_db_states.append(automation.doc.SketchManager.AddToDB)
            return original(code)

        automation.doc.SketchAddConstraints = record_state
        result = automation.create_parametric_sketch(
            name="LockedLine", plane="Front", unit="mm",
            entities=[{"id": "line", "type": "line",
                       "start": [0, 0], "end": [10, 2]}],
            constraints=[], dimensions=[], equations=[],
            solve={"mode": "locked_trace", "target": "fully_defined"},
            validation={}, transaction={"rollback_on_failure": True},
            output_mode="locked_trace")
        self.assertTrue(result["success"], result)
        self.assertEqual(len(add_to_db_states), 3)
        self.assertFalse(any(add_to_db_states))

    def test_stroke_edges_require_explicit_side_selection(self):
        import numpy as np
        from solidworks_mcp.automation.lineart_vectorization import (
            vectorize_line_art)

        image = np.full((80, 80, 3), 255, dtype=np.uint8)
        with patch(
                "solidworks_mcp.automation.lineart_vectorization._load_models",
                return_value=object()), patch(
                "solidworks_mcp.automation.lineart_vectorization._model_consensus",
                return_value=(np.zeros((80, 80), dtype=bool), {
                    "dexined_threshold": 0.5, "teed_threshold": 0.5,
                    "consensus_radius_px": 2.0, "dexined_support": 1.0,
                    "teed_support": 1.0, "balanced_support": 1.0,
                    "distance_median_px": 0.0, "distance_p95_px": 0.0,
                    "edge_pixels": 1,
                })):
            with self.assertRaisesRegex(ValueError, "stroke_edge_side"):
                vectorize_line_art(image, trace={"mode": "stroke_edges"})

    def test_vector_preview_is_offline_and_non_mutating(self):
        self.assertFalse(_is_effectively_mutating(
            "image_to_sketch", {"commit": {"mode": "analyze_only"}}))
        self.assertFalse(_is_effectively_mutating(
            "image_to_sketch", {"commit": {"mode": "preview"}}))
        self.assertTrue(_is_effectively_mutating(
            "image_to_sketch", {"commit": {"mode": "commit_if_confident"}}))

    def test_native_ellipse_entity_uses_metric_axis_points(self):
        automation = SolidWorksAutomation()

        class EllipseManager:
            def __init__(self):
                self.arguments = None

            def CreateEllipse(self, *arguments):
                self.arguments = arguments
                return object()

        manager = EllipseManager()
        document = type("EllipseDocument", (), {"SketchManager": manager})()
        result = automation._create_entity(document, {
            "id": "ellipse", "type": "ellipse", "center": [1.0, 2.0],
            "major_radius": 10.0, "minor_radius": 4.0,
            "rotation_deg": 0.0,
        }, "mm")
        self.assertIsNotNone(result)
        self.assertEqual(manager.arguments[:3], (0.001, 0.002, 0.0))
        self.assertEqual(manager.arguments[3:6], (0.011, 0.002, 0.0))
        self.assertEqual(manager.arguments[6:9], (0.001, 0.006, 0.0))

    def test_revolved_body_names_and_verifies_inside_transaction(self):
        automation = SolidWorksAutomation()
        captured = {}
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "none", data={"bodies": []})

        def run_transaction(**kwargs):
            captured.update(kwargs)
            return automation._result(True, "committed", data={"steps": [
                {"data": {"operation": "create_parametric_sketch",
                          "sketch_name": "Profile"}},
                {"data": {"operation": "revolve_boss",
                          "feature_name": "Body_Revolve"}},
                {"data": {"operation": "rename_new_body"}},
                {"data": {"operation": "verify_named_body",
                          "body_name": "Body"}},
            ]})

        automation.run_transaction = run_transaction
        result = automation.create_revolved_body(
            sketch={"name": "Profile", "unit": "mm"},
            revolve={"angle": 360}, body_name="Body")
        self.assertTrue(result["success"], result)
        operations = captured["operations"]
        self.assertFalse(operations[1]["args"]["merge"])
        self.assertEqual([item["op"] for item in operations], [
            "create_parametric_sketch", "revolve_boss",
            "rename_new_body", "verify_named_body"])
        self.assertEqual(captured["invariants"]["solid_body_count"], 1)
        self.assertEqual(captured["invariants"]["required_bodies"], ["Body"])

    def test_revolved_body_rejects_merge(self):
        automation = SolidWorksAutomation()
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "none", data={"bodies": []})
        result = automation.create_revolved_body(
            sketch={"name": "Profile"}, revolve={"merge": True},
            body_name="Body")
        self.assertFalse(result["success"])
        self.assertIn("merge=false", result["message"])

    def test_elliptical_sweep_builds_atomic_profile_and_quality_plan(self):
        automation = SolidWorksAutomation()
        captured = {}
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "none", data={"bodies": []})

        def run_transaction(**kwargs):
            captured.update(kwargs)
            steps = [{"data": {"operation": item["op"]}}
                     for item in kwargs["operations"]]
            return automation._result(True, "committed", data={"steps": steps})

        automation.run_transaction = run_transaction
        result = automation.create_swept_member(
            path={"name": "Path", "plane": "Front", "entities": [
                {"id": "path", "type": "line",
                 "start": [0.0, 0.0], "end": [0.0, 20.0]}]},
            profile={"type": "ellipse", "plane": "Top",
                     "major_radius": 4.0, "minor_radius": 2.0},
            body_name="EllipticalTube", unit="mm")
        self.assertTrue(result["success"], result)
        operations = captured["operations"]
        self.assertEqual([item["op"] for item in operations], [
            "create_parametric_sketch", "create_parametric_sketch",
            "create_sweep_feature"])
        ellipse = operations[1]["args"]["entities"][0]
        self.assertEqual(ellipse["type"], "ellipse")
        self.assertAlmostEqual(
            result["data"]["path_quality"]["required_min_bend_radius"], 4.2)
        self.assertEqual(operations[2]["args"]["profile_type"], "ellipse")
        self.assertEqual(result["data"]["path_validation_source"],
                         "declared_geometry")
        self.assertEqual(result["data"]["profile_validation_source"],
                         "create_parametric_sketch")
        self.assertEqual(captured["invariants"]["solid_body_count"], 1)

    def test_custom_sweep_requires_curvature_scale(self):
        automation = SolidWorksAutomation()
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "none", data={"bodies": []})
        result = automation.create_swept_member(
            path_sketch="Path",
            profile={"type": "custom", "sketch_name": "Profile"},
            body_name="Tube")
        self.assertFalse(result["success"])
        self.assertIn("min_bend_radius", result["message"])

    def test_declared_sweep_path_is_rejected_before_transaction(self):
        automation = SolidWorksAutomation()
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "none", data={"bodies": []})
        called = []
        automation.run_transaction = lambda **kwargs: called.append(kwargs)
        result = automation.create_swept_member(
            path={"name": "Crossing", "plane": "Front", "entities": [
                {"id": "a", "type": "line", "start": [0, 0],
                 "end": [10, 10]},
                {"id": "b", "type": "line", "start": [0, 10],
                 "end": [10, 0]}]},
            profile={"type": "circle", "plane": "Top", "diameter": 2},
            body_name="Tube", unit="mm")
        self.assertFalse(result["success"])
        self.assertEqual(called, [])
        self.assertIn("quality gate", result["message"])

    def test_declared_sweep_requires_contact_with_profile_plane(self):
        automation = SolidWorksAutomation()
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "none", data={"bodies": []})
        result = automation.create_swept_member(
            path={"name": "Detached", "plane": "Front", "entities": [
                {"id": "path", "type": "line", "start": [0, 5],
                 "end": [0, 20]}]},
            profile={"type": "circle", "plane": "Top", "diameter": 2},
            body_name="Tube", unit="mm")
        self.assertFalse(result["success"])
        self.assertIn("does not touch", result["message"])

    def test_sweep_path_quality_rejects_crossing_and_sharp_corner(self):
        automation = SolidWorksAutomation()
        crossing = {
            "entities": [
                {"id": "a", "type": "line", "start": [0, 0],
                 "end": [10, 10]},
                {"id": "b", "type": "line", "start": [0, 10],
                 "end": [10, 0]}],
            "contours": [{"id": "one"}, {"id": "two"}]}
        automation._load_geometry_payload = lambda *args, **kwargs: (
            {"success": True}, crossing)
        _, quality = automation._path_quality("Crossing", 1.0, "mm")
        self.assertFalse(quality["pass"])
        self.assertFalse(quality["is_simple"])

        corner = {
            "entities": [
                {"id": "a", "type": "line", "start": [0, 0],
                 "end": [10, 0]},
                {"id": "b", "type": "line", "start": [10, 0],
                 "end": [10, 10]}],
            "contours": [{"id": "one", "entities": ["a", "b"]}]}
        automation._load_geometry_payload = lambda *args, **kwargs: (
            {"success": True}, corner)
        _, quality = automation._path_quality("Corner", 1.0, "mm")
        self.assertFalse(quality["pass"])
        self.assertEqual(quality["sharp_corner_count"], 1)
        _, allowed = automation._path_quality(
            "Corner", 1.0, "mm", allow_sharp_corners=True)
        self.assertTrue(allowed["pass"])

    def test_sweep_path_quality_is_dependency_free_and_topology_safe(self):
        automation = SolidWorksAutomation()
        original_import = __import__

        def guarded_import(name, *args, **kwargs):
            if name.startswith("shapely"):
                raise AssertionError("Sweep gate must not import Shapely")
            return original_import(name, *args, **kwargs)

        straight = {"entities": [
            {"id": "path", "type": "line",
             "start": [30, 0], "end": [30, 35]}]}
        with patch("builtins.__import__", side_effect=guarded_import):
            quality = automation._path_quality_from_geometry(
                straight, 4.2, False)
        self.assertTrue(quality["pass"], quality)
        self.assertEqual(quality["quality_engine"], "native_segment_grid")
        self.assertEqual(quality["intersection_count"], 0)

        crossing = {"entities": [
            {"id": "a", "type": "line",
             "start": [0, 0], "end": [10, 10]},
            {"id": "b", "type": "line",
             "start": [0, 10], "end": [10, 0]}]}
        quality = automation._path_quality_from_geometry(
            crossing, 1.0, True)
        self.assertFalse(quality["pass"])
        self.assertGreaterEqual(quality["intersection_count"], 1)

        shared_chain = {"entities": [
            {"id": "a", "type": "line",
             "start": [0, 0], "end": [10, 0]},
            {"id": "b", "type": "line",
             "start": [10, 0], "end": [20, 0]}]}
        quality = automation._path_quality_from_geometry(
            shared_chain, 1.0, True)
        self.assertTrue(quality["pass"], quality)

        overlap = {"entities": [
            {"id": "a", "type": "line",
             "start": [0, 0], "end": [10, 0]},
            {"id": "b", "type": "line",
             "start": [5, 0], "end": [15, 0]}]}
        quality = automation._path_quality_from_geometry(
            overlap, 1.0, True)
        self.assertFalse(quality["pass"])

    def test_sweep_path_quality_checks_numeric_spline_curvature(self):
        automation = SolidWorksAutomation()
        geometry = {
            "entities": [{"id": "curve", "type": "spline",
                          "fit_points": [[-1, 0], [0, 1], [1, 0]]}],
            "contours": [{"id": "one", "entities": ["curve"]}]}
        automation._load_geometry_payload = lambda *args, **kwargs: (
            {"success": True}, geometry)
        _, quality = automation._path_quality("Curve", 1.1, "mm")
        self.assertFalse(quality["pass"])
        self.assertTrue(quality["spline_curvature_checked"])
        self.assertAlmostEqual(quality["minimum_spline_radius_estimate"], 1.0)

    def test_sweep_profile_accepts_full_circle_arc_readback(self):
        automation = SolidWorksAutomation()
        geometry = {
            "entities": [{"id": "circle", "type": "arc",
                          "construction": False,
                          "center": [0.0, 0.0], "radius": 3.0,
                          "start": [3.0, 0.0], "end": [3.0, 0.0]}],
            "contours": [{"id": "contour_001", "entities": ["circle"],
                          "closed": False}],
        }
        automation._load_geometry_payload = lambda *args, **kwargs: (
            {"success": True}, geometry)
        result = automation.validate_sweep_profile("Circle", unit="mm")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["closed_contours"], 1)

    def test_sweep_feature_uses_official_profile_and_path_marks(self):
        automation = SolidWorksAutomation()

        class SelectableFeature:
            def __init__(self, sketch=None):
                self.calls = []
                self.sketch = sketch

            def Select2(self, append, mark):
                self.calls.append((append, mark))
                return True

            def GetSpecificFeature2(self):
                return self.sketch

        class SelectableSegment:
            ConstructionGeometry = False

            def __init__(self):
                self.calls = []

            def Select4(self, append, selection_data):
                self.calls.append((append, selection_data.Mark))
                return True

        class PathSketch:
            def __init__(self, segments):
                self.segments = segments

            def GetSketchSegments(self):
                return self.segments

        class SweepFeature:
            Name = "Sweep1"

            def GetFaces(self):
                return [object()]

        class SweepDefinition:
            def __init__(self):
                self.wall_thickness = []
                self.twist_angle = None

            def SetWallThickness(self, forward, value):
                self.wall_thickness.append((forward, value))

            def SetTwistAngle(self, value):
                self.twist_angle = value

        class FeatureManager:
            def __init__(self):
                self.definition = SweepDefinition()
                self.definition_type = None

            def CreateDefinition(self, definition_type):
                self.definition_type = definition_type
                return self.definition

            def CreateFeature(self, definition):
                self.created_with = definition
                return SweepFeature()

        path_segment = SelectableSegment()
        path_feature = SelectableFeature(PathSketch([path_segment]))
        profile_feature = SelectableFeature()
        manager = FeatureManager()
        document = type("SweepDocument", (), {
            "SketchManager": type("SketchState", (), {"ActiveSketch": None})(),
            "FeatureManager": manager,
            "SelectionManager": FakeSelectionManager(),
            "ClearSelection2": lambda self, all_items: True,
        })()
        automation.get_active_doc = lambda: (document, None)
        automation.ensure_features_not_frozen = lambda doc: None
        automation._find_sketch_feature = lambda doc, name: (
            path_feature if name == "Path" else profile_feature)
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "none", data={"bodies": []})
        automation._rename_new_body = lambda before, name: automation._result(
            True, "renamed", data={"body_name": name})
        automation.verify_named_body = lambda name, unit=None: automation._result(
            True, "verified", data={"body_name": name})
        with patch("solidworks_mcp.automation.high_level.typed",
                   return_value=manager):
            result = automation.create_sweep_feature(
                "Path", "custom", "Tube", profile_sketch="Profile")
        self.assertTrue(result["success"], result)
        self.assertEqual(profile_feature.calls, [(False, 1)])
        self.assertEqual(path_feature.calls, [(True, 4)])
        self.assertEqual(path_segment.calls, [])
        self.assertFalse(manager.definition.CircularProfile)
        self.assertIs(manager.created_with, manager.definition)
        self.assertEqual(result["data"]["selection_contract"], {
            "path_mark": 4, "profile_mark": 1,
            "path_selection": "sketch_feature",
            "path_segments_selected": None,
            "profile_strategy": "materialized_sketch"})
        self.assertEqual(result["data"]["creation_api"],
                         "ISweepFeatureData+CreateFeature")

    def test_native_circular_sweep_is_rejected_before_selection(self):
        automation = SolidWorksAutomation()

        class PathSegment:
            ConstructionGeometry = False

            def __init__(self):
                self.calls = []

            def Select4(self, append, selection_data):
                self.calls.append((append, selection_data.Mark))
                return True

        segment = PathSegment()
        sketch = type("PathSketch", (), {
            "GetSketchSegments": lambda self: [segment]})()
        container_calls = []
        path_feature = type("PathFeature", (), {
            "GetSpecificFeature2": lambda self: sketch,
            "Select2": lambda self, append, mark: container_calls.append(
                (append, mark)) or True})()
        sweep_feature = type("SweepFeature", (), {
            "Name": "Sweep1", "GetFaces": lambda self: [object()]})()
        class SweepDefinition:
            def SetWallThickness(self, forward, value):
                pass

            def SetTwistAngle(self, value):
                pass

        definition = SweepDefinition()
        manager = type("FeatureManager", (), {
            "CreateDefinition": lambda self, feature_type: definition,
            "CreateFeature": lambda self, feature_data: sweep_feature})()
        document = type("SweepDocument", (), {
            "SketchManager": type("SketchState", (), {"ActiveSketch": None})(),
            "FeatureManager": manager,
            "SelectionManager": FakeSelectionManager(),
            "ClearSelection2": lambda self, all_items: True})()
        automation.get_active_doc = lambda: (document, None)
        automation.ensure_features_not_frozen = lambda doc: None
        automation._find_sketch_feature = lambda doc, name: path_feature
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "none", data={"bodies": []})
        automation._rename_new_body = lambda before, name: automation._result(
            True, "renamed", data={"body_name": name})
        automation.verify_named_body = lambda name, unit=None: automation._result(
            True, "verified", data={"body_name": name})
        with patch("solidworks_mcp.automation.high_level.typed",
                   return_value=manager):
            result = automation.create_sweep_feature(
                "Path", "circle", "Tube", diameter=6.0)
        self.assertFalse(result["success"], result)
        self.assertEqual(result["data"]["error"]["code"],
                         "CAPABILITY_UNAVAILABLE")
        self.assertEqual(container_calls, [])
        self.assertEqual(segment.calls, [])

    def test_circular_swept_member_materializes_native_circle_profile(self):
        automation = SolidWorksAutomation()
        captured = {}
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "none", data={"bodies": []})

        def run_transaction(**kwargs):
            captured.update(kwargs)
            steps = [{"data": {"operation": item["op"]}}
                     for item in kwargs["operations"]]
            return automation._result(True, "committed", data={"steps": steps})

        automation.run_transaction = run_transaction
        result = automation.create_swept_member(
            path={"name": "Path", "plane": "Front", "entities": [
                {"id": "path", "type": "line",
                 "start": [2.0, 0.0], "end": [2.0, 20.0]}]},
            profile={"type": "circle", "plane": "Top",
                     "center": [2.0, 0.0], "diameter": 6.0},
            body_name="Tube", unit="mm")
        self.assertTrue(result["success"], result)
        operations = captured["operations"]
        self.assertEqual([item["op"] for item in operations], [
            "create_parametric_sketch", "create_parametric_sketch",
            "create_sweep_feature"])
        circle = operations[1]["args"]["entities"][0]
        self.assertEqual(circle["type"], "circle")
        self.assertEqual(circle["center"], [2.0, 0.0])
        self.assertEqual(circle["radius"], 3.0)
        self.assertEqual(operations[2]["args"]["profile_type"], "circle")
        self.assertTrue(operations[2]["args"]["profile_sketch"].startswith(
            "$steps."))

    def test_multibody_insert_verifies_both_sides_inside_transaction(self):
        automation = SolidWorksAutomation()
        captured = {}
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "host", data={"bodies": [{"name": "Host"}]})

        def run_transaction(*args, **kwargs):
            captured.update(kwargs)
            captured["name"] = args[0]
            captured["operations"] = args[1]
            steps = [{"data": {"operation": item["op"]}}
                     for item in args[1]]
            return automation._result(True, "committed", data={"steps": steps})

        automation.run_transaction = run_transaction
        entities = [
            {"id": "a", "type": "line", "start": [0, 0], "end": [4, 0]},
            {"id": "b", "type": "line", "start": [4, 0], "end": [4, 4]},
            {"id": "c", "type": "line", "start": [4, 4], "end": [0, 4]},
            {"id": "d", "type": "line", "start": [0, 4], "end": [0, 0]},
        ]
        result = automation.create_multibody_insert(
            insert_sketch={"name": "InsertProfile", "plane": "Front",
                           "unit": "mm", "entities": entities},
            insert_extrude={"depth": 5.0}, host_body="Host",
            insert_body="Insert", clearance=0.5,
            pocket_cut={"depth": 5.0})
        self.assertTrue(result["success"], result)
        operations = captured["operations"]
        self.assertEqual([item["op"] for item in operations], [
            "create_parametric_sketch", "advanced_extrude", "rename_body",
            "create_parametric_sketch", "advanced_cut", "verify_named_body",
            "verify_named_body", "check_clearance"])
        self.assertNotIn("continue_on_failure", operations[2])
        self.assertEqual(operations[4]["args"]["scope_bodies"], ["Host"])
        self.assertEqual(operations[4]["args"]["end_condition"], "through_all")
        self.assertTrue(operations[4]["args"]["auto_flags"])
        self.assertAlmostEqual(
            operations[7]["args"]["min_clearance"], 0.4999)
        self.assertEqual(captured["invariants"]["solid_body_count"], 2)
        self.assertEqual(set(result["data"]["mating_sides"]),
                         {"host", "insert"})

    def test_advanced_extrude_reconciles_body_name_after_feature_rename(self):
        automation = SolidWorksAutomation()
        state = {"created": False, "renamed": False}

        class Feature:
            Name = "Boss-Extrude1"

        class Manager:
            def FeatureExtrusion3(self, *args):
                state["created"] = True
                return Feature()

        class Doc:
            FeatureManager = Manager()

        automation.get_active_doc = lambda: (Doc(), None)
        automation.ensure_features_not_frozen = lambda doc: {}
        automation._prepare_profile_selection = lambda doc, name: (name, None)
        automation._feature_bbox = lambda feature, unit: {
            "min": [0, 0, 0], "max": [1, 1, 1]}

        def body_names(doc):
            if not state["created"]:
                return ["Host"]
            if not state["renamed"]:
                return ["Host", "Boss-Extrude1"]
            return ["Host", "Insert_Boss"]

        def rename_feature(doc, feature, requested):
            state["renamed"] = True
            feature.Name = requested
            return requested, None

        automation._body_names = body_names
        automation._rename_feature_safe = rename_feature
        with patch(
                "solidworks_mcp.automation.advanced_features.typed",
                return_value=Doc.FeatureManager), patch(
                "solidworks_mcp.automation.advanced_features."
                "feature_face_count", return_value=6):
            result = automation.advanced_extrude(
                sketch_name="Insert_Profile", end_condition="mid_plane",
                depth=5.0, merge=False, feature_name="Insert_Boss",
                unit="mm")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["new_bodies"], ["Insert_Boss"])
        self.assertEqual(
            result["data"]["body_names_after"], ["Host", "Insert_Boss"])

    def test_scoped_cut_preserves_host_body_name_across_feature_rename(self):
        from solidworks_mcp.constants import SwEndConditions

        automation = SolidWorksAutomation()
        cut_arguments = []

        class Body:
            def __init__(self, name, document):
                self._name = name
                self.document = document

            @property
            def Name(self):
                return self._name

            @Name.setter
            def Name(self, value):
                feature_names = {
                    feature.Name for feature in self.document.features}
                if value not in feature_names:
                    self._name = value

            def GetBodyBox(self):
                return [0.0, 0.0, 0.0, 0.010, 0.010, 0.010]

        class SourceFeature:
            def __init__(self, name):
                self.Name = name

        class CutFeature:
            def __init__(self, document):
                self.document = document
                self._name = "Cut-Extrude1"

            @property
            def Name(self):
                return self._name

            @Name.setter
            def Name(self, value):
                self._name = value
                self.document.current_body._name = value

        class Doc:
            pass

        document = Doc()
        source_feature = SourceFeature("Host")
        document.features = [source_feature]
        document.current_body = Body("Host", document)

        class Manager:
            def FeatureCut4(self, *args):
                cut_arguments.append(args)
                document.current_body = Body("Cut-Extrude1", document)
                cut_feature = CutFeature(document)
                document.features.append(cut_feature)
                return cut_feature

        document.FeatureManager = Manager()
        automation.get_active_doc = lambda: (document, None)
        automation.ensure_features_not_frozen = lambda doc: {}
        automation._prepare_profile_selection = lambda doc, name: (name, None)
        automation._select_scope_bodies = lambda doc, names: None
        automation._get_solid_bodies = lambda doc: [doc.current_body]
        automation._find_body = lambda doc, name: (
            doc.current_body if doc.current_body.Name == name else None)
        automation._body_names = lambda doc: [doc.current_body.Name]
        automation._find_feature = lambda doc, name: next(
            (feature for feature in doc.features
             if feature.Name == name), None)
        automation._feature_names = lambda doc: [
            feature.Name for feature in doc.features]
        automation._feature_bbox = lambda feature, unit: {
            "min": [0, 0, 0], "max": [1, 1, 1]}
        with patch(
                "solidworks_mcp.automation.advanced_features.typed",
                return_value=document.FeatureManager), patch(
                "solidworks_mcp.automation.advanced_features."
                "feature_face_count", return_value=5):
            result = automation.advanced_cut(
                sketch_name="Pocket_Profile",
                end_condition="through_all_both",
                scope_bodies=["Host"], feature_name="Pocket",
                unit="mm")
        self.assertTrue(result["success"], result)
        self.assertEqual(document.current_body.Name, "Host")
        self.assertEqual(source_feature.Name, "Host_SourceFeature")
        self.assertEqual(result["data"]["body_names_after"], ["Host"])
        self.assertEqual(result["data"]["new_bodies"], [])
        self.assertEqual(result["data"]["merged_bodies"], [])
        self.assertEqual(len(result["data"]["scope_name_restorations"]), 1)
        self.assertEqual(result["data"]["scope_feature_renames"], [{
            "from": "Host",
            "to": "Host_SourceFeature",
            "reason": "body_name_namespace_collision",
            "rename_warning": None,
        }])
        self.assertTrue(result["data"]["double_ended"])
        self.assertFalse(cut_arguments[0][0])
        self.assertEqual(
            cut_arguments[0][3],
            int(SwEndConditions.swEndCondThroughAll))
        self.assertEqual(
            cut_arguments[0][4],
            int(SwEndConditions.swEndCondThroughAll))

    def test_multibody_insert_rejects_disconnected_profile(self):
        automation = SolidWorksAutomation()
        automation.list_bodies = lambda **kwargs: automation._result(
            True, "host", data={"bodies": [{"name": "Host"}]})
        entities = [
            {"id": "a", "type": "line", "start": [0, 0], "end": [4, 0]},
            {"id": "b", "type": "line", "start": [5, 0], "end": [5, 4]},
            {"id": "c", "type": "line", "start": [5, 4], "end": [0, 4]},
            {"id": "d", "type": "line", "start": [0, 4], "end": [0, 0]},
        ]
        result = automation.create_multibody_insert(
            insert_sketch={"name": "Broken", "plane": "Front",
                           "unit": "mm", "entities": entities},
            insert_extrude={"depth": 5.0}, host_body="Host",
            insert_body="Insert", clearance=0.5)
        self.assertFalse(result["success"])
        self.assertIn("contiguous", result["message"])

    def test_multibody_offset_isolated_worker_handles_concave_polygon(self):
        automation = SolidWorksAutomation()
        vertices = [[0, 0], [4, 0], [4, 1],
                    [1, 1], [1, 4], [0, 4], [0, 0]]
        entities = [
            {"id": f"edge_{index}", "type": "line",
             "start": vertices[index], "end": vertices[index + 1]}
            for index in range(len(vertices) - 1)]
        original_import = __import__

        def guarded_import(name, *args, **kwargs):
            if name.startswith("shapely"):
                raise AssertionError("GEOS must not load in the COM process")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            offset = automation._offset_profile_entities(entities, 0.5)
        self.assertGreaterEqual(len(offset), 6)
        points = [entity["start"] for entity in offset]
        self.assertAlmostEqual(min(point[0] for point in points), -0.5)
        self.assertAlmostEqual(max(point[0] for point in points), 4.5)
        self.assertAlmostEqual(min(point[1] for point in points), -0.5)
        self.assertAlmostEqual(max(point[1] for point in points), 4.5)

        bow_tie = [[0, 0], [4, 4], [0, 4], [4, 0], [0, 0]]
        invalid = [
            {"id": f"bad_{index}", "type": "line",
             "start": bow_tie[index], "end": bow_tie[index + 1]}
            for index in range(len(bow_tie) - 1)]
        with self.assertRaisesRegex(ValueError, "not a valid polygon"):
            automation._offset_profile_entities(invalid, 0.5)

    def test_geometry_sampler_preserves_full_circle_ellipse_and_three_point_arc(self):
        automation = SolidWorksAutomation()
        geometry = {"entities": [
            {"id": "circle", "type": "arc", "center": [0.0, 0.0],
             "radius": 3.0, "start": [3.0, 0.0], "end": [3.0, 0.0]},
            {"id": "ellipse", "type": "ellipse", "center": [10.0, 0.0],
             "major_point": [14.0, 0.0], "minor_point": [10.0, 2.0]},
            {"id": "arc3", "type": "arc_3pt", "start": [21.0, 0.0],
             "point": [20.0, 1.0], "end": [19.0, 0.0]},
        ]}
        sampled = {item["entity_id"]: item["points"]
                   for item in automation._sample_geometry_entities(geometry, 0.1)}
        self.assertGreater(len(sampled["circle"]), 100)
        self.assertGreater(math.dist(sampled["circle"][0],
                                     sampled["circle"][len(sampled["circle"]) // 2]),
                           5.9)
        ellipse_x = [point[0] for point in sampled["ellipse"]]
        ellipse_y = [point[1] for point in sampled["ellipse"]]
        self.assertAlmostEqual(min(ellipse_x), 6.0, places=2)
        self.assertAlmostEqual(max(ellipse_x), 14.0, places=2)
        self.assertAlmostEqual(max(ellipse_y), 2.0, places=2)
        self.assertLess(min(math.dist(point, [20.0, 1.0])
                            for point in sampled["arc3"]), 0.02)

    def test_entity_bbox_includes_directed_arc_and_rotated_ellipse_extrema(self):
        automation = SolidWorksAutomation()
        upper = automation._entity_bbox([{
            "id": "upper", "type": "arc_3pt",
            "start": [140.0, 40.0], "point": [145.0, 45.0],
            "end": [150.0, 40.0]}], "mm")
        self.assertAlmostEqual(upper["min"][1], 40.0)
        self.assertAlmostEqual(upper["max"][1], 45.0)

        lower = automation._entity_bbox([{
            "id": "lower", "type": "arc_3pt",
            "start": [140.0, 40.0], "point": [145.0, 35.0],
            "end": [150.0, 40.0]}], "mm")
        self.assertAlmostEqual(lower["min"][1], 35.0)
        self.assertAlmostEqual(lower["max"][1], 40.0)

        readback = automation._entity_bbox([{
            "id": "readback", "type": "arc",
            "start": [140.0, 40.0], "end": [150.0, 40.0],
            "center": [145.0, 40.0], "radius": 5.0,
            "start_angle": -math.pi, "end_angle": 0.0,
            "clockwise": True}], "mm")
        self.assertAlmostEqual(readback["max"][1], 45.0)

        ellipse = automation._entity_bbox([{
            "id": "ellipse", "type": "ellipse", "center": [192.0, 8.0],
            "major_radius": 6.0, "minor_radius": 3.0,
            "rotation_deg": 30.0}], "mm")
        self.assertAlmostEqual(
            ellipse["max"][0], 192.0 + math.sqrt(29.25))
        self.assertAlmostEqual(
            ellipse["max"][1], 8.0 + math.sqrt(15.75))

    def test_geometry_sampler_evaluates_explicit_b_spline(self):
        automation = SolidWorksAutomation()
        sampled = automation._sample_geometry_entities({"entities": [{
            "id": "curve", "type": "b_spline", "order": 3,
            "control_points": [[0.0, 0.0], [1.0, 1.0], [2.0, 0.0]],
            "knots": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
            "periodic": False,
        }]}, 0.05)
        self.assertEqual(len(sampled), 1)
        self.assertGreaterEqual(len(sampled[0]["points"]), 64)
        self.assertEqual(sampled[0]["points"][0], [0.0, 0.0])
        self.assertEqual(sampled[0]["points"][-1], [2.0, 0.0])
        self.assertGreater(max(point[1] for point in sampled[0]["points"]), 0.49)

    def test_ellipse_export_reads_native_axis_points(self):
        automation = SolidWorksAutomation()

        class EllipseSegment:
            ConstructionGeometry = False

            def GetType(self):
                return 2

            def GetCenterPoint2(self):
                return FakePoint(0.0, 0.0)

            def GetMajorPoint2(self):
                return FakePoint(0.010, 0.0)

            def GetMinorPoint2(self):
                return FakePoint(0.0, 0.004)

            def GetStartPoint2(self):
                return FakePoint(0.010, 0.0)

            def GetEndPoint2(self):
                return FakePoint(0.010, 0.0)

        automation._persist = lambda doc, entity: "ellipse-pid"
        item = automation._export_segment(
            object(), EllipseSegment(), "ellipse", "mm",
            include={"constraint_status": False})
        self.assertEqual(item["type"], "ellipse")
        self.assertEqual(item["major_point"], [10.0, 0.0])
        self.assertEqual(item["minor_point"], [0.0, 4.0])
        self.assertAlmostEqual(item["major_radius"], 10.0)
        self.assertAlmostEqual(item["minor_radius"], 4.0)

    def test_sketch_reference_comparison_preserves_hole_and_writes_svg(self):
        import cv2
        import numpy as np
        import xml.etree.ElementTree as ET

        automation = SolidWorksAutomation()
        geometry = {
            "entities": [
                {"id": "outer", "type": "circle", "center": [32.0, 32.0],
                 "radius": 20.0},
                {"id": "hole", "type": "circle", "center": [32.0, 32.0],
                 "radius": 8.0},
            ],
            "contours": [
                {"id": "outer_loop", "entities": ["outer"], "closed": True},
                {"id": "hole_loop", "entities": ["hole"], "closed": True},
            ],
        }
        automation._load_geometry_payload = lambda *args, **kwargs: (
            {"success": True}, geometry)
        reference = np.zeros((64, 64), dtype=np.uint8)
        cv2.circle(reference, (32, 32), 20, 255, -1)
        cv2.circle(reference, (32, 32), 8, 0, -1)
        rgb = cv2.cvtColor(reference, cv2.COLOR_GRAY2RGB)
        automation._reference_mask = lambda *args, **kwargs: (
            rgb, reference, 1.0, [])
        with tempfile.TemporaryDirectory() as directory:
            image_path = os.path.join(directory, "reference.png")
            svg_path = os.path.join(directory, "overlay.svg")
            self.assertTrue(cv2.imwrite(image_path, reference))
            result = automation.compare_sketch_to_reference(
                "Ring", image_path,
                transform={"mode": "explicit", "matrix": np.eye(3).tolist()},
                tolerance={"sample_step_mm": 0.05, "min_iou": 0.90,
                           "mean_mm": 2.0, "p95_mm": 2.0, "max_mm": 2.0},
                outputs={"svg_overlay": svg_path})
            self.assertTrue(result["success"], result)
            self.assertEqual(result["data"]["rasterization"]["closed_contours"], 2)
            self.assertEqual(result["data"]["rasterization"][
                "disconnected_closed_contours"], [])
            self.assertTrue(os.path.isfile(svg_path))
            ET.parse(svg_path)

    def test_sketch_rasterization_even_odd_fill_avoids_shapely_in_com_process(self):
        import builtins
        import numpy as np
        from unittest import mock

        automation = SolidWorksAutomation()
        geometry = {
            "entities": [
                {"id": "outer", "type": "circle", "center": [64.0, 64.0],
                 "radius": 40.0},
                {"id": "hole", "type": "circle", "center": [64.0, 64.0],
                 "radius": 20.0},
                {"id": "island", "type": "circle", "center": [64.0, 64.0],
                 "radius": 6.0},
                {"id": "disjoint", "type": "circle",
                 "center": [108.0, 20.0], "radius": 8.0},
            ],
            "contours": [
                {"id": "outer_loop", "entities": ["outer"], "closed": True},
                {"id": "hole_loop", "entities": ["hole"], "closed": True},
                {"id": "island_loop", "entities": ["island"], "closed": True},
                {"id": "disjoint_loop", "entities": ["disjoint"],
                 "closed": True},
            ],
        }
        original_import = builtins.__import__

        def reject_shapely(name, *args, **kwargs):
            if name == "shapely" or name.startswith("shapely."):
                raise AssertionError("Shapely must not load in the COM process")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=reject_shapely):
            filled, _, report = automation._rasterize_sketch_geometry(
                geometry, np.eye(3), (128, 128), 0.2,
                line_mode=False, supersample=2)
            lines, _, line_report = automation._rasterize_sketch_geometry(
                geometry, np.eye(3), (128, 128), 0.2,
                line_mode=True, supersample=2)

        self.assertEqual(int(filled[64, 64]), 255)
        self.assertEqual(int(filled[64, 48]), 0)
        self.assertEqual(int(filled[64, 32]), 255)
        self.assertEqual(int(filled[20, 108]), 255)
        self.assertEqual(int(filled[5, 5]), 0)
        self.assertGreater(int(lines[64, 104]), 0)
        self.assertEqual(report["fill_rule"], "even_odd_xor")
        self.assertEqual(report["degenerate_closed_contours"], [])
        self.assertIsNone(line_report["fill_rule"])

    def test_sketch_reference_comparison_attributes_real_problem_entity(self):
        import cv2
        import numpy as np

        automation = SolidWorksAutomation()
        geometry = {
            "entities": [{"id": "shifted_circle", "type": "circle",
                          "center": [36.0, 32.0], "radius": 16.0}],
            "contours": [{"id": "loop", "entities": ["shifted_circle"],
                          "closed": True}],
        }
        automation._load_geometry_payload = lambda *args, **kwargs: (
            {"success": True}, geometry)
        reference = np.zeros((64, 64), dtype=np.uint8)
        cv2.circle(reference, (32, 32), 16, 255, -1)
        rgb = cv2.cvtColor(reference, cv2.COLOR_GRAY2RGB)
        automation._reference_mask = lambda *args, **kwargs: (
            rgb, reference, 1.0, [])
        with tempfile.TemporaryDirectory() as directory:
            image_path = os.path.join(directory, "reference.png")
            self.assertTrue(cv2.imwrite(image_path, reference))
            result = automation.compare_sketch_to_reference(
                "Shifted", image_path,
                transform={"mode": "explicit", "matrix": np.eye(3).tolist()},
                tolerance={"min_iou": 0.999, "mean_mm": 0.1,
                           "p95_mm": 0.1, "max_mm": 0.1})
            self.assertFalse(result["success"])
            data = result["data"]
            self.assertEqual(data["problem_entities"][0]["entity_id"],
                             "shifted_circle")
            self.assertTrue(data["maximum_deviation_zones"])
            self.assertEqual(data["maximum_deviation_zones"][0]["entity_id"],
                             "shifted_circle")
            self.assertEqual(data["error"]["code"], "REFERENCE_MISMATCH")

    def test_export_inspectors_reject_structurally_incomplete_files(self):
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            part_path = os.path.join(directory, "valid.sldprt")
            with open(part_path, "wb") as handle:
                handle.write(bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 504)
            self.assertEqual(automation._inspect_sldprt(part_path)["container"],
                             "CFBF")
            native_part = os.path.join(directory, "native.sldprt")
            with open(native_part, "wb") as handle:
                handle.write(bytes.fromhex(
                    "96730ED9000000048B1E2DD6CEC146CF"
                    "56080ED65FD2140006000800DFFFFFFF") + b"\0" * 480)
            self.assertEqual(automation._inspect_sldprt(native_part)["container"],
                             "SOLIDWORKS_NATIVE")
            alternate_native = os.path.join(directory, "alternate.sldprt")
            with open(alternate_native, "wb") as handle:
                handle.write(bytes.fromhex(
                    "2552D26A0000000451462DD4BF8BEC2A"
                    "3CD122B460140006000800DFFFFFFFFF") + b"\0" * 480)
            self.assertEqual(
                automation._inspect_sldprt(alternate_native)["container"],
                "SOLIDWORKS_NATIVE")
            sw2026_save_as_variant = os.path.join(
                directory, "sw2026-save-as-variant.sldprt")
            with open(sw2026_save_as_variant, "wb") as handle:
                handle.write(bytes.fromhex(
                    "24D0DEB300000004028346D87DB11E6C"
                    "AC9F1D519A140006000800DF7FEFFF") + b"\0" * 512)
            self.assertEqual(
                automation._inspect_sldprt(sw2026_save_as_variant)["container"],
                "SOLIDWORKS_NATIVE")
            delayed_native = os.path.join(directory, "delayed-native.sldprt")
            with open(delayed_native, "wb") as handle:
                handle.write(bytes.fromhex("AABBCCDD00000004") + b"\0" * 504)

            def finish_delayed_native():
                time.sleep(0.05)
                with open(delayed_native, "wb") as handle:
                    handle.write(bytes.fromhex(
                        "AABBCCDD00000004" + "00" * 80 +
                        "140006000800DFFFFFFF") + b"\0" * 512)

            publisher = threading.Thread(target=finish_delayed_native)
            publisher.start()
            delayed_inspection = automation._inspect_sldprt(
                delayed_native, timeout_sec=0.5, poll_interval_sec=0.01)
            publisher.join()
            self.assertEqual(delayed_inspection["container"],
                             "SOLIDWORKS_NATIVE")
            self.assertGreater(delayed_inspection["native_record_offset"], 48)
            fake_native = os.path.join(directory, "fake-native.sldprt")
            with open(fake_native, "wb") as handle:
                handle.write(bytes.fromhex("FFFFFFFF00000004") + b"\0" * 504)
            with self.assertRaisesRegex(ValueError, "recognized"):
                automation._inspect_sldprt(fake_native)
            step_path = os.path.join(directory, "valid.step")
            with open(step_path, "wb") as handle:
                handle.write(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n" +
                             b"#1=MANIFOLD_SOLID_BREP('A',#3);\n" +
                             b"#2=MANIFOLD_SOLID_BREP('B',#4);\n" +
                             b"#3=CLOSED_SHELL('',());\n" +
                             b"#4=CLOSED_SHELL('',());\n" +
                             b"ENDSEC;\nEND-ISO-10303-21;\n")
            inspection = automation._inspect_step(step_path)
            self.assertEqual(inspection["entity_records"], 4)
            self.assertEqual(inspection["solid_body_count"], 2)
            self.assertEqual(inspection["closed_shell_count"], 2)
            broken_step = os.path.join(directory, "broken.step")
            with open(broken_step, "wb") as handle:
                handle.write(b"ISO-10303-21;" + b"x" * 256)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                automation._inspect_step(broken_step)

    def test_export_commit_rolls_back_every_replaced_target(self):
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            target_a = os.path.join(directory, "a.step")
            target_b = os.path.join(directory, "b.step")
            staged_a = os.path.join(directory, "a.staged")
            staged_b = os.path.join(directory, "b.staged")
            for path, content in ((target_a, b"old-a"), (target_b, b"old-b"),
                                  (staged_a, b"new-a"), (staged_b, b"new-b")):
                with open(path, "wb") as handle:
                    handle.write(content)
            statuses = []
            for staged, target in ((staged_a, target_a), (staged_b, target_b)):
                statuses.append({
                    "kind": "test", "staged": staged, "target": target,
                    "size_bytes": os.path.getsize(staged),
                    "sha256": automation._file_hash(staged),
                })
            real_replace = os.replace
            failed = {"value": False}

            def fail_second_install(source, target):
                if (source == staged_b and target == target_b and
                        not failed["value"]):
                    failed["value"] = True
                    raise OSError("injected second-target failure")
                return real_replace(source, target)

            with patch("solidworks_mcp.automation.high_level.os.replace",
                       side_effect=fail_second_install):
                with self.assertRaisesRegex(RuntimeError, "all prior targets were restored"):
                    automation._commit_export_targets(statuses, overwrite=True)
            self.assertEqual(Path(target_a).read_bytes(), b"old-a")
            self.assertEqual(Path(target_b).read_bytes(), b"old-b")
            self.assertFalse(any(item.get("committed") for item in statuses))

    def test_export_commit_verifies_hashes_before_success(self):
        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            staged = os.path.join(directory, "bundle.staged")
            target = os.path.join(directory, "bundle.step")
            with open(staged, "wb") as handle:
                handle.write(b"verified payload")
            status = {"kind": "test", "staged": staged, "target": target,
                      "size_bytes": os.path.getsize(staged),
                      "sha256": automation._file_hash(staged)}
            automation._commit_export_targets([status], overwrite=False)
            self.assertTrue(status["committed"])
            self.assertEqual(Path(target).read_bytes(), b"verified payload")

    def test_stl_settings_are_deterministic_without_global_preferences(self):
        automation = SolidWorksAutomation()

        class PreferenceApp:
            def __getattr__(self, name):
                if "UserPreference" in name:
                    raise AssertionError(
                        "Native body tessellation must not access global prefs")
                raise AttributeError(name)

        automation._sw_app = PreferenceApp()
        with automation._stl_export_preferences({
                "quality": "custom", "deviation_mm": 0.03,
                "angle_tolerance_deg": 5, "binary": True,
                "preserve_origin": True}, "mm") as applied:
            self.assertEqual(applied["backend"],
                             "solidworks_itessellation")
            self.assertEqual(applied["quality"], "custom")
            self.assertAlmostEqual(applied["deviation_mm"], 0.03)
            self.assertAlmostEqual(applied["angle_tolerance_deg"], 5.0)
            self.assertFalse(applied["preferences_mutated"])

    def test_native_body_tessellation_writes_verified_binary_and_ascii_stl(self):
        automation = SolidWorksAutomation()
        vertices = {
            0: (-0.005, -0.005, -0.005),
            1: (0.005, -0.005, -0.005),
            2: (0.005, 0.005, -0.005),
            3: (-0.005, 0.005, -0.005),
            4: (-0.005, -0.005, 0.005),
            5: (0.005, -0.005, 0.005),
            6: (0.005, 0.005, 0.005),
            7: (-0.005, 0.005, 0.005),
        }
        facets = [
            (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
            (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
        ]

        class Tessellation:
            def __init__(self):
                self.fins = {}
                for facet_id, (a, b, c) in enumerate(facets):
                    self.fins[facet_id * 3] = (a, b)
                    self.fins[facet_id * 3 + 1] = (b, c)
                    self.fins[facet_id * 3 + 2] = (c, a)

            def Tessellate(self):
                return True

            def GetFacetCount(self):
                return len(facets)

            def GetFacetFins(self, facet_id):
                return [facet_id * 3 + offset for offset in range(3)]

            def GetFinVertices(self, fin_id):
                return self.fins[fin_id]

            def GetVertexPoint(self, vertex_id):
                return vertices[vertex_id]

            def GetVertexNormal(self, vertex_id):
                return (0.0, 0.0, 1.0)

        tessellation = Tessellation()

        class Body:
            Name = "Cube"

            def GetTessellation(self, faces):
                self.faces_argument = faces
                return tessellation

        body = Body()
        with tempfile.TemporaryDirectory() as directory, patch(
                "solidworks_mcp.automation.high_level.typed",
                side_effect=lambda value, interface: value):
            binary_path = os.path.join(directory, "cube-binary.stl")
            settings = automation._resolve_stl_settings({
                "quality": "custom", "deviation_mm": 0.03,
                "angle_tolerance_deg": 5, "binary": True}, "mm")
            export = automation._export_body_stl(
                object(), body, binary_path, settings)
            binary = automation._inspect_stl(binary_path)
            self.assertEqual(export["backend"],
                             "solidworks_itessellation")
            self.assertEqual(binary["triangles"], 12)
            for actual, expected in zip(binary["bbox"]["min"], [-5, -5, -5]):
                self.assertAlmostEqual(actual, expected, places=5)
            for actual, expected in zip(binary["bbox"]["max"], [5, 5, 5]):
                self.assertAlmostEqual(actual, expected, places=5)
            self.assertTrue(tessellation.ImprovedQuality)
            self.assertTrue(tessellation.NeedVertexNormal)
            self.assertAlmostEqual(
                tessellation.SurfacePlaneTolerance, 0.00003)

            ascii_path = os.path.join(directory, "cube-ascii.stl")
            ascii_settings = {**settings, "binary": False,
                              "preserve_origin": False}
            automation._export_body_stl(
                object(), body, ascii_path, ascii_settings)
            ascii_mesh = automation._inspect_stl(ascii_path)
            self.assertEqual(ascii_mesh["triangles"], 12)
            for actual in ascii_mesh["bbox"]["min"]:
                self.assertAlmostEqual(actual, 0.0, places=5)

            limited = {**settings, "max_triangles": 11}
            with self.assertRaisesRegex(RuntimeError, "max_triangles=11"):
                automation._export_body_stl(
                    object(), body, os.path.join(directory, "limited.stl"),
                    limited)

    def test_stl_bbox_verification_handles_explicit_translation_only(self):
        body = {"bbox": {"min": [-5.0, 2.0, 1.0],
                         "max": [15.0, 12.0, 6.0]}}
        inspection = {"bbox": {"min": [0.0, 0.0, 0.0],
                               "max": [20.0, 10.0, 5.0]}}
        verified = SolidWorksAutomation._verify_stl_bbox(
            inspection, body, preserve_origin=False)
        self.assertTrue(verified["bbox_verification"]["passed"])
        self.assertEqual(
            verified["bbox_verification"]["coordinate_translation"],
            [5.0, -2.0, -1.0])
        with self.assertRaisesRegex(ValueError, "preserve the CAD origin"):
            SolidWorksAutomation._verify_stl_bbox(
                {"bbox": {"min": [0.0, 0.0, 0.0],
                          "max": [20.0, 10.0, 5.0]}},
                body, preserve_origin=True)

    def test_transaction_snapshot_skips_persistent_id_readback_by_default(self):
        automation = FakeAutomation()
        feature = type("SnapshotFeature", (), {
            "Name": "FeatureA", "GetTypeName2": "Boss"})()
        body = type("SnapshotBody", (), {
            "Name": "BodyA",
            "GetBodyBox": lambda self: [0.0, 0.0, 0.0, 0.01, 0.01, 0.01],
            "GetFaces": lambda self: [object()]})()
        automation._walk_features_tx = lambda doc: [feature]
        automation.doc.GetBodies2 = lambda body_type, visible: [body]
        automation._persist_reference = lambda *args, **kwargs: (
            (_ for _ in ()).throw(
                AssertionError("Persistent IDs must be opt-in for snapshots")))
        snapshot = automation._transaction_snapshot(automation.doc)
        self.assertGreater(snapshot["feature_count"], 0)
        self.assertTrue(all(item["persistent_id"] is None
                            for item in snapshot["features"]))
        self.assertTrue(all(item["persistent_id"] is None
                            for item in snapshot["bodies"]))

    def test_body_silhouette_comparison_requires_metric_scale(self):
        import numpy as np

        automation = SolidWorksAutomation()
        mask = np.zeros((32, 32), dtype=np.uint8)
        mask[8:24, 8:24] = 255
        rgb = np.dstack([mask, mask, mask])
        automation.take_screenshot = lambda *args, **kwargs: automation._result(
            True, "shot", data={"path": args[0]})
        automation._reference_mask = lambda *args, **kwargs: (
            rgb, mask, 1.0, [])
        result = automation.compare_body_silhouette_to_image(
            "reference.png", "candidate.png",
            transform={"candidate_to_reference": np.eye(3).tolist()})
        self.assertFalse(result["success"])
        self.assertIn("mm_per_pixel", result["message"])

    def test_body_silhouette_comparison_reports_maximum_deviation_zones(self):
        import cv2
        import numpy as np

        automation = SolidWorksAutomation()
        reference = np.zeros((64, 64), dtype=np.uint8)
        candidate = np.zeros((64, 64), dtype=np.uint8)
        cv2.circle(reference, (30, 32), 16, 255, -1)
        cv2.circle(candidate, (35, 32), 16, 255, -1)
        masks = iter((reference, candidate))
        automation.take_screenshot = lambda *args, **kwargs: automation._result(
            True, "shot", data={"bbox": {"min": [0, 0, 0],
                                          "max": [1, 1, 1]}})

        def reference_mask(*args, **kwargs):
            mask = next(masks)
            return cv2.cvtColor(mask, cv2.COLOR_GRAY2RGB), mask, 1.0, []

        automation._reference_mask = reference_mask
        result = automation.compare_body_silhouette_to_image(
            "reference.png", "candidate.png",
            transform={"candidate_to_reference": np.eye(3).tolist(),
                       "mm_per_pixel": 0.1},
            tolerance={"min_iou": 0.999, "max_hausdorff_mm": 0.05})
        self.assertFalse(result["success"])
        self.assertTrue(result["data"]["maximum_deviation_zones"])
        self.assertEqual(result["data"]["quality_profile"], "balanced")
        self.assertEqual(result["data"]["framing"]["bbox"]["min"], [0, 0, 0])
        self.assertEqual(result["data"]["error"]["code"],
                         "REFERENCE_MISMATCH")

    def test_screenshot_readability_rejects_background_gradient(self):
        import builtins
        import numpy as np
        from PIL import Image, ImageDraw

        automation = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as directory:
            gradient_path = os.path.join(directory, "gradient.png")
            line_path = os.path.join(directory, "geometry.png")
            gradient = np.tile(
                np.linspace(215, 252, 720, dtype=np.uint8)[:, None],
                (1, 1400))
            Image.fromarray(gradient, mode="L").save(gradient_path)
            line_image = Image.fromarray(gradient, mode="L").convert("RGB")
            draw = ImageDraw.Draw(line_image)
            draw.rectangle((350, 180, 1050, 540), outline=(20, 20, 20),
                           width=8)
            line_image.save(line_path)

            original_import = builtins.__import__

            def reject_numpy(name, *args, **kwargs):
                if name == "numpy" or name.startswith("numpy."):
                    raise AssertionError(
                        "NumPy must not load in the COM screenshot path")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=reject_numpy):
                blank = automation._check_frame_readability(gradient_path)
                geometry = automation._check_frame_readability(line_path)

        self.assertTrue(blank["frame_unreadable"])
        self.assertEqual(blank["readability_reason"],
                         "no_central_model_edges")
        self.assertFalse(geometry["frame_unreadable"])
        self.assertGreater(geometry["central_edge_share"], 0.0005)

    def test_model_screenshot_replaces_blank_saveas_with_viewport(self):
        import numpy as np
        from PIL import Image, ImageDraw

        automation = SolidWorksAutomation()

        class BlankSaveAsDoc:
            def SaveAs3(self, path, options, silent):
                gradient = np.tile(
                    np.linspace(215, 252, 480, dtype=np.uint8)[:, None],
                    (1, 800))
                Image.fromarray(gradient, mode="L").save(path)
                return True

        def capture_viewport(path, compress, width, height):
            image = Image.new("RGB", (800, 480), (240, 240, 240))
            draw = ImageDraw.Draw(image)
            draw.ellipse((200, 100, 600, 400), outline=(10, 10, 10),
                         width=8)
            image.save(path)
            info = {"path": path, "size_bytes": os.path.getsize(path),
                    "capture_method": "screen_viewport_fallback"}
            info.update(automation._check_frame_readability(path))
            return automation._result(True, "viewport",
                                      data=info)

        automation._capture_sw_viewport = capture_viewport
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shot.png")
            result = automation._capture_model_image(
                BlankSaveAsDoc(), path, False, 0, 0)
            final_check = automation._check_frame_readability(path)

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["capture_method"],
                         "screen_viewport_fallback")
        self.assertEqual(result["data"]["primary_capture_method"],
                         "save_as3")
        self.assertEqual(result["data"]["fallback_reason"],
                         "no_central_model_edges")
        self.assertFalse(final_check["frame_unreadable"])

    def test_section_cleanup_retries_until_active_data_is_gone(self):
        automation = SolidWorksAutomation()

        class ViewManager:
            def __init__(self):
                self.remove_calls = 0
                self.verify_calls = 0

            def RemoveSectionView(self):
                self.remove_calls += 1
                return False

            def GetSectionViewData(self, name):
                self.verify_calls += 1
                return object() if self.verify_calls == 1 else None

        class Document:
            def __init__(self):
                self.redraws = 0

            def GraphicsRedraw2(self):
                self.redraws += 1

        manager = ViewManager()
        document = Document()
        cleanup = automation._remove_section_view_verified(
            manager, document, max_attempts=3)
        self.assertTrue(cleanup["verified_off"])
        self.assertEqual(cleanup["attempts"], 2)
        self.assertEqual(manager.remove_calls, 2)
        self.assertEqual(document.redraws, 2)

    def test_section_cleanup_never_claims_success_without_verification(self):
        automation = SolidWorksAutomation()

        class ViewManager:
            def RemoveSectionView(self):
                return True

            def GetSectionViewData(self, name):
                return object()

        class Document:
            def GraphicsRedraw2(self):
                return True

        cleanup = automation._remove_section_view_verified(
            ViewManager(), Document(), max_attempts=3)
        self.assertFalse(cleanup["verified_off"])
        self.assertEqual(cleanup["attempts"], 3)

    @staticmethod
    def _normal_to_fixture(view_matrix=None, native_matrix=None,
                           points=None, pixel_scale=1000.0):
        identity = [1.0, 0.0, 0.0,
                    0.0, 1.0, 0.0,
                    0.0, 0.0, 1.0,
                    0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]

        class Transform:
            def __init__(self, data):
                self.ArrayData = list(data)

            @property
            def Inverse(self):
                return Transform(identity)

        class Feature:
            Name = "TestSketch"

        class Sketch:
            Name = "TestSketch"
            ModelToSketchTransform = Transform(identity)

            def GetFeature(self):
                return Feature()

            def GetSketchPoints2(self):
                return [FakePoint(*point) for point in (points or [])]

            def GetSketchSegments(self):
                return []

        class Manager:
            ActiveSketch = Sketch()

        class View:
            FrameWidth = 1000
            FrameHeight = 800

            def __init__(self):
                self.Orientation3 = Transform(view_matrix or identity)
                self._scale2 = float(pixel_scale)
                self._translation_generation = 0
                self.Translation3 = Transform([0.0, 0.0, 0.0])

            @property
            def Scale2(self):
                return self._scale2

            @Scale2.setter
            def Scale2(self, value):
                self._scale2 = float(value)
                self._translation_generation += 1
                self.Translation3 = Transform([
                    float(self._translation_generation), 0.0, 0.0])

            @property
            def Transform(self):
                return Transform([
                    1.0, 0.0, 0.0,
                    0.0, 1.0, 0.0,
                    0.0, 0.0, 1.0,
                    500.0, 400.0, 0.0, self.Scale2, 0.0, 0.0, 0.0])

            def Update(self):
                return True

        class Document:
            SketchManager = Manager()

            def __init__(self):
                self.ActiveView = View()
                self.zoom_box_calls = 0
                self.zoom_fit_calls = 0

            def GraphicsRedraw2(self):
                return True

            def ViewZoomtofit2(self):
                self.zoom_fit_calls += 1

            def ViewZoomTo2(self, *args):
                self.zoom_box_calls += 1
                self.ActiveView.Scale2 = 10000.0

            def GetBodies2(self, body_type, visible_only):
                return []

        document = Document()

        class Application:
            def __init__(self):
                self.calls = []

            def IsCommandEnabled(self, command):
                return True

            def RunCommand(self, command, argument):
                self.calls.append(command)
                if command == 169 and native_matrix is not None:
                    document.ActiveView.Orientation3 = Transform(native_matrix)
                return True

        automation = SolidWorksAutomation()
        automation._sw_app = Application()
        return automation, document, identity, Transform

    def test_normal_to_is_noop_when_already_verified(self):
        automation, document, _, _ = self._normal_to_fixture()
        result = automation.orient_normal_to_active_sketch(
            document, zoom_to_fit=False)
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["verified"])
        self.assertFalse(result["data"]["changed"])
        self.assertEqual(automation._sw_app.calls, [])

    def test_normal_to_back_side_is_stable_noop(self):
        back = [-1.0, 0.0, 0.0,
                0.0, 1.0, 0.0,
                0.0, 0.0, -1.0,
                0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        automation, document, _, _ = self._normal_to_fixture(
            view_matrix=back)
        result = automation.orient_normal_to_active_sketch(
            document, zoom_to_fit=False)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["target_axes"]["side"],
                         "sketch_back")
        self.assertEqual(automation._sw_app.calls, [])

    def test_normal_to_verifies_native_command_readback(self):
        isometric = [0.707106781, -0.408204056, 0.577381545,
                     0.0, 0.816540812, 0.577287712,
                     -0.707106781, -0.408204056, 0.577381545,
                     0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        identity = [1.0, 0.0, 0.0,
                    0.0, 1.0, 0.0,
                    0.0, 0.0, 1.0,
                    0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        automation, document, _, _ = self._normal_to_fixture(
            view_matrix=isometric, native_matrix=identity)
        result = automation.orient_normal_to_active_sketch(
            document, zoom_to_fit=False)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["methods"],
                         ["swCommands_NormalTo"])
        self.assertEqual(automation._sw_app.calls, [169])
        self.assertLessEqual(
            result["data"]["angular_error_deg"]["normal_deg"], 0.1)

    def test_normal_to_uses_matrix_fallback_when_native_is_ineffective(self):
        isometric = [0.707106781, -0.408204056, 0.577381545,
                     0.0, 0.816540812, 0.577287712,
                     -0.707106781, -0.408204056, 0.577381545,
                     0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        automation, document, _, Transform = self._normal_to_fixture(
            view_matrix=isometric)

        def assign(_, target):
            right, up = target["right"], target["up"]
            toward = target["toward_viewer"]
            document.ActiveView.Orientation3 = Transform([
                right[0], up[0], toward[0],
                right[1], up[1], toward[1],
                right[2], up[2], toward[2],
                0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0])

        with patch.object(automation, "_assign_view_basis",
                          side_effect=assign):
            result = automation.orient_normal_to_active_sketch(
                document, zoom_to_fit=False)
        self.assertTrue(result["success"])
        self.assertIn("Orientation3[1]", result["data"]["methods"])
        self.assertFalse(result["data"]["attempts"][0]["verified"])

    def test_normal_to_never_claims_success_without_readback_match(self):
        isometric = [0.707106781, -0.408204056, 0.577381545,
                     0.0, 0.816540812, 0.577287712,
                     -0.707106781, -0.408204056, 0.577381545,
                     0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        automation, document, _, _ = self._normal_to_fixture(
            view_matrix=isometric)
        with patch.object(automation, "_assign_view_basis",
                          return_value=None):
            result = automation.orient_normal_to_active_sketch(
                document, zoom_to_fit=False)
        self.assertFalse(result["success"])
        self.assertFalse(result["data"]["verified"])
        self.assertEqual(len(result["data"]["attempts"]), 3)

    def test_fit_to_screen_repairs_tiny_active_sketch_and_verifies_pixels(self):
        points = [(-0.05, -0.025, 0.0), (0.05, 0.025, 0.0),
                  (-0.05, 0.025, 0.0), (0.05, -0.025, 0.0)]
        automation, document, _, _ = self._normal_to_fixture(
            points=points, pixel_scale=500.0)
        result = automation._fit_active_working_geometry(document)
        self.assertTrue(result["verified"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["scope"], "active_sketch")
        self.assertEqual(document.zoom_box_calls, 1)
        self.assertGreaterEqual(
            result["actual"]["dominant_fill_ratio"], 0.35)
        self.assertLessEqual(result["actual"]["dominant_fill_ratio"], 0.90)

    def test_fit_to_screen_is_invoked_even_when_initial_frame_is_valid(self):
        points = [(-0.05, -0.025, 0.0), (0.05, 0.025, 0.0),
                  (-0.05, 0.025, 0.0), (0.05, -0.025, 0.0)]
        automation, document, _, _ = self._normal_to_fixture(
            points=points, pixel_scale=8000.0)

        initial = automation._fit_measurement(document, points)
        self.assertTrue(initial["verified"])
        result = automation._fit_active_working_geometry(document)

        self.assertTrue(result["verified"])
        self.assertTrue(result["changed"])
        self.assertEqual(document.zoom_box_calls, 1)
        self.assertIn("ViewZoomTo2(active_geometry)", result["methods"])
        self.assertIn("Scale2(after ViewZoomTo2(active_geometry))",
                      result["methods"])
        self.assertEqual(document.ActiveView.Translation3.ArrayData,
                         [1.0, 0.0, 0.0])

    def test_legacy_geometry_requires_normal_to_before_and_fit_after(self):
        automation = FakeAutomation()
        calls = []

        def verified(doc, zoom_to_fit=True):
            calls.append(bool(zoom_to_fit))
            return automation._result(True, "verified", data={
                "verified": True,
                "normal_to_verified": True,
                "fit_to_screen": {
                    "verified": True,
                    "verification_applicable": bool(zoom_to_fit),
                },
            })

        with patch.object(automation, "_auto_normal_to",
                          side_effect=verified):
            result = automation.draw_line(0, 0, 40, 10, unit="mm")

        self.assertTrue(result["success"])
        self.assertEqual(calls, [False, True])
        self.assertEqual(len(automation.doc.sketch.segments), 1)
        self.assertTrue(result["data"]["orientation"]
                        ["after_geometry"]["fit_to_screen"]["verified"])

    def test_legacy_geometry_aborts_before_mutation_when_view_is_unverified(self):
        automation = FakeAutomation()
        failed = automation._result(False, "unverified", data={
            "verified": False,
            "normal_to_verified": False,
            "fit_to_screen": {"verified": False},
        })

        with patch.object(automation, "_auto_normal_to",
                          return_value=failed) as mocked:
            result = automation.draw_line(0, 0, 40, 10, unit="mm")

        self.assertFalse(result["success"])
        self.assertFalse(result["data"]["geometry_created"])
        self.assertEqual(len(automation.doc.sketch.segments), 0)
        mocked.assert_called_once_with(automation.doc, zoom_to_fit=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
