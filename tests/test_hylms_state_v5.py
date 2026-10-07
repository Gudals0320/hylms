from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hylms_snapshot as hs

from tests.test_hylms_diff import (
    announcement_application,
    course_payload,
    decision_result,
    event,
    pending,
    phase2_state,
    qa_verdict,
    write_run,
)


V4_AUTHORITIES = {
    "611389:913014:replacement-1": "not_applicable",
    "611209:911634:company-briefing": "not_applicable",
    "612893:911546:first-class-date-conflict": "not_applicable",
    "611209:911126:chat-entry-deadline": "user",
    "611652:service-period": "user",
    "611652:report-submission-period": "user",
    "613105:910601:sk-ai-application": "user",
    "611697:912958:team-formation-deadline": "lms",
    "613105:910594:assignment-submission-period": "lms",
}


def v4_event(event_id: str, authority: str) -> dict:
    kind = "class_session" if authority == "not_applicable" else "action"
    value = event(event_id, kind=kind, authority=authority)
    value.pop("action_state_authority")
    value.pop("user_confirmed")
    return value


def v4_state(cursor: str) -> dict:
    state = phase2_state(cursor)
    state["schema_version"] = 4
    state["natural_events"] = [
        v4_event(event_id, authority) for event_id, authority in V4_AUTHORITIES.items()
    ]
    state["pending"] = [pending(f"pending-{index}") for index in range(9)]
    state["rules"] = [
        {"id": f"rule-{index}", "source": "user", "text": f"rule {index}"}
        for index in range(5)
    ]
    state["announcement_applications"] = [
        announcement_application(f"application-{index}") for index in range(3)
    ]
    return state


def make_pass(preview: dict, *, attempt=1, review_id=None):
    packet = hs.prepare_qa_packet(
        preview["qa_context"], attempt=attempt, review_id=review_id
    )
    return packet, qa_verdict(packet)


class QaPacketContractTest(unittest.TestCase):
    def test_packet_hash_and_privacy_boundary_are_enforced(self):
        context = {
            "mode": "automatic",
            "transaction_id": "transaction",
            "candidate_state_sha256": "a" * 64,
            "changes": [{"id": "change"}],
            "structured_changes": [],
            "current_entities": [],
            "candidate_entities": [{"id": "event"}],
            "rules": [],
            "decision_draft": {"safe": True},
            "instruction": None,
        }
        packet = hs.prepare_qa_packet(context)
        verdict = qa_verdict(packet)
        self.assertEqual("pass", hs.validate_qa_verdict(verdict, packet)["verdict"])
        prompt = hs.build_qa_prompt(packet)
        self.assertIn("읽기 전용 Schema QA reviewer", prompt)
        self.assertIn("BEGIN UNTRUSTED QA PACKET", prompt)
        self.assertIn(packet["qa_packet_id"], prompt)

        tampered = copy.deepcopy(packet)
        tampered["candidate_state_sha256"] = "b" * 64
        with self.assertRaises(hs.HylmsError):
            hs.validate_qa_verdict(verdict, tampered)

        unsafe = copy.deepcopy(context)
        unsafe["decision_draft"] = {"body": "private submission"}
        with self.assertRaises(hs.HylmsError):
            hs.prepare_qa_packet(unsafe)


class StateV5MigrationTest(unittest.TestCase):
    def test_v4_to_v5_preserves_baseline_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "20260901T000000+0900"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            source = v4_state(run_id)
            source["natural_events"][3]["action_state"] = "skipped"
            hs.atomic_write_json(path, source)

            migrated = hs.migrate_state_v4_to_v5(path, term)
            before = path.read_bytes(), path.stat().st_mtime_ns
            repeated = hs.migrate_state_v4_to_v5(path, term)

            self.assertEqual(5, migrated["schema_version"])
            self.assertEqual(run_id, migrated["last_processed_run_id"])
            self.assertEqual((9, 9, 5, 3), (
                len(migrated["natural_events"]), len(migrated["pending"]),
                len(migrated["rules"]), len(migrated["announcement_applications"]),
            ))
            self.assertEqual(
                V4_AUTHORITIES,
                {item["id"]: item["action_state_authority"] for item in migrated["natural_events"]},
            )
            for item in migrated["natural_events"]:
                self.assertTrue(item["user_confirmed"])
                self.assertEqual({None}, set(item["user_confirmed"].values()))
            self.assertEqual("unknown", migrated["natural_events"][3]["action_state"])
            self.assertEqual(migrated, repeated)
            self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_unknown_v4_event_and_write_failure_preserve_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "20260901T000000+0900"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            unknown = v4_state(run_id)
            unknown["natural_events"][0]["id"] = "unknown-event"
            hs.atomic_write_json(path, unknown)
            original = path.read_bytes()

            with self.assertRaises(hs.HylmsError):
                hs.migrate_state_v4_to_v5(path, term)
            self.assertEqual(original, path.read_bytes())

            valid = v4_state(run_id)
            hs.atomic_write_json(path, valid)
            original = path.read_bytes()
            with mock.patch("hylms.diff.atomic_write_json", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    hs.migrate_state_v4_to_v5(path, term)
            self.assertEqual(original, path.read_bytes())

    def test_v5_authority_and_confirmation_invariants(self):
        base = phase2_state("run")
        invalid_events = []
        skipped = event("event", kind="action")
        skipped["action_state"] = "skipped"
        invalid_events.append(skipped)
        lms_confirmed = event("event", kind="action", authority="lms")
        lms_confirmed["user_confirmed"] = {"action_state": None}
        invalid_events.append(lms_confirmed)
        class_user = event("event", kind="class_session")
        class_user["action_state_authority"] = "user"
        invalid_events.append(class_user)
        naive_time = event("event", kind="action", confirmed={"timing.end": "2026-09-01T11:00:00"})
        invalid_events.append(naive_time)

        for invalid_event in invalid_events:
            invalid = copy.deepcopy(base)
            invalid["natural_events"] = [invalid_event]
            with self.subTest(invalid_event=invalid_event):
                with self.assertRaises(hs.HylmsError):
                    hs.validate_state(invalid)


class AutomaticQaTransactionTest(unittest.TestCase):
    def test_semantic_mutation_requires_bound_qa_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first, second = "old", "new"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(
                term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST),
                course_payload(announcement="new text"),
            )
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            decision = hs.prepare_decision_packet(term, state)
            change_id = decision["changes"][0]["id"]
            result = decision_result(decision, {
                change_id: [{"op": "upsert_event", "value": event("new-event")}]
            })
            preview = hs.preview_state_transaction(term, state, result)
            before = path.read_bytes()

            self.assertTrue(preview["semantic_mutation"])
            with self.assertRaises(hs.HylmsError) as caught:
                hs.commit_state(path, state, result, term_directory=term)
            self.assertEqual("phase2_qa_required", caught.exception.code)
            self.assertEqual(before, path.read_bytes())

            qa_packet, verdict = make_pass(preview)
            committed = hs.commit_state(
                path, state, result, term_directory=term,
                qa_packet=qa_packet, qa_verdict=verdict,
            )
            self.assertEqual(second, committed["state"]["last_processed_run_id"])
            self.assertEqual("pass", committed["receipt"]["qa"]["verdict"])

    def test_qa_state_machine_and_pending_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first, second = "old", "new"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), course_payload(announcement="ambiguous"))
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            decision = hs.prepare_decision_packet(term, state)
            change_id = decision["changes"][0]["id"]
            result = decision_result(decision, {
                change_id: [{"op": "upsert_event", "value": event("unsafe-event")}]
            })
            preview = hs.preview_state_transaction(term, state, result)
            packet = hs.prepare_qa_packet(preview["qa_context"])
            issue = {
                "code": "ambiguous_time",
                "message": "시각을 하나로 확정할 수 없습니다.",
                "change_ids": [change_id],
                "entity_ids": ["unsafe-event"],
                "field_paths": ["timing.start"],
            }
            revise = qa_verdict(packet, "revise", [issue])
            self.assertEqual("revise", hs.qa_next_action(packet, revise))
            retry = hs.prepare_qa_packet(
                preview["qa_context"], attempt=2, review_id=packet["review_id"]
            )
            retry_revise = qa_verdict(retry, "revise", [{**issue}])
            self.assertEqual("pending", hs.qa_next_action(retry, retry_revise))

            before = path.read_bytes()
            with self.assertRaises(hs.HylmsError) as caught:
                hs.commit_qa_pending_state(path, state, result, retry, retry_revise, term_directory=term)
            self.assertEqual(caught.exception.code, "phase2_qa_review_required")
            self.assertEqual(path.read_bytes(), before)

    def test_failed_or_invalid_qa_never_writes_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first, second = "old", "new"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), course_payload(announcement="changed"))
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            decision = hs.prepare_decision_packet(term, state)
            change_id = decision["changes"][0]["id"]
            result = decision_result(decision, {
                change_id: [{"op": "upsert_event", "value": event("new-event")}]
            })
            preview = hs.preview_state_transaction(term, state, result)
            packet = hs.prepare_qa_packet(preview["qa_context"])
            issue = {
                "code": "review_failed", "message": "검토 결과를 만들 수 없습니다.",
                "change_ids": [change_id], "entity_ids": ["new-event"], "field_paths": [],
            }
            failed = qa_verdict(packet, "failed", [issue])
            before = path.read_bytes()

            with self.assertRaises(hs.HylmsError):
                hs.commit_state(
                    path, state, result, term_directory=term,
                    qa_packet=packet, qa_verdict=failed,
                )
            self.assertEqual(before, path.read_bytes())

            stale = copy.deepcopy(packet)
            stale["candidate_state_sha256"] = "0" * 64
            with self.assertRaises(hs.HylmsError):
                hs.commit_state(
                    path, state, result, term_directory=term,
                    qa_packet=stale, qa_verdict=failed,
                )
            self.assertEqual(before, path.read_bytes())

    def test_cursor_only_commit_skips_qa(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first, second = "old", "new"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), course_payload(due="2026-09-12T23:59:00+09:00"))
            path = root / "state.json"
            state = phase2_state(first)
            hs.atomic_write_json(path, state)
            decision = hs.prepare_decision_packet(term, state)
            result = decision_result(decision)
            preview = hs.preview_state_transaction(term, state, result)

            self.assertFalse(preview["semantic_mutation"])
            committed = hs.commit_state(path, state, result, term_directory=term)
            self.assertEqual("not_required", committed["receipt"]["qa"]["verdict"])

    def test_lms_action_state_uses_structured_provider_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first, second = "old", "new"
            before = course_payload()
            after = course_payload()
            after["weekly_learning"][0]["progress"]["completed"] = True
            after["weekly_learning"][0]["attendance"].update(
                status="present", provider_status="ATTENDANCE"
            )
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), before)
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), after)
            path = root / "state.json"
            state = phase2_state(first)
            linked = event("lms-event", kind="activity", authority="lms")
            linked["source_record_ids"] = ["weekly_learning:10"]
            state["natural_events"] = [linked]
            hs.atomic_write_json(path, state)
            decision = hs.prepare_decision_packet(term, state)
            result = decision_result(decision)
            preview = hs.preview_state_transaction(term, state, result)

            self.assertEqual(["lms-event"], preview["lms_action_state_event_ids"])
            qa_packet, verdict = make_pass(preview)
            committed = hs.commit_state(
                path, state, result, term_directory=term,
                qa_packet=qa_packet, qa_verdict=verdict,
            )
            self.assertEqual("done", committed["state"]["natural_events"][0]["action_state"])

    def test_each_lms_record_kind_can_supply_done_state(self):
        cases = []
        assignment_before = course_payload(extra=True)
        assignment_after = copy.deepcopy(assignment_before)
        assignment_after["assignments"][0]["progress"]["submitted"] = True
        assignment_after["assignments"][0]["submission"]["workflow_state"] = "submitted"
        cases.append(("assignment:30", assignment_before, assignment_after))

        quiz_before = course_payload()
        quiz_before["quizzes"] = [{
            "id": "40", "title": "시험", "description": {"text": "", "links": [], "images": []},
            "source_url": "https://example.test/quizzes/40", "source_kind": "classic_quiz",
            "progress": {"workflow_state": "unsubmitted", "attempt": None},
            "submission": {"workflow_state": "unsubmitted"},
        }]
        quiz_after = copy.deepcopy(quiz_before)
        quiz_after["quizzes"][0]["progress"]["workflow_state"] = "complete"
        quiz_after["quizzes"][0]["submission"]["workflow_state"] = "complete"
        cases.append(("quiz:40", quiz_before, quiz_after))

        discussion_before = course_payload(
            discussion_graded=True, discussion_workflow="unsubmitted", own_entry_count=0
        )
        discussion_after = course_payload(
            discussion_graded=True, discussion_workflow="submitted", own_entry_count=1
        )
        cases.append(("discussion:20", discussion_before, discussion_after))

        for source_record_id, before, after in cases:
            with self.subTest(source_record_id=source_record_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                term = root / "26-2"
                first, second = "old", "new"
                write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), before)
                write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), after)
                state = phase2_state(first)
                linked = event("lms-event", kind="action", authority="lms")
                linked["source_record_ids"] = [source_record_id]
                state["natural_events"] = [linked]
                decision = hs.prepare_decision_packet(term, state)
                preview = hs.preview_state_transaction(
                    term, state, decision_result(decision)
                )

                self.assertEqual(
                    "done", preview["candidate_state"]["natural_events"][0]["action_state"]
                )
                self.assertEqual(["lms-event"], preview["lms_action_state_event_ids"])

    def test_user_confirmed_conflict_keeps_value_and_aggregates_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            first, second, third = "old", "new", "newer"
            write_run(term, first, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            write_run(term, second, dt.datetime(2026, 9, 2, tzinfo=hs.KST), course_payload(announcement="changed"))
            write_run(
                term, third, dt.datetime(2026, 9, 3, tzinfo=hs.KST),
                course_payload(announcement="changed again"),
            )
            path = root / "state.json"
            state = phase2_state(first)
            protected = event("protected", confirmed={"timing.end": None})
            state["natural_events"] = [protected]
            hs.atomic_write_json(path, state)
            decision = hs.prepare_decision_packet(term, state)
            change_id = decision["changes"][0]["id"]
            proposed = copy.deepcopy(protected)
            proposed["timing"]["end"] = "2026-09-01T12:00:00+09:00"
            proposed["evidence"] = ["new evidence"]
            result = decision_result(decision, {
                change_id: [{"op": "upsert_event", "value": proposed}]
            })
            preview = hs.preview_state_transaction(term, state, result)
            qa_packet, verdict = make_pass(preview)
            committed = hs.commit_state(
                path, state, result, term_directory=term,
                qa_packet=qa_packet, qa_verdict=verdict,
            )["state"]

            self.assertEqual(
                "2026-09-01T11:00:00+09:00",
                committed["natural_events"][0]["timing"]["end"],
            )
            self.assertEqual(1, len(committed["pending"]))
            conflict = committed["pending"][0]
            self.assertEqual("protected:conflict:timing.end", conflict["id"])
            self.assertEqual("user_confirmed_conflict", conflict["context"]["kind"])

            next_decision = hs.prepare_decision_packet(term, committed)
            next_change_id = next_decision["changes"][0]["id"]
            repeated = copy.deepcopy(committed["natural_events"][0])
            repeated["timing"]["end"] = "2026-09-01T12:00:00+09:00"
            repeated["evidence"].append("later evidence")
            next_result = decision_result(next_decision, {
                next_change_id: [{"op": "upsert_event", "value": repeated}]
            })
            next_preview = hs.preview_state_transaction(term, committed, next_result)
            next_qa_packet, next_verdict = make_pass(next_preview)
            committed = hs.commit_state(
                path, committed, next_result, term_directory=term,
                qa_packet=next_qa_packet, qa_verdict=next_verdict,
            )["state"]

            self.assertEqual(1, len(committed["pending"]))
            conflict = committed["pending"][0]
            self.assertEqual(1, len(conflict["context"]["candidates"]))
            self.assertEqual(second, conflict["context"]["first_seen_run_id"])
            self.assertEqual(third, conflict["context"]["last_seen_run_id"])


class ManualTransactionTest(unittest.TestCase):
    def test_manual_event_edit_preserves_cursor_and_confirmation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "run"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            state = phase2_state(run_id)
            value = event("user-event", kind="action", authority="user")
            state["natural_events"] = [value]
            hs.atomic_write_json(path, state)
            requested_at = "2026-09-04T21:00:00+09:00"
            packet = hs.prepare_manual_packet(state, "마감을 한 시간 늦춰", ["user-event"], requested_at)
            updated = copy.deepcopy(value)
            updated["timing"]["end"] = "2026-09-01T12:00:00+09:00"
            result = {
                "schema_version": hs.MANUAL_SCHEMA_VERSION,
                "transaction_id": packet["transaction"]["id"],
                "reason": "사용자가 마감을 명시적으로 수정함",
                "operations": [{
                    "op": "upsert_event", "value": updated,
                    "confirmed_fields": ["timing.end"],
                }],
            }
            preview = hs.preview_manual_transaction(term, state, packet, result)
            qa_packet, verdict = make_pass(preview)
            committed = hs.commit_manual_state(
                path, state, packet, result, term_directory=term,
                qa_packet=qa_packet, qa_verdict=verdict,
            )

            changed = committed["state"]["natural_events"][0]
            self.assertEqual(run_id, committed["state"]["last_processed_run_id"])
            self.assertEqual(requested_at, changed["user_confirmed"]["timing.end"])
            self.assertEqual("2026-09-01T12:00:00+09:00", changed["timing"]["end"])

    def test_manual_completion_rejects_lms_authority_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "run"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            state = phase2_state(run_id)
            value = event("lms-event", kind="action", authority="lms")
            state["natural_events"] = [value]
            hs.atomic_write_json(path, state)
            before = path.read_bytes()
            packet = hs.prepare_manual_packet(
                state, "완료했어", ["lms-event"], "2026-09-04T21:00:00+09:00"
            )
            result = {
                "schema_version": hs.MANUAL_SCHEMA_VERSION,
                "transaction_id": packet["transaction"]["id"],
                "reason": "완료 요청",
                "operations": [{
                    "op": "set_user_action_state", "id": "lms-event",
                    "state": "done", "evidence": ["user confirmation"],
                }],
            }

            with self.assertRaises(hs.HylmsError) as caught:
                hs.preview_manual_transaction(term, state, packet, result)
            self.assertEqual("phase2_manual_conflict", caught.exception.code)
            self.assertEqual(before, path.read_bytes())

    def test_user_authority_completion_is_qa_approved_and_timestamped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "run"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            state = phase2_state(run_id)
            state["natural_events"] = [event("user-event", kind="action", authority="user")]
            hs.atomic_write_json(path, state)
            requested_at = "2026-09-04T21:00:00+09:00"
            packet = hs.prepare_manual_packet(state, "완료했어", ["user-event"], requested_at)
            result = {
                "schema_version": hs.MANUAL_SCHEMA_VERSION,
                "transaction_id": packet["transaction"]["id"],
                "reason": "사용자가 외부 행동 완료를 확인함",
                "operations": [{
                    "op": "set_user_action_state", "id": "user-event", "state": "done",
                    "evidence": ["user_confirmed"],
                }],
            }
            preview = hs.preview_manual_transaction(term, state, packet, result)
            qa_packet, verdict = make_pass(preview)
            committed = hs.commit_manual_state(
                path, state, packet, result, term_directory=term,
                qa_packet=qa_packet, qa_verdict=verdict,
            )["state"]

            changed = committed["natural_events"][0]
            self.assertEqual("done", changed["action_state"])
            self.assertEqual(requested_at, changed["user_confirmed"]["action_state"])
            self.assertEqual(run_id, committed["last_processed_run_id"])

    def test_targeted_pending_can_resolve_to_a_new_event_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "run"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            state = phase2_state(run_id)
            state["pending"] = [pending("pending-event")]
            hs.atomic_write_json(path, state)
            requested_at = "2026-09-04T21:00:00+09:00"
            packet = hs.prepare_manual_packet(
                state, "9월 5일 오전 10시 일정으로 확정해", ["pending-event"], requested_at
            )
            new_event = event(
                "resolved-event", kind="action", authority="user",
                start=None, end="2026-09-05T10:00:00+09:00", mode="deadline",
            )
            result = {
                "schema_version": hs.MANUAL_SCHEMA_VERSION,
                "transaction_id": packet["transaction"]["id"],
                "reason": "사용자가 pending 시각을 확정함",
                "operations": [
                    {"op": "upsert_event", "value": new_event, "confirmed_fields": ["timing.end"]},
                    {"op": "resolve_pending", "id": "pending-event"},
                ],
            }
            preview = hs.preview_manual_transaction(term, state, packet, result)
            qa_packet, verdict = make_pass(preview)
            committed = hs.commit_manual_state(
                path, state, packet, result, term_directory=term,
                qa_packet=qa_packet, qa_verdict=verdict,
            )["state"]

            self.assertEqual([], committed["pending"])
            self.assertEqual(["resolved-event"], [item["id"] for item in committed["natural_events"]])
            self.assertTrue(all(
                confirmed_at == requested_at
                for confirmed_at in committed["natural_events"][0]["user_confirmed"].values()
            ))

    def test_manual_and_qa_packets_scrub_sensitive_instruction(self):
        state = phase2_state("run")
        state["natural_events"] = [event("user-event", kind="action", authority="user")]
        packet = hs.prepare_manual_packet(
            state,
            "입장코드 secret https://docs.google.com/forms/d/private",
            ["user-event"],
            "2026-09-04T21:00:00+09:00",
        )
        encoded = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("docs.google.com", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertIn("sensitive-url sha256", encoded)

    def test_manual_pending_verdict_returns_user_input_and_never_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "run"
            write_run(term, run_id, dt.datetime(2026, 9, 1, tzinfo=hs.KST), course_payload())
            path = root / "state.json"
            state = phase2_state(run_id)
            value = event("user-event", kind="action", authority="user")
            state["natural_events"] = [value]
            hs.atomic_write_json(path, state)
            packet = hs.prepare_manual_packet(
                state, "아마 마감이 늦어진 것 같아", ["user-event"],
                "2026-09-04T21:00:00+09:00",
            )
            updated = copy.deepcopy(value)
            updated["timing"]["end"] = "2026-09-01T12:00:00+09:00"
            result = {
                "schema_version": hs.MANUAL_SCHEMA_VERSION,
                "transaction_id": packet["transaction"]["id"],
                "reason": "불명확",
                "operations": [{
                    "op": "upsert_event", "value": updated,
                    "confirmed_fields": ["timing.end"],
                }],
            }
            preview = hs.preview_manual_transaction(term, state, packet, result)
            qa_packet = hs.prepare_qa_packet(preview["qa_context"])
            issue = {
                "code": "ambiguous_user_instruction", "message": "정확한 시각이 필요합니다.",
                "change_ids": [], "entity_ids": ["user-event"],
                "field_paths": ["timing.end"],
            }
            verdict = qa_verdict(qa_packet, "pending", [issue])

            self.assertEqual("needs_user_input", hs.manual_qa_outcome(qa_packet, verdict)["status"])
            before = path.read_bytes()
            with self.assertRaises(hs.HylmsError):
                hs.commit_manual_state(
                    path, state, packet, result, term_directory=term,
                    qa_packet=qa_packet, qa_verdict=verdict,
                )
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
