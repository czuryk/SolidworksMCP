"""COM-free progress snapshots; a heartbeat is not a counter advance."""

import math
import time


def update_progress(previous, event, now=None):
    now = time.time() if now is None else now
    phase = str(event.get('phase') or event.get('stage') or 'running')
    previous = previous or {}
    changed = phase != previous.get('phase') or event.get('reset', False)
    event = {key: value for key, value in event.items() if key != 'reset'}
    base = {} if changed else previous
    completed = event.get('completed', event.get('facet', base.get('completed')))
    if not isinstance(completed, (int, float)) or not math.isfinite(completed):
        completed = None
    if completed is not None:
        completed = max(0, completed, base.get('completed') or 0)
    total = event.get('total', base.get('total'))
    if not isinstance(total, (int, float)) or not math.isfinite(total) or total <= 0:
        total = None
    advanced = changed or (completed is not None and completed > (base.get('completed') or 0))
    return {**base, **event, 'phase': phase, 'completed': completed, 'total': total,
            'unit': event.get('unit', 'facets' if 'facet' in event else base.get('unit')),
            'updated_at': now, 'started_at': base.get('started_at', now),
            'last_advance_at': now if advanced else base.get('last_advance_at', now)}


def progress_snapshot(progress, now=None):
    if not progress:
        return None
    now = time.time() if now is None else now
    result = dict(progress)
    completed, total = result.get('completed'), result.get('total')
    elapsed = max(0, now - result['started_at'])
    result.update(
        phase_percent=round(100 * completed / total, 2) if total and completed is not None else None,
        rate_per_sec=round(completed / elapsed, 3) if completed is not None and elapsed else None,
        seconds_without_progress=round(max(0, now - result['last_advance_at']), 3))
    if result['seconds_without_progress'] >= 60:
        result['warning'] = 'NO_PROGRESS_OBSERVED'
    return result
