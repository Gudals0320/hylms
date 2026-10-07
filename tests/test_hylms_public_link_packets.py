import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from hylms import diff
from hylms.core import HylmsError, KST
from hylms.qa import prepare_qa_packet, validate_qa_verdict
from hylms.storage import atomic_write_json
from tests.test_hylms_diff import course_payload, decision_result, pending, phase2_state, qa_verdict, write_run


class PublicLinkPacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.term = Path(self.temp.name) / "26-2"
        self.state = phase2_state("before")

    def packet(self, before, after):
        write_run(self.term, "before", dt.datetime(2026, 9, 8, tzinfo=KST), before)
        write_run(self.term, "after", dt.datetime(2026, 9, 9, tzinfo=KST), after)
        return diff.prepare_decision_packet(self.term, self.state)

    def test_new_assignment_with_empty_public_body_links_prepares(self):
        before, after = course_payload(), course_payload(extra=True)
        after["assignments"][0]["links"] = {"body": [], "attachments": []}
        packet = self.packet(before, after)
        change = next(x for x in packet["changes"] if x["section"] == "assignment")
        self.assertEqual(change["after"]["record_links"], {"content_links": [], "attachments": []})
        self.assertNotIn('"body"', json.dumps(packet))

    def test_discussion_change_keeps_both_sides_links(self):
        before = course_payload()
        before["discussions"][0]["links"] = {"body": [{"text": "old", "url": "https://example.test/old"}], "attachments": []}
        after = copy.deepcopy(before)
        after["discussions"][0]["links"]["body"][0]["url"] = "https://example.test/new"
        packet = self.packet(before, after)
        change = next(x for x in packet["changes"] if x["section"] == "discussion")
        self.assertEqual(change["before"]["record_links"]["content_links"][0]["url"], "https://example.test/old")
        self.assertEqual(change["after"]["record_links"]["content_links"][0]["url"], "https://example.test/new")

    def test_deleted_assignment_with_links_prepares_without_source_changes(self):
        before = course_payload(extra=True)
        before["assignments"][0]["links"] = {"body": [], "attachments": [{"id": "file", "filename": "handout.pdf"}]}
        after = course_payload()
        packet = self.packet(before, after)
        self.assertTrue(any(x["section"] == "assignment" and x["type"] == "deleted" for x in packet["changes"]))
        raw = json.loads((self.term / "runs" / "before" / "course__c101.json").read_text(encoding="utf-8"))
        self.assertIn("body", raw["assignments"][0]["links"])

    def test_private_submission_and_unexpected_attachment_fields_do_not_leak(self):
        after = course_payload(extra=True)
        after["assignments"][0]["links"] = {"body": [{"text": "handout", "url": "https://example.test/file", "body": "PRIVATE-LINK-BODY"}],
            "attachments": [{"id": "file", "filename": "handout.pdf", "url": "https://example.test/file",
                             "body": "PRIVATE-ATTACHMENT-BODY", "token": "PRIVATE-TOKEN"}]}
        after["assignments"][0]["submission"]["body"] = "PRIVATE-SUBMISSION"
        packet = self.packet(course_payload(), after)
        serialized = json.dumps(packet)
        self.assertNotIn("PRIVATE-", serialized)
        self.assertIn("handout.pdf", serialized)

    def test_sensitive_public_links_still_redacted(self):
        after = course_payload(extra=True)
        after["assignments"][0]["links"] = {"body": [{"text": "참고", "url": "https://docs.google.com/document/d/fixture"}], "attachments": []}
        packet = self.packet(course_payload(), after)
        self.assertNotIn("docs.google.com", json.dumps(packet))
        self.assertIn("sensitive-url sha256", json.dumps(packet))

    def test_actual_private_body_remains_forbidden(self):
        with self.assertRaises(HylmsError):
            diff._safe_json({"submission": {"body": "private"}})
        with self.assertRaises(HylmsError):
            diff._safe_json({"body": []})

    def test_packet_reaches_qa_and_commit_in_temporary_state(self):
        after = course_payload(extra=True)
        after["assignments"][0]["links"] = {"body": [{"text": "자료", "url": "https://example.test/file"}], "attachments": []}
        packet = self.packet(course_payload(), after)
        change = next(x for x in packet["changes"] if x["section"] == "assignment")
        item = pending("needs-date", source="assignment:30")
        item["context"] = {"record_links": change["after"]["record_links"]}
        decision = decision_result(packet, {change["id"]: [{"op": "upsert_pending", "value": item}]})
        preview = diff.preview_state_transaction(self.term, self.state, decision)
        qa_packet = prepare_qa_packet(preview["qa_context"])
        verdict = validate_qa_verdict(qa_verdict(qa_packet), qa_packet)
        path = Path(self.temp.name) / "state.json"
        atomic_write_json(path, self.state)
        result = diff.commit_state(path, self.state, decision, term_directory=self.term, qa_packet=qa_packet, qa_verdict=verdict)
        self.assertEqual(result["state"]["last_processed_run_id"], "after")
        self.assertEqual(result["state"]["pending"][0]["id"], "needs-date")

    def test_malformed_public_link_structure_is_not_silently_accepted(self):
        for value in ("private text", False, {"body": "private text"}, {"body": ["not a link"]}):
            with self.subTest(value=value), self.assertRaises(HylmsError):
                diff._public_record_links({"links": value})
