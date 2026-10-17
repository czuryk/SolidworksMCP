"""Cross-process exclusion for commands writing to the interactive SW session."""

import os
import tempfile
import json
import threading
from pathlib import Path


class SessionBusy(RuntimeError):
    pass


class SessionQuarantined(SessionBusy):
    pass


_owners = threading.local()
UNRESOLVED = {'RUNNING', 'RECOVERING', 'OUTCOME_UNKNOWN', 'RETURNED_LATE', 'ROLLBACK_UNCONFIRMED'}


def read_session_state(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('status'), str):
            raise ValueError('Invalid session journal')
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SessionQuarantined('Session journal is unreadable; recovery required') from exc


class SessionLease:
    def __init__(self, path=None, *, recovery=False):
        self.path = Path(path) if path else Path(
            os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        ) / "SolidworksMCP" / "session-writer.lock"
        self.handle = None
        self.state_path = self.path.with_suffix('.state.json')
        self.recovery = recovery
        self._key = os.path.normcase(str(self.path.resolve()))
        self._nested = False

    def acquire(self):
        import msvcrt
        if self.handle is not None or self._nested:
            raise RuntimeError('Lease instance is already acquired')
        owned = getattr(_owners, 'leases', {})
        if self._key in owned:
            owned[self._key] += 1
            self._nested = True
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise SessionBusy("Another MCP command is writing to SolidWorks") from exc
        self.handle = handle
        try:
            if not self.recovery and read_session_state(self.state_path).get('status') in UNRESOLVED:
                raise SessionQuarantined('Previous CAD outcome is unresolved; use reconcile_session')
        except Exception:
            self.close()
            raise
        owned[self._key] = 1
        _owners.leases = owned
        return self

    def close(self):
        owned = getattr(_owners, 'leases', {})
        if self._nested:
            owned[self._key] -= 1
            self._nested = False
            return
        if self.handle is not None:
            if owned.get(self._key, 1) != 1:
                raise RuntimeError('Nested leases must close before the owner')
            owned.pop(self._key, None)
            import msvcrt
            handle, self.handle = self.handle, None
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.close()
