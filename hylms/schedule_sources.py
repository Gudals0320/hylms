"""Shared, read-only snapshot and schedule projection for output engines."""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any, Mapping

from .core import HylmsError, KST
from .diff import discover_runs
from .participation import normalize_discussion_participation

SECTION_KEYS = {
    "assignment": "assignments",
    "quiz": "quizzes",
    "discussion": "discussions",
    "weekly_learning": "weekly_learning",
}

def _invalid(code: str, message: str) -> None:
    raise HylmsError(code, message)


def _parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or "T" not in value:
        _invalid("schedule_input_invalid", f"{label} 시각이 올바르지 않습니다.")
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            _invalid("schedule_input_invalid", f"{label} 시각에 timezone이 없습니다.")
        return parsed.astimezone(KST)
    except (ValueError, OverflowError):
        _invalid("schedule_input_invalid", f"{label} 시각이 올바르지 않습니다.")


def _safe_relative_json(root: Path, relative: Any, label: str) -> Path:
    root = root.resolve()
    relative_path = Path(str(relative or ""))
    path = (root / relative_path).resolve()
    if path.parent != root or path.suffix.lower() != ".json":
        _invalid("schedule_snapshot_invalid", f"안전하지 않은 {label} 경로입니다.")
    return path


def _read_json(path: Path, code: str, message: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HylmsError(code, message) from exc
    if not isinstance(value, dict):
        _invalid(code, message)
    return value


def _record_usable(item: Mapping[str, Any]) -> bool:
    return item.get("detail_state") != "unavailable"


def _load_logical_records(term_directory: Path) -> dict[str, Any]:
    runs = discover_runs(term_directory)
    if not runs:
        return {"runs": [], "records": {}, "course_times": {}, "latest_run": None, "empty_confirmed_at": None}
    records: dict[str, dict[str, Any]] = {}
    course_times: dict[str, str] = {}
    source_urls: dict[str, str] = {}
    empty_confirmed_at: str | None = None
    for run in runs:
        run_root = Path(run["path"]).resolve()
        course_statuses = run["status"].get("courses")
        if not isinstance(course_statuses, list):
            _invalid("schedule_snapshot_invalid", "snapshot run의 과목 목록이 올바르지 않습니다.")
        if run["status"].get("overall_status") == "no_courses":
            if course_statuses:
                _invalid("schedule_snapshot_invalid", "no_courses run의 과목 목록이 비어 있지 않습니다.")
            # A successful empty result supersedes historical structured records.
            # Keep source URLs for confirmed natural events whose sources disappeared.
            records.clear()
            course_times.clear()
            empty_confirmed_at = run["started_at"].isoformat(timespec="seconds")
            continue
        complete_run = run["status"].get("overall_status") == "success" and all(
            course.get("status") == "updated" for course in course_statuses
        )
        for course_status in course_statuses:
            course_id = str(course_status.get("id"))
            status = course_status.get("status")
            if status not in {"updated", "updated_with_warnings"}:
                continue
            path = _safe_relative_json(run_root, course_status.get("path"), "과목 snapshot")
            if not path.is_file():
                _invalid("schedule_snapshot_invalid", "과목 snapshot 파일이 없습니다.")
            data = _read_json(path, "schedule_snapshot_invalid", "과목 snapshot을 읽을 수 없습니다.")
            course = data.get("course")
            if not isinstance(course, dict) or str(course.get("id")) != course_id:
                _invalid("schedule_snapshot_invalid", "과목 snapshot identity가 일치하지 않습니다.")
            for section, key in {**SECTION_KEYS, "announcement": "announcements"}.items():
                values = data.get(key) or []
                if not isinstance(values, list):
                    _invalid("schedule_snapshot_invalid", f"{key} section이 목록이 아닙니다.")
                for item in values:
                    if not isinstance(item, dict) or item.get("id") is None:
                        _invalid("schedule_snapshot_invalid", f"{key} record가 올바르지 않습니다.")
                    if isinstance(item.get("source_url"), str):
                        source_urls[f"{course_id}:{section}:{item['id']}"] = item["source_url"]
            success_at = course_status.get("last_success_at") or course.get("last_success_at")
            success_time = _parse_time(success_at, "course last_success_at")
            if status == "updated":
                records = {
                    key: value for key, value in records.items()
                    if value["course_id"] != course_id
                }
            for section, key in SECTION_KEYS.items():
                values = data.get(key) or []
                if not isinstance(values, list):
                    _invalid("schedule_snapshot_invalid", f"{key} section이 목록이 아닙니다.")
                seen: set[str] = set()
                for item in values:
                    if not isinstance(item, dict) or item.get("id") is None:
                        _invalid("schedule_snapshot_invalid", f"{key} record가 올바르지 않습니다.")
                    record_id = str(item["id"])
                    if record_id in seen:
                        _invalid("schedule_snapshot_invalid", f"{key} record ID가 중복됩니다.")
                    seen.add(record_id)
                    if not _record_usable(item):
                        complete_run = False
                        continue
                    identity = f"{course_id}:{section}:{record_id}"
                    records[identity] = {
                        "identity": identity,
                        "course_id": course_id,
                        "course_name": str(course.get("name") or course_id),
                        "section": section,
                        "record_id": record_id,
                        "item": copy.deepcopy(item),
                        "usable_at": success_time.isoformat(timespec="seconds"),
                        "run_id": run["id"],
                    }
            course_times[course_id] = success_time.isoformat(timespec="seconds")
        if records:
            empty_confirmed_at = None
        elif complete_run:
            empty_confirmed_at = run["started_at"].isoformat(timespec="seconds")
    return {
        "runs": runs,
        "records": records,
        "course_times": course_times,
        "source_urls": source_urls,
        "empty_confirmed_at": empty_confirmed_at,
        "latest_run": runs[-1],
    }


def _root_status(term_directory: Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = term_directory / "status.json"
    if not path.is_file():
        return None, ["snapshot_status_missing"]
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["snapshot_status_invalid"]
    if not isinstance(status, dict):
        return None, ["snapshot_status_invalid"]
    warnings: list[str] = []
    overall = status.get("overall_status")
    if overall not in {"success", "no_courses"}:
        warnings.append(f"snapshot_{overall or 'unknown'}")
    for item in status.get("courses") or []:
        if item.get("status") in {"updated_with_warnings", "kept", "failed"}:
            warnings.append(f"course_{item.get('id')}_{item.get('status')}")
    return status, list(dict.fromkeys(warnings))


def _application_overrides(
    state: Mapping[str, Any], warnings: list[str]
) -> dict[tuple[str, str], dict[str, Any]]:
    collected: dict[tuple[str, str], dict[str, list[Any]]] = {}
    for application in state["announcement_applications"]:
        course_id = application["course_id"]
        patch = application["patch"]
        for target in application["target_record_ids"]:
            fields = collected.setdefault((course_id, target), {})
            if patch.get("timing") is not None:
                fields.setdefault("timing", []).append(copy.deepcopy(patch["timing"]))
            if patch.get("optional") is not None:
                fields.setdefault("optional", []).append(patch["optional"])
    overrides: dict[tuple[str, str], dict[str, Any]] = {}
    for target, fields in collected.items():
        resolved: dict[str, Any] = {}
        for field, values in fields.items():
            unique = {json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values}
            if len(unique) == 1:
                resolved[field] = values[0]
            else:
                warnings.append(f"application_conflict:{target[0]}:{target[1]}:{field}")
        overrides[target] = resolved
    return overrides


def _known_boundary(schedule: Any, name: str) -> str | None:
    if not isinstance(schedule, Mapping):
        return None
    boundary = (schedule.get("effective") or {}).get(name)
    known_states = {"known", "same_as_closes_at"} if name == "late_until_at" else {"known"}
    if not isinstance(boundary, Mapping) or boundary.get("state") not in known_states:
        return None
    value = boundary.get("value")
    _parse_time(value, name)
    return value


def _base_schedule(item: Mapping[str, Any], *, calendar: bool = False) -> dict[str, str | None]:
    schedule = item.get("schedule")
    # Validate all known provider boundaries before any override or wrapper merge.
    result = {
        name: _known_boundary(schedule, name)
        for name in ("opens_at", "due_at", "late_until_at", "closes_at")
    }
    return result if calendar else {name: result[name] for name in ("opens_at", "due_at")}


def _apply_override(
    schedule: dict[str, str | None], optional: bool, override: Mapping[str, Any]
) -> tuple[dict[str, str | None], bool]:
    result = copy.deepcopy(schedule)
    timing = override.get("timing")
    if isinstance(timing, Mapping):
        if timing.get("start") is not None:
            result["opens_at"] = timing["start"]
        if timing.get("end") is not None:
            result["due_at"] = timing["end"]
    return result, override.get("optional", optional)


def _completion(section: str, course_id: str, item: Mapping[str, Any]) -> tuple[str, str | None]:
    access = item.get("access") or {}
    restricted = access.get("state") in {"restricted", "unknown"} or item.get(
        "detail_state"
    ) in {"restricted", "not_available"}
    if section == "assignment":
        if item.get("informational") is True or item.get("submission_required") is not True:
            return "excluded", "not_actionable"
        progress = item.get("progress") or {}
        workflow = (item.get("submission") or {}).get("workflow_state") or progress.get(
            "workflow_state"
        )
        if progress.get("submitted") is True or workflow in {"submitted", "graded"}:
            return "done", None
        if restricted or workflow not in {"unsubmitted", None}:
            return "confirmation_required", "completion_unknown"
        if workflow == "unsubmitted" or progress.get("submitted") is False:
            return "incomplete", None
        return "confirmation_required", "completion_unknown"
    if section == "quiz":
        progress = item.get("progress") or {}
        workflow = (item.get("submission") or {}).get("workflow_state") or progress.get(
            "workflow_state"
        )
        if workflow in {"complete", "submitted", "graded"}:
            return "done", None
        if restricted or workflow is None:
            return "confirmation_required", "completion_unknown"
        return "incomplete", None
    if section == "discussion":
        participation = normalize_discussion_participation(course_id, item)
        state = participation["state"]
        if state == "participated":
            return "done", None
        if state == "not_participated":
            return "incomplete", None
        if state == "not_evaluated":
            return "excluded", "not_evaluated"
        return "confirmation_required", "completion_unknown"
    progress = item.get("progress") or {}
    attendance = item.get("attendance") or {}
    if progress.get("completed") is True or attendance.get("status") in {"present", "late"}:
        return "done", None
    if restricted or progress.get("state") != "known":
        return "confirmation_required", "completion_unknown"
    if progress.get("completed") is False or attendance.get("status") in {"absent", "none"}:
        return "incomplete", None
    return "confirmation_required", "completion_unknown"


def _structured_items(
    logical: Mapping[str, Any], state: Mapping[str, Any], warnings: list[str],
    *, calendar: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    overrides = _application_overrides(state, warnings)
    canonical: dict[str, dict[str, Any]] = {}
    weekly: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for record in logical["records"].values():
        item = record["item"]
        qualified = f"{record['section']}:{record['record_id']}"
        schedule, optional = _apply_override(
            _base_schedule(item, calendar=calendar), bool(item.get("optional")),
            overrides.get((record["course_id"], qualified), {}),
        )
        completion, reason = _completion(record["section"], record["course_id"], item)
        normalized = {
            "identity": record["identity"],
            "course_id": record["course_id"],
            "course_name": record["course_name"],
            "section": record["section"],
            "record_id": record["record_id"],
            "title": str(item.get("title") or record["record_id"]),
            "schedule": schedule,
            "optional": optional,
            "completion": completion,
            "usable_at": record["usable_at"],
            "source_record_ids": [qualified],
            "timing_overridden": "timing" in overrides.get(
                (record["course_id"], qualified), {}
            ),
            "optional_overridden": "optional" in overrides.get(
                (record["course_id"], qualified), {}
            ),
        }
        if calendar:
            normalized["kind"] = item.get("kind")
            normalized["source_urls"] = [item["source_url"]] if item.get("source_url") else []
        if completion == "excluded":
            excluded.append({"identity": record["identity"], "reason": reason})
        if record["section"] == "weekly_learning":
            normalized["linked_entity"] = copy.deepcopy(item.get("linked_entity") or {})
            weekly.append(normalized)
        else:
            canonical[record["identity"]] = normalized

    for item in weekly:
        linked = item.get("linked_entity") or {}
        if linked.get("state") == "linked" and linked.get("kind") in {
            "assignment", "quiz", "discussion"
        }:
            target_id = f"{item['course_id']}:{linked['kind']}:{linked.get('id')}"
            target = canonical.get(target_id)
            if target is not None:
                if item["timing_overridden"] or (
                    not target["timing_overridden"] and any(item["schedule"].get(key) for key in ("opens_at", "due_at"))
                ):
                    # A partial wrapper supplies only its known boundaries; it cannot
                    # erase the canonical item's valid opening/deadline with missing data.
                    target["schedule"].update({
                        key: value for key, value in item["schedule"].items() if value is not None
                    })
                    target["timing_overridden"] = item["timing_overridden"]
                if item["optional_overridden"]:
                    target["optional"] = item["optional"]
                    target["optional_overridden"] = True
                elif not target["optional_overridden"]:
                    target["optional"] = target["optional"] or item["optional"]
                if calendar:
                    target["source_urls"] = list(dict.fromkeys([*target["source_urls"], *item["source_urls"]]))
                target["usable_at"] = min(target["usable_at"], item["usable_at"])
                target["source_record_ids"] = list(dict.fromkeys([
                    *target["source_record_ids"], *item["source_record_ids"],
                ]))
                continue
            warnings.append(f"linked_record_missing:{item['identity']}:{target_id}")
            if calendar:
                # Keep the canonical UID even while only the provider wrapper is usable.
                item["identity"] = target_id
                item["section"] = linked["kind"]
                item["record_id"] = str(linked["id"])
        canonical[item["identity"]] = item
    return canonical, excluded


# Engine-facing interface. Private helpers remain implementation details.
load_logical_records = _load_logical_records
structured_items = _structured_items
root_status = _root_status
read_json = _read_json
