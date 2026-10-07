"""Install only the reviewed skill package, with verified staging and rollback."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path


def manifest(root):
    result = {}
    for path in Path(root).rglob("*"):
        if path.is_symlink():
            raise ValueError("Skill package must not contain symlinks")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def install(source, user_codex):
    source, user_codex = Path(source).resolve(), Path(user_codex).resolve()
    target = (user_codex / "skills" / "hylms").resolve()
    backups = (user_codex / "skill-backups").resolve()
    # Verify every move target before moving any directory, including on Windows.
    if target.parent != user_codex / "skills" or backups.parent != user_codex:
        raise ValueError("Unexpected installation path")
    expected = manifest(source)
    required = {"SKILL.md", "agents/openai.yaml", "scripts/hylms.py"}
    if not required <= set(expected):
        raise ValueError("Incomplete skill package")
    previous = manifest(target) if target.exists() else None
    if previous == expected:
        return {"status": "unchanged", "target": str(target), "files": len(expected)}
    backups.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex
    staging = (backups / f"hylms-staging-{suffix}").resolve()
    backup = (backups / f"hylms-previous-{suffix}").resolve()
    if staging.parent != backups or backup.parent != backups:
        raise ValueError("Unexpected backup path")
    shutil.copytree(source, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if manifest(staging) != expected:
        raise ValueError("Staged package verification failed")
    target.parent.mkdir(parents=True, exist_ok=True)
    if previous is not None:
        os.replace(target, backup)
    try:
        os.replace(staging, target)
        if manifest(target) != expected:
            raise ValueError("Installed package verification failed")
    except BaseException:
        if target.exists():
            os.replace(target, staging)
        if previous is not None:
            os.replace(backup, target)
        raise
    return {"status": "installed", "target": str(target), "files": len(expected),
            "backup": str(backup) if previous is not None else None}


if __name__ == "__main__":
    import argparse
    import tempfile
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "hylms"
        shutil.copytree(root / "skills" / "hylms", source)
        (source / "repository.json").write_text(json.dumps({"root": str(root)}), encoding="utf-8")
        print(json.dumps(install(source, args.codex_home), ensure_ascii=True))
