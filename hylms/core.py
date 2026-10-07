"""Shared constants, errors, time handling, and term selection."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

CANVAS_ORIGIN = "https://learning.hanyang.ac.kr"
CREDENTIAL_TARGET = "hylms-snapshot:learning.hanyang.ac.kr"
SCHEMA_VERSION = 5
KST = dt.timezone(dt.timedelta(hours=9))
DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".hwp",
    ".hwpx",
    ".xls",
    ".xlsx",
    ".txt",
}
SOURCE_NOT_COLLECTED = {"status": "not_collected"}

class HylmsError(Exception):
    """An expected, secret-safe application error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CanvasTransportError(HylmsError):
    pass


class CanvasHTTPError(HylmsError):
    def __init__(self, status: int, path: str, reason: str | None = None) -> None:
        super().__init__(f"canvas_http_{status}", f"Canvas 요청이 HTTP {status}로 실패했습니다: {path}")
        self.status = status
        self.path = path
        self.reason = reason


class CredentialStoreError(HylmsError):
    pass


class TermSelectionError(HylmsError):
    pass

def parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid datetime")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def kst_iso(value: str | dt.datetime | None) -> str | None:
    if value is None:
        return None
    parsed = parse_iso_datetime(value) if isinstance(value, str) else value
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST).isoformat(timespec="seconds")


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def extract_token_id(token: str) -> str | None:
    prefix, separator, _ = token.partition("~")
    return prefix if separator and prefix.isdigit() else None

TERM_RE = re.compile(r"^(\d{4})년\s*(1|2|여름|겨울)학기$")


@dataclass(frozen=True)
class TermSelection:
    canvas_id: str | None
    name: str
    term_id: str
    courses: list[Mapping[str, Any]]


def expected_term(now: dt.datetime) -> tuple[str, str]:
    local = now.astimezone(KST)
    if local.month <= 2:
        year, label, suffix = local.year - 1, "겨울", "W"
    elif local.month <= 6:
        year, label, suffix = local.year, "1", "1"
    elif local.month <= 8:
        year, label, suffix = local.year, "여름", "S"
    else:
        year, label, suffix = local.year, "2", "2"
    return f"{year}년 {label}학기", f"{year % 100:02d}-{suffix}"


def term_id_from_name(name: str) -> str:
    match = TERM_RE.fullmatch(name.strip())
    if not match:
        raise TermSelectionError("term_unknown", f"지원하지 않는 학기 이름입니다: {name.strip() or '(빈 이름)'}")
    suffix = {"1": "1", "2": "2", "여름": "S", "겨울": "W"}[match.group(2)]
    return f"{int(match.group(1)) % 100:02d}-{suffix}"


def _term_contains_now(term: Mapping[str, Any], now: dt.datetime) -> bool | None:
    start_raw, end_raw = term.get("start_at"), term.get("end_at")
    if start_raw is None and end_raw is None:
        return None
    try:
        start, end = parse_iso_datetime(start_raw), parse_iso_datetime(end_raw)
    except ValueError as exc:
        raise TermSelectionError("term_invalid_date", "Canvas 학기 날짜 형식이 올바르지 않습니다.") from exc
    moment = now.astimezone(dt.timezone.utc)
    return (start is None or start.astimezone(dt.timezone.utc) <= moment) and (
        end is None or moment <= end.astimezone(dt.timezone.utc)
    )


def select_current_term(courses: Sequence[Mapping[str, Any]], now: dt.datetime) -> TermSelection:
    expected_name, expected_id = expected_term(now)
    eligible: list[Mapping[str, Any]] = []
    for course in courses:
        term = course.get("term") or {}
        term_name = str(term.get("name") or "").strip()
        if term_name.upper() == "HY-MOOC":
            continue
        if course.get("concluded") is True or course.get("access_restricted_by_date") is True:
            continue
        eligible.append(course)
    if not eligible:
        return TermSelection(None, expected_name, expected_id, [])

    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    terms: dict[tuple[str, str], Mapping[str, Any]] = {}
    for course in eligible:
        term = course.get("term") or {}
        name = str(term.get("name") or "").strip()
        if not TERM_RE.fullmatch(name):
            raise TermSelectionError("term_unknown", f"지원하지 않는 학기 이름입니다: {name or '(빈 이름)'}")
        canvas_id = str(term.get("id")) if term.get("id") is not None else ""
        key = (canvas_id, name)
        groups.setdefault(key, []).append(course)
        terms[key] = term

    # Hanyang Canvas can leave old terms open-ended, so their dates may still
    # contain today.  A unique calendar-expected term is therefore the primary
    # signal unless Canvas explicitly says that term has already ended or has
    # not started.  Dates remain the fallback for non-standard term calendars.
    name_matches = [key for key in groups if key[1] == expected_name]
    if len(name_matches) > 1:
        raise TermSelectionError(
            "term_ambiguous", f"현재 학기 이름 후보가 여러 개입니다: {expected_name}"
        )
    if len(name_matches) == 1 and _term_contains_now(terms[name_matches[0]], now) is not False:
        selected = name_matches[0]
    else:
        dated_matches = [key for key, term in terms.items() if _term_contains_now(term, now) is True]
        if len(dated_matches) > 1:
            names = ", ".join(sorted({key[1] for key in dated_matches}))
            raise TermSelectionError("term_ambiguous", f"현재 학기 날짜 후보가 여러 개입니다: {names}")
        if len(dated_matches) == 1:
            selected = dated_matches[0]
        else:
            candidates = ", ".join(sorted({key[1] for key in groups}))
            raise TermSelectionError(
                "term_not_found", f"현재 학기를 안전하게 결정하지 못했습니다. 후보: {candidates}"
            )

    canvas_id, name = selected
    return TermSelection(canvas_id or None, name, term_id_from_name(name), groups[selected])

def source_status(count: int, *, restricted_count: int = 0) -> dict[str, Any]:
    if count:
        status = "collected"
    elif restricted_count:
        status = "restricted"
    else:
        status = "empty"
    return {
        "status": status,
        "discovered_count": count + restricted_count,
        "normalized_count": count,
        "restricted_count": restricted_count,
        "completeness": "complete" if restricted_count == 0 else "restricted",
    }


def unwrap_collection(
    payload: Any, root_key: str, *, error_code: str, label: str
) -> list[Any]:
    """Accept a Canvas collection envelope while tolerating legacy bare lists."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get(root_key), list):
        return payload[root_key]
    raise HylmsError(error_code, f"{label} 응답 형식이 올바르지 않습니다.")


def stable_error_code(exc: BaseException) -> str:
    if isinstance(exc, HylmsError):
        return exc.code
    if isinstance(exc, OSError):
        return "filesystem_error"
    return "unexpected_error"
