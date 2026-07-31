"""
SolidWorks Automation Base
--------------------------
Core automation class with connection management and utility methods.
"""

import os
import time
import logging
import datetime
import traceback
from typing import Optional, Dict, Any, Tuple

# COM imports
import win32com.client
import pythoncom

from ..constants import (SwErrors, SwPlanes, SwDocumentTypes, SwViews,
                         SwUserPreferenceToggle, SwMoveFreezeBarTo)
from ..config import get_config
from ..utils import UnitConverter, find_solidworks, find_template
from .com_utils import com_get, detect_modal_dialog
from .com_utils import set_com_observer, resolve_solidworks_constant
from .runtime import (RuntimeState, DimensionInputGuard, error_result,
                      structured_error)

logger = logging.getLogger(__name__)


class SolidWorksAutomation:
    """
    Core SolidWorks automation class

    Handles connection management, document operations, and provides
    utility methods for all automation tasks.
    """

    def __init__(self):
        """Initialize automation instance"""
        self._sw_app = None
        self._connected = False
        self._config = get_config()
        self._units = UnitConverter(self._config.default_unit)
        self._sw_exe_path = None
        self._runtime = RuntimeState()
        set_com_observer(self._runtime.observe_com)

        logger.info("SolidWorksAutomation initialized")

    # ========================================================================
    # Properties
    # ========================================================================

    @property
    def is_connected(self) -> bool:
        """Check if connected to SolidWorks"""
        if not self._connected or self._sw_app is None:
            return False

        try:
            # Test connection (RevisionNumber may be property or method)
            _ = com_get(self._sw_app, "RevisionNumber")
            return True
        except:
            self._connected = False
            self._sw_app = None
            return False

    @property
    def units(self) -> UnitConverter:
        """Get unit converter"""
        return self._units

    @property
    def app(self):
        """Get SolidWorks application object"""
        return self._sw_app

    # ========================================================================
    # Result Helper
    # ========================================================================

    def _result(self, success: bool, message: str,
                error_code: SwErrors = SwErrors.swSuccess,
                data: Optional[Dict] = None) -> Dict:
        """
        Create standardized result dictionary

        Args:
            success: Operation success status
            message: Human-readable message
            error_code: Error code enum
            data: Optional additional data

        Returns:
            Standardized result dictionary
        """
        result = {
            "success": success,
            "message": message,
            "error_code": int(error_code),
            "error_name": error_code.name,
            "timestamp": datetime.datetime.now().isoformat()
        }
        if data:
            result["data"] = data
        return result

    def _error(self, code: str, message: str, **kwargs) -> Dict:
        """Return a standard result with a machine-actionable error payload."""
        return error_result(self, code, message, **kwargs)

    def dimension_input_guard(self, policy: str = "operation_scoped"):
        return DimensionInputGuard(self._sw_app, self._runtime, policy)

    def _document_snapshot(self, doc=None) -> Dict:
        """Cheap best-effort state used for telemetry and transaction guards."""
        if doc is None:
            try:
                doc = self._sw_app.ActiveDoc if self._sw_app else None
            except Exception:
                doc = None
        if doc is None:
            return {"document": None, "feature_count": 0,
                    "solid_body_count": 0}
        snapshot = {
            "document": self._get_doc_title(doc),
            "path": self._get_doc_path(doc),
            "feature_count": 0,
            "solid_body_count": 0,
            "active_sketch": None,
        }
        try:
            snapshot["feature_count"] = int(com_get(
                doc, "GetFeatureCount", default=0) or 0)
        except Exception:
            pass
        try:
            bodies = doc.GetBodies2(0, False)
            snapshot["solid_body_count"] = len(bodies) if bodies else 0
        except Exception:
            pass
        try:
            sketch = doc.SketchManager.ActiveSketch
            if sketch is not None:
                feat = com_get(sketch, "GetFeature", default=None)
                snapshot["active_sketch"] = com_get(
                    feat, "Name", default="<active sketch>")
        except Exception:
            pass
        return snapshot

    def ui_preflight(self, tool_name: str, arguments=None,
                     *, mutating: bool = False) -> Optional[Dict]:
        """Block mutation when an owned SolidWorks popup is already present."""
        if not mutating:
            return None
        ui = detect_modal_dialog()
        if ui.get("modal"):
            self._runtime.increment("ui_blocks")
            return self._error(
                "MODAL_DIALOG_BLOCKING",
                f"SolidWorks UI is blocked before '{tool_name}'",
                stage="preflight", recoverable=True,
                recommended_actions=[
                    "Resolve the reported dialog, or call an explicitly "
                    "whitelisted UI recovery operation."],
                details={"ui": ui, "caused_by": {
                    "tool": tool_name,
                    "transaction_id": self._runtime.active_transaction_id}})
        return None

    def ui_postflight(self, tool_name: str, result: Dict,
                      *, mutating: bool = False) -> Dict:
        if not mutating:
            return result
        ui = detect_modal_dialog()
        if not ui.get("modal"):
            return result
        self._runtime.increment("ui_blocks")
        artifacts = []
        try:
            from .runtime import default_state_dir
            shot = default_state_dir() / (
                f"ui-{int(time.time() * 1000)}-{tool_name}.png")
            captured = self.take_screenshot(
                str(shot), full_window=True, compress=True)
            if captured.get("success") and shot.exists():
                artifacts.append(str(shot))
                self._runtime.increment("verification_artifacts")
        except Exception:
            pass
        return self._error(
            "MODAL_DIALOG_BLOCKING",
            f"SolidWorks UI became blocked during '{tool_name}'",
            stage="postflight", recoverable=True,
            debug_artifacts=artifacts,
            details={"ui": ui, "caused_by": {
                "tool": tool_name,
                "transaction_id": self._runtime.active_transaction_id},
                "partial_result": result})

    def record_rebuild(self, elapsed_sec: float = 0.0):
        self._runtime.increment("rebuilds")
        if elapsed_sec:
            self._runtime.increment("solver_time_sec", float(elapsed_sec))

    def restore_session_preferences(self):
        original = self._runtime.session_preferences.pop(
            "swInputDimValOnCreate", None)
        if original is None or self._sw_app is None:
            return
        try:
            enum_value = resolve_solidworks_constant("swInputDimValOnCreate")
            com_get(self._sw_app, "SetUserPreferenceToggle", enum_value,
                    bool(original), default=False)
        except Exception:
            logger.exception("Failed to restore session-scoped preferences")

    # ========================================================================
    # Connection Methods
    # ========================================================================

    def _try_connect_com(self) -> bool:
        """
        Try multiple COM connection methods

        Returns:
            True if connection successful
        """
        methods = [
            # Method 1: GetObject (running instance)
            lambda: win32com.client.GetObject(Class="SldWorks.Application"),
            # Method 2: Dispatch (creates or gets existing)
            lambda: win32com.client.Dispatch("SldWorks.Application"),
            # Method 3: Dynamic Dispatch
            lambda: win32com.client.dynamic.Dispatch("SldWorks.Application"),
            # Method 4: GetActiveObject
            lambda: win32com.client.GetActiveObject("SldWorks.Application"),
        ]

        for i, method in enumerate(methods):
            try:
                logger.debug(f"Trying connection method {i+1}...")
                pythoncom.CoInitialize()
                app = method()

                if app is not None:
                    # Force DYNAMIC dispatch. Once the makepy module for
                    # sldworks.tlb is cached (get_typed_module), Dispatch()
                    # returns TYPED gen_py objects where GetTitle/FirstFeature/
                    # RevisionNumber etc. become METHODS - which breaks all the
                    # property-style traversal the codebase relies on. Wrapping
                    # the raw IDispatch as dynamic restores property access for
                    # the app and all descendant objects (doc, sketch, feature).
                    # Typed wrappers are still obtained on demand via typed().
                    try:
                        app = win32com.client.dynamic.Dispatch(app._oleobj_)
                    except Exception as e:
                        logger.debug(f"Force-dynamic wrap failed: {e}")

                    self._sw_app = app
                    self._sw_app.Visible = True

                    version = com_get(self._sw_app, "RevisionNumber",
                                      default="unknown")
                    logger.info(f"Connected via method {i+1}: {version}")
                    self._connected = True
                    return True

            except Exception as e:
                logger.debug(f"Method {i+1} failed: {e}")
                continue

        return False

    def connect(self) -> Dict:
        """
        Connect to SolidWorks - launches if not running

        Returns:
            Result dictionary with connection status
        """
        try:
            logger.info("=== Connecting to SolidWorks ===")

            # Step 1: Try connecting to running instance
            if self._try_connect_com():
                version = com_get(self._sw_app, "RevisionNumber",
                                  default="unknown")

                freeze_info = self.ensure_features_not_frozen()
                modal_info = detect_modal_dialog()

                return self._result(True, f"Connected to SolidWorks {version}",
                                  SwErrors.swSuccess,
                                  {"version": str(version), "launched": False,
                                   "freeze_bar": freeze_info,
                                   "modal_dialog": modal_info})

            # Step 2: Find SolidWorks executable
            if self._sw_exe_path is None:
                if self._config.exe_path != "auto":
                    self._sw_exe_path = self._config.exe_path
                else:
                    self._sw_exe_path = find_solidworks()

            if not self._sw_exe_path or not os.path.exists(self._sw_exe_path):
                return self._result(False,
                    f"SolidWorks not found. Set exe_path in config or install SolidWorks.",
                    SwErrors.swSolidWorksNotFound)

            # Step 3: Launch SolidWorks
            logger.info(f"Launching SolidWorks: {self._sw_exe_path}")
            os.startfile(self._sw_exe_path)

            # Step 4: Wait for SolidWorks to start
            logger.info("Waiting for SolidWorks startup...")
            max_wait = self._config.startup_timeout
            retry_interval = self._config.connection_retry_interval
            start_time = time.time()

            while time.time() - start_time < max_wait:
                time.sleep(retry_interval)
                elapsed = int(time.time() - start_time)
                logger.debug(f"Connection attempt at {elapsed}s...")

                if self._try_connect_com():
                    version = com_get(self._sw_app, "RevisionNumber",
                                      default="unknown")

                    logger.info(f"Connected after {elapsed}s")
                    freeze_info = self.ensure_features_not_frozen()
                    return self._result(True,
                        f"Launched and connected to SolidWorks {version} (took {elapsed}s)",
                        SwErrors.swSuccess,
                        {"version": str(version), "launched": True,
                         "startup_time": elapsed, "freeze_bar": freeze_info})

            return self._result(False,
                f"Timeout after {max_wait}s. Close any dialogs and try again.",
                SwErrors.swConnectionError)

        except Exception as e:
            logger.error(f"Connection error: {e}\n{traceback.format_exc()}")
            return self._result(False, f"Connection error: {e}",
                              SwErrors.swConnectionError)

    def disconnect(self) -> Dict:
        """
        Disconnect from SolidWorks (does not close SolidWorks)

        Returns:
            Result dictionary
        """
        self._sw_app = None
        self._connected = False
        logger.info("Disconnected from SolidWorks")
        return self._result(True, "Disconnected from SolidWorks")

    # ========================================================================
    # Document Methods
    # ========================================================================

    def get_active_doc(self) -> Tuple[Any, Optional[Dict]]:
        """
        Get active document with auto-connect

        Returns:
            Tuple of (document, error_result)
            - If successful: (document, None)
            - If failed: (None, error_dict)
        """
        if not self.is_connected:
            result = self.connect()
            if not result["success"]:
                return None, result

        doc = self._sw_app.ActiveDoc
        if doc is None:
            return None, self._result(False,
                "No document open. Use create_new_part first.",
                SwErrors.swNoActiveDocument)

        return doc, None

    def _get_doc_title(self, doc) -> str:
        """Get document title (handles property/method difference)"""
        return com_get(doc, "GetTitle", default="Unknown")

    def _get_doc_path(self, doc) -> str:
        """Get document path (handles property/method difference)"""
        return com_get(doc, "GetPathName", default="")

    # ========================================================================
    # Freeze Bar protection
    # ========================================================================

    def ensure_features_not_frozen(self, doc=None) -> Dict:
        """
        Freeze Bar silently kills API features: if the freeze bar sits at
        the end of the tree, every new feature (FeatureExtrusion3/FeatureCut4)
        is created frozen - no faces, no geometry, no error. Must be checked
        on connect and before every feature-creating operation.

        Disables the Freeze Bar option globally and moves the bar to the
        top of the tree in the given (or active) document.
        """
        info = {"option_disabled": False, "bar_moved_to_top": False}
        try:
            self._sw_app.SetUserPreferenceToggle(
                int(SwUserPreferenceToggle.swUserEnableFreezeBar), False)
            info["option_disabled"] = True
        except Exception as e:
            logger.debug(f"SetUserPreferenceToggle(FreezeBar) failed: {e}")

        try:
            if doc is None:
                doc = self._sw_app.ActiveDoc
            if doc is not None:
                moved = doc.FeatureManager.EditFreeze(
                    int(SwMoveFreezeBarTo.swMoveFreezeBarToTop), "", True)
                info["bar_moved_to_top"] = bool(moved)
        except Exception as e:
            logger.debug(f"EditFreeze(ToTop) failed: {e}")

        return info
