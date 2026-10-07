"""HTML, URL, filename, attachment, and submission normalization."""

from __future__ import annotations

import html.parser
import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Mapping

from .core import CANVAS_ORIGIN, kst_iso

SENSITIVE_QUERY_NAMES = {
    "access_token",
    "auth",
    "authorization",
    "expires",
    "expiration",
    "email",
    "key-pair-id",
    "login_id",
    "sis_login_id",
    "oauth_signature",
    "oauth_token",
    "phpsessid",
    "policy",
    "session",
    "session_id",
    "sig",
    "signature",
    "token",
    "user",
    "user_id",
    "user_login",
    "user_name",
    "canvas_user_id",
    "student_id",
    "sis_user_id",
    "verifier",
    "xn_api_token",
}


def sanitize_url(url: str | None, base_url: str = CANVAS_ORIGIN) -> str | None:
    if not url or not isinstance(url, str):
        return None
    absolute = urllib.parse.urljoin(base_url, url)
    parsed = urllib.parse.urlsplit(absolute)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    safe_query: list[tuple[str, str]] = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        normalized = lowered.replace("-", "_")
        if (
            normalized in SENSITIVE_QUERY_NAMES
            or normalized.startswith("x_amz_")
            or normalized.startswith("oauth_")
            or normalized.endswith("_signature")
            or normalized.endswith("_token")
        ):
            continue
        safe_query.append((key, value))
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path, urllib.parse.urlencode(safe_query), parsed.fragment)
    )


class _ContentParser(html.parser.HTMLParser):
    BLOCKS = {
        "address", "article", "aside", "blockquote", "br", "div", "dl", "dt", "dd", "figcaption",
        "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
        "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.raw_links: list[str] = []
        self.links: list[dict[str, str | None]] = []
        self.images: list[dict[str, str | None]] = []
        self._skip_depth = 0
        self._anchor: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "a" and attrs_dict.get("href"):
            raw = attrs_dict["href"] or ""
            self.raw_links.append(raw)
            self._anchor = {"url": sanitize_url(raw, self.base_url), "text": []}
        elif tag == "img" and attrs_dict.get("src"):
            raw = attrs_dict["src"] or ""
            safe = sanitize_url(raw, self.base_url)
            if safe:
                self.images.append({"url": safe, "alt": attrs_dict.get("alt") or None})

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a" and self._anchor is not None:
            if self._anchor["url"]:
                self.links.append(
                    {"url": self._anchor["url"], "text": clean_inline_text("".join(self._anchor["text"])) or None}
                )
            self._anchor = None
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.parts.append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)


def clean_inline_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def clean_block_text(parts: Iterable[str]) -> str:
    text = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    result: list[str] = []
    for line in lines:
        if line or (result and result[-1]):
            result.append(line)
    return "\n".join(result).strip()


def dedupe_dicts(values: Iterable[dict[str, Any]], key: str = "url") -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for value in values:
        marker = value.get(key)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def clean_html(value: str | None, base_url: str = CANVAS_ORIGIN) -> tuple[dict[str, Any], list[str]]:
    parser = _ContentParser(base_url)
    parser.feed(value or "")
    parser.close()
    return (
        {
            "text": clean_block_text(parser.parts),
            "links": dedupe_dicts(parser.links),
            "images": dedupe_dicts(parser.images),
        },
        parser.raw_links,
    )


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sanitize_filename_component(value: str, *, max_length: int = 100, fallback: str = "item") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(". ")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned += "_"
    cleaned = cleaned[:max_length].rstrip(". ") or fallback
    return cleaned

def normalize_rules(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "drop_lowest": value.get("drop_lowest"),
        "drop_highest": value.get("drop_highest"),
        "never_drop": [str(item) for item in value.get("never_drop", [])],
    }


def normalize_attachment(value: Mapping[str, Any], base_url: str = CANVAS_ORIGIN) -> dict[str, Any]:
    return {
        "id": str(value["id"]) if value.get("id") is not None else None,
        "filename": value.get("display_name") or value.get("filename") or value.get("name"),
        "content_type": value.get("content-type") or value.get("content_type"),
        "size": value.get("size"),
        "url": sanitize_url(value.get("url"), base_url),
    }


def normalize_rubric(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for criterion_id, assessment in value.items():
        if not isinstance(assessment, dict):
            continue
        result[str(criterion_id)] = {
            "points": assessment.get("points"),
            "rating_id": str(assessment["rating_id"]) if assessment.get("rating_id") is not None else None,
            "comments": assessment.get("comments"),
        }
    return result


def normalize_submission(value: Mapping[str, Any] | None, base_url: str) -> dict[str, Any] | None:
    if value is None:
        return None
    body, _ = clean_html(value.get("body"), base_url)
    comments: list[dict[str, Any]] = []
    for comment in value.get("submission_comments") or []:
        if not isinstance(comment, dict):
            continue
        comment_body, _ = clean_html(comment.get("comment"), base_url)
        comments.append(
            {
                "id": str(comment["id"]) if comment.get("id") is not None else None,
                "author_display_name": comment.get("author_name"),
                "created_at": kst_iso(comment.get("created_at")),
                "comment": comment_body,
            }
        )
    history: list[dict[str, Any]] = []
    for item in value.get("submission_history") or []:
        if not isinstance(item, dict):
            continue
        history.append(
            {
                "attempt": item.get("attempt"),
                "workflow_state": item.get("workflow_state"),
                "submitted_at": kst_iso(item.get("submitted_at")),
                "graded_at": kst_iso(item.get("graded_at")),
                "score": item.get("score"),
                "grade": item.get("grade"),
                "late": item.get("late"),
                "missing": item.get("missing"),
            }
        )
    return {
        "workflow_state": value.get("workflow_state"),
        "attempt": value.get("attempt"),
        "submitted_at": kst_iso(value.get("submitted_at")),
        "graded_at": kst_iso(value.get("graded_at")),
        "score": value.get("score"),
        "grade": value.get("grade"),
        "excused": value.get("excused"),
        "late": value.get("late"),
        "missing": value.get("missing"),
        "seconds_late": value.get("seconds_late"),
        "body": body,
        "url": sanitize_url(value.get("url") or value.get("preview_url"), base_url),
        "attachments": [normalize_attachment(item, base_url) for item in value.get("attachments") or [] if isinstance(item, dict)],
        "comments": comments,
        "history": history,
        "rubric_assessment": normalize_rubric(value.get("rubric_assessment")),
    }
