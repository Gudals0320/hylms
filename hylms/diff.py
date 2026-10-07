"""Immutable snapshot-run discovery, semantic diffing, and Phase 2 state commits."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .core import HylmsError
from .config import load_config
from .participation import normalize_discussion_participation
from .qa import prepare_qa_packet, qa_next_action, validate_qa_verdict
from .storage import atomic_write_json

SUPPORTED_SNAPSHOT_SCHEMAS = {4, 5}
STATE_SCHEMA_VERSION = 5
V4_STATE_SCHEMA_VERSION = 4
DECISION_SCHEMA_VERSION = 2
MANUAL_SCHEMA_VERSION = 1
STATE_KEYS = {
    "schema_version", "phase", "term", "timezone", "last_processed_run_id",
    "natural_events", "pending", "rules", "announcement_applications", "last_failure",
}
EVENT_KEYS = {
    "id", "status", "course_id", "course_name", "title", "kind", "timing", "optional",
    "action_state", "action_state_authority", "user_confirmed", "location",
    "attendance_required", "source_record_ids", "evidence", "details",
}
V4_EVENT_KEYS = EVENT_KEYS - {"action_state_authority", "user_confirmed"}
PENDING_KEYS = {
    "id", "status", "course_id", "course_name", "title", "source_record_ids", "reason", "context",
}
APPLICATION_KEYS = {
    "id", "course_id", "course_name", "source_record_ids", "target_record_ids", "patch", "evidence",
}
PATCH_KEYS = {
    "timing", "attendance_required", "attendance_excluded_weeks",
    "attendance_status_check_required", "minimum_study_time_required", "delivery_mode",
    "maximum_playback_speed", "required_for_all", "optional", "location", "requirements",
    "consequence", "details",
}
EVENT_STATUSES = {"active", "cancelled"}
EVENT_KINDS = {
    "class_replacement", "class_session", "special_event", "action", "activity", "submission",
    "application",
}
TIMING_MODES = {"session", "period", "deadline", "action_window"}
ACTION_STATES = {"not_applicable", "unknown", "done"}
V4_ACTION_STATES = ACTION_STATES | {"skipped"}
ACTION_STATE_AUTHORITIES = {"not_applicable", "lms", "user"}
USER_CONFIRMABLE_FIELDS = {
    "status", "title", "kind", "timing.mode", "timing.all_day", "timing.start",
    "timing.end", "timing.end_inclusive", "optional", "location",
    "attendance_required", "details", "action_state",
}
MIGRATED_USER_CONFIRMED_FIELDS = USER_CONFIRMABLE_FIELDS - {"action_state"}
V4_EVENT_AUTHORITIES = load_config().get("legacy_event_authorities", {})
FAILURE_STAGES = {"prepare", "interpret", "commit"}
DECISION_DISPOSITIONS = {"mutate", "no_additional_change"}
DECISION_OPERATIONS = {
    "upsert_event", "cancel_event", "upsert_pending", "resolve_pending",
    "upsert_announcement_application",
}
MANUAL_OPERATIONS = {
    "upsert_event", "cancel_event", "resolve_pending", "upsert_pending",
    "upsert_announcement_application", "set_user_action_state",
}
SECTIONS = (
    ("announcement", "announcements"),
    ("assignment", "assignments"),
    ("quiz", "quizzes"),
    ("discussion", "discussions"),
    ("weekly_learning", "weekly_learning"),
)
DIAGNOSTIC_PREFIXES = (
    "instructor_display_name", "last_reply_at", "position", "read_state",
    "reply_count", "subscribed", "unread_count", "participation.entries_state",
    "participation.last_own_reply_at", "participation.own_entry_count",
    "participation.submission",
)
_SENSITIVE_URL = re.compile(
    r"https?://(?:docs\.google\.com|forms\.gle|open\.kakao\.com)/[^\s\"'<>]+", re.I
)
_ENTRY_CODE = re.compile(r"(입장코드\s+)([^\s,.)]+)", re.I)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        value = _ENTRY_CODE.sub(r"\1[REDACTED]", value)
        return _SENSITIVE_URL.sub(
            lambda match: "[sensitive-url sha256:"
            + hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()[:16]
            + "]",
            value,
        )
    if isinstance(value, list):
        cleaned = [_scrub(item) for item in value]
        return sorted(cleaned, key=_canonical)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _invalid(message: str) -> None:
    raise HylmsError("phase2_state_invalid", message)


def _decision_error(code: str, message: str) -> None:
    raise HylmsError(code, message)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _invalid(f"{label} key가 schema와 일치하지 않습니다.")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{label}은 비어 있지 않은 문자열이어야 합니다.")
    return value


def _string_list(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        _invalid(f"{label}은 문자열 목록이어야 합니다.")
    if nonempty and not value:
        _invalid(f"{label}은 비어 있을 수 없습니다.")
    if len(value) != len(set(value)):
        _invalid(f"{label}에 중복값이 있습니다.")
    return value


def _safe_json(value: Any, label: str = "state") -> None:
    forbidden_keys = {
        "access_code", "answer", "authorization", "body", "comments", "entries", "feedback",
        "feedback_entries",
        "history", "password", "phpsessid", "questions", "token", "user_answer", "xn_api_token",
    }
    forbidden_values = ("example-blocked-code", "docs.google.com", "forms.gle", "open.kakao.com", "phpsessid", "xn_api_token")
    if isinstance(value, dict):
        if any(str(key).lower() in forbidden_keys for key in value):
            _invalid(f"{label}에 저장할 수 없는 필드가 있습니다.")
        for key, item in value.items():
            _safe_json(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _safe_json(item, f"{label}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if any(pattern in lowered for pattern in forbidden_values) or _SENSITIVE_URL.search(value):
            _invalid(f"{label}에 민감값이 있습니다.")
        match = _ENTRY_CODE.search(value)
        if match and match.group(2) != "[REDACTED]":
            _invalid(f"{label}에 입장코드가 있습니다.")
    elif value is not None and not isinstance(value, (bool, int, float)):
        _invalid(f"{label}에 JSON으로 저장할 수 없는 값이 있습니다.")


def _validate_timing(value: Any, label: str = "timing") -> None:
    if not isinstance(value, dict):
        _invalid(f"{label}은 객체여야 합니다.")
    _exact_keys(value, {"mode", "all_day", "start", "end", "end_inclusive"}, label)
    mode = value["mode"]
    if mode not in TIMING_MODES:
        _invalid(f"{label}.mode가 올바르지 않습니다.")
    if not isinstance(value["all_day"], bool) or not isinstance(value["end_inclusive"], bool):
        _invalid(f"{label}의 boolean 필드가 올바르지 않습니다.")
    if value["end"] is None:
        _invalid(f"{label}.end는 필수입니다.")
    if mode != "deadline" and value["start"] is None:
        _invalid(f"{label}.start는 {mode}에서 필수입니다.")
    if value["all_day"] != value["end_inclusive"]:
        _invalid(f"{label}.end_inclusive가 all_day 규칙과 일치하지 않습니다.")

    def parse(raw: Any) -> dt.date | dt.datetime | None:
        if raw is None:
            return None
        if not isinstance(raw, str):
            _invalid(f"{label}의 날짜는 문자열이어야 합니다.")
        try:
            if value["all_day"]:
                if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
                    raise ValueError
                return dt.date.fromisoformat(raw)
            if "T" not in raw:
                raise ValueError
            parsed = dt.datetime.fromisoformat(raw)
            if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(hours=9):
                raise ValueError
            return parsed
        except ValueError:
            _invalid(f"{label}의 날짜 형식이 올바르지 않습니다.")
        return None

    start, end = parse(value["start"]), parse(value["end"])
    if start is not None and end is not None and end < start:
        _invalid(f"{label}.end가 start보다 빠릅니다.")
    if mode == "session" and value["all_day"]:
        _invalid(f"{label}의 session은 시각 일정이어야 합니다.")


def _validate_confirmation_timestamp(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        _invalid(f"{label}은 서울 시각 문자열 또는 null이어야 합니다.")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        _invalid(f"{label}의 시각 형식이 올바르지 않습니다.")
    if (
        "T" not in value
        or parsed.tzinfo is None
        or parsed.utcoffset() != dt.timedelta(hours=9)
        or parsed.isoformat(timespec="seconds") != value
    ):
        _invalid(f"{label}은 서울 시각 ISO 8601이어야 합니다.")


def _validate_event_v4(value: Any) -> None:
    if not isinstance(value, dict):
        _invalid("natural event는 객체여야 합니다.")
    _exact_keys(value, V4_EVENT_KEYS, "v4 natural event")
    for key in ("id", "course_id", "course_name", "title"):
        _required_string(value[key], f"natural event.{key}")
    if value["status"] not in EVENT_STATUSES or value["kind"] not in EVENT_KINDS:
        _invalid("natural event enum이 올바르지 않습니다.")
    if value["action_state"] not in V4_ACTION_STATES:
        _invalid("natural event.action_state가 올바르지 않습니다.")
    if not isinstance(value["optional"], bool):
        _invalid("natural event.optional은 boolean이어야 합니다.")
    if value["location"] is not None and not isinstance(value["location"], str):
        _invalid("natural event.location이 올바르지 않습니다.")
    if value["attendance_required"] is not None and not isinstance(value["attendance_required"], bool):
        _invalid("natural event.attendance_required가 올바르지 않습니다.")
    class_kinds = {"class_replacement", "class_session", "special_event"}
    if (value["kind"] in class_kinds) != (value["action_state"] == "not_applicable"):
        _invalid("natural event의 kind와 action_state가 일치하지 않습니다.")
    _validate_timing(value["timing"], "natural event.timing")
    _string_list(value["source_record_ids"], "natural event.source_record_ids", nonempty=True)
    _string_list(value["evidence"], "natural event.evidence")
    if not isinstance(value["details"], dict):
        _invalid("natural event.details는 객체여야 합니다.")


def _validate_event(value: Any) -> None:
    if not isinstance(value, dict):
        _invalid("natural event는 객체여야 합니다.")
    _exact_keys(value, EVENT_KEYS, "natural event")
    for key in ("id", "course_id", "course_name", "title"):
        _required_string(value[key], f"natural event.{key}")
    if value["status"] not in EVENT_STATUSES or value["kind"] not in EVENT_KINDS:
        _invalid("natural event enum이 올바르지 않습니다.")
    if value["action_state"] not in ACTION_STATES:
        _invalid("natural event.action_state가 올바르지 않습니다.")
    authority = value["action_state_authority"]
    if authority not in ACTION_STATE_AUTHORITIES:
        _invalid("natural event.action_state_authority가 올바르지 않습니다.")
    class_kinds = {"class_replacement", "class_session", "special_event"}
    if value["kind"] in class_kinds:
        if authority != "not_applicable" or value["action_state"] != "not_applicable":
            _invalid("수업 event의 완료 권위와 상태가 올바르지 않습니다.")
    elif authority not in {"lms", "user"} or value["action_state"] == "not_applicable":
        _invalid("행동 event의 완료 권위와 상태가 올바르지 않습니다.")
    if not isinstance(value["optional"], bool):
        _invalid("natural event.optional은 boolean이어야 합니다.")
    if value["location"] is not None and not isinstance(value["location"], str):
        _invalid("natural event.location이 올바르지 않습니다.")
    if value["attendance_required"] is not None and not isinstance(value["attendance_required"], bool):
        _invalid("natural event.attendance_required가 올바르지 않습니다.")
    _validate_timing(value["timing"], "natural event.timing")
    _string_list(value["source_record_ids"], "natural event.source_record_ids", nonempty=True)
    _string_list(value["evidence"], "natural event.evidence")
    if not isinstance(value["details"], dict):
        _invalid("natural event.details는 객체여야 합니다.")
    confirmed = value["user_confirmed"]
    if not isinstance(confirmed, dict):
        _invalid("natural event.user_confirmed는 객체여야 합니다.")
    if any(field not in USER_CONFIRMABLE_FIELDS for field in confirmed):
        _invalid("natural event.user_confirmed에 허용되지 않은 field가 있습니다.")
    if "action_state" in confirmed and authority != "user":
        _invalid("user 권위가 아닌 event는 action_state를 사용자 확정할 수 없습니다.")
    for field, confirmed_at in confirmed.items():
        _validate_confirmation_timestamp(
            confirmed_at, f"natural event.user_confirmed.{field}"
        )


def _validate_pending(value: Any) -> None:
    if not isinstance(value, dict):
        _invalid("pending은 객체여야 합니다.")
    _exact_keys(value, PENDING_KEYS, "pending")
    for key in ("id", "course_id", "course_name", "title", "reason"):
        _required_string(value[key], f"pending.{key}")
    if value["status"] != "pending":
        _invalid("pending.status가 올바르지 않습니다.")
    _string_list(value["source_record_ids"], "pending.source_record_ids", nonempty=True)
    if not isinstance(value["context"], dict):
        _invalid("pending.context는 객체여야 합니다.")


def _empty_application_patch() -> dict[str, Any]:
    return {
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
    }


def _validate_application(value: Any) -> None:
    if not isinstance(value, dict):
        _invalid("공지 반영은 객체여야 합니다.")
    _exact_keys(value, APPLICATION_KEYS, "공지 반영")
    for key in ("id", "course_id", "course_name"):
        _required_string(value[key], f"공지 반영.{key}")
    _string_list(value["source_record_ids"], "공지 반영.source_record_ids", nonempty=True)
    _string_list(value["target_record_ids"], "공지 반영.target_record_ids", nonempty=True)
    _string_list(value["evidence"], "공지 반영.evidence")
    patch = value["patch"]
    if not isinstance(patch, dict):
        _invalid("공지 반영.patch는 객체여야 합니다.")
    _exact_keys(patch, PATCH_KEYS, "공지 반영.patch")
    if patch["timing"] is not None:
        _validate_timing(patch["timing"], "공지 반영.patch.timing")
    for key in (
        "attendance_required", "attendance_status_check_required", "minimum_study_time_required",
        "required_for_all", "optional",
    ):
        if patch[key] is not None and not isinstance(patch[key], bool):
            _invalid(f"공지 반영.patch.{key}가 올바르지 않습니다.")
    weeks = patch["attendance_excluded_weeks"]
    if not isinstance(weeks, list) or any(isinstance(week, bool) or not isinstance(week, int) or not 1 <= week <= 53 for week in weeks) or len(weeks) != len(set(weeks)):
        _invalid("공지 반영.patch.attendance_excluded_weeks가 올바르지 않습니다.")
    speed = patch["maximum_playback_speed"]
    if speed is not None and (isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0):
        _invalid("공지 반영.patch.maximum_playback_speed가 올바르지 않습니다.")
    for key in ("delivery_mode", "location", "consequence"):
        if patch[key] is not None and not isinstance(patch[key], str):
            _invalid(f"공지 반영.patch.{key}가 올바르지 않습니다.")
    _string_list(patch["requirements"], "공지 반영.patch.requirements")
    if not isinstance(patch["details"], dict):
        _invalid("공지 반영.patch.details는 객체여야 합니다.")


def _validate_state_version(
    state: Mapping[str, Any], schema_version: int, event_validator: Any
) -> None:
    if not isinstance(state, dict):
        _invalid("state는 객체여야 합니다.")
    _exact_keys(state, STATE_KEYS, "state")
    if state["schema_version"] != schema_version or state["phase"] != 2:
        _invalid("state version 또는 phase가 올바르지 않습니다.")
    if state["timezone"] != "Asia/Seoul":
        _invalid("state timezone은 Asia/Seoul이어야 합니다.")
    for key in ("term", "last_processed_run_id"):
        _required_string(state[key], f"state.{key}")
    for key in ("natural_events", "pending", "rules", "announcement_applications"):
        if not isinstance(state[key], list):
            _invalid(f"state.{key}는 목록이어야 합니다.")
    for event in state["natural_events"]:
        event_validator(event)
    for item in state["pending"]:
        _validate_pending(item)
    for application in state["announcement_applications"]:
        _validate_application(application)
    for rule in state["rules"]:
        if not isinstance(rule, dict):
            _invalid("rule은 객체여야 합니다.")
        _exact_keys(rule, {"id", "source", "text"}, "rule")
        for key in ("id", "source", "text"):
            _required_string(rule[key], f"rule.{key}")
    failure = state["last_failure"]
    if failure is not None:
        if not isinstance(failure, dict):
            _invalid("last_failure는 객체여야 합니다.")
        _exact_keys(failure, {"run_id", "stage", "code"}, "last_failure")
        _required_string(failure["run_id"], "last_failure.run_id")
        _required_string(failure["code"], "last_failure.code")
        if failure["stage"] not in FAILURE_STAGES:
            _invalid("last_failure.stage가 올바르지 않습니다.")
    ids = [item["id"] for key in ("natural_events", "pending", "rules", "announcement_applications") for item in state[key]]
    if len(ids) != len(set(ids)):
        _invalid("state 전체에 중복 ID가 있습니다.")
    _safe_json(state)


def _validate_state_v4(state: Mapping[str, Any]) -> None:
    _validate_state_version(state, V4_STATE_SCHEMA_VERSION, _validate_event_v4)


def validate_state(state: Mapping[str, Any]) -> None:
    _validate_state_version(state, STATE_SCHEMA_VERSION, _validate_event)


def _validate_state_references_version(
    state: Mapping[str, Any], term_directory: Path, validator: Any
) -> None:
    validator(state)
    if state["term"] != term_directory.name:
        _invalid("state term과 snapshot term이 일치하지 않습니다.")
    runs = discover_runs(term_directory)
    ids = [run["id"] for run in runs]
    if state["last_processed_run_id"] not in ids:
        _invalid("state cursor에 해당하는 snapshot run이 없습니다.")
    cursor = ids.index(state["last_processed_run_id"])
    failure = state["last_failure"]
    if failure is not None:
        failure_id = failure["run_id"]
        if failure_id not in ids or ids.index(failure_id) > cursor + 1:
            _invalid("last_failure.run_id가 cursor 범위를 벗어났습니다.")
    records: dict[str, set[str]] = {}
    for run in runs[: cursor + 1]:
        for course_id, course in _load_run_courses(run).items():
            records.setdefault(course_id, set()).update(
                item["qualified_id"] for item in course["records"].values()
            )
    for collection in ("natural_events", "pending", "announcement_applications"):
        for item in state[collection]:
            known = records.get(item["course_id"], set())
            for record_id in item["source_record_ids"]:
                if record_id not in known:
                    _invalid(f"{item['id']}의 source record를 찾을 수 없습니다.")
            for record_id in item.get("target_record_ids") or []:
                if record_id not in known:
                    _invalid(f"{item['id']}의 target record를 찾을 수 없습니다.")


def _validate_state_v4_references(state: Mapping[str, Any], term_directory: Path) -> None:
    _validate_state_references_version(state, term_directory, _validate_state_v4)


def validate_state_references(state: Mapping[str, Any], term_directory: Path) -> None:
    _validate_state_references_version(state, term_directory, validate_state)


def _take(item: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {key: item.get(key) for key in keys if key in item}


def _safe_submission(item: Mapping[str, Any], section: str) -> dict[str, Any] | None:
    submission = item.get("submission")
    if not isinstance(submission, dict):
        return None
    keys = {
        "assignment": (
            "attempt", "excused", "graded_at", "late", "missing", "seconds_late",
            "submitted_at", "workflow_state",
        ),
        "discussion": (
            "attempt", "excused", "graded_at", "late", "missing", "seconds_late",
            "submitted_at", "workflow_state",
        ),
        "quiz": (
            "attempt", "attempts_remaining", "duration_basis", "duration_seconds", "end_at",
            "finished_at", "id", "late", "missing", "overdue", "overdue_basis",
            "score_state", "started_at", "workflow_state",
        ),
    }[section]
    return _take(submission, keys)


def _public_record_links(item: Mapping[str, Any]) -> dict[str, Any]:
    """Project only the collector's public links, not its submission/body data.

    The raw `links.body` field is a link list. It must not cross the decision/QA
    boundary under a key reserved for private submission bodies.
    """
    links = item.get("links")
    if links is None:
        links = {}
    if not isinstance(links, Mapping):
        _invalid("record links가 객체가 아닙니다.")
    result = {}
    for source, target, fields in (
        ("body", "content_links", ("text", "url")),
        ("attachments", "attachments", ("id", "filename", "content_type", "size", "url")),
    ):
        values = links.get(source)
        if values is None:
            values = []
        if not isinstance(values, list) or any(not isinstance(value, Mapping) for value in values):
            _invalid("record links가 링크 목록이 아닙니다.")
        result[target] = [_take(value, fields) for value in values]
    return result


def _text_projection(section: str, item: Mapping[str, Any]) -> dict[str, Any]:
    if section == "syllabus":
        return _scrub({
            "text": item.get("text"),
            "links": item.get("links") or [],
            "images": item.get("images") or [],
        })
    if section == "announcement":
        message = item.get("message") or {}
        return _scrub({
            "title": item.get("title"),
            "text": message.get("text"),
            "links": message.get("links") or [],
            "images": message.get("images") or [],
            "document_refs": item.get("document_refs") or [],
            "posted_at": item.get("posted_at"),
        })
    if section in {"assignment", "quiz"}:
        description = item.get("description") or {}
        value = {
            "title": item.get("title"),
            "text": description.get("text"),
            "links": description.get("links") or [],
            "images": description.get("images") or [],
        }
        if section == "assignment":
            value["record_links"] = _public_record_links(item)
        return _scrub(value)
    if section == "discussion":
        prompt = item.get("prompt") or {}
        return _scrub({
            "title": item.get("title"),
            "text": prompt.get("text"),
            "links": prompt.get("links") or [],
            "images": prompt.get("images") or [],
            "record_links": _public_record_links(item),
            "posted_at": item.get("posted_at"),
        })
    return _scrub({"title": item.get("title")})


def _structured_projection(
    course_id: str, section: str, item: Mapping[str, Any]
) -> dict[str, Any]:
    if section == "syllabus":
        return {}
    if section == "announcement":
        return _scrub(_take(item, ("author_display_name", "delayed_post_at", "lock_at")))
    if section == "assignment":
        value = _take(item, (
            "access", "allowed_extensions", "assignment_group_id", "attempts", "grading_type",
            "group_assignment", "informational", "omit_from_final_grade", "peer_review",
            "points_possible", "position", "progress", "schedule", "submission_required",
            "submission_types", "external_tool_kind",
        ))
        value["submission"] = _safe_submission(item, section)
        return _scrub(value)
    if section == "quiz":
        value = _take(item, (
            "access", "assignment_group_id", "assignment_id", "attempts", "detail_reason",
            "detail_state", "navigation", "points_possible", "position", "progress",
            "question_count", "quiz_type", "result_visibility", "schedule", "score_state",
            "source_kind", "time_limit_minutes",
        ))
        requirements = item.get("access_requirements") or {}
        value["access_requirements"] = {
            "access_code_required": bool(requirements.get("access_code")),
            "ip_filter": requirements.get("ip_filter"),
            "lockdown_browser": requirements.get("lockdown_browser"),
        }
        value["submission"] = _safe_submission(item, section)
        return _scrub(value)
    if section == "discussion":
        value = _take(item, (
            "access", "assignment_group_id", "assignment_id", "delayed_post_at", "detail_reason",
            "detail_state", "discussion_type", "last_reply_at", "points_possible", "position",
            "published", "read_state", "reply_count", "require_initial_post", "schedule",
            "sections", "subscribed", "unread_count", "user_can_see_posts",
        ))
        value["participation"] = normalize_discussion_participation(course_id, item)
        return _scrub(value)
    return _scrub(_take(item, (
        "access", "attendance", "detail_state", "document_refs", "duration_seconds", "error_code",
        "instructor_display_name", "kind", "linked_entity", "position", "progress", "provider_id",
        "provider_type", "schedule",
    )))


def _record(course: Mapping[str, Any], section: str, record_id: str, item: Mapping[str, Any]) -> dict[str, Any]:
    course_id = str(course["id"])
    qualified = f"{section}:{record_id}" if section != "syllabus" else f"{course_id}/syllabus"
    text = _text_projection(section, item)
    structured = _structured_projection(course_id, section, item)
    return {
        "key": f"{course_id}:{qualified}",
        "course_id": course_id,
        "course": str(course.get("name") or "course"),
        "section": section,
        "record_id": str(record_id),
        "qualified_id": qualified,
        "source_url": item.get("source_url") or (course.get("url") if section == "syllabus" else None),
        "usable": item.get("detail_state") != "unavailable",
        "text": {"fingerprint": _fingerprint(text), "compare": text},
        "structured": {"fingerprint": _fingerprint(structured), "compare": structured},
    }


def discover_runs(term_directory: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    root = term_directory / "runs"
    if not root.exists():
        return runs
    for directory in root.iterdir():
        status_path = directory / "status.json"
        if not directory.is_dir() or directory.name.startswith(".") or not status_path.is_file():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            started_at = dt.datetime.fromisoformat(str(status["started_at"]))
            schema = int(status["schema_version"])
            term_id = str(status["term"]["id"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise HylmsError("snapshot_run_invalid", f"유효하지 않은 snapshot run: {directory.name}") from exc
        if schema not in SUPPORTED_SNAPSHOT_SCHEMAS or term_id != term_directory.name or started_at.tzinfo is None:
            raise HylmsError("snapshot_run_unsupported", f"지원하지 않는 snapshot run: {directory.name}")
        runs.append({"id": directory.name, "path": directory, "status": status, "started_at": started_at})
    return sorted(runs, key=lambda run: (run["started_at"], run["id"]))


def _load_run_courses(run: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    root = Path(run["path"]).resolve()
    courses: dict[str, dict[str, Any]] = {}
    for status_item in run["status"].get("courses") or []:
        course_id = str(status_item["id"])
        relative = Path(str(status_item.get("path") or ""))
        path = (root / relative).resolve()
        if path.parent != root or path.suffix.lower() != ".json":
            raise HylmsError("snapshot_run_invalid", f"안전하지 않은 과목 경로: {run['id']}")
        if not path.exists():
            if status_item.get("status") == "failed":
                courses[course_id] = {"status": status_item, "records": {}}
                continue
            raise HylmsError("snapshot_run_incomplete", f"과목 JSON이 없는 snapshot run: {run['id']}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HylmsError("snapshot_run_invalid", f"과목 JSON을 읽을 수 없습니다: {run['id']}") from exc
        if int(data.get("schema_version") or 0) not in SUPPORTED_SNAPSHOT_SCHEMAS:
            raise HylmsError("snapshot_run_unsupported", f"지원하지 않는 과목 schema: {run['id']}")
        course = data["course"]
        records: dict[str, dict[str, Any]] = {}
        syllabus = data.get("syllabus") or {}
        if str(syllabus.get("text") or "").strip():
            item = _record(course, "syllabus", "syllabus", syllabus)
            records[item["key"]] = item
        for section, key in SECTIONS:
            for raw in data.get(key) or []:
                item = _record(course, section, str(raw["id"]), raw)
                records[item["key"]] = item
        courses[course_id] = {"status": status_item, "records": records}
    return courses


def _logical_records(runs: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    logical: dict[str, dict[str, Any]] = {}
    known_courses: set[str] = set()
    for run in runs:
        for course_id, course in _load_run_courses(run).items():
            status = course["status"].get("status")
            known_courses.add(course_id)
            if status not in {"updated", "updated_with_warnings"}:
                continue
            usable = {key: value for key, value in course["records"].items() if value["usable"]}
            if status == "updated":
                logical = {key: value for key, value in logical.items() if value["course_id"] != course_id}
            logical.update(usable)
    return logical, known_courses


def _changed_paths(old: Any, new: Any, prefix: str = "") -> list[str]:
    if type(old) is not type(new):
        return [prefix or "$" ]
    if isinstance(old, dict):
        paths: list[str] = []
        for key in sorted(set(old) | set(new)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in old or key not in new:
                paths.append(path)
            else:
                paths.extend(_changed_paths(old[key], new[key], path))
        return paths
    if isinstance(old, list):
        return [] if old == new else [prefix or "$"]
    return [] if old == new else [prefix or "$"]


def _diagnostic_path(path: str) -> bool:
    return path.startswith(DIAGNOSTIC_PREFIXES)


def next_run_diff(term_directory: Path, state: Mapping[str, Any]) -> dict[str, Any] | None:
    validate_state_references(state, term_directory)
    runs = discover_runs(term_directory)
    cursor = state.get("last_processed_run_id")
    ids = [run["id"] for run in runs]
    if cursor not in ids:
        raise HylmsError("phase2_cursor_missing", "마지막 처리 snapshot run을 찾을 수 없습니다.")
    index = ids.index(str(cursor))
    if index + 1 == len(runs):
        return None

    previous, known_courses = _logical_records(runs[: index + 1])
    current_run = runs[index + 1]
    current_courses = _load_run_courses(current_run)
    current_ids = set(current_courses)
    added: list[dict[str, Any]] = []
    structured_added: list[dict[str, Any]] = []
    text_modified: list[dict[str, Any]] = []
    structured_modified: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    structured_deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for course_id, course in current_courses.items():
        status = course["status"].get("status")
        if status not in {"updated", "updated_with_warnings"}:
            skipped.append({"course_id": course_id, "status": status})
            continue
        usable = {key: value for key, value in course["records"].items() if value["usable"]}
        for key, item in usable.items():
            before = previous.get(key)
            identity = {name: item[name] for name in ("key", "course_id", "course", "section", "record_id", "qualified_id", "source_url")}
            if before is None:
                added.append({**identity, "text": item["text"]["compare"]})
                structured_added.append({**identity, "structured": item["structured"]["compare"]})
                continue
            if before["text"]["fingerprint"] != item["text"]["fingerprint"]:
                text_modified.append({**identity, "before": before["text"]["compare"], "after": item["text"]["compare"]})
            if before["structured"]["fingerprint"] != item["structured"]["fingerprint"]:
                paths = _changed_paths(before["structured"]["compare"], item["structured"]["compare"])
                diagnostic = [path for path in paths if _diagnostic_path(path)]
                actionable = [path for path in paths if path not in diagnostic]
                structured_modified.append({
                    **identity,
                    "actionable_paths": actionable,
                    "diagnostic_paths": diagnostic,
                    "before": before["structured"]["compare"],
                    "after": item["structured"]["compare"],
                })
        if status == "updated":
            for key, item in previous.items():
                if item["course_id"] == course_id and key not in course["records"]:
                    identity = {name: item[name] for name in ("key", "course_id", "course", "section", "record_id", "qualified_id", "source_url")}
                    deleted.append({**identity, "text": item["text"]["compare"]})
                    structured_deleted.append(identity)

    return {
        "previous_run_id": str(cursor),
        "current_run_id": current_run["id"],
        "current_started_at": current_run["status"]["started_at"],
        "current_overall_status": current_run["status"].get("overall_status"),
        "course_added": sorted(current_ids - known_courses),
        "course_missing": sorted(known_courses - current_ids),
        "counts": {
            "baseline_records": len(previous),
            "current_records": sum(len(course["records"]) for course in current_courses.values()),
            "added": len(added),
            "text_modified": len(text_modified),
            "structured_modified": len(structured_modified),
            "diagnostic_only": sum(not item["actionable_paths"] for item in structured_modified),
            "deleted": len(deleted),
        },
        "added": added,
        "structured_added": structured_added,
        "text_modified": text_modified,
        "structured_modified": structured_modified,
        "deleted": deleted,
        "structured_deleted": structured_deleted,
        "skipped_courses": skipped,
    }


def _decision_changes(diff: Mapping[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    specifications = (
        ("added", "added", None, "text"),
        ("text_modified", "text_modified", "before", "after"),
        ("deleted", "deleted", "text", None),
    )
    for change_type, collection, before_key, after_key in specifications:
        for item in diff[collection]:
            identity = {
                "previous_run_id": diff["previous_run_id"],
                "current_run_id": diff["current_run_id"],
                "type": change_type,
                "course_id": item["course_id"],
                "section": item["section"],
                "record_id": item["record_id"],
            }
            changes.append({
                "id": f"change:{_fingerprint(identity)}",
                "type": change_type,
                "course_id": item["course_id"],
                "course_name": item["course"],
                "section": item["section"],
                "record_id": item["record_id"],
                "source_record_id": item["qualified_id"],
                "before": copy.deepcopy(item[before_key]) if before_key else None,
                "after": copy.deepcopy(item[after_key]) if after_key else None,
            })
    return changes


def prepare_decision_packet(
    term_directory: Path,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Bind model-visible text changes to one exact state and adjacent run pair."""
    validate_state_references(state, term_directory)
    diff = next_run_diff(term_directory, state)
    if diff is None:
        return None
    transaction = {
        "term": state["term"],
        "previous_run_id": diff["previous_run_id"],
        "current_run_id": diff["current_run_id"],
        "base_state_sha256": _fingerprint(state),
        "diff_sha256": _fingerprint(diff),
    }
    transaction["id"] = _fingerprint(transaction)
    changes = _decision_changes(diff)
    change_ids = [item["id"] for item in changes]
    if len(change_ids) != len(set(change_ids)):
        _decision_error("phase2_decision_invalid", "diff에 중복 change ID가 있습니다.")
    packet = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "transaction": transaction,
        "changes": changes,
    }
    try:
        _safe_json(packet, "decision packet")
    except HylmsError as exc:
        raise HylmsError("phase2_decision_invalid", exc.message) from exc
    return packet


def _migrate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    legacy_keys = {
        "id", "status", "course_id", "course", "title", "kind", "timing_mode", "start", "end",
        "date", "start_date", "end_date_inclusive", "all_day", "optional", "action_state",
        "location", "attendance_required", "source_record_ids", "evidence",
    }
    kind = {
        "zoom_replacement": "class_replacement",
        "class_replacement": "class_replacement",
        "normal_ot_confirmation": "class_session",
        "action_deadline": "action",
        "required_activity_period": "activity",
        "submission_period": "submission",
        "application": "application",
    }.get(str(event.get("kind")))
    if kind is None:
        _invalid(f"migration할 수 없는 event kind: {event.get('kind')}")
    all_day = bool(event.get("all_day"))
    start = event.get("start") or event.get("start_date")
    end = event.get("end") or event.get("end_date_inclusive") or event.get("date")
    if event.get("date") is not None:
        start = None
    evidence = event.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    action_state = event.get("action_state")
    if action_state is None:
        action_state = "not_applicable" if kind in {"class_replacement", "class_session", "special_event"} else "unknown"
    migrated = {
        "id": event["id"],
        "status": "cancelled" if event.get("status") == "cancelled" else "active",
        "course_id": str(event["course_id"]),
        "course_name": event["course"],
        "title": event["title"],
        "kind": kind,
        "timing": {
            "mode": event["timing_mode"],
            "all_day": all_day,
            "start": start,
            "end": end,
            "end_inclusive": all_day,
        },
        "optional": bool(event.get("optional", False)),
        "action_state": action_state,
        "location": event.get("location"),
        "attendance_required": event.get("attendance_required"),
        "source_record_ids": copy.deepcopy(event.get("source_record_ids") or []),
        "evidence": copy.deepcopy(evidence),
        "details": {key: copy.deepcopy(value) for key, value in event.items() if key not in legacy_keys},
    }
    _validate_event_v4(migrated)
    return migrated


def _migrate_pending(item: Mapping[str, Any]) -> dict[str, Any]:
    legacy_keys = {"id", "status", "course_id", "course", "title", "source_record_ids", "reason"}
    title = item.get("title")
    if not title:
        title = item.get("reason") or "Legacy pending item"
    migrated = {
        "id": item["id"],
        "status": "pending",
        "course_id": str(item["course_id"]),
        "course_name": item["course"],
        "title": title,
        "source_record_ids": copy.deepcopy(item.get("source_record_ids") or []),
        "reason": item["reason"],
        "context": {key: copy.deepcopy(value) for key, value in item.items() if key not in legacy_keys},
    }
    _validate_pending(migrated)
    return migrated


def _range_timing(value: str, mode: str) -> dict[str, Any]:
    try:
        start, end = value.split("~", 1)
    except ValueError as exc:
        raise HylmsError("phase2_state_invalid", "공지 반영 기간을 migration할 수 없습니다.") from exc
    timing = {"mode": mode, "all_day": False, "start": start, "end": end, "end_inclusive": False}
    _validate_timing(timing, "공지 반영.patch.timing")
    return timing


def _migrate_application(item: Mapping[str, Any]) -> dict[str, Any]:
    values = copy.deepcopy(item.get("values") or {})
    patch = _empty_application_patch()
    if values.get("session"):
        patch["timing"] = _range_timing(values.pop("session"), "session")
    elif values.get("course_period"):
        patch["timing"] = _range_timing(values.pop("course_period"), "period")
    elif values.get("due_at"):
        patch["timing"] = {
            "mode": values.pop("timing_mode", "deadline"),
            "all_day": False,
            "start": values.pop("opens_at", None),
            "end": values.pop("due_at"),
            "end_inclusive": False,
        }
    mapping = {
        "attendance_required": "attendance_required",
        "attendance_excluded_weeks": "attendance_excluded_weeks",
        "attendance_status_check_required": "attendance_status_check_required",
        "minimum_study_time_required": "minimum_study_time_required",
        "mode": "delivery_mode",
        "maximum_playback_speed": "maximum_playback_speed",
        "required_for_all": "required_for_all",
        "optional": "optional",
        "location": "location",
        "requirements": "requirements",
        "consequence": "consequence",
    }
    for old, new in mapping.items():
        if old in values:
            patch[new] = values.pop(old)
    patch["details"] = values
    sources = item.get("source_record_ids") or []
    if item.get("source_record_id"):
        sources = [item["source_record_id"], *sources]
    evidence = [item["note"]] if item.get("note") else []
    migrated = {
        "id": item["application_id"],
        "course_id": str(item["course_id"]),
        "course_name": item["course"],
        "source_record_ids": list(dict.fromkeys(sources)),
        "target_record_ids": copy.deepcopy(item.get("target_record_ids") or []),
        "patch": patch,
        "evidence": evidence,
    }
    _validate_application(migrated)
    return migrated


def _migrated_state(state: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "phase", "term", "last_processed_run_id", "natural_events", "pending",
        "rules", "announcement_applications", "last_failure",
    }
    _exact_keys(state, expected, "v3 state")
    if state.get("schema_version") != 3 or state.get("phase") != 2 or state.get("last_failure") is not None:
        _invalid("v3 state를 안전하게 migration할 수 없습니다.")
    return {
        "schema_version": V4_STATE_SCHEMA_VERSION,
        "phase": 2,
        "term": state["term"],
        "timezone": "Asia/Seoul",
        "last_processed_run_id": state["last_processed_run_id"],
        "natural_events": [_migrate_event(item) for item in state["natural_events"]],
        "pending": [_migrate_pending(item) for item in state["pending"]],
        "rules": copy.deepcopy(state["rules"]),
        "announcement_applications": [
            _migrate_application(item) for item in state["announcement_applications"]
        ],
        "last_failure": None,
    }


def _write_state_if_changed(path: Path, state: Mapping[str, Any]) -> None:
    serialized = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        if path.read_text(encoding="utf-8") == serialized:
            return
    except FileNotFoundError:
        pass
    atomic_write_json(path, state)


def migrate_state_v3_to_v4(path: Path, term_directory: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HylmsError("phase2_state_invalid", "Phase 2 state를 읽을 수 없습니다.") from exc
    if state.get("schema_version") == V4_STATE_SCHEMA_VERSION:
        _validate_state_v4_references(state, term_directory)
        return state
    try:
        migrated = _migrated_state(state)
    except HylmsError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise HylmsError("phase2_state_invalid", "v3 state를 migration할 수 없습니다.") from exc
    _validate_state_v4_references(migrated, term_directory)
    _write_state_if_changed(path, migrated)
    return migrated


def _migrate_event_v4_to_v5(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_v4(event)
    event_id = str(event["id"])
    authority = V4_EVENT_AUTHORITIES.get(event_id)
    if authority is None:
        _invalid(f"완료 권위를 알 수 없는 v4 event입니다: {event_id}")
    migrated = copy.deepcopy(dict(event))
    migrated["action_state_authority"] = authority
    if authority == "not_applicable":
        migrated["action_state"] = "not_applicable"
    elif migrated["action_state"] == "skipped":
        migrated["action_state"] = "unknown"
    migrated["user_confirmed"] = {
        field: None for field in sorted(MIGRATED_USER_CONFIRMED_FIELDS)
    }
    _validate_event(migrated)
    return migrated


def _migrated_state_v5(state: Mapping[str, Any]) -> dict[str, Any]:
    _validate_state_v4(state)
    migrated = copy.deepcopy(dict(state))
    migrated["schema_version"] = STATE_SCHEMA_VERSION
    migrated["natural_events"] = [
        _migrate_event_v4_to_v5(event) for event in state["natural_events"]
    ]
    validate_state(migrated)
    return migrated


def migrate_state_v4_to_v5(path: Path, term_directory: Path) -> dict[str, Any]:
    """Atomically migrate the approved Phase 2 baseline to state schema v5."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HylmsError("phase2_state_invalid", "Phase 2 state를 읽을 수 없습니다.") from exc
    if state.get("schema_version") == STATE_SCHEMA_VERSION:
        validate_state_references(state, term_directory)
        return state
    try:
        _validate_state_v4_references(state, term_directory)
        migrated = _migrated_state_v5(state)
        validate_state_references(migrated, term_directory)
    except HylmsError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise HylmsError("phase2_state_invalid", "v4 state를 migration할 수 없습니다.") from exc
    _write_state_if_changed(path, migrated)
    return migrated


def _compact_state(state: Mapping[str, Any], last_processed_run_id: str) -> dict[str, Any]:
    validate_state(state)
    compacted = copy.deepcopy(dict(state))
    compacted["last_processed_run_id"] = last_processed_run_id
    compacted["last_failure"] = None
    validate_state(compacted)
    return compacted


def _decision_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _decision_error("phase2_decision_invalid", f"{label} key가 schema와 일치하지 않습니다.")
    return value


def _decision_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _decision_error("phase2_decision_invalid", f"{label}은 비어 있지 않은 문자열이어야 합니다.")
    return value


def _read_committed_state(path: Path, term_directory: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HylmsError(
            "phase2_decision_stale", "현재 Phase 2 state를 안전하게 읽을 수 없습니다."
        ) from exc
    try:
        validate_state_references(value, term_directory)
    except HylmsError as exc:
        raise HylmsError("phase2_decision_stale", "현재 Phase 2 state가 유효하지 않습니다.") from exc
    return value


def _validate_decision_result(
    value: Any,
    prepared: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    result = _decision_exact_keys(
        value, {"schema_version", "transaction_id", "decisions"}, "decision result"
    )
    if result["schema_version"] != DECISION_SCHEMA_VERSION:
        _decision_error("phase2_decision_invalid", "decision schema version이 올바르지 않습니다.")
    _decision_string(result["transaction_id"], "decision result.transaction_id")
    if result["transaction_id"] != prepared["transaction"]["id"]:
        _decision_error("phase2_decision_stale", "decision transaction이 현재 diff와 일치하지 않습니다.")
    if not isinstance(result["decisions"], list):
        _decision_error("phase2_decision_invalid", "decision result.decisions는 목록이어야 합니다.")

    expected = {item["id"]: item for item in prepared["changes"]}
    decisions: dict[str, dict[str, Any]] = {}
    for raw in result["decisions"]:
        decision = _decision_exact_keys(
            raw, {"change_id", "disposition", "reason", "operations"}, "decision"
        )
        change_id = _decision_string(decision["change_id"], "decision.change_id")
        _decision_string(decision["reason"], "decision.reason")
        if change_id not in expected:
            _decision_error("phase2_decision_invalid", "등록되지 않은 change ID가 있습니다.")
        if change_id in decisions:
            _decision_error("phase2_decision_invalid", "같은 change가 두 번 결정되었습니다.")
        if decision["disposition"] not in DECISION_DISPOSITIONS:
            _decision_error("phase2_decision_invalid", "decision disposition이 올바르지 않습니다.")
        if not isinstance(decision["operations"], list):
            _decision_error("phase2_decision_invalid", "decision.operations는 목록이어야 합니다.")
        if decision["disposition"] == "mutate" and not decision["operations"]:
            _decision_error("phase2_decision_invalid", "mutate decision에는 operation이 필요합니다.")
        if decision["disposition"] == "no_additional_change" and decision["operations"]:
            _decision_error(
                "phase2_decision_invalid", "no_additional_change에는 operation을 넣을 수 없습니다."
            )
        decisions[change_id] = decision
    if set(decisions) != set(expected):
        _decision_error("phase2_decision_invalid", "모든 text change를 정확히 한 번 결정해야 합니다.")
    try:
        _safe_json(result, "decision result")
    except HylmsError as exc:
        raise HylmsError("phase2_decision_invalid", exc.message) from exc
    return decisions



def validate_decision_submission(value: Any, prepared: Mapping[str, Any]) -> None:
    """Read-only format validation; preview and independent QA remain mandatory."""
    decisions = _validate_decision_result(value, prepared)
    validators = {"upsert_event": _validate_event, "upsert_pending": _validate_pending,
                  "upsert_announcement_application": _validate_application}
    for decision in decisions.values():
        for operation in decision["operations"]:
            if not isinstance(operation, dict) or operation.get("op") not in DECISION_OPERATIONS:
                _decision_error("phase2_decision_invalid", "unsupported_operation")
            kind = operation["op"]
            if kind in validators:
                _decision_exact_keys(operation, {"op", "value"}, kind)
                _validate_operation_value(validators[kind], operation["value"], kind + ".value")
            elif kind == "resolve_pending":
                _decision_exact_keys(operation, {"op", "id"}, kind)
                _decision_string(operation["id"], kind + ".id")
            else:
                _decision_exact_keys(operation, {"op", "id", "source_record_ids", "evidence"}, kind)
                _decision_string(operation["id"], kind + ".id")
                _validate_operation_value(lambda v: _string_list(v, kind + ".source_record_ids", nonempty=True),
                                          operation["source_record_ids"], kind + ".source_record_ids")
                _validate_operation_value(lambda v: _string_list(v, kind + ".evidence"),
                                          operation["evidence"], kind + ".evidence")


def decision_format_diagnostic(value: Any) -> dict[str, Any]:
    """Return schema-only labels, never echo provider text or arbitrary keys."""
    if isinstance(value, dict) and isinstance(value.get("decisions"), list):
        for di, decision in enumerate(value["decisions"]):
            operations = decision.get("operations") if isinstance(decision, dict) else None
            for oi, operation in enumerate(operations if isinstance(operations, list) else []):
                if not isinstance(operation, dict) or operation.get("op") != "upsert_announcement_application":
                    continue
                entity = operation.get("value")
                patch = entity.get("patch") if isinstance(entity, dict) else None
                if isinstance(patch, dict) and set(patch) != PATCH_KEYS:
                    return {"reason": "application_patch_keys_mismatch",
                            "field_path": f"decisions[{di}].operations[{oi}].value.patch",
                            "missing_keys": sorted(PATCH_KEYS - set(patch)),
                            "unexpected_key_count": len(set(patch) - PATCH_KEYS)}
    return {"reason": "decision_schema_invalid"}


def _state_entity_locations(state: Mapping[str, Any]) -> dict[str, str]:
    locations: dict[str, str] = {}
    for collection in ("natural_events", "pending", "rules", "announcement_applications"):
        for item in state[collection]:
            locations[item["id"]] = collection
    return locations


def _upsert_entity(collection: list[dict[str, Any]], value: Mapping[str, Any]) -> bool:
    copied = copy.deepcopy(dict(value))
    for index, existing in enumerate(collection):
        if existing["id"] == copied["id"]:
            collection[index] = copied
            return False
    collection.append(copied)
    return True


def _validate_operation_value(validator: Any, value: Any, label: str) -> dict[str, Any]:
    try:
        validator(value)
    except HylmsError as exc:
        raise HylmsError("phase2_decision_invalid", f"{label}: {exc.message}") from exc
    return value


def _event_field_value(event: Mapping[str, Any], field: str) -> Any:
    if field.startswith("timing."):
        return event["timing"][field.split(".", 1)[1]]
    return event[field]


def _set_event_field(event: dict[str, Any], field: str, value: Any) -> None:
    if field.startswith("timing."):
        event["timing"][field.split(".", 1)[1]] = copy.deepcopy(value)
    else:
        event[field] = copy.deepcopy(value)


def _upsert_conflict_pending(
    candidate: dict[str, Any],
    event: Mapping[str, Any],
    field: str,
    proposed_value: Any,
    change: Mapping[str, Any],
    transaction: Mapping[str, Any],
    source_record_ids: list[str],
    evidence: list[str],
) -> tuple[str, bool]:
    pending_id = f"{event['id']}:conflict:{field}"
    existing = next((item for item in candidate["pending"] if item["id"] == pending_id), None)
    if existing is not None and (
        existing["course_id"] != event["course_id"]
        or (existing.get("context") or {}).get("event_id") != event["id"]
        or (existing.get("context") or {}).get("field") != field
    ):
        _decision_error("phase2_decision_conflict", "기존 conflict pending identity가 다릅니다.")
    current_value = copy.deepcopy(_event_field_value(event, field))
    observations = copy.deepcopy((existing or {}).get("context", {}).get("candidates") or [])
    proposed_key = _canonical(proposed_value)
    observation = next(
        (item for item in observations if _canonical(item.get("value")) == proposed_key), None
    )
    if observation is None:
        observations.append(
            {
                "value": copy.deepcopy(proposed_value),
                "source_record_ids": list(dict.fromkeys(source_record_ids)),
                "evidence": list(dict.fromkeys(evidence)),
                "first_seen_run_id": transaction["current_run_id"],
                "last_seen_run_id": transaction["current_run_id"],
            }
        )
    else:
        observation["source_record_ids"] = list(dict.fromkeys([
            *observation.get("source_record_ids", []), *source_record_ids,
        ]))
        observation["evidence"] = list(dict.fromkeys([
            *observation.get("evidence", []), *evidence,
        ]))
        observation["last_seen_run_id"] = transaction["current_run_id"]
    observations.sort(key=lambda item: _canonical(item["value"]))
    all_sources = list(dict.fromkeys([
        *((existing or {}).get("source_record_ids") or []), *source_record_ids,
    ]))
    pending = {
        "id": pending_id,
        "status": "pending",
        "course_id": event["course_id"],
        "course_name": event["course_name"],
        "title": f"사용자 확정값 충돌: {event['title']} · {field}",
        "source_record_ids": all_sources,
        "reason": "새 LMS 근거가 사용자 확정값과 달라 기존 값을 유지합니다.",
        "context": {
            "kind": "user_confirmed_conflict",
            "event_id": event["id"],
            "field": field,
            "current_value": current_value,
            "current_confirmed_at": event["user_confirmed"].get(field),
            "candidates": observations,
            "first_seen_run_id": (existing or {}).get("context", {}).get(
                "first_seen_run_id", transaction["current_run_id"]
            ),
            "last_seen_run_id": transaction["current_run_id"],
            "change_id": change["id"],
        },
    }
    _validate_pending(pending)
    added = _upsert_entity(candidate["pending"], pending)
    return pending_id, added


def _protect_automatic_event_operation(
    candidate: dict[str, Any],
    operation: Mapping[str, Any],
    change: Mapping[str, Any],
    transaction: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[tuple[str, bool]]]:
    operation_type = operation.get("op")
    if operation_type not in {"upsert_event", "cancel_event"}:
        return copy.deepcopy(dict(operation)), []
    event_id = operation.get("id") if operation_type == "cancel_event" else (
        operation.get("value") or {}
    ).get("id")
    existing = next(
        (item for item in candidate["natural_events"] if item["id"] == event_id), None
    )
    if existing is None:
        if operation_type == "upsert_event":
            value = operation.get("value")
            if not isinstance(value, dict) or value.get("user_confirmed") != {}:
                _decision_error(
                    "phase2_decision_conflict",
                    "자동으로 추가하는 event의 user_confirmed는 비어 있어야 합니다.",
                )
        return copy.deepcopy(dict(operation)), []

    if operation_type == "cancel_event":
        if "status" not in existing["user_confirmed"]:
            return copy.deepcopy(dict(operation)), []
        source_ids = operation.get("source_record_ids")
        evidence = operation.get("evidence")
        try:
            source_ids = _string_list(
                source_ids, "cancel_event.source_record_ids", nonempty=True
            )
            evidence = _string_list(evidence, "cancel_event.evidence")
        except HylmsError as exc:
            raise HylmsError("phase2_decision_invalid", exc.message) from exc
        if existing["course_id"] != change["course_id"]:
            _decision_error("phase2_decision_conflict", "다른 과목의 event를 취소할 수 없습니다.")
        if change["source_record_id"] not in source_ids:
            _decision_error("phase2_decision_conflict", "event 취소 근거가 change와 연결되지 않았습니다.")
        pending = _upsert_conflict_pending(
            candidate, existing, "status", "cancelled", change, transaction,
            source_ids, evidence,
        )
        return None, [pending]

    value = operation.get("value")
    if not isinstance(value, dict) or set(value) != EVENT_KEYS:
        _decision_error("phase2_decision_invalid", "upsert_event.value key가 올바르지 않습니다.")
    if value.get("course_id") != change["course_id"]:
        _decision_error("phase2_decision_conflict", "operation과 change의 과목이 일치하지 않습니다.")
    if change["source_record_id"] not in (value.get("source_record_ids") or []):
        _decision_error("phase2_decision_conflict", "operation source가 change와 연결되지 않았습니다.")
    if value["action_state_authority"] != existing["action_state_authority"]:
        _decision_error("phase2_decision_conflict", "기존 event의 완료 권위를 변경할 수 없습니다.")
    if value["user_confirmed"] != existing["user_confirmed"]:
        _decision_error("phase2_decision_conflict", "자동 decision은 사용자 확정 근거를 변경할 수 없습니다.")
    if value["action_state"] != existing["action_state"]:
        _decision_error("phase2_decision_conflict", "자동 event upsert는 action_state를 직접 변경할 수 없습니다.")
    merged = copy.deepcopy(value)
    merged["source_record_ids"] = list(dict.fromkeys([
        *existing["source_record_ids"], *value["source_record_ids"],
    ]))
    merged["evidence"] = list(dict.fromkeys([*existing["evidence"], *value["evidence"]]))
    conflicts: list[tuple[str, bool]] = []
    for field in sorted(USER_CONFIRMABLE_FIELDS - {"action_state"}):
        before = _event_field_value(existing, field)
        after = _event_field_value(value, field)
        if before == after or field not in existing["user_confirmed"]:
            continue
        conflicts.append(
            _upsert_conflict_pending(
                candidate, existing, field, after, change, transaction,
                value["source_record_ids"], value["evidence"],
            )
        )
        _set_event_field(merged, field, before)
    merged["action_state_authority"] = existing["action_state_authority"]
    merged["action_state"] = existing["action_state"]
    merged["user_confirmed"] = copy.deepcopy(existing["user_confirmed"])
    return {"op": "upsert_event", "value": merged}, conflicts


def _apply_operation(
    candidate: dict[str, Any],
    operation: Any,
    change: Mapping[str, Any],
    entity_locations: Mapping[str, str],
) -> tuple[str, str, bool]:
    if not isinstance(operation, dict) or operation.get("op") not in DECISION_OPERATIONS:
        _decision_error("phase2_decision_invalid", "허용되지 않은 decision operation입니다.")
    operation_type = operation["op"]
    if change["type"] == "deleted":
        _decision_error(
            "phase2_decision_conflict", "삭제 change는 직접 state를 변경할 수 없습니다."
        )

    if operation_type == "upsert_event":
        _decision_exact_keys(operation, {"op", "value"}, "upsert_event")
        value = _validate_operation_value(_validate_event, operation["value"], "upsert_event.value")
        collection = "natural_events"
    elif operation_type == "upsert_pending":
        _decision_exact_keys(operation, {"op", "value"}, "upsert_pending")
        value = _validate_operation_value(
            _validate_pending, operation["value"], "upsert_pending.value"
        )
        collection = "pending"
    elif operation_type == "upsert_announcement_application":
        _decision_exact_keys(
            operation, {"op", "value"}, "upsert_announcement_application"
        )
        value = _validate_operation_value(
            _validate_application,
            operation["value"],
            "upsert_announcement_application.value",
        )
        collection = "announcement_applications"
    elif operation_type == "cancel_event":
        _decision_exact_keys(
            operation, {"op", "id", "source_record_ids", "evidence"}, "cancel_event"
        )
        event_id = _decision_string(operation["id"], "cancel_event.id")
        try:
            source_record_ids = _string_list(
                operation["source_record_ids"], "cancel_event.source_record_ids", nonempty=True
            )
            evidence = _string_list(operation["evidence"], "cancel_event.evidence")
        except HylmsError as exc:
            raise HylmsError("phase2_decision_invalid", exc.message) from exc
        existing = next(
            (item for item in candidate["natural_events"] if item["id"] == event_id), None
        )
        if existing is None:
            _decision_error("phase2_decision_conflict", "취소할 event가 존재하지 않습니다.")
        if existing["course_id"] != change["course_id"]:
            _decision_error("phase2_decision_conflict", "다른 과목의 event를 취소할 수 없습니다.")
        if change["source_record_id"] not in source_record_ids:
            _decision_error("phase2_decision_conflict", "event 취소 근거가 change와 연결되지 않았습니다.")
        value = copy.deepcopy(existing)
        value["status"] = "cancelled"
        value["source_record_ids"] = list(dict.fromkeys([
            *value["source_record_ids"], *source_record_ids,
        ]))
        value["evidence"] = list(dict.fromkeys([*value["evidence"], *evidence]))
        collection = "natural_events"
    else:
        _decision_exact_keys(operation, {"op", "id"}, "resolve_pending")
        pending_id = _decision_string(operation["id"], "resolve_pending.id")
        existing = next((item for item in candidate["pending"] if item["id"] == pending_id), None)
        if existing is None:
            _decision_error("phase2_decision_conflict", "해결할 pending이 존재하지 않습니다.")
        if existing["course_id"] != change["course_id"]:
            _decision_error("phase2_decision_conflict", "다른 과목의 pending을 해결할 수 없습니다.")
        candidate["pending"] = [item for item in candidate["pending"] if item["id"] != pending_id]
        return "pending", pending_id, False

    entity_id = value["id"]
    current_location = entity_locations.get(entity_id)
    if current_location is not None and current_location != collection:
        _decision_error("phase2_decision_conflict", "기존 ID의 entity 종류를 변경할 수 없습니다.")
    existing = next((item for item in candidate[collection] if item["id"] == entity_id), None)
    if existing is not None and existing["course_id"] != value["course_id"]:
        _decision_error("phase2_decision_conflict", "기존 ID를 다른 과목으로 이전할 수 없습니다.")
    if value["course_id"] != change["course_id"]:
        _decision_error("phase2_decision_conflict", "operation과 change의 과목이 일치하지 않습니다.")
    if change["source_record_id"] not in value["source_record_ids"]:
        _decision_error("phase2_decision_conflict", "operation source가 change와 연결되지 않았습니다.")
    if operation_type == "upsert_event" and value["status"] == "cancelled":
        _decision_error("phase2_decision_conflict", "event 취소에는 cancel_event를 사용해야 합니다.")
    added = _upsert_entity(candidate[collection], value)
    return collection, entity_id, added


def _deleted_source_pending(
    candidate: dict[str, Any],
    base_state: Mapping[str, Any],
    change: Mapping[str, Any],
    transaction: Mapping[str, Any],
    operation_targets: set[str],
) -> tuple[str | None, bool]:
    source_record_id = change["source_record_id"]
    linked_events = sorted(
        item["id"] for item in base_state["natural_events"]
        if source_record_id in item["source_record_ids"]
    )
    linked_applications = sorted(
        item["id"] for item in base_state["announcement_applications"]
        if source_record_id in item["source_record_ids"]
    )
    if not linked_events and not linked_applications:
        return None, False

    pending_id = f"{change['course_id']}:{source_record_id}:source-removed"
    if pending_id in operation_targets:
        _decision_error(
            "phase2_decision_conflict", "자동 source 삭제 pending과 같은 ID를 변경할 수 없습니다."
        )
    locations = _state_entity_locations(candidate)
    if pending_id in locations and locations[pending_id] != "pending":
        _decision_error("phase2_decision_conflict", "자동 pending ID가 기존 entity와 충돌합니다.")
    existing = next((item for item in candidate["pending"] if item["id"] == pending_id), None)
    if existing is not None and (
        existing["course_id"] != change["course_id"]
        or source_record_id not in existing["source_record_ids"]
    ):
        _decision_error("phase2_decision_conflict", "기존 자동 pending의 identity가 다릅니다.")

    before = change.get("before") or {}
    record_title = before.get("title") if isinstance(before, dict) else None
    title = f"삭제된 원본 확인: {record_title or source_record_id}"
    existing_context = (existing or {}).get("context") or {}
    pending = {
        "id": pending_id,
        "status": "pending",
        "course_id": change["course_id"],
        "course_name": change["course_name"],
        "title": title,
        "source_record_ids": [source_record_id],
        "reason": "연결된 source record가 최신 정상 snapshot에서 삭제되어 확인이 필요합니다.",
        "context": {
            "kind": "source_removed",
            "first_seen_run_id": existing_context.get(
                "first_seen_run_id", transaction["current_run_id"]
            ),
            "last_seen_run_id": transaction["current_run_id"],
            "linked_event_ids": sorted(set([
                *existing_context.get("linked_event_ids", []), *linked_events,
            ])),
            "linked_announcement_application_ids": sorted(set([
                *existing_context.get("linked_announcement_application_ids", []),
                *linked_applications,
            ])),
        },
    }
    _validate_operation_value(_validate_pending, pending, "자동 source 삭제 pending")
    added = _upsert_entity(candidate["pending"], pending)
    return pending_id, added


def _lms_record_action_state(record: Mapping[str, Any]) -> str | None:
    section = record.get("section")
    structured = (record.get("structured") or {}).get("compare") or {}
    if section == "assignment":
        progress = structured.get("progress") or {}
        submission = structured.get("submission") or {}
        return "done" if (
            progress.get("submitted") is True
            or submission.get("workflow_state") in {"submitted", "graded"}
        ) else "unknown"
    if section == "quiz":
        submission = structured.get("submission") or {}
        progress = structured.get("progress") or {}
        workflow = submission.get("workflow_state") or progress.get("workflow_state")
        return "done" if workflow in {"complete", "submitted", "graded"} else "unknown"
    if section == "discussion":
        participation = structured.get("participation") or {}
        return "done" if participation.get("state") == "participated" else "unknown"
    if section == "weekly_learning":
        progress = structured.get("progress") or {}
        attendance = structured.get("attendance") or {}
        return "done" if (
            progress.get("completed") is True
            or attendance.get("status") in {"present", "late"}
        ) else "unknown"
    return None


def _reconcile_lms_action_states(
    candidate: dict[str, Any], term_directory: Path, current_run_id: str
) -> list[str]:
    runs = discover_runs(term_directory)
    ids = [run["id"] for run in runs]
    if current_run_id not in ids:
        _decision_error("phase2_decision_stale", "LMS 완료 상태의 current run이 없습니다.")
    records, _ = _logical_records(runs[: ids.index(current_run_id) + 1])
    changed: list[str] = []
    for event in candidate["natural_events"]:
        if event["action_state_authority"] != "lms":
            continue
        states: list[str] = []
        missing_authoritative_source = False
        for source_record_id in event["source_record_ids"]:
            section = source_record_id.split(":", 1)[0]
            record = records.get(f"{event['course_id']}:{source_record_id}")
            if record is None:
                if section in {"assignment", "quiz", "discussion", "weekly_learning"}:
                    missing_authoritative_source = True
                continue
            state = _lms_record_action_state(record)
            if state is not None:
                states.append(state)
        if missing_authoritative_source or not states:
            continue
        action_state = "done" if all(state == "done" for state in states) else "unknown"
        if event["action_state"] != action_state:
            event["action_state"] = action_state
            changed.append(event["id"])
    return changed


def _semantic_collections(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: state[key]
        for key in ("natural_events", "pending", "rules", "announcement_applications")
    }


def _changed_entities(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    collections = ("natural_events", "pending", "announcement_applications")
    before_by_id = {
        item["id"]: item for collection in collections for item in before[collection]
    }
    after_by_id = {
        item["id"]: item for collection in collections for item in after[collection]
    }
    changed_ids = sorted(
        entity_id for entity_id in set(before_by_id) | set(after_by_id)
        if before_by_id.get(entity_id) != after_by_id.get(entity_id)
    )
    return (
        [copy.deepcopy(before_by_id[entity_id]) for entity_id in changed_ids if entity_id in before_by_id],
        [copy.deepcopy(after_by_id[entity_id]) for entity_id in changed_ids if entity_id in after_by_id],
        changed_ids,
    )


def _compile_decision(
    state: Mapping[str, Any],
    prepared: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
    term_directory: Path,
) -> tuple[dict[str, Any], dict[str, int], list[str], list[str], list[str]]:
    transaction = prepared["transaction"]
    candidate = _compact_state(state, transaction["current_run_id"])
    entity_locations = _state_entity_locations(state)
    operation_targets: set[str] = set()
    operation_counts = {name: 0 for name in sorted(DECISION_OPERATIONS)}
    operation_counts["auto_pending"] = 0
    operation_counts["conflict_pending"] = 0
    operation_counts["lms_action_state"] = 0
    new_pending_ids: list[str] = []
    changes = {item["id"]: item for item in prepared["changes"]}

    for change_id in (item["id"] for item in prepared["changes"]):
        change = changes[change_id]
        decision = decisions[change_id]
        if change["type"] == "deleted" and decision["operations"]:
            _decision_error(
                "phase2_decision_conflict", "삭제 change는 자동 보존 규칙으로만 처리됩니다."
            )
        for operation in decision["operations"]:
            operation, conflicts = _protect_automatic_event_operation(
                candidate, operation, change, transaction
            )
            for pending_id, added in conflicts:
                if pending_id in operation_targets:
                    _decision_error(
                        "phase2_decision_conflict",
                        "같은 conflict pending을 decision에서 다시 변경할 수 없습니다.",
                    )
                operation_targets.add(pending_id)
                operation_counts["conflict_pending"] += 1
                if added:
                    new_pending_ids.append(pending_id)
            if operation is None:
                continue
            collection, target_id, added = _apply_operation(
                candidate, operation, change, entity_locations
            )
            if target_id in operation_targets:
                _decision_error("phase2_decision_conflict", "같은 entity를 두 번 변경할 수 없습니다.")
            operation_targets.add(target_id)
            operation_counts[operation["op"]] += 1
            if collection == "pending" and operation.get("op") == "upsert_pending" and added:
                new_pending_ids.append(target_id)

    for change in prepared["changes"]:
        if change["type"] != "deleted":
            continue
        pending_id, added = _deleted_source_pending(
            candidate, state, change, transaction, operation_targets
        )
        if pending_id is not None:
            operation_counts["auto_pending"] += 1
            if added:
                new_pending_ids.append(pending_id)

    lms_changed_ids = _reconcile_lms_action_states(
        candidate, term_directory, transaction["current_run_id"]
    )
    operation_counts["lms_action_state"] = len(lms_changed_ids)

    try:
        validate_state_references(candidate, term_directory)
    except HylmsError as exc:
        raise HylmsError("phase2_decision_conflict", exc.message) from exc
    _, _, changed_ids = _changed_entities(state, candidate)
    return candidate, operation_counts, list(dict.fromkeys(new_pending_ids)), changed_ids, lms_changed_ids


def _course_context(term_directory, state):
    from .course_context import read_context
    return read_context(Path(term_directory).resolve().parent.parent, state["term"])


def _qa_entities(state, candidate, changes=()):
    """Read-only closure over changed entities, changed sources and known links."""
    collections = ("natural_events", "pending", "announcement_applications")
    before = {item["id"]: item for name in collections for item in state[name]}
    after = {item["id"]: item for name in collections for item in candidate[name]}
    selected = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    recovery_courses = {str(entity["course_id"]) for key in selected
                        for entity in (before.get(key), after.get(key)) if entity
                        and (entity.get("context") or {}).get("kind") == "qa_pending"}
    sources = {(str(item["course_id"]), item["source_record_id"]) for item in changes}
    for entity in [*before.values(), *after.values()]:
        refs = [*entity.get("source_record_ids", []), *entity.get("target_record_ids", [])]
        if str(entity.get("course_id")) in recovery_courses or any((str(entity.get("course_id")), ref) in sources for ref in refs):
            selected.add(entity["id"])
    todo = list(selected)
    while todo:
        key = todo.pop()
        for entity in (before.get(key), after.get(key)):
            if entity is None:
                continue
            context = entity.get("context") or {}
            refs = [*context.get("linked_event_ids", []),
                    *context.get("linked_announcement_application_ids", [])]
            if isinstance(context.get("event_id"), str):
                refs.append(context["event_id"])
            for ref in refs:
                if ref not in selected and (ref in before or ref in after):
                    selected.add(ref)
                    todo.append(ref)
    return ([copy.deepcopy(before[key]) for key in sorted(selected) if key in before],
            [copy.deepcopy(after[key]) for key in sorted(selected) if key in after])


def _structured_qa_evidence(term_directory, state, entities, changes=(), *, current_run_id=None):
    """Safe, bounded projections; do not expose raw records or future runs."""
    sources = {(str(item["course_id"]), item["source_record_id"]) for item in changes}
    for entity in entities:
        for ref in [*entity.get("source_record_ids", []), *entity.get("target_record_ids", [])]:
            sources.add((str(entity["course_id"]), ref))
    runs = discover_runs(term_directory)
    ids = [run["id"] for run in runs]
    previous_id = state["last_processed_run_id"]
    current_id = current_run_id or previous_id
    previous, _ = _logical_records(runs[:ids.index(previous_id) + 1])
    current, _ = _logical_records(runs[:ids.index(current_id) + 1])
    values = []
    for course_id, ref in sorted(sources):
        key = f"{course_id}:{ref}"
        old, new = previous.get(key), current.get(key)
        identity = new or old
        value = {"course_id": course_id, "qualified_id": ref,
                 "before_run_id": previous_id, "after_run_id": current_id,
                 "before": copy.deepcopy(old["structured"]["compare"]) if old else None,
                 "after": copy.deepcopy(new["structured"]["compare"]) if new else None,
                 "source_text_before": copy.deepcopy(old["text"]["compare"]) if old else None,
                 "source_text_after": copy.deepcopy(new["text"]["compare"]) if new else None}
        from .course_context import classify_record
        value["management"] = {"before": classify_record(old) if old else None,
                               "after": classify_record(new) if new else None}
        if identity:
            value.update({name: identity[name] for name in ("key", "course", "section", "record_id", "source_url")})
        values.append(_scrub(value))
    return values


def preview_state_transaction(
    term_directory: Path,
    state: Mapping[str, Any],
    decision_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile one adjacent-run transaction completely without writing state."""
    validate_state_references(state, term_directory)
    prepared = prepare_decision_packet(term_directory, state)
    if prepared is None:
        _decision_error("phase2_decision_stale", "처리할 다음 snapshot run이 없습니다.")
    decisions = _validate_decision_result(decision_result, prepared)
    candidate, operation_counts, new_pending_ids, changed_ids, lms_changed_ids = (
        _compile_decision(state, prepared, decisions, term_directory)
    )
    diff = next_run_diff(term_directory, state)
    if diff is None or _fingerprint(diff) != prepared["transaction"]["diff_sha256"]:
        _decision_error("phase2_decision_stale", "preview 중 snapshot diff가 변경되었습니다.")
    current_entities, candidate_entities, derived_changed_ids = _changed_entities(
        state, candidate
    )
    if derived_changed_ids != changed_ids:
        _decision_error("phase2_decision_conflict", "preview entity 변경 집계가 일치하지 않습니다.")
    new_pending = [
        copy.deepcopy(item) for item in candidate["pending"] if item["id"] in new_pending_ids
    ]
    semantic_mutation = _semantic_collections(state) != _semantic_collections(candidate)
    current_entities, candidate_entities = _qa_entities(state, candidate, prepared["changes"])
    qa_context = {
        "mode": "automatic",
        "course_context": _course_context(term_directory, state),
        "transaction_id": prepared["transaction"]["id"],
        "candidate_state_sha256": _fingerprint(candidate),
        "changes": copy.deepcopy(prepared["changes"]),
        "structured_changes": _structured_qa_evidence(
            term_directory, state, [*current_entities, *candidate_entities], prepared["changes"],
            current_run_id=prepared["transaction"]["current_run_id"]
        ),
        "current_entities": current_entities,
        "candidate_entities": candidate_entities,
        "rules": copy.deepcopy(state["rules"]),
        "decision_draft": copy.deepcopy(dict(decision_result)),
        "instruction": None,
    }
    _safe_json(qa_context, "QA context")
    return {
        "prepared": prepared,
        "candidate_state": candidate,
        "candidate_state_sha256": _fingerprint(candidate),
        "semantic_mutation": semantic_mutation,
        "operation_counts": operation_counts,
        "new_pending": new_pending,
        "changed_entity_ids": changed_ids,
        "lms_action_state_event_ids": lms_changed_ids,
        "qa_context": qa_context,
    }


def _validate_qa_gate(
    preview: Mapping[str, Any],
    qa_packet: Mapping[str, Any] | None,
    qa_verdict: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not preview["semantic_mutation"]:
        if qa_packet is not None or qa_verdict is not None:
            _decision_error("phase2_qa_invalid", "의미 변경이 없는 transaction에는 QA가 필요하지 않습니다.")
        return {"required": False, "verdict": "not_required", "attempt": 0}
    if qa_packet is None or qa_verdict is None:
        _decision_error("phase2_qa_required", "state 의미 변경에는 QA pass가 필요합니다.")
    attempt = qa_packet.get("attempt")
    review_id = qa_packet.get("review_id")
    expected = prepare_qa_packet(
        preview["qa_context"], attempt=attempt, review_id=review_id
    )
    if expected != qa_packet:
        _decision_error("phase2_qa_invalid", "QA packet이 현재 candidate와 일치하지 않습니다.")
    verdict = validate_qa_verdict(qa_verdict, qa_packet)
    if verdict["verdict"] != "pass":
        _decision_error("phase2_qa_not_approved", "QA pass가 아닌 candidate는 commit할 수 없습니다.")
    return {"required": True, "verdict": "pass", "attempt": qa_packet["attempt"]}


def _decision_receipt(
    prepared: Mapping[str, Any],
    state: Mapping[str, Any],
    operation_counts: Mapping[str, int],
    new_pending: list[Mapping[str, Any]],
    changed_entity_ids: list[str],
    qa: Mapping[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    transaction = prepared["transaction"]
    return {
        "transaction_id": transaction["id"],
        "previous_run_id": transaction["previous_run_id"],
        "current_run_id": transaction["current_run_id"],
        "operation_counts": dict(operation_counts),
        "changed_entity_ids": copy.deepcopy(changed_entity_ids),
        "new_pending": copy.deepcopy(new_pending),
        "pending_total": len(state["pending"]),
        "qa": copy.deepcopy(dict(qa)),
        "state_sha256": _fingerprint(state),
        "replayed": replayed,
    }


def commit_state(
    path: Path,
    state: Mapping[str, Any],
    decision_result: Mapping[str, Any],
    *,
    term_directory: Path,
    qa_packet: Mapping[str, Any] | None = None,
    qa_verdict: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and atomically commit one complete decision transaction."""
    validate_state_references(state, term_directory)
    on_disk = _read_committed_state(path, term_directory)
    preview = preview_state_transaction(term_directory, state, decision_result)
    prepared = preview["prepared"]
    if prepared["transaction"]["base_state_sha256"] != _fingerprint(state):
        _decision_error("phase2_decision_stale", "base state hash가 일치하지 않습니다.")
    qa = _validate_qa_gate(preview, qa_packet, qa_verdict)
    committed = preview["candidate_state"]

    disk_hash = _fingerprint(on_disk)
    base_hash = prepared["transaction"]["base_state_sha256"]
    committed_hash = _fingerprint(committed)
    if disk_hash == committed_hash:
        receipt = _decision_receipt(
            prepared, on_disk, preview["operation_counts"], preview["new_pending"],
            preview["changed_entity_ids"], qa, replayed=True
        )
        return {"state": on_disk, "receipt": receipt}
    if disk_hash != base_hash:
        _decision_error("phase2_decision_stale", "state가 decision 준비 이후 변경되었습니다.")

    _write_state_if_changed(path, committed)
    receipt = _decision_receipt(
        prepared, committed, preview["operation_counts"], preview["new_pending"],
        preview["changed_entity_ids"], qa, replayed=False
    )
    return {"state": committed, "receipt": receipt}


def commit_qa_pending_state(
    path: Path,
    state: Mapping[str, Any],
    decision_result: Mapping[str, Any],
    qa_packet: Mapping[str, Any],
    qa_verdict: Mapping[str, Any],
    *,
    term_directory: Path,
) -> dict[str, Any]:
    """Compatibility entry point: unapproved QA can never advance state."""
    _decision_error("phase2_qa_review_required", "QA 미승인은 시스템 재검토 대상이며 사용자 pending으로 저장할 수 없습니다.")


def prepare_manual_packet(
    state: Mapping[str, Any],
    instruction: str,
    target_ids: list[str],
    requested_at: str,
) -> dict[str, Any]:
    """Bind one explicit user instruction to the current state without writing it."""
    validate_state(state)
    if not isinstance(instruction, str) or not instruction.strip():
        _decision_error("phase2_manual_invalid", "사용자 수정 지시가 비어 있습니다.")
    try:
        targets = _string_list(target_ids, "manual target_ids", nonempty=True)
        _validate_confirmation_timestamp(requested_at, "manual requested_at")
    except HylmsError as exc:
        raise HylmsError("phase2_manual_invalid", exc.message) from exc
    locations = _state_entity_locations(state)
    if any(target_id not in locations for target_id in targets):
        _decision_error("phase2_manual_invalid", "manual target을 현재 state에서 찾을 수 없습니다.")
    if any(locations[target_id] == "rules" for target_id in targets):
        _decision_error("phase2_manual_invalid", "manual transaction은 승인 rule을 변경할 수 없습니다.")
    scrubbed_instruction = _scrub(instruction.strip())
    transaction = {
        "base_state_sha256": _fingerprint(state),
        "last_processed_run_id": state["last_processed_run_id"],
        "requested_at": requested_at,
        "instruction_sha256": _fingerprint(scrubbed_instruction),
        "target_ids": copy.deepcopy(targets),
    }
    transaction["id"] = _fingerprint(transaction)
    packet = {
        "schema_version": MANUAL_SCHEMA_VERSION,
        "transaction": transaction,
        "instruction": scrubbed_instruction,
    }
    try:
        _safe_json(packet, "manual packet")
    except HylmsError as exc:
        raise HylmsError("phase2_manual_invalid", exc.message) from exc
    return packet


def _validate_manual_result(
    value: Mapping[str, Any], packet: Mapping[str, Any]
) -> list[dict[str, Any]]:
    result = _decision_exact_keys(
        value, {"schema_version", "transaction_id", "reason", "operations"},
        "manual result",
    )
    if result["schema_version"] != MANUAL_SCHEMA_VERSION:
        _decision_error("phase2_manual_invalid", "manual result schema version이 올바르지 않습니다.")
    if result["transaction_id"] != packet["transaction"]["id"]:
        _decision_error("phase2_manual_stale", "manual transaction이 현재 packet과 일치하지 않습니다.")
    _decision_string(result["reason"], "manual result.reason")
    if not isinstance(result["operations"], list) or not result["operations"]:
        _decision_error("phase2_manual_invalid", "manual result에는 operation이 필요합니다.")
    operations: list[dict[str, Any]] = []
    for operation in result["operations"]:
        if not isinstance(operation, dict) or operation.get("op") not in MANUAL_OPERATIONS:
            _decision_error("phase2_manual_invalid", "허용되지 않은 manual operation입니다.")
        operations.append(copy.deepcopy(operation))
    try:
        _safe_json(result, "manual result")
    except HylmsError as exc:
        raise HylmsError("phase2_manual_invalid", exc.message) from exc
    return operations


def _manual_existing_entity(state: Mapping[str, Any], entity_id: str) -> tuple[str, dict[str, Any]] | None:
    for collection in ("natural_events", "pending", "announcement_applications"):
        item = next((entry for entry in state[collection] if entry["id"] == entity_id), None)
        if item is not None:
            return collection, item
    return None


def _manual_upsert_event(
    candidate: dict[str, Any],
    operation: Mapping[str, Any],
    requested_at: str,
    target_ids: set[str],
    resolving_pending: bool,
) -> str:
    _decision_exact_keys(operation, {"op", "value", "confirmed_fields"}, "manual upsert_event")
    value = copy.deepcopy(operation["value"])
    try:
        fields = _string_list(
            operation["confirmed_fields"], "manual confirmed_fields", nonempty=True
        )
    except HylmsError as exc:
        raise HylmsError("phase2_manual_invalid", exc.message) from exc
    if any(field not in USER_CONFIRMABLE_FIELDS - {"action_state"} for field in fields):
        _decision_error("phase2_manual_invalid", "manual confirmed field가 올바르지 않습니다.")
    if not isinstance(value, dict) or set(value) != EVENT_KEYS:
        _decision_error("phase2_manual_invalid", "manual event key가 schema와 일치하지 않습니다.")
    event_id = str(value.get("id") or "")
    existing = next(
        (item for item in candidate["natural_events"] if item["id"] == event_id), None
    )
    if existing is None:
        if not resolving_pending:
            _decision_error(
                "phase2_manual_conflict", "새 event는 targeted pending을 함께 해결해야 합니다."
            )
        if value.get("user_confirmed") != {}:
            _decision_error("phase2_manual_conflict", "새 manual event의 user_confirmed는 비어 있어야 합니다.")
        if (
            value.get("action_state_authority") in {"lms", "user"}
            and value.get("action_state") != "unknown"
        ):
            _decision_error(
                "phase2_manual_conflict", "새 행동 event는 unknown 완료 상태로 시작해야 합니다."
            )
        value["user_confirmed"] = {
            field: requested_at for field in sorted(fields)
        }
    else:
        if event_id not in target_ids:
            _decision_error("phase2_manual_conflict", "수정 event가 manual target과 일치하지 않습니다.")
        if value["course_id"] != existing["course_id"]:
            _decision_error("phase2_manual_conflict", "manual event의 과목을 변경할 수 없습니다.")
        if value["action_state_authority"] != existing["action_state_authority"]:
            _decision_error("phase2_manual_conflict", "manual event의 완료 권위를 변경할 수 없습니다.")
        if value["action_state"] != existing["action_state"]:
            _decision_error(
                "phase2_manual_conflict", "manual event upsert로 action_state를 변경할 수 없습니다."
            )
        if value["user_confirmed"] != existing["user_confirmed"]:
            _decision_error("phase2_manual_conflict", "manual result가 confirmation 시각을 직접 쓸 수 없습니다.")
        changed_fields = {
            field for field in USER_CONFIRMABLE_FIELDS - {"action_state"}
            if _event_field_value(existing, field) != _event_field_value(value, field)
        }
        if not changed_fields.issubset(set(fields)):
            _decision_error(
                "phase2_manual_conflict", "변경된 모든 event field를 confirmed_fields에 포함해야 합니다."
            )
        value["source_record_ids"] = list(dict.fromkeys([
            *existing["source_record_ids"], *value["source_record_ids"],
        ]))
        value["evidence"] = list(dict.fromkeys([*existing["evidence"], *value["evidence"]]))
        value["user_confirmed"] = copy.deepcopy(existing["user_confirmed"])
        for field in fields:
            value["user_confirmed"][field] = requested_at
    try:
        _validate_event(value)
    except HylmsError as exc:
        raise HylmsError("phase2_manual_invalid", exc.message) from exc
    _upsert_entity(candidate["natural_events"], value)
    return event_id


def _compile_manual_transaction(
    state: Mapping[str, Any],
    packet: Mapping[str, Any],
    result: Mapping[str, Any],
    term_directory: Path,
) -> tuple[dict[str, Any], list[str]]:
    validate_state_references(state, term_directory)
    if not isinstance(packet, dict) or set(packet) != {"schema_version", "transaction", "instruction"}:
        _decision_error("phase2_manual_invalid", "manual packet key가 schema와 일치하지 않습니다.")
    if packet["schema_version"] != MANUAL_SCHEMA_VERSION:
        _decision_error("phase2_manual_invalid", "manual packet schema version이 올바르지 않습니다.")
    expected = prepare_manual_packet(
        state,
        packet["instruction"],
        packet["transaction"]["target_ids"],
        packet["transaction"]["requested_at"],
    )
    if expected != packet:
        _decision_error("phase2_manual_stale", "manual packet이 현재 state와 일치하지 않습니다.")
    operations = _validate_manual_result(result, packet)
    candidate = copy.deepcopy(dict(state))
    target_ids = set(packet["transaction"]["target_ids"])
    requested_at = packet["transaction"]["requested_at"]
    resolving_pending = any(operation.get("op") == "resolve_pending" for operation in operations)
    changed_targets: set[str] = set()
    for operation in operations:
        operation_type = operation["op"]
        if operation_type == "upsert_event":
            target_id = _manual_upsert_event(
                candidate, operation, requested_at, target_ids, resolving_pending
            )
        elif operation_type == "cancel_event":
            _decision_exact_keys(operation, {"op", "id", "evidence"}, "manual cancel_event")
            target_id = _decision_string(operation["id"], "manual cancel_event.id")
            if target_id not in target_ids:
                _decision_error("phase2_manual_conflict", "취소 event가 manual target과 일치하지 않습니다.")
            event = next(
                (item for item in candidate["natural_events"] if item["id"] == target_id), None
            )
            if event is None:
                _decision_error("phase2_manual_conflict", "취소할 event가 없습니다.")
            try:
                evidence = _string_list(operation["evidence"], "manual cancel_event.evidence")
            except HylmsError as exc:
                raise HylmsError("phase2_manual_invalid", exc.message) from exc
            event["status"] = "cancelled"
            event["evidence"] = list(dict.fromkeys([*event["evidence"], *evidence]))
            event["user_confirmed"]["status"] = requested_at
        elif operation_type == "resolve_pending":
            _decision_exact_keys(operation, {"op", "id"}, "manual resolve_pending")
            target_id = _decision_string(operation["id"], "manual resolve_pending.id")
            if target_id not in target_ids:
                _decision_error("phase2_manual_conflict", "해결 pending이 manual target과 일치하지 않습니다.")
            if not any(item["id"] == target_id for item in candidate["pending"]):
                _decision_error("phase2_manual_conflict", "해결할 pending이 없습니다.")
            candidate["pending"] = [item for item in candidate["pending"] if item["id"] != target_id]
        elif operation_type == "upsert_pending":
            _decision_exact_keys(operation, {"op", "value"}, "manual upsert_pending")
            value = _validate_operation_value(_validate_pending, operation["value"], "manual pending")
            target_id = value["id"]
            existing = next((p for p in candidate["pending"] if p["id"] == target_id), None)
            if target_id not in target_ids or existing is None or value["course_id"] != existing["course_id"]:
                _decision_error("phase2_manual_conflict", "pending 재분류 대상이 기존 대상과 다릅니다.")
            if not set(existing["source_record_ids"]).issubset(value["source_record_ids"]):
                _decision_error("phase2_manual_conflict", "pending 재분류에서 기존 출처를 제거할 수 없습니다.")
            _upsert_entity(candidate["pending"], value)
        elif operation_type == "upsert_announcement_application":
            _decision_exact_keys(
                operation, {"op", "value"}, "manual upsert_announcement_application"
            )
            value = _validate_operation_value(
                _validate_application, operation["value"], "manual announcement application"
            )
            target_id = value["id"]
            existing = _manual_existing_entity(candidate, target_id)
            if existing is not None and target_id not in target_ids:
                _decision_error("phase2_manual_conflict", "공지 반영이 manual target과 일치하지 않습니다.")
            if existing is None and not resolving_pending:
                _decision_error(
                    "phase2_manual_conflict", "새 공지 반영은 targeted pending을 함께 해결해야 합니다."
                )
            if existing is not None:
                collection, current = existing
                if collection != "announcement_applications":
                    _decision_error("phase2_manual_conflict", "기존 entity 종류를 변경할 수 없습니다.")
                if value["course_id"] != current["course_id"]:
                    _decision_error("phase2_manual_conflict", "공지 반영의 과목을 변경할 수 없습니다.")
                value = copy.deepcopy(value)
                value["source_record_ids"] = list(dict.fromkeys([
                    *current["source_record_ids"], *value["source_record_ids"],
                ]))
                value["evidence"] = list(dict.fromkeys([
                    *current["evidence"], *value["evidence"],
                ]))
            _upsert_entity(candidate["announcement_applications"], value)
        else:
            _decision_exact_keys(
                operation, {"op", "id", "state", "evidence"}, "set_user_action_state"
            )
            target_id = _decision_string(operation["id"], "set_user_action_state.id")
            if target_id not in target_ids:
                _decision_error("phase2_manual_conflict", "완료 event가 manual target과 일치하지 않습니다.")
            if operation["state"] not in {"done", "unknown"}:
                _decision_error("phase2_manual_invalid", "사용자 action state가 올바르지 않습니다.")
            event = next(
                (item for item in candidate["natural_events"] if item["id"] == target_id), None
            )
            if event is None or event["action_state_authority"] != "user":
                _decision_error("phase2_manual_conflict", "user 권위 event만 완료 상태를 변경할 수 있습니다.")
            try:
                evidence = _string_list(operation["evidence"], "set_user_action_state.evidence")
            except HylmsError as exc:
                raise HylmsError("phase2_manual_invalid", exc.message) from exc
            event["action_state"] = operation["state"]
            event["evidence"] = list(dict.fromkeys([*event["evidence"], *evidence]))
            event["user_confirmed"]["action_state"] = requested_at
        if target_id in changed_targets:
            _decision_error("phase2_manual_conflict", "같은 entity를 두 번 수정할 수 없습니다.")
        changed_targets.add(target_id)
    try:
        validate_state_references(candidate, term_directory)
    except HylmsError as exc:
        raise HylmsError("phase2_manual_conflict", exc.message) from exc
    _, _, changed_ids = _changed_entities(state, candidate)
    if not changed_ids:
        _decision_error("phase2_manual_invalid", "manual transaction에 실제 변경이 없습니다.")
    return candidate, changed_ids


def preview_manual_transaction(
    term_directory: Path,
    state: Mapping[str, Any],
    packet: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    candidate, changed_ids = _compile_manual_transaction(
        state, packet, result, term_directory
    )
    current_entities, candidate_entities, derived_ids = _changed_entities(state, candidate)
    if derived_ids != changed_ids:
        _decision_error("phase2_manual_conflict", "manual entity 변경 집계가 일치하지 않습니다.")
    current_entities, candidate_entities = _qa_entities(state, candidate)
    context = {
        "mode": "manual",
        "course_context": _course_context(term_directory, state),
        "transaction_id": packet["transaction"]["id"],
        "candidate_state_sha256": _fingerprint(candidate),
        "changes": [],
        "structured_changes": _structured_qa_evidence(
            term_directory, state, [*current_entities, *candidate_entities]),
        "current_entities": current_entities,
        "candidate_entities": candidate_entities,
        "rules": copy.deepcopy(state["rules"]),
        "decision_draft": copy.deepcopy(dict(result)),
        "instruction": packet["instruction"],
    }
    _safe_json(context, "manual QA context")
    return {
        "candidate_state": candidate,
        "candidate_state_sha256": _fingerprint(candidate),
        "semantic_mutation": True,
        "changed_entity_ids": changed_ids,
        "qa_context": context,
    }


def commit_manual_state(
    path: Path,
    state: Mapping[str, Any],
    packet: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    term_directory: Path,
    qa_packet: Mapping[str, Any],
    qa_verdict: Mapping[str, Any],
) -> dict[str, Any]:
    """Commit one QA-approved user correction while preserving the run cursor."""
    validate_state_references(state, term_directory)
    on_disk = _read_committed_state(path, term_directory)
    preview = preview_manual_transaction(term_directory, state, packet, result)
    qa = _validate_qa_gate(preview, qa_packet, qa_verdict)
    candidate = preview["candidate_state"]
    if candidate["last_processed_run_id"] != state["last_processed_run_id"]:
        _decision_error("phase2_manual_conflict", "manual transaction은 cursor를 변경할 수 없습니다.")
    disk_hash = _fingerprint(on_disk)
    base_hash = packet["transaction"]["base_state_sha256"]
    candidate_hash = _fingerprint(candidate)
    replayed = disk_hash == candidate_hash
    if not replayed and disk_hash != base_hash:
        _decision_error("phase2_manual_stale", "state가 manual 준비 이후 변경되었습니다.")
    if not replayed:
        _write_state_if_changed(path, candidate)
    receipt = {
        "transaction_id": packet["transaction"]["id"],
        "cursor": candidate["last_processed_run_id"],
        "changed_entity_ids": copy.deepcopy(preview["changed_entity_ids"]),
        "new_pending": [],
        "pending_total": len(candidate["pending"]),
        "qa": qa,
        "state_sha256": candidate_hash,
        "replayed": replayed,
    }
    return {"state": candidate if not replayed else on_disk, "receipt": receipt}


def record_failure(
    path: Path,
    state: Mapping[str, Any],
    run_id: str,
    stage: str,
    code: str,
    *,
    term_directory: Path,
) -> dict[str, Any]:
    validate_state_references(state, term_directory)
    on_disk = _read_committed_state(path, term_directory)
    if _fingerprint(on_disk) != _fingerprint(state):
        _decision_error("phase2_decision_stale", "state가 실패 기록 준비 이후 변경되었습니다.")
    runs = discover_runs(term_directory)
    ids = [run["id"] for run in runs]
    cursor = ids.index(state["last_processed_run_id"])
    if run_id not in ids or ids.index(run_id) > cursor + 1:
        _invalid("실패는 현재 또는 바로 다음 snapshot run에만 기록할 수 있습니다.")
    failed = copy.deepcopy(dict(state))
    failed["last_failure"] = {"run_id": run_id, "stage": stage, "code": code}
    validate_state_references(failed, term_directory)
    _write_state_if_changed(path, failed)
    return failed
