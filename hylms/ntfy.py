"""Deterministic ntfy alert planning and no-queue delivery for HY-LMS."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .core import HylmsError, KST
from .diff import validate_state, validate_state_references
from .schedule_sources import (
    load_logical_records as _shared_load_logical_records,
    read_json as _read_json,
    root_status as _root_status,
    structured_items as _structured_items,
)

NTFY_SCHEMA_VERSION = 2
from .config import load_config

NTFY_TOPIC = load_config().get("ntfy_topic", "")
NTFY_URL = "https://ntfy.sh/"
# Retained as an import-compatible legacy constant; never emitted or accepted.
NTFY_SEPARATOR = "-" * 41
NTFY_MESSAGE_LIMIT = 4096
SNAPSHOT_FRESHNESS = dt.timedelta(hours=24)
ALERT_SEVERITIES = {"urgent", "general"}
REASON_ORDER = (
    "today_due",
    "today_ends",
    "completion_check",
    "today_open",
    "today_starts",
    "tomorrow_due",
    "tomorrow_open",
    "natural_today_upcoming",
    "natural_today_ongoing",
    "natural_tomorrow",
    "optional",
)
REASON_LABELS = {
    "today_due": "오늘 마감",
    "today_ends": "오늘 종료",
    "completion_check": "완료 확인 필요",
    "today_open": "오늘 공개",
    "today_starts": "오늘 시작",
    "tomorrow_due": "내일 마감",
    "tomorrow_open": "내일 공개",
    "natural_today_upcoming": "오늘 예정",
    "natural_today_ongoing": "진행 중",
    "natural_tomorrow": "내일 일정",
    "optional": "선택",
}
_TITLE_PREFIX = re.compile(r"^\[(?:선택|필수|마감|입장 마감)\]\s*")
PAYLOAD_STYLES = {
    "urgent": ("HY-LMS 긴급", 5, ["rotating_light", "warning"]),
    "general": ("HY-LMS 알림", 3, ["loudspeaker"]),
    "empty": ("HY-LMS 상태", 3, ["heavy_check_mark"]),
    "refresh": ("HY-LMS 갱신 필요", 4, ["warning"]),
}
PAYLOAD_GROUPS = set(PAYLOAD_STYLES)


def _invalid(code: str, message: str) -> None:
    raise HylmsError(code, message)


def _parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or "T" not in value:
        _invalid("ntfy_input_invalid", f"{label} 시각이 올바르지 않습니다.")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        _invalid("ntfy_input_invalid", f"{label} 시각이 올바르지 않습니다.")
    if parsed.tzinfo is None:
        _invalid("ntfy_input_invalid", f"{label} 시각에 timezone이 없습니다.")
    return parsed.astimezone(KST)


def _normalize_now(value: dt.datetime) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        _invalid("ntfy_input_invalid", "now는 timezone이 있는 datetime이어야 합니다.")
    return value.astimezone(KST)


def _load_logical_records(term_directory: Path) -> dict[str, Any]:
    try:
        return _shared_load_logical_records(term_directory)
    except HylmsError as exc:
        code = exc.code.replace("schedule_", "ntfy_", 1)
        raise HylmsError(code, exc.message) from exc


def _date_of(value: str) -> dt.date:
    if "T" in value:
        return _parse_time(value, "schedule boundary").date()
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        _invalid("ntfy_input_invalid", "schedule 날짜가 올바르지 않습니다.")
    raise AssertionError


def _structured_reasons(item: Mapping[str, Any], today: dt.date) -> tuple[str | None, list[str]]:
    reasons: list[str] = []
    due_at = item["schedule"].get("due_at")
    opens_at = item["schedule"].get("opens_at")
    tomorrow = today + dt.timedelta(days=1)
    if due_at is not None and _date_of(due_at) == today:
        reasons.append("today_due")
    if opens_at is not None and _date_of(opens_at) == today:
        reasons.append("today_open")
    if due_at is not None and _date_of(due_at) == tomorrow:
        reasons.append("tomorrow_due")
    if opens_at is not None and _date_of(opens_at) == tomorrow:
        reasons.append("tomorrow_open")
    if not reasons:
        return None, []
    if item["completion"] == "confirmation_required":
        reasons.append("completion_check")
    if item["optional"]:
        reasons.append("optional")
    severity = "urgent" if "today_due" in reasons else "general"
    return severity, [reason for reason in REASON_ORDER if reason in reasons]


def _natural_reasons(event: Mapping[str, Any], now: dt.datetime) -> tuple[str | None, list[str], str]:
    if event["status"] == "cancelled" or event["action_state"] == "done":
        return None, [], "inactive"
    timing = event["timing"]
    mode = timing["mode"]
    all_day = timing["all_day"]
    today = now.date()
    tomorrow = today + dt.timedelta(days=1)
    start_raw, end_raw = timing.get("start"), timing.get("end")
    reasons: list[str] = []
    display_time = end_raw or start_raw or ""
    if all_day:
        start_date = dt.date.fromisoformat(start_raw) if start_raw else None
        end_date = dt.date.fromisoformat(end_raw)
        if end_date < today:
            return None, [], "ended"
        if mode == "deadline":
            if end_date == today:
                reasons.append("today_due")
            elif end_date == tomorrow:
                reasons.append("tomorrow_due")
        elif start_date == today and end_date == today:
            reasons.append("natural_today_ongoing")
        elif end_date == today and start_date is not None and start_date < today:
            reasons.append("today_ends")
        elif start_date == today:
            reasons.append("today_starts")
        elif start_date == tomorrow:
            reasons.append("natural_tomorrow")
        elif end_date == tomorrow and start_date is not None and start_date < today:
            reasons.append("tomorrow_due")
    else:
        start = _parse_time(start_raw, "natural event start") if start_raw else None
        end = _parse_time(end_raw, "natural event end")
        if end <= now:
            return None, [], "ended"
        if mode == "deadline":
            if end.date() == today:
                reasons.append("today_due")
            elif end.date() == tomorrow:
                reasons.append("tomorrow_due")
        elif start is not None and start.date() == today and end.date() == today:
            reasons.append(
                "natural_today_upcoming" if now < start else "natural_today_ongoing"
            )
            display_time = start_raw
        elif start is not None and start.date() < today and end.date() == today:
            reasons.append("today_ends")
        elif start is not None and start.date() == today:
            reasons.append("today_starts")
            display_time = start_raw
        elif start is not None and start.date() == tomorrow:
            reasons.append("natural_tomorrow")
            display_time = start_raw
        elif start is not None and start.date() < today and end.date() == tomorrow:
            reasons.append("tomorrow_due")
    if not reasons:
        return None, [], "outside_window"
    if event["action_state"] == "unknown" and "today_due" in reasons:
        reasons.append("completion_check")
    if event["optional"]:
        reasons.append("optional")
    severity = "urgent" if any(
        reason in reasons for reason in (
            "today_due", "today_ends", "natural_today_upcoming", "natural_today_ongoing"
        )
    ) else "general"
    return severity, [reason for reason in REASON_ORDER if reason in reasons], display_time


def _target(
    identity: str,
    course_id: str,
    course_name: str,
    section: str,
    record_id: str,
    title: str,
    severity: str,
    reasons: list[str],
    display_time: str,
    completion: str,
    source_record_ids: Sequence[str],
) -> dict[str, Any]:
    return {
        "identity": identity,
        "course_id": course_id,
        "course_name": course_name,
        "section": section,
        "record_id": record_id,
        "title": title,
        "severity": severity,
        "reasons": reasons,
        "display_time": display_time,
        "completion": completion,
        "source_record_ids": list(source_record_ids),
    }


def _merge_target(targets: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    existing = targets.get(candidate["identity"])
    if existing is None:
        targets[candidate["identity"]] = candidate
        return
    if candidate["severity"] == "urgent":
        existing["severity"] = "urgent"
    existing["reasons"] = [
        reason for reason in REASON_ORDER
        if reason in set([*existing["reasons"], *candidate["reasons"]])
    ]
    existing["source_record_ids"] = list(dict.fromkeys([
        *existing["source_record_ids"], *candidate["source_record_ids"],
    ]))


def _display_time(value: str, reasons: Sequence[str]) -> str:
    if not value:
        return "시각 미정"
    if "T" not in value:
        parsed_date = dt.date.fromisoformat(value)
        compact = f"{parsed_date.month}/{parsed_date.day}"
    else:
        parsed = _parse_time(value, "display time")
        compact = f"{parsed.month}/{parsed.day} {parsed.strftime('%H:%M')}"
    if any(reason in reasons for reason in ("today_due", "tomorrow_due", "today_ends")):
        return compact + "까지"
    return compact


def _warning_label(value: str) -> str:
    if value == "snapshot_status_missing":
        return "snapshot 상태 없음"
    if value == "snapshot_status_invalid":
        return "snapshot 상태 오류"
    if value == "snapshot_partial_failure":
        return "snapshot 일부 실패"
    if value == "state_stale":
        return "state 오래됨"
    if value == "state_cursor_behind":
        return "state 처리 지연"
    if value in {"snapshot_missing", "snapshot_refresh_required"}:
        return "snapshot 갱신 필요"
    if value.startswith("stale_courses:"):
        return "일부 과목 snapshot 오래됨"
    if value.startswith("course_"):
        return "일부 과목 fallback 사용"
    if value.startswith("application_conflict:"):
        return "공지 반영 충돌"
    if value.startswith("linked_record_missing:"):
        return "연결 record 확인 필요"
    return value


def _header(now: dt.datetime, snapshot: Mapping[str, Any], warnings: Sequence[str]) -> str:
    if not warnings:
        return ""
    selected_at = snapshot.get("selected_at")
    if selected_at:
        parsed = _parse_time(selected_at, "snapshot display time")
        snapshot_text = f"{parsed.month}/{parsed.day} {parsed.strftime('%H:%M')}"
    else:
        snapshot_text = "없음"
    labels = list(dict.fromkeys(_warning_label(value) for value in warnings))
    return f"⚠ {' · '.join(labels)}\nSnapshot {snapshot_text}"


def _target_line(item: Mapping[str, Any]) -> str:
    labels = " · ".join(REASON_LABELS[reason] for reason in item["reasons"])
    title = _TITLE_PREFIX.sub("", item["title"])
    timing = _display_time(item["display_time"], item["reasons"])
    return f"{item['course_name']}\n• {title} · {labels} · {timing}"


def _split_utf8(value: str, limit: int) -> list[str]:
    if len(value.encode("utf-8")) <= limit:
        return [value]
    parts: list[str] = []
    current = ""
    for character in value:
        if len((current + character).encode("utf-8")) > limit:
            if not current:
                _invalid("ntfy_payload_invalid", "UTF-8 message fragment를 분할할 수 없습니다.")
            parts.append(current)
            current = character
        else:
            current += character
    if current:
        parts.append(current)
    return parts


def _message_parts(
    header: str,
    blocks: list[tuple[str, str]],
    footer: str,
) -> list[dict[str, Any]]:
    prefix = header + "\n" if header else ""
    joiner = "\n\n"
    base_bytes = len(prefix.encode("utf-8"))
    if base_bytes >= NTFY_MESSAGE_LIMIT:
        _invalid("ntfy_payload_invalid", "ntfy message header가 너무 깁니다.")
    capacity = NTFY_MESSAGE_LIMIT - base_bytes
    expanded: list[tuple[str, str]] = []
    for identity, block in blocks:
        if len(block.encode("utf-8")) <= capacity:
            expanded.append((identity, block))
        else:
            marker_reserve = 64
            if capacity <= marker_reserve:
                _invalid("ntfy_payload_invalid", "ntfy continuation 공간이 부족합니다.")
            fragments = _split_utf8(block, capacity - marker_reserve)
            for index, fragment in enumerate(fragments, 1):
                marker = f"[항목 {index}/{len(fragments)}] "
                expanded.append((identity, marker + fragment))
    if footer:
        expanded.append(("__footer__", footer))
    if not expanded:
        expanded = [("__status__", "알림 대상 없음")]

    parts: list[dict[str, Any]] = []
    lines: list[str] = []
    identities: list[str] = []
    for identity, block in expanded:
        new_identity = not identity.startswith("__") and identity not in identities
        candidate = prefix + joiner.join([*lines, block])
        if lines and new_identity and len(identities) >= 3:
            parts.append({"message": prefix + joiner.join(lines), "item_ids": identities})
            lines, identities = [], []
            candidate = prefix + block
        if lines and len(candidate.encode("utf-8")) > NTFY_MESSAGE_LIMIT:
            parts.append({"message": prefix + joiner.join(lines), "item_ids": identities})
            lines, identities = [], []
        candidate = prefix + block
        if len(candidate.encode("utf-8")) > NTFY_MESSAGE_LIMIT:
            _invalid("ntfy_payload_invalid", "분할된 ntfy message가 제한을 넘습니다.")
        lines.append(block)
        if not identity.startswith("__") and identity not in identities:
            identities.append(identity)
    parts.append({"message": prefix + joiner.join(lines), "item_ids": identities})
    return parts


def _payloads(
    now: dt.datetime,
    snapshot: Mapping[str, Any],
    targets: list[dict[str, Any]],
    warnings: list[str],
    pending: list[Mapping[str, Any]],
    new_pending_ids: set[str],
    *,
    refresh_only: bool,
) -> list[dict[str, Any]]:
    header = _header(now, snapshot, warnings)
    groups: list[tuple[str, list[dict[str, Any]], list[tuple[str, str]]]] = []
    if refresh_only:
        groups.append(("refresh", [], [("__refresh__", "snapshot 갱신 필요")]))
    else:
        urgent = [item for item in targets if item["severity"] == "urgent"]
        general = [item for item in targets if item["severity"] == "general"]
        if urgent:
            groups.append(("urgent", urgent, [(item["identity"], _target_line(item)) for item in urgent]))
        new_pending = [item for item in pending if item["id"] in new_pending_ids]
        general_blocks = [(item["identity"], _target_line(item)) for item in general]
        general_blocks.extend(
            (f"pending:{item['id']}", f"- [새 확인 대기] {item['course_name']} · {item['title']}")
            for item in new_pending
        )
        if general_blocks:
            groups.append(("general", general, general_blocks))
        if not groups:
            groups.append(("empty", [], [("__status__", "알림 대상 없음")]))
    new_count = len(new_pending_ids & {item["id"] for item in pending})
    footer = f"실행 {now.month}/{now.day} {now.strftime('%H:%M')} · 확인대기 {new_count}+({len(pending) - new_count})"
    payloads: list[dict[str, Any]] = []
    for group_index, (group, group_targets, blocks) in enumerate(groups):
        parts = _message_parts(header, blocks, footer if group_index == len(groups) - 1 else "")
        title, priority, tags = PAYLOAD_STYLES[group]
        visible_count = len(group_targets)
        if group == "general":
            visible_count += len([item for item in pending if item["id"] in new_pending_ids])
        if group in {"urgent", "general"}:
            title = f"{title} · {visible_count}건"
        total = len(parts)
        for index, part in enumerate(parts, 1):
            part_title = f"{title} ({index}/{total})" if total > 1 else title
            payloads.append(
                {
                    "group": group,
                    "part": index,
                    "parts": total,
                    "item_ids": part["item_ids"],
                    "payload": {
                        "topic": NTFY_TOPIC,
                        "message": part["message"],
                        "title": part_title,
                        "tags": copy.deepcopy(tags),
                        "priority": priority,
                    },
                }
            )
    return payloads


def _validate_new_pending(state: Mapping[str, Any], values: Sequence[str]) -> set[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        _invalid("ntfy_input_invalid", "new_pending_ids는 문자열 목록이어야 합니다.")
    ids = [str(value) for value in values]
    if len(ids) != len(set(ids)):
        _invalid("ntfy_input_invalid", "new_pending_ids가 중복됩니다.")
    known = {item["id"] for item in state["pending"]}
    if any(value not in known for value in ids):
        _invalid("ntfy_input_invalid", "알 수 없는 new pending ID가 있습니다.")
    return set(ids)


def prepare_ntfy_delivery(
    term_directory: Path,
    state: Mapping[str, Any],
    *,
    now: dt.datetime,
    new_pending_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Prepare a complete delivery without writing files or contacting ntfy."""
    now = _normalize_now(now)
    validate_state(state)
    new_pending = _validate_new_pending(state, new_pending_ids)
    from .course_context import technical_pending
    user_pending = [p for p in state["pending"] if not technical_pending(p) and p.get("context", {}).get("resolution_owner") != "source"]
    new_pending &= {p["id"] for p in user_pending}
    warnings: list[str] = []
    if any(technical_pending(p) for p in state["pending"]):
        warnings.append("technical_review_required")
    root_status, status_warnings = _root_status(term_directory)
    warnings.extend(status_warnings)
    logical = _load_logical_records(term_directory)
    latest = logical["latest_run"]
    run_by_id = {run["id"]: run for run in logical["runs"]}
    state_run = run_by_id.get(state["last_processed_run_id"])
    if logical["runs"]:
        validate_state_references(state, term_directory)
        if state_run is None:
            _invalid("ntfy_state_invalid", "state cursor run을 찾을 수 없습니다.")
    state_at = state_run["started_at"].astimezone(KST) if state_run else None
    state_fresh = state_at is not None and now - state_at <= SNAPSHOT_FRESHNESS
    if not state_fresh:
        warnings.append("state_stale")
    if latest is not None and latest["id"] != state["last_processed_run_id"]:
        warnings.append("state_cursor_behind")

    structured, excluded = _structured_items(logical, state, warnings)
    targets: dict[str, dict[str, Any]] = {}
    fresh_records = 0
    stale_courses: set[str] = set()
    for item in structured.values():
        usable_at = _parse_time(item["usable_at"], "record usable_at")
        if now - usable_at > SNAPSHOT_FRESHNESS:
            stale_courses.add(item["course_id"])
            excluded.append({"identity": item["identity"], "reason": "stale_snapshot"})
            continue
        fresh_records += 1
        if item["completion"] == "done":
            excluded.append({"identity": item["identity"], "reason": "completed"})
            continue
        if item["completion"] == "excluded":
            continue
        severity, reasons = _structured_reasons(item, now.date())
        if severity is None:
            excluded.append({"identity": item["identity"], "reason": "outside_window"})
            continue
        display = item["schedule"].get("due_at") or item["schedule"].get("opens_at") or ""
        _merge_target(
            targets,
            _target(
                item["identity"], item["course_id"], item["course_name"], item["section"],
                item["record_id"], item["title"], severity, reasons, display, item["completion"],
                item["source_record_ids"],
            ),
        )
    if stale_courses:
        warnings.append("stale_courses:" + ",".join(sorted(stale_courses)))

    if state_fresh:
        for event in state["natural_events"]:
            severity, reasons, display = _natural_reasons(event, now)
            if severity is None:
                excluded.append({"identity": f"natural:{event['id']}", "reason": display})
                continue
            _merge_target(
                targets,
                _target(
                    f"natural:{event['id']}", event["course_id"], event["course_name"],
                    "natural_event", event["id"], event["title"], severity, reasons, display,
                    event["action_state"], event["source_record_ids"],
                ),
            )

    empty_at = logical.get("empty_confirmed_at")
    fresh_empty_snapshot = empty_at is not None and now - _parse_time(empty_at, "empty snapshot") <= SNAPSHOT_FRESHNESS
    fresh_natural_input = state_fresh and (
        any(event["status"] == "active" for event in state["natural_events"]) or bool(new_pending)
    )
    # A stale input must not erase valid targets from the other input. A known
    # empty successful collection is also usable; a partial failure alone is not.
    refresh_only = latest is None or not (fresh_records or fresh_natural_input or fresh_empty_snapshot)
    if refresh_only:
        targets = {}
        if latest is None:
            warnings.append("snapshot_missing")
        else:
            warnings.append("snapshot_refresh_required")
    warnings = list(dict.fromkeys(warnings))
    sorted_targets = sorted(
        targets.values(),
        key=lambda item: (
            0 if item["severity"] == "urgent" else 1,
            item["display_time"], item["course_name"], item["section"], item["identity"],
        ),
    )
    selected_at = latest["started_at"].astimezone(KST).isoformat(timespec="seconds") if latest else None
    snapshot = {
        "status": "missing" if latest is None else "stale" if refresh_only else "fresh_with_warnings" if warnings else "fresh",
        "selected_run_id": latest["id"] if latest else None,
        "selected_at": selected_at,
        "state_run_id": state["last_processed_run_id"],
        "state_at": state_at.isoformat(timespec="seconds") if state_at else None,
        "latest_overall_status": (root_status or {}).get("overall_status"),
        "fresh_record_count": fresh_records,
        "stale_course_ids": sorted(stale_courses),
    }
    payloads = _payloads(
        now, snapshot, sorted_targets, warnings, user_pending, new_pending,
        refresh_only=refresh_only,
    )
    counts = {
        "separator": 0,
        "urgent": sum(item["severity"] == "urgent" for item in sorted_targets),
        "general": sum(item["severity"] == "general" for item in sorted_targets),
        "excluded": len(excluded),
        "pending_total": len(user_pending),
        "new_pending": len(new_pending),
        "payloads": len(payloads),
        "sent": 0,
        "failed": 0,
    }
    delivery = {
        "schema_version": NTFY_SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "snapshot": snapshot,
        "targets": sorted_targets,
        "excluded": sorted(excluded, key=lambda item: (item["identity"], item["reason"])),
        "payloads": payloads,
        "deliveries": [],
        "warnings": warnings,
        "counts": counts,
    }
    _validate_delivery(delivery, allow_results=False)
    return delivery


def _validate_delivery(value: Mapping[str, Any], *, allow_results: bool) -> None:
    keys = {
        "schema_version", "generated_at", "snapshot", "targets", "excluded",
        "payloads", "deliveries", "warnings", "counts",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _invalid("ntfy_delivery_invalid", "ntfy delivery key가 schema와 일치하지 않습니다.")
    if value["schema_version"] != NTFY_SCHEMA_VERSION:
        _invalid("ntfy_delivery_invalid", "ntfy delivery schema version이 올바르지 않습니다.")
    if not all(isinstance(value[key], list) for key in ("targets", "excluded", "payloads", "deliveries", "warnings")):
        _invalid("ntfy_delivery_invalid", "ntfy delivery 목록 field가 올바르지 않습니다.")
    if value["deliveries"] and not allow_results:
        _invalid("ntfy_delivery_invalid", "준비된 delivery에는 전송 결과가 있을 수 없습니다.")
    target_keys = {
        "identity", "course_id", "course_name", "section", "record_id", "title",
        "severity", "reasons", "display_time", "completion", "source_record_ids",
    }
    target_ids: set[str] = set()
    for target in value["targets"]:
        if not isinstance(target, Mapping) or set(target) != target_keys:
            _invalid("ntfy_delivery_invalid", "ntfy target key가 schema와 일치하지 않습니다.")
        if any(
            not isinstance(target[key], str) or not target[key]
            for key in (
                "identity", "course_id", "course_name", "section", "record_id",
                "title", "severity", "display_time", "completion",
            )
        ):
            _invalid("ntfy_delivery_invalid", "ntfy target 문자열 field가 올바르지 않습니다.")
        if target["identity"] in target_ids or target["severity"] not in ALERT_SEVERITIES:
            _invalid("ntfy_delivery_invalid", "ntfy target identity 또는 severity가 올바르지 않습니다.")
        target_ids.add(target["identity"])
        if not isinstance(target["reasons"], list) or any(
            reason not in REASON_ORDER for reason in target["reasons"]
        ):
            _invalid("ntfy_delivery_invalid", "ntfy target reason이 올바르지 않습니다.")
        if target["reasons"] != [reason for reason in REASON_ORDER if reason in target["reasons"]]:
            _invalid("ntfy_delivery_invalid", "ntfy target reason 순서가 올바르지 않습니다.")
        if not isinstance(target["source_record_ids"], list) or any(
            not isinstance(source, str) or not source for source in target["source_record_ids"]
        ) or len(target["source_record_ids"]) != len(set(target["source_record_ids"])):
            _invalid("ntfy_delivery_invalid", "ntfy target source가 중복됩니다.")
    for excluded in value["excluded"]:
        if not isinstance(excluded, Mapping) or set(excluded) != {"identity", "reason"}:
            _invalid("ntfy_delivery_invalid", "ntfy excluded item이 올바르지 않습니다.")
    if any(not isinstance(warning, str) or not warning for warning in value["warnings"]):
        _invalid("ntfy_delivery_invalid", "ntfy warning이 올바르지 않습니다.")
    seen_parts: set[tuple[str, int]] = set()
    group_parts: dict[str, tuple[int, set[int]]] = {}
    for item in value["payloads"]:
        if not isinstance(item, Mapping) or set(item) != {
            "group", "part", "parts", "item_ids", "payload"
        }:
            _invalid("ntfy_delivery_invalid", "ntfy payload wrapper가 올바르지 않습니다.")
        if not isinstance(item["group"], str) or item["group"] not in PAYLOAD_GROUPS:
            _invalid("ntfy_delivery_invalid", "ntfy payload group이 올바르지 않습니다.")
        payload = item["payload"]
        expected_payload_keys = {"topic", "message", "title", "tags", "priority"}
        if not isinstance(payload, Mapping) or set(payload) != expected_payload_keys:
            _invalid("ntfy_delivery_invalid", "ntfy JSON payload가 올바르지 않습니다.")
        if payload["topic"] != NTFY_TOPIC:
            _invalid("ntfy_delivery_invalid", "ntfy topic 또는 priority가 올바르지 않습니다.")
        if payload["priority"] not in {3, 4, 5}:
            _invalid("ntfy_delivery_invalid", "ntfy priority가 올바르지 않습니다.")
        if not isinstance(payload["title"], str) or len(payload["title"].encode("utf-8")) > 1024:
            _invalid("ntfy_delivery_invalid", "ntfy title이 올바르지 않습니다.")
        if not isinstance(payload["tags"], list) or any(not isinstance(tag, str) for tag in payload["tags"]):
            _invalid("ntfy_delivery_invalid", "ntfy tags가 올바르지 않습니다.")
        if len(",".join(payload["tags"]).encode("utf-8")) > 512:
            _invalid("ntfy_delivery_invalid", "ntfy tags가 제한을 넘습니다.")
        expected_title, expected_priority, expected_tags = PAYLOAD_STYLES[item["group"]]
        if (
            payload["priority"] != expected_priority
            or payload["tags"] != expected_tags
            or not (
                payload["title"] == expected_title
                or payload["title"].startswith(expected_title + " (")
                or payload["title"].startswith(expected_title + " · ")
            )
        ):
            _invalid("ntfy_delivery_invalid", "ntfy payload style이 group과 일치하지 않습니다.")
        if not isinstance(payload["message"], str) or len(payload["message"].encode("utf-8")) > NTFY_MESSAGE_LIMIT:
            _invalid("ntfy_delivery_invalid", "ntfy message가 4,096바이트를 넘습니다.")
        if (
            item["group"] not in PAYLOAD_GROUPS
            or isinstance(item["part"], bool)
            or not isinstance(item["part"], int)
            or isinstance(item["parts"], bool)
            or not isinstance(item["parts"], int)
            or not 1 <= item["part"] <= item["parts"]
            or not isinstance(item["item_ids"], list)
            or any(not isinstance(identity, str) or not identity for identity in item["item_ids"])
        ):
            _invalid("ntfy_delivery_invalid", "ntfy payload part metadata가 올바르지 않습니다.")
        part_key = (item["group"], item["part"])
        if part_key in seen_parts:
            _invalid("ntfy_delivery_invalid", "ntfy payload part가 중복됩니다.")
        seen_parts.add(part_key)
        total, seen = group_parts.setdefault(item["group"], (item["parts"], set()))
        if total != item["parts"]:
            _invalid("ntfy_delivery_invalid", "ntfy payload parts 합계가 일치하지 않습니다.")
        seen.add(item["part"])
    if any(seen != set(range(1, total + 1)) for total, seen in group_parts.values()):
        _invalid("ntfy_delivery_invalid", "ntfy payload part가 누락되었습니다.")

    count_keys = {
        "separator", "urgent", "general", "excluded", "pending_total", "new_pending",
        "payloads", "sent", "failed",
    }
    counts = value["counts"]
    if not isinstance(counts, Mapping) or set(counts) != count_keys:
        _invalid("ntfy_delivery_invalid", "ntfy counts key가 schema와 일치하지 않습니다.")
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts.values()):
        _invalid("ntfy_delivery_invalid", "ntfy count가 올바르지 않습니다.")
    if (
        counts["separator"] != 0
        or counts["separator"] != sum(item["group"] == "separator" for item in value["payloads"])
        or not value["payloads"]
        or value["payloads"][0]["part"] != 1
        or counts["urgent"] != sum(target["severity"] == "urgent" for target in value["targets"])
        or counts["general"] != sum(target["severity"] == "general" for target in value["targets"])
        or counts["excluded"] != len(value["excluded"])
        or counts["payloads"] != len(value["payloads"])
    ):
        _invalid("ntfy_delivery_invalid", "ntfy 대상 count가 내용과 일치하지 않습니다.")
    if allow_results:
        delivery_keys = {
            "group", "part", "status", "http_status", "error_code",
            "response_id", "response_time", "item_ids",
        }
        if len(value["deliveries"]) != len(value["payloads"]):
            _invalid("ntfy_delivery_invalid", "ntfy 전송 결과 수가 payload와 다릅니다.")
        for result in value["deliveries"]:
            if not isinstance(result, Mapping) or set(result) != delivery_keys:
                _invalid("ntfy_delivery_invalid", "ntfy 전송 결과가 올바르지 않습니다.")
            if result["status"] not in {"sent", "failed"}:
                _invalid("ntfy_delivery_invalid", "ntfy 전송 상태가 올바르지 않습니다.")
        if (
            counts["sent"] != sum(item["status"] == "sent" for item in value["deliveries"])
            or counts["failed"] != sum(item["status"] == "failed" for item in value["deliveries"])
        ):
            _invalid("ntfy_delivery_invalid", "ntfy 전송 count가 결과와 일치하지 않습니다.")
    elif counts["sent"] or counts["failed"]:
        _invalid("ntfy_delivery_invalid", "미전송 delivery의 결과 count는 0이어야 합니다.")


class _NtfyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        if urllib.parse.urlsplit(newurl)[:2] != urllib.parse.urlsplit(NTFY_URL)[:2]:
            raise urllib.error.HTTPError(req.full_url, 470, "cross-origin redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibNtfyTransport:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(_NtfyRedirectHandler())

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> tuple[int, Mapping[str, str], bytes]:
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return (
                    int(response.status),
                    {key.lower(): value for key, value in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            response_body = exc.read() if exc.fp is not None else b""
            response_headers = {
                key.lower(): value for key, value in exc.headers.items()
            } if exc.headers else {}
            return int(exc.code), response_headers, response_body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HylmsError("ntfy_transport_error", "ntfy에 연결하지 못했습니다.") from exc


def send_ntfy_delivery(
    delivery: Mapping[str, Any],
    *,
    transport: Any | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send each prepared payload once; never retry, queue, or write local state."""
    _validate_delivery(delivery, allow_results=False)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        _invalid("ntfy_delivery_invalid", "ntfy timeout이 올바르지 않습니다.")
    if not NTFY_TOPIC:
        raise HylmsError("ntfy_not_configured", "Configure your own notification topic first")
    client = transport or UrllibNtfyTransport()
    result = copy.deepcopy(dict(delivery))
    deliveries: list[dict[str, Any]] = []
    for item in result["payloads"]:
        body = json.dumps(item["payload"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response_id = None
        response_time = None
        try:
            status, _, response_body = client.request(
                "POST",
                NTFY_URL,
                {"Content-Type": "application/json", "User-Agent": "hylms-ntfy/1"},
                body,
                float(timeout),
            )
            success = 200 <= status <= 299
            error_code = None if success else f"ntfy_http_{status}"
            if success and response_body:
                try:
                    response = json.loads(response_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response = None
                if isinstance(response, dict):
                    response_id = response.get("id")
                    response_time = response.get("time")
        except HylmsError as exc:
            status, success, error_code = None, False, exc.code
        deliveries.append(
            {
                "group": item["group"],
                "part": item["part"],
                "status": "sent" if success else "failed",
                "http_status": status,
                "error_code": error_code,
                "response_id": response_id,
                "response_time": response_time,
                "item_ids": copy.deepcopy(item["item_ids"]),
            }
        )
    result["deliveries"] = deliveries
    result["counts"]["sent"] = sum(item["status"] == "sent" for item in deliveries)
    result["counts"]["failed"] = sum(item["status"] == "failed" for item in deliveries)
    _validate_delivery(result, allow_results=True)
    return result


def acceptance_delivery(sample: str, *, now: dt.datetime) -> dict[str, Any]:
    if sample not in {"urgent", "general", "empty"}:
        _invalid("ntfy_input_invalid", "acceptance sample이 올바르지 않습니다.")
    now = _normalize_now(now)
    group = sample
    title, priority, tags = PAYLOAD_STYLES[group]
    messages = {
        "urgent": "[오늘 마감] Build #10 긴급 수신 표본",
        "general": "[내일 일정] Build #10 일반 수신 표본",
        "empty": "알림 대상 없음 · Build #10 수신 표본",
    }
    targets = [] if sample == "empty" else [
        _target(
            f"acceptance:{sample}", "acceptance", "Build #10", "acceptance",
            sample, "수신 표본", sample, ["today_due"] if sample == "urgent" else ["natural_tomorrow"],
            now.isoformat(timespec="seconds"), "incomplete", [f"acceptance:{sample}"],
        )
    ]
    payload = {
        "group": group,
        "part": 1,
        "parts": 1,
        "item_ids": [] if sample == "empty" else [f"acceptance:{sample}"],
        "payload": {
            "topic": NTFY_TOPIC,
            "message": messages[sample],
            "title": title,
            "tags": tags,
            "priority": priority,
        },
    }
    delivery = {
        "schema_version": NTFY_SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "snapshot": {"status": "acceptance_sample", "sample": sample},
        "targets": targets,
        "excluded": [],
        "payloads": [payload],
        "deliveries": [],
        "warnings": [],
        "counts": {
            "separator": 0,
            "urgent": int(sample == "urgent"),
            "general": int(sample == "general"),
            "excluded": 0,
            "pending_total": 0,
            "new_pending": 0,
            "payloads": 1,
            "sent": 0,
            "failed": 0,
        },
    }
    _validate_delivery(delivery, allow_results=False)
    return delivery


def _load_state(path: Path) -> dict[str, Any]:
    return _read_json(path, "ntfy_state_invalid", "Phase 2 state를 읽을 수 없습니다.")


def main(
    argv: Sequence[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(KST),
    transport: Any | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="py -m hylms.ntfy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "send"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--term-dir", type=Path, required=True)
        subparser.add_argument("--state", type=Path, required=True)
    acceptance = subparsers.add_parser("acceptance")
    acceptance.add_argument("--sample", choices=("urgent", "general", "empty"), required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "acceptance":
            delivery = acceptance_delivery(args.sample, now=clock())
            result = send_ntfy_delivery(delivery, transport=transport)
        else:
            state = _load_state(args.state)
            delivery = prepare_ntfy_delivery(args.term_dir, state, now=clock())
            result = delivery if args.command == "plan" else send_ntfy_delivery(
                delivery, transport=transport
            )
        out(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2 if result["counts"]["failed"] else 0
    except HylmsError as exc:
        out(f"[{exc.code}] {exc.message}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
