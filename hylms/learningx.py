"""Minimal LearningX LTI bootstrap and Build #3 snapshot enrichment."""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .content import sanitize_filename_component, sanitize_url
from .core import (
    CANVAS_ORIGIN,
    DOCUMENT_EXTENSIONS,
    HylmsError,
    KST,
    kst_iso,
)
from .http import CanvasClient, canonical_origin, retry_after_seconds, safe_request_path
from .schedule import evaluate_summary


LEARNINGX_PREFIX = "/learningx/"
KIND_MAP = {
    "commons/file": "file",
    "commons/movie": "video",
    "commons/mp4": "video",
    "commons/pdf": "pdf",
    "commons/embed": "embed",
    "commons/none": "resource",
    "commons/screenlecture": "video",
    "commons/zoom": "conference",
    "smart_attendance": "attendance",
    "video_conference": "conference",
    "assignment": "assignment",
    "quiz": "quiz",
    "exam": "quiz",
    "discussion": "discussion",
    "module_builder_text": "text",
    "wiki_page": "page",
    "external_url": "link",
    "website": "link",
    "text": "text",
}
for _commons_video_type in (
    "audio", "everlec", "lecturecam", "movie360", "oncast", "readystream",
    "remix", "webstudio", "xenoglobal",
):
    KIND_MAP[f"commons/{_commons_video_type}"] = "video"

COMMONS_CONTENT_TYPES = {
    1: "syncslide", 2: "movie", 3: "ssz", 4: "syncthink", 5: "embed",
    6: "ssslide", 7: "estage", 8: "howlimage", 9: "xenoglobal", 10: "pdf",
    11: "oncast", 12: "photoset", 13: "screenlecture", 14: "lecturecam",
    15: "readystream", 16: "webstudio", 17: "file", 18: "everlec",
    19: "wbtzip", 20: "xenoglobalembed", 21: "movie360", 26: "audio",
    27: "youtube", 28: "mp4", 29: "zoom", 30: "remix",
}
for _commons_video_type in ("estage", "ssz", "syncthink", "youtube"):
    KIND_MAP[f"commons/{_commons_video_type}"] = "video"


class LearningXHTTPError(HylmsError):
    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"learningx_http_{status}", f"LearningX 요청이 HTTP {status}로 실패했습니다: {path}")
        self.status = status
        self.path = path


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "form" and self.action is None:
            self.action = values.get("action")
            self._inside = True
        elif self._inside and tag.lower() == "input" and values.get("name"):
            self.fields[values["name"] or ""] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._inside:
            self._inside = False


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep redirects on Hanyang and never forward the Canvas bearer into LearningX."""

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        if canonical_origin(newurl) != canonical_origin(CANVAS_ORIGIN):
            raise urllib.error.HTTPError(req.full_url, 470, "cross-origin redirect blocked", headers, fp)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(newurl).path.startswith(LEARNINGX_PREFIX):
            redirected.remove_header("Authorization")
        return redirected


@dataclass
class LearningXSession:
    token: str
    user_id: str
    user_login: str
    role: str
    request_type: str
    opener: Any
    sleep: Callable[[float], None] = time.sleep
    timeout: float = 30.0

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith(LEARNINGX_PREFIX):
            raise HylmsError("learningx_path_rejected", "허용하지 않은 LearningX API 경로입니다.")
        url = urllib.parse.urljoin(CANVAS_ORIGIN, path)
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        for attempt in range(3):
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": "Mozilla/5.0 hylms-snapshot/3",
                    "Referer": CANVAS_ORIGIN + "/",
                },
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    body, status, headers = response.read(), response.status, response.headers
            except urllib.error.HTTPError as exc:
                body, status, headers = exc.read(), exc.code, exc.headers
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 2:
                    self.sleep(float(2**attempt))
                    continue
                raise HylmsError("learningx_transport_error", "LearningX에 연결하지 못했습니다.") from exc
            if status == 429 and attempt < 2:
                self.sleep(retry_after_seconds(headers.get("Retry-After"), 60.0))
                continue
            if 500 <= status <= 599 and attempt < 2:
                self.sleep(float(2**attempt))
                continue
            if not 200 <= status <= 299:
                raise LearningXHTTPError(status, safe_request_path(url))
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HylmsError("learningx_invalid_json", "LearningX JSON 응답을 해석하지 못했습니다.") from exc
        raise HylmsError("learningx_request_failed", "LearningX 요청에 실패했습니다.")


class LearningXBootstrap:
    def __init__(self, canvas: CanvasClient, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.canvas = canvas
        self.sleep = sleep

    def prepare(self, courses: Sequence[Mapping[str, Any]]) -> LearningXSession:
        course_id: str | None = None
        for course in courses:
            candidate_id = str(course["id"])
            tabs = self.canvas.get_paginated(f"/api/v1/courses/{candidate_id}/tabs")
            if any(_is_weekly_learning_tab(tab, candidate_id) for tab in tabs):
                course_id = candidate_id
                break
        if course_id is None:
            raise HylmsError("learningx_tool_not_found", "현재 과목에서 주차학습 도구를 찾지 못했습니다.")

        launch = self.canvas.get_json(
            f"/api/v1/courses/{course_id}/external_tools/sessionless_launch",
            {"id": "140", "launch_type": "course_navigation"},
        )
        if not isinstance(launch, dict) or not isinstance(launch.get("url"), str):
            raise HylmsError("learningx_launch_invalid", "Canvas LTI launch URL이 누락됐습니다.")
        cookies = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies), _RedirectHandler())
        launch_url = launch["url"]
        if canonical_origin(launch_url) != canonical_origin(CANVAS_ORIGIN):
            raise HylmsError("learningx_launch_origin", "외부 LTI launch origin을 차단했습니다.")
        headers = {"User-Agent": "Mozilla/5.0 hylms-snapshot/3"}
        if not urllib.parse.urlsplit(launch_url).path.startswith(LEARNINGX_PREFIX):
            headers["Authorization"] = f"Bearer {self.canvas.token}"
        html = self._open(opener, launch_url, headers=headers).decode("utf-8", errors="replace")
        parser = _FormParser()
        parser.feed(html)
        if not parser.action:
            raise HylmsError("learningx_form_missing", "Canvas LTI launch form을 찾지 못했습니다.")
        action = urllib.parse.urljoin(launch_url, parser.action)
        if (
            canonical_origin(action) != canonical_origin(CANVAS_ORIGIN)
            or urllib.parse.urlsplit(action).path != "/learningx/lti/modulebuilder"
        ):
            raise HylmsError("learningx_form_origin", "허용하지 않은 LTI form action을 차단했습니다.")
        body = urllib.parse.urlencode(parser.fields).encode("utf-8")
        page = self._open(
            opener,
            action,
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 hylms-snapshot/3",
                "Origin": CANVAS_ORIGIN,
                "Referer": launch_url,
            },
        ).decode("utf-8", errors="replace")
        token = _cookie_value(cookies, "xn_api_token")
        user_id = _data_attribute(page, "data-user_id")
        user_login = _data_attribute(page, "data-user_login")
        role = _data_attribute(page, "data-role")
        if not token or not user_id or not user_login or not role:
            raise HylmsError("learningx_context_missing", "LearningX session context가 누락됐습니다.")
        return LearningXSession(token, user_id, user_login, role, "", opener, self.sleep)

    @staticmethod
    def _open(opener: Any, url: str, *, method: str = "GET", body: bytes | None = None,
              headers: Mapping[str, str] | None = None) -> bytes:
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            with opener.open(request, timeout=30.0) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise HylmsError(f"learningx_launch_http_{exc.code}", "LearningX LTI launch에 실패했습니다.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HylmsError("learningx_launch_transport", "LearningX LTI launch에 연결하지 못했습니다.") from exc


def _is_weekly_learning_tab(tab: Any, course_id: str) -> bool:
    if not isinstance(tab, dict) or tab.get("hidden") is True:
        return False
    path = urllib.parse.urlsplit(str(tab.get("html_url") or "")).path
    return tab.get("id") == "context_external_tool_140" and path == f"/courses/{course_id}/external_tools/140"


def _cookie_value(cookies: http.cookiejar.CookieJar, name: str) -> str:
    for cookie in cookies:
        if cookie.name == name and cookie.domain.lstrip(".") == urllib.parse.urlsplit(CANVAS_ORIGIN).hostname:
            return urllib.parse.unquote(cookie.value.strip('"'))
    return ""


def _data_attribute(html: str, name: str) -> str:
    match = re.search(rf'{re.escape(name)}=["\']([^"\']+)', html)
    return match.group(1) if match else ""


def _collection(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    raise HylmsError("learningx_collection_invalid", "LearningX 목록 응답 형식이 올바르지 않습니다.")


def _component_id(item: Mapping[str, Any]) -> str | None:
    value = (
        item.get("_module_item_id")
        or item.get("module_item_id")
        or item.get("component_id")
        or item.get("attendance_item_id")
        or item.get("id")
    )
    return str(value) if value is not None else None


def _module_content_id(item: Mapping[str, Any]) -> str | None:
    value = item.get("_module_content_id")
    return str(value) if value not in {None, "", "not_open"} else None


def _provider_type(item: Mapping[str, Any]) -> str:
    value = str(
        item.get("component_type")
        or item.get("item_content_type")
        or item.get("type")
        or item.get("content_type")
        or ("commons" if isinstance(item.get("commons_content"), dict) else "")
        or "unknown"
    ).lower()
    if value == "commons":
        detail = item.get("item_content_data") or item.get("commons_content") or {}
        if isinstance(detail, dict) and detail.get("content_type"):
            raw_type = str(detail["content_type"]).lower()
            if raw_type.isdigit():
                raw_type = COMMONS_CONTENT_TYPES.get(int(raw_type), "unknown")
            return f"commons/{raw_type}" if raw_type != "unknown" else "unknown"
    return value


def _flatten(value: Any, location: Mapping[str, Any] | None = None) -> list[tuple[Mapping[str, Any], dict[str, Any]]]:
    result: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    if isinstance(value, list):
        for item in value:
            result.extend(_flatten(item, location))
    elif isinstance(value, dict):
        current = dict(location or {})
        module_week = value.get("position") if isinstance(value.get("module_items"), list) else None
        if any(key in value for key in ("week", "week_no", "week_position", "module_id", "section_name", "section_title")):
            current.update(
                {
                    "section_id": str(value.get("section_id") or value.get("module_id") or value.get("id")) if value.get("section_id") or value.get("module_id") or value.get("id") else None,
                    "section_title": value.get("section_name") or value.get("section_title") or value.get("title"),
                    "week": value.get("week") or value.get("week_no") or value.get("week_position") or module_week,
                }
            )
        item: dict[str, Any] = {}
        content_data = value.get("content_data")
        if isinstance(content_data, dict):
            if isinstance(content_data.get("item_content_data"), dict):
                item.update(content_data["item_content_data"])
                cms_content_id = content_data["item_content_data"].get("content_id")
                if cms_content_id is not None:
                    item["cms_content_id"] = cms_content_id
            item.update(content_data)
        item.update(value)
        if value.get("module_item_id") is not None:
            item["_module_item_id"] = value["module_item_id"]
            item["_module_content_id"] = value.get("content_id")
        provider_type = _provider_type(item)
        if _component_id(item) and (
            any(
                key in item
                for key in ("component_id", "component_type", "content_type", "attendance_item_id", "module_item_id")
            )
            or provider_type in KIND_MAP
            or "/" in provider_type
        ):
            result.append((item, current))
        for key, child in value.items():
            if key in {
                "items", "components", "children", "sections", "subsections",
                "units", "weeks", "module_items", "data",
            }:
                result.extend(_flatten(child, current))
    return result


def _learningx_schedule(item: Mapping[str, Any]) -> dict[str, Any]:
    def boundary(key: str) -> dict[str, Any]:
        if key not in item:
            return {"value": None, "state": "provider_omitted"}
        if item.get(key) is None:
            return {"value": None, "state": "unbounded"}
        try:
            return {"value": kst_iso(item[key]), "state": "known"}
        except ValueError:
            return {"value": None, "state": "unknown"}

    return {
        "effective": {
            "opens_at": boundary("unlock_at"),
            "due_at": boundary("due_at"),
            "late_until_at": boundary("late_at"),
            "closes_at": boundary("lock_at"),
        },
        "default": None,
        "basis": "provider_effective",
    }


class LearningXCollector:
    def __init__(self, session: LearningXSession, *, now: dt.datetime) -> None:
        self.session = session
        self.now = now.astimezone(KST)

    def enrich(self, data: dict[str, Any]) -> list[str]:
        course_id = str(data["course"]["id"])
        self._doc_discovered: set[str] = set()
        self._doc_eligible: set[str] = set()
        self._doc_skipped: set[str] = set()
        sections = self.session.get_json(
            f"/learningx/api/v1/courses/{course_id}/sections_db",
            {"user_id": self.session.user_id, "role": self.session.role, "type": self.session.request_type},
        )
        components = self.session.get_json(
            f"/learningx/api/v1/courses/{course_id}/allcomponents_db",
            {"user_id": self.session.user_id, "user_login": self.session.user_login, "role": self.session.role},
        )
        modules = self.session.get_json(
            f"/learningx/api/v1/courses/{course_id}/modules",
            {"include_detail": "true"},
        )
        section_items = _flatten(_collection(sections, "sections", "data", "items"))
        module_items = _flatten(_collection(modules, "modules", "data", "items"))
        component_values = _collection(components, "components", "data", "items")
        component_items = [
            (item, {})
            for item in component_values
            if isinstance(item, dict) and _component_id(item) is not None
        ]
        component_items.extend(_flatten(component_values))
        component_lookup = {
            component_id: item
            for item, _ in component_items
            if (component_id := _component_id(item)) is not None
        }
        primary_items = module_items or section_items
        merged: dict[str, dict[str, Any]] = {}
        locations: dict[str, dict[str, Any]] = {}
        for item, location in primary_items:
            component_id = _component_id(item)
            if component_id is None:
                raise HylmsError("learningx_component_id_missing", "LearningX component ID가 누락됐습니다.")
            lookup_id = _module_content_id(item) or component_id
            merged[component_id] = {**component_lookup.get(lookup_id, {}), **item}
            if location:
                locations[component_id] = location

        warnings: list[str] = []
        weekly: list[dict[str, Any]] = []
        unknown_count = restricted_count = failed_detail_count = unresolved_count = 0
        documents = data["documents"]
        for component_id, bulk in merged.items():
            bulk_type = _provider_type(bulk)
            not_open = str(bulk.get("_module_content_id") or "").lower() == "not_open"
            needs_detail = bulk_type.startswith("commons/") or bulk_type in {
                "smart_attendance", "video_conference"
            }
            detail_state, error_code = (
                ("restricted", None) if not_open
                else ("collected", None) if needs_detail
                else ("not_applicable", None)
            )
            detail: Mapping[str, Any] = {}
            if not_open:
                restricted_count += 1
            elif needs_detail:
                detail_id = bulk.get("attendance_item_id") or _module_content_id(bulk)
                if detail_id is None and bulk.get("_module_item_id") is None:
                    detail_id = component_id
                if detail_id is None:
                    detail_state = "provider_omitted"
                    needs_detail = False
            if needs_detail and not not_open:
                try:
                    value = self.session.get_json(
                        f"/learningx/api/v1/courses/{course_id}/attendance_items/{detail_id}"
                    )
                    if not isinstance(value, dict):
                        raise HylmsError("learningx_detail_invalid", "LearningX 항목 상세 형식이 올바르지 않습니다.")
                    detail = value
                except LearningXHTTPError as exc:
                    if exc.status in {401, 403}:
                        detail_state, error_code = "restricted", exc.code
                        restricted_count += 1
                    else:
                        detail_state, error_code = "unavailable", exc.code
                        failed_detail_count += 1
                        warnings.append("learningx_detail_failed")
                except HylmsError as exc:
                    detail_state, error_code = "unavailable", exc.code
                    failed_detail_count += 1
                    warnings.append("learningx_detail_failed")
            item = {**bulk, **detail}
            provider_type = _provider_type(item)
            kind = KIND_MAP.get(provider_type, "unknown")
            unknown_count += int(kind == "unknown")
            if kind == "unknown":
                warnings.append("learningx_unknown_type")
            schedule = _learningx_schedule(item)
            attendance_raw = str(item.get("attendance_status") or "NONE").upper()
            attendance_targeted = item.get("use_attendance")
            attendance_state = {
                "ATTENDANCE": "present", "LATE": "late", "ABSENT": "absent",
                "EXCUSED": "excused", "NONE": "none",
            }.get(attendance_raw, "unknown")
            if attendance_targeted is False:
                attendance_state = "not_applicable"
            summary_evidence: dict[str, Any] | None = None
            if attendance_targeted is not False and attendance_raw == "ATTENDANCE":
                summary_evidence = {"workflow_state": "complete", "finished_at": "provider", "late": False}
            elif attendance_targeted is not False and attendance_raw == "LATE":
                summary_evidence = {"workflow_state": "complete", "finished_at": "provider", "late": True}
            elif attendance_targeted is not False and attendance_raw == "ABSENT":
                summary_evidence = {"missing": True}
            linked = self._link(kind, item, data)
            if linked["state"] == "unresolved":
                unresolved_count += 1
                warnings.append("learningx_link_unresolved")
            document_refs = self._document(item, documents, component_id, "weekly_learning")
            location = locations.get(component_id, {})
            weekly.append(
                {
                    "id": component_id,
                    "provider_id": component_id,
                    "kind": kind,
                    "provider_type": provider_type,
                    "title": item.get("title") or item.get("name"),
                    "position": {
                        **location,
                        "week": location.get("week") or item.get("week_position"),
                        "item_order": item.get("position") or item.get("order"),
                    },
                    "instructor_display_name": item.get("instructor_name") or item.get("teacher_name"),
                    "duration_seconds": item.get("duration_seconds") or item.get("duration") or item.get("playtime"),
                    "source_url": sanitize_url(item.get("source_url") or item.get("html_url") or item.get("url")),
                    "schedule": schedule,
                    "access": {
                        "state": "locked" if not_open else self._access(item),
                        "reason": "not_open" if not_open else None,
                    },
                    "progress": {"completed": item.get("completed"), "state": "known" if "completed" in item else "provider_omitted"},
                    "attendance": {
                        "targeted": attendance_targeted,
                        "status": attendance_state,
                        "provider_status": attendance_raw,
                    },
                    "summary_state": evaluate_summary(schedule, summary_evidence, self.now),
                    "document_refs": document_refs,
                    "linked_entity": linked,
                    "detail_state": detail_state,
                    "error_code": error_code,
                }
            )

        weekly_restricted_count = restricted_count
        weekly_has_warnings = bool(warnings)
        resources, resource_warnings, resource_failed, resource_restricted, resource_unknown = self._resources(
            course_id, documents
        )
        warnings.extend(resource_warnings)
        failed_detail_count += resource_failed
        restricted_count += resource_restricted
        data["weekly_learning"] = weekly
        data["course_resources"] = resources
        data["sources"]["weekly_learning"] = {
            "status": "collected" if weekly else "empty",
            "discovered_count": len(merged), "normalized_count": len(weekly),
            "restricted_count": weekly_restricted_count, "completeness": "with_warnings" if weekly_has_warnings else "restricted" if weekly_restricted_count else "complete",
        }
        data["sources"]["course_resources"] = {
            "status": "collected" if resources else "empty",
            "discovered_count": len(resources), "normalized_count": len(resources),
            "restricted_count": resource_restricted, "unknown_type_count": resource_unknown,
            "completeness": "with_warnings" if resource_warnings else "restricted" if resource_restricted else "complete",
        }
        data["sources"]["learningx"] = {
            "status": "collected" if weekly or resources else "empty",
            "discovered_count": len(merged), "normalized_count": len(weekly),
            "unknown_type_count": unknown_count + resource_unknown, "restricted_count": restricted_count,
            "detail_failed_count": failed_detail_count, "unresolved_count": unresolved_count,
            "section_component_count": len({_component_id(item) for item, _ in section_items}),
            "module_item_count": len({_component_id(item) for item, _ in module_items}),
            "component_count": len({_component_id(item) for item, _ in component_items}),
            "resource_count": len(resources),
            "completeness": "with_warnings" if warnings else "restricted" if restricted_count else "complete",
        }
        self._refresh_document_source(data)
        if len(weekly) != len(merged):
            raise HylmsError("learningx_incomplete", "LearningX 발견 수와 정규화 수가 일치하지 않습니다.")
        return list(dict.fromkeys(warnings))

    @staticmethod
    def _access(item: Mapping[str, Any]) -> str:
        value = str(item.get("lecture_period_status") or item.get("access_state") or "").lower()
        if value in {"before", "upcoming", "not_open", "scheduled"}:
            return "locked"
        if value in {"closed", "locked", "expired"}:
            return "locked"
        return "available" if value in {"open", "available", "active"} else "unknown"

    @staticmethod
    def _link(kind: str, item: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, Any]:
        if kind not in {"assignment", "quiz", "discussion"}:
            return {"state": "not_applicable", "kind": None, "id": None}
        ids = {
            "assignment": item.get("assignment_id"),
            "quiz": item.get("quiz_id"),
            "discussion": item.get("discussion_id") or item.get("discussion_topic_id"),
        }
        records = {"assignment": data["assignments"], "quiz": data["quizzes"], "discussion": data["discussions"]}[kind]
        wanted = ids[kind]
        if wanted is None:
            wanted = _module_content_id(item)
        if wanted is None:
            url = str(item.get("source_url") or item.get("html_url") or item.get("url") or "")
            patterns = {
                "assignment": r"/assignments/(\d+)",
                "quiz": r"/quizzes/(\d+)",
                "discussion": r"/discussion_topics/(\d+)",
            }
            match = re.search(patterns[kind], urllib.parse.urlsplit(url).path)
            wanted = match.group(1) if match else None
        if wanted is None and item.get("assignment_id") is not None:
            assignment_id = str(item["assignment_id"])
            match = next((record for record in records if str(record.get("assignment_id") or record.get("id")) == assignment_id), None)
        else:
            match = next((record for record in records if wanted is not None and str(record.get("id")) == str(wanted)), None)
        if match:
            return {"state": "linked", "kind": kind, "id": str(match["id"]), "assignment_id": match.get("assignment_id")}
        return {"state": "unresolved", "kind": kind, "provider_id": str(wanted) if wanted is not None else None}

    def _resources(self, course_id: str, documents: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], int, int, int]:
        values = _collection(
            self.session.get_json(
                f"/learningx/api/v1/courses/{course_id}/resources",
                {"user_login": self.session.user_login},
            ),
            "resources", "data", "items",
        )
        result: list[dict[str, Any]] = []
        warnings: list[str] = []
        failed = restricted = unknown = 0
        for raw in values:
            if not isinstance(raw, dict) or (raw.get("resource_id") is None and raw.get("id") is None):
                raise HylmsError("learningx_resource_invalid", "LearningX 강의자료 ID가 누락됐습니다.")
            resource_id = str(raw.get("resource_id") or raw.get("id"))
            detail, state, error = {}, "collected", None
            try:
                value = self.session.get_json(f"/learningx/api/v1/courses/{course_id}/resources/{resource_id}")
                if not isinstance(value, dict):
                    raise HylmsError("learningx_resource_detail_invalid", "LearningX 강의자료 상세 형식이 올바르지 않습니다.")
                detail = value
            except LearningXHTTPError as exc:
                if exc.status in {401, 403}:
                    state, error, restricted = "restricted", exc.code, restricted + 1
                else:
                    state, error, failed = "unavailable", exc.code, failed + 1
                    warnings.append("learningx_resource_detail_failed")
            except HylmsError as exc:
                state, error, failed = "unavailable", exc.code, failed + 1
                warnings.append("learningx_resource_detail_failed")
            item = {**raw, **detail}
            provider_type = _provider_type(item)
            known_types = set(KIND_MAP)
            if provider_type not in known_types:
                unknown += 1
                warnings.append("learningx_resource_unknown_type")
            refs = self._document(item, documents, resource_id, "course_resource")
            result.append(
                {
                    "id": resource_id, "title": item.get("title") or item.get("name"),
                    "provider_type": provider_type, "position": item.get("position") or item.get("order"),
                    "published": item.get("published"),
                    "completed": item.get("completed") if "completed" in item else item.get("submitted"),
                    "source_url": sanitize_url(item.get("source_url") or item.get("html_url")),
                    "document_refs": refs, "detail_state": state, "error_code": error,
                }
            )
        return result, list(dict.fromkeys(warnings)), failed, restricted, unknown

    def _document(self, item: Mapping[str, Any], documents: dict[str, Any], source_id: str, source: str) -> list[str]:
        nested = item.get("content") if isinstance(item.get("content"), dict) else item.get("commons_content") if isinstance(item.get("commons_content"), dict) else {}
        content_id = item.get("cms_content_id")
        if content_id is None and item.get("_module_item_id") is None:
            content_id = item.get("content_id") or nested.get("content_id") or nested.get("id")
        filename = item.get("filename") or item.get("file_name") or nested.get("filename") or nested.get("file_name") or nested.get("name")
        extension = Path(str(filename or "")).suffix.lower()
        if content_id is None or str(content_id).strip().lower() in {"", "not_open"}:
            return []
        content_id = str(content_id).strip()
        document_id = f"hycms-content:{content_id}"
        self._doc_discovered.add(document_id)
        if extension not in DOCUMENT_EXTENSIONS:
            self._doc_skipped.add(document_id)
            return []
        self._doc_eligible.add(document_id)
        reference = {"source": source, "source_id": source_id, "relation": "content"}
        record = documents.setdefault(
            document_id,
            {
                "id": document_id, "provider": "hycms", "provider_id": content_id,
                "provider_filename": filename, "extension": extension,
                "content_type": item.get("mime_type") or nested.get("mime_type") or _provider_type(item),
                "size": item.get("size") or nested.get("size"),
                "saved_filename": f"hycms-content-{content_id}__{sanitize_filename_component(str(filename), max_length=100)}",
                "saved_path": None, "stable_url": None, "access_state": "available",
                "download_state": "not_collected", "error_code": None, "references": [],
            },
        )
        record["provider_filename"] = filename
        if reference not in record["references"]:
            record["references"].append(reference)
        return [document_id]

    def _refresh_document_source(self, data: dict[str, Any]) -> None:
        source = data["sources"]["documents"]
        source["discovered_count"] = int(source.get("discovered_count") or 0) + len(self._doc_discovered)
        source["eligible_count"] = int(source.get("eligible_count") or 0) + len(self._doc_eligible)
        source["skipped_extension_count"] = int(source.get("skipped_extension_count") or 0) + len(self._doc_skipped)
        source["registered_count"] = len(data["documents"])
        source["status"] = "collected" if source["discovered_count"] else "empty"
        if source["eligible_count"] != source["registered_count"]:
            source["completeness"] = "incomplete"
        elif source.get("completeness") != "with_warnings":
            source["completeness"] = "complete"
