"""Read-only calendar projection and atomic, local RFC 5545 export."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .content import sanitize_url
from .core import HylmsError, KST
from .diff import validate_state, validate_state_references
from .schedule_sources import load_logical_records, read_json, root_status, structured_items

CALENDAR_SCHEMA_VERSION = 1
BOUNDARY_LABELS = {
    "opens_at": "공개", "due_at": "마감", "late_until_at": "지각 마감", "closes_at": "최종 종료",
}
COMPLETION_LABELS = {
    "done": "완료", "incomplete": "미완료", "confirmation_required": "완료 확인 필요",
    "unknown": "완료 확인 필요", "not_applicable": "해당 없음", "excluded": "판정 대상 아님",
}


def _invalid(message: str) -> None:
    raise HylmsError("ics_input_invalid", message)


def _time(value: Any) -> dt.datetime:
    if not isinstance(value, str) or "T" not in value:
        _invalid("시각 일정은 timezone이 있는 날짜·시간이어야 합니다.")
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(KST)
    except (ValueError, OverflowError) as exc:
        raise HylmsError("ics_input_invalid", "일정 시각이 올바르지 않습니다.") from exc


def _date(value: Any) -> dt.date:
    if not isinstance(value, str) or len(value) != 10:
        _invalid("종일 일정은 YYYY-MM-DD 날짜여야 합니다.")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HylmsError("ics_input_invalid", "종일 날짜가 올바르지 않습니다.") from exc


def _uid(term: str, identity: str) -> str:
    key = json.dumps(["hylms-calendar-v1", term, identity], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest() + "@hylms.local"


def _urls(values: Sequence[str]) -> list[str]:
    result: set[str] = set()
    for value in values:
        # Source links only; never serialize userinfo, control characters or credential query parameters.
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            continue
        try:
            parsed = urllib.parse.urlsplit(value)
            if not parsed.hostname or parsed.username or parsed.password:
                continue
            safe = sanitize_url(value)
            if safe:
                result.add(safe)
        except ValueError:
            continue
    return sorted(result)


def _display_boundary(value: str | None) -> str:
    if value is None:
        return "미지정·확인 불가"
    if "T" not in value:
        return _date(value).isoformat() + " (시각 미지정)"
    return _time(value).strftime("%Y-%m-%d %H:%M:%S") + " (서울)"


def _event(
    term: str, identity: str, course_id: str, title: str, start: str, end: str | None,
    all_day: bool, location: str | None, description: list[str], sources: Sequence[str],
    urls: Sequence[str], *, completion: str,
) -> dict[str, Any]:
    links = _urls(urls)
    return {
        "identity": identity, "uid": _uid(term, identity), "course_id": course_id,
        "title": title, "start": start, "end": end, "all_day": all_day,
        "completion": completion,
        "location": location, "description": "\n".join([
            *description, *(f"LMS: {url}" for url in links),
        ]),
        "source_record_ids": sorted(set(sources)), "source_urls": links,
    }


def _prepare_calendar(term_directory: Path, state: Mapping[str, Any], now: dt.datetime) -> dict[str, Any]:
    validate_state(state)
    if state["term"] != term_directory.name:
        _invalid("state와 snapshot 학기가 일치하지 않습니다.")
    logical = load_logical_records(term_directory)
    latest = logical["latest_run"]
    if latest is None:
        raise HylmsError("ics_snapshot_missing", "사용 가능한 immutable snapshot run이 없습니다.")
    validate_state_references(state, term_directory)
    state_run = next((run for run in logical["runs"] if run["id"] == state["last_processed_run_id"]), None)
    if state_run is None:
        _invalid("state cursor run을 찾을 수 없습니다.")
    if not logical["course_times"] and latest["status"].get("overall_status") != "no_courses":
        raise HylmsError("ics_snapshot_missing", "정상 수집된 과목 snapshot이 없습니다.")
    _, warnings = root_status(term_directory)
    if latest["id"] != state["last_processed_run_id"]:
        warnings.append("state_cursor_behind")
    if state.get("last_failure"):
        warnings.append("state_last_failure")
    if now - state_run["started_at"] > dt.timedelta(hours=24):
        warnings.append("state_stale")
    for course_id, observed in logical["course_times"].items():
        if now - _time(observed) > dt.timedelta(hours=24):
            warnings.append(f"course_stale:{course_id}")
    for record in logical["records"].values():
        if now - _time(record["usable_at"]) > dt.timedelta(hours=24):
            warnings.append(f"record_stale:{record['identity']}")
    structured, _ = structured_items(logical, state, warnings, calendar=True)
    events: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    term = state["term"]
    for item in structured.values():
        identity = item["identity"]
        if item["section"] == "weekly_learning" and item["kind"] != "video":
            # A linked wrapper was already merged; standalone pages/PDFs are not video deadlines.
            excluded.append({"identity": identity, "reason": "not_calendar_content"})
            continue
        due = item["schedule"].get("due_at")
        if due is None:
            excluded.append({"identity": identity, "reason": "deadline_missing"})
            continue
        if "T" not in due:
            _date(due)
            excluded.append({"identity": identity, "reason": "deadline_time_unspecified"})
            warnings.append(f"deadline_time_unspecified:{identity}")
            continue
        start = _time(due)
        description = [
            f"{label}: {_display_boundary(item['schedule'].get(key))}"
            for key, label in BOUNDARY_LABELS.items()
        ]
        description.extend([
            f"완료 상태: {COMPLETION_LABELS[item['completion']]}",
            f"선택 여부: {'선택' if item['optional'] else '선택 표시 없음'}",
        ])
        events.append(_event(
            term, identity, item["course_id"], f"[마감] {item['course_name']} · {item['title']}",
            start.isoformat(timespec="seconds"), (start + dt.timedelta(minutes=1)).isoformat(timespec="seconds"),
            False, None, description, item["source_record_ids"], item["source_urls"], completion=item["completion"],
        ))
    for natural in state["natural_events"]:
        identity = f"natural:{natural['id']}"
        if natural["status"] == "cancelled":
            excluded.append({"identity": identity, "reason": "cancelled"})
            continue
        timing = natural["timing"]
        if timing["all_day"]:
            start = _date(timing["start"] or timing["end"]).isoformat()
            end = (_date(timing["end"]) + dt.timedelta(days=1)).isoformat()
        elif timing["start"] is None:
            start_time = _time(timing["end"])
            start = start_time.isoformat(timespec="seconds")
            end = (start_time + dt.timedelta(minutes=1)).isoformat(timespec="seconds")
        else:
            start = _time(timing["start"]).isoformat(timespec="seconds")
            end = _time(timing["end"]).isoformat(timespec="seconds")
            if start == end:
                end = None
        sources = natural["source_record_ids"]
        urls = [logical["source_urls"].get(f"{natural['course_id']}:{source}", "") for source in sources]
        description = [
            f"과목: {natural['course_name']}",
            f"완료 상태: {COMPLETION_LABELS[natural['action_state']]}",
            f"선택 여부: {'선택' if natural['optional'] else '선택 표시 없음'}",
        ]
        if natural["attendance_required"] is not None:
            description.append(f"출석: {'필수' if natural['attendance_required'] else '필수 아님'}")
        # Pending and free-form details are not interpreted or dumped into a portable calendar.
        events.append(_event(
            term, identity, natural["course_id"], natural["title"], start, end,
            timing["all_day"], natural["location"], description, sources, urls, completion=natural["action_state"],
        ))
    events.sort(key=lambda item: (item["start"], item["identity"]))
    result = {
        "schema_version": CALENDAR_SCHEMA_VERSION, "status": "planned", "term": term,
        "generated_at": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        "snapshot_run_id": latest["id"], "state_cursor": state["last_processed_run_id"],
        "events": events, "counts": {"events": len(events), "excluded": len(excluded), "pending": len(state["pending"])},
        "warnings": sorted(set(warnings)), "excluded": sorted(excluded, key=lambda item: item["identity"]),
        "output_path": str((term_directory / "HY-LMS.ics").resolve()), "error": None,
    }
    _validate_calendar(result)
    return result


def prepare_calendar(
    term_directory: Path, state: Mapping[str, Any], *, now: dt.datetime,
) -> dict[str, Any]:
    """Return calendar events and diagnostics without writes or network access."""
    if not isinstance(now, dt.datetime) or now.tzinfo is None or now.utcoffset() is None:
        _invalid("now는 timezone이 있는 datetime이어야 합니다.")
    try:
        return _prepare_calendar(Path(term_directory), state, now)
    except HylmsError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise HylmsError("ics_input_invalid", "캘린더 원본을 읽거나 변환할 수 없습니다.") from exc


def _validate_calendar(calendar: Mapping[str, Any]) -> None:
    try:
        if calendar["schema_version"] != CALENDAR_SCHEMA_VERSION or not isinstance(calendar["events"], list):
            _invalid("지원하지 않는 calendar projection입니다.")
        _time(calendar["generated_at"])
        if not isinstance(calendar["term"], str) or not calendar["term"]:
            _invalid("학기가 없습니다.")
        seen: set[str] = set()
        for event in calendar["events"]:
            for key in ("identity", "title", "course_id", "uid"):
                if not isinstance(event[key], str) or not event[key]:
                    _invalid("일정 identity·제목이 올바르지 않습니다.")
            if event["uid"] != _uid(calendar["term"], event["identity"]) or event["uid"] in seen:
                _invalid("일정 UID가 잘못되었거나 중복됩니다.")
            seen.add(event["uid"])
            if not isinstance(event["all_day"], bool) or not isinstance(event["description"], str):
                _invalid("일정 설명·종일 여부가 올바르지 않습니다.")
            # Legacy projections without completion remain valid; display text
            # is never sufficient evidence that an item is complete.
            if "completion" in event and event["completion"] not in COMPLETION_LABELS:
                _invalid("일정 완료 상태가 올바르지 않습니다.")
            if event["location"] is not None and not isinstance(event["location"], str):
                _invalid("장소가 올바르지 않습니다.")
            parse = _date if event["all_day"] else _time
            start = parse(event["start"])
            if event["end"] is not None and parse(event["end"]) <= start:
                _invalid("일정 종료는 시작보다 뒤여야 합니다.")
            if event["all_day"] and event["end"] is None:
                _invalid("종일 종료일이 없습니다.")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise HylmsError("ics_input_invalid", "calendar projection이 올바르지 않습니다.") from exc


def _text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        _invalid("일정 텍스트에 지원하지 않는 제어 문자가 있습니다.")
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _fold(line: str) -> bytes:
    physical: list[bytes] = []
    current = b""
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > 75:
            physical.append(current)
            current = b" "
        current += encoded
    physical.append(current)
    return b"\r\n".join(physical) + b"\r\n"


def render_ics(calendar: Mapping[str, Any]) -> bytes:
    """Serialize a validated projection; generated_at supplies UTC DTSTAMP."""
    _validate_calendar(calendar)
    stamp = _time(calendar["generated_at"]).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HY-LMS//Calendar 1.0//KO", "CALSCALE:GREGORIAN", "X-WR-CALNAME:HY-LMS"]
    timed = [event for event in calendar["events"] if not event["all_day"]]
    if timed:
        year = min(_time(event["start"]).year for event in timed)
        lines.extend([
            "BEGIN:VTIMEZONE", "TZID:Asia/Seoul", "BEGIN:STANDARD", f"DTSTART:{year:04d}0101T000000",
            "TZOFFSETFROM:+0900", "TZOFFSETTO:+0900", "TZNAME:KST", "END:STANDARD", "END:VTIMEZONE",
        ])
    for event in sorted(calendar["events"], key=lambda item: (item["start"], item["identity"])):
        lines.extend(["BEGIN:VEVENT", f"UID:{event['uid']}", f"DTSTAMP:{stamp}", f"SUMMARY:{_text(event['title'])}"])
        for key, name in (("start", "DTSTART"), ("end", "DTEND")):
            if event[key] is None:
                continue
            if event["all_day"]:
                lines.append(f"{name};VALUE=DATE:{_date(event[key]).strftime('%Y%m%d')}")
            else:
                lines.append(f"{name};TZID=Asia/Seoul:{_time(event[key]).strftime('%Y%m%dT%H%M%S')}")
        if event["location"]:
            lines.append(f"LOCATION:{_text(event['location'])}")
        lines.extend([f"DESCRIPTION:{_text(event['description'])}", "END:VEVENT"])
    lines.append("END:VCALENDAR")
    try:
        return b"".join(_fold(line) for line in lines)
    except UnicodeError as exc:
        raise HylmsError("ics_input_invalid", "일정 텍스트를 UTF-8로 표현할 수 없습니다.") from exc


def _existing_file(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
        return {"exists": True, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    except FileNotFoundError:
        return {"exists": False}
    except OSError:
        return {"exists": path.exists(), "readable": False}


def _output_path(path: Path) -> Path:
    path = path.resolve()
    if path.suffix.lower() != ".ics":
        _invalid("출력 파일 확장자는 .ics여야 합니다.")
    return path


def write_ics(calendar: Mapping[str, Any], output_path: Path | None = None) -> dict[str, Any]:
    """Atomically export; failure results preserve the existing file and do not block other engines."""
    path = Path(output_path) if output_path is not None else Path(calendar["output_path"])
    result = {key: calendar[key] for key in (
        "schema_version", "term", "generated_at", "snapshot_run_id", "state_cursor", "counts", "warnings", "excluded",
    )}
    result.update(status="failed", output_path=str(path.absolute()), error=None, previous_file=_existing_file(path), previous_preserved=False)
    temporary: str | None = None
    try:
        path = _output_path(path)
        result["output_path"] = str(path)
        content = render_ics(calendar)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        result.update(status="written", sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
    except HylmsError as exc:
        result["error"] = {"code": exc.code, "message": exc.message}
    except OSError:
        result["error"] = {"code": "ics_write_failed", "message": "ICS 파일을 저장하지 못했습니다."}
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                result["warnings"] = [*result["warnings"], "temporary_cleanup_failed"]
    if result["status"] == "failed":
        result["previous_preserved"] = bool(result["previous_file"]["exists"]) and _existing_file(path) == result["previous_file"]
    return result


def main(
    argv: Sequence[str] | None = None, *, out: Callable[[str], None] = print,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(KST),
) -> int:
    parser = argparse.ArgumentParser(prog="py -m hylms.ics")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "build"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--term-dir", type=Path, required=True)
        subparser.add_argument("--state", type=Path, required=True)
        subparser.add_argument("--output", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    output = args.output if args.output is not None else args.term_dir / "HY-LMS.ics"
    try:
        output = _output_path(output)
        state = read_json(args.state, "ics_state_invalid", "state를 읽을 수 없습니다.")
        result = prepare_calendar(args.term_dir, state, now=clock())
        result["output_path"] = str(output)
        render_ics(result)  # plan also verifies that its entire projection is serializable.
        if args.command == "build":
            result = write_ics(result, output)
    except HylmsError as exc:
        previous = _existing_file(output)
        result = {
            "schema_version": CALENDAR_SCHEMA_VERSION, "status": "failed", "output_path": str(output.absolute()),
            "error": {"code": exc.code, "message": exc.message}, "previous_file": previous,
            "previous_preserved": previous["exists"],
        }
    out(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["status"] == "failed":
        return 2 if result["error"]["code"] == "ics_write_failed" else 1
    return 0


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
