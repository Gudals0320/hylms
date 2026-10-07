"""Google Desktop OAuth with explicit login and Credential Manager persistence."""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, Mapping

from .core import HylmsError
from .credentials import WindowsCredentialStore
from .http import HttpResponse, retry_after_seconds

GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
GOOGLE_CREDENTIAL_TARGET = "hylms-google-calendar:desktop-oauth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class GoogleError(HylmsError):
    def __init__(self, code: str, *, status: int | None = None, retry_after: str | None = None):
        super().__init__(code, code)
        self.status = status
        self.retry_after = retry_after


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
        if not isinstance(value, str) or "T" not in value or parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise GoogleError("google_timestamp_invalid") from None


def validate_credential(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise GoogleError("setup_required")
    for name in ("client_id", "client_secret", "project_id", "access_token", "refresh_token"):
        if not isinstance(record.get(name), str) or not record[name]:
            raise GoogleError("setup_required")
    if not record["client_id"].endswith(".apps.googleusercontent.com"):
        raise GoogleError("setup_required")
    if record.get("scopes") != [GOOGLE_SCOPE]:
        raise GoogleError("reauth_required")
    timestamp(record.get("expires_at"))
    return copy.deepcopy(record)


class GoogleCredentialStore(WindowsCredentialStore):
    def __init__(self):
        super().__init__(GOOGLE_CREDENTIAL_TARGET, comment="HY-LMS Google Calendar Desktop OAuth",
                         username="HY-LMS Google OAuth")

    def read(self) -> dict[str, Any] | None:
        raw = self._read_bytes(self.target)
        if raw is None:
            return None
        try:
            return validate_credential(json.loads(raw))
        except (ValueError, UnicodeError):
            raise GoogleError("setup_required") from None

    def write(self, record: Mapping[str, Any]) -> None:
        value = validate_credential(record)
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > 2560:
            raise GoogleError("google_credential_too_large")
        previous = self._read_bytes(self.target)
        self._write_bytes(self.target, raw)
        try:
            if self._read_bytes(self.target) != raw:
                raise GoogleError("google_credential_readback_failed")
        except BaseException:
            try:
                if previous is not None:
                    self._write_bytes(self.target, previous)
                else:
                    self._delete_target(self.target)
            except BaseException:
                raise GoogleError("google_credential_rollback_failed") from None
            raise


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GoogleTransport:
    def __init__(self):
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method, url, headers, body=None, timeout=10.0) -> HttpResponse:
        request = urllib.request.Request(url, method=method, headers=dict(headers), data=body)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                content = response.read(8 * 1024 * 1024 + 1)
                if len(content) > 8 * 1024 * 1024:
                    raise GoogleError("google_response_too_large")
                return HttpResponse(response.status, {k.lower(): v for k, v in response.headers.items()}, content)
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read(65536))
        except (OSError, urllib.error.URLError):
            raise GoogleError("google_transport_error") from None


def response_json(response: HttpResponse) -> dict[str, Any]:
    try:
        value = json.loads(response.body)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise GoogleError("google_response_invalid", status=response.status) from None


def transient(response: HttpResponse) -> bool:
    if response.status == 429 or 500 <= response.status <= 599:
        return True
    if response.status == 403:
        try:
            reasons = [item.get("reason") for item in response_json(response).get("error", {}).get("errors", [])]
        except (GoogleError, AttributeError, TypeError):
            return False
        return any(reason in {"rateLimitExceeded", "userRateLimitExceeded", "calendarUsageLimitsExceeded"} for reason in reasons)
    return False


def delay(response: HttpResponse | None, attempt: int, sleep: Callable[[float], None]) -> None:
    wait = min(30.0, float(2 ** attempt))
    if response is not None and "retry-after" in response.headers:
        wait = max(wait, retry_after_seconds(response.headers["retry-after"], cap=30.0))
    sleep(wait)


class GoogleAuth:
    def __init__(self, store=None, *, transport=None, clock=utc_now, sleep=time.sleep):
        self.store = store if store is not None else GoogleCredentialStore()
        self.transport = transport if transport is not None else GoogleTransport()
        self.clock, self.sleep = clock, sleep
        self.record: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        if self.record is None:
            record = self.store.read()
            if record is None:
                raise GoogleError("setup_required")
            self.record = validate_credential(record)
        return self.record

    def status(self) -> dict[str, Any]:
        record = self.load()
        return {"status": "configured", "credential_target": GOOGLE_CREDENTIAL_TARGET,
                "project_id": record["project_id"], "client_id": record["client_id"],
                "scopes": record["scopes"], "expires_at": record["expires_at"],
                "expired": timestamp(record["expires_at"]) <= self.clock()}

    def token_request(self, fields: Mapping[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(fields).encode("ascii")
        for attempt in range(3):
            try:
                response = self.transport.request("POST", TOKEN_URL,
                    {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, body)
            except GoogleError:
                if attempt == 2:
                    raise
                delay(None, attempt, self.sleep)
                continue
            if transient(response) and attempt < 2:
                delay(response, attempt, self.sleep)
                continue
            if not 200 <= response.status < 300:
                code = "reauth_required" if response.status in {400, 401, 403} else "google_token_request_failed"
                raise GoogleError(code, status=response.status)
            return response_json(response)
        raise GoogleError("google_token_request_failed")

    def token_record(self, response: Mapping[str, Any], client: Mapping[str, Any]) -> dict[str, Any]:
        record = copy.deepcopy(dict(client))
        token, refresh = response.get("access_token"), response.get("refresh_token", record.get("refresh_token"))
        scope = response.get("scope", GOOGLE_SCOPE)
        expires = response.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(refresh, str) or not refresh:
            raise GoogleError("reauth_required")
        if str(response.get("token_type", "")).lower() != "bearer" or not isinstance(scope, str) or set(scope.split()) != {GOOGLE_SCOPE}:
            raise GoogleError("reauth_required")
        if type(expires) is not int or not 0 < expires <= 86400:
            raise GoogleError("google_token_response_invalid")
        record.update(schema_version=1, access_token=token, refresh_token=refresh, scopes=[GOOGLE_SCOPE],
                      expires_at=(self.clock() + dt.timedelta(seconds=expires)).isoformat(timespec="seconds"),
                      refresh_verified_at=self.clock().isoformat(timespec="seconds"))
        return validate_credential(record)

    def refresh(self, record: Mapping[str, Any]) -> dict[str, Any]:
        response = self.token_request({"grant_type": "refresh_token", "client_id": record["client_id"],
            "client_secret": record["client_secret"], "refresh_token": record["refresh_token"]})
        return self.token_record(response, record)

    def access_token(self, *, force=False) -> str:
        record = self.load()
        if force or timestamp(record["expires_at"]) <= self.clock() + dt.timedelta(seconds=60):
            candidate = self.refresh(record)
            self.store.write(candidate)
            self.record = candidate
        return self.record["access_token"]

    def check(self) -> dict[str, Any]:
        self.store.self_test()
        self.access_token(force=True)
        if self.store.read() != self.record:
            raise GoogleError("google_credential_readback_failed")
        return {**self.status(), "refresh_verified": True}

    def login(self, *, client_file: Path | None = None, open_browser=webbrowser.open,
              notify: Callable[[dict], None] = lambda value: None, timeout=600) -> dict[str, Any]:
        if client_file is None:
            client = {key: self.load()[key] for key in ("client_id", "client_secret", "project_id")}
        else:
            try:
                installed = json.loads(client_file.read_text(encoding="utf-8-sig"))["installed"]
                client = {key: installed[key] for key in ("client_id", "client_secret", "project_id")}
                if installed["token_uri"] != TOKEN_URL or installed["auth_uri"] not in {AUTH_URL, "https://accounts.google.com/o/oauth2/auth"}:
                    raise ValueError
                if not all(isinstance(v, str) and v for v in client.values()) or not client["client_id"].endswith(".apps.googleusercontent.com"):
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise GoogleError("google_client_file_invalid") from None
        self.store.self_test()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        outcome: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                        raise GoogleError("google_oauth_callback_invalid")
                    code = validate_callback(self.path, state)
                except GoogleError as exc:
                    if exc.code == "google_oauth_denied":
                        outcome["error"] = exc.code
                    status, body = 400, b"HY-LMS: authorization not completed. Return to the setup command."
                else:
                    outcome["code"] = code
                    status, body = 200, b"HY-LMS: authorization received. Check the setup command for storage verification."
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 1
            redirect = f"http://127.0.0.1:{server.server_port}/oauth2callback"
            url = AUTH_URL + "?" + urllib.parse.urlencode({"client_id": client["client_id"], "redirect_uri": redirect,
                "response_type": "code", "scope": GOOGLE_SCOPE, "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"})
            notify({"status": "awaiting_browser", "authorization_url": url})
            open_browser(url)
            deadline = time.monotonic() + timeout
            while not outcome and time.monotonic() < deadline:
                server.handle_request()
        if not outcome:
            raise GoogleError("google_oauth_timeout")
        if "error" in outcome:
            raise GoogleError(outcome["error"])
        response = self.token_request({"client_id": client["client_id"], "client_secret": client["client_secret"],
            "grant_type": "authorization_code", "code": outcome.pop("code"), "code_verifier": verifier,
            "redirect_uri": redirect})
        candidate = self.refresh(self.token_record(response, client))
        self.store.write(candidate)
        self.record = candidate
        return {**self.status(), "status": "authorized", "refresh_verified": True}


def validate_callback(path: str, expected_state: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=12)
        supplied = query.get("state", [])
        if parsed.path != "/oauth2callback" or len(supplied) != 1 or not secrets.compare_digest(supplied[0], expected_state):
            raise ValueError
        if "error" in query:
            raise GoogleError("google_oauth_denied")
        codes = query.get("code", [])
        if len(codes) != 1 or not codes[0]:
            raise ValueError
        return codes[0]
    except (ValueError, TypeError):
        raise GoogleError("google_oauth_callback_invalid") from None
