"""Privacy-safe Canvas discussion participation normalization."""

from __future__ import annotations

import datetime as dt
from types import MappingProxyType
from typing import Any, Mapping

from .core import HylmsError, KST
from .config import load_config

PARTICIPATION_SCHEMA_VERSION = 1
PARTICIPATION_STATES = frozenset({
    "participated",
    "not_participated",
    "conflict",
    "unknown",
    "not_evaluated",
})
PARTICIPATION_BASES = frozenset({
    "graded_submission_and_own_reply",
    "approved_own_reply_rule",
    "no_approved_rule",
})
DISCUSSION_ENTRY_STATES = frozenset({"collected", "restricted", "unavailable", "not_available"})
SUPPORTED_DISCUSSION_SNAPSHOT_SCHEMAS = frozenset({4, 5})

EXAMPLE_OWN_REPLY_RULE_ID = "example-own-reply-v1"
APPROVED_OWN_REPLY_RULES: Mapping[tuple[str, str], str] = MappingProxyType({
    (str(rule["course_id"]), str(rule["discussion_id"])): rule["rule_id"]
    for rule in load_config().get("discussion_rules", [])
})

_PROJECTION_KEYS = {
    "state",
    "basis",
    "rule_id",
    "entries_state",
    "own_entry_count",
    "last_own_reply_at",
    "submission",
}


def _invalid(message: str) -> None:
    raise HylmsError("discussion_participation_invalid", message)


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{label}은 비어 있지 않은 문자열이어야 합니다.")
    return value


def _stable_id(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _invalid(f"{label}가 올바르지 않습니다.")
    return _nonempty_string(str(value), label)


def _timestamp(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _invalid(f"{label}은 시각 문자열이어야 합니다.")
    try:
        text = value.strip()
        if "T" not in text:
            raise ValueError("date-only value")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(KST).isoformat(timespec="seconds")
    except (TypeError, ValueError) as exc:
        raise HylmsError(
            "discussion_participation_invalid", f"{label}의 시각 형식이 올바르지 않습니다."
        ) from exc


def _safe_submission(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _invalid("discussion submission은 객체 또는 null이어야 합니다.")
    workflow = value.get("workflow_state")
    if workflow is not None and (not isinstance(workflow, str) or not workflow.strip()):
        _invalid("discussion submission.workflow_state가 올바르지 않습니다.")
    attempt = value.get("attempt")
    seconds_late = value.get("seconds_late")
    for label, number in (("attempt", attempt), ("seconds_late", seconds_late)):
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, int) or number < 0
        ):
            _invalid(f"discussion submission.{label}가 올바르지 않습니다.")
    for label in ("excused", "late", "missing"):
        flag = value.get(label)
        if flag is not None and not isinstance(flag, bool):
            _invalid(f"discussion submission.{label}가 올바르지 않습니다.")
    return {
        "workflow_state": workflow,
        "attempt": attempt,
        "submitted_at": _timestamp(value.get("submitted_at"), "discussion submission.submitted_at"),
        "graded_at": _timestamp(value.get("graded_at"), "discussion submission.graded_at"),
        "excused": value.get("excused"),
        "late": value.get("late"),
        "missing": value.get("missing"),
        "seconds_late": seconds_late,
    }


def _own_entry_evidence(entries: Any) -> tuple[str, int | None, str | None]:
    if not isinstance(entries, Mapping):
        _invalid("discussion entries는 객체여야 합니다.")
    state = entries.get("state")
    if state not in DISCUSSION_ENTRY_STATES:
        _invalid("discussion entries.state가 올바르지 않습니다.")
    items = entries.get("items")
    if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
        _invalid("discussion entries.items는 객체 목록이어야 합니다.")
    if state != "collected":
        if items:
            _invalid("수집되지 않은 discussion entries에는 item이 있을 수 없습니다.")
        return state, None, None

    entry_ids: list[str] = []
    created: list[dt.datetime] = []
    for item in items:
        entry_id = _stable_id(item.get("id"), "own entry.id")
        entry_ids.append(entry_id)
        raw_created = item.get("created_at")
        normalized = _timestamp(raw_created, "own entry.created_at")
        if normalized is not None:
            created.append(dt.datetime.fromisoformat(normalized))
    if len(entry_ids) != len(set(entry_ids)):
        _invalid("discussion entries.items에 중복 ID가 있습니다.")
    last = max(created).isoformat(timespec="seconds") if created else None
    return state, len(items), last


def _expected_state(
    basis: str,
    entries_state: str,
    own_entry_count: int | None,
    submission: Mapping[str, Any] | None,
) -> str:
    if basis == "no_approved_rule":
        return "not_evaluated"
    if entries_state != "collected":
        return "unknown"
    if basis == "approved_own_reply_rule":
        return "participated" if own_entry_count else "not_participated"

    workflow = submission.get("workflow_state") if submission is not None else None
    if workflow in {"submitted", "graded"}:
        return "participated" if own_entry_count else "conflict"
    if workflow == "unsubmitted":
        return "conflict" if own_entry_count else "not_participated"
    return "unknown"


def validate_discussion_participation(value: Mapping[str, Any]) -> None:
    """Validate the exact privacy-safe participation projection contract."""
    if not isinstance(value, Mapping) or set(value) != _PROJECTION_KEYS:
        _invalid("discussion participation key가 schema와 일치하지 않습니다.")
    if value["state"] not in PARTICIPATION_STATES:
        _invalid("discussion participation.state가 올바르지 않습니다.")
    basis = value["basis"]
    if basis not in PARTICIPATION_BASES:
        _invalid("discussion participation.basis가 올바르지 않습니다.")
    rule_id = value["rule_id"]
    if rule_id is not None:
        _nonempty_string(rule_id, "discussion participation.rule_id")
    if basis == "approved_own_reply_rule" and rule_id is None:
        _invalid("승인된 own-reply 판정에는 rule ID가 필요합니다.")
    if basis == "no_approved_rule" and rule_id is not None:
        _invalid("rule 없는 판정에는 rule ID가 있을 수 없습니다.")

    entries_state = value["entries_state"]
    if entries_state not in DISCUSSION_ENTRY_STATES:
        _invalid("discussion participation.entries_state가 올바르지 않습니다.")
    count = value["own_entry_count"]
    if entries_state == "collected":
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _invalid("수집된 participation의 own_entry_count가 올바르지 않습니다.")
    elif count is not None:
        _invalid("수집되지 않은 participation의 own_entry_count는 null이어야 합니다.")
    last = value["last_own_reply_at"]
    if last is not None:
        if count in {None, 0}:
            _invalid("본인 답글이 없으면 last_own_reply_at은 null이어야 합니다.")
        normalized = _timestamp(last, "discussion participation.last_own_reply_at")
        if normalized != last:
            _invalid("last_own_reply_at은 서울 시각 ISO 8601이어야 합니다.")

    submission = value["submission"]
    if basis == "graded_submission_and_own_reply":
        normalized_submission = _safe_submission(submission)
        if normalized_submission != submission:
            _invalid("discussion participation.submission이 canonical하지 않습니다.")
    elif submission is not None:
        _invalid("ungraded participation에는 submission을 전달하지 않습니다.")
    expected = _expected_state(basis, entries_state, count, submission)
    if value["state"] != expected:
        _invalid("discussion participation 신호와 state가 일치하지 않습니다.")


def normalize_discussion_participation(
    course_id: str,
    discussion: Mapping[str, Any],
    *,
    rules: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """Derive participation without exposing entry bodies, IDs, feedback, or authors."""
    normalized_course_id = _stable_id(course_id, "course_id")
    if not isinstance(discussion, Mapping):
        _invalid("discussion은 객체여야 합니다.")
    discussion_id = _stable_id(discussion.get("id"), "discussion.id")
    selected_rules = APPROVED_OWN_REPLY_RULES if rules is None else rules
    if not isinstance(selected_rules, Mapping):
        _invalid("discussion participation rules는 mapping이어야 합니다.")
    rule_id = selected_rules.get((normalized_course_id, discussion_id))
    if rule_id is not None:
        _nonempty_string(rule_id, "discussion participation rule ID")

    assignment_id = discussion.get("assignment_id")
    graded = assignment_id is not None
    if graded:
        _stable_id(assignment_id, "discussion.assignment_id")
    entries_state, own_entry_count, last_own_reply_at = _own_entry_evidence(
        discussion.get("entries")
    )
    if graded:
        basis = "graded_submission_and_own_reply"
        submission = _safe_submission(discussion.get("submission"))
    elif rule_id is not None:
        basis = "approved_own_reply_rule"
        submission = None
    else:
        basis = "no_approved_rule"
        submission = None

    projection = {
        "state": _expected_state(basis, entries_state, own_entry_count, submission),
        "basis": basis,
        "rule_id": rule_id,
        "entries_state": entries_state,
        "own_entry_count": own_entry_count,
        "last_own_reply_at": last_own_reply_at,
        "submission": submission,
    }
    validate_discussion_participation(projection)
    return projection


def discussion_participation_report(
    snapshot: Mapping[str, Any],
    *,
    rules: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """Build a stable, privacy-safe course report for LMS comparison."""
    if not isinstance(snapshot, Mapping):
        _invalid("snapshot은 객체여야 합니다.")
    schema_version = snapshot.get("schema_version")
    if schema_version not in SUPPORTED_DISCUSSION_SNAPSHOT_SCHEMAS:
        _invalid("지원하지 않는 discussion snapshot schema입니다.")
    term = snapshot.get("term")
    course = snapshot.get("course")
    discussions = snapshot.get("discussions")
    if not isinstance(term, Mapping) or not isinstance(course, Mapping):
        _invalid("snapshot term 또는 course가 올바르지 않습니다.")
    if not isinstance(discussions, list) or any(not isinstance(item, Mapping) for item in discussions):
        _invalid("snapshot discussions는 객체 목록이어야 합니다.")
    term_id = _stable_id(term.get("id"), "term.id")
    course_id = _stable_id(course.get("id"), "course.id")
    course_name = _nonempty_string(course.get("name"), "course.name")

    records: list[dict[str, Any]] = []
    for discussion in discussions:
        discussion_id = _stable_id(discussion.get("id"), "discussion.id")
        title = discussion.get("title")
        if title is not None and not isinstance(title, str):
            _invalid("discussion.title이 올바르지 않습니다.")
        assignment_id = discussion.get("assignment_id")
        if assignment_id is not None:
            assignment_id = _stable_id(assignment_id, "discussion.assignment_id")
        records.append(
            {
                "discussion_id": discussion_id,
                "title": title,
                "assignment_id": assignment_id,
                "participation": normalize_discussion_participation(
                    course_id, discussion, rules=rules
                ),
            }
        )
    records.sort(key=lambda item: item["discussion_id"])
    record_ids = [item["discussion_id"] for item in records]
    if len(record_ids) != len(set(record_ids)):
        _invalid("snapshot discussions에 중복 ID가 있습니다.")
    counts = {"total": len(records)}
    counts.update(
        {
            state: sum(item["participation"]["state"] == state for item in records)
            for state in sorted(PARTICIPATION_STATES)
        }
    )
    return {
        "schema_version": PARTICIPATION_SCHEMA_VERSION,
        "term_id": term_id,
        "course_id": course_id,
        "course_name": course_name,
        "records": records,
        "counts": counts,
    }
