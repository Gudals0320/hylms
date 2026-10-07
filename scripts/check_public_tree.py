"""Fail closed on accidental private artifacts in the publishable Git tree."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FILES = {'hylms.local.json', 'repository.json', 'phase2_state.json',
                   'google_calendar_state.json', 'baseline_review.json', 'credentials.json', 'token.json'}
FORBIDDEN_PARTS = {'.hylms-runtime', 'snapshots', '.venv', 'venv', '__pycache__', 'private-backup'}
FORBIDDEN_SUFFIXES = {'.dpapi', '.pem', '.key', '.ics', '.sqlite', '.sqlite3', '.db', '.bundle', '.zip', '.log', '.pyc', '.bak'}


def scan(root=ROOT):
    result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            cwd=root, capture_output=True, check=True)
    errors = []
    for name in set(result.stdout.decode('utf-8').split('\0')) - {''}:
        path = Path(name)
        if (path.name in FORBIDDEN_FILES or path.suffix in FORBIDDEN_SUFFIXES or
                set(path.parts) & FORBIDDEN_PARTS or path.name.startswith('client_secret') or
                path.name == '.env' or path.name.startswith('.env.') and path.name != '.env.example'):
            errors.append((name, 'private_artifact'))
            continue
        target = root / path
        if target.is_symlink() or target.stat().st_size > 2_000_000:
            errors.append((name, 'unexpected_link_or_large_file'))
            continue
        try:
            text = target.read_text(encoding='utf-8')
        except UnicodeError:
            errors.append((name, 'unexpected_binary'))
            continue
        checks = {
            'private_key': r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----',
            'github_token': r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b',
            'google_api_key': r'\bAIza[A-Za-z0-9_-]{30,}\b',
            'personal_chat': r'codex://threads/[0-9a-f-]{36}',
            'personal_windows_path': r'(?i)C:[/\\]Users[/\\](?!example-user(?:[/\\]|["\s]))[^/\\\s"<>]+',
        }
        for code, pattern in checks.items():
            if re.search(pattern, text):
                errors.append((name, code))
    return errors


if __name__ == '__main__':
    issues = scan()
    for path, code in issues:
        print(f'{path}: {code}')
    print(f'Public tree check: {len(issues)} issue(s); values never printed')
    sys.exit(bool(issues))
