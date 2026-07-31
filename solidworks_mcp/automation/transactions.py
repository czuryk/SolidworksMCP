"""Transactional execution, CAD plans, capabilities, and model graphs."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pythoncom
import win32com.client

from ..constants import SwErrors, SwSaveAsOptions
from .com_utils import (com_get, detect_modal_dialog, get_typed_module,
                        out_i4, null_dispatch)
from .runtime import atomic_json_write, default_state_dir


MCP_VERSION = "6.5.31"
MAX_PLAN_OPERATIONS = 100


logger = logging.getLogger(__name__)


class TransactionOperations:
    """Mixin providing safe compound operations over one COM apartment."""

    # Explicit whitelist.  Raw Python and recursive transaction/plan calls are
    # intentionally absent.
    PLAN_OPERATIONS = {
        "save_document", "create_parametric_sketch", "add_dimensions_batch",
        "advanced_extrude", "advanced_cut", "revolve_boss", "shell",
        "fillet_edges", "chamfer_edges", "reference_plane", "reference_axis",
        "linear_pattern", "circular_pattern", "mirror_feature",
        "rename_feature", "rename_body", "delete_feature", "show_body",
        "hide_body", "set_body_color", "set_body_transparency",
        "export_file", "export_bundle", "create_revolved_body",
        "create_swept_member", "create_multibody_insert",
        "create_semantic_primitive", "image_to_sketch",
        "export_sketch_geometry", "render_sketch_svg",
        "compare_sketches", "compare_sketch_to_reference",
        "compare_body_silhouette_to_image", "check_clearance",
        "body_volume", "probe_rays", "probe_section",
        "rename_new_body", "verify_named_body", "validate_sweep_path",
        "validate_sweep_profile", "create_sweep_feature",
    }

    @staticmethod
    def _elapsed_budget_exceeded(started: float,
                                 budget: Optional[Dict[str, Any]]) -> bool:
        limit = float((budget or {}).get("max_elapsed_sec", 300))
        if limit < 0:
            raise ValueError("max_elapsed_sec must be non-negative")
        return time.monotonic() - started >= limit

    def _walk_features_tx(self, doc):
        result = []
        feat = com_get(doc, "FirstFeature", default=None)
        guard = 0
        while feat is not None and guard < 10000:
            guard += 1
            result.append(feat)
            feat = com_get(feat, "GetNextFeature", default=None)
        return result

    def _persist_reference(self, doc, obj):
        try:
            raw = doc.Extension.GetPersistReference3(obj)
            if raw:
                return base64.b64encode(bytes(raw)).decode("ascii")
        except Exception:
            pass
        return None

    def _transaction_snapshot(self, doc,
                              include_persistent_ids: bool = False) -> Dict[str, Any]:
        """Capture rollback state without risky per-object persistent-ID COM calls.

        Feature/body names are unique in the transaction layer and are sufficient
        to identify objects created by a plan. Persistent references remain
        opt-in for diagnostics only: GetPersistReference3 can block indefinitely
        on a dirty SW2026 document after an interrupted feature operation.
        """
        features = []
        for index, feat in enumerate(self._walk_features_tx(doc)):
            features.append({
                "index": index,
                "name": str(com_get(feat, "Name", default="?")),
                "type": str(com_get(feat, "GetTypeName2", default="?")),
                "persistent_id": (self._persist_reference(doc, feat)
                                  if include_persistent_ids else None),
            })
        bodies = []
        try:
            for body in doc.GetBodies2(0, False) or []:
                bbox = com_get(body, "GetBodyBox", default=None)
                bodies.append({
                    "name": str(com_get(body, "Name", default="?")),
                    "bbox_m": list(bbox) if bbox else None,
                    "faces": len(com_get(body, "GetFaces", default=[]) or []),
                    "persistent_id": (self._persist_reference(doc, body)
                                      if include_persistent_ids else None),
                })
        except Exception:
            pass
        active_sketch = None
        try:
            sketch = doc.SketchManager.ActiveSketch
            if sketch is not None:
                sf = com_get(sketch, "GetFeature", default=None)
                active_sketch = com_get(sf, "Name", default="<active>")
        except Exception:
            pass
        path = self._get_doc_path(doc)
        return {
            "document": self._get_doc_title(doc), "path": path,
            "saved_mtime": os.path.getmtime(path) if path and os.path.exists(path)
            else None,
            "features": features, "bodies": bodies,
            "feature_count": len(features), "solid_body_count": len(bodies),
            "active_sketch": active_sketch,
            "captured_at": time.time(),
        }

    def _save_copy(self, doc, path: str) -> Dict[str, Any]:
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        errors, warnings = out_i4(), out_i4()
        ok = False
        try:
            ok = bool(doc.Extension.SaveAs(
                path, 0, int(SwSaveAsOptions.swSaveAsOptions_Copy),
                null_dispatch(), errors, warnings))
        except Exception:
            try:
                ok = bool(doc.SaveAs3(
                    path, 0, int(SwSaveAsOptions.swSaveAsOptions_Copy)))
            except Exception:
                ok = False
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        return {"success": bool(exists and (ok or exists)), "path": path,
                "size_bytes": os.path.getsize(path) if exists else 0,
                "errors": errors.value, "warnings": warnings.value}

    @staticmethod
    def _sha256(path):
        if not path or not os.path.exists(path):
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _create_checkpoint(self, doc, transaction_name: str,
                           checkpoint: Optional[Dict[str, Any]],
                           snapshot: Dict[str, Any], arguments=None):
        checkpoint = checkpoint or {}
        if checkpoint.get("mode") == "none":
            return {"success": True, "path": None, "manifest": None}
        path = checkpoint.get("path")
        if not path:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", transaction_name)
            path = str(default_state_dir() / "checkpoints" /
                       f"{int(time.time() * 1000)}_{safe_name}.SLDPRT")
        saved = self._save_copy(doc, path)
        if not saved["success"]:
            return saved
        try:
            sw_version = str(com_get(self._sw_app, "RevisionNumber",
                                     default="unknown"))
        except Exception:
            sw_version = "unknown"
        manifest = {
            "schema": "solidworks-mcp/checkpoint/v1",
            "transaction": transaction_name,
            "checkpoint": saved,
            "source": {"path": snapshot.get("path"),
                       "sha256": self._sha256(snapshot.get("path"))},
            "solidworks_version": sw_version, "mcp_version": MCP_VERSION,
            "unit": self._units.default_unit.value,
            "features": snapshot["features"], "bodies": snapshot["bodies"],
            "arguments": arguments or {}, "created_at": time.time(),
        }
        manifest_path = saved["path"] + ".manifest.json"
        atomic_json_write(manifest_path, manifest)
        saved["manifest"] = manifest_path
        keep_last = max(1, int(checkpoint.get(
            "keep_last_n", getattr(self._config, "keep_last_n_checkpoints", 10))))
        directory = Path(saved["path"]).parent
        checkpoints = sorted(directory.glob("*.SLDPRT"),
                             key=lambda item: item.stat().st_mtime,
                             reverse=True)
        removed = []
        for old in checkpoints[keep_last:]:
            try:
                old.unlink()
                sidecar = Path(str(old) + ".manifest.json")
                if sidecar.exists():
                    sidecar.unlink()
                removed.append(str(old))
            except OSError:
                pass
        saved["retention_removed"] = removed
        self._runtime.last_checkpoint_at = time.time()
        self._runtime.increment("checkpoints_created")
        return saved

    def _delete_created_features(self, doc, before, after):
        old_ids = {f.get("persistent_id") for f in before["features"]
                   if f.get("persistent_id")}
        old_names = {f["name"] for f in before["features"]}
        created = [f for f in after["features"] if
                   ((f.get("persistent_id") and f["persistent_id"] not in old_ids)
                    or (not f.get("persistent_id") and f["name"] not in old_names))]
        deleted = []
        failed = []
        for item in reversed(created):
            result = self.delete_feature(item["name"], delete_absorbed=True)
            if result.get("success"):
                deleted.append(item["name"])
            else:
                failed.append(item["name"])
        return deleted, failed

    @staticmethod
    def _snapshots_equivalent(left, right):
        return ({f["name"] for f in left["features"]} ==
                {f["name"] for f in right["features"]} and
                {b["name"] for b in left["bodies"]} ==
                {b["name"] for b in right["bodies"]} and
                left["solid_body_count"] == right["solid_body_count"])

    def _rollback_transaction(self, doc, before, checkpoint_path):
        after = self._transaction_snapshot(doc)
        deleted, failed = self._delete_created_features(doc, before, after)
        try:
            started = time.perf_counter()
            com_get(doc, "EditRebuild3", default=False)
            self.record_rebuild(time.perf_counter() - started)
        except Exception:
            pass
        current = self._transaction_snapshot(doc)
        restored = self._snapshots_equivalent(before, current)
        fallback = False
        fallback_error = None
        if not restored and checkpoint_path and os.path.exists(checkpoint_path):
            fallback = True
            title = self._get_doc_title(doc)
            try:
                self._sw_app.CloseDoc(title)
                opened = self.open_document(checkpoint_path)
                if opened.get("success"):
                    restored_doc, err = self.get_active_doc()
                    if err is None:
                        current = self._transaction_snapshot(restored_doc)
                        restored = self._snapshots_equivalent(before, current)
            except Exception as exc:
                fallback_error = str(exc)
        self._runtime.increment("rollbacks")
        return {"rolled_back": bool(restored), "document_restored": restored,
                "deleted_features": deleted, "delete_failures": failed,
                "checkpoint_reopened": fallback,
                "fallback_error": fallback_error, "current": current}

    def _call_operation(self, name: str, args: Dict[str, Any]) -> Dict:
        if name not in self.PLAN_OPERATIONS:
            return self._error(
                "INVALID_PLAN", f"Operation '{name}' is not whitelisted",
                recommended_actions=[
                    "Use a supported native CAD operation; arbitrary logic "
                    "belongs in execute_python_async outside a CAD plan."])
        args = dict(args or {})
        for common in ("budget", "idempotency_key", "allow_unsaved_document",
                       "save_path", "ui_guard"):
            args.pop(common, None)
        if name == "show_body":
            return self.set_body_visibility(args.get("name", ""), True)
        if name == "hide_body":
            return self.set_body_visibility(args.get("name", ""), False)
        method = getattr(self, name, None)
        if method is None:
            return self._error("CAPABILITY_UNAVAILABLE",
                               f"Operation '{name}' is unavailable")
        try:
            signature = inspect.signature(method)
            accepted = {k: v for k, v in args.items()
                        if k in signature.parameters}
            return method(**accepted)
        except Exception as exc:
            hresult = getattr(exc, "hresult", None)
            return self._error(
                "COM_MEMBER_MISMATCH", f"{name} failed: {exc}",
                com_hresult=hresult, recoverable=True,
                recommended_actions=[
                    "Inspect capabilities and the structured COM details; do "
                    "not retry more than once without changing the strategy."])

    @staticmethod
    def _resolve_step_refs(value, steps):
        if isinstance(value, str) and value.startswith("$steps."):
            parts = value.split(".")[1:]
            current = steps
            for part in parts:
                current = current[int(part)] if isinstance(current, list) else current[part]
            return current
        if isinstance(value, list):
            return [TransactionOperations._resolve_step_refs(v, steps)
                    for v in value]
        if isinstance(value, dict):
            return {k: TransactionOperations._resolve_step_refs(v, steps)
                    for k, v in value.items()}
        return value

    def _condition_matches(self, condition, steps):
        if not condition:
            return True
        if set(condition) - {"step_success", "body_exists", "feature_exists"}:
            raise ValueError("Unsafe/unknown plan condition")
        if "step_success" in condition:
            index = int(condition["step_success"])
            if index >= len(steps) or not steps[index].get("success"):
                return False
        doc, err = self.get_active_doc()
        if err:
            return False
        snapshot = self._transaction_snapshot(doc)
        if "body_exists" in condition and condition["body_exists"] not in {
                b["name"] for b in snapshot["bodies"]}:
            return False
        if "feature_exists" in condition and condition["feature_exists"] not in {
                f["name"] for f in snapshot["features"]}:
            return False
        return True

    def _validate_invariants(self, invariants, before, after, step_results):
        invariants = invariants or {}
        failures = []
        feature_names = {f["name"] for f in after["features"]}
        before_features = {f["name"] for f in before["features"]}
        body_names = {b["name"] for b in after["bodies"]}
        for name in invariants.get("expected_new_features", []):
            if name not in feature_names or name in before_features:
                failures.append(f"expected new feature '{name}'")
        for name in invariants.get("required_bodies", []):
            if name not in body_names:
                failures.append(f"required body '{name}'")
        expected_bodies = invariants.get("solid_body_count")
        count = after["solid_body_count"]
        if isinstance(expected_bodies, int) and count != expected_bodies:
            failures.append(f"solid_body_count={count}, expected {expected_bodies}")
        elif isinstance(expected_bodies, dict):
            if count < expected_bodies.get("min", count):
                failures.append("solid body count below minimum")
            if count > expected_bodies.get("max", count):
                failures.append("solid body count above maximum")
        for path in invariants.get("required_files", []):
            if not os.path.exists(path) or os.path.getsize(path) <= 0:
                failures.append(f"required file missing/empty: {path}")
        if invariants.get("no_modal_dialog", True) and detect_modal_dialog().get("modal"):
            failures.append("SolidWorks UI is blocked by a modal dialog")
        for sketch_name, expected in invariants.get("sketch_status", {}).items():
            status = self.analyze_sketch_dof(sketch_name,
                                             include_recommendations=False)
            actual = (status.get("data") or {}).get("status")
            if actual != expected:
                failures.append(
                    f"sketch '{sketch_name}' status={actual}, expected={expected}")
        return failures

    def run_transaction(self, name: str, operations: List[Dict[str, Any]],
                        checkpoint: Dict[str, Any] = None,
                        invariants: Dict[str, Any] = None,
                        on_failure: str = "rollback",
                        idempotency_key: str = None,
                        save_policy: Dict[str, Any] = None,
                        budget: Dict[str, Any] = None,
                        allow_unsaved_document: bool = False,
                        save_path: str = None) -> Dict:
        cached = self._runtime.idempotent_get(idempotency_key)
        if cached is not None:
            cached.setdefault("data", {})["idempotent_replay"] = True
            return cached
        if not name:
            return self._error("INVALID_PLAN", "Transaction name is required")
        if not isinstance(operations, list) or len(operations) > MAX_PLAN_OPERATIONS:
            return self._error(
                "INVALID_PLAN",
                f"operations must be a list with at most {MAX_PLAN_OPERATIONS} items")
        doc, err = self.get_active_doc()
        if err:
            return err
        if not self._get_doc_path(doc):
            if save_path:
                saved = self.save_document(save_path)
                if not saved.get("success"):
                    return saved
                doc, _ = self.get_active_doc()
            elif not allow_unsaved_document:
                return self._error(
                    "DOCUMENT_UNSAVED",
                    "Compound mutation requires save_path or explicit "
                    "allow_unsaved_document=true",
                    recommended_actions=["Provide an absolute .SLDPRT save_path."])
        save_policy = save_policy or {}
        if save_policy.get("save_before"):
            save_before_result = self.save_document()
            if not save_before_result.get("success"):
                return save_before_result
        logger.info("Transaction name=%s stage=snapshot_before begin", name)
        before = self._transaction_snapshot(doc)
        logger.info(
            "Transaction name=%s stage=snapshot_before complete features=%s bodies=%s",
            name, before["feature_count"], before["solid_body_count"])
        tx_id = uuid.uuid4().hex
        self._runtime.active_transaction_id = tx_id
        effective_checkpoint = checkpoint
        if save_policy.get("save_checkpoint") is False:
            effective_checkpoint = {"mode": "none"}
        elif save_policy.get("keep_last_n_checkpoints") is not None:
            effective_checkpoint = dict(checkpoint or {})
            effective_checkpoint["keep_last_n"] = save_policy[
                "keep_last_n_checkpoints"]
        logger.info("Transaction id=%s name=%s stage=checkpoint begin mode=%s",
                    tx_id, name, (effective_checkpoint or {}).get("mode"))
        checkpoint_result = self._create_checkpoint(
            doc, name, effective_checkpoint, before,
            {"operations": operations, "invariants": invariants})
        logger.info(
            "Transaction id=%s name=%s stage=checkpoint complete success=%s path=%s",
            tx_id, name, bool(checkpoint_result.get("success")),
            checkpoint_result.get("path"))
        if not checkpoint_result.get("success"):
            self._runtime.active_transaction_id = None
            return self._error(
                "TRANSACTION_ROLLBACK_FAILED",
                "Could not create the required checkpoint",
                recoverable=False, details=checkpoint_result)
        steps = []
        started = time.monotonic()
        failure = None
        logger.info("Transaction id=%s name=%s stage=execute begin steps=%s",
                    tx_id, name, len(operations))
        try:
            for index, operation in enumerate(operations):
                if self._elapsed_budget_exceeded(started, budget):
                    self._runtime.increment("budget_exceeded")
                    failure = self._error(
                        "BUDGET_EXCEEDED", "Transaction elapsed-time budget exceeded",
                        details={"step": index})
                    break
                if not isinstance(operation, dict) or "op" not in operation:
                    failure = self._error("INVALID_PLAN",
                                          f"Invalid operation at index {index}")
                    break
                if not self._condition_matches(operation.get("when"), steps):
                    steps.append({"success": True, "skipped": True,
                                  "message": "condition did not match"})
                    continue
                args = self._resolve_step_refs(operation.get("args", {}), steps)
                step_started = time.monotonic()
                logger.info(
                    "Transaction id=%s name=%s step=%s op=%s stage=begin",
                    tx_id, name, index, operation["op"])
                step = self._call_operation(operation["op"], args)
                logger.info(
                    "Transaction id=%s name=%s step=%s op=%s "
                    "stage=complete success=%s elapsed_ms=%.3f",
                    tx_id, name, index, operation["op"],
                    bool(step.get("success")),
                    (time.monotonic() - step_started) * 1000.0)
                step.setdefault("data", {})["step_index"] = index
                step["data"]["operation"] = operation["op"]
                steps.append(step)
                if not step.get("success") and not operation.get(
                        "continue_on_failure", False):
                    failure = step
                    break
            logger.info("Transaction id=%s name=%s stage=snapshot_after begin",
                        tx_id, name)
            after = self._transaction_snapshot(doc)
            logger.info("Transaction id=%s name=%s stage=snapshot_after complete",
                        tx_id, name)
            invariant_failures = [] if failure else self._validate_invariants(
                invariants, before, after, steps)
            if invariant_failures:
                failure = self._error(
                    "INVARIANT_FAILED", "Transaction invariant failed",
                    details={"failures": invariant_failures})
            if failure:
                rollback = ({"rolled_back": False, "document_restored": False}
                            if on_failure != "rollback" else
                            self._rollback_transaction(
                                doc, before, checkpoint_result.get("path")))
                code = ((failure.get("data") or {}).get("error") or {}).get(
                    "code", "INVARIANT_FAILED")
                result = self._error(
                    code, f"Transaction '{name}' failed",
                    document_restored=rollback.get("document_restored"),
                    details={"transaction_id": tx_id, "committed": False,
                             "rolled_back": rollback.get("rolled_back"),
                             "checkpoint": checkpoint_result,
                             "changed_objects": self._diff_snapshots(before, after),
                             "steps": steps, "cause": failure,
                             "rollback": rollback})
                return result
            save_result = None
            if save_policy.get("save_after_success", True):
                save_result = self.save_document()
                if save_result.get("success"):
                    self._runtime.increment("files_saved")
                    if after["solid_body_count"]:
                        self._runtime.last_saved_body_at = time.time()
            result = self._result(
                True, f"Transaction '{name}' committed", SwErrors.swSuccess,
                {"transaction_id": tx_id, "committed": True,
                 "rolled_back": False, "checkpoint": checkpoint_result,
                 "changed_objects": self._diff_snapshots(before, after),
                 "steps": steps, "invariants": "passed",
                 "save_result": save_result,
                 "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
            self._runtime.idempotent_put(idempotency_key, result)
            logger.info("Transaction id=%s name=%s stage=committed", tx_id, name)
            return result
        except Exception as exc:
            after = self._transaction_snapshot(doc)
            rollback = self._rollback_transaction(
                doc, before, checkpoint_result.get("path"))
            return self._error(
                "COM_MEMBER_MISMATCH", f"Transaction '{name}' crashed: {exc}",
                com_hresult=getattr(exc, "hresult", None),
                document_restored=rollback.get("document_restored"),
                details={"transaction_id": tx_id, "committed": False,
                         "rolled_back": rollback.get("rolled_back"),
                         "checkpoint": checkpoint_result, "steps": steps,
                         "rollback": rollback})
        finally:
            self._runtime.active_transaction_id = None

    @staticmethod
    def _diff_snapshots(before, after):
        bf, af = {f["name"] for f in before["features"]}, {
            f["name"] for f in after["features"]}
        bb, ab = {b["name"] for b in before["bodies"]}, {
            b["name"] for b in after["bodies"]}
        return {"features_created": sorted(af - bf),
                "features_deleted": sorted(bf - af),
                "bodies_created": sorted(ab - bb),
                "bodies_deleted": sorted(bb - ab)}

    def execute_cad_plan(self, plan_id: str, operations: List[Dict[str, Any]],
                         transaction: Dict[str, Any] = None,
                         invariants: Dict[str, Any] = None,
                         unit: str = None, budget: Dict[str, Any] = None,
                         allow_unsaved_document: bool = False,
                         save_path: str = None) -> Dict:
        if unit:
            for operation in operations or []:
                operation.setdefault("args", {}).setdefault("unit", unit)
        transaction = transaction or {}
        return self.run_transaction(
            name=plan_id, operations=operations,
            checkpoint={"mode": "save_copy"} if transaction.get(
                "checkpoint_before", True) else {"mode": "none"},
            invariants=invariants,
            on_failure="rollback" if transaction.get(
                "rollback_on_failure", True) else "leave_partial",
            idempotency_key=plan_id, budget=budget,
            allow_unsaved_document=allow_unsaved_document,
            save_path=save_path)

    def get_session_metrics(self) -> Dict:
        return self._result(True, "Session metrics", SwErrors.swSuccess,
                            self._runtime.report())

    def get_capabilities(self) -> Dict:
        modules = {name: importlib.util.find_spec(name) is not None for name in
                   ("cv2", "numpy", "scipy", "skimage", "PIL", "shapely")}
        version = "not connected"
        if self._sw_app is not None:
            version = str(com_get(self._sw_app, "RevisionNumber",
                                  default="unknown"))
        limitations = [
            "Perspective images require explicit homography or trace_as_is.",
            "Region matting supports silhouette modes; stroke_centerlines, "
            "stroke_edges, and all_visible_edges require the separate line-art backend.",
            "Existing sweep paths/profiles must be named sketches and require "
            "SOLIDWORKS read-back. Newly declared paths are validated before "
            "COM. On SW2026 circular members use a materialized circle sketch; "
            "the special circular-profile API is disabled because it can block COM.",
            "Persistent IDs can become invalid after topology-changing rebuilds.",
            "Unknown modal dialogs are never auto-confirmed.",
            "Autotrace and Picture to Sketch are interactive PropertyManager "
            "features and expose no public API entry point.",
            "Equation B-splines above the per-entity control-point cap are "
            "rejected before COM because SW2026 import cost grows nonlinearly.",
            "SW2026 curve-parameter and tessellation read-back can block for "
            "minutes on dense fit-splines; construction-reference image "
            "commits therefore batch explicit NURBS per loop and use bounded "
            "metadata/endpoint read-back.",
        ]
        try:
            from .deep_vectorization import capability_report
            deep_vectorization = capability_report()
        except Exception as exc:
            deep_vectorization = {"available": False, "error": str(exc)}
        return self._result(True, "Capabilities", SwErrors.swSuccess, {
            "solidworks_version": version, "mcp_version": MCP_VERSION,
            "typed_interfaces": get_typed_module() is not None,
            "operations": sorted(self.PLAN_OPERATIONS | {
                "run_transaction", "execute_cad_plan", "analyze_sketch_dof",
                "get_session_metrics", "get_capabilities", "sync_model_graph"}),
            "geometry_backend": {"shapely": modules["shapely"],
                                 "scipy": modules["scipy"]},
            "image_backend": modules,
            "deep_vectorization": deep_vectorization,
            "vectorization_controls": {
                "projection_modes": [
                    "orthographic", "homography", "trace_as_is"],
                "output_modes": [
                    "locked_trace", "minimal_parametric",
                    "reference_spline", "construction_reference"],
                "homography_requires_ordered_source_quad": True,
                "post_com_reverse_raster_required": True,
                "default_max_control_points_per_spline": 64,
                "cad_cost_model": "batched_composite_nurbs_with_bounded_readback",
            },
            "body_silhouette_comparison": {
                "default_candidate_source": "native_mesh",
                "candidate_sources": ["native_mesh",
                                      "screenshot_segmentation"],
                "native_mesh_projection": "selected_body_stl_triangle_union",
                "viewport_role": "independent_visual_evidence",
                "requires_explicit_metric_transform": True,
            },
            "max_plan_operations": MAX_PLAN_OPERATIONS,
            "export_formats": ["sldprt", "step", "stp", "stl", "iges",
                               "x_t", "x_b", "3mf", "sat", "wrl", "ply"],
            "known_limitations": limitations,
        })

    def recover_environment(self, retry_operation: Dict[str, Any] = None,
                            max_retries: int = 1) -> Dict:
        max_retries = max(0, min(int(max_retries), 1))
        actions = []
        ui = detect_modal_dialog()
        if ui.get("modal"):
            return self._error(
                "MODAL_DIALOG_BLOCKING",
                "Recovery stopped because a dialog requires explicit policy",
                details={"ui": ui, "actions": actions})
        doc, err = self.get_active_doc()
        if err:
            return err
        try:
            doc.ClearSelection2(True)
            actions.append("selection_cleared")
        except Exception:
            pass
        try:
            if doc.SketchManager.ActiveSketch is not None:
                doc.SketchManager.InsertSketch(True)
                actions.append("sketch_edit_exited")
        except Exception:
            pass
        freeze = self.ensure_features_not_frozen(doc)
        actions.append("freeze_bar_checked")
        retried = None
        if retry_operation and max_retries:
            retried = self._call_operation(retry_operation.get("op", ""),
                                           retry_operation.get("args", {}))
        return self._result(True, "Environment recovery completed",
                            SwErrors.swSuccess,
                            {"state": "UI_RECOVERED", "actions": actions,
                             "freeze_bar": freeze, "retry": retried})

    def sync_model_graph(self, graph_id: str, nodes: List[Dict[str, Any]],
                         mode: str = "apply", invariants=None,
                         save_path: str = None,
                         allow_unsaved_document: bool = False) -> Dict:
        if mode not in {"plan", "apply"}:
            return self._error("INVALID_PLAN", "mode must be plan or apply")
        node_by_id = {}
        for node in nodes or []:
            node_id = node.get("id")
            if not node_id or node_id in node_by_id:
                return self._error("INVALID_PLAN", "Node IDs must be unique")
            node_by_id[node_id] = node
        visiting, visited, order = set(), set(), []

        def visit(node_id):
            if node_id in visiting:
                raise ValueError(f"Cycle detected at '{node_id}'")
            if node_id in visited:
                return
            if node_id not in node_by_id:
                raise ValueError(f"Unknown dependency '{node_id}'")
            visiting.add(node_id)
            for dep in node_by_id[node_id].get("depends_on", []):
                visit(dep)
            visiting.remove(node_id)
            visited.add(node_id)
            order.append(node_id)

        try:
            for node_id in node_by_id:
                visit(node_id)
        except ValueError as exc:
            return self._error("INVALID_PLAN", str(exc))
        doc, err = self.get_active_doc()
        if err:
            return err
        current = self._transaction_snapshot(doc)
        feature_names = {f["name"] for f in current["features"]}
        operations, diff = [], []
        previous = self._runtime.model_graphs.get(graph_id, {})
        for node_id in order:
            node = node_by_id[node_id]
            digest = hashlib.sha256(json.dumps(
                node, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            expected = node.get("expected_feature")
            unchanged = previous.get(node_id) == digest and (
                not expected or expected in feature_names)
            action = "unchanged" if unchanged else "create_or_update"
            diff.append({"id": node_id, "action": action,
                         "expected_feature": expected})
            if not unchanged:
                operations.append({"op": node.get("op"),
                                   "args": node.get("args", {})})
        if mode == "plan":
            return self._result(True, "Model graph diff", SwErrors.swSuccess,
                                {"graph_id": graph_id, "order": order,
                                 "diff": diff, "operations": operations})
        desired_digest = hashlib.sha256(json.dumps(
            {node_id: node_by_id[node_id] for node_id in order},
            sort_keys=True, default=str).encode("utf-8")).hexdigest()
        result = self.execute_cad_plan(
            f"model_graph:{graph_id}:{desired_digest[:16]}", operations,
            transaction={"checkpoint_before": True,
                         "rollback_on_failure": True},
            invariants=invariants, save_path=save_path,
            allow_unsaved_document=allow_unsaved_document)
        if result.get("success"):
            self._runtime.model_graphs[graph_id] = {
                node_id: hashlib.sha256(json.dumps(
                    node_by_id[node_id], sort_keys=True, default=str
                ).encode("utf-8")).hexdigest() for node_id in order}
            result.setdefault("data", {})["graph_diff"] = diff
            result["data"]["desired_graph_sha256"] = desired_digest
        return result
