"""Common four-boundary schedules and non-inferential summary states."""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Sequence

from .core import KST, kst_iso, parse_iso_datetime

def _boundary(source: Mapping[str, Any], key: str) -> dict[str, Any]:
    if key not in source:
        return {"value": None, "state": "provider_omitted"}
    if source.get(key) is None:
        return {"value": None, "state": "unbounded"}
    try:
        return {"value": kst_iso(source.get(key)), "state": "known"}
    except ValueError:
        return {"value": None, "state": "unknown"}


def _schedule_window(source: Mapping[str, Any]) -> dict[str, Any]:
    opens = _boundary(source, "unlock_at")
    due = _boundary(source, "due_at")
    closes = _boundary(source, "lock_at")
    if due["state"] in {"unbounded", "provider_omitted"}:
        late = {"value": None, "state": "not_applicable"}
    elif closes["state"] == "known":
        late = {"value": closes["value"], "state": "same_as_closes_at"}
    elif closes["state"] == "unbounded":
        late = {"value": None, "state": "unbounded"}
    else:
        late = {"value": None, "state": closes["state"]}
    return {"opens_at": opens, "due_at": due, "late_until_at": late, "closes_at": closes}


def _same_window(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("unlock_at", "due_at", "lock_at"))


def normalize_schedule(
    source: Mapping[str, Any],
    all_dates: Sequence[Mapping[str, Any]] | None,
    user_id: str | None,
    *,
    restricted: bool = False,
) -> dict[str, Any]:
    if restricted:
        restricted_window = {
            key: {"value": None, "state": "restricted"}
            for key in ("opens_at", "due_at", "late_until_at", "closes_at")
        }
        return {"effective": restricted_window, "default": None, "basis": "unknown"}
    dates = list(all_dates or [])
    base = next((item for item in dates if item.get("base") is True), None)
    effective = _schedule_window(source)
    default = _schedule_window(base) if base is not None else None
    matching_overrides = [item for item in dates if item is not base and _same_window(source, item)]
    user_match = next(
        (
            item
            for item in matching_overrides
            if user_id
            and (
                str(item.get("student_id")) == user_id
                or user_id in [str(value) for value in item.get("student_ids", [])]
            )
        ),
        None,
    )
    section_match = next(
        (
            item
            for item in matching_overrides
            if item.get("course_section_id") is not None or item.get("section_id") is not None
        ),
        None,
    )
    if user_match is not None:
        basis = "user_override"
    elif section_match is not None:
        basis = "section_override"
    elif base is not None and _same_window(source, base):
        basis = "course_default"
    elif matching_overrides or base is not None:
        basis = "provider_effective"
    else:
        basis = "provider_effective" if any(key in source for key in ("unlock_at", "due_at", "lock_at")) else "unknown"
    return {"effective": effective, "default": default, "basis": basis}


def normalize_discussion_schedule(
    topic: Mapping[str, Any], assignment: Mapping[str, Any] | None, user_id: str
) -> dict[str, Any]:
    if assignment is not None:
        return normalize_schedule(assignment, assignment.get("all_dates"), user_id)
    source: dict[str, Any] = {}
    if "delayed_post_at" in topic or "posted_at" in topic:
        source["unlock_at"] = topic.get("delayed_post_at") or topic.get("posted_at")
    if "lock_at" in topic:
        source["lock_at"] = topic.get("lock_at")
    schedule = normalize_schedule(source, None, user_id)
    schedule["effective"]["due_at"] = {"value": None, "state": "not_applicable"}
    schedule["effective"]["late_until_at"] = {"value": None, "state": "not_applicable"}
    schedule["default"] = None
    return schedule


def _known_time(boundary: Mapping[str, Any]) -> dt.datetime | None:
    if boundary.get("state") not in {"known", "same_as_closes_at"} or not boundary.get("value"):
        return None
    try:
        return parse_iso_datetime(boundary["value"])
    except ValueError:
        return None


def evaluate_summary(
    schedule: Mapping[str, Any], submission: Mapping[str, Any] | None, now: dt.datetime
) -> dict[str, Any]:
    evaluated = now.astimezone(KST)
    window = schedule.get("effective") or {}
    opens = _known_time(window.get("opens_at") or {})
    due = _known_time(window.get("due_at") or {})
    late_until = _known_time(window.get("late_until_at") or {})
    closes = _known_time(window.get("closes_at") or {})
    submitted = bool(
        submission
        and (
            submission.get("submitted_at")
            or submission.get("finished_at")
            or submission.get("workflow_state") in {"submitted", "graded", "complete"}
        )
    )
    if submitted and submission and submission.get("late") is True:
        state = "late"
    elif submitted and submission and submission.get("late") is False:
        state = "present"
    elif closes and evaluated > closes:
        state = "closed_absent" if submission and submission.get("missing") is True else "closed_unconfirmed"
    elif opens and evaluated < opens:
        state = "not_open"
    elif due is None or evaluated <= due:
        state = "open_on_time" if any((opens, due, closes)) else "unknown"
    elif late_until and evaluated <= late_until:
        state = "open_late"
    elif (window.get("late_until_at") or {}).get("state") == "unbounded":
        state = "open_late"
    elif closes is None or evaluated <= closes:
        state = "open_after_due"
    else:
        state = "unknown"

    transitions: list[tuple[dt.datetime, str]] = []
    if opens and evaluated < opens:
        transitions.append((opens, "open_on_time"))
    if due and evaluated < due:
        transitions.append((due, "open_late" if late_until or closes else "closed_unconfirmed"))
    if late_until and evaluated < late_until and (closes is None or late_until < closes):
        transitions.append((late_until, "open_after_due"))
    if closes and evaluated < closes:
        transitions.append((closes, "closed_unconfirmed"))
    next_transition = min(transitions, default=None, key=lambda item: item[0])
    return {
        "state": state,
        "evaluated_at": evaluated.isoformat(timespec="seconds"),
        "next_transition_at": next_transition[0].astimezone(KST).isoformat(timespec="seconds") if next_transition else None,
        "next_state": next_transition[1] if next_transition else None,
    }
