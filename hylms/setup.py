"""Explicit local setup; never logs credentials or modifies external services."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import uuid

from .config import ROOT, config_path, load_config
from .core import KST
from .storage import atomic_write_json


def initialize_state(root, term):
    if not re.fullmatch(r'\d{2}-(?:1|2|summer|winter)', term):
        raise ValueError('Use a term ID such as 26-2')
    root = Path(root)
    state_path = root / 'phase2_state.json'
    if state_path.exists() or (root / 'snapshots' / term / 'runs').exists():
        raise ValueError('Existing state/snapshots must not be overwritten; use a fresh installation')
    now = dt.datetime.now(KST)
    run_id = 'bootstrap-' + uuid.uuid4().hex
    term_root = root / 'snapshots' / term
    status = {'schema_version': 5, 'term': {'id': term, 'name': 'Empty setup baseline', 'canvas_id': ''},
              'started_at': now.isoformat(), 'ended_at': now.isoformat(),
              'overall_status': 'success', 'exit_code': 0, 'courses': []}
    # Explicitly empty baseline ensures the first real snapshot's text is reviewed.
    atomic_write_json(term_root / 'runs' / run_id / 'status.json', status)
    state = {'schema_version': 5, 'phase': 2, 'term': term, 'timezone': 'Asia/Seoul',
             'last_processed_run_id': run_id, 'natural_events': [], 'pending': [],
             'rules': [], 'announcement_applications': [], 'last_failure': None}
    from .diff import validate_state_references
    validate_state_references(state, term_root)
    atomic_write_json(state_path, state)
    return {'status': 'initialized', 'term': term, 'external_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    configure = sub.add_parser('configure')
    configure.add_argument('--google-project', required=True)
    configure.add_argument('--ntfy-topic', required=True)
    state = sub.add_parser('init-state')
    state.add_argument('--term', required=True)
    sub.add_parser('check')
    sub.add_parser('calendar-marker')
    bind = sub.add_parser('bind-calendar')
    bind.add_argument('--calendar-id', required=True)
    bind.add_argument('--installation-id', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            from .execution_context import windows_token_user
            path = config_path()
            if path.exists():
                raise ValueError('Configuration already exists; edit it deliberately instead of overwriting')
            if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', args.google_project) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.ntfy_topic):
                raise ValueError('Invalid project ID or topic')
            atomic_write_json(path, {'windows_user': windows_token_user(), 'google_project': args.google_project,
                                    'ntfy_topic': args.ntfy_topic, 'schedule_hours': [10, 19], 'discussion_rules': []})
            result = {'status': 'configured', 'external_calls': 0}
        elif args.command == 'calendar-marker':
            from .google_calendar import calendar_marker
            identity = uuid.uuid4().hex
            result = {'status': 'ready', 'installation_id': identity,
                      'description': calendar_marker(identity), 'external_calls': 0}
        elif args.command == 'bind-calendar':
            from .execution_context import require_execution_owner
            require_execution_owner()
            result = bind_calendar(ROOT, args.calendar_id, args.installation_id)
        elif args.command == 'init-state':
            result = initialize_state(ROOT, args.term)
        else:
            settings = load_config()
            from .execution_context import execution_context
            if not all(settings.get(k) for k in ('windows_user', 'google_project', 'ntfy_topic')):
                raise ValueError('Run configure first')
            owner = execution_context()
            result = {'status': owner['status'], 'external_calls': 0,
                      'state_initialized': (ROOT / 'phase2_state.json').exists()}
        print(json.dumps(result))
        return 0 if result['status'] in {'configured', 'initialized', 'ready'} else 1
    except Exception as exc:
        # Avoid exposing file contents or account identifiers in diagnostics.
        print(json.dumps({'status': 'failed', 'code': getattr(exc, 'code', 'setup_invalid'),
                          'message': str(exc) if isinstance(exc, ValueError) else 'Local setup failed'}))
        return 1


def bind_calendar(root, calendar_id, installation_id, *, client=None):
    from .google_calendar import (GoogleCalendarClient, auth_identity, check_binding,
                                  save_sync_state, sync_lock)
    from .google_service_account import ServiceAccountAuth
    if calendar_id == 'primary' or not calendar_id.strip() or not re.fullmatch(r'[0-9a-f]{32}', installation_id):
        raise ValueError('Use a dedicated calendar and generated installation ID')
    path = Path(root) / 'google_calendar_state.json'
    with sync_lock(path):
        if path.exists():
            raise ValueError('Existing binding must be preserved; use documented migration instead')
        client = client or GoogleCalendarClient(ServiceAccountAuth())
        binding = {'schema_version': 2, 'installation_id': installation_id,
                   'auth': auth_identity(client.auth), 'calendar_id': calendar_id,
                   'phase': 'ready', 'events': {}, 'last_result': None}
        check_binding(client, binding)
        # list_events also enforces writer/owner access for service accounts.
        events = client.list_events(calendar_id)
        if any(e.get('extendedProperties', {}).get('private', {}).get('hylms_owner') for e in events):
            raise ValueError('Calendar already contains managed events; preserve its original binding')
        save_sync_state(path, binding)
        return {'status': 'ready', 'external_writes': 0}


if __name__ == '__main__':
    raise SystemExit(main())
