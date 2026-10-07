"""Build reviewable repairs from confirmed context; never commit without QA."""
from __future__ import annotations
import copy
from . import diff
from .course_context import read_context, classify_record, class_slot


def prepare_recovery(root, state, term, requested_at):
    context = read_context(root, state["term"])
    runs = diff.discover_runs(term)
    index = [r["id"] for r in runs].index(state["last_processed_run_id"])
    records, _ = diff._logical_records(runs[:index + 1])
    operations, targets, reasons = [], [], []
    for item in state["pending"]:
        details = item.get("context") or {}
        if details.get("kind") == "qa_pending":
            sources = [records.get(f"{item['course_id']}:{ref}") for ref in item["source_record_ids"]]
            if sources and all(record and classify_record(record) == "resource_only" for record in sources):
                targets.append(item["id"])
                operations.append({"op": "resolve_pending", "id": item["id"]})
                reasons.append("기술적 QA 질문을 자료정책에 따라 재검토: 알려진 마감 없음, 출석 대상 아님. 완료 표시만으로 의무를 만들지 않음. 원본과 일정은 보존.")
        elif details.get("kind") == "source_removed":
            linked = [next((e for e in state["natural_events"] if e["id"] == identity), None)
                      for identity in details.get("linked_event_ids", [])]
            # Closing a historical, non-attendance source alert does not cancel
            # its preserved event or assert that two provider IDs are identical.
            if linked and not details.get("linked_announcement_application_ids") and all(
                e and e["kind"] in {"class_session", "class_replacement", "special_event"}
                and e["attendance_required"] is False and e["action_state"] == "not_applicable"
                and e["timing"]["end"][:10] < requested_at[:10] for e in linked):
                targets.append(item["id"])
                operations.append({"op": "resolve_pending", "id": item["id"]})
                reasons.append("삭제 source의 연결 일정은 모두 과거의 비출석 대상 정보이다. 일정·출처·사용자 확정값은 보존하며 원본 교체의 동일성을 단정하지 않고 불필요한 사용자 확인만 QA 후 해소한다.")
        elif details.get("deadline_condition") == "수업 전까지" and details.get("date"):
            slot = class_slot(context, item["course_id"], details["date"])
            if slot is None:
                continue
            # Explicit dated announcement plus confirmed same-course timetable.
            # Do not infer semester-week dates, office hours or online lectures.
            targets.append(item["id"])
            source = item["source_record_ids"][0]
            event_id = f"{item['course_id']}:{source}:before-class-submission"
            if any(event["id"] == event_id for event in state["natural_events"]):
                targets.pop()
                continue
            event = {"id": event_id, "status": "active", "course_id": item["course_id"],
                     "course_name": item["course_name"], "title": item["title"].replace("시각 확인", "· 수업 전 제출"),
                     "kind": "submission", "timing": {"mode": "deadline", "all_day": False,
                         "start": None, "end": f"{slot['date']}T{slot['start']}:00+09:00", "end_inclusive": False},
                     "optional": False, "action_state": "unknown", "action_state_authority": "user",
                     "user_confirmed": {}, "location": slot["room"], "attendance_required": None,
                     "source_record_ids": copy.deepcopy(item["source_record_ids"]),
                     "evidence": [item["reason"], f"사용자 확정 시간표: {slot['date']} {slot['start']} 수업 시작, {slot['room']}"],
                     "details": {"deadline_condition": "수업 시작 전까지", "context_basis": "user_confirmed_timetable",
                                 "requirements": details.get("requirements", []), "late_submission_note": details.get("late_submission_note")}}
            operations.extend([{"op": "upsert_event", "value": event, "confirmed_fields": ["timing.end", "location"]}, {"op": "resolve_pending", "id": item["id"]}])
            reasons.append("명시된 제출 날짜와 같은 과목의 사용자 확정 시간표를 결합해 수업 전 마감 경계를 확인.")
    if not operations:
        return None
    instruction = "사용자가 요청한 자료관리/Schema QA 근본 복구와 고정 시간표 맥락 적용. 기술적 확인 대기는 실제 근거로 검토하고, 확정 맥락으로 해소 가능한 제출 시각만 반영한다. 불명확한 다른 pending 및 기존 일정은 유지한다."
    packet = diff.prepare_manual_packet(state, instruction, targets, requested_at)
    decision = {"schema_version": diff.MANUAL_SCHEMA_VERSION, "transaction_id": packet["transaction"]["id"],
                "reason": " ".join(dict.fromkeys(reasons)), "operations": operations}
    preview = diff.preview_manual_transaction(term, state, packet, decision)
    return {"packet": packet, "decision": decision, "preview": preview}
