"""Production adapters around the independently validated HY-LMS engines."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from . import diff
from .core import HylmsError, now_kst
from .credentials import WindowsCredentialStore
from .google_calendar import sync_google_calendar
from .ics import prepare_calendar, write_ics
from .ntfy import prepare_ntfy_delivery, send_ntfy_delivery
from .orchestration import (STAGES, CommitReceipt, InputRefs, Preview, RunPair,
                            StepOutcome, WorkflowContext, run_workflow)
from .qa import build_qa_prompt, prepare_qa_packet, qa_next_action
from .pending_agenda import create_agenda, extend_agenda
from .course_context import read_context, classify_record, record_review, unresolved_reviews, technical_pending


def error(code):
    raise HylmsError(code, code)


def read_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        diff.validate_state(state)
        return state
    except (OSError, ValueError, UnicodeError, HylmsError):
        error("runtime_state_invalid")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def pending_review(state, agenda=None):
    """Current conversation agenda, including old pending; never a mutation."""
    user_pending = [p for p in state["pending"] if not technical_pending(p) and p.get("context", {}).get("resolution_owner") != "source"]
    ids = {item["id"] for item in user_pending}
    agenda = create_agenda(ids, ids) if agenda is None else extend_agenda(agenda, ids)
    items = []
    for pending in user_pending:
        item = {key: pending[key] for key in ("id", "course_name", "title", "reason")}
        item["label"] = agenda["labels"][pending["id"]]
        item["group"] = item["label"][0]
        question = pending["context"].get("question")
        item["question"] = question if isinstance(question, str) and question.strip() else (
            f"‘{pending['title']}’에 대해 확인된 내용이나 원하는 처리 방식을 알려주세요."
        )
        items.append(item)
    items.sort(key=lambda item: (item["group"], int(item["label"][1:])))
    counts = {"new": sum(item["group"] == "A" for item in items),
              "existing": sum(item["group"] == "B" for item in items)}
    return {"items": items, "count": len(items), "counts": counts,
            "technical_items": [{"id": p["id"], "title": p["title"], "issue_codes": p.get("context", {}).get("issue_codes", [])}
                                for p in state["pending"] if technical_pending(p)],
            "source_waiting": [{"id": p["id"], "title": p["title"], "reason": p["reason"]}
                               for p in state["pending"] if p.get("context", {}).get("resolution_owner") == "source"],
            "first_target_id": items[0]["id"] if items else None,
            "opening_question": items[0]["question"] if items else None}


def collect_snapshot(root):
    """No credential setup, interactive stdin, or raw CLI output escapes here."""
    if WindowsCredentialStore().read() is None:
        error("auth_missing")
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(root) / "hylms_snapshot.py")], cwd=root,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=1800, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        error("snapshot_timeout")
    if completed.returncode and isinstance(completed.stdout, str) and "오류[auth_rejected]" in completed.stdout:
        error("auth_rejected")
    return completed.returncode


def outcome_counts(counts, codes=(), warnings=()):
    counts = {key: value for key, value in counts.items() if type(value) is int and value >= 0}
    failed = counts.get("failed", 0)
    status = ("partial_failure" if counts.get("succeeded", 0) else "failed") if failed else (
        "warning" if warnings else "success")
    codes = list(codes or (("runtime_stage_failed",) if failed else ()))
    for warning in warnings or ():
        code = warning.split(":", 1)[0] if isinstance(warning, str) else "source_warning"
        codes.append(code if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else "source_warning")
    return StepOutcome(status, counts=counts, codes=tuple(sorted(set(codes))))


class EngineAdapters:
    def __init__(self, root, exchange, *, clock=now_kst, collector=collect_snapshot,
                 ntfy_transport=None, google_client=None, check_cancel=lambda: None):
        self.root = Path(root).resolve()
        self.path = self.root / "phase2_state.json"
        self.exchange, self.clock, self.collector = exchange, clock, collector
        self.ntfy_transport, self.google_client = ntfy_transport, google_client
        self.check_cancel = check_cancel
        self.state = None
        self.term = None
        self.new_pending_ids = set()
        self.receipts = []
        self.external = {}
        self.text_counts = {"added": 0, "modified": 0, "deleted": 0}
        self.text_counts_complete = False
        self.collection_failed = False
        self.invalid_input = False
        self.progress = lambda **kwargs: None
        self.snapshot_hashes = None
        self.event_changes = []
        self.baseline_pending_ids = None
        self.agenda = None
        self.agenda_writer = lambda agenda: None

    def ensure_agenda(self):
        ids = {item["id"] for item in self.state["pending"]} if self.state else set()
        if self.agenda is None:
            baseline = ids if self.baseline_pending_ids is None else self.baseline_pending_ids
            updated = create_agenda(baseline, ids)
        else:
            updated = extend_agenda(self.agenda, ids)
        if updated != self.agenda:
            self.agenda_writer(updated)
            self.agenda = updated
        return self.agenda

    def review(self):
        return pending_review(self.state or {"pending": []}, self.ensure_agenda())

    def load(self):
        state = read_state(self.path)
        term = self.root / "snapshots" / state["term"]
        if term.resolve().parent != (self.root / "snapshots").resolve():
            error("runtime_term_mismatch")
        diff.validate_state_references(state, term)
        self.state, self.term = state, term
        self.snapshot_hashes = self.source_hashes()

    def source_hashes(self):
        hashes = {str(path.relative_to(self.term)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.term.rglob("*.json")}
        hashes["__course_context__"] = fingerprint(read_context(self.root, self.state["term"]))
        return hashes

    def refs(self):
        runs = diff.discover_runs(self.term)
        if not runs:
            error("runtime_snapshot_missing")
        return InputRefs(runs[-1]["id"], self.state["last_processed_run_id"],
                         str(self.term), fingerprint(self.state))

    def guard(self, request=None):
        self.check_cancel()
        if self.invalid_input or self.state is None:
            error("runtime_state_invalid")
        if fingerprint(read_state(self.path)) != fingerprint(self.state):
            error("runtime_input_changed")
        if self.source_hashes() != self.snapshot_hashes:
            error("runtime_input_changed")
        if request and request.inputs and request.inputs.state_ref != fingerprint(self.state):
            error("runtime_input_changed")

    def adapters(self):
        def wrapped(name):
            def invoke(request):
                self.check_cancel()
                self.progress(stage=name, status="running")
                outcome = getattr(self, name)(request)
                self.progress(stage=name, status="running", last_completed_stage=name,
                              state_cursor=self.state["last_processed_run_id"] if self.state else None)
                return outcome
            return invoke
        return {name: wrapped(name) for name in STAGES}

    def run(self, session):
        try:
            self.load()
            initial = self.refs()
        except HylmsError:
            initial = None
        self.baseline_pending_ids = {item["id"] for item in self.state["pending"]} if self.state else set()
        recovery = self.recover_known_context(session) if self.state else None
        if self.state:
            initial = self.refs()
        result = run_workflow(WorkflowContext(session, str(self.root), initial),
                              self.adapters(), clock=self.clock)
        result["summary"] = self.summary()
        result["new_pending"] = result["summary"]["pending_review"]["counts"]["new"]
        result["context_recovery"] = recovery
        if result["summary"]["technical_reviews"] and result["status"] in {"success", "warning"}:
            result["status"] = "partial_failure"
        result["report"] = render_report(result)
        return result

    def recover_known_context(self, session):
        from .context_recovery import prepare_recovery
        for review in unresolved_reviews(self.root):
            data = review["details"]
            if review["status"] == "approved" and data.get("packet", {}).get("candidate_state_sha256") == fingerprint(self.state):
                record_review(self.root, review["id"], "resolved", {**data, "reconciled_after_commit": True})
        proposed = prepare_recovery(self.root, self.state, self.term, self.clock().isoformat(timespec="seconds"))
        if proposed is None:
            targets = [p for p in self.state["pending"] if technical_pending(p)]
            if not targets:
                return None
            identity = "context-recovery:" + fingerprint([p["id"] for p in targets])
            instruction = "사용자가 확정한 과목 맥락과 자료관리 규칙으로 시스템의 미해결 검토를 재판단한다. 사용자가 Schema QA를 대신 수행하는 작업이 아니다. 실제 의무와 확정값을 보존하고, 자료가 부족하면 오류를 반환하며 해결을 조작하지 않는다."
            manual_packet = diff.prepare_manual_packet(self.state, instruction, [p["id"] for p in targets], self.clock().isoformat(timespec="seconds"))
            payload = {"packet": manual_packet, "entities": targets, "rules": self.state["rules"],
                       "course_context": read_context(self.root, self.state["term"]),
                       "structured_evidence": diff._structured_qa_evidence(self.term, self.state, targets),
                       "target_labels": {}, "previous_decision": None, "feedback": None}
            record_review(self.root, identity, "reviewing", {"payload": payload})
            try:
                decision = self.exchange("manual", payload, 1)
                preview = diff.preview_manual_transaction(self.term, self.state, manual_packet, decision)
                proposed = {"packet": manual_packet, "decision": decision, "preview": preview}
            except HylmsError as exc:
                record_review(self.root, identity, "blocked", {"payload": payload, "code": exc.code})
                return {"status": "blocked", "code": exc.code}
        identity = "context-recovery:" + fingerprint(proposed["packet"]["transaction"]["target_ids"])
        originals = [p for p in self.state["pending"] if p["id"] in proposed["packet"]["transaction"]["target_ids"]]
        for attempt in (1, 2):
            self.guard()
            packet = prepare_qa_packet(proposed["preview"]["qa_context"], attempt=attempt, review_id=f"{session}:context-recovery")
            record_review(self.root, identity, "reviewing", {"original_pending": originals, "packet": packet})
            try:
                verdict = self.exchange("qa", {"packet": packet, "prompt": build_qa_prompt(packet)}, attempt)
                action = qa_next_action(packet, verdict)
                if action == "revise":
                    payload = {"packet": proposed["packet"], "entities": originals, "rules": self.state["rules"],
                               "course_context": read_context(self.root, self.state["term"]),
                               "structured_evidence": packet["structured_changes"], "target_labels": {},
                               "previous_decision": proposed["decision"], "feedback": verdict}
                    decision = self.exchange("manual", payload, attempt + 1)
                    proposed["preview"] = diff.preview_manual_transaction(self.term, self.state, proposed["packet"], decision)
                    proposed["decision"] = decision
                    continue
                if action != "commit":
                    record_review(self.root, identity, "blocked", {"packet": packet, "verdict": verdict})
                    return {"status": "blocked", "review_id": identity}
                self.guard()
                record_review(self.root, identity, "approved", {"packet": packet, "verdict": verdict})
                committed = diff.commit_manual_state(self.path, self.state, proposed["packet"], proposed["decision"],
                    term_directory=self.term, qa_packet=packet, qa_verdict=verdict)
                record_review(self.root, identity, "resolved", {"packet": packet, "verdict": verdict, "receipt": committed["receipt"]})
                before_events = {e["id"]: e for e in self.state["natural_events"]}
                for event in committed["state"]["natural_events"]:
                    if before_events.get(event["id"]) != event:
                        self.event_changes.append({"id": event["id"], "title": event["title"],
                            "kind": "created" if event["id"] not in before_events else "modified"})
                self.state = committed["state"]
                self.receipts.append(committed["receipt"])
                return {"status": "success", "changed_entity_ids": committed["receipt"]["changed_entity_ids"]}
            except HylmsError as exc:
                record_review(self.root, identity, "blocked", {"packet": packet, "code": exc.code})
                return {"status": "blocked", "review_id": identity, "code": exc.code}
        return {"status": "blocked", "review_id": identity}

    def collect(self, request):
        self.check_cancel()
        before = {item["id"] for item in diff.discover_runs(self.term)} if self.term else set()
        inventories = {str(p): p.read_bytes() for p in (self.root / "snapshots").glob("*/status.json")}
        code = None
        try:
            exit_code = self.collector(self.root)
            if exit_code not in {0, 1, 2}:
                error("snapshot_process_failed")
        except HylmsError as exc:
            exit_code, code = 1, exc.code
        except Exception:
            exit_code, code = 1, "snapshot_process_failed"
        self.check_cancel()
        try:
            self.load()
            refs = self.refs()
            if request.inputs and refs.state_ref != request.inputs.state_ref:
                error("runtime_input_changed")
            # A collection in a different term must not drive writes to an old term.
            if any(p.parent.name != self.state["term"] and inventories.get(str(p)) != p.read_bytes()
                   for p in (self.root / "snapshots").glob("*/status.json")):
                error("runtime_term_mismatch")
            runs = diff.discover_runs(self.term)
            new = [item for item in runs if item["id"] not in before]
            if code is None and not new:
                code = "snapshot_archive_missing"
            status = new[-1]["status"] if new else {}
            if code is None and (status.get("exit_code") != exit_code
                                 or status.get("overall_status") not in {"success", "no_courses"}):
                code = "snapshot_partial_failure" if exit_code == 2 else "snapshot_failed"
        except HylmsError:
            self.invalid_input = True
            raise
        self.collection_failed = bool(code)
        if code:
            details = {value for course in status.get("courses", [])
                       for value in [course.get("error_code"), *course.get("warning_codes", [])]
                       if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value)}
            return StepOutcome("failed", refs, codes=tuple([code, *sorted(details - {code})]), fallback=True)
        return StepOutcome(data=refs, counts={"succeeded": 1})

    def prepare(self, request):
        try:
            self.guard(request)
            packet = diff.prepare_decision_packet(self.term, self.state)
        except Exception:
            self.text_counts_complete = False
            raise
        self.text_counts_complete = True
        if packet is None:
            return StepOutcome()
        tx = packet["transaction"]
        for change in packet["changes"]:
            kind = "modified" if change["type"] == "text_modified" else change["type"]
            if kind in self.text_counts:
                self.text_counts[kind] += 1
        return StepOutcome(data=RunPair(tx["previous_run_id"], tx["current_run_id"],
                                       packet, bool(packet["changes"])))

    def interpretation_context(self, packet):
        courses = {item["course_id"] for item in packet["changes"]}
        value = {key: [item for item in self.state[key] if item["course_id"] in courses]
                 for key in ("natural_events", "pending", "announcement_applications")}
        value["rules"] = self.state["rules"]
        value["course_context"] = read_context(self.root, self.state["term"])
        runs = diff.discover_runs(self.term)
        index = next(i for i, run in enumerate(runs) if run["id"] == packet["transaction"]["current_run_id"])
        records, _ = diff._logical_records(runs[:index + 1])
        value["structured_targets"] = [
            {"course_id": item["course_id"], "source_record_id": item["qualified_id"],
             "title": item["text"]["compare"].get("title", ""),
             "schedule": item["structured"]["compare"].get("schedule"),
             "kind": item["structured"]["compare"].get("kind"),
             "progress": item["structured"]["compare"].get("progress"),
             "attendance": item["structured"]["compare"].get("attendance"),
             "management": classify_record(item),
             "linked_entity": item["structured"]["compare"].get("linked_entity")}
            for item in records.values() if item["course_id"] in courses
            and item["section"] in {"assignment", "quiz", "discussion", "weekly_learning"}
        ]
        diff._safe_json(value)
        return value

    def interpret(self, request):
        self.guard(request)
        data = {"packet": request.pair.packet,
                "context": self.interpretation_context(request.pair.packet),
                "previous_decision": request.decision, "feedback": request.qa_verdict}
        return StepOutcome(data=self.exchange("interpret", data, request.attempt))

    def decision(self, request):
        return request.decision if request.decision is not None else {
            "schema_version": diff.DECISION_SCHEMA_VERSION,
            "transaction_id": request.pair.packet["transaction"]["id"], "decisions": [],
        }

    def preview(self, request):
        self.guard(request)
        preview = diff.preview_state_transaction(self.term, self.state, self.decision(request))
        return StepOutcome(data=Preview(preview["semantic_mutation"], preview["qa_context"]))

    def qa(self, request):
        self.guard(request)
        identity = f"pair:{self.state['term']}:{request.pair.previous_run}:{request.pair.current_run}"
        try:
            verdict = self.exchange("qa", {"packet": request.qa_packet,
                "prompt": build_qa_prompt(request.qa_packet)}, request.attempt)
        except HylmsError as exc:
            record_review(self.root, identity, "blocked", {"packet": request.qa_packet, "code": exc.code})
            raise
        action = qa_next_action(request.qa_packet, verdict)
        if action in {"fail", "pending"}:
            identity = f"pair:{self.state['term']}:{request.pair.previous_run}:{request.pair.current_run}"
            record_review(self.root, identity, "blocked", {"packet": request.qa_packet, "verdict": verdict})
        return StepOutcome(data=verdict)

    def commit(self, request):
        self.guard(request)
        before_events = {item["id"]: item for item in self.state["natural_events"]}
        if request.commit_mode == "pending":
            error("phase2_qa_review_required")
        else:
            committed = diff.commit_state(self.path, self.state, self.decision(request),
                term_directory=self.term, qa_packet=request.qa_packet, qa_verdict=request.qa_verdict)
        self.state = committed["state"]
        identity = f"pair:{self.state['term']}:{request.pair.previous_run}:{request.pair.current_run}"
        if any(item["id"] == identity for item in unresolved_reviews(self.root)):
            record_review(self.root, identity, "resolved", {"receipt": committed["receipt"], "verdict": request.qa_verdict})
        for item in self.state["natural_events"]:
            before = before_events.get(item["id"])
            if item != before:
                kind = "created" if before is None else "cancelled" if item["status"] == "cancelled" and before["status"] != "cancelled" else "modified"
                self.event_changes.append({"kind": kind, "id": item["id"], "title": item["title"]})
        receipt = committed["receipt"]
        self.receipts.append(receipt)
        ids = {item["id"] for item in receipt["new_pending"] if not technical_pending(item) and item.get("context", {}).get("resolution_owner") != "source"}
        new = ids - self.new_pending_ids
        self.new_pending_ids.update(ids)
        return StepOutcome(data=CommitReceipt(self.refs(), len(new)),
                           counts={"succeeded": 1, **receipt["operation_counts"]})

    def project(self, request):
        self.guard(request)
        self.ensure_agenda()
        projection = prepare_calendar(self.term, self.state, now=self.clock())
        normalized = outcome_counts({}, warnings=projection["warnings"])
        return StepOutcome(normalized.status, data=projection, codes=normalized.codes)

    def ntfy(self, request):
        self.guard(request)
        ids = {item["id"] for item in self.review()["items"] if item["group"] == "A"}
        delivery = prepare_ntfy_delivery(self.term, self.state, now=self.clock(), new_pending_ids=sorted(ids))
        self.guard(request)
        result = send_ntfy_delivery(delivery, transport=self.ntfy_transport)
        counts = {"succeeded": result["counts"]["sent"], "failed": result["counts"]["failed"]}
        for group in ("urgent", "general"):
            counts[group] = sum(item["status"] == "sent" and item["group"] == group for item in result["deliveries"])
        self.external["ntfy"] = counts
        codes = [item["error_code"] for item in result["deliveries"] if item["error_code"]]
        return outcome_counts(counts, codes, delivery.get("warnings"))

    def ics(self, request):
        self.guard(request)
        result = write_ics(request.projection)
        count = len(request.projection["events"])
        success = result["status"] == "written"
        counts = {"succeeded": int(success), "failed": int(not success), "events": count}
        self.external["ics"] = counts
        return outcome_counts(counts, () if success else ("ics_write_failed",), result["warnings"])

    def google(self, request):
        self.guard(request)
        result = sync_google_calendar(self.term, self.path,
            state_path=self.root / "google_calendar_state.json", client=self.google_client, clock=self.clock)
        counts = dict(result["counts"])
        counts["succeeded"] = sum(value for key, value in counts.items() if key != "failed")
        self.external["google"] = {"counts": counts, "failures": result["failures"],
            "excluded_completed": result["excluded_completed"],
            "excluded_non_actionable": result["excluded_non_actionable"]}
        return outcome_counts(counts, [item["code"] for item in result["failures"]], result.get("warnings"))

    def summary(self):
        pending = self.state["pending"] if self.state else []
        review = self.review()
        new_ids = {item["id"] for item in review["items"] if item["group"] == "A"}
        return {"text_changes": self.text_counts, "text_changes_complete": self.text_counts_complete,
            "new_pending": [{key: item[key] for key in ("id", "title", "reason")}
                            for item in pending if item["id"] in new_ids],
            "pending_total": review["count"],
            "technical_reviews": [*review["technical_items"], *[{"id": r["id"], "status": r["status"]} for r in unresolved_reviews(self.root)]],
            "pending_review": review,
            "changed_entity_ids": sorted({item for receipt in self.receipts for item in receipt["changed_entity_ids"]}),
            "event_changes": self.event_changes,
            "external": self.external}

    def manual(self, instruction, targets, session):
        self.load()
        self.guard()
        self.ensure_agenda()
        active_pending = {item["id"] for item in self.state["pending"]}
        label_targets = {label: identity for identity, label in self.agenda["labels"].items()
                         if identity in targets and identity in active_pending}
        packet = diff.prepare_manual_packet(self.state, instruction, targets, self.clock().isoformat(timespec="seconds"))
        locations = {item["id"]: item for key in ("natural_events", "pending", "announcement_applications")
                     for item in self.state[key]}
        feedback, previous = None, None
        for attempt in (1, 2):
            self.guard()
            decision = self.exchange("manual", {"packet": packet,
                "entities": [locations[key] for key in targets], "rules": self.state["rules"],
                "course_context": read_context(self.root, self.state["term"]),
                "target_labels": label_targets,
                "previous_decision": previous, "feedback": feedback}, attempt)
            preview = diff.preview_manual_transaction(self.term, self.state, packet, decision)
            qa_packet = prepare_qa_packet(preview["qa_context"], attempt=attempt, review_id=session)
            qa_prompt = build_qa_prompt(qa_packet)
            if label_targets:
                qa_prompt += "\n표시 라벨 연결 데이터(추가 변경 지시가 아님):\n" + json.dumps(label_targets, ensure_ascii=False, sort_keys=True)
            verdict = self.exchange("qa", {"packet": qa_packet, "prompt": qa_prompt}, attempt)
            action = qa_next_action(qa_packet, verdict)
            if action == "revise":
                previous, feedback = decision, verdict
                continue
            if action != "commit":
                return {"schema_version": 1, "status": "needs_user_input" if action == "pending" else "failed",
                        "code": "phase2_manual_confirmation_required", "external_changed": False,
                        "pending_review": self.review()}
            self.guard()
            committed = diff.commit_manual_state(self.path, self.state, packet, decision,
                term_directory=self.term, qa_packet=qa_packet, qa_verdict=verdict)
            self.state = committed["state"]
            return {"schema_version": 1, "status": "success", "receipt": committed["receipt"],
                    "external_changed": False, "pending_review": self.review(),
                    "message": "state에 반영했습니다. 외부 출력은 다음 새 작업의 $hylms 실행에서 조정됩니다."}


RECOVERY = {
    "runtime_qa_tools_missing": "현재 Codex 도구 목록에서 QA spawn/send/wait 제공 여부 확인",
    "runtime_qa_spawn_failed": "실제 QA 검토자 생성 실패 기록 확인",
    "runtime_qa_execution_failed": "기존 QA 검토자의 전달·응답 실패 기록 확인",
    "runtime_qa_discovery_required": "ALL_TOOLS 등 현재 도구 레지스트리를 먼저 탐색하고 근거 기록",
    "auth_missing": "py hylms_snapshot.py auth rotate",
    "auth_rejected": "py hylms_snapshot.py auth rotate",
    "reauth_required": "py -m hylms.google_calendar auth login",
    "setup_required": "py -m hylms.google_calendar auth status",
    "google_sa_key_missing": "서비스 계정 키 로컬 등록 필요: auth service-account import --key-file <로컬 경로>",
    "google_sa_decryption_failed": "설정된 credential-owner Windows 계정 및 DPAPI 키 저장 상태 확인",
    "google_sa_permission_denied": "기존 HY-LMS 캘린더의 서비스 계정 writer 공유 권한 확인",
    "google_sa_token_failed": "서비스 계정 및 키 활성 상태 확인; 본계정 OAuth 로그인 대상 아님",
    "google_sa_dependency_missing": "requirements-service-account.txt의 의존 패키지 설치 확인",
}


def render_report(result):
    labels = {"success": "전체 완료", "warning": "경고와 함께 완료",
              "partial_failure": "일부 단계 실패", "failed": "일부 단계 실패"}
    summary = result["summary"]
    output = result["output"] or {}
    lines = ["## 실행 상태", "", labels[result["status"]],
             f"실행: {result['started_at']} · snapshot: {output.get('snapshot_run', '없음')} · cursor: {output.get('cursor', '없음')}",
             "", "## LMS 변경", "", f"처리한 run pair {result['pairs_committed']}개 · 변경 항목 {len(summary['changed_entity_ids'])}개",
             (f"텍스트 추가 {summary['text_changes']['added']} · 수정 {summary['text_changes']['modified']} · 삭제 {summary['text_changes']['deleted']}"
              if summary.get("text_changes_complete", True) else "텍스트 변경 수 미집계 — 준비 단계가 완료되지 않았습니다."),
             f"새 확인 대기 {len(summary['new_pending'])}개 · 기존 확인 대기 {summary['pending_total'] - len(summary['new_pending'])}개 · 전체 {summary['pending_total']}개",
             "", "## 외부 반영", ""]
    qa_labels = {"commit": "통과", "pending": "확인 대기로 확정", "revise": "수정 요청", "fail": "검토 실패",
                 "skipped": "미실행", "failed": "검토 실패", "success": "통과", "warning": "경고"}
    qa_actions = [qa_labels.get(step.get("qa_action", step["status"]), "검토 결과 확인")
                  for step in result["steps"] if step["stage"] == "qa"]
    if result.get("context_recovery"):
        qa_actions.insert(0, "기존 검토 복구 통과" if result["context_recovery"]["status"] == "success" else "기존 시스템 검토 미완료")
    insert = lines.index("## 외부 반영") - 1
    details = ["QA: " + (", ".join(qa_actions) if qa_actions else "변경 없음으로 미실행")]
    for item in summary["event_changes"]:
        verb = {"created": "생성", "modified": "수정", "cancelled": "취소"}[item["kind"]]
        details.append(f"- {verb}: {item['title']}")
    lines[insert:insert] = details
    status_labels = {"success": "완료", "warning": "경고와 함께 완료", "partial_failure": "일부 실패",
                     "failed": "실패", "skipped": "보류"}
    count_labels = {"succeeded": "성공", "failed": "실패", "created": "생성", "updated": "수정",
                    "restored": "복원", "deleted": "삭제", "unchanged": "유지", "events": "일정",
                    "urgent": "긴급 전송", "general": "일반 전송"}
    for stage, label in (("ntfy", "ntfy"), ("ics", "ICS"), ("google", "Google Calendar")):
        step = next(item for item in result["steps"] if item["stage"] == stage)
        counts = ", ".join(f"{count_labels.get(key, key)} {value}" for key, value in step["counts"].items())
        lines.append(f"- {label}: {status_labels[step['status']]}" + (f" · {counts}" if counts else ""))
        if stage == "google" and stage in summary["external"]:
            excluded = summary["external"][stage]
            lines.append(f"  완료 제외 {excluded.get('excluded_completed', 0)} · 비대상 제외 {excluded.get('excluded_non_actionable', 0)}")
    lines.extend(["", "## 확인할 내용", ""])
    actions = []
    if summary.get("technical_reviews"):
        actions.append(f"- 시스템 재검토 미완료 {len(summary['technical_reviews'])}건: 사용자 학사 질문이 아닙니다. QA 검토/근거 복구가 필요합니다.")
    if summary["pending_review"].get("source_waiting"):
        waiting = summary["pending_review"]["source_waiting"]
        actions.append(f"- 후속 공지 대기 {len(waiting)}건: 시스템이 이후 LMS 변경에서 재확인합니다. 사용자 답변은 필요하지 않습니다.")
        actions.extend(f"  · {item['title']}" for item in waiting)
    recovery = result.get("context_recovery")
    if recovery and recovery.get("status") == "success":
        actions.append(f"- 고정 시간표·자료정책에 따른 독립 QA 복구 완료: {len(recovery['changed_entity_ids'])}개 항목 반영")
    codes = sorted({code for step in result["steps"] for code in step["codes"]
                    if code not in {"no_text_changes", "no_semantic_mutation", "pending_confirmation"}})
    actions.extend(f"- {code}" + (f" · 복구: `{RECOVERY[code]}`" if code in RECOVERY else "") for code in codes)
    if result["fallback"]:
        actions.append("- 일부 단계는 마지막 정상 입력을 사용했습니다.")
    for failure in summary["external"].get("google", {}).get("failures", []):
        actions.append(f"- Google 항목 {failure['id']}: {failure['code']}")
    review = summary["pending_review"]
    if review["items"]:
        actions.extend(["", f"확인대기 {review['counts']['new']}+({review['counts']['existing']}) — 모든 항목을 함께 확인해 봅시다."])
        for group, title, count in (("A", "신규 확인 대기", review["counts"]["new"]),
                                    ("B", "기존 확인 대기", review["counts"]["existing"])):
            actions.extend(["", f"{group}. {title} — {count}건"])
            actions.extend(f"{item['label']}. [{item['course_name']}] {item['title']} — {item['question']}"
                           for item in review["items"] if item["group"] == group)
            if count == 0:
                actions.append("없음")
        examples = "·".join(item["label"] for item in review["items"][:2])
        actions.extend(["", f"{examples}처럼 라벨을 붙여 한 건씩 또는 여러 건을 함께 답해주세요. 모르는 항목은 보류로 유지하고 다음 실행에서 다시 확인하겠습니다."])
    lines.extend(actions or ["확인할 내용 없음"])
    return "\n".join(lines)
