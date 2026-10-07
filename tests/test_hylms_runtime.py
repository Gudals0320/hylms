from __future__ import annotations

import copy
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hylms.core import HylmsError
from hylms.google_calendar import initialize_calendar, sync_lock
from hylms.runtime import FileExchange, RuntimeService, alive, session_id, self_check, worker, read_json
from hylms.storage import atomic_write_json
from hylms.workflow_adapters import EngineAdapters, collect_snapshot, read_state
from tests.test_hylms_diff import course_payload, decision_result, event, pending, phase2_state, qa_verdict
from tests.test_hylms_google_calendar import Backend
from tests.test_hylms_ntfy import NOW, write_ntfy_run


class NtfyTransport:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def request(self, *args):
        self.calls.append(args)
        return (503 if self.fail else 200), {}, b'{}'


def response(request, result=None, code=None):
    return {key: request[key] for key in ("schema_version", "session_id", "operation_id", "request_id", "binding")} | {
        "result": result, "error": code}


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.term = self.root / "snapshots" / "26-2"
        self.path = self.root / "phase2_state.json"
        self.data = course_payload()
        self.data["weekly_learning"][0]["kind"] = "video"
        self.index = 0
        write_ntfy_run(self.term, "r0", NOW - dt.timedelta(hours=1), self.data)
        atomic_write_json(self.path, phase2_state("r0"))
        self.backend = Backend()
        initialize_calendar(self.backend, self.root / "google_calendar_state.json")
        self.transport = NtfyTransport()
        self.model_calls = []
        for name in ("socket.socket", "webbrowser.open", "hylms.credentials.WindowsCredentialStore._read_bytes",
                     "hylms.credentials.WindowsCredentialStore._write_bytes"):
            patch = mock.patch(name, side_effect=AssertionError("live I/O forbidden"))
            self.addCleanup(patch.stop)
            blocked = patch.start()
            self.addCleanup(blocked.assert_not_called)

    def collector(self, root):
        self.index += 1
        write_ntfy_run(self.term, f"r{self.index}", NOW + dt.timedelta(seconds=self.index), self.data)
        return 0

    def model(self, kind, payload, attempt):
        self.model_calls.append((kind, copy.deepcopy(payload), attempt))
        if kind == "qa":
            return qa_verdict(payload["packet"])
        return decision_result(payload["packet"])

    def engine(self, **kwargs):
        return EngineAdapters(self.root, kwargs.pop("exchange", self.model), clock=lambda: NOW,
            collector=kwargs.pop("collector", self.collector), ntfy_transport=self.transport,
            google_client=self.backend, **kwargs)

    def test_no_text_change_advances_cursor_and_reconciles_real_engines(self):
        first = self.engine().run("one")
        ids = set(self.backend.events)
        second = self.engine().run("two")
        self.assertEqual(first["pairs_committed"], 1)
        self.assertEqual(read_state(self.path)["last_processed_run_id"], "r2")
        self.assertEqual(self.model_calls, [])
        self.assertEqual(set(self.backend.events), ids)
        self.assertEqual(second["summary"]["external"]["google"]["counts"]["created"], 0)
        self.assertTrue((self.term / "HY-LMS.ics").exists())
        self.assertEqual([line for line in second["report"].splitlines() if line.startswith("##")],
                         ["## 실행 상태", "## LMS 변경", "## 외부 반영", "## 확인할 내용"])
        self.assertEqual(len(self.transport.calls), 2)

    def change_model(self, kind, payload, attempt):
        if kind == "qa":
            return qa_verdict(payload["packet"])
        packet = payload["packet"]
        return decision_result(packet, {packet["changes"][0]["id"]: [{"op": "upsert_event", "value": event("new-event")}]})

    def test_changed_professor_text_uses_actual_preview_qa_commit(self):
        self.data["announcements"][0]["message"]["text"] = "수업이 9월 1일 10시로 변경됨"
        result = self.engine(exchange=self.change_model).run("semantic")
        self.assertEqual(result["pairs_committed"], 1)
        self.assertEqual(read_state(self.path)["natural_events"][0]["id"], "new-event")
        self.assertTrue(any(step.get("qa_action") == "commit" for step in result["steps"]))
        self.assertEqual(result["summary"]["text_changes"]["modified"], 1)

    def test_model_failure_leaves_state_bytes_and_continues_outputs(self):
        before = self.path.read_bytes()
        self.data["announcements"][0]["message"]["text"] = "new"
        def fail(*args):
            raise HylmsError("runtime_model_timeout", "SECRET")
        result = self.engine(exchange=fail).run("failed")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(result["status"], "partial_failure")
        self.assertTrue(self.transport.calls)
        self.assertTrue(self.backend.events)

    def test_qa_pending_is_internal_failure_with_state_preserved(self):
        self.data["announcements"][0]["message"]["text"] = "new"
        def model(kind, payload, attempt):
            if kind == "qa":
                packet = payload["packet"]
                return qa_verdict(packet, "pending", [{"code": "ambiguous", "message": "확인 필요",
                    "change_ids": [packet["changes"][0]["id"]], "entity_ids": [], "field_paths": []}])
            return self.change_model(kind, payload, attempt)
        result = self.engine(exchange=model).run("pending")
        state = read_state(self.path)
        self.assertEqual(state["natural_events"], [])
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["last_processed_run_id"], "r0")
        self.assertEqual(result["status"], "partial_failure")
        self.assertTrue(result["summary"]["technical_reviews"])
        self.assertEqual(result["summary"]["pending_review"]["count"], 0)

    def test_collect_failure_with_archive_fallback_keeps_outputs(self):
        def failed(root):
            raise HylmsError("auth_missing", "PRIVATE")
        result = self.engine(collector=failed).run("fallback")
        self.assertEqual(result["output"]["cursor"], "r0")
        self.assertTrue(result["fallback"])
        self.assertTrue(self.transport.calls)
        self.assertIn("auth rotate", result["report"])

    def test_success_without_new_archive_is_not_reported_as_collection_success(self):
        result = self.engine(collector=lambda root: 0).run("no_archive")
        self.assertEqual(result["steps"][0]["status"], "failed")
        self.assertEqual(result["steps"][0]["codes"], ["snapshot_archive_missing"])

    def test_invalid_state_prevents_all_external_writes(self):
        self.path.write_text('{}', encoding="utf-8")
        result = self.engine().run("invalid")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.backend.events, {})

    def test_ics_failure_does_not_stop_google(self):
        with mock.patch("hylms.workflow_adapters.write_ics", side_effect=OSError("PRIVATE")):
            result = self.engine().run("ics_failure")
        self.assertEqual(next(s for s in result["steps"] if s["stage"] == "google")["status"], "success")
        self.assertTrue(self.backend.events)

    def test_ntfy_failure_does_not_stop_real_ics_google(self):
        self.transport.fail = True
        result = self.engine().run("ntfy_failure")
        self.assertEqual(result["status"], "partial_failure")
        self.assertTrue((self.term / "HY-LMS.ics").exists())
        self.assertTrue(self.backend.events)

    def test_google_auth_failure_reports_command_and_preserves_local_output(self):
        def fail(_):
            raise HylmsError("reauth_required", "PRIVATE")
        self.backend.list_events = fail
        result = self.engine().run("reauth")
        self.assertIn("auth login", result["report"])
        self.assertTrue((self.term / "HY-LMS.ics").exists())
        self.assertEqual(self.backend.events, {})

    def test_manual_completion_changes_only_state_until_next_run(self):
        state = read_state(self.path)
        state["natural_events"] = [event("user-event", kind="action", authority="user")]
        atomic_write_json(self.path, state)
        self.engine().run("initial")
        files = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p != self.path}
        calls = len(self.transport.calls), len(self.backend.calls)
        def model(kind, payload, attempt):
            if kind == "qa":
                return qa_verdict(payload["packet"])
            return {"schema_version": 1, "transaction_id": payload["packet"]["transaction"]["id"],
                "reason": "사용자 완료 확인", "operations": [{"op": "set_user_action_state", "id": "user-event",
                                                         "state": "done", "evidence": ["user_confirmed"]}]}
        engine = self.engine(exchange=model)
        engine.clock = lambda: NOW.replace(microsecond=123456)
        result = engine.manual("완료했어", ["user-event"], "initial")
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["external_changed"])
        self.assertEqual(calls, (len(self.transport.calls), len(self.backend.calls)))
        self.assertTrue(all(p.read_bytes() == data for p, data in files.items()))
        self.assertEqual(read_state(self.path)["last_processed_run_id"], "r1")
        next_result = self.engine().run("next")
        self.assertEqual(next_result["summary"]["external"]["google"]["counts"]["deleted"], 1)

    def test_manual_cannot_override_lms_completion(self):
        state = read_state(self.path)
        state["natural_events"] = [event("lms-event", kind="action", authority="lms")]
        atomic_write_json(self.path, state)
        before = self.path.read_bytes()
        def model(kind, payload, attempt):
            return {"schema_version": 1, "transaction_id": payload["packet"]["transaction"]["id"], "reason": "fixture",
                "operations": [{"op": "set_user_action_state", "id": "lms-event", "state": "done", "evidence": []}]}
        with self.assertRaises(HylmsError):
            self.engine(exchange=model).manual("완료", ["lms-event"], "manual")
        self.assertEqual(self.path.read_bytes(), before)

    def test_collector_missing_credential_never_starts_process(self):
        with mock.patch("hylms.workflow_adapters.WindowsCredentialStore") as store, mock.patch("subprocess.run") as launch:
            store.return_value.read.return_value = None
            with self.assertRaises(HylmsError) as caught:
                collect_snapshot(self.root)
            self.assertEqual(caught.exception.code, "auth_missing")
            launch.assert_not_called()

    def test_collector_closes_stdin_and_uses_absolute_repo(self):
        with mock.patch("hylms.workflow_adapters.WindowsCredentialStore"), mock.patch("subprocess.run") as launch:
            launch.return_value.returncode = 0
            self.assertEqual(collect_snapshot(self.root), 0)
            self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(launch.call_args.kwargs["cwd"], self.root)
        self.assertEqual(launch.call_args.args[0], [sys.executable, str(self.root / "hylms_snapshot.py")])

    def test_multiple_backlogged_runs_are_processed_sequentially(self):
        self.collector(self.root)
        self.collector(self.root)
        result = self.engine().run("backlog")
        self.assertEqual(result["pairs_committed"], 3)
        self.assertEqual(result["output"]["cursor"], "r3")

    def test_prepare_failure_does_not_report_zero_changes(self):
        with mock.patch("hylms.workflow_adapters.diff.prepare_decision_packet", side_effect=HylmsError("phase2_decision_invalid", "fixture")):
            result = self.engine().run("prepare_failed")
        self.assertFalse(result["summary"]["text_changes_complete"])
        self.assertIn("변경 수 미집계", result["report"])
        self.assertNotIn("텍스트 추가 0", result["report"])
        self.assertEqual(result["pairs_committed"], 0)

    def test_partial_collection_keeps_previous_course_records(self):
        def collect(root):
            write_ntfy_run(self.term, "r1", NOW, self.data, course_status="kept")
            return 2
        result = self.engine(collector=collect).run("partial")
        self.assertTrue(result["fallback"])
        self.assertEqual(len(self.backend.events), 1)

    def test_no_courses_clears_structured_but_retains_natural(self):
        state = read_state(self.path)
        state["natural_events"] = [event("retained")]
        atomic_write_json(self.path, state)
        def collect(root):
            status = {"schema_version": 5, "term": {"id": "26-2"}, "courses": [],
                      "started_at": NOW.isoformat(), "ended_at": NOW.isoformat(),
                      "overall_status": "no_courses", "exit_code": 0}
            atomic_write_json(self.term / "runs" / "empty" / "status.json", status)
            atomic_write_json(self.term / "status.json", status)
            return 0
        result = self.engine(collector=collect).run("empty")
        self.assertEqual(len(read_state(self.path)["natural_events"]), 1)
        self.assertEqual(len(self.backend.events), 1)
        self.assertEqual(result["output"]["cursor"], "empty")

    def test_source_deletion_keeps_confirmed_event(self):
        state = read_state(self.path)
        state["natural_events"] = [event("retained")]
        atomic_write_json(self.path, state)
        self.data["announcements"] = []
        result = self.engine().run("removed")
        self.assertEqual(read_state(self.path)["natural_events"][0]["status"], "active")
        self.assertTrue(result["summary"]["technical_reviews"])
        self.assertEqual(result["summary"]["pending_review"]["count"], 0)

    def test_snapshot_change_during_model_wait_blocks_commit_and_outputs(self):
        self.data["announcements"][0]["message"]["text"] = "new"
        def changed(kind, payload, attempt):
            path = self.term / "runs" / "r1" / "course__c101.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["announcements"][0]["message"]["text"] = "changed while waiting"
            atomic_write_json(path, value)
            return decision_result(payload["packet"])
        result = self.engine(exchange=changed).run("input_changed")
        self.assertEqual(read_state(self.path)["last_processed_run_id"], "r0")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.backend.events, {})
        self.assertIn("runtime_input_changed", result["report"])

    def test_model_revision_uses_second_candidate(self):
        self.data["announcements"][0]["message"]["text"] = "new"
        attempts = []
        def model(kind, payload, attempt):
            attempts.append((kind, attempt))
            if kind == "qa" and attempt == 1:
                packet = payload["packet"]
                return qa_verdict(packet, "revise", [{"code": "revise_title", "message": "다시 확인",
                    "change_ids": [packet["changes"][0]["id"]], "entity_ids": [], "field_paths": []}])
            return self.change_model(kind, payload, attempt)
        result = self.engine(exchange=model).run("revision")
        self.assertEqual(result["pairs_committed"], 1)
        self.assertEqual(attempts, [("interpret", 1), ("qa", 1), ("interpret", 2), ("qa", 2)])

    def test_google_partial_insert_failure_keeps_successful_event(self):
        state = read_state(self.path)
        state["natural_events"] = [event("another")]
        atomic_write_json(self.path, state)
        seen = []
        def fail_first(event_id):
            seen.append(event_id)
            if len(seen) == 1:
                raise HylmsError("google_http_403", "PRIVATE")
        self.backend.hooks["insert"] = fail_first
        result = self.engine().run("partial_google")
        self.assertEqual(len(self.backend.events), 1)
        step = next(item for item in result["steps"] if item["stage"] == "google")
        self.assertEqual(step["status"], "partial_failure")
        self.assertEqual(step["counts"]["failed"], 1)

    def test_old_pending_nine_are_listed_and_start_discussion_after_outputs(self):
        state = read_state(self.path)
        state["pending"] = [pending(f"pending-{i}") for i in range(9)]
        for i, item in enumerate(state["pending"]):
            item["title"] = f"미확정 항목 {i + 1}"
            item["context"]["question"] = f"항목 {i + 1}의 날짜를 확인해 주실래요?"
        atomic_write_json(self.path, state)
        result = self.engine().run("old_pending")
        self.assertEqual(result["new_pending"], 0)
        self.assertEqual(result["summary"]["pending_review"]["count"], 9)
        for item in state["pending"]:
            self.assertIn(item["title"], result["report"])
        self.assertIn("B1.", result["report"])
        self.assertIn("항목 1의 날짜", result["report"])
        self.assertNotIn("확인할 내용 없음", result["report"])
        self.assertEqual([call["id"] for call in read_state(self.path)["pending"]], [p["id"] for p in state["pending"]])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(self.backend.events)
        self.assertEqual(self.model_calls, [])  # A question alone is not a mutation/QA.

    def test_pending_answer_is_reviewed_and_committed_without_resending(self):
        state = read_state(self.path)
        state["pending"] = [pending("answer-this"), pending("later")]
        atomic_write_json(self.path, state)
        self.engine().run("initial")
        original = read_state(self.path)
        files = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p != self.path}
        counts = len(self.transport.calls), len(self.backend.calls)
        phases = []
        def model(kind, payload, attempt):
            phases.append(kind)
            if kind == "qa":
                self.assertEqual(read_state(self.path)["pending"], original["pending"])
                return qa_verdict(payload["packet"])
            resolved = event("confirmed-deadline", kind="action", mode="deadline", authority="user",
                             start=None, end="2026-09-10T18:00:00+09:00")
            return {"schema_version": 1, "transaction_id": payload["packet"]["transaction"]["id"],
                "reason": "사용자가 마감을 확인함", "operations": [
                    {"op": "upsert_event", "value": resolved, "confirmed_fields": ["timing.end"]},
                    {"op": "resolve_pending", "id": "answer-this"}]}
        result = self.engine(exchange=model).manual("9월 10일 18시 마감이야", ["answer-this"], "initial")
        self.assertEqual(phases, ["manual", "qa"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["pending_review"]["first_target_id"], "later")
        current = read_state(self.path)
        self.assertEqual(current["natural_events"][0]["timing"]["end"], "2026-09-10T18:00:00+09:00")
        self.assertIn("timing.end", current["natural_events"][0]["user_confirmed"])
        self.assertEqual(current["last_processed_run_id"], original["last_processed_run_id"])
        self.assertEqual(current["last_failure"], original["last_failure"])
        self.assertEqual(counts, (len(self.transport.calls), len(self.backend.calls)))
        self.assertTrue(all(p.read_bytes() == content for p, content in files.items()))

    def test_pending_qa_clarification_preserves_unresolved_state(self):
        state = read_state(self.path)
        state["pending"] = [pending("unclear")]
        atomic_write_json(self.path, state)
        original = self.path.read_bytes()
        def model(kind, payload, attempt):
            if kind == "qa":
                return qa_verdict(payload["packet"], "pending", [{"code": "needs_date", "message": "날짜 확인 필요",
                    "change_ids": [], "entity_ids": ["unclear"], "field_paths": []}])
            return {"schema_version": 1, "transaction_id": payload["packet"]["transaction"]["id"],
                "reason": "확인이 불충분함", "operations": [{"op": "resolve_pending", "id": "unclear"}]}
        result = self.engine(exchange=model).manual("처리해줘", ["unclear"], "manual")
        self.assertEqual(result["status"], "needs_user_input")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(result["pending_review"]["count"], 1)
        self.assertEqual(self.transport.calls, [])

    def test_empty_pending_has_no_conversation_opener(self):
        result = self.engine().run("none")
        self.assertIsNone(result["summary"]["pending_review"]["opening_question"])
        self.assertIn("확인할 내용 없음", result["report"])
        self.assertNotIn("먼저 1번", result["report"])

    def grouped_pending_run(self, session="grouped"):
        state = read_state(self.path)
        state["pending"] = [pending(f"old-{i}") for i in range(7)]
        atomic_write_json(self.path, state)
        self.data["announcements"][0]["message"]["text"] = "new pending questions"
        def model(kind, payload, attempt):
            if kind == "qa":
                return qa_verdict(payload["packet"])
            packet = payload["packet"]
            return decision_result(packet, {packet["changes"][0]["id"]: [
                {"op": "upsert_pending", "value": pending("new-z")},
                {"op": "upsert_pending", "value": pending("new-a")}]})
        service = RuntimeService(self.root, launcher=lambda *args: os.getpid(), execution_check=lambda: None)
        engine = self.engine(exchange=model)
        engine.agenda_writer = lambda agenda: service.save_agenda(session, agenda)
        result = engine.run(session)
        atomic_write_json(service.folder(session) / "session.json", {"status": "completed", "pipeline_completed": True})
        atomic_write_json(service.folder(session) / "result.json", result)
        return service, result

    def test_two_new_seven_existing_share_labels_and_ntfy_counts(self):
        service, result = self.grouped_pending_run()
        review = result["summary"]["pending_review"]
        self.assertEqual(review["counts"], {"new": 2, "existing": 7})
        self.assertEqual([(x["label"], x["id"]) for x in review["items"][:2]], [("A1", "new-a"), ("A2", "new-z")])
        self.assertEqual([x["label"] for x in review["items"][2:]], [f"B{i}" for i in range(1, 8)])
        self.assertEqual(service.pending("grouped"), review)
        self.assertEqual(result["new_pending"], 2)
        for item in review["items"]:
            self.assertIn(f"{item['label']}.", result["report"])
            self.assertIn(item["question"], result["report"])
        messages = [json.loads(call[3])["message"] for call in self.transport.calls]
        self.assertIn("확인대기 2+(7)", messages[-1])
        self.assertEqual(sum(message.count("[새 확인 대기]") for message in messages), 2)

    def test_manual_overwritten_result_keeps_labels_and_does_not_send_again(self):
        service, result = self.grouped_pending_run()
        calls = len(self.transport.calls), len(self.backend.calls)
        agenda_before = service.read_agenda("grouped")
        def model(kind, payload, attempt):
            if kind == "qa":
                self.assertIn('"A1": "new-a"', payload["prompt"])
                return qa_verdict(payload["packet"])
            self.assertEqual(payload["target_labels"], {"A1": "new-a", "B2": "old-1"})
            return {"schema_version": 1, "transaction_id": payload["packet"]["transaction"]["id"],
                "reason": "사용자가 두 항목을 해결함", "operations": [
                    {"op": "resolve_pending", "id": "new-a"}, {"op": "resolve_pending", "id": "old-1"}]}
        engine = self.engine(exchange=model)
        engine.agenda = service.read_agenda("grouped")
        engine.agenda_writer = lambda agenda: service.save_agenda("grouped", agenda)
        manual = engine.manual("A1과 B2는 확인 완료", ["new-a", "old-1"], "grouped")
        atomic_write_json(service.folder("grouped") / "result.json", manual)
        review = service.pending("grouped")
        self.assertEqual(review, manual["pending_review"])
        self.assertEqual(review["counts"], {"new": 1, "existing": 6})
        labels = {x["label"]: x["id"] for x in review["items"]}
        self.assertEqual(labels["A2"], "new-z")
        self.assertNotIn("A1", labels)
        self.assertNotIn("B2", labels)
        self.assertEqual(service.read_agenda("grouped"), agenda_before)
        self.assertEqual(calls, (len(self.transport.calls), len(self.backend.calls)))

    def test_next_session_reasks_every_unanswered_item_as_existing(self):
        _, first = self.grouped_pending_run()
        before = read_state(self.path)["pending"]
        second = self.engine().run("next")
        review = second["summary"]["pending_review"]
        self.assertEqual(read_state(self.path)["pending"], before)
        self.assertEqual(review["counts"], {"new": 0, "existing": 9})
        self.assertTrue(all(x["group"] == "B" for x in review["items"]))
        for item in review["items"]:
            self.assertIn(item["question"], second["report"])
        self.assertIn("확인대기 0+(9)", json.loads(self.transport.calls[-1][3])["message"])

    def test_qa_deferral_preserves_a_b_mapping_and_state(self):
        service, initial = self.grouped_pending_run()
        state_before = self.path.read_bytes()
        agenda_before = (service.folder("grouped") / "pending-agenda.json").read_bytes()
        calls_before = len(self.transport.calls), len(self.backend.calls)
        def model(kind, payload, attempt):
            if kind == "qa":
                return qa_verdict(payload["packet"], "pending", [{"code": "needs_confirmation", "message": "확인 필요",
                    "change_ids": [], "entity_ids": ["new-a"], "field_paths": []}])
            return {"schema_version": 1, "transaction_id": payload["packet"]["transaction"]["id"],
                    "reason": "추가 확인 필요", "operations": [{"op": "resolve_pending", "id": "new-a"}]}
        engine = self.engine(exchange=model)
        engine.agenda = service.read_agenda("grouped")
        engine.agenda_writer = lambda agenda: service.save_agenda("grouped", agenda)
        result = engine.manual("A1 확인해줘", ["new-a"], "grouped")
        self.assertEqual(result["status"], "needs_user_input")
        self.assertEqual(result["pending_review"], initial["summary"]["pending_review"])
        self.assertEqual(self.path.read_bytes(), state_before)
        self.assertEqual((service.folder("grouped") / "pending-agenda.json").read_bytes(), agenda_before)
        self.assertEqual((len(self.transport.calls), len(self.backend.calls)), calls_before)

    def test_existing_id_updated_by_lms_stays_b(self):
        state = read_state(self.path)
        state["pending"] = [pending("same-id")]
        atomic_write_json(self.path, state)
        self.data["announcements"][0]["message"]["text"] = "new evidence"
        def model(kind, payload, attempt):
            if kind == "qa":
                return qa_verdict(payload["packet"])
            packet = payload["packet"]
            updated = pending("same-id")
            updated["reason"] = "새 근거가 있지만 아직 날짜를 확인해야 함"
            return decision_result(packet, {packet["changes"][0]["id"]: [{"op": "upsert_pending", "value": updated}]})
        result = self.engine(exchange=model).run("same-id")
        self.assertEqual(result["summary"]["pending_review"]["counts"], {"new": 0, "existing": 1})
        self.assertEqual(result["summary"]["pending_review"]["items"][0]["label"], "B1")

    def test_failed_commit_does_not_create_a_or_new_ntfy_count(self):
        self.data["announcements"][0]["message"]["text"] = "new"
        def model(kind, payload, attempt):
            if kind == "qa":
                return qa_verdict(payload["packet"])
            p = payload["packet"]
            return decision_result(p, {p["changes"][0]["id"]: [{"op": "upsert_pending", "value": pending("unsaved")}]})
        with mock.patch("hylms.workflow_adapters.diff.commit_state", side_effect=HylmsError("state_write_failed", "fixture")):
            result = self.engine(exchange=model).run("failed")
        self.assertEqual(result["summary"]["pending_review"]["count"], 0)
        self.assertIn("확인대기 0+(0)", json.loads(self.transport.calls[-1][3])["message"])


class ExchangeOnlyEngine:
    """Subprocess fixture; no collector, credentials, or external adapters."""
    def __init__(self, root, exchange, **kwargs):
        self.exchange = exchange

    def run(self, session):
        value = self.exchange("manual", {"fixture": "no-private-data"}, 1)
        return {"status": "success", "echo": value}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = RuntimeService(self.root, launcher=lambda *args: os.getpid(), execution_check=lambda: None)
        self.folder = self.service.folder("session")

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if hasattr(self, "process") and self.process.poll() is not None:
                message = self.process.stderr.read().decode(errors="replace")
                self.process.stderr.close()
                self.fail("fixture worker exited: " + message)
            time.sleep(.01)
        self.fail("fixture timed out")

    def test_session_identity_and_no_fabricated_fallback(self):
        self.assertEqual(session_id({"CODEX_THREAD_ID": "task", "CODEX_SESSION_ID": "legacy"}), "task")
        self.assertEqual(session_id({"CODEX_SESSION_ID": "legacy"}), "legacy")
        for env in ({}, {"CODEX_THREAD_ID": "../bad"}):
            with self.assertRaises(HylmsError):
                session_id(env)

    def test_no_token_and_similar_tokens_have_zero_files_or_launches(self):
        self.service.launcher = mock.Mock()
        for prompt in ("LMS 일정", "hylms.py", "$HYLMS", "$hylms-other", "$hylmss", "abc$hylms", "$$hylms"):
            self.assertEqual(self.service.start("session", prompt)["status"], "not_invoked")
        self.service.launcher.assert_not_called()
        self.assertFalse(self.service.directory.exists())

    def test_duplicate_start_returns_existing_without_launch(self):
        self.service.launcher = mock.Mock(return_value=os.getpid())
        first = self.service.start("session", "$hylms")
        second = self.service.start("session", "$hylms unknown suffix")
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.service.launcher.assert_called_once()

    def test_other_session_is_busy(self):
        self.service.start("session", "$hylms")
        with self.assertRaises(HylmsError) as caught:
            self.service.start("another", "$hylms")
        self.assertEqual(caught.exception.code, "busy")
        self.assertFalse(self.service.folder("another").exists())

    def test_scheduled_cycles_preserve_history_and_deduplicate_across_threads(self):
        first = self.service.start('session', '$hylms', run_key='20261001T1000')
        self.assertEqual(first['operation_id'], self.service.start(
            'session', '$hylms', run_key='20261001T1900')['operation_id'])
        first.update(status='completed', pipeline_completed=True)
        atomic_write_json(self.folder / 'session.json', first)
        atomic_write_json(self.folder / 'result.json', {'status': 'success'})
        atomic_write_json(self.folder / 'pending-agenda.json', {'saved': True})
        (self.service.directory / 'active.json').unlink()
        second = self.service.start('session', '$hylms', run_key='20261001T1900')
        self.assertNotEqual(first['operation_id'], second['operation_id'])
        self.assertFalse(second['pipeline_completed'])
        self.assertFalse((self.folder / 'pending-agenda.json').exists())
        self.assertFalse((self.folder / 'result.json').exists())
        history = read_json(self.folder / 'history' / '20261001T1000.json')
        self.assertEqual(history['status']['result'], {'status': 'success'})
        self.assertEqual(history['agenda'], {'saved': True})
        duplicate = self.service.start('replacement', '$hylms', run_key='20261001T1000')
        self.assertEqual(duplicate['operation_id'], first['operation_id'])
        self.assertFalse(self.service.folder('replacement').exists())

    def test_scheduled_invalid_keys_and_archive_failure_do_not_start(self):
        self.service.launcher = mock.Mock(return_value=os.getpid())
        for key in ('../escape', '20260230T1000', '20261001T2500'):
            with self.assertRaises(HylmsError):
                self.service.start('session', '$hylms', run_key=key)
        self.service.launcher.assert_not_called()
        first = self.service.start('session', '$hylms', run_key='20261001T1000')
        first.update(status='completed')
        atomic_write_json(self.folder / 'session.json', first)
        (self.service.directory / 'active.json').unlink()
        with mock.patch('hylms.runtime.atomic_write_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.service.start('session', '$hylms', run_key='20261001T1900')
        self.assertEqual(self.service.status('session')['operation_id'], first['operation_id'])
        self.assertEqual(self.service.launcher.call_count, 1)

    def test_interrupted_scheduled_occurrence_is_not_replayed(self):
        first = self.service.start('session', '$hylms', run_key='20261001T1000')
        self.service.is_alive = lambda pid: False
        repeat = self.service.start('replacement', '$hylms', run_key='20261001T1000')
        self.assertEqual(repeat['status'], 'interrupted')
        self.assertEqual(repeat['operation_id'], first['operation_id'])
        second = self.service.start('session', '$hylms', run_key='20261001T1900')
        self.assertNotEqual(second['operation_id'], first['operation_id'])

    def test_actual_process_lock_blocks_admission(self):
        with sync_lock(self.service.directory / "pipeline"):
            with self.assertRaises(HylmsError):
                self.service.start("session", "$hylms")

    def test_dead_worker_is_interrupted_and_never_replayed(self):
        self.service.start("session", "$hylms")
        self.service.is_alive = lambda pid: False
        self.service.launcher = mock.Mock()
        self.assertEqual(self.service.start("session", "$hylms")["status"], "interrupted")
        self.service.launcher.assert_not_called()

    def test_launch_failure_is_not_success(self):
        self.service.launcher = mock.Mock(side_effect=OSError("SECRET"))
        with self.assertRaises(HylmsError):
            self.service.start("session", "$hylms")
        self.assertEqual(self.service.status("session")["status"], "interrupted")
        self.assertNotIn("SECRET", (self.folder / "session.json").read_text())

    def test_round_trip_bound_response_and_ephemeral_cleanup(self):
        self.folder.mkdir(parents=True)
        exchange = FileExchange(self.folder, "session", "operation", timeout=2)
        results = []
        thread = threading.Thread(target=lambda: results.append(exchange("manual", {"data": [2, 1]}, 1)))
        thread.start()
        self.addCleanup(thread.join, 3)
        self.wait(lambda: (self.folder / "request.json").exists())
        request = json.loads((self.folder / "request.json").read_text())
        self.assertEqual(request["payload"]["data"], [2, 1])
        self.service.submit("session", response(request, {"decision": "accepted"}))
        thread.join(3)
        self.assertEqual(results, [{"decision": "accepted"}])
        self.assertFalse((self.folder / "request.json").exists())
        self.assertFalse((self.folder / "response.json").exists())

    def interpret_request(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        packet = {"transaction": {"id": "transaction"}, "changes": [{"id": "change"}]}
        request = {"schema_version": 1, "session_id": "session", "operation_id": "operation",
                   "request_id": "request", "binding": "candidate-hash", "kind": "interpret",
                   "payload": {"packet": packet}}
        atomic_write_json(self.folder / "request.json", request)
        return request

    def interpret_result(self, operation):
        return {"schema_version": 2, "transaction_id": "transaction", "decisions": [
            {"change_id": "change", "disposition": "mutate", "reason": "source evidence",
             "operations": [operation]}]}

    def test_invalid_activity_can_be_corrected_without_consuming_request(self):
        request = self.interpret_request()
        candidate = event("mentoring")
        candidate.update(kind="activity", action_state="not_applicable", action_state_authority="not_applicable")
        result = self.interpret_result({"op": "upsert_event", "value": candidate})
        rejected = self.service.submit("session", response(request, result))
        self.assertEqual(rejected["status"], "rejected")
        self.assertFalse((self.folder / "response.json").exists())
        self.assertEqual(read_json(self.folder / "request.json"), request)
        candidate.update(action_state="unknown", action_state_authority="user")
        self.assertEqual(self.service.submit("session", response(request, result))["status"], "submitted")

    def test_real_failure_partial_application_patch_rejected_then_format_repaired(self):
        from hylms.diff import _empty_application_patch
        request = self.interpret_request()
        application = {"id": "application", "course_id": "course", "course_name": "Course",
                       "source_record_ids": ["discussion:923424"],
                       "target_record_ids": ["discussion:923424", "weekly_learning:9631648"],
                       "patch": {"requirements": ["three comments"], "details": {"representative_upload": True}},
                       "evidence": ["published instructions"]}
        result = self.interpret_result({"op": "upsert_announcement_application", "value": application})
        rejected = self.service.submit("session", response(request, result))
        self.assertEqual(rejected["reason"], "application_patch_keys_mismatch")
        self.assertEqual(rejected["field_path"], "decisions[0].operations[0].value.patch")
        self.assertEqual(len(rejected["missing_keys"]), 11)
        self.assertFalse(rejected["worker_response_written"])
        self.assertEqual(read_json(self.folder / "request.json"), request)
        application["patch"] = _empty_application_patch() | application["patch"]
        self.assertEqual(self.service.submit("session", response(request, result))["status"], "submitted")
        self.assertEqual(read_json(self.folder / "response.json")["result"], result)

    def test_interpret_format_repair_exhaustion_is_bound_and_never_pass(self):
        request = self.interpret_request()
        malformed = {"schema_version": 2, "transaction_id": "transaction", "decisions": []}
        self.assertEqual(self.service.submit("session", response(request, malformed))["repair_remaining"], 1)
        failed = self.service.submit("session", response(request, malformed))
        self.assertEqual(failed["code"], "runtime_interpret_format_exhausted")
        stored = read_json(self.folder / "response.json")
        self.assertIsNone(stored["result"])
        self.assertEqual(stored["error"], failed["code"])
        with self.assertRaises(HylmsError):
            self.service.submit("session", response(request, malformed))

    def test_interpret_diagnostic_never_echoes_unknown_patch_keys(self):
        from hylms.diff import decision_format_diagnostic
        value = self.interpret_result({"op": "upsert_announcement_application", "value": {
            "patch": {"secret-provider-body": "sensitive"}}})
        diagnostic = decision_format_diagnostic(value)
        self.assertNotIn("secret-provider-body", json.dumps(diagnostic))
        self.assertNotIn("sensitive", json.dumps(diagnostic))
        self.assertEqual(diagnostic["unexpected_key_count"], 1)

    def test_stale_binding_and_duplicate_submit_rejected(self):
        self.folder.mkdir(parents=True)
        request = {"schema_version": 1, "session_id": "session", "operation_id": "operation",
                   "request_id": "request", "binding": "candidate-hash"}
        atomic_write_json(self.folder / "request.json", request)
        for key in ("session_id", "operation_id", "request_id", "binding"):
            wrong = response(request, {})
            wrong[key] = "stale"
            with self.assertRaises(HylmsError):
                self.service.submit("session", wrong)
        self.service.submit("session", response(request, {}))
        with self.assertRaises(HylmsError) as caught:
            self.service.submit("session", response(request, {}))
        self.assertEqual(caught.exception.code, "runtime_response_duplicate")

    def test_timeout_and_cancel_remove_packets(self):
        self.folder.mkdir(parents=True)
        with self.assertRaises(HylmsError) as caught:
            FileExchange(self.folder, "session", "op", timeout=0)("interpret", {}, 1)
        self.assertEqual(caught.exception.code, "runtime_model_timeout")
        self.assertFalse((self.folder / "request.json").exists())
        atomic_write_json(self.folder / "cancel.json", {})
        with self.assertRaises(KeyboardInterrupt):
            FileExchange(self.folder, "session", "op")("interpret", {}, 1)

    def test_error_response_is_safe_and_not_a_decision(self):
        self.folder.mkdir(parents=True)
        def respond(_):
            request = json.loads((self.folder / "request.json").read_text())
            atomic_write_json(self.folder / "qa-capabilities.json", {
                "schema_version": 1, "session_id": "session", "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "discovery_method": "tool_registry", "discovered_tools": [], "status": "tools_missing"})
            self.service.submit("session", response(request, code="runtime_qa_unavailable"))
        with self.assertRaises(HylmsError) as caught:
            FileExchange(self.folder, "session", "op", sleep=respond)("qa", {}, 1)
        self.assertEqual(caught.exception.code, "runtime_qa_tools_missing")

    def test_worker_subprocess_roundtrip_without_production_engines(self):
        def launch(session, operation):
            process = subprocess.Popen([sys.executable, "-c",
                "from hylms.runtime import worker; from tests.test_hylms_runtime import ExchangeOnlyEngine; import sys; worker(sys.argv[1],sys.argv[2],sys.argv[3],engine_factory=ExchangeOnlyEngine,execution_check=lambda:None)",
                str(self.root), session, operation], cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            self.process = process
            return process.pid
        self.service.launcher = launch
        self.service.start("session", "$hylms")
        waiting = self.wait(lambda: (value if (value := self.service.status("session"))["status"] == "waiting" else None))
        self.service.submit("session", response(waiting["request"], {"ok": True}))
        self.assertEqual(self.process.wait(timeout=5), 0, self.process.stderr.read().decode())
        self.process.stderr.close()
        complete = self.service.status("session")
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(complete["result"]["echo"], {"ok": True})
        self.assertFalse((self.folder / "request.json").exists())
        self.assertEqual(self.service.start("session", "$hylms")["status"], "completed")

    def test_manual_requires_completed_pipeline(self):
        with self.assertRaises(HylmsError) as caught:
            self.service.start("session", manual={"instruction": "완료", "target_ids": ["id"]})
        self.assertEqual(caught.exception.code, "runtime_manual_not_ready")

    def test_manual_admission_accepts_clock_with_microseconds(self):
        self.folder.mkdir(parents=True)
        atomic_write_json(self.folder / "session.json", {"status": "completed", "pipeline_completed": True})
        state = phase2_state("r0")
        state["pending"] = [pending("existing")]
        path = self.root / "phase2_state.json"
        atomic_write_json(path, state)
        before = path.read_bytes()
        with mock.patch("hylms.core.now_kst", return_value=NOW.replace(microsecond=123456)):
            result = self.service.start("session", manual={"instruction": "확인 완료", "target_ids": ["existing"]})
        self.assertEqual(result["status"], "starting")
        self.assertEqual(result["mode"], "manual")
        self.assertEqual(path.read_bytes(), before)

    def test_pending_endpoint_reads_current_state_even_with_old_cached_report(self):
        self.folder.mkdir(parents=True)
        atomic_write_json(self.folder / "session.json", {"status": "completed", "pipeline_completed": True})
        atomic_write_json(self.folder / "result.json", {"report": "확인할 내용 없음"})
        state = phase2_state("r0")
        state["pending"] = [pending("existing")]
        path = self.root / "phase2_state.json"
        atomic_write_json(path, state)
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(self.service.pending("session")["first_target_id"], "existing")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        state["pending"] = []
        atomic_write_json(path, state)
        self.assertEqual(self.service.pending("session")["count"], 0)

    def test_alive_does_not_terminate_current_process(self):
        self.assertTrue(alive(os.getpid()))
