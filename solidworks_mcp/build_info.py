"""Which code is this process running? Detects a server left behind by edits on disk.

An MCP client keeps the server process alive across source changes; nothing in
the protocol says so. The process remembers a fingerprint of its package at
import time and reports when the files no longer match.
"""

from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
STARTED_AT = time.time()


def source_fingerprint(root=None):
    """{relative path: (mtime_ns, size)} of every Python source of the package."""
    root = Path(root) if root else PACKAGE_ROOT
    result = {}
    for path in root.rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        result[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return result


BASELINE = source_fingerprint()


def changed_files(baseline=None, root=None):
    """Source files added, removed or modified since the baseline was taken."""
    baseline = BASELINE if baseline is None else baseline
    current = source_fingerprint(root)
    return sorted(name for name in baseline.keys() | current.keys() if baseline.get(name) != current.get(name))


def server_state(baseline=None, root=None):
    from . import __version__
    changed = changed_files(baseline, root)
    state = {'version': __version__, 'pid': os.getpid(),
             'started_at': datetime.datetime.fromtimestamp(STARTED_AT).isoformat(timespec='seconds'),
             'code_changed_since_start': bool(changed)}
    if changed:
        state['changed_files'] = changed[:8]
        state['changed_file_count'] = len(changed)
        state['action'] = ('The server code on disk is newer than this process: reconnect the MCP server '
                           'in the client to load it. Until then the old behaviour applies.')
    return state
