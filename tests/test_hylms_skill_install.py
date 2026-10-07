import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.install_hylms_skill import install, manifest


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        for name in ("SKILL.md", "agents/openai.yaml", "scripts/hylms.py"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        self.user = self.root / "user" / ".codex"

    def test_initial_install_and_repeat_are_identical(self):
        result = install(self.source, self.user)
        target = Path(result["target"])
        self.assertEqual(manifest(target), manifest(self.source))
        before = (target / "SKILL.md").stat().st_mtime_ns
        self.assertEqual(install(self.source, self.user)["status"], "unchanged")
        self.assertEqual((target / "SKILL.md").stat().st_mtime_ns, before)

    def test_existing_install_is_backed_up_before_replacement(self):
        first = install(self.source, self.user)
        target = Path(first["target"])
        (self.source / "SKILL.md").write_text("new", encoding="utf-8")
        second = install(self.source, self.user)
        self.assertEqual((Path(second["backup"]) / "SKILL.md").read_text(), "fixture")
        self.assertEqual((target / "SKILL.md").read_text(), "new")

    def test_move_failure_restores_existing_install(self):
        import os
        result = install(self.source, self.user)
        target = Path(result["target"])
        before = manifest(target)
        (self.source / "SKILL.md").write_text("new", encoding="utf-8")
        real_replace = os.replace
        def fail_staging(source, destination):
            if "staging" in Path(source).name:
                raise OSError("fixture failed promotion")
            return real_replace(source, destination)
        with mock.patch("scripts.install_hylms_skill.os.replace", side_effect=fail_staging):
            with self.assertRaises(OSError):
                install(self.source, self.user)
        self.assertEqual(manifest(target), before)

    def test_incomplete_package_never_replaces_install(self):
        result = install(self.source, self.user)
        before = manifest(Path(result["target"]))
        (self.source / "SKILL.md").unlink()
        with self.assertRaises(ValueError):
            install(self.source, self.user)
        self.assertEqual(manifest(Path(result["target"])), before)
