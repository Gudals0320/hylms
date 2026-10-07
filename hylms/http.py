"""Secret-safe, same-origin Canvas HTTP transport and pagination."""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .core import CANVAS_ORIGIN, CanvasHTTPError, CanvasTransportError, HylmsError

@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str) -> None:
        self.origin = canonical_origin(origin)

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if canonical_origin(newurl) != self.origin:
            raise urllib.error.HTTPError(req.full_url, 470, "cross-origin redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibTransport:
    def __init__(self, origin: str = CANVAS_ORIGIN) -> None:
        self.opener = urllib.request.build_opener(_SameOriginRedirectHandler(origin))

    def request(
        self, method: str, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        request = urllib.request.Request(url, method=method, headers=dict(headers))
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp is not None else b""
            response_headers = (
                {key.lower(): value for key, value in exc.headers.items()} if exc.headers else {}
            )
            return HttpResponse(exc.code, response_headers, body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CanvasTransportError(
                "canvas_transport_error", "Canvas에 연결하지 못했습니다."
            ) from exc


class CanvasClient:
    def __init__(
        self,
        token: str,
        *,
        origin: str = CANVAS_ORIGIN,
        transport: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
        max_attempts: int = 3,
    ) -> None:
        self.token = token
        self.origin = origin.rstrip("/")
        self._origin_key = canonical_origin(self.origin)
        self.transport = transport or UrllibTransport(self.origin)
        self.sleep = sleep
        self.timeout = timeout
        self.max_attempts = max_attempts

    def _url(self, path_or_url: str, params: Mapping[str, Any] | None = None) -> str:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else urllib.parse.urljoin(
            self.origin + "/", path_or_url.lstrip("/")
        )
        if canonical_origin(url) != self._origin_key:
            raise CanvasTransportError(
                "canvas_cross_origin", "인증된 Canvas 요청의 외부 origin 이동을 차단했습니다."
            )
        if params:
            parsed = urllib.parse.urlsplit(url)
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            for key, value in params.items():
                if isinstance(value, (list, tuple)):
                    query.extend((key, item) for item in value)
                elif value is not None:
                    query.append((key, value))
            url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query, doseq=True), parsed.fragment)
            )
        return url

    def _request_json(
        self, method: str, path_or_url: str, params: Mapping[str, Any] | None = None
    ) -> tuple[Any, Mapping[str, str]]:
        url = self._url(path_or_url, params)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "hylms-snapshot/1",
        }
        last_transport_error: CanvasTransportError | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self.transport.request(method, url, headers, self.timeout)
            except CanvasTransportError as exc:
                last_transport_error = exc
                if attempt + 1 < self.max_attempts:
                    self.sleep(float(2**attempt))
                    continue
                raise

            if response.status == 429 and attempt + 1 < self.max_attempts:
                self.sleep(retry_after_seconds(response.headers.get("retry-after"), cap=60.0))
                continue
            if 500 <= response.status <= 599 and attempt + 1 < self.max_attempts:
                self.sleep(float(2**attempt))
                continue
            if not 200 <= response.status <= 299:
                known_reason = None
                if response.status == 403 and b"require_initial_post" in response.body[:4096].lower():
                    known_reason = "initial_post_required"
                raise CanvasHTTPError(response.status, safe_request_path(url), known_reason)
            if response.status == 204 or not response.body.strip():
                return None, response.headers
            try:
                return json.loads(response.body.decode("utf-8")), response.headers
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HylmsError(
                    "canvas_invalid_json", f"Canvas 응답 JSON을 해석하지 못했습니다: {safe_request_path(url)}"
                ) from exc
        if last_transport_error is not None:
            raise last_transport_error
        raise CanvasTransportError("canvas_request_failed", "Canvas 요청에 실패했습니다.")

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._request_json("GET", path, params)[0]

    def get_paginated(self, path: str, params: Mapping[str, Any] | None = None) -> list[Any]:
        current = self._url(path, params)
        seen: set[str] = set()
        result: list[Any] = []
        while current:
            if current in seen:
                raise HylmsError("canvas_pagination_loop", "Canvas pagination 순환을 감지했습니다.")
            seen.add(current)
            payload, headers = self._request_json("GET", current)
            if not isinstance(payload, list):
                raise HylmsError(
                    "canvas_invalid_collection", f"Canvas 목록 응답 형식이 올바르지 않습니다: {safe_request_path(current)}"
                )
            result.extend(payload)
            current = parse_next_link(headers.get("link"))
            if current and canonical_origin(current) != self._origin_key:
                raise CanvasTransportError(
                    "canvas_cross_origin", "Canvas pagination의 외부 origin 이동을 차단했습니다."
                )
        return result

    def validate_user(self) -> Mapping[str, Any]:
        payload = self.get_json("/api/v1/users/self")
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise HylmsError("auth_invalid_response", "Canvas 사용자 검증 응답이 올바르지 않습니다.")
        return payload

    def revoke_self(self) -> None:
        self._request_json("DELETE", "/login/oauth2/token")


def canonical_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    return scheme, hostname, port


def safe_request_path(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return parsed.path or "/"


def retry_after_seconds(value: str | None, cap: float = 60.0) -> float:
    if not value:
        return 1.0
    try:
        return min(cap, max(0.0, float(value)))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            now = dt.datetime.now(dt.timezone.utc)
            return min(cap, max(0.0, (when - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 1.0


def parse_next_link(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel\s*=\s*"?([^";]+)"?', part)
        if match and match.group(2).strip() == "next":
            return match.group(1)
    return None
