import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from hylms.core import HylmsError
from hylms.pending_agenda import create_agenda, extend_agenda, resolve_labels, validate_agenda
from hylms.runtime import RuntimeService
from hylms.storage import atomic_write_json
from hylms.workflow_adapters import pending_review
from tests.test_hylms_diff import pending, phase2_state
from tests.test_hylms_ntfy import NOW
from hylms.ntfy import prepare_ntfy_delivery
from tests.test_hylms_diff import course_payload
from tests.test_hylms_ntfy import write_ntfy_run


class PendingAgendaTests(unittest.TestCase):
    def test_initial_labels_sorted_by_id_with_resolved_baseline_excluded(self):
        agenda = create_agenda(["old-z", "old-a", "already-resolved"], ["new-z", "old-z", "new-a", "old-a"])
        self.assertEqual(agenda["labels"], {"new-a": "A1", "new-z": "A2", "old-a": "B1", "old-z": "B2"})

    def test_retired_labels_are_never_reused(self):
        agenda = create_agenda(["old-a", "old-b"], ["new-a", "new-b", "old-a", "old-b"])
        updated = extend_agenda(agenda, ["new-b", "old-b", "late-legacy"])
        self.assertEqual(updated["labels"]["new-b"], "A2")
        self.assertEqual(updated["labels"]["old-b"], "B2")
        self.assertEqual(updated["labels"]["late-legacy"], "B3")
        self.assertEqual(updated["labels"]["new-a"], "A1")

    def test_bad_duplicate_and_resolved_labels_are_rejected(self):
        agenda = create_agenda(["old"], ["new", "old"])
        for labels in (["A0"], ["1"], ["a1"], ["A1", "A1"], ["B2"], ["A1"]):
            with self.subTest(labels=labels):
                with self.assertRaises(HylmsError):
                    resolve_labels(agenda, ["old"], labels)
        self.assertEqual(resolve_labels(agenda, ["old", "new"], ["A1", "B1"]), ["new", "old"])

    def test_corrupted_agenda_cannot_silently_relabel(self):
        good = create_agenda(["old"], ["old", "new"])
        for change in ({"labels": {"old": "A1"}}, {"labels": {"old": "B1", "new": "B1"}}, {"schema_version": True}):
            with self.subTest(change=change):
                bad = copy.deepcopy(good)
                bad.update(change)
                with self.assertRaises(HylmsError):
                    validate_agenda(bad)

    def test_report_and_footer_new_only_old_only_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            write_ntfy_run(term, "run", NOW, course_payload())
            for new_count, old_count in ((2, 7), (0, 4), (2, 0), (0, 0)):
                with self.subTest(new=new_count, old=old_count):
                    state = phase2_state("run")
                    new = [f"new-{i}" for i in range(new_count)]
                    old = [f"old-{i}" for i in range(old_count)]
                    state["pending"] = [pending(x) for x in new + old]
                    review = pending_review(state, create_agenda(old, new + old))
                    delivery = prepare_ntfy_delivery(term, state, now=NOW, new_pending_ids=new)
                    self.assertEqual(review["counts"], {"new": new_count, "existing": old_count})
                    self.assertIn(f"확인대기 {new_count}+({old_count})", delivery["payloads"][-1]["payload"]["message"])


class PendingLabelAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = RuntimeService(self.root, launcher=lambda *args: os.getpid(), execution_check=lambda: None)
        self.folder = self.service.folder("session")
        self.state = phase2_state("run")
        self.state["pending"] = [pending("new"), pending("old")]
        atomic_write_json(self.root / "phase2_state.json", self.state)
        atomic_write_json(self.folder / "session.json", {"status": "completed", "pipeline_completed": True})
        self.service.save_agenda("session", create_agenda(["old"], ["new", "old"]))

    def test_batch_labels_become_ids_before_manual_worker(self):
        self.service.start("session", manual={"instruction": "A1 B1 확인 완료", "target_labels": ["A1", "B1"]})
        staged = json.loads((self.folder / "manual.json").read_text(encoding="utf-8"))
        self.assertEqual(set(staged["target_ids"]), {"new", "old"})
        self.assertNotIn("target_labels", staged)
        self.assertEqual(json.loads((self.root / "phase2_state.json").read_text(encoding="utf-8")), self.state)

    def test_resolved_label_is_not_reassigned_to_remaining_item(self):
        self.state["pending"] = [pending("old")]
        atomic_write_json(self.root / "phase2_state.json", self.state)
        before = (self.folder / "session.json").read_bytes()
        with self.assertRaises(HylmsError) as caught:
            self.service.start("session", manual={"instruction": "A1 완료", "target_labels": ["A1"]})
        self.assertEqual(caught.exception.code, "pending_label_stale")
        self.assertEqual((self.folder / "session.json").read_bytes(), before)
        self.assertFalse((self.folder / "manual.json").exists())

    def test_ids_and_labels_together_are_rejected(self):
        with self.assertRaises(HylmsError):
            self.service.start("session", manual={"instruction": "완료", "target_labels": ["A1"], "target_ids": ["old"]})

    def test_legacy_session_initializes_b_and_keeps_number_after_resolution(self):
        (self.folder / "pending-agenda.json").unlink()
        first = self.service.pending("session")
        self.assertEqual(first["counts"], {"new": 0, "existing": 2})
        self.state["pending"] = [pending("old")]
        atomic_write_json(self.root / "phase2_state.json", self.state)
        self.assertEqual(self.service.pending("session")["items"][0]["label"], "B2")

    def test_metadata_from_another_session_is_rejected(self):
        path = self.folder / "pending-agenda.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["session_id"] = "another"
        atomic_write_json(path, value)
        with self.assertRaises(HylmsError):
            self.service.pending("session")
