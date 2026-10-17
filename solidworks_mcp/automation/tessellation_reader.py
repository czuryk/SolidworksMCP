"""Measured ITessellation reads on the STA owner; no normal or facet is skipped."""

import inspect
import os
import time

from ..shape_program import ProgramError
from .com_utils import com_get, is_rpc_disconnect


class TessellationReader:
    def __init__(self, tessellation, runtime):
        self.tessellation = tessellation
        self.runtime = runtime
        self.methods = {}
        self.stats = {}
        self.backend = os.environ.get('SOLIDWORKS_MCP_TESSELLATION_READER', 'compat')

    def call(self, member, *args, array=False):
        # Bound mode is opt-in until the native A/B benchmark qualifies it.
        if member not in self.methods:
            method = getattr(self.tessellation, member) if self.backend == 'bound' else None
            self.methods[member] = method if inspect.isroutine(method) else None
        method = self.methods[member]
        stats = self.stats.setdefault(member, {'calls': 0, 'cache_hits': 0, 'elapsed_sec': 0.,
                                               'max_call_sec': 0., 'conversion_sec': 0.})
        self.runtime.begin_com_read(member, args[0] if args and isinstance(args[0], int) else None)
        started = time.perf_counter()
        hresult = None
        failed = None
        try:
            value = method(*args) if method is not None else com_get(self.tessellation, member, *args)
            elapsed = time.perf_counter() - started
        except Exception as exc:
            hresult = getattr(exc, 'hresult', None)
            failed = exc
        finally:
            elapsed = time.perf_counter() - started
            stats['calls'] += 1
            stats['elapsed_sec'] += elapsed
            stats['max_call_sec'] = max(stats['max_call_sec'], elapsed)
            self.runtime.end_com_read(elapsed, hresult)
        if failed is not None:
            raise ProgramError('RPC_CONNECTION_LOST' if is_rpc_disconnect(failed) else 'TESSELLATION_READ_FAILED',
                               str(failed), com_hresult=hresult, com_method=member,
                               com_index=args[0] if args and isinstance(args[0], int) else None,
                               stage=(self.runtime.reader_snapshot().get('progress') or {}).get('phase',
                                                                                               'tessellation_read'),
                               reader=self.snapshot()) from failed
        if array:
            conversion = time.perf_counter()
            value = list(value or [])
            stats['conversion_sec'] += time.perf_counter() - conversion
        return value

    def hit(self, member):
        self.stats[member]['cache_hits'] += 1

    def snapshot(self):
        return {'backend': self.backend, 'methods': {key: dict(value) for key, value in self.stats.items()},
                **self.runtime.reader_snapshot()}
