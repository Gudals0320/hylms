"""Synthetic material/assessment regressions; no real tokens or submission data."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hylms.collector import CanvasCollector
from hylms.collectors.assignments import external_tool_kind
from hylms.core import CANVAS_ORIGIN
from hylms.diff import next_run_diff
from hylms.google_calendar import (google_event_id, initialize_calendar, load_sync_state,
    prepare_google_sync, sync_google_calendar)
from hylms.ics import prepare_calendar, render_ics
from hylms.ntfy import prepare_ntfy_delivery
from hylms.storage import atomic_write_json
from tests.test_hylms_diff import course_payload, phase2_state
from tests.test_hylms_google_calendar import Backend
from tests.test_hylms_ntfy import NOW, write_ntfy_run

TEXT_URL = CANVAS_ORIGIN + "/learningx/lti/coursebuilder/view/text"


def raw_assignment(identity="1", **changes):
    return {"id": identity, "name": "학습 자료", "description": "자료 안내", "points_possible": 0.0,
            "omit_from_final_grade": True, "grading_type": "pass_fail", "submission_types": ["external_tool"],
            "external_tool_tag_attributes": {"url": TEXT_URL}, "due_at": NOW.isoformat(),
            "unlock_at": (NOW - dt.timedelta(days=1)).isoformat(),
            "lock_at": (NOW + dt.timedelta(days=90)).isoformat(), **changes}


def normalize(raw):
    return CanvasCollector(None, now=NOW, user_id="fixture")._normalize_assignment(
        raw, {"workflow_state": "unsubmitted"}, False, CANVAS_ORIGIN + "/courses/101")


def note_course(*, legacy=False):
    data = course_payload()
    data["schema_version"] = 5
    data["assignments"], data["weekly_learning"] = [], []
    for index in range(14):
        submission_types = ["none"] if index < 11 else ["external_tool"] if index < 13 else ["online_upload"]
        item = normalize(raw_assignment(str(index + 1), submission_types=submission_types))
        if legacy and index in {11, 12}:
            item.pop("external_tool_kind")
            item.update(submission_required=True, informational=False)
        data["assignments"].append(item)
        data["weekly_learning"].append({"id": f"wrapper-{index + 1}", "title": "주차 항목",
            "kind": "assignment", "detail_state": "not_applicable",
            "linked_entity": {"state": "linked", "kind": "assignment", "id": item["id"]},
            "schedule": copy.deepcopy(item["schedule"]), "progress": {"state": "known", "completed": False}})
    return data


class MaterialClassificationTests(unittest.TestCase):
    def test_known_text_with_all_facts_is_informational(self):
        result = normalize(raw_assignment())
        self.assertTrue(result["informational"])
        self.assertFalse(result["submission_required"])
        self.assertEqual(result["external_tool_kind"], "learningx_text")

    def test_query_values_and_external_tool_attributes_are_not_stored(self):
        # Deliberately fake query values, not production launch URLs or tokens.
        raw = raw_assignment(external_tool_tag_attributes={
            "url": TEXT_URL + "?access_token=fixture-only&signature=fixture-only#ignored", "content_id": 123})
        result = normalize(raw)
        self.assertEqual(result["external_tool_kind"], "learningx_text")
        serialized = json.dumps(result)
        self.assertNotIn("fixture-only", serialized)
        self.assertNotIn("external_tool_tag_attributes", serialized)
        self.assertNotIn("/learningx/lti/", serialized)

    def test_wrong_host_path_scheme_and_malformed_url_keep_submission(self):
        for url in ("https://learning.hanyang.ac.kr.example.test/learningx/lti/coursebuilder/view/text",
                    "https://example.test/learningx/lti/coursebuilder/view/text", TEXT_URL + "/assessment",
                    TEXT_URL.replace("/text", "/quiz"), TEXT_URL.replace("https:", "http:"),
                    TEXT_URL.replace("https://", "https://someone@"),
                    TEXT_URL.replace(".kr/", ".kr:8443/"), "https://[bad", "\n" + TEXT_URL):
            with self.subTest(url=url):
                result = normalize(raw_assignment(external_tool_tag_attributes={"url": url}))
                self.assertEqual(result["external_tool_kind"], "unknown")
                self.assertTrue(result["submission_required"])
                self.assertFalse(result["informational"])

    def test_missing_invalid_and_unknown_metadata_preserve_prior_behavior(self):
        for attributes in (None, {}, [], "invalid", {"url": None}, {"url": 123}, {"url": ""}):
            with self.subTest(attributes=attributes):
                result = normalize(raw_assignment(external_tool_tag_attributes=attributes))
                self.assertEqual(result["external_tool_kind"], "unknown")
                self.assertTrue(result["submission_required"])
                self.assertFalse(result["informational"])

    def test_upload_and_mixed_types_are_not_excluded(self):
        for types in (["online_upload"], ["online_text_entry"], ["external_tool", "online_upload"], ["external_tool", "none"]):
            with self.subTest(types=types):
                result = normalize(raw_assignment(submission_types=types))
                self.assertTrue(result["submission_required"])
                self.assertFalse(result["informational"])
                if "external_tool" not in types:
                    self.assertNotIn("external_tool_kind", result)

    def test_nonzero_invalid_points_and_grade_inclusion_keep_assessments(self):
        for changes in ({"points_possible": 1}, {"points_possible": -1}, {"points_possible": "0"},
                        {"points_possible": False}, {"points_possible": None}, {"points_possible": float("nan")},
                        {"points_possible": float("inf")}, {"omit_from_final_grade": False},
                        {"omit_from_final_grade": None}, {"omit_from_final_grade": 1}):
            with self.subTest(changes=changes):
                result = normalize(raw_assignment(**changes))
                self.assertTrue(result["submission_required"])
                self.assertFalse(result["informational"])

    def test_relative_same_origin_url_is_recognized_without_network(self):
        self.assertEqual(external_tool_kind({"url": "/learningx/lti/coursebuilder/view/text"}), "learningx_text")


class MaterialOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.term = self.root / "26-2"
        self.source, self.binding = self.root / "state.json", self.root / "google.json"
        self.data = note_course()
        self.index = 0
        self.write(self.data)
        self.state = phase2_state("run-0")
        atomic_write_json(self.source, self.state)
        self.backend = Backend()
        self.calendar_id = initialize_calendar(self.backend, self.binding)["calendar_id"]
        for target in ("socket.socket", "webbrowser.open", "hylms.credentials.WindowsCredentialStore._read_bytes",
                       "hylms.credentials.WindowsCredentialStore._write_bytes"):
            patcher = mock.patch(target, side_effect=AssertionError("production I/O forbidden"))
            self.addCleanup(patcher.stop)
            blocked = patcher.start()
            self.addCleanup(blocked.assert_not_called)

    def write(self, data, course_status="updated"):
        run = write_ntfy_run(self.term, f"run-{self.index}", NOW + dt.timedelta(seconds=self.index), data, course_status=course_status)
        for path in (run / "status.json", self.term / "status.json"):
            status = json.loads(path.read_text(encoding="utf-8"))
            status["schema_version"] = 5
            atomic_write_json(path, status)
        self.index += 1
        return run

    def sync(self, *, plan=False):
        return sync_google_calendar(self.term, self.source, state_path=self.binding,
                                    client=self.backend, clock=lambda: NOW, plan_only=plan)

    def seed_old_calendar(self):
        projection = prepare_calendar(self.term, self.state, now=NOW)
        for event in projection["events"]:
            event["completion"] = "incomplete"  # Before-fix inclusion behavior.
        binding = load_sync_state(self.binding)
        for action in prepare_google_sync(projection, binding, []):
            self.backend.insert(self.calendar_id, action["body"])
        return projection

    def test_fourteen_notes_keep_ics_but_only_upload_enters_google_ntfy(self):
        projection = prepare_calendar(self.term, self.state, now=NOW)
        self.assertEqual(len(projection["events"]), 14)
        self.assertEqual(render_ics(projection).count(b"BEGIN:VEVENT"), 14)
        self.assertEqual(sum(e["completion"] == "excluded" for e in projection["events"]), 13)
        upload = next(e for e in projection["events"] if e["identity"] == "101:assignment:14")
        self.assertEqual(upload["completion"], "incomplete")
        self.assertTrue(all(len(e["source_record_ids"]) == 2 for e in projection["events"]))
        ntfy = prepare_ntfy_delivery(self.term, self.state, now=NOW)
        self.assertEqual([x["identity"] for x in ntfy["targets"]], ["101:assignment:14"])
        result = self.sync()
        self.assertEqual(result["excluded_non_actionable"], 13)
        self.assertEqual(result["excluded_completed"], 0)
        self.assertEqual(result["counts"]["created"], 1)
        self.assertNotIn("excluded_non_actionable", result["counts"])
        self.assertEqual(json.loads(self.source.read_text(encoding="utf-8"))["schema_version"], 5)

    def test_wrong_existing_events_are_deleted_then_noop(self):
        original = self.seed_old_calendar()
        expected_deleted = {google_event_id(e["uid"]) for e in original["events"] if e["identity"] != "101:assignment:14"}
        before = copy.deepcopy(self.backend.events)
        binding_before = self.binding.read_bytes()
        source_before = {p: p.read_bytes() for p in [self.source, *self.term.rglob('*.json')]}
        plan = self.sync(plan=True)
        self.assertEqual(plan["counts"]["deleted"], 13)
        self.assertEqual({x["id"] for x in plan["operations"] if x["operation"] == "delete"}, expected_deleted)
        self.assertEqual(self.backend.events, before)
        self.assertEqual(self.binding.read_bytes(), binding_before)
        result = self.sync()
        self.assertEqual(result["counts"]["deleted"], 13)
        self.assertTrue(all(self.backend.events[x]["status"] == "cancelled" for x in expected_deleted))
        again = self.sync()
        self.assertEqual(again["counts"]["deleted"], 0)
        self.assertEqual(again["counts"]["unchanged"], 1)
        self.assertTrue(all(p.read_bytes() == content for p, content in source_before.items()))

    def test_old_snapshot_missing_kind_is_not_guessed_from_title(self):
        legacy = note_course(legacy=True)
        self.write(legacy)
        before = {p: p.read_bytes() for p in self.term.rglob('*.json')}
        plan = self.sync(plan=True)
        self.assertEqual(plan["excluded_non_actionable"], 11)
        self.assertEqual(plan["counts"]["created"], 3)
        self.assertTrue(all(p.read_bytes() == content for p, content in before.items()))

    def test_new_collection_changes_classification_without_rewriting_old_run(self):
        legacy = note_course(legacy=True)
        legacy_path = self.write(legacy)
        old = {p: p.read_bytes() for p in legacy_path.rglob('*.json')}
        self.state["last_processed_run_id"] = legacy_path.name
        atomic_write_json(self.source, self.state)
        self.write(note_course())
        delta = next_run_diff(self.term, self.state)
        modified = {x["qualified_id"]: x for x in delta["structured_modified"]}
        self.assertIn("assignment:12", modified)
        self.assertIn("external_tool_kind", modified["assignment:12"]["actionable_paths"])
        self.assertEqual(self.sync(plan=True)["excluded_non_actionable"], 13)
        self.assertTrue(all(p.read_bytes() == content for p, content in old.items()))

    def test_partial_collection_preserves_last_successful_classification(self):
        self.write(note_course(legacy=True), course_status="kept")
        result = self.sync(plan=True)
        self.assertEqual(result["excluded_non_actionable"], 13)
        self.assertEqual(result["counts"]["created"], 1)

    def test_material_becoming_upload_is_restored_with_original_id(self):
        original = self.seed_old_calendar()
        old_id = google_event_id(next(e["uid"] for e in original["events"] if e["identity"] == "101:assignment:12"))
        self.sync()
        changed = note_course()
        changed["assignments"][11] = normalize(raw_assignment("12", submission_types=["online_upload"]))
        self.write(changed)
        result = self.sync()
        self.assertEqual(result["counts"]["restored"], 1)
        self.assertEqual(self.backend.events[old_id]["status"], "confirmed")

    def test_user_prior_term_and_attendee_events_survive_exclusion(self):
        self.seed_old_calendar()
        selected = next(iter(self.backend.events.values()))
        selected["attendees"] = [{"email": "guest@example.test"}]
        attendee = copy.deepcopy(selected)
        old = copy.deepcopy(selected)
        old.pop("attendees")
        props = old["extendedProperties"]["private"]
        props["hylms_term"] = "26-1"
        props["hylms_uid"] = "a" * 64 + "@hylms.local"
        old["id"] = google_event_id(props["hylms_uid"])
        user = {"id": "user-created", "summary": "Personal event", "status": "confirmed"}
        self.backend.events[old["id"]], self.backend.events[user["id"]] = copy.deepcopy(old), copy.deepcopy(user)
        result = self.sync()
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(self.backend.events[attendee["id"]], attendee)
        self.assertEqual(self.backend.events[old["id"]], old)
        self.assertEqual(self.backend.events[user["id"]], user)
        self.assertEqual(result["counts"]["deleted"], 12)

    def test_done_and_non_actionable_counts_are_separate(self):
        changed = note_course()
        changed["assignments"][-1]["progress"].update(submitted=True, workflow_state="submitted")
        self.write(changed)
        result = self.sync(plan=True)
        self.assertEqual(result["excluded_non_actionable"], 13)
        self.assertEqual(result["excluded_completed"], 1)
        self.assertEqual(sum(result["counts"].values()), 0)
