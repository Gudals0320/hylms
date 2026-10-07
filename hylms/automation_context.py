"""Compact, read-only context for the HY-LMS scheduling controller."""
import datetime as dt
import json
from pathlib import Path

from .core import KST
from .runtime import ROOT, RuntimeService, read_json


def occurrence(now):
    if now.tzinfo is None:
        raise ValueError('A timezone-aware clock is required')
    local = now.astimezone(KST)
    day = local.date()
    from .config import load_config
    hours = sorted(load_config().get('schedule_hours', [10, 19]))
    eligible = [h for h in hours if h <= local.hour]
    if eligible:
        hour = eligible[-1]
    else:
        day -= dt.timedelta(days=1)
        hour = hours[-1]
    return f'{day:%Y%m%d}T{hour:02d}00'


def context(root=ROOT, now=None):
    root = Path(root)
    key = occurrence(now or dt.datetime.now(KST))
    directory = root / '.hylms-runtime'
    route = read_json(directory / 'automation' / 'routing.json', {})
    admitted = read_json(directory / 'scheduled-runs' / f'{key}.json')
    state = read_json(root / 'phase2_state.json', {})
    thread = route.get('execution_thread_id')
    status = RuntimeService(root).status(thread) if thread else {'status': 'not_started'}
    # Do not replay professor bodies, credentials, or the entire conversation.
    return {'run_key': key, 'timezone': 'Asia/Seoul', 'routing': route,
            'occurrence_admitted': admitted,
            'execution': {k: status.get(k) for k in ('status', 'run_key', 'operation_id', 'code')},
            'term': state.get('term'), 'cursor': state.get('last_processed_run_id'),
            'last_failure': state.get('last_failure'),
            'natural_event_count': len(state.get('natural_events', [])), 'pending_count': len(state.get('pending', [])),
            'authoritative_context': str(root / 'hylms_context.sqlite3')}


if __name__ == '__main__':
    print(json.dumps(context(), ensure_ascii=True))
