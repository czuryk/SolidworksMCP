"""Safety, observability, budgets, and structured error primitives.

This module intentionally contains no feature-specific SolidWorks logic.  It
is the common layer used by every new native tool and by the server boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Dict, Optional

from ..constants import SwErrors
from .com_utils import (com_get, detect_modal_dialog,
                        resolve_solidworks_constant)


ERROR_DEFAULTS = {
    "MODAL_DIALOG_BLOCKING": ("ui_guard", True),
    "FREEZE_BAR_STATE_INVALID": ("preflight", True),
    "COM_MEMBER_MISMATCH": ("com_call", True),
    "SKETCH_UNDERDEFINED": ("solve", True),
    "SKETCH_OVERDEFINED": ("solve", True),
    "SKETCH_OPEN_CONTOUR": ("validate_topology", True),
    "SKETCH_SELF_INTERSECTION": ("validate_topology", True),
    "FEATURE_DEAD": ("verify_feature", True),
    "FEATURE_WRONG_BBOX": ("verify_feature", True),
    "UNEXPECTED_BODY_MERGE": ("verify_feature", True),
    "BODY_INTERFERENCE": ("verify_body", True),
    "DOCUMENT_UNSAVED": ("preflight", True),
    "TRANSACTION_ROLLBACK_FAILED": ("rollback", False),
    "IMAGE_LOW_CONFIDENCE": ("validate_image", True),
    "REFERENCE_MISMATCH": ("validate_reference", True),
    "BUDGET_EXCEEDED": ("budget", True),
    "INVALID_PLAN": ("validate_plan", True),
    "INVARIANT_FAILED": ("validate_invariants", True),
    "CAPABILITY_UNAVAILABLE": ("capability", False),
}


def structured_error(code: str, message: str, *, stage: Optional[str] = None,
                     recoverable: Optional[bool] = None,
                     document_restored: Optional[bool] = None,
                     com_hresult: Optional[int] = None,
                     likely_causes=None, conflicting_entities=None,
                     recommended_actions=None, debug_artifacts=None,
                     details=None) -> Dict[str, Any]:
    default_stage, default_recoverable = ERROR_DEFAULTS.get(
        code, ("unknown", False))
    payload = {
        "code": code,
        "message": message,
        "stage": stage or default_stage,
        "recoverable": (default_recoverable if recoverable is None
                        else bool(recoverable)),
        "document_restored": document_restored,
        "com_hresult": com_hresult,
        "likely_causes": list(likely_causes or []),
        "conflicting_entities": list(conflicting_entities or []),
        "recommended_actions": list(recommended_actions or []),
        "debug_artifacts": list(debug_artifacts or []),
    }
    if details:
        payload["details"] = details
    return payload


def error_result(owner, code: str, message: str, **kwargs) -> Dict[str, Any]:
    data = dict(kwargs.pop("data", {}) or {})
    data["error"] = structured_error(code, message, **kwargs)
    return owner._result(False, message, SwErrors.swUnknownError, data)


def enrich_legacy_error(result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach a structured code to legacy native-tool failures."""
    if result.get("success"):
        return result
    data = result.setdefault("data", {})
    if data.get("error"):
        return result
    message = str(result.get("message", ""))
    upper = message.upper()
    mappings = [
        ("DEAD FEATURE", "FEATURE_DEAD"),
        ("0 FACES", "FEATURE_DEAD"),
        ("OUTSIDE THE EXPECTED", "FEATURE_WRONG_BBOX"),
        ("MERGED UNEXPECTED", "UNEXPECTED_BODY_MERGE"),
        ("INTERFERENCE", "BODY_INTERFERENCE"),
        ("MODAL DIALOG", "MODAL_DIALOG_BLOCKING"),
        ("SELF-INTERSECT", "SKETCH_SELF_INTERSECTION"),
        ("NOT CLOSED", "SKETCH_OPEN_CONTOUR"),
        ("SAVE FAILED", "DOCUMENT_UNSAVED"),
        ("FREEZE BAR", "FREEZE_BAR_STATE_INVALID"),
    ]
    code = next((candidate for needle, candidate in mappings
                 if needle in upper), "COM_MEMBER_MISMATCH")
    data["error"] = structured_error(
        code, message, com_hresult=data.get("com_hresult"),
        details={"legacy_error_code": result.get("error_code"),
                 "legacy_error_name": result.get("error_name")})
    return result


class RuntimeState:
    """Thread-safe session state shared by tools in one MCP process."""

    def __init__(self):
        now = time.time()
        self._lock = threading.RLock()
        self.session_id = uuid.uuid4().hex
        self.session_started_at = now
        self.last_progress_at = now
        self.last_new_solid_body_at = None
        self.last_saved_body_at = None
        self.last_checkpoint_at = None
        self.last_safe_operation = None
        self.active_operation = None
        self.active_transaction_id = None
        self.session_preferences = {}
        self.idempotency = {}
        self.entities = {}
        self.calibrations = {}
        self.model_graphs = {}
        self.metrics = {
            "mcp_calls": 0,
            "com_operations": 0,
            "rebuilds": 0,
            "solver_time_sec": 0.0,
            "rollbacks": 0,
            "features_created": 0,
            "features_deleted": 0,
            "files_saved": 0,
            "checkpoints_created": 0,
            "ui_blocks": 0,
            "budget_exceeded": 0,
            "verification_artifacts": 0,
            "cad_result_files": 0,
        }
        self.per_tool = {}

    def observe_com(self, operation, member):
        with self._lock:
            self.metrics["com_operations"] += 1

    def begin_tool(self, name: str, arguments: Dict[str, Any], snapshot=None):
        with self._lock:
            self.metrics["mcp_calls"] += 1
            stats = self.per_tool.setdefault(name, {
                "calls": 0, "successes": 0, "failures": 0,
                "elapsed_sec": 0.0})
            stats["calls"] += 1
            token = {
                "name": name, "started_at": time.perf_counter(),
                "wall_started_at": time.time(), "snapshot": snapshot,
                "arguments_hash": hashlib.sha256(json.dumps(
                    arguments or {}, sort_keys=True, default=str
                ).encode("utf-8")).hexdigest(),
            }
            self.active_operation = copy.deepcopy(token)
            return token

    def finish_tool(self, token, success: bool, snapshot=None):
        elapsed = max(0.0, time.perf_counter() - token["started_at"])
        with self._lock:
            stats = self.per_tool.setdefault(token["name"], {
                "calls": 1, "successes": 0, "failures": 0,
                "elapsed_sec": 0.0})
            stats["elapsed_sec"] += elapsed
            stats["successes" if success else "failures"] += 1
            before = token.get("snapshot") or {}
            after = snapshot or {}
            feature_delta = int(after.get("feature_count", 0) or 0) - int(
                before.get("feature_count", 0) or 0)
            if feature_delta > 0:
                self.metrics["features_created"] += feature_delta
            elif feature_delta < 0:
                self.metrics["features_deleted"] += -feature_delta
            body_delta = int(after.get("solid_body_count", 0) or 0) - int(
                before.get("solid_body_count", 0) or 0)
            if body_delta > 0:
                self.last_new_solid_body_at = time.time()
            if success:
                self.last_safe_operation = {
                    "tool": token["name"], "finished_at": time.time(),
                    "arguments_hash": token["arguments_hash"]}
                self.last_progress_at = time.time()
            self.active_operation = None
        return elapsed

    def increment(self, name: str, amount=1):
        with self._lock:
            self.metrics[name] = self.metrics.get(name, 0) + amount
            self.last_progress_at = time.time()

    def register_entities(self, document_key, sketch_name, entity_records):
        with self._lock:
            self.entities[(document_key, sketch_name)] = entity_records

    def get_entities(self, document_key, sketch_name):
        with self._lock:
            return self.entities.get((document_key, sketch_name), {})

    def idempotent_get(self, key):
        if not key:
            return None
        with self._lock:
            value = self.idempotency.get(key)
            return copy.deepcopy(value) if value is not None else None

    def idempotent_put(self, key, result):
        if key:
            with self._lock:
                self.idempotency[key] = copy.deepcopy(result)

    def report(self):
        with self._lock:
            now = time.time()
            metrics = copy.deepcopy(self.metrics)
            metrics.update({
                "session_id": self.session_id,
                "session_elapsed_sec": round(now - self.session_started_at, 3),
                "seconds_since_last_progress": round(
                    now - self.last_progress_at, 3),
                "seconds_since_last_new_solid_body": (
                    round(now - self.last_new_solid_body_at, 3)
                    if self.last_new_solid_body_at else None),
                "seconds_since_last_checkpoint": (
                    round(now - self.last_checkpoint_at, 3)
                    if self.last_checkpoint_at else None),
                "verification_to_cad_result_ratio": (
                    metrics["verification_artifacts"] /
                    max(1, metrics["cad_result_files"])),
                "last_safe_operation": copy.deepcopy(self.last_safe_operation),
                "active_operation": copy.deepcopy(self.active_operation),
                "active_transaction_id": self.active_transaction_id,
                "per_tool": copy.deepcopy(self.per_tool),
            })
            return metrics

    def budget_violation(self, budget: Optional[Dict[str, Any]],
                         *, rebuilds=0, solver_time=0.0,
                         rollbacks=0) -> Optional[Dict[str, Any]]:
        budget = budget or {}
        checks = [
            ("max_rebuilds_per_sketch", rebuilds),
            ("max_solver_time_per_sketch_sec", solver_time),
            ("max_rollbacks_per_component", rollbacks),
        ]
        now = time.time()
        anchor = self.last_saved_body_at or self.session_started_at
        checks.append(("max_elapsed_without_saved_body_sec", now - anchor))
        for limit_name, actual in checks:
            limit = budget.get(limit_name)
            if limit is not None and float(actual) > float(limit):
                self.increment("budget_exceeded")
                return {
                    "limit": limit_name, "allowed": limit, "actual": actual,
                    "warn_only": bool(budget.get("warn_only", False)),
                    "last_checkpoint_at": self.last_checkpoint_at,
                    "recommended_strategy": (
                        "Reduce the plan, restore the last checkpoint, and "
                        "create a verified saved body before adding detail."),
                }
        return None


class DimensionInputGuard(AbstractContextManager):
    """Suppress the SolidWorks Modify dialog while dimensions are created."""

    def __init__(self, sw, runtime: RuntimeState,
                 policy: str = "operation_scoped"):
        if policy not in {"operation_scoped", "session_scoped",
                          "leave_disabled"}:
            raise ValueError(f"Unknown dimension guard policy: {policy}")
        self.sw = sw
        self.runtime = runtime
        self.policy = policy
        self.constant = None
        self.original = None
        self.disabled_verified = False
        self.restored = None

    def __enter__(self):
        self.constant = resolve_solidworks_constant("swInputDimValOnCreate")
        self.original = bool(com_get(
            self.sw, "GetUserPreferenceToggle", self.constant, default=True))
        ok = bool(com_get(self.sw, "SetUserPreferenceToggle",
                          self.constant, False, default=False))
        current = bool(com_get(
            self.sw, "GetUserPreferenceToggle", self.constant, default=True))
        if not ok and current:
            raise RuntimeError("Could not disable swInputDimValOnCreate")
        if current:
            raise RuntimeError("swInputDimValOnCreate read-back is still enabled")
        self.disabled_verified = True
        if self.policy == "session_scoped":
            self.runtime.session_preferences.setdefault(
                "swInputDimValOnCreate", self.original)
        return self

    def __exit__(self, exc_type, exc_value, traceback_value):
        if self.policy == "operation_scoped" and self.constant is not None:
            com_get(self.sw, "SetUserPreferenceToggle", self.constant,
                    bool(self.original), default=False)
            restored = bool(com_get(
                self.sw, "GetUserPreferenceToggle", self.constant,
                default=not bool(self.original)))
            self.restored = restored == bool(self.original)
            if restored != bool(self.original) and exc_value is None:
                raise RuntimeError(
                    "Could not restore swInputDimValOnCreate preference")
        elif self.policy in {"session_scoped", "leave_disabled"}:
            self.restored = False
        return False


def default_state_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "solidworks-mcp-state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_json_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return str(path)
