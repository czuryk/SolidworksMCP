"""Local trace of MCP calls and the session report built from it.

One JSON line per tool call: what was called, how long it took, how much text
came back and how it ended. Program text, patches and images are never stored;
only their sizes. Nothing leaves the machine.
"""

from __future__ import annotations

import collections
import json
import os
import threading
import time
from pathlib import Path

RECOVERY_TOOLS = ('reconcile_session', 'restart_solidworks')
WAIT_TOOLS = ('wait_operation', 'get_job_result')
MAX_MEMORY_CALLS = 5000


class SessionTrace:
    def __init__(self, directory=None):
        self._lock = threading.Lock()
        self.calls = collections.deque(maxlen=MAX_MEMORY_CALLS)
        self.started_at = time.time()
        self.path = None
        if directory is not None:
            stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(self.started_at))
            self.path = Path(directory) / f'session-{stamp}-{os.getpid()}.jsonl'

    def record(self, tool, arguments, result, duration_sec, text_chars, image_count, operation=None):
        """Store one finished call. Arguments are reduced to sizes and a few routing fields."""
        arguments = arguments or {}
        data = (result or {}).get('data') if isinstance(result, dict) else None
        data = data if isinstance(data, dict) else {}
        error = data.get('error') if isinstance(data.get('error'), dict) else {}
        success = bool(result.get('success')) if isinstance(result, dict) else None
        preparation = data.get('response_preparation') or {}
        entry = {
            'at': round(time.time(), 3), 'tool': tool, 'duration_ms': round(duration_sec * 1000),
            'success': success, 'result_chars': int(text_chars), 'images': int(image_count),
            'handler_success': preparation.get('handler_success', success),
            'serialization_success': preparation.get('success', True),
            'code_chars': len(arguments.get('code') or ''),
            'patch_chars': sum(len(p.get('old', '')) + len(p.get('new', '')) for p in arguments.get('patch') or []
                               if isinstance(p, dict)),
        }
        if arguments.get('mode'):
            entry['mode'] = arguments['mode']
        if tool == 'execute_model_code' and 'mode' not in entry:
            entry['mode'] = 'analyze'
        if success is False:
            entry['error'] = error.get('code') or (result or {}).get('error_name')
        if entry['handler_success'] is False:
            entry['handler_error'] = preparation.get('handler_error', entry.get('error'))
        if data.get('status') in {'RUNNING', 'QUEUED'}:
            entry['running'] = True
        operation_id = (data.get('executor') or {}).get('operation_id') or data.get('operation_id')
        if operation_id:
            entry['operation_id'] = operation_id
        if operation:
            entry['operation_tool'] = operation['tool']
            if operation.get('mode'):
                entry['operation_mode'] = operation['mode']
        timings = data.get('timings_ms')
        if isinstance(timings, dict) and 'total' in timings:
            entry['operation_ms'] = timings['total']
        with self._lock:
            self.calls.append(entry)
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.path, 'a', encoding='utf-8') as stream:
                        stream.write(json.dumps(entry, separators=(',', ':')) + '\n')
                except OSError:
                    self.path = None      # Tracing must never fail a tool call.
        return entry

    def snapshot(self):
        with self._lock:
            return list(self.calls)


def load_trace(path):
    calls = []
    with open(path, encoding='utf-8') as stream:
        for line in stream:
            try:
                calls.append(json.loads(line))
            except ValueError:
                continue
    return calls


def operation_outcomes(calls):
    """Join starts and terminal waits without counting a fetched result twice.

    Older trace files have no handler fields or operation metadata. Their
    operation IDs still let us recover the originating tool and mode.
    """
    groups = {}
    for index, call in enumerate(calls):
        key = ('operation', call['operation_id']) if call.get('operation_id') else ('call', index)
        groups.setdefault(key, []).append(call)
    outcomes = []
    for events in groups.values():
        origin = next((e for e in events if e['tool'] not in WAIT_TOOLS), events[0])
        item = dict(origin)
        item['tool'] = origin.get('operation_tool', origin['tool'])
        item['mode'] = origin.get('operation_mode', origin.get('mode'))
        terminal = [e for e in events if not e.get('running')
                    and e.get('handler_success', e.get('success')) is not None]
        item['answered_running'] = any(e.get('running') for e in events)
        item['success'] = None
        item.pop('error', None)
        if terminal:
            last = terminal[-1]
            item['success'] = last.get('handler_success', last.get('success'))
            if item['success'] is False:
                item['error'] = last.get('handler_error', last.get('error'))
        timings = [e['operation_ms'] for e in events if e.get('operation_ms') is not None]
        if timings:
            item['operation_ms'] = timings[-1]
        outcomes.append(item)
    return outcomes


def session_report(calls):
    """The counters used to judge a modeling run; the same ones as the offline transcript script."""
    by_tool = collections.Counter(call['tool'] for call in calls)
    chars = collections.Counter()
    seconds = collections.Counter()
    for call in calls:
        chars[call['tool']] += call.get('result_chars', 0)
        seconds[call['tool']] += call.get('duration_ms', 0) / 1000
    outcomes = operation_outcomes(calls)
    mutations = [c for c in outcomes if c['tool'] == 'execute_model_code' and c.get('mode') in {'create', 'edit', 'revert'}]
    edits = [c for c in mutations if c.get('mode') == 'edit']
    total_chars = sum(chars.values())
    errors = collections.Counter(c['error'] for c in calls if c.get('error'))
    operation_errors = collections.Counter(c['error'] for c in outcomes if c.get('error'))
    operations = sorted((c for c in outcomes if c.get('operation_ms')), key=lambda c: -c['operation_ms'])
    return {
        'calls': len(calls),
        'calls_by_tool': dict(by_tool.most_common()),
        'mutations_create_edit': len(mutations),
        'mutations_answered_running': sum(1 for c in mutations if c.get('answered_running')),
        'mutations_failed': sum(1 for c in mutations if c.get('success') is False),
        'mutations_completed': sum(1 for c in mutations if c.get('success') is not None),
        'mutations_pending': sum(1 for c in mutations if c.get('success') is None),
        'parameter_only_edits': sum(1 for c in edits if not c.get('code_chars') and not c.get('patch_chars')),
        'patch_edits': sum(1 for c in edits if c.get('patch_chars')),
        'full_program_edits': sum(1 for c in edits if c.get('code_chars')),
        'analyze_calls': sum(1 for c in calls if c['tool'] == 'execute_model_code' and c.get('mode') == 'analyze'),
        'compare_reference_calls': by_tool['compare_reference'],
        'exports': by_tool['export_model'],
        'recovery_calls': sum(by_tool[name] for name in RECOVERY_TOOLS),
        'wait_operation_calls': by_tool['wait_operation'],
        'status_calls': by_tool['get_executor_status'],
        'reconcile_session_calls': by_tool['reconcile_session'],
        'api_lookups': by_tool['model_api_lookup'],
        'modal_blocks': errors['MODAL_DIALOG_BLOCKING'],
        'errors_by_code': dict(errors.most_common()),
        'operation_errors_by_code': dict(operation_errors.most_common()),
        'response_serialization_failures': sum(c.get('serialization_success') is False for c in calls),
        'result_chars_total': total_chars,
        'result_chars_by_tool': dict(chars.most_common()),
        'result_chars_recovery_share': (round(sum(chars[name] for name in RECOVERY_TOOLS) / total_chars, 3)
                                        if total_chars else 0),
        'input_chars_code': sum(c.get('code_chars', 0) for c in calls),
        'input_chars_patch': sum(c.get('patch_chars', 0) for c in calls),
        'images_returned': sum(c.get('images', 0) for c in calls),
        'wall_seconds_in_calls': round(sum(seconds.values()), 1),
        'seconds_by_tool': {name: round(value, 1) for name, value in seconds.most_common()},
        'operation_seconds_total': round(sum(c['operation_ms'] for c in operations) / 1000, 1),
        'slowest_operations': [{'tool': c['tool'], 'mode': c.get('mode'), 'seconds': round(c['operation_ms'] / 1000, 1)}
                               for c in operations[:5]],
    }
