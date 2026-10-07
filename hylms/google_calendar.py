"""Independent, resumable Google Calendar reconciliation for HY-LMS."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from .core import HylmsError, KST
from .config import load_config
from .google_auth import (GoogleAuth, GoogleError, GoogleTransport, delay, response_json,
                          timestamp, transient, utc_now)
from .http import retry_after_seconds
from .ics import _validate_calendar, prepare_calendar
from .storage import atomic_write_json

GOOGLE_SYNC_SCHEMA_VERSION = 1
DEFAULT_SYNC_STATE = Path("google_calendar_state.json")
API_ROOT = "https://www.googleapis.com/calendar/v3/"
UID_PATTERN = re.compile(r"[0-9a-f]{64}@hylms\.local\Z")
EVENT_FIELDS = "id,etag,status,summary,description,location,start,end,endTimeUnspecified,extendedProperties,attendees,recurrence,recurringEventId"
OPERATIONS = ("created", "updated", "restored", "deleted", "unchanged", "failed")


class GoogleCalendarClient:
    def __init__(self, auth: GoogleAuth, *, transport=None, sleep=time.sleep):
        self.auth = auth
        self.transport = transport if transport is not None else GoogleTransport()
        self.sleep = sleep

    def call(self, method, path, *, body=None, params=None, etag=None, retry=True):
        if path.startswith(("/", "http:" , "https:")) or ".." in path.split("/"):
            raise GoogleError("google_request_path_invalid")
        url = API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        refreshed = False
        attempts = 0
        while True:
            headers = {"Authorization": "Bearer " + self.auth.access_token(), "Accept": "application/json"}
            if encoded is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"
            if etag:
                headers["If-Match"] = etag
            try:
                response = self.transport.request(method, url, headers, encoded)
            except GoogleError:
                if not retry or attempts >= 2:
                    raise
                delay(None, attempts, self.sleep)
                attempts += 1
                continue
            if response.status == 401:
                if refreshed:
                    raise GoogleError("google_sa_token_failed" if getattr(self.auth, "auth_type", None) == "service_account" else "reauth_required", status=401)
                self.auth.access_token(force=True)
                refreshed = True
                continue
            if transient(response) and retry and attempts < 2:
                delay(response, attempts, self.sleep)
                attempts += 1
                continue
            if not 200 <= response.status < 300:
                code = f"google_http_{response.status}"
                if response.status == 403:
                    try:
                        reasons = [e.get("reason") for e in response_json(response).get("error", {}).get("errors", [])]
                    except (GoogleError, AttributeError, TypeError):
                        reasons = []
                    if "insufficientPermissions" in reasons:
                        code = "google_sa_permission_denied" if getattr(self.auth, "auth_type", None) == "service_account" else "reauth_required"
                    elif "accessNotConfigured" in reasons or "serviceDisabled" in reasons:
                        code = "setup_required"
                    elif transient(response):
                        code = "google_rate_limited"
                raise GoogleError(code, status=response.status, retry_after=response.headers.get("retry-after"))
            return {} if response.status == 204 else response_json(response)

    @staticmethod
    def path(calendar_id, event_id=None):
        path = "calendars/" + urllib.parse.quote(calendar_id, safe="")
        return path if event_id is None else path + "/events/" + urllib.parse.quote(event_id, safe="")

    def get_calendar(self, calendar_id):
        return self.call("GET", self.path(calendar_id))

    def create_calendar(self, installation_id):
        if getattr(self.auth, "auth_type", None) == "service_account":
            raise GoogleError("google_sa_creation_forbidden")
        return self.call("POST", "calendars", body={"summary": "HY-LMS", "timeZone": "Asia/Seoul",
            "description": calendar_marker(installation_id)}, retry=False)

    def list_events(self, calendar_id):
        items, seen_pages, seen_ids = [], set(), set()
        page = None
        while True:
            params = {"showDeleted": "true", "singleEvents": "false", "maxResults": 2500,
                      "fields": f"accessRole,nextPageToken,items({EVENT_FIELDS})"}
            if page:
                params["pageToken"] = page
            result = self.call("GET", self.path(calendar_id) + "/events", params=params)
            if result.get("accessRole") not in getattr(self.auth, "allowed_roles", {"owner"}):
                raise GoogleError("google_sa_permission_denied" if getattr(self.auth, "auth_type", None) == "service_account" else "setup_required")
            values = result.get("items", [])
            if not isinstance(values, list):
                raise GoogleError("google_response_invalid")
            for item in values:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen_ids:
                    raise GoogleError("google_response_invalid")
                seen_ids.add(item["id"])
                items.append(item)
            page = result.get("nextPageToken")
            if not page:
                return items
            if not isinstance(page, str) or page in seen_pages:
                raise GoogleError("google_pagination_invalid")
            seen_pages.add(page)

    def get_event(self, calendar_id, event_id):
        try:
            result = self.call("GET", self.path(calendar_id, event_id), params={"fields": EVENT_FIELDS})
            if result.get("id") != event_id:
                raise GoogleError("google_response_invalid")
            return result
        except GoogleError as exc:
            if exc.status in {404, 410}:
                return {"id": event_id, "status": "missing", "gone": exc.status == 410}
            raise

    def insert(self, calendar_id, body):
        return self.call("POST", self.path(calendar_id) + "/events", body=body,
                         params={"sendUpdates": "none"}, retry=False)

    def patch(self, calendar_id, event_id, body, etag):
        return self.call("PATCH", self.path(calendar_id, event_id), body=body,
                         etag=etag, params={"sendUpdates": "none"})

    def delete(self, calendar_id, event_id, etag):
        return self.call("DELETE", self.path(calendar_id, event_id), etag=etag, params={"sendUpdates": "none"})


def calendar_marker(installation_id):
    return f"HY-LMS managed calendar; binding={installation_id}; version=1"


def google_event_id(uid: str, generation: int = 0) -> str:
    return "hl" + hashlib.sha256(f"{uid}:{generation}".encode("utf-8")).hexdigest()


def validate_sync_state(value):
    try:
        version = value["schema_version"]
        identity_key = "client_id" if version == 1 else "auth"
        if set(value) != {"schema_version", "installation_id", identity_key, "calendar_id", "phase", "events", "last_result"}:
            raise ValueError
        if version not in {1, 2} or not re.fullmatch(r"[0-9a-f]{32}", value["installation_id"]):
            raise ValueError
        identity = binding_identity(value)
        if set(identity) != {"type", "principal_id"} or identity["type"] not in {"desktop_oauth", "service_account"}:
            raise ValueError
        suffix = ".apps.googleusercontent.com" if identity["type"] == "desktop_oauth" else "@" + load_config().get("google_project", "UNCONFIGURED") + ".iam.gserviceaccount.com"
        if not isinstance(identity["principal_id"], str) or not identity["principal_id"].endswith(suffix):
            raise ValueError
        if value["phase"] not in {"new", "creating", "ready"} or not isinstance(value["events"], dict):
            raise ValueError
        if value["calendar_id"] is not None and (not isinstance(value["calendar_id"], str) or not value["calendar_id"] or value["calendar_id"] == "primary"):
            raise ValueError
        if (value["phase"] == "ready") != (value["calendar_id"] is not None):
            raise ValueError
        for uid, entry in value["events"].items():
            if not UID_PATTERN.fullmatch(uid) or set(entry) != {"id", "term", "generation", "confirmed"}:
                raise ValueError
            if type(entry["generation"]) is not int or entry["generation"] < 0 or type(entry["confirmed"]) is not bool:
                raise ValueError
            if entry["id"] != google_event_id(uid, entry["generation"]) or not isinstance(entry["term"], str) or not entry["term"]:
                raise ValueError
        if value["last_result"] is not None:
            summary = value["last_result"]
            if set(summary) != {"at", "status", "counts", "failures"} or set(summary["counts"]) != set(OPERATIONS):
                raise ValueError
            timestamp(summary["at"])
            if any(type(count) is not int or count < 0 for count in summary["counts"].values()):
                raise ValueError
            if any(set(failure) != {"id", "code"} for failure in summary["failures"]):
                raise ValueError
    except (TypeError, ValueError, KeyError, AttributeError):
        raise GoogleError("google_sync_state_invalid") from None
    return value


def load_sync_state(path: Path, *, missing_ok=False):
    try:
        return validate_sync_state(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise GoogleError("setup_required") from None
    except (OSError, ValueError, UnicodeError):
        raise GoogleError("google_sync_state_invalid") from None


def save_sync_state(path: Path, state: dict):
    validate_sync_state(state)
    try:
        atomic_write_json(path, state)
    except OSError:
        raise GoogleError("google_sync_state_write_failed") from None


@contextmanager
def sync_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise GoogleError("busy") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def binding_identity(binding):
    return {"type": "desktop_oauth", "principal_id": binding["client_id"]} if binding["schema_version"] == 1 else binding["auth"]


def auth_identity(auth):
    record = auth.load()
    kind = getattr(auth, "auth_type", "desktop_oauth")
    return {"type": kind, "principal_id": record["client_email" if kind == "service_account" else "client_id"]}


def auth_for_binding(binding, *, clock=utc_now):
    if binding is not None and binding_identity(binding)["type"] == "service_account":
        from .google_service_account import ServiceAccountAuth
        return ServiceAccountAuth(clock=clock)
    return GoogleAuth(clock=clock)


def check_binding(client, binding):
    if binding["phase"] != "ready" or auth_identity(client.auth) != binding_identity(binding):
        raise GoogleError("setup_required")
    try:
        calendar = client.get_calendar(binding["calendar_id"])
    except GoogleError as exc:
        if exc.status in {403, 404, 410} and exc.code != "reauth_required":
            raise GoogleError("setup_required") from None
        raise
    if calendar.get("id") != binding["calendar_id"] or calendar.get("description") != calendar_marker(binding["installation_id"]):
        raise GoogleError("google_calendar_binding_mismatch")


def initialize_calendar(client, state_path: Path = DEFAULT_SYNC_STATE, *, recover_id=None):
    with sync_lock(state_path):
        if getattr(client.auth, "auth_type", None) == "service_account":
            raise GoogleError("google_sa_creation_forbidden")
        credential = client.auth.load()
        binding = load_sync_state(state_path, missing_ok=True)
        if binding is None:
            if recover_id:
                raise GoogleError("google_creation_record_missing")
            binding = {"schema_version": 1, "installation_id": uuid.uuid4().hex, "client_id": credential["client_id"],
                       "calendar_id": None, "phase": "new", "events": {}, "last_result": None}
        if binding_identity(binding) != auth_identity(client.auth):
            raise GoogleError("google_calendar_binding_mismatch")
        if recover_id:
            if recover_id == "primary" or binding["phase"] == "ready" and recover_id != binding["calendar_id"]:
                raise GoogleError("google_calendar_binding_mismatch")
            candidate = {**binding, "phase": "ready", "calendar_id": recover_id}
            check_binding(client, candidate)
            client.list_events(recover_id)  # Owner access, without CalendarList scope.
            save_sync_state(state_path, candidate)
            binding = candidate
        elif binding["phase"] == "ready":
            check_binding(client, binding)
        elif binding["phase"] == "creating":
            raise GoogleError("google_calendar_creation_uncertain")
        else:
            client.auth.access_token()  # Resolve missing/expired authentication before journaling a create.
            binding["phase"] = "creating"
            save_sync_state(state_path, binding)
            try:
                created = client.create_calendar(binding["installation_id"])
            except GoogleError as exc:
                if exc.status in {400, 401, 403, 404}:
                    binding["phase"] = "new"
                    save_sync_state(state_path, binding)
                raise
            if not isinstance(created.get("id"), str) or not created["id"] or created["id"] == "primary":
                raise GoogleError("google_calendar_creation_uncertain")
            binding.update(calendar_id=created["id"], phase="ready")
            save_sync_state(state_path, binding)
            check_binding(client, binding)
        return {"schema_version": 1, "status": "ready", "calendar_id": binding["calendar_id"],
                "installation_id": binding["installation_id"]}


def event_payload(event, term, binding, generation):
    description = event["description"]
    if event["all_day"]:
        start, end = {"date": event["start"]}, {"date": event["end"]}
        first, exclusive_end = dt.date.fromisoformat(event["start"]), dt.date.fromisoformat(event["end"])
        if exclusive_end - first >= dt.timedelta(days=7):
            last = exclusive_end - dt.timedelta(days=1)
            start = {"date": last.isoformat()}
            description += f"\n전체 기간: {first.isoformat()} ~ {last.isoformat()} (마지막 날 포함)"
    else:
        ending = event["end"] or (timestamp(event["start"]) + dt.timedelta(minutes=1)).isoformat(timespec="seconds")
        start, end = {"dateTime": event["start"], "timeZone": "Asia/Seoul"}, {"dateTime": ending, "timeZone": "Asia/Seoul"}
        first, last = timestamp(event["start"]), timestamp(ending)
        if last - first >= dt.timedelta(days=7):
            start = {"dateTime": last.astimezone(KST).isoformat(timespec="seconds"), "timeZone": "Asia/Seoul"}
            end = {"dateTime": (last + dt.timedelta(minutes=1)).astimezone(KST).isoformat(timespec="seconds"), "timeZone": "Asia/Seoul"}
            description += f"\n전체 기간: {first.astimezone(KST):%Y-%m-%d %H:%M:%S} ~ {last.astimezone(KST):%Y-%m-%d %H:%M:%S} (서울)"
    return {"id": google_event_id(event["uid"], generation), "summary": event["title"],
            "description": html.escape(description, quote=False), "location": event["location"] or "",
            "start": start, "end": end, "endTimeUnspecified": event["end"] is None, "status": "confirmed",
            "extendedProperties": {"private": {"hylms_owner": binding["installation_id"], "hylms_uid": event["uid"],
                "hylms_term": term, "hylms_generation": str(generation)}}}


def owned_event(remote, binding):
    event_id = remote.get("id")
    for uid, entry in binding["events"].items():
        if entry["id"] == event_id and entry["confirmed"]:
            return uid, copy.deepcopy(entry)
    try:
        props = remote["extendedProperties"]["private"]
        uid, term, generation = props["hylms_uid"], props["hylms_term"], int(props["hylms_generation"])
        if props["hylms_owner"] != binding["installation_id"] or not UID_PATTERN.fullmatch(uid) or generation < 0:
            return None
        if not isinstance(term, str) or not term or event_id != google_event_id(uid, generation):
            return None
        return uid, {"term": term, "generation": generation, "id": event_id, "confirmed": True}
    except (KeyError, ValueError, TypeError):
        return None


def managed_equal(remote, body):
    def boundary(value):
        if value.get("date") is not None:
            return "date", dt.date.fromisoformat(value["date"])
        return "time", timestamp(value["dateTime"])
    try:
        for field in ("summary", "description", "location"):
            if (remote.get(field) or "").replace("\r\n", "\n") != body[field].replace("\r\n", "\n"):
                return False
        if remote.get("status", "confirmed") != body["status"] or bool(remote.get("endTimeUnspecified")) != body["endTimeUnspecified"]:
            return False
        if any(boundary(remote[key]) != boundary(body[key]) for key in ("start", "end")):
            return False
        props = (remote.get("extendedProperties") or {}).get("private") or {}
        return all(props.get(k) == v for k, v in body["extendedProperties"]["private"].items())
    except (KeyError, TypeError, ValueError, AttributeError, GoogleError):
        return False


def patch_payload(body, remote):
    result = copy.deepcopy(body)
    result.pop("id")
    for key in ("start", "end"):
        for field in ("date", "dateTime", "timeZone"):
            result[key].setdefault(field, None)
    props = copy.deepcopy((remote.get("extendedProperties") or {}).get("private") or {})
    props.update(result["extendedProperties"]["private"])
    result["extendedProperties"]["private"] = props
    return result


def blocked_event(remote):
    if remote.get("attendees"):
        return "google_attendees_require_review"
    if remote.get("recurrence") or remote.get("recurringEventId"):
        return "google_recurrence_requires_review"
    return None


def prepare_google_sync(projection, binding, remote_events):
    """Pure plan. Internal bodies stay in memory; CLI exposes IDs and operations only."""
    _validate_calendar(projection)
    validate_sync_state(binding)
    remote = {item["id"]: item for item in remote_events}
    if len(remote) != len(remote_events):
        raise GoogleError("google_response_invalid")
    discovered = {}
    for item in remote_events:
        owned = owned_event(item, binding)
        if owned and owned[1]["term"] == projection["term"]:
            uid, entry = owned
            if uid not in discovered or entry["generation"] > discovered[uid]["generation"]:
                discovered[uid] = entry
    actions, keep_ids = [], set()
    for event in projection["events"]:
        if event.get("completion") in {"done", "excluded"}:
            continue
        uid = event["uid"]
        entry = copy.deepcopy(binding["events"].get(uid) or discovered.get(uid) or {
            "id": google_event_id(uid), "generation": 0, "term": projection["term"], "confirmed": False})
        if entry["term"] != projection["term"]:
            raise GoogleError("google_sync_state_invalid")
        body = event_payload(event, projection["term"], binding, entry["generation"])
        current = remote.get(entry["id"], {"id": entry["id"], "status": "missing"})
        keep_ids.add(entry["id"])
        error = blocked_event(current)
        if current.get("status") != "missing" and owned_event(current, binding) is None:
            error = "google_event_ownership_conflict"
        operation = "conflict" if error else "create" if current.get("status") == "missing" else (
            "restore" if current.get("status") == "cancelled" else "unchanged" if managed_equal(current, body) else "update")
        actions.append({"operation": operation, "id": entry["id"], "uid": uid, "entry": entry,
                        "body": body, "remote": current, "error": error})
    for item in remote_events:
        owned = owned_event(item, binding)
        if item["id"] in keep_ids or not owned or owned[1]["term"] != projection["term"] or item.get("status") in {"cancelled", "missing"}:
            continue
        error = blocked_event(item)
        actions.append({"operation": "conflict" if error else "delete", "id": item["id"], "uid": owned[0],
                        "entry": owned[1], "body": None, "remote": item, "error": error})
    return actions


class SourceGuard:
    """Watch the immutable-run inventory and input files throughout a run."""
    def __init__(self, term_directory, state_path):
        self.term, self.state_path = term_directory.resolve(), state_path.resolve()
        self.before = self.manifest()

    def manifest(self):
        try:
            files = {self.state_path, *self.term.rglob("*.json")}
            return {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
        except OSError:
            raise GoogleError("google_input_changed") from None

    def check(self):
        if self.before != self.manifest():
            raise GoogleError("google_input_changed")


def _advance_generation(action, binding, state_path):
    entry = action["entry"]
    current = action["remote"]
    if not entry["confirmed"] and not (current.get("status") == "cancelled" and owned_event(current, binding)):
        raise GoogleError("google_event_ownership_conflict")
    entry.update(generation=entry["generation"] + 1, confirmed=False)
    entry["id"] = google_event_id(action["uid"], entry["generation"])
    action["id"] = entry["id"]
    action["body"]["id"] = entry["id"]
    action["body"]["extendedProperties"]["private"]["hylms_generation"] = str(entry["generation"])
    action["remote"] = {"id": entry["id"], "status": "missing"}
    binding["events"][action["uid"]] = copy.deepcopy(entry)
    save_sync_state(state_path, binding)


def _execute_action(action, binding, client, state_path, guard):
    if action["error"]:
        raise GoogleError(action["error"])
    if action["operation"] == "unchanged":
        return "unchanged"
    calendar_id = binding["calendar_id"]
    original_operation = action["operation"]
    for attempt in range(3):
        guard()
        entry, current, body = action["entry"], action["remote"], action["body"]
        blocked = blocked_event(current)
        if blocked:
            raise GoogleError(blocked)
        status = current.get("status")
        if status != "missing" and owned_event(current, binding) is None:
            raise GoogleError("google_event_ownership_conflict")
        if original_operation == "delete":
            if status in {"cancelled", "missing"}:
                return "deleted"
        elif status not in {"cancelled", "missing"} and managed_equal(current, body):
            return {"create": "created", "restore": "restored", "update": "updated"}[original_operation]
        # Before mutating Google, journal the exact ID we will reconcile after interruption.
        existing = binding["events"].get(action["uid"])
        if original_operation != "delete" or existing is None or existing["id"] == entry["id"]:
            binding["events"][action["uid"]] = copy.deepcopy(entry)
        save_sync_state(state_path, binding)
        if status == "missing" and current.get("gone") and original_operation != "delete":
            _advance_generation(action, binding, state_path)
            continue
        try:
            guard()
            if original_operation == "delete":
                if not current.get("etag"):
                    raise GoogleError("google_etag_missing")
                client.delete(calendar_id, entry["id"], current["etag"])
                return "deleted"
            if status == "missing":
                response = client.insert(calendar_id, body)
                outcome = "restored" if original_operation == "restore" or entry["generation"] else "created"
            else:
                if not current.get("etag"):
                    # Some tombstones expose only the ID; try a full get before deciding.
                    current = client.get_event(calendar_id, entry["id"])
                    action["remote"] = current
                    if current.get("status") == "missing":
                        continue
                    if not current.get("etag"):
                        raise GoogleError("google_etag_missing")
                    if blocked_event(current):
                        raise GoogleError(blocked_event(current))
                response = client.patch(calendar_id, entry["id"], patch_payload(body, current), current["etag"])
                outcome = "restored" if original_operation == "restore" else "updated"
            if response.get("id") != entry["id"] or not owned_event(response, binding):
                raise GoogleError("google_response_invalid")
            if not managed_equal(response, body):
                raise GoogleError("google_event_not_converged")
            return outcome
        except GoogleError as exc:
            if exc.code in {"setup_required", "reauth_required", "google_input_changed"}:
                raise
            if original_operation == "delete" and exc.status in {404, 410}:
                return "deleted"
            if exc.status == 412:
                action["remote"] = client.get_event(calendar_id, entry["id"])
                continue
            if original_operation != "delete" and status == "cancelled" and exc.status in {404, 409, 410}:
                guard()
                _advance_generation(action, binding, state_path)
                continue
            uncertain_insert = status == "missing" and (
                exc.status in {409, 429} or exc.status is not None and exc.status >= 500
                or exc.code in {"google_transport_error", "google_response_invalid", "google_rate_limited"})
            if uncertain_insert:
                found = client.get_event(calendar_id, entry["id"])
                action["remote"] = found
                if found.get("status") != "missing":
                    if not owned_event(found, binding):
                        raise GoogleError("google_event_ownership_conflict") from None
                    if found.get("status") != "cancelled" and managed_equal(found, body):
                        return "restored" if original_operation == "restore" or entry["generation"] else "created"
                    continue
                if exc.status == 409:
                    guard()
                    _advance_generation(action, binding, state_path)
                    continue
                if attempt < 2:
                    client.sleep(max(float(2 ** attempt), retry_after_seconds(exc.retry_after, cap=30.0)))
                    continue
            raise
    raise GoogleError("google_reconciliation_retry_exhausted")


def _result(projection, binding, actions):
    return {"schema_version": 1, "status": "planned", "calendar_id": binding["calendar_id"],
            "term": projection["term"], "snapshot_run_id": projection["snapshot_run_id"],
            "excluded_completed": sum(event.get("completion") == "done" for event in projection["events"]),
            "excluded_non_actionable": sum(event.get("completion") == "excluded" for event in projection["events"]),
            "state_cursor": projection["state_cursor"], "counts": {key: 0 for key in OPERATIONS},
            "warnings": projection["warnings"], "failures": [], "error": None,
            "operations": [{"operation": a["operation"], "id": a["id"], "uid": a["uid"], "error": a["error"]} for a in actions]}


def sync_google_calendar(term_directory: Path, phase2_state_path: Path, *, state_path: Path = DEFAULT_SYNC_STATE,
                         client=None, plan_only=False, clock=utc_now):
    term_directory, phase2_state_path, state_path = Path(term_directory), Path(phase2_state_path), Path(state_path)
    resolved = state_path.resolve()
    if resolved == phase2_state_path.resolve() or resolved.is_relative_to(term_directory.resolve()) or resolved.suffix != ".json":
        raise GoogleError("google_sync_state_path_invalid")
    with sync_lock(state_path):
        guard = SourceGuard(term_directory, phase2_state_path)
        try:
            state = json.loads(phase2_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise GoogleError("google_source_invalid") from None
        projection = prepare_calendar(term_directory, state, now=clock())
        guard.check()
        binding = load_sync_state(state_path)
        client = client if client is not None else GoogleCalendarClient(auth_for_binding(binding, clock=clock))
        check_binding(client, binding)
        remote = client.list_events(binding["calendar_id"])
        known_ids = {item["id"] for item in remote}
        expected = {entry["id"] for entry in binding["events"].values() if entry["term"] == projection["term"]}
        # Full listing proves absence for new IDs. Only journaled IDs need a separate
        # tombstone lookup; insert collisions/uncertain responses are resolved by ID.
        for event_id in sorted(expected - known_ids):
            remote.append(client.get_event(binding["calendar_id"], event_id))
        actions = prepare_google_sync(projection, binding, remote)
        guard.check()
        result = _result(projection, binding, actions)
        if plan_only:
            categories = {"create": "created", "update": "updated", "restore": "restored", "delete": "deleted", "unchanged": "unchanged", "conflict": "failed"}
            for action in actions:
                result["counts"][categories[action["operation"]]] += 1
            return result
        for index, action in enumerate(actions):
            try:
                guard.check()
                category = _execute_action(action, binding, client, state_path, guard.check)
                result["counts"][category] += 1
                entry = {**action["entry"], "confirmed": True}
                existing = binding["events"].get(action["uid"])
                if action["operation"] != "delete" or existing is None or existing["id"] == entry["id"]:
                    if existing != entry:
                        binding["events"][action["uid"]] = entry
                        save_sync_state(state_path, binding)
                result["operations"][index]["id"] = action["id"]
            except HylmsError as exc:
                result["counts"]["failed"] += 1
                failure_id = "__input__" if exc.code == "google_input_changed" else action["id"]
                result["failures"].append({"id": failure_id, "code": exc.code})
                if exc.code in {"reauth_required", "setup_required", "google_sync_state_write_failed", "google_input_changed"} or exc.code.startswith(("credential_", "google_credential_", "google_sa_")):
                    result["error"] = {"code": exc.code}
                    break
        # The last remote request (or the last no-op) is still inside the input
        # consistency boundary. There may be no next iteration to catch a change.
        if result["error"] is None:
            try:
                guard.check()
            except HylmsError as exc:
                result["error"] = {"code": exc.code}
                result["counts"]["failed"] += 1
                result["failures"].append({"id": "__input__", "code": exc.code})
        successful = sum(result["counts"][name] for name in OPERATIONS if name != "failed")
        result["status"] = "ok" if not result["failures"] else "partial_failure" if successful else "failed"
        binding["last_result"] = {"at": clock().isoformat(timespec="seconds"), "status": result["status"],
                                  "counts": result["counts"], "failures": result["failures"]}
        if not result["error"] or result["error"]["code"] != "google_sync_state_write_failed":
            try:
                save_sync_state(state_path, binding)
            except GoogleError as exc:
                result["error"] = {"code": exc.code}
                result["status"] = "partial_failure" if successful else "failed"
        return result


def service_account_setup(action, state_path, *, key_file=None, auth=None):
    from .google_service_account import ServiceAccountAuth, validate_key
    auth = auth or ServiceAccountAuth()
    with sync_lock(state_path):
        if action == "import":
            try:
                value = validate_key(json.loads(Path(key_file).read_text(encoding="utf-8-sig")))
            except (OSError, ValueError, UnicodeError):
                raise GoogleError("google_sa_key_invalid") from None
            binding = load_sync_state(state_path, missing_ok=True)
            if binding is not None and binding_identity(binding)["type"] == "service_account" and binding_identity(binding)["principal_id"] != value["client_email"]:
                raise GoogleError("google_calendar_binding_mismatch")
            auth.store.write(value)
            return {"status": "configured", "auth_type": "service_account", "principal_id": value["client_email"],
                    "active_binding_changed": False, "plaintext_source_retained": True}
        binding = load_sync_state(state_path)
        if binding["phase"] != "ready":
            raise GoogleError("setup_required")
        original = state_path.read_bytes()
        backup = None
        if action == "activate" and binding_identity(binding) != auth_identity(auth):
            backup = state_path.with_name(state_path.name + ".pre-service-account-" + uuid.uuid4().hex + ".bak")
            try:
                with backup.open("xb") as stream:
                    stream.write(original)
                    stream.flush()
                    import os
                    os.fsync(stream.fileno())
            except OSError:
                raise GoogleError("google_sa_backup_failed") from None
        candidate = copy.deepcopy(binding)
        candidate.pop("client_id", None)
        candidate.update(schema_version=2, auth=auth_identity(auth))
        auth.check()
        client = GoogleCalendarClient(auth)
        check_binding(client, candidate)
        remote = client.list_events(candidate["calendar_id"])
        found = {item["id"]: item for item in remote}
        # Check every journaled ID, including older terms, before switching actor.
        # Previously cancelled events may be tombstones, but must still be visible.
        for uid, entry in candidate["events"].items():
            if not entry["confirmed"]:
                raise GoogleError("google_sa_unconfirmed_event")
            item = found.get(entry["id"])
            if item is None:
                item = client.get_event(candidate["calendar_id"], entry["id"])
            if item.get("status") == "missing" or item.get("id") != entry["id"]:
                raise GoogleError("google_sa_event_visibility_failed")
            props = item.get("extendedProperties", {}).get("private", {})
            if item.get("status") != "cancelled" and any(props.get(k) != v for k, v in {
                "hylms_owner": candidate["installation_id"], "hylms_uid": uid,
                "hylms_term": entry["term"], "hylms_generation": str(entry["generation"])}.items()):
                raise GoogleError("google_sa_event_marker_mismatch")
        if state_path.read_bytes() != original:
            raise GoogleError("google_input_changed")
        if action == "activate":
            save_sync_state(state_path, candidate)
        return {**auth.status(), "status": "ready", "calendar_id": candidate["calendar_id"],
                "events_verified": len(candidate["events"]), "activated": action == "activate",
                "backup": str(backup) if backup else None, "external_writes": 0}


def main(argv=None, *, out=print, clock=utc_now, auth=None, client=None):
    parser = argparse.ArgumentParser(prog="py -m hylms.google_calendar")
    commands = parser.add_subparsers(dest="command", required=True)
    auth_commands = commands.add_parser("auth").add_subparsers(dest="action", required=True)
    sa_commands = auth_commands.add_parser("service-account").add_subparsers(dest="sa_action", required=True)
    for name in ("import", "check", "activate"):
        command = sa_commands.add_parser(name)
        command.add_argument("--sync-state", type=Path, default=DEFAULT_SYNC_STATE)
        if name == "import":
            command.add_argument("--key-file", type=Path, required=True)
    for name in ("status", "check", "login"):
        command = auth_commands.add_parser(name)
        command.add_argument("--sync-state", type=Path, default=DEFAULT_SYNC_STATE)
        if name == "login":
            command.add_argument("--client-file", type=Path)
            command.add_argument("--no-browser", action="store_true")
            command.add_argument("--timeout", type=int, default=600)
    calendar_commands = commands.add_parser("calendar").add_subparsers(dest="action", required=True)
    for name in ("init", "recover"):
        command = calendar_commands.add_parser(name)
        command.add_argument("--sync-state", type=Path, default=DEFAULT_SYNC_STATE)
        if name == "recover":
            command.add_argument("--calendar-id", required=True)
    for name in ("plan", "sync"):
        command = commands.add_parser(name)
        command.add_argument("--term-dir", type=Path, required=True)
        command.add_argument("--state", type=Path, required=True)
        command.add_argument("--sync-state", type=Path, default=DEFAULT_SYNC_STATE)
    args = parser.parse_args(argv)
    try:
        if args.command == "auth" and args.action == "service-account":
            result = service_account_setup(args.sa_action, args.sync_state, key_file=getattr(args, "key_file", None), auth=auth)
            result.setdefault("schema_version", 1)
            out(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if auth is None and client is not None:
            auth = client.auth
        auth = auth if auth is not None else auth_for_binding(load_sync_state(args.sync_state, missing_ok=True), clock=clock)
        client = client if client is not None else GoogleCalendarClient(auth)
        if args.command == "auth":
            if args.action == "status":
                result = auth.status()
                result.setdefault("auth_type", getattr(auth, "auth_type", "desktop_oauth"))
            else:
                with sync_lock(args.sync_state):
                    if args.action == "check":
                        result = auth.check()
                    else:
                        if getattr(auth, "auth_type", None) == "service_account":
                            raise GoogleError("google_sa_login_not_applicable")
                        options = {"client_file": args.client_file, "timeout": args.timeout,
                                   "notify": lambda message: out(json.dumps(message, ensure_ascii=False))}
                        if args.no_browser:
                            options["open_browser"] = lambda url: None
                        result = auth.login(**options)
        elif args.command == "calendar":
            result = initialize_calendar(client, args.sync_state,
                                         recover_id=args.calendar_id if args.action == "recover" else None)
        else:
            result = sync_google_calendar(args.term_dir, args.state, state_path=args.sync_state,
                                          client=client, plan_only=args.command == "plan", clock=clock)
        result.setdefault("schema_version", GOOGLE_SYNC_SCHEMA_VERSION)
    except HylmsError as exc:
        status = exc.code if exc.code in {"setup_required", "reauth_required", "busy"} else "failed"
        result = {"schema_version": 1, "status": status, "error": {"code": exc.code},
                  "counts": {key: 0 for key in OPERATIONS}}
    except (OSError, ValueError, TypeError, KeyError):
        result = {"schema_version": 1, "status": "failed", "error": {"code": "google_local_error"},
                  "counts": {key: 0 for key in OPERATIONS}}
    out(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["status"] == "partial_failure":
        return 2
    return 0 if result["status"] in {"ok", "ready", "planned", "configured", "authorized"} else 1


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
