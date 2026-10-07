"""Non-secret, per-installation settings. Credentials never belong here."""
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent


def config_path():
    return Path(os.environ.get('HYLMS_CONFIG', ROOT / 'hylms.local.json')).resolve()


def load_config():
    path = config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        allowed = {'windows_user', 'google_project', 'ntfy_topic', 'discussion_rules',
                   'legacy_event_authorities', 'schedule_hours'}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError()
        for key in ('windows_user', 'google_project', 'ntfy_topic'):
            if key in value and (not isinstance(value[key], str) or not value[key].strip()):
                raise ValueError()
        if 'google_project' in value and not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', value['google_project']):
            raise ValueError()
        if 'ntfy_topic' in value and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['ntfy_topic']):
            raise ValueError()
        hours = value.get('schedule_hours', [10, 19])
        if not isinstance(hours, list) or not hours or any(type(h) is not int or not 0 <= h <= 23 for h in hours) or len(set(hours)) != len(hours):
            raise ValueError()
        rules = value.get('discussion_rules', [])
        if not isinstance(rules, list):
            raise ValueError()
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != {'course_id', 'discussion_id', 'rule_id'} or any(not isinstance(v, str) or not v for v in rule.values()):
                raise ValueError()
        legacy = value.get('legacy_event_authorities', {})
        if not isinstance(legacy, dict) or any(not isinstance(k, str) or v not in {'lms', 'user', 'not_applicable'} for k, v in legacy.items()):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError('Invalid local configuration; no configuration contents are logged') from None
