from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hylms_snapshot as hs


def course_payload(*, announcement="old", due="2026-09-10T23:59:00+09:00", discussion_count=1,
                   discussion_state="available", discussion_graded=False,
                   discussion_workflow=None, own_entry_count=0, extra=False,
                   collected_at="one"):
    assignments = []
    if extra:
        assignments.append({
            "id": "30",
            "title": "새 과제",
            "description": {"text": "", "links": [], "images": []},
            "source_url": "https://example.test/assignments/30",
            "access": {"state": "available", "reason": None},
            "progress": {"submitted": False, "graded": False, "workflow_state": "unsubmitted"},
            "schedule": {"basis": "course_default", "default": None, "effective": {}},
            "submission": {"workflow_state": "unsubmitted", "body": "must not leak"},
            "submission_required": True,
        })
    return {
        "schema_version": 4,
        "collected_at": collected_at,
        "course": {"id": "101", "name": "자료구조", "url": "https://example.test/courses/101"},
        "syllabus": {"text": "", "links": [], "images": []},
        "announcements": [{
            "id": "1",
            "title": "공지",
            "message": {"text": announcement, "links": [], "images": []},
            "posted_at": "2026-09-01T00:00:00+09:00",
            "source_url": "https://example.test/announcements/1",
            "document_refs": [],
        }],
        "assignments": assignments,
        "quizzes": [],
        "discussions": [{
            "id": "20",
            "title": "토론",
            "prompt": {"text": "질문" if discussion_state == "available" else "", "links": [], "images": []},
            "detail_state": discussion_state,
            "assignment_id": "40" if discussion_graded else None,
            "last_reply_at": "2026-09-01T00:00:00+09:00",
            "reply_count": discussion_count,
            "unread_count": discussion_count,
            "source_url": "https://example.test/discussions/20",
            "entries": {
                "state": "unavailable" if discussion_state == "unavailable" else "collected",
                "reason": "detail_request_failed" if discussion_state == "unavailable" else None,
                "items": [] if discussion_state == "unavailable" else [
                    {
                        "id": str(100 + index),
                        "created_at": f"2026-09-0{index + 1}T10:00:00+09:00",
                        "updated_at": f"2026-09-0{index + 1}T11:00:00+09:00",
                        "body": {"text": f"private-own-body-{index}"},
                        "feedback_entries": [{"author_display_name": "Peer", "body": {"text": "peer-body"}}],
                    }
                    for index in range(own_entry_count)
                ],
            },
            "submission": {
                "workflow_state": discussion_workflow,
                "attempt": 1,
                "submitted_at": "2026-09-01T10:00:00+09:00" if discussion_workflow in {"submitted", "graded"} else None,
                "graded_at": "2026-09-02T10:00:00+09:00" if discussion_workflow == "graded" else None,
                "excused": False,
                "late": False,
                "missing": False,
                "seconds_late": 0,
                "body": "private-submission-body",
            } if discussion_graded else None,
        }],
        "weekly_learning": [{
            "id": "10",
            "title": "1주차",
            "source_url": "https://example.test/weekly/10",
            "schedule": {"basis": "provider_effective", "default": None, "effective": {"due_at": {"state": "known", "value": due}}},
            "access": {"state": "available", "reason": None},
            "progress": {"completed": False, "state": "known"},
            "attendance": {"status": "none", "provider_status": "NONE", "targeted": True},
            "detail_state": "available",
        }],
    }


def write_run(term: Path, run_id: str, started_at: dt.datetime, payload: dict, *,
              course_status="updated", filename="course__c101.json") -> Path:
    run = term / "runs" / run_id
    run.mkdir(parents=True)
    hs.atomic_write_json(run / filename, payload)
    hs.atomic_write_json(run / "status.json", {
        "schema_version": 4,
        "term": {"id": term.name, "name": "2026년 2학기", "canvas_id": "1"},
        "started_at": started_at.isoformat(timespec="seconds"),
        "ended_at": started_at.isoformat(timespec="seconds"),
        "overall_status": "success" if course_status == "updated" else "partial_failure",
        "exit_code": 0 if course_status == "updated" else 2,
        "courses": [{"id": "101", "name": "자료구조", "path": filename, "status": course_status}],
    })
    return run


def phase2_state(cursor: str) -> dict:
    return {
        "schema_version": hs.STATE_SCHEMA_VERSION,
        "phase": 2,
        "term": "26-2",
        "timezone": "Asia/Seoul",
        "last_processed_run_id": cursor,
        "natural_events": [],
        "pending": [],
        "rules": [],
        "announcement_applications": [],
        "last_failure": None,
    }


def event(event_id: str, *, kind="class_session", mode="session", all_day=False,
          start="2026-09-01T10:00:00+09:00", end="2026-09-01T11:00:00+09:00",
          authority=None, confirmed=None) -> dict:
    class_kind = kind in {"class_replacement", "class_session", "special_event"}
    return {
        "id": event_id,
        "status": "active",
        "course_id": "101",
        "course_name": "자료구조",
        "title": "일정",
        "kind": kind,
        "timing": {"mode": mode, "all_day": all_day, "start": start, "end": end, "end_inclusive": all_day},
        "optional": False,
        "action_state": "not_applicable" if class_kind else "unknown",
        "action_state_authority": authority or ("not_applicable" if class_kind else "user"),
        "user_confirmed": copy.deepcopy(confirmed or {}),
        "location": None,
        "attendance_required": None,
        "source_record_ids": ["announcement:1"],
        "evidence": [],
        "details": {},
    }


def announcement_application(application_id="application") -> dict:
    return {
        "id": application_id,
        "course_id": "101",
        "course_name": "자료구조",
        "source_record_ids": ["announcement:1"],
        "target_record_ids": ["weekly_learning:10"],
        "patch": {
            "timing": None,
            "attendance_required": None,
            "attendance_excluded_weeks": [],
            "attendance_status_check_required": None,
            "minimum_study_time_required": None,
            "delivery_mode": None,
            "maximum_playback_speed": None,
            "required_for_all": None,
            "optional": None,
            "location": None,
            "requirements": [],
            "consequence": None,
            "details": {},
        },
        "evidence": [],
    }


def pending(pending_id="pending", *, source="announcement:1") -> dict:
    return {
        "id": pending_id,
        "status": "pending",
        "course_id": "101",
        "course_name": "자료구조",
        "title": "확인 필요",
        "source_record_ids": [source],
        "reason": "일정이 명확하지 않습니다.",
        "context": {},
    }


def decision_result(packet: dict, operations: dict[str, list[dict]] | None = None) -> dict:
    operations = operations or {}
    decisions = []
    for change in packet["changes"]:
        change_operations = copy.deepcopy(operations.get(change["id"], []))
        decisions.append({
            "change_id": change["id"],
            "disposition": "mutate" if change_operations else "no_additional_change",
            "reason": "fixture decision",
            "operations": change_operations,
        })
    return {
        "schema_version": hs.DECISION_SCHEMA_VERSION,
        "transaction_id": packet["transaction"]["id"],
        "decisions": decisions,
    }


def qa_verdict(packet: dict, verdict="pass", issues=None) -> dict:
    return {
        "schema_version": hs.QA_SCHEMA_VERSION,
        "qa_packet_id": packet["qa_packet_id"],
        "review_id": packet["review_id"],
        "attempt": packet["attempt"],
        "verdict": verdict,
        "issues": copy.deepcopy(issues or []),
    }


def commit_with_pass(path: Path, state: dict, result: dict, term: Path) -> dict:
    preview = hs.preview_state_transaction(term, state, result)
    packet = hs.prepare_qa_packet(preview["qa_context"])
    return hs.commit_state(
        path,
        state,
        result,
        term_directory=term,
        qa_packet=packet,
        qa_verdict=qa_verdict(packet),
    )


class PhaseTwoDiffTest(unittest.TestCase):
    def test_stable_ids_split_added_text_and_structured_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(announcement="new", due="2026-09-11T23:59:00+09:00", extra=True),
                filename="renamed__c101.json",
            )

            packet = hs.next_run_diff(term, phase2_state(first))

            self.assertEqual(second, packet["current_run_id"])
            self.assertEqual([], packet["course_added"])
            self.assertEqual(1, packet["counts"]["added"])
            self.assertEqual(1, packet["counts"]["text_modified"])
            self.assertEqual(1, packet["counts"]["structured_modified"])
            self.assertNotIn("structured", packet["added"][0])
            self.assertNotIn("text", packet["structured_added"][0])
            self.assertNotIn("must not leak", json.dumps(packet, ensure_ascii=False))

    def test_complete_run_deletion_keeps_previous_text_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            before = course_payload()
            after = course_payload()
            after["weekly_learning"] = []
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), before)
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), after)

            packet = hs.next_run_diff(term, phase2_state(first))

            self.assertEqual(1, packet["counts"]["deleted"])
            self.assertEqual("1주차", packet["deleted"][0]["text"]["title"])
            self.assertNotIn("text", packet["structured_deleted"][0])

    def test_cursor_processes_every_run_and_volatile_changes_are_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            ids = ["20260901T000000+0900", "20260902T000000+0900", "20260903T000000+0900"]
            write_run(term, ids[0], dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(term, ids[1], dt.datetime(2026, 9, 2, tzinfo=hs.KST), course_payload(announcement="new"))
            write_run(term, ids[2], dt.datetime(2026, 9, 3, tzinfo=hs.KST), course_payload(announcement="new", collected_at="three"))
            state_path = root / "state.json"
            state = phase2_state(ids[0])
            hs.atomic_write_json(state_path, state)

            first_packet = hs.next_run_diff(term, state)
            self.assertEqual(ids[1], first_packet["current_run_id"])
            prepared = hs.prepare_decision_packet(term, state)
            state = hs.commit_state(
                state_path, state, decision_result(prepared), term_directory=term
            )["state"]
            second_packet = hs.next_run_diff(term, state)
            self.assertEqual(ids[2], second_packet["current_run_id"])
            self.assertEqual(0, second_packet["counts"]["added"])
            self.assertEqual(0, second_packet["counts"]["text_modified"])
            self.assertEqual(0, second_packet["counts"]["structured_modified"])

    def test_unavailable_record_uses_nearest_earlier_usable_value(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            ids = ["20260901T000000+0900", "20260902T000000+0900", "20260903T000000+0900"]
            write_run(term, ids[0], dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term, ids[1], dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(discussion_state="unavailable"), course_status="updated_with_warnings",
            )
            write_run(term, ids[2], dt.datetime(2026, 9, 3, tzinfo=hs.KST), course_payload(discussion_count=2))

            skipped = hs.next_run_diff(term, phase2_state(ids[0]))
            self.assertEqual(0, skipped["counts"]["text_modified"])
            recovered = hs.next_run_diff(term, phase2_state(ids[1]))
            changed = [item for item in recovered["structured_modified"] if item["record_id"] == "20"]
            self.assertEqual(1, len(changed))
            self.assertEqual([], changed[0]["actionable_paths"])
            self.assertIn("reply_count", changed[0]["diagnostic_paths"])

    def test_participation_state_change_is_actionable_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(
                term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST),
                course_payload(discussion_graded=True, discussion_workflow="unsubmitted"),
            )
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(
                    discussion_graded=True, discussion_workflow="submitted", own_entry_count=1,
                ),
            )

            packet = hs.next_run_diff(term, phase2_state(first))
            changed = next(item for item in packet["structured_modified"] if item["record_id"] == "20")
            encoded = json.dumps(changed, ensure_ascii=False)

            self.assertEqual("not_participated", changed["before"]["participation"]["state"])
            self.assertEqual("participated", changed["after"]["participation"]["state"])
            self.assertIn("participation.state", changed["actionable_paths"])
            self.assertIn("participation.own_entry_count", changed["diagnostic_paths"])
            self.assertIn("participation.submission.workflow_state", changed["diagnostic_paths"])
            for forbidden in ("private-own-body", "peer-body", "Peer", "private-submission-body"):
                self.assertNotIn(forbidden, encoded)

    def test_additional_own_reply_while_participated_is_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(
                term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST),
                course_payload(
                    discussion_graded=True, discussion_workflow="submitted", own_entry_count=1,
                ),
            )
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(
                    discussion_graded=True, discussion_workflow="submitted", own_entry_count=2,
                ),
            )

            packet = hs.next_run_diff(term, phase2_state(first))
            changed = next(item for item in packet["structured_modified"] if item["record_id"] == "20")

            self.assertEqual([], changed["actionable_paths"])
            self.assertIn("participation.own_entry_count", changed["diagnostic_paths"])
            self.assertIn("participation.last_own_reply_at", changed["diagnostic_paths"])

    def test_kept_course_does_not_delete_or_advance_records(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            payload = course_payload()
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), payload)
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), payload, course_status="kept")

            packet = hs.next_run_diff(term, phase2_state(first))

            self.assertEqual(0, packet["counts"]["deleted"])
            self.assertEqual([{"course_id": "101", "status": "kept"}], packet["skipped_courses"])

    def test_failed_commit_keeps_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            term = Path(directory) / "26-2"
            write_run(term, "old", dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(term, "new", dt.datetime(2026, 9, 2, tzinfo=hs.KST), course_payload())
            state = phase2_state("old")
            hs.atomic_write_json(path, state)
            before = path.read_bytes()
            prepared = hs.prepare_decision_packet(term, state)

            with mock.patch("hylms.diff.atomic_write_json", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    hs.commit_state(
                        path, state, decision_result(prepared), term_directory=term
                    )

            self.assertEqual(before, path.read_bytes())
            self.assertEqual("old", json.loads(path.read_text(encoding="utf-8"))["last_processed_run_id"])


class PhaseTwoDecisionCommitTest(unittest.TestCase):
    def test_prepare_packet_is_stable_bound_and_secret_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term,
                second,
                dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(
                    announcement="입장코드 secret https://docs.google.com/forms/d/example",
                    due="2026-09-11T23:59:00+09:00",
                ),
            )
            state = phase2_state(first)

            first_packet = hs.prepare_decision_packet(term, state)
            second_packet = hs.prepare_decision_packet(term, state)
            serialized = json.dumps(first_packet, ensure_ascii=False)

            self.assertEqual(first_packet, second_packet)
            self.assertEqual(first, first_packet["transaction"]["previous_run_id"])
            self.assertEqual(second, first_packet["transaction"]["current_run_id"])
            self.assertEqual(1, len(first_packet["changes"]))
            self.assertEqual("text_modified", first_packet["changes"][0]["type"])
            self.assertNotIn("structured", serialized)
            self.assertNotIn("secret", serialized)
            self.assertNotIn("docs.google.com/forms", serialized)
            self.assertIn("[REDACTED]", serialized)

    def test_event_pending_and_application_commit_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(announcement="일정과 적용 규칙 변경"),
            )
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            packet = hs.prepare_decision_packet(term, state)
            change_id = packet["changes"][0]["id"]
            new_event = event("new-event")
            new_pending = pending("new-pending")
            application = announcement_application("new-application")
            result = decision_result(packet, {change_id: [
                {"op": "upsert_event", "value": new_event},
                {"op": "upsert_pending", "value": new_pending},
                {"op": "upsert_announcement_application", "value": application},
            ]})

            committed = commit_with_pass(path, state, result, term)

            self.assertEqual(second, committed["state"]["last_processed_run_id"])
            self.assertEqual([new_event], committed["state"]["natural_events"])
            self.assertEqual([new_pending], committed["state"]["pending"])
            self.assertEqual([application], committed["state"]["announcement_applications"])
            self.assertEqual(1, committed["receipt"]["operation_counts"]["upsert_event"])
            self.assertEqual(
                ["new-pending"],
                [item["id"] for item in committed["receipt"]["new_pending"]],
            )
            self.assertIsNone(committed["state"]["last_failure"])

    def test_explicit_cancel_and_pending_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(announcement="기존 일정 취소"),
            )
            path = root / "state.json"
            state = phase2_state(first)
            state["natural_events"] = [event("existing-event")]
            state["pending"] = [pending("existing-pending")]
            hs.atomic_write_json(path, state)
            packet = hs.prepare_decision_packet(term, state)
            change_id = packet["changes"][0]["id"]
            result = decision_result(packet, {change_id: [
                {
                    "op": "cancel_event",
                    "id": "existing-event",
                    "source_record_ids": ["announcement:1"],
                    "evidence": ["취소 공지"],
                },
                {"op": "resolve_pending", "id": "existing-pending"},
            ]})

            committed = commit_with_pass(path, state, result, term)["state"]

            self.assertEqual("cancelled", committed["natural_events"][0]["status"])
            self.assertIn("취소 공지", committed["natural_events"][0]["evidence"])
            self.assertEqual([], committed["pending"])

    def test_structured_only_change_advances_with_empty_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(due="2026-09-12T23:59:00+09:00"),
            )
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            packet = hs.prepare_decision_packet(term, state)

            self.assertEqual([], packet["changes"])
            result = hs.commit_state(
                path, state, decision_result(packet), term_directory=term
            )

            self.assertEqual(second, result["state"]["last_processed_run_id"])
            self.assertTrue(all(count == 0 for count in result["receipt"]["operation_counts"].values()))

    def test_decision_coverage_and_duplicate_targets_are_rejected_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(announcement="changed"),
            )
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            before = path.read_bytes()
            packet = hs.prepare_decision_packet(term, state)
            valid = decision_result(packet)
            missing = copy.deepcopy(valid)
            missing["decisions"] = []
            duplicate = copy.deepcopy(valid)
            duplicate["decisions"].append(copy.deepcopy(duplicate["decisions"][0]))
            unknown = copy.deepcopy(valid)
            unknown["decisions"][0]["change_id"] = "change:unknown"

            for invalid in (missing, duplicate, unknown):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(hs.HylmsError) as raised:
                        hs.commit_state(path, state, invalid, term_directory=term)
                    self.assertEqual("phase2_decision_invalid", raised.exception.code)
                    self.assertEqual(before, path.read_bytes())

            change_id = packet["changes"][0]["id"]
            duplicate_target = decision_result(packet, {change_id: [
                {"op": "upsert_event", "value": event("same-event")},
                {"op": "upsert_event", "value": event("same-event")},
            ]})
            with self.assertRaises(hs.HylmsError) as raised:
                hs.commit_state(path, state, duplicate_target, term_directory=term)
            self.assertEqual("phase2_decision_conflict", raised.exception.code)
            self.assertEqual(before, path.read_bytes())

    def test_malformed_operations_and_references_are_rejected_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(announcement="changed"),
            )
            path = root / "state.json"
            state = phase2_state(first)
            state["rules"] = [{"id": "reserved", "source": "user", "text": "rule"}]
            hs.atomic_write_json(path, state)
            before = path.read_bytes()
            packet = hs.prepare_decision_packet(term, state)
            change_id = packet["changes"][0]["id"]
            bad_time = event("bad-time")
            bad_time["timing"]["end"] = "2026-09-01T11:00:00"
            missing_ref = event("missing-ref")
            missing_ref["source_record_ids"].append("announcement:999")
            cross_course = event("cross-course")
            cross_course["course_id"] = "999"
            duplicate_id = event("reserved")
            bad_patch = announcement_application("bad-patch")
            bad_patch["patch"]["extra"] = True
            incomplete_patch = announcement_application("incomplete-patch")
            del incomplete_patch["patch"]["optional"]
            missing_target = announcement_application("missing-target")
            missing_target["target_record_ids"] = ["weekly_learning:999"]
            cases = (
                (bad_time, "upsert_event", "phase2_decision_invalid"),
                (missing_ref, "upsert_event", "phase2_decision_conflict"),
                (cross_course, "upsert_event", "phase2_decision_conflict"),
                (duplicate_id, "upsert_event", "phase2_decision_conflict"),
                (bad_patch, "upsert_announcement_application", "phase2_decision_invalid"),
                (incomplete_patch, "upsert_announcement_application", "phase2_decision_invalid"),
                (missing_target, "upsert_announcement_application", "phase2_decision_conflict"),
            )

            for value, operation, code in cases:
                with self.subTest(operation=operation, value=value["id"]):
                    result = decision_result(packet, {change_id: [{"op": operation, "value": value}]})
                    with self.assertRaises(hs.HylmsError) as raised:
                        hs.commit_state(path, state, result, term_directory=term)
                    self.assertEqual(code, raised.exception.code)
                    self.assertEqual(before, path.read_bytes())

    def test_stale_state_and_changed_diff_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            second_run = write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(announcement="changed"),
            )
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            packet = hs.prepare_decision_packet(term, state)
            result = decision_result(packet)

            failed = copy.deepcopy(state)
            failed["last_failure"] = {"run_id": second, "stage": "interpret", "code": "failed"}
            hs.atomic_write_json(path, failed)
            with self.assertRaises(hs.HylmsError) as raised:
                hs.commit_state(path, state, result, term_directory=term)
            self.assertEqual("phase2_decision_stale", raised.exception.code)

            hs.atomic_write_json(path, state)
            hs.atomic_write_json(
                second_run / "course__c101.json", course_payload(announcement="changed again")
            )
            with self.assertRaises(hs.HylmsError) as raised:
                hs.commit_state(path, state, result, term_directory=term)
            self.assertEqual("phase2_decision_stale", raised.exception.code)
            self.assertEqual(first, json.loads(path.read_text(encoding="utf-8"))["last_processed_run_id"])

    def test_deleted_source_preserves_links_and_adds_one_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            before_payload = course_payload()
            after_payload = course_payload()
            after_payload["announcements"] = []
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), before_payload)
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), after_payload)
            path = root / "state.json"
            state = phase2_state(first)
            state["natural_events"] = [event("linked-event")]
            state["announcement_applications"] = [announcement_application("linked-application")]
            hs.atomic_write_json(path, state)
            packet = hs.prepare_decision_packet(term, state)

            committed = commit_with_pass(path, state, decision_result(packet), term)

            self.assertEqual([event("linked-event")], committed["state"]["natural_events"])
            self.assertEqual(
                [announcement_application("linked-application")],
                committed["state"]["announcement_applications"],
            )
            self.assertEqual(1, len(committed["state"]["pending"]))
            auto_pending = committed["state"]["pending"][0]
            self.assertEqual("source_removed", auto_pending["context"]["kind"])
            self.assertEqual(["linked-event"], auto_pending["context"]["linked_event_ids"])
            self.assertEqual(1, committed["receipt"]["operation_counts"]["auto_pending"])

    def test_deleted_change_cannot_cancel_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            before_payload = course_payload()
            after_payload = course_payload()
            after_payload["announcements"] = []
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), before_payload)
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), after_payload)
            path = root / "state.json"
            state = phase2_state(first)
            state["natural_events"] = [event("linked-event")]
            hs.atomic_write_json(path, state)
            before = path.read_bytes()
            packet = hs.prepare_decision_packet(term, state)
            change_id = next(change["id"] for change in packet["changes"] if change["type"] == "deleted")
            result = decision_result(packet, {change_id: [{
                "op": "cancel_event",
                "id": "linked-event",
                "source_record_ids": ["announcement:1"],
                "evidence": ["source deleted"],
            }]})

            with self.assertRaises(hs.HylmsError) as raised:
                hs.commit_state(path, state, result, term_directory=term)

            self.assertEqual("phase2_decision_conflict", raised.exception.code)
            self.assertEqual(before, path.read_bytes())

    def test_sensitive_decision_and_atomic_write_failure_preserve_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(announcement="changed"),
            )
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            before = path.read_bytes()
            packet = hs.prepare_decision_packet(term, state)
            sensitive = decision_result(packet)
            sensitive["decisions"][0]["reason"] = "https://forms.gle/secret"

            with self.assertRaises(hs.HylmsError) as raised:
                hs.commit_state(path, state, sensitive, term_directory=term)
            self.assertEqual("phase2_decision_invalid", raised.exception.code)
            self.assertEqual(before, path.read_bytes())

            with mock.patch("hylms.diff.atomic_write_json", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    hs.commit_state(
                        path, state, decision_result(packet), term_directory=term
                    )
            self.assertEqual(before, path.read_bytes())


class PhaseTwoStateSchemaTest(unittest.TestCase):
    def test_v3_migration_preserves_extras_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "20260901T000000+0900"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            legacy = {
                "schema_version": 3,
                "phase": 2,
                "term": "26-2",
                "last_processed_run_id": run_id,
                "natural_events": [{
                    "id": "event",
                    "status": "confirmed",
                    "course_id": "101",
                    "course": "자료구조",
                    "title": "대체수업",
                    "kind": "zoom_replacement",
                    "timing_mode": "session",
                    "all_day": False,
                    "start": "2026-09-01T10:00:00+09:00",
                    "end": "2026-09-01T11:00:00+09:00",
                    "attendance_required": False,
                    "source_record_ids": ["announcement:1"],
                    "evidence": "원문",
                    "attendance_note": "선택 출석",
                }],
                "pending": [{
                    "id": "pending",
                    "status": "pending",
                    "course_id": "101",
                    "course": "자료구조",
                    "title": "미정 일정",
                    "kind": "ambiguous",
                    "source_record_ids": ["announcement:1"],
                    "reason": "시간 미정",
                    "question": "언제인가",
                }],
                "rules": [],
                "announcement_applications": [{
                    "application_id": "application",
                    "course_id": "101",
                    "course": "자료구조",
                    "source_record_id": "announcement:1",
                    "target_record_ids": ["weekly_learning:10"],
                    "values": {
                        "session": "2026-09-01T10:00:00+09:00~2026-09-01T11:00:00+09:00",
                        "mode": "Zoom",
                        "attendance_required": False,
                    },
                    "note": "공지 근거",
                }],
                "last_failure": None,
            }
            hs.atomic_write_json(path, legacy)

            migrated = hs.migrate_state_v3_to_v4(path, term)
            before = path.read_bytes(), path.stat().st_mtime_ns
            second = hs.migrate_state_v3_to_v4(path, term)

            self.assertEqual(4, migrated["schema_version"])
            self.assertEqual("class_replacement", migrated["natural_events"][0]["kind"])
            self.assertEqual("선택 출석", migrated["natural_events"][0]["details"]["attendance_note"])
            self.assertEqual("ambiguous", migrated["pending"][0]["context"]["kind"])
            self.assertEqual("Zoom", migrated["announcement_applications"][0]["patch"]["delivery_mode"])
            self.assertEqual(["공지 근거"], migrated["announcement_applications"][0]["evidence"])
            self.assertEqual(migrated, second)
            self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_all_timing_shapes_are_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "20260901T000000+0900"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            state = phase2_state(run_id)
            state["natural_events"] = [
                event("session"),
                event("timed-deadline", kind="submission", mode="deadline"),
                event("all-day-deadline", kind="action", mode="deadline", all_day=True, start=None, end="2026-09-02"),
                event("all-day-period", kind="activity", mode="period", all_day=True, start="2026-09-01", end="2026-09-03"),
                event("window", kind="application", mode="action_window", all_day=True, start="2026-09-01", end="2026-09-07"),
            ]

            hs.validate_state_references(state, term)

    def test_invalid_enum_time_ids_and_references_are_rejected(self):
        base = phase2_state("run")
        base["natural_events"] = [event("event")]
        cases = []
        wrong_kind = copy.deepcopy(base)
        wrong_kind["natural_events"][0]["kind"] = "unknown"
        cases.append(wrong_kind)
        naive = copy.deepcopy(base)
        naive["natural_events"][0]["timing"]["end"] = "2026-09-01T11:00:00"
        cases.append(naive)
        mixed = copy.deepcopy(base)
        mixed["natural_events"][0]["timing"].update(all_day=True, start="2026-09-01", end="2026-09-01", end_inclusive=False)
        cases.append(mixed)
        backwards = copy.deepcopy(base)
        backwards["natural_events"][0]["timing"]["end"] = "2026-09-01T09:00:00+09:00"
        cases.append(backwards)
        missing_end = copy.deepcopy(base)
        missing_end["natural_events"][0]["timing"]["end"] = None
        cases.append(missing_end)
        duplicate = copy.deepcopy(base)
        duplicate["rules"] = [{"id": "event", "source": "user", "text": "rule"}]
        cases.append(duplicate)
        empty_source = copy.deepcopy(base)
        empty_source["natural_events"][0]["source_record_ids"] = []
        cases.append(empty_source)
        sensitive = copy.deepcopy(base)
        sensitive["natural_events"][0]["details"] = {"url": "https://forms.gle/secret"}
        cases.append(sensitive)
        unknown_key = copy.deepcopy(base)
        unknown_key["extra"] = True
        cases.append(unknown_key)

        for state in cases:
            with self.subTest(state=state):
                with self.assertRaises(hs.HylmsError):
                    hs.validate_state(state)

        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), course_payload(extra=True))
            future = phase2_state(first)
            application = announcement_application()
            application["target_record_ids"] = ["assignment:30"]
            future["announcement_applications"] = [application]
            with self.assertRaises(hs.HylmsError):
                hs.validate_state_references(future, term)

    def test_replayed_commit_keeps_bytes_and_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            prepared = hs.prepare_decision_packet(term, state)
            result = decision_result(prepared)

            first_result = hs.commit_state(path, state, result, term_directory=term)
            before = path.read_bytes(), path.stat().st_mtime_ns
            replay = hs.commit_state(path, state, result, term_directory=term)

            self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
            self.assertFalse(first_result["receipt"]["replayed"])
            self.assertTrue(replay["receipt"]["replayed"])

    def test_historical_source_reference_survives_deletion_but_cross_course_target_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            first = "20260901T000000+0900"
            second = "20260902T000000+0900"
            before = course_payload()
            after = course_payload()
            after["announcements"] = []
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), before)
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), after)
            state = phase2_state(second)
            state["natural_events"] = [event("historical")]

            hs.validate_state_references(state, term)

            invalid = copy.deepcopy(state)
            application = announcement_application()
            application["course_id"] = "999"
            invalid["announcement_applications"] = [application]
            with self.assertRaises(hs.HylmsError):
                hs.validate_state_references(invalid, term)

    def test_invalid_migration_and_failure_write_preserve_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "20260901T000000+0900"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            legacy = {"schema_version": 3, "phase": 2, "term": "26-2", "last_processed_run_id": run_id, "natural_events": [{"kind": "bad"}], "pending": [], "rules": [], "announcement_applications": [], "last_failure": None}
            hs.atomic_write_json(path, legacy)
            before = path.read_bytes()
            with self.assertRaises(hs.HylmsError):
                hs.migrate_state_v3_to_v4(path, term)
            self.assertEqual(before, path.read_bytes())

            state = phase2_state(run_id)
            hs.atomic_write_json(path, state)
            failed = hs.record_failure(
                path, state, run_id, "interpret", "model_failed", term_directory=term
            )
            self.assertEqual(run_id, failed["last_processed_run_id"])
            self.assertEqual("interpret", failed["last_failure"]["stage"])

            with self.assertRaises(hs.HylmsError) as raised:
                hs.record_failure(
                    path, state, run_id, "commit", "stale", term_directory=term
                )
            self.assertEqual("phase2_decision_stale", raised.exception.code)
            self.assertEqual(failed, json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
