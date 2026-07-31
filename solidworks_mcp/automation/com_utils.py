"""
SolidWorks COM Utilities
------------------------
Helpers that hide SolidWorks 2025/2026 COM quirks:

- com_get(): normalized property-vs-method access. NEVER use callable() on
  win32com objects - any live COM dynamic-dispatch object implements __call__,
  so callable() is always True and breaks tree traversal.
- Typed makepy wrappers: EnsureDispatch() fails on SW objects (they do not
  expose GetTypeInfo -> "can not automate the makepy process"). The working
  path is generating a module from sldworks.tlb and wrapping raw IUnknown
  manually: mod.IFeatureManager(obj._oleobj_). Feature-creating calls
  (FeatureExtrusion3 / FeatureCut4) MUST go through typed wrappers - dynamic
  dispatch raises COM 61704 "Internal application error".
- select_by_id2(): hides the Callout parameter that requires
  VARIANT(VT_DISPATCH, None) (passing plain None -> Type mismatch, param 8).
- Modal dialog detection: modal SW dialogs (Options etc.) silently swallow
  API calls; detect them via win32gui.
- Math helpers for IMathTransform ArrayData (row-major r11..r33, translation
  at [9..11], scale at [12]; point transform uses row-vector convention).
"""

import os
import math
import inspect
import logging
import threading
import time
from dataclasses import dataclass

import pythoncom
import win32com.client
from win32com.client import VARIANT, gencache

logger = logging.getLogger(__name__)

_MISSING = object()

# Typed makepy module cache (generated once per process)
_typed_module = None
_typed_module_error = None
_typed_module_lock = threading.Lock()

# swconst.tlb is a separate type library.  Loading it is the only reliable
# symbolic route to enums such as swInputDimValOnCreate; hard-coded values are
# deliberately not used by the dimension guard.
_constants_module = None
_constants_module_error = None
_constants_module_lock = threading.Lock()

# Optional observer used by the telemetry layer.  Keeping it here lets both
# legacy helpers and new native tools report COM activity without wrapping COM
# objects in fragile proxy classes.
_com_observer = None


def set_com_observer(observer):
    """Install a best-effort callback receiving ``(operation, member)``."""
    global _com_observer
    _com_observer = observer


def _observe(operation, member):
    if _com_observer is not None:
        try:
            _com_observer(operation, member)
        except Exception:
            pass


# ============================================================================
# Property / method normalization
# ============================================================================

def com_get(obj, name, *args, default=_MISSING):
    """
    Read a COM member that may be exposed as a property (SW 2025/2026 dynamic
    dispatch: GetFaces, GetTypeName2, GetNextFeature, GetType...) or as a
    method (typed makepy wrappers).

    Property access is tried first; a bound Python routine (typed wrapper
    method) is invoked with *args. If property access fails, the member is
    re-flagged as a method (_FlagAsMethod) and called.

    Args:
        obj: COM object (dynamic or typed)
        name: Member name
        *args: Arguments if the member turns out to be a method
        default: Value returned instead of raising on total failure

    Returns:
        Member value
    """
    if obj is None:
        if default is not _MISSING:
            return default
        raise ValueError(f"com_get: object is None (member '{name}')")

    _observe("read" if not args else "call", name)
    try:
        value = getattr(obj, name)
        if inspect.isroutine(value):
            # Typed makepy wrapper (or dynamic method binding) - call it
            return value(*args)
        return value
    except Exception as first_err:
        # Property-get failed; retry as an explicit method call.
        # _FlagAsMethod exists only on dynamic CDispatch objects.
        try:
            flag = getattr(obj, "_FlagAsMethod", None)
            if flag is not None:
                flag(name)
                return getattr(obj, name)(*args)
        except Exception:
            pass
        if default is not _MISSING:
            return default
        raise first_err


def feature_face_count(feat) -> int:
    """
    Count faces of a feature via GetFaces. 0 faces = dead feature:
    SolidWorks silently creates empty features (or "ICE" features) for
    infeasible parameters - without this check the agent works blind.
    Note: GetTypeName2 is unreliable for health checks (returns "ICE" even
    for healthy extrusions via dynamic property access).
    """
    faces = com_get(feat, "GetFaces", default=None)
    if not faces:
        return 0
    try:
        return len(faces)
    except TypeError:
        return 0


# ============================================================================
# Selection helpers
# ============================================================================

def null_dispatch():
    """VARIANT(VT_DISPATCH, None) for optional COM-object parameters."""
    return VARIANT(pythoncom.VT_DISPATCH, None)


def out_i4(value=0):
    """Create a signed 32-bit COM by-reference output value."""
    return VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, int(value))


def array_r8(values):
    """Create a COM SAFEARRAY of doubles with explicit marshaling."""
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                   [float(v) for v in values])


def select_by_id2(doc, name, sel_type, x=0.0, y=0.0, z=0.0,
                  append=False, mark=0) -> bool:
    """
    IModelDocExtension::SelectByID2 wrapper. Hides the Callout parameter
    (must be VARIANT(VT_DISPATCH, None); plain None -> Type mismatch param 8).
    Coordinates are in meters.
    """
    try:
        return bool(doc.Extension.SelectByID2(
            name, sel_type, x, y, z, append, mark, null_dispatch(), 0))
    except Exception as e:
        logger.debug(f"SelectByID2({name}, {sel_type}) failed: {e}")
        return False


def select_by_ray(doc, origin_m, direction, sel_type=2, radius_m=1e-5,
                  append=False, mark=0) -> bool:
    """
    IModelDocExtension::SelectByRay wrapper.

    Args:
        origin_m: Ray origin (x, y, z) in meters
        direction: Ray direction vector (normalized internally)
        sel_type: swSelectType_e entity filter (2 = swSelFACES)
        radius_m: Ray radius in meters
        append: Append to current selection
        mark: Selection mark (e.g. 1 = end-condition reference face)
    """
    dx, dy, dz = normalize(direction)
    try:
        return bool(doc.Extension.SelectByRay(
            origin_m[0], origin_m[1], origin_m[2],
            dx, dy, dz, radius_m, int(sel_type), append, int(mark), 0))
    except Exception as e:
        logger.debug(f"SelectByRay failed: {e}")
        return False


def create_select_data(doc, mark=0):
    """Create ISelectData with the given mark (for IBody2::Select2 etc.)."""
    sel_data = com_get(doc.SelectionManager, "CreateSelectData", default=None)
    if sel_data is not None:
        try:
            sel_data.Mark = int(mark)
        except Exception:
            pass
    return sel_data


# ============================================================================
# Typed makepy module (sldworks.tlb)
# ============================================================================

def _find_sldworks_tlb():
    """Locate sldworks.tlb near the SolidWorks executable."""
    candidates = []
    try:
        from ..utils import find_solidworks
        exe = find_solidworks()
        if exe:
            candidates.append(os.path.join(os.path.dirname(exe), "sldworks.tlb"))
    except Exception:
        pass
    candidates.append(
        r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\sldworks.tlb")

    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def get_typed_module():
    """
    Get the makepy-generated module for the SolidWorks type library,
    generating it once on first use. Returns None if generation failed
    (callers must fall back to dynamic dispatch).
    """
    global _typed_module, _typed_module_error
    if _typed_module is not None:
        return _typed_module
    if _typed_module_error is not None:
        return None

    with _typed_module_lock:
        if _typed_module is not None:
            return _typed_module
        if _typed_module_error is not None:
            return None
        try:
            tlb_path = _find_sldworks_tlb()
            if not tlb_path:
                raise FileNotFoundError("sldworks.tlb not found")

            tlib = pythoncom.LoadTypeLib(tlb_path)
            attr = tlib.GetLibAttr()
            guid, lcid, major, minor = str(attr[0]), attr[1], attr[3], attr[4]

            mod = gencache.EnsureModule(guid, lcid, major, minor)
            if mod is None:
                from win32com.client import makepy
                makepy.GenerateFromTypeLibSpec(tlb_path, bForDemand=0)
                mod = gencache.EnsureModule(guid, lcid, major, minor)
            if mod is None:
                raise RuntimeError("EnsureModule returned None after generation")

            _typed_module = mod
            logger.info(f"Typed SW module loaded: {tlb_path} v{major}.{minor}")
            return _typed_module
        except Exception as e:
            _typed_module_error = str(e)
            logger.error(f"Typed module generation failed: {e}")
            return None


def get_constants_module():
    """Load the SolidWorks constants typelib (swconst.tlb) once."""
    global _constants_module, _constants_module_error
    if _constants_module is not None:
        return _constants_module
    if _constants_module_error is not None:
        return None
    with _constants_module_lock:
        if _constants_module is not None:
            return _constants_module
        if _constants_module_error is not None:
            return None
        try:
            candidates = []
            try:
                from ..utils import find_solidworks
                exe = find_solidworks()
                if exe:
                    candidates.append(os.path.join(os.path.dirname(exe),
                                                   "swconst.tlb"))
            except Exception:
                pass
            candidates.append(
                r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\swconst.tlb")
            tlb_path = next((p for p in candidates if os.path.exists(p)), None)
            if not tlb_path:
                raise FileNotFoundError("swconst.tlb not found")
            tlib = pythoncom.LoadTypeLib(tlb_path)
            attr = tlib.GetLibAttr()
            _constants_module = gencache.EnsureModule(
                str(attr[0]), attr[1], attr[3], attr[4])
            if _constants_module is None:
                raise RuntimeError("Could not generate swconst typelib")
            # Importing the module populates win32com.client.constants.
            logger.info("SolidWorks constants loaded from %s", tlb_path)
            return _constants_module
        except Exception as exc:
            _constants_module_error = str(exc)
            logger.error("SolidWorks constants generation failed: %s", exc)
            return None


def resolve_solidworks_constant(name):
    """Resolve a SolidWorks enum by name from swconst.tlb."""
    get_constants_module()
    value = getattr(win32com.client.constants, name, _MISSING)
    if value is _MISSING:
        raise LookupError(f"SolidWorks constant '{name}' is unavailable")
    return int(value)


def typed(obj, interface_name):
    """
    Wrap a COM object into a typed makepy interface, e.g.
    typed(doc.FeatureManager, "IFeatureManager").
    Returns None if the typed module is unavailable.
    """
    if obj is None:
        return None
    mod = get_typed_module()
    if mod is None:
        return None
    try:
        cls = getattr(mod, interface_name)
        return cls(obj._oleobj_)
    except Exception as e:
        logger.debug(f"typed({interface_name}) failed: {e}")
        return None


def get_modeler(sw_app):
    """
    Get IModeler. sw.GetModeler is unavailable via dynamic dispatch
    ("Member not found") and Dispatch("SldWorks.Modeler") is an invalid
    class string - the only working path is the typed ISldWorks wrapper.
    """
    sw_typed = typed(sw_app, "ISldWorks")
    if sw_typed is None:
        return None
    try:
        return sw_typed.GetModeler()
    except Exception as e:
        logger.debug(f"GetModeler failed: {e}")
        return None


# ============================================================================
# Modal dialog detection
# ============================================================================

def _exe_name_for_pid(pid):
    """
    Resolve the executable base name (lowercase) for a process id, best
    effort. Returns None if the process cannot be queried.
    """
    try:
        import win32api
        import win32process

        # PROCESS_QUERY_LIMITED_INFORMATION works across privilege levels;
        # fall back to the classic flags for older systems.
        handle = None
        for access in (0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
                       0x0400 | 0x0010):  # QUERY_INFORMATION | VM_READ
            try:
                handle = win32api.OpenProcess(access, False, pid)
                if handle:
                    break
            except Exception:
                handle = None
        if not handle:
            return None
        try:
            try:
                path = win32process.QueryFullProcessImageName(handle, 0)
            except Exception:
                path = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
        return os.path.basename(path).lower() if path else None
    except Exception:
        return None


_SW_MAIN_WINDOW_CACHE = {"hwnd": 0, "pid": 0}


def _cached_sldworks_main(windows):
    """Return a still-valid, previously verified SolidWorks main frame."""
    cached_hwnd = int(_SW_MAIN_WINDOW_CACHE.get("hwnd") or 0)
    cached_pid = int(_SW_MAIN_WINDOW_CACHE.get("pid") or 0)
    if not cached_hwnd or not cached_pid:
        return None
    for window in windows:
        hwnd, cls, title, pid = window
        if (int(hwnd) == cached_hwnd and int(pid) == cached_pid and
                cls != "#32770" and title and
                "SOLIDWORKS" in title.upper()):
            return window
    _SW_MAIN_WINDOW_CACHE.update({"hwnd": 0, "pid": 0})
    return None


def _window_owner_chain(hwnd, win32gui, limit=12):
    chain = []
    current = hwnd
    seen = set()
    for _ in range(limit):
        try:
            owner = win32gui.GetWindow(current, 4)  # GW_OWNER
        except Exception:
            owner = 0
        if not owner or owner in seen:
            break
        seen.add(owner)
        chain.append(owner)
        current = owner
    return chain


def _window_controls(hwnd, win32gui):
    controls = []

    def collect(child, _):
        if len(controls) >= 64:
            return False
        try:
            text_value = win32gui.GetWindowText(child)
            cls = win32gui.GetClassName(child)
            bounds = list(win32gui.GetWindowRect(child))
            controls.append({
                "hwnd": int(child), "class": cls, "text": text_value[:512],
                "enabled": bool(win32gui.IsWindowEnabled(child)),
                "visible": bool(win32gui.IsWindowVisible(child)),
                "bounds": bounds,
                "control_id": int(win32gui.GetDlgCtrlID(child)),
            })
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, collect, None)
    except Exception:
        pass
    return controls


def _classify_dialog(detail):
    import re
    title = (detail.get("title") or "").strip().lower()
    text_value = "\n".join(
        c.get("text", "") for c in detail.get("controls", [])
        if c.get("text"))
    context_text = str(detail.get("context_text") or "")
    combined = f"{title}\n{text_value}\n{context_text}".lower()
    active_sketch_context = bool(re.search(
        r"\[[^\]]+\s+of\s+[^\]]+\.sldprt\]", context_text, re.I))
    if (title == "modify" or "modify" in title) and (
            re.search(r"\bD\d+@", text_value, re.I)
            or re.search(r"[-+]?\d+(?:[.,]\d+)?\s*(mm|in|cm|m)\b",
                         text_value, re.I)
            or active_sketch_context):
        return "dimension_modify", 0.98
    if "save as" in title or "save document" in combined:
        return "save_prompt", 0.92
    if "rebuild" in combined and ("error" in combined or "warning" in combined):
        return "rebuild_error", 0.85
    return "unknown", 0.25


def _detect_modal_dialog_fast():
    """Non-blocking watchdog scan using only a verified main HWND/PID.

    Reading titles or child controls can synchronously wait on the SolidWorks
    UI thread while it is constructing a modal dialog. The watchdog hot path
    therefore uses only process-local Win32 state and owner relationships.
    """
    scan_started = time.perf_counter()
    info = {"modal": False, "dialogs": [], "window_details": [],
            "main_window": None, "main_window_hwnd": None,
            "main_window_enabled": None, "state": "UI_READY",
            "checked_at": time.time(), "inspection_level": "basic"}
    try:
        import win32gui
        import win32process

        main_hwnd = int(_SW_MAIN_WINDOW_CACHE.get("hwnd") or 0)
        cached_pid = int(_SW_MAIN_WINDOW_CACHE.get("pid") or 0)
        valid = bool(main_hwnd and cached_pid and
                     win32gui.IsWindow(main_hwnd))
        if valid:
            actual_pid = int(
                win32process.GetWindowThreadProcessId(main_hwnd)[1])
            valid = actual_pid == cached_pid
        if not valid:
            _SW_MAIN_WINDOW_CACHE.update({"hwnd": 0, "pid": 0})
            fallback = detect_modal_dialog(include_controls=True)
            fallback["inspection_level"] = "basic_fallback_full"
            return fallback

        info["main_window_hwnd"] = main_hwnd
        enabled = bool(win32gui.IsWindowEnabled(main_hwnd))
        info["main_window_enabled"] = enabled
        if not enabled:
            info["modal"] = True
            info["state"] = "UI_BLOCKED_UNKNOWN_DIALOG"
            info["detected_at"] = time.time()
            return info

        owned_popups = []

        def _collect(hwnd, _):
            if hwnd == main_hwnd or not win32gui.IsWindowVisible(hwnd):
                return True
            try:
                pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
                if pid != cached_pid:
                    return True
                if main_hwnd in _window_owner_chain(hwnd, win32gui):
                    owned_popups.append(int(hwnd))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_collect, None)
        if owned_popups:
            info["modal"] = True
            info["state"] = "UI_BLOCKED_UNKNOWN_DIALOG"
            info["detected_at"] = time.time()
            info["owned_popup_hwnds"] = owned_popups
    except Exception as exc:
        logger.debug(f"Fast modal dialog detection failed: {exc}")
    finally:
        info["scan_elapsed_ms"] = round(
            (time.perf_counter() - scan_started) * 1000.0, 3)
    return info


def detect_modal_dialog(include_controls=True):
    """
    Detect a modal dialog blocking the SolidWorks UI. Modal dialogs
    (Options etc.) silently swallow API calls, so feature operations
    should report this state in their diagnostics.

    The SolidWorks main frame is identified strictly by its owning process
    (SLDWORKS.exe), not by window title - otherwise an unrelated window such
    as a File Explorer folder named "Solidworks-MCP" is mistaken for it.

    Returns:
        Dict: {"modal": bool, "dialogs": [titles], "main_window": title,
               "main_window_enabled": bool or None}
    """
    if not include_controls:
        return _detect_modal_dialog_fast()

    scan_started = time.perf_counter()
    info = {"modal": False, "dialogs": [], "window_details": [],
            "main_window": None, "main_window_hwnd": None,
            "main_window_enabled": None, "state": "UI_READY",
            "checked_at": time.time(),
            "inspection_level": ("full" if include_controls else "basic")}
    try:
        import win32gui
        import win32process

        windows = []

        def _collect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                try:
                    windows.append((
                        hwnd,
                        win32gui.GetClassName(hwnd),
                        win32gui.GetWindowText(hwnd),
                        win32process.GetWindowThreadProcessId(hwnd)[1],
                    ))
                except Exception:
                    pass
            return True

        win32gui.EnumWindows(_collect, None)

        # Cache exe name per pid (one OpenProcess per distinct process).
        # A previously verified HWND/PID pair avoids OpenProcess entirely on
        # the watchdog hot path. The cache is invalidated if either changes.
        exe_cache = {}

        def exe_of(pid):
            if pid not in exe_cache:
                exe_cache[pid] = _exe_name_for_pid(pid)
            return exe_cache[pid]

        def _area(hwnd):
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                return (r - l) * (b - t)
            except Exception:
                return 0

        main = _cached_sldworks_main(windows)
        if main is None:
            # Verify the title candidates first. Querying every visible
            # process executable can add hundreds of milliseconds on a busy
            # workstation, which is unacceptable for a modal watchdog.
            titled = [w for w in windows
                      if w[1] != "#32770" and w[2]
                      and "SOLIDWORKS" in w[2].upper()]
            candidates = [w for w in titled
                          if exe_of(w[3]) == "sldworks.exe"]
            if not candidates:
                candidates = [w for w in windows
                              if w[1] != "#32770" and w[2]
                              and exe_of(w[3]) == "sldworks.exe"]
            if candidates:
                main = max(candidates, key=lambda w: _area(w[0]))
                _SW_MAIN_WINDOW_CACHE.update({
                    "hwnd": int(main[0]), "pid": int(main[3])})
            else:
                # Fallback: title match (process query may have failed). It is
                # deliberately not cached because identity was not verified.
                for w in windows:
                    if ("SOLIDWORKS" in w[2].upper() and
                            w[1] != "#32770"):
                        main = w
                        break
        if main is None:
            return info

        info["main_window"] = main[2]
        enabled = bool(win32gui.IsWindowEnabled(main[0]))
        info["main_window_enabled"] = enabled
        if not enabled:
            info["detected_at"] = time.time()

        info["main_window_hwnd"] = int(main[0])

        # Include every meaningful visible owned popup in the SolidWorks
        # process, not only the classic #32770 dialog class.
        sw_pid = main[3]
        for hwnd, cls, title, pid in windows:
            if pid != sw_pid or hwnd == main[0]:
                continue
            owners = _window_owner_chain(hwnd, win32gui)
            try:
                style = int(win32gui.GetWindowLong(hwnd, -16))
            except Exception:
                style = 0
            owned = main[0] in owners
            popup = bool(style & 0x80000000)  # WS_POPUP
            area = _area(hwnd)
            meaningful = (cls == "#32770" or owned or
                          ((not enabled) and popup and area > 400 and title))
            if not meaningful:
                continue
            info.setdefault("detected_at", time.time())
            controls = (_window_controls(hwnd, win32gui)
                        if include_controls else [])
            try:
                bounds = list(win32gui.GetWindowRect(hwnd))
            except Exception:
                bounds = None
            detail = {
                "hwnd": int(hwnd), "title": title or "<untitled dialog>",
                "class": cls, "pid": int(pid),
                "owner_chain": [int(v) for v in owners],
                "enabled": bool(win32gui.IsWindowEnabled(hwnd)),
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "bounds": bounds, "controls": controls,
                "context_text": main[2],
            }
            classification, confidence = _classify_dialog(detail)
            detail["classification"] = classification
            detail["confidence"] = confidence
            detail["text"] = "\n".join(
                c["text"] for c in controls if c.get("text"))[:2048]
            info["window_details"].append(detail)
            info["dialogs"].append(detail["title"])

        info["modal"] = (not enabled) or bool(info["window_details"])
        if info["modal"]:
            known = bool(info["window_details"]) and all(
                d["classification"] != "unknown"
                for d in info["window_details"])
            info["state"] = ("UI_BLOCKED_KNOWN_DIALOG" if known
                             else "UI_BLOCKED_UNKNOWN_DIALOG")
    except Exception as e:
        logger.debug(f"Modal dialog detection failed: {e}")
    info["scan_elapsed_ms"] = round(
        (time.perf_counter() - scan_started) * 1000.0, 3)
    return info


def resolve_known_dialog(dialog, allowed=None, expected_text=None):
    """Resolve an explicitly allowed, positively classified dialog.

    Only ``dimension_modify`` is supported.  The expected dimension token must
    occur in the captured dialog text; unknown dialogs are never acted on.
    """
    allowed = set(allowed or [])
    classification = dialog.get("classification")
    if classification not in allowed or classification != "dimension_modify":
        return {"resolved": False, "reason": "not_allowed"}
    haystack = (f"{dialog.get('title', '')}\n{dialog.get('text', '')}\n"
                f"{dialog.get('context_text', '')}")
    if not expected_text or str(expected_text).lower() not in haystack.lower():
        return {"resolved": False, "reason": "identity_mismatch"}
    try:
        import win32api
        import win32con
        for control in dialog.get("controls", []):
            if (control.get("class", "").lower() == "button" and
                    control.get("text", "").strip().lower() in
                    {"ok", "accept", "apply"}):
                win32api.PostMessage(int(control["hwnd"]),
                                     win32con.BM_CLICK, 0, 0)
                return {"resolved": True, "button": control.get("text")}
        if (str(dialog.get("title") or "").strip().lower() == "modify" and
                str(dialog.get("class") or "") == "#32770"):
            hwnd = int(dialog.get("hwnd") or 0)
            if not hwnd:
                return {"resolved": False, "reason": "dialog_hwnd_missing"}
            win32api.PostMessage(
                hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
            win32api.PostMessage(
                hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
            return {"resolved": True, "button": "ENTER",
                    "method": "dialog_enter"}
        return {"resolved": False, "reason": "safe_button_not_found"}
    except Exception as exc:
        return {"resolved": False, "reason": str(exc)}


@dataclass
class ComAdapter:
    """Single typed-COM compatibility adapter for all new native tools."""

    observer: object = None

    def _record(self, operation, member):
        _observe(operation, member)
        if self.observer is not None:
            try:
                self.observer(operation, member)
            except Exception:
                pass

    def read(self, obj, name, default=_MISSING):
        self._record("read", name)
        return com_get(obj, name, default=default)

    def call(self, obj, name, *args, default=_MISSING):
        self._record("call", name)
        return com_get(obj, name, *args, default=default)

    def typed(self, obj, interface_name):
        self._record("typed", interface_name)
        return typed(obj, interface_name)

    @staticmethod
    def null_dispatch():
        return null_dispatch()

    @staticmethod
    def out_i4(value=0):
        return out_i4(value)

    @staticmethod
    def array_r8(values):
        return array_r8(values)

    def call_verified(self, obj, name, *args, verifier=None):
        value = self.call(obj, name, *args)
        if verifier is not None and not verifier(value):
            raise RuntimeError(f"Verification failed for COM call {name}")
        return value


# ============================================================================
# Math helpers (IMathTransform ArrayData)
# ============================================================================

def normalize(vec):
    """Normalize a 3-vector. Raises ValueError on zero length."""
    x, y, z = float(vec[0]), float(vec[1]), float(vec[2])
    n = math.sqrt(x * x + y * y + z * z)
    if n < 1e-12:
        raise ValueError("Zero-length direction vector")
    return (x / n, y / n, z / n)


def cross(a, b):
    """Cross product of two 3-vectors."""
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    """Dot product of two 3-vectors."""
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def transform_point(xform_data, pt):
    """
    Apply an IMathTransform (its ArrayData, 16 doubles) to a point.
    Layout: [0..8] rotation row-major (r11..r33), [9..11] translation,
    [12] scale. Point transform uses row-vector convention:
    p'_j = scale * sum_i(p_i * R[i][j]) + t_j.
    """
    d = list(xform_data)
    s = d[12] if len(d) > 12 else 1.0
    x, y, z = float(pt[0]), float(pt[1]), float(pt[2])
    return (
        s * (x * d[0] + y * d[3] + z * d[6]) + d[9],
        s * (x * d[1] + y * d[4] + z * d[7]) + d[10],
        s * (x * d[2] + y * d[5] + z * d[8]) + d[11],
    )


def build_view_orientation_data(view_direction, up_direction=None):
    """
    Build IMathTransform ArrayData (16 doubles) for IModelView::Orientation3.
    COLUMNS of the rotation part = [screenRight, screenUp, towardViewer]
    in model coordinates (row-major data r11..r33).

    Args:
        view_direction: Direction the camera looks along (from viewer into
                        the model); towardViewer = -view_direction.
        up_direction: Approximate screen-up in model coords (default +Y,
                      or +X if view is along Y).
    """
    t = normalize([-c for c in normalize(view_direction)])
    if up_direction is None:
        up_direction = (0.0, 1.0, 0.0)
        if abs(dot(t, up_direction)) > 0.99:
            up_direction = (1.0, 0.0, 0.0)
    # Orthonormalize up against towardViewer
    u_raw = normalize(up_direction)
    proj = dot(u_raw, t)
    u = normalize((u_raw[0] - proj * t[0],
                   u_raw[1] - proj * t[1],
                   u_raw[2] - proj * t[2]))
    # Right-handed screen basis: right x up = towardViewer => right = up x toward
    r = cross(u, t)

    # Row-major r11..r33 with columns = [r, u, t]
    return [
        r[0], u[0], t[0],
        r[1], u[1], t[1],
        r[2], u[2], t[2],
        0.0, 0.0, 0.0,   # translation
        1.0,             # scale
        0.0, 0.0, 0.0,
    ]
