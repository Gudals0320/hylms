import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hylms import diff, qa
from hylms.core import HylmsError
from hylms.runtime import RuntimeService, main
from hylms.storage import atomic_write_json
from tests.test_hylms_diff import course_payload, write_run, phase2_state, event, pending, decision_result, qa_verdict
from tests.test_hylms_runtime import response


class ContextTests(unittest.TestCase):
    def test_four_additions_one_deletion_include_preserved_event_and_baselines(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            old = course_payload()
            old["weekly_learning"][0]["kind"] = "conference"
            new = copy.deepcopy(old)
            new["weekly_learning"] = []
            for number in range(4):
                item = copy.deepcopy(old["weekly_learning"][0])
                item.update(id=str(100 + number), kind="pdf" if number > 1 else "conference")
                item["submission"] = {"body": "PRIVATE-NOT-FOR-QA"}
                new["weekly_learning"].append(item)
            now = dt.datetime(2026, 9, 16, tzinfo=dt.timezone(dt.timedelta(hours=9)))
            write_run(term, "r0", now, old)
            write_run(term, "r1", now + dt.timedelta(days=1), new)
            future = copy.deepcopy(new)
            future["weekly_learning"][0]["kind"] = "FUTURE-NOT-FOR-QA"
            write_run(term, "r2", now + dt.timedelta(days=2), future)
            state = phase2_state("r0")
            existing = event("existing", confirmed={"title": now.isoformat()})
            existing["source_record_ids"] = ["weekly_learning:10"]
            state["natural_events"] = [existing]
            original = copy.deepcopy(state)
            packet = diff.prepare_decision_packet(term, state)
            self.assertEqual(len(packet["changes"]), 5)
            preview = diff.preview_state_transaction(term, state, decision_result(packet))
            context = preview["qa_context"]
            before = {e["id"]: e for e in context["current_entities"]}
            after = {e["id"]: e for e in context["candidate_entities"]}
            self.assertEqual(before["existing"], after["existing"])
            self.assertNotIn("existing", preview["changed_entity_ids"])
            self.assertEqual(len(preview["changed_entity_ids"]), 1)
            self.assertEqual(state, original)
            evidence = {e["qualified_id"]: e for e in context["structured_changes"]}
            self.assertEqual(evidence["weekly_learning:10"]["before"]["kind"], "conference")
            self.assertIsNone(evidence["weekly_learning:10"]["after"])
            for number in range(4):
                self.assertIsNone(evidence[f"weekly_learning:{100+number}"]["before"])
                self.assertIn("schedule", evidence[f"weekly_learning:{100+number}"]["after"])
                self.assertIn("progress", evidence[f"weekly_learning:{100+number}"]["after"])
            encoded = json.dumps(context)
            self.assertNotIn("PRIVATE-NOT-FOR-QA", encoded)
            self.assertNotIn("FUTURE-NOT-FOR-QA", encoded)
            qp = qa.prepare_qa_packet(context)
            self.assertEqual(qa.qa_next_action(qp, qa_verdict(qp)), "commit")

    def test_related_entity_closure_handles_manual_resolution_and_cycles(self):
        before = phase2_state("r0")
        p = pending("p")
        p["context"] = {"event_id": "e", "linked_announcement_application_ids": ["a"]}
        before["pending"] = [p, {**pending("a"), "context": {"linked_event_ids": ["e"]}}]
        before["natural_events"] = [event("e"), event("unrelated")]
        after = copy.deepcopy(before)
        after["pending"] = [after["pending"][1]]
        current, candidate = diff._qa_entities(before, after)
        self.assertEqual({e["id"] for e in current}, {"p", "a", "e"})
        self.assertEqual({e["id"] for e in candidate}, {"a", "e"})


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = RuntimeService(Path(self.temp.name))
        self.folder = self.service.folder("session")
        self.folder.mkdir(parents=True)
        self.packet = qa.prepare_qa_packet({"mode":"automatic", "transaction_id":"t", "candidate_state_sha256":"h",
            "changes":[{"id":"c"}], "structured_changes":[], "current_entities":[],
            "candidate_entities":[{"id":"p"}], "rules":[], "decision_draft":{}, "instruction":None})
        self.request = {"schema_version":1,"session_id":"session","operation_id":"op","request_id":"r",
            "binding":"binding","kind":"qa","payload":{"packet":self.packet}}
        atomic_write_json(self.folder / "request.json", self.request)
        self.verdict = qa_verdict(self.packet, "pending", [{"code":"MISSING", "message":"missing target",
            "change_ids":["c"],"entity_ids":["p","absent"],"field_paths":["candidate_entities"]}])

    def test_rejected_then_corrected_same_verdict(self):
        result = self.service.submit("session", response(self.request, self.verdict))
        self.assertEqual(result["reason"], "unknown_entity_id")
        self.assertFalse((self.folder / "response.json").exists())
        self.assertEqual(json.loads((self.folder / "request.json").read_text()), self.request)
        self.verdict["issues"][0]["entity_ids"] = ["p"]
        self.assertEqual(self.service.submit("session", response(self.request, self.verdict))["status"], "submitted")
        saved = json.loads((self.folder / "response.json").read_text())
        self.assertEqual(saved["result"]["verdict"], "pending")

    def test_second_invalid_submission_writes_explicit_failure(self):
        self.service.submit("session", response(self.request, self.verdict))
        second = self.service.submit("session", response(self.request, self.verdict))
        self.assertEqual(second["code"], "runtime_qa_format_exhausted")
        saved = json.loads((self.folder / "response.json").read_text())
        self.assertIsNone(saved["result"])
        self.assertEqual(saved["error"], "runtime_qa_format_exhausted")

    def test_cli_preserves_rejected_file_and_reports_safe_reason(self):
        source = self.folder / "submission.json"
        atomic_write_json(source, response(self.request, self.verdict))
        original = source.read_bytes()
        with mock.patch('hylms.runtime.RuntimeService', return_value=self.service), mock.patch('hylms.runtime.session_id', return_value='session'), mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['submit','--response-file',str(source)]),1)
            self.assertEqual(json.loads(output.getvalue())["reason"], "unknown_entity_id")
        self.assertEqual(source.read_bytes(), original)

    def test_stale_response_does_not_consume_format_repair(self):
        stale = response(self.request, self.verdict)
        stale["binding"] = "stale"
        with self.assertRaises(HylmsError):
            self.service.submit("session", stale)
        self.assertFalse((self.folder / "qa-submit-validation.json").exists())

    def test_unknown_change_and_hash_mismatch_rejected(self):
        self.verdict["issues"][0]["entity_ids"] = ["p"]
        self.verdict["issues"][0]["change_ids"] = ["missing"]
        result = self.service.submit("session", response(self.request, self.verdict))
        self.assertEqual(result["reason"], "unknown_change_id")
        self.verdict["qa_packet_id"] = "bad"
        result = self.service.submit("session", response(self.request, self.verdict))
        self.assertEqual(result["reason"], "packet_id_mismatch")

    def test_nonscalar_verdict_is_a_repairable_schema_error(self):
        self.verdict["verdict"] = ["pass"]
        result = self.service.submit("session", response(self.request, self.verdict))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "schema_or_privacy_violation")
        self.assertFalse((self.folder / "response.json").exists())
