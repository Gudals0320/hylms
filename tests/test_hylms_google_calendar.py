from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from unittest import mock

import hylms.google_calendar as google_calendar_module
from hylms.core import CredentialStoreError
from hylms.google_auth import (AUTH_URL, TOKEN_URL, GOOGLE_SCOPE, GoogleAuth, GoogleError,
                               GoogleCredentialStore, validate_callback, validate_credential)
from hylms.google_calendar import (GoogleCalendarClient, calendar_marker, google_event_id,
    initialize_calendar, load_sync_state, main, sync_google_calendar, sync_lock)
from hylms.http import HttpResponse
from hylms.storage import atomic_write_json
from tests.test_hylms_diff import course_payload, event, phase2_state
from tests.test_hylms_ics import payload
from tests.test_hylms_ntfy import NOW, schedule, write_ntfy_run


def credential():
    return {"schema_version": 1, "client_id": "test.apps.googleusercontent.com", "client_secret": "SECRET-CLIENT",
            "project_id": "test-project", "access_token": "SECRET-ACCESS", "refresh_token": "SECRET-REFRESH",
            "scopes": [GOOGLE_SCOPE], "expires_at": (NOW + dt.timedelta(hours=1)).isoformat(),
            "refresh_verified_at": NOW.isoformat()}


def response(status=200, body=None, headers=None):
    return HttpResponse(status, headers or {}, json.dumps(body or {}).encode())


class MemoryStore:
    def __init__(self, value=None):
        self.value = copy.deepcopy(value)
        self.writes = 0

    def read(self):
        return copy.deepcopy(self.value)

    def write(self, value):
        self.value = copy.deepcopy(value)
        self.writes += 1

    def self_test(self):
        pass


class Transport:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def request(self, method, url, headers, body=None, timeout=10):
        self.requests.append((method, url, headers, body))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class AuthTests(unittest.TestCase):
    def auth(self, responses=(), value=None):
        return GoogleAuth(MemoryStore(credential() if value is None else value), transport=Transport(responses),
                          clock=lambda: NOW, sleep=lambda _: None)

    def token(self, **changes):
        return {"access_token": "NEW-SECRET", "token_type": "Bearer", "expires_in": 3600, "scope": GOOGLE_SCOPE, **changes}

    def test_existing_preparation_format_and_status_redaction(self):
        auth = self.auth()
        self.assertEqual("SECRET-ACCESS", auth.access_token())
        text = json.dumps(auth.status())
        for secret in ("SECRET-ACCESS", "SECRET-CLIENT", "SECRET-REFRESH"):
            self.assertNotIn(secret, text)
        self.assertEqual([], auth.transport.requests)
        self.assertEqual(0, auth.store.writes)

    def test_refresh_preserves_refresh_token_and_validates_before_save(self):
        auth = self.auth([response(body=self.token())])
        self.assertEqual("NEW-SECRET", auth.access_token(force=True))
        self.assertEqual("SECRET-REFRESH", auth.store.value["refresh_token"])
        self.assertEqual(1, auth.store.writes)
        self.assertEqual(TOKEN_URL, auth.transport.requests[0][1])

    def test_invalid_grant_and_scope_do_not_replace_credentials(self):
        for result in (response(400, {"error": "invalid_grant", "secret": "DO-NOT-LOG"}),
                       response(body=self.token(scope="https://www.googleapis.com/auth/calendar")),
                       response(body=self.token(expires_in=False))):
            with self.subTest(result=result.status):
                auth = self.auth([result])
                before = auth.store.read()
                with self.assertRaises(GoogleError) as caught:
                    auth.access_token(force=True)
                self.assertNotIn("DO-NOT-LOG", str(caught.exception))
                self.assertEqual(before, auth.store.read())
                self.assertEqual(0, auth.store.writes)

    def test_refresh_failure_leaves_old_record_in_memory(self):
        auth = self.auth([response(body=self.token())])
        with mock.patch.object(auth.store, "write", side_effect=GoogleError("credential_write_failed")):
            with self.assertRaises(GoogleError):
                auth.access_token(force=True)
        self.assertEqual("SECRET-ACCESS", auth.record["access_token"])

    def test_missing_and_wrong_scope_require_setup_or_reauth(self):
        with self.assertRaises(GoogleError) as caught:
            GoogleAuth(MemoryStore(), clock=lambda: NOW).status()
        self.assertEqual("setup_required", caught.exception.code)
        wrong = credential()
        wrong["scopes"] = [GOOGLE_SCOPE, "openid"]
        with self.assertRaises(GoogleError) as caught:
            validate_credential(wrong)
        self.assertEqual("reauth_required", caught.exception.code)

    def test_callback_state_path_duplicates_and_denial(self):
        self.assertEqual("opaque-code", validate_callback("/oauth2callback?state=expected&code=opaque-code", "expected"))
        for path in ("/wrong?state=expected&code=x", "/oauth2callback?state=other&code=x",
                     "/oauth2callback?state=expected&state=expected&code=x", "/oauth2callback?state=expected&error=access_denied"):
            with self.subTest(path=path), self.assertRaises(GoogleError):
                validate_callback(path, "expected")

    def test_login_timeout_and_invalid_client_do_not_touch_store(self):
        auth = self.auth()
        opened, notices = [], []
        with self.assertRaises(GoogleError) as caught:
            auth.login(open_browser=opened.append, notify=notices.append, timeout=0)
        self.assertEqual("google_oauth_timeout", caught.exception.code)
        self.assertEqual(0, auth.store.writes)
        self.assertTrue(opened[0].startswith(AUTH_URL))
        self.assertIn("code_challenge_method=S256", opened[0])
        self.assertNotIn("SECRET-CLIENT", opened[0])

    def test_runtime_missing_credential_never_opens_browser(self):
        with mock.patch("webbrowser.open", side_effect=AssertionError("unexpected browser")):
            output = []
            self.assertEqual(1, main(["auth", "status"], auth=GoogleAuth(MemoryStore()), out=output.append))
            self.assertEqual("setup_required", json.loads(output[0])["status"])

    def test_explicit_login_pkce_callback_exchange_refresh_and_single_save(self):
        auth = self.auth([response(body=self.token(refresh_token="NEW-REFRESH")), response(body=self.token())])
        workers, errors, urls = [], [], []
        def browser(url):
            urls.append(url)
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            callback = params["redirect_uri"][0] + "?" + urllib.parse.urlencode({"state": params["state"][0], "code": "fixture-code"})
            def visit():
                try:
                    with urllib.request.urlopen(callback, timeout=3) as result:
                        self.assertEqual(200, result.status)
                except Exception as exc:
                    errors.append(type(exc).__name__)
            worker = threading.Thread(target=visit)
            workers.append(worker)
            worker.start()
        result = auth.login(open_browser=browser, timeout=5)
        for worker in workers:
            worker.join(timeout=5)
        self.assertEqual([], errors)
        self.assertEqual("authorized", result["status"])
        self.assertEqual(1, auth.store.writes)
        self.assertEqual("NEW-REFRESH", auth.store.value["refresh_token"])
        self.assertNotIn("fixture-code", json.dumps(result))
        first_form = urllib.parse.parse_qs(auth.transport.requests[0][3].decode())
        self.assertEqual(["fixture-code"], first_form["code"])
        self.assertIn("code_verifier", first_form)
        self.assertIn("code_challenge_method=S256", urls[0])


class BlobStoreFixture(GoogleCredentialStore):
    """Exercise the real write transaction without calling Windows credential APIs."""
    def __init__(self, previous, *, readback="ok", fail_read=None, fail_write=None, fail_delete=False):
        self.target = "test-only"
        self.blob = previous
        self.readback = readback
        self.fail_read, self.fail_write, self.fail_delete = fail_read, fail_write, fail_delete
        self.reads = self.writes = self.deletes = 0

    def _read_bytes(self, target):
        self.reads += 1
        if self.reads == self.fail_read:
            raise CredentialStoreError("credential_read_failed", "injected read failure")
        if self.reads == 2:
            if self.readback == "mismatch":
                return b"different"
            if self.readback == "exception":
                raise CredentialStoreError("credential_read_failed", "injected readback failure")
            if self.readback == "interrupt":
                raise KeyboardInterrupt
        return self.blob

    def _write_bytes(self, target, value):
        self.writes += 1
        if self.writes == self.fail_write:
            raise CredentialStoreError("credential_write_failed", "injected write failure")
        self.blob = value

    def _delete_target(self, target, missing_ok=True):
        self.deletes += 1
        if self.fail_delete:
            raise CredentialStoreError("credential_delete_failed", "injected delete failure")
        self.blob = None


class CredentialWriteRegressionTests(unittest.TestCase):
    def test_readback_mismatch_exception_and_interrupt_restore_original_blob(self):
        previous = json.dumps(credential(), indent=2).encode()
        candidate = {**credential(), "access_token": "NEW-FIXTURE"}
        for failure, expected in (("mismatch", GoogleError), ("exception", CredentialStoreError), ("interrupt", KeyboardInterrupt)):
            with self.subTest(failure=failure):
                store = BlobStoreFixture(previous, readback=failure)
                with self.assertRaises(expected) as caught:
                    store.write(candidate)
                self.assertEqual(previous, store.blob)
                self.assertEqual(2, store.writes)
                self.assertEqual(0, store.deletes)
                if failure == "exception":
                    self.assertEqual("credential_read_failed", caught.exception.code)

    def test_first_credential_is_removed_when_readback_fails(self):
        for failure in ("mismatch", "exception"):
            with self.subTest(failure=failure):
                store = BlobStoreFixture(None, readback=failure)
                with self.assertRaises((GoogleError, CredentialStoreError)):
                    store.write(credential())
                self.assertIsNone(store.blob)
                self.assertEqual((1, 1), (store.writes, store.deletes))

    def test_rollback_write_or_delete_failure_has_its_own_safe_error(self):
        for previous in (b"original-fixture", None):
            with self.subTest(has_previous=previous is not None):
                store = BlobStoreFixture(previous, readback="exception", fail_write=2 if previous else None,
                                         fail_delete=previous is None)
                with self.assertRaises(GoogleError) as caught:
                    store.write(credential())
                self.assertEqual("google_credential_rollback_failed", caught.exception.code)
                self.assertEqual("google_credential_rollback_failed", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_initial_read_or_first_write_failure_does_not_attempt_rollback(self):
        for fail_read, fail_write in ((1, None), (None, 1)):
            with self.subTest(fail_read=fail_read, fail_write=fail_write):
                store = BlobStoreFixture(b"original", fail_read=fail_read, fail_write=fail_write)
                with self.assertRaises(CredentialStoreError):
                    store.write(credential())
                self.assertEqual(b"original", store.blob)
                self.assertEqual(0 if fail_read else 1, store.writes)
                self.assertEqual(0, store.deletes)

    def test_validation_before_write_and_successful_write(self):
        store = BlobStoreFixture(b"original")
        candidate = credential()
        candidate["scopes"] = ["unexpected"]
        with self.assertRaises(GoogleError):
            store.write(candidate)
        self.assertEqual((0, 0), (store.reads, store.writes))
        store.write(credential())
        self.assertEqual(credential(), json.loads(store.blob))
        self.assertEqual((2, 1, 0), (store.reads, store.writes, store.deletes))


class ApiTests(unittest.TestCase):
    def client(self, responses, auth_responses=()):
        auth = GoogleAuth(MemoryStore(credential()), transport=Transport(auth_responses), clock=lambda: NOW, sleep=lambda _: None)
        self.transport = Transport(responses)
        self.waits = []
        return GoogleCalendarClient(auth, transport=self.transport, sleep=self.waits.append)

    def test_pagination_reads_all_pages_and_owner_access(self):
        client = self.client([response(body={"accessRole": "owner", "items": [{"id": "one"}], "nextPageToken": "next"}),
                              response(body={"accessRole": "owner", "items": [{"id": "two"}]})])
        self.assertEqual(["one", "two"], [e["id"] for e in client.list_events("calendar@example.test")])
        self.assertIn("pageToken=next", self.transport.requests[1][1])
        self.assertNotIn("timeMin", self.transport.requests[0][1])
        self.assertNotIn("calendarList", self.transport.requests[0][1])

    def test_pagination_failure_loop_duplicate_and_nonowner_rejected(self):
        fixtures = [
            [response(body={"accessRole": "reader", "items": []})],
            [response(body={"accessRole": "owner", "items": [{"id": "one"}, {"id": "one"}]})],
            [response(body={"accessRole": "owner", "items": [], "nextPageToken": "x"})] * 2,
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture), self.assertRaises(GoogleError):
                self.client(fixture).list_events("calendar")

    def test_rate_limit_backoff_and_retry_after(self):
        client = self.client([response(429, headers={"retry-after": "999"}), response(503), response(body={"id": "calendar"})])
        self.assertEqual("calendar", client.get_calendar("calendar")["id"])
        self.assertEqual([30.0, 2.0], self.waits)

    def test_403_rate_limit_retries_but_permission_failure_does_not(self):
        limited = response(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})
        client = self.client([limited, response(body={"id": "calendar"})])
        self.assertEqual("calendar", client.get_calendar("calendar")["id"])
        denied = response(403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}})
        with self.assertRaises(GoogleError) as caught:
            self.client([denied]).get_calendar("calendar")
        self.assertEqual("reauth_required", caught.exception.code)

    def test_401_refresh_once_then_fail_without_browser(self):
        token = response(body={"access_token": "new", "token_type": "Bearer", "expires_in": 3600})
        client = self.client([response(401), response(401)], [token])
        with self.assertRaises(GoogleError) as caught:
            client.get_calendar("calendar")
        self.assertEqual("reauth_required", caught.exception.code)
        self.assertEqual(2, len(self.transport.requests))

    def test_create_calendar_never_blindly_retries(self):
        client = self.client([GoogleError("google_transport_error")])
        with self.assertRaises(GoogleError):
            client.create_calendar("marker")
        self.assertEqual(1, len(self.transport.requests))

    def test_401_refresh_then_success(self):
        token = response(body={"access_token": "new", "token_type": "Bearer", "expires_in": 3600})
        client = self.client([response(401), response(body={"id": "calendar"})], [token])
        self.assertEqual("calendar", client.get_calendar("calendar")["id"])
        self.assertEqual("Bearer new", self.transport.requests[-1][2]["Authorization"])

    def test_patch_and_delete_use_etag_and_suppress_guest_updates(self):
        client = self.client([response(body={"id": "event"}), response(204)])
        client.patch("calendar", "event", {"summary": "source"}, '"old"')
        client.delete("calendar", "event", '"new"')
        self.assertEqual('"old"', self.transport.requests[0][2]["If-Match"])
        self.assertEqual('"new"', self.transport.requests[1][2]["If-Match"])
        self.assertIn("sendUpdates=none", self.transport.requests[0][1])


class Backend:
    def __init__(self):
        self.auth = GoogleAuth(MemoryStore(credential()), clock=lambda: NOW)
        self.calendars, self.events, self.gone = {}, {}, set()
        self.calls, self.hooks = [], {}
        self.sleep = lambda _: None

    def before(self, operation, event_id=None):
        self.calls.append((operation, event_id))
        hook = self.hooks.get(operation)
        if hook:
            hook(event_id)

    def create_calendar(self, installation_id):
        self.before("create_calendar")
        value = {"id": "secondary@group.calendar.google.com", "summary": "HY-LMS", "description": calendar_marker(installation_id)}
        self.calendars[value["id"]] = value
        return copy.deepcopy(value)

    def get_calendar(self, calendar_id):
        self.before("get_calendar")
        if calendar_id not in self.calendars:
            raise GoogleError("google_http_404", status=404)
        return copy.deepcopy(self.calendars[calendar_id])

    def list_events(self, calendar_id):
        self.before("list")
        return copy.deepcopy(list(self.events.values()))

    def get_event(self, calendar_id, event_id):
        self.before("get", event_id)
        return copy.deepcopy(self.events.get(event_id, {"id": event_id, "status": "missing", "gone": event_id in self.gone}))

    def insert(self, calendar_id, body):
        self.before("insert", body["id"])
        if body["id"] in self.events or body["id"] in self.gone:
            raise GoogleError("google_http_409", status=409)
        value = {**copy.deepcopy(body), "etag": '"1"'}
        self.events[value["id"]] = value
        self.before("inserted", body["id"])
        return copy.deepcopy(value)

    def patch(self, calendar_id, event_id, body, etag):
        self.before("patch", event_id)
        value = self.events.get(event_id)
        if value is None:
            raise GoogleError("google_http_410", status=410)
        if value["etag"] != etag:
            raise GoogleError("google_http_412", status=412)
        def merge(old, patch):
            for key, item in patch.items():
                if item is None:
                    old.pop(key, None)
                elif isinstance(item, dict):
                    merge(old.setdefault(key, {}), item)
                else:
                    old[key] = copy.deepcopy(item)
        merge(value, body)
        value["etag"] = f'"revision-{len(self.calls)}"'
        return copy.deepcopy(value)

    def delete(self, calendar_id, event_id, etag):
        self.before("delete", event_id)
        value = self.events[event_id]
        if value["etag"] != etag:
            raise GoogleError("google_http_412", status=412)
        value.update(status="cancelled", etag='"deleted"')
        return {}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.term = self.root / "26-2"
        self.source = self.root / "phase2_state.json"
        self.binding = self.root / "google_calendar_state.json"
        self.data = payload()
        self.run = write_ntfy_run(self.term, "first", NOW, self.data)
        self.state = phase2_state("first")
        atomic_write_json(self.source, self.state)
        self.backend = Backend()
        self.calendar_id = initialize_calendar(self.backend, self.binding)["calendar_id"]

    def sync(self, plan=False):
        return sync_google_calendar(self.term, self.source, state_path=self.binding,
                                    client=self.backend, clock=lambda: NOW, plan_only=plan)

    def active(self):
        return [e for e in self.backend.events.values() if e["status"] != "cancelled"]

    def test_first_sync_then_noop_preserves_inputs(self):
        before = {p: p.read_bytes() for p in (self.source, *self.term.rglob("*.json"))}
        first = self.sync()
        self.assertEqual("ok", first["status"])
        self.assertEqual(2, first["counts"]["created"])
        ids = set(self.backend.events)
        self.backend.calls = []
        second = self.sync()
        self.assertEqual(2, second["counts"]["unchanged"])
        self.assertEqual(ids, set(self.backend.events))
        self.assertFalse(any(op in {"insert", "patch", "delete"} for op, _ in self.backend.calls))
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_plan_does_not_write_calendar_or_sync_state(self):
        before = self.binding.read_bytes()
        result = self.sync(plan=True)
        self.assertEqual(2, result["counts"]["created"])
        self.assertEqual({}, self.backend.events)
        self.assertEqual(before, self.binding.read_bytes())

    def test_remote_listing_failure_and_invalid_source_never_write(self):
        def failure(_):
            raise GoogleError("google_http_503", status=503)
        self.backend.hooks["list"] = failure
        before = self.binding.read_bytes()
        with self.assertRaises(GoogleError):
            self.sync()
        self.assertEqual(before, self.binding.read_bytes())
        self.assertEqual({}, self.backend.events)
        self.backend.hooks.clear()
        self.source.write_text("invalid", encoding="utf-8")
        with self.assertRaises(GoogleError):
            self.sync()
        self.assertEqual(before, self.binding.read_bytes())

    def test_insert_rate_limit_checks_id_and_honors_retry_after(self):
        waits = []
        self.backend.sleep = waits.append
        def limit(event_id):
            self.backend.hooks.pop("insert")
            raise GoogleError("google_http_429", status=429, retry_after="20")
        self.backend.hooks["insert"] = limit
        self.assertEqual("ok", self.sync()["status"])
        self.assertEqual([20.0], waits)
        self.assertTrue(any(operation == "get" for operation, _ in self.backend.calls))

    def test_managed_changes_restore_and_unmanaged_fields_remain(self):
        self.sync()
        value = self.active()[0]
        original = copy.deepcopy(value)
        value.update(summary="manual", location="manual", description="manual", colorId="6", reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 7}]})
        value["start"]["dateTime"] = "2026-10-01T12:00:00+09:00"
        result = self.sync()
        self.assertEqual(1, result["counts"]["updated"])
        for key in ("summary", "location", "description", "start"):
            self.assertEqual(original[key], value[key])
        self.assertEqual("6", value["colorId"])
        self.assertFalse(value["reminders"]["useDefault"])

    def test_deleted_event_restores_same_id_and_permanent_delete_uses_generation(self):
        self.sync()
        value = self.active()[0]
        original_id = value["id"]
        value["status"] = "cancelled"
        self.assertEqual(1, self.sync()["counts"]["restored"])
        self.assertEqual("confirmed", self.backend.events[original_id]["status"])
        self.backend.events.pop(original_id)
        self.backend.gone.add(original_id)
        result = self.sync()
        self.assertEqual(1, result["counts"]["restored"])
        self.assertEqual(2, len(self.active()))
        self.assertNotIn(original_id, self.backend.events)
        self.assertEqual(2, self.sync()["counts"]["unchanged"])

    def test_tombstone_restore_410_recreates_without_duplicate(self):
        self.sync()
        value = self.active()[0]
        old_id = value["id"]
        value["status"] = "cancelled"
        def permanent(event_id):
            self.backend.hooks.pop("patch")
            self.backend.events.pop(event_id)
            self.backend.gone.add(event_id)
            raise GoogleError("google_http_410", status=410)
        self.backend.hooks["patch"] = permanent
        self.assertEqual(1, self.sync()["counts"]["restored"])
        self.assertNotIn(old_id, self.backend.events)
        self.assertEqual(2, len(self.active()))

    def test_user_events_prior_terms_and_attendees_are_preserved(self):
        self.sync()
        user = {"id": "user-event", "summary": "PRIVATE", "status": "confirmed", "etag": '"1"'}
        self.backend.events[user["id"]] = copy.deepcopy(user)
        old = copy.deepcopy(self.active()[0])
        props = old["extendedProperties"]["private"]
        props["hylms_uid"] = "a" * 64 + "@hylms.local"
        props["hylms_term"] = "26-1"
        old["id"] = google_event_id(props["hylms_uid"])
        self.backend.events[old["id"]] = copy.deepcopy(old)
        managed = self.active()[0]
        managed["attendees"] = [{"email": "guest@example.test"}]
        managed["summary"] = "manual with guests"
        before = copy.deepcopy(managed)
        result = self.sync()
        self.assertEqual("partial_failure", result["status"])
        self.assertEqual(before, managed)
        self.assertEqual(user, self.backend.events[user["id"]])
        self.assertEqual(old, self.backend.events[old["id"]])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_current_term_removed_source_deletes_only_managed(self):
        self.sync()
        self.backend.events["user"] = {"id": "user", "status": "confirmed"}
        self.data["assignments"] = []
        atomic_write_json(self.run / "course__c101.json", self.data)
        result = self.sync()
        self.assertEqual(1, result["counts"]["deleted"])
        self.assertEqual(2, len(self.active()))
        self.assertEqual("confirmed", self.backend.events["user"]["status"])

    def test_foreign_id_collision_is_not_overwritten(self):
        plan = self.sync(plan=True)
        event_id = plan["operations"][0]["id"]
        foreign = {"id": event_id, "summary": "user", "status": "confirmed"}
        self.backend.events[event_id] = copy.deepcopy(foreign)
        result = self.sync()
        self.assertEqual("google_event_ownership_conflict", result["failures"][0]["code"])
        self.assertEqual(foreign, self.backend.events[event_id])

    def test_completed_item_is_hidden_and_reopened_item_restores_same_id(self):
        self.sync()
        self.backend.events["user-completed"] = {"id": "user-completed", "summary": "완료", "status": "confirmed"}
        old_ids = set(self.backend.events)
        assignment = self.data["assignments"][0]
        assignment["submission"]["workflow_state"] = "graded"
        assignment["progress"]["submitted"] = True
        atomic_write_json(self.run / "course__c101.json", self.data)
        planned = self.sync(plan=True)
        self.assertEqual(1, planned["excluded_completed"])
        self.assertEqual(1, planned["counts"]["deleted"])
        self.assertEqual(3, len(self.active()))
        removed = self.sync()
        self.assertEqual((1, 1), (removed["counts"]["deleted"], removed["counts"]["unchanged"]))
        self.assertEqual(2, len(self.active()))
        self.assertEqual("confirmed", self.backend.events["user-completed"]["status"])
        self.assertEqual(0, self.sync()["counts"]["deleted"])
        assignment["submission"]["workflow_state"] = "unsubmitted"
        assignment["progress"]["submitted"] = False
        atomic_write_json(self.run / "course__c101.json", self.data)
        reopened = self.sync()
        self.assertEqual(1, reopened["counts"]["restored"])
        self.assertEqual(0, reopened["excluded_completed"])
        self.assertEqual(old_ids, set(self.backend.events))
        self.assertEqual(3, len(self.active()))

    def test_completion_filter_covers_lms_and_natural_actions_but_keeps_classes_and_unknowns(self):
        data = course_payload(extra=True, discussion_graded=True, discussion_workflow="submitted", own_entry_count=1)
        for item in (data["assignments"][0], data["discussions"][0]):
            item["schedule"] = schedule(due="2026-09-10T12:00:00+09:00")
        data["assignments"][0]["progress"]["submitted"] = True
        data["quizzes"] = [{"id": "60", "title": "완료한 퀴즈", "progress": {"workflow_state": "complete"},
                            "schedule": schedule(due="2026-09-10T12:00:00+09:00")}]
        data["weekly_learning"][0]["kind"] = "video"
        data["weekly_learning"][0]["progress"]["completed"] = True
        atomic_write_json(self.run / "course__c101.json", data)
        done = event("external-done", kind="action", mode="deadline", start=None)
        done["action_state"] = "done"
        unknown = event("unknown", kind="action", mode="deadline", start=None)
        unknown["title"] = "제목에 완료라고 적혀 있어도 미확인"
        past_class = event("past-class")
        self.state["natural_events"] = [done, unknown, past_class]
        atomic_write_json(self.source, self.state)
        before = self.source.read_bytes()
        result = self.sync()
        self.assertEqual(5, result["excluded_completed"])
        self.assertEqual(2, result["counts"]["created"])
        self.assertEqual({unknown["title"], past_class["title"]}, {e["summary"] for e in self.active()})
        self.assertEqual(before, self.source.read_bytes())

    def test_legacy_projection_without_completion_does_not_infer_from_description(self):
        from hylms.ics import prepare_calendar
        from hylms.google_calendar import prepare_google_sync
        self.data["assignments"][0]["progress"]["submitted"] = True
        atomic_write_json(self.run / "course__c101.json", self.data)
        projection = prepare_calendar(self.term, self.state, now=NOW)
        self.assertTrue(any(e["completion"] == "done" for e in projection["events"]))
        for item in projection["events"]:
            item.pop("completion")
        actions = prepare_google_sync(projection, load_sync_state(self.binding), [])
        self.assertEqual(2, sum(a["operation"] == "create" for a in actions))

    def test_attached_markers_with_random_id_are_not_treated_as_owned(self):
        self.sync()
        copied = copy.deepcopy(self.active()[0])
        copied["id"] = "user-copy"
        self.backend.events["user-copy"] = copied
        self.assertEqual(2, self.sync()["counts"]["unchanged"])
        self.assertEqual(copied, self.backend.events["user-copy"])

    def test_lost_marker_on_confirmed_id_is_repaired(self):
        self.sync()
        value = self.active()[0]
        value.pop("extendedProperties")
        self.assertEqual(1, self.sync()["counts"]["updated"])
        self.assertIn("hylms_owner", value["extendedProperties"]["private"])

    def test_insert_timeout_after_success_is_recovered_by_id(self):
        def lost_response(event_id):
            self.backend.hooks.pop("inserted")
            raise GoogleError("google_transport_error")
        self.backend.hooks["inserted"] = lost_response
        result = self.sync()
        self.assertEqual("ok", result["status"])
        self.assertEqual(2, len(self.active()))
        self.assertEqual(2, sum(op == "insert" for op, _ in self.backend.calls))

    def test_partial_api_failure_next_run_converges(self):
        first_id = self.sync(plan=True)["operations"][0]["id"]
        def failure(event_id):
            if event_id == first_id:
                raise GoogleError("google_http_400", status=400)
        self.backend.hooks["insert"] = failure
        result = self.sync()
        self.assertEqual("partial_failure", result["status"])
        self.assertEqual(1, len(self.active()))
        self.backend.hooks.clear()
        repaired = self.sync()
        self.assertEqual((1, 1), (repaired["counts"]["created"], repaired["counts"]["unchanged"]))

    def test_412_refetch_recomputes_and_attendees_can_stop_patch(self):
        self.sync()
        value = self.active()[0]
        value["summary"] = "edited"
        def concurrent(event_id):
            self.backend.hooks.pop("patch")
            self.backend.events[event_id].update(etag='"99"', attendees=[{"email": "guest@example.test"}])
            raise GoogleError("google_http_412", status=412)
        self.backend.hooks["patch"] = concurrent
        result = self.sync()
        self.assertEqual("google_attendees_require_review", result["failures"][0]["code"])
        self.assertEqual("edited", value["summary"])

    def test_source_change_during_sync_stops_subsequent_writes(self):
        def change_source(event_id):
            self.state["pending"] = []
            self.source.write_text(json.dumps(self.state) + "\n ", encoding="utf-8")
        self.backend.hooks["inserted"] = change_source
        result = self.sync()
        self.assertEqual(1, len(self.active()))
        self.assertEqual("google_input_changed", result["error"]["code"])
        self.assertEqual(1, result["counts"]["failed"])
        self.assertEqual(1, len(result["failures"]))
        self.assertEqual("__input__", result["failures"][0]["id"])

    def single_record_source(self):
        self.data["weekly_learning"] = []
        atomic_write_json(self.run / "course__c101.json", self.data)

    def add_source_event(self):
        self.state["natural_events"] = [event("added-during-final-work")]
        atomic_write_json(self.source, self.state)

    def assert_final_input_failure(self, result, *, status="partial_failure"):
        self.assertEqual(status, result["status"])
        self.assertEqual({"code": "google_input_changed"}, result["error"])
        self.assertEqual(1, result["counts"]["failed"])
        self.assertEqual([{"id": "__input__", "code": "google_input_changed"}], result["failures"])
        saved = load_sync_state(self.binding)["last_result"]
        self.assertEqual(status, saved["status"])
        self.assertEqual(result["failures"], saved["failures"])

    def test_final_insert_input_change_preserves_success_and_requests_rerun(self):
        self.single_record_source()
        def change(event_id):
            self.backend.hooks.pop("inserted")
            self.add_source_event()
        self.backend.hooks["inserted"] = change
        result = self.sync()
        self.assert_final_input_failure(result)
        self.assertEqual(1, result["counts"]["created"])
        self.assertEqual(1, len(self.active()))
        retried = self.sync()
        self.assertEqual("ok", retried["status"])
        self.assertEqual((1, 1), (retried["counts"]["created"], retried["counts"]["unchanged"]))
        self.assertFalse(any(event_id == "__input__" for _, event_id in self.backend.calls))

    def test_final_patch_input_change_is_not_reported_as_success(self):
        self.single_record_source()
        self.sync()
        self.active()[0]["summary"] = "manual edit"
        def change(event_id):
            self.backend.hooks.pop("patch")
            self.add_source_event()
        self.backend.hooks["patch"] = change
        result = self.sync()
        self.assert_final_input_failure(result)
        self.assertEqual(1, result["counts"]["updated"])
        self.assertEqual(1, len(self.active()))

    def test_final_delete_input_change_keeps_completed_delete_receipt(self):
        self.single_record_source()
        self.sync()
        self.data["assignments"] = []
        atomic_write_json(self.run / "course__c101.json", self.data)
        def change(event_id):
            self.backend.hooks.pop("delete")
            self.add_source_event()
        self.backend.hooks["delete"] = change
        result = self.sync()
        self.assert_final_input_failure(result)
        self.assertEqual(1, result["counts"]["deleted"])
        self.assertEqual([], self.active())

    def test_final_noop_input_change_is_detected(self):
        self.single_record_source()
        self.sync()
        execute = google_calendar_module._execute_action
        def change_after_noop(*args, **kwargs):
            result = execute(*args, **kwargs)
            self.add_source_event()
            return result
        self.backend.calls.clear()
        with mock.patch("hylms.google_calendar._execute_action", side_effect=change_after_noop):
            result = self.sync()
        self.assert_final_input_failure(result)
        self.assertEqual(1, result["counts"]["unchanged"])
        self.assertFalse(any(operation in {"insert", "patch", "delete"} for operation, _ in self.backend.calls))

    def test_zero_actions_input_change_reports_failed(self):
        for key in ("assignments", "quizzes", "discussions", "weekly_learning"):
            self.data[key] = []
        atomic_write_json(self.run / "course__c101.json", self.data)
        make_result = google_calendar_module._result
        def change_after_planning(*args, **kwargs):
            result = make_result(*args, **kwargs)
            self.add_source_event()
            return result
        with mock.patch("hylms.google_calendar._result", side_effect=change_after_planning):
            result = self.sync()
        self.assert_final_input_failure(result, status="failed")
        self.assertEqual([], result["operations"])
        self.assertEqual({}, self.backend.events)

    def test_existing_fatal_error_is_not_replaced_by_final_input_check(self):
        def fail_auth(event_id):
            self.add_source_event()
            raise GoogleError("reauth_required")
        self.backend.hooks["insert"] = fail_auth
        result = self.sync()
        self.assertEqual({"code": "reauth_required"}, result["error"])
        self.assertEqual(1, len(result["failures"]))
        self.assertNotEqual("__input__", result["failures"][0]["id"])
        self.assertEqual(1, result["counts"]["failed"])
        self.assertEqual({}, self.backend.events)

    def test_credential_rollback_failure_stops_further_google_operations(self):
        def fail_credential(event_id):
            raise GoogleError("google_credential_rollback_failed")
        self.backend.hooks["insert"] = fail_credential
        result = self.sync()
        self.assertEqual({"code": "google_credential_rollback_failed"}, result["error"])
        self.assertEqual(1, result["counts"]["failed"])
        self.assertEqual(1, sum(operation == "insert" for operation, _ in self.backend.calls))
        self.assertEqual({}, self.backend.events)

    def test_persistence_failure_before_write_stops_all_remote_mutations(self):
        with mock.patch("hylms.google_calendar.atomic_write_json", side_effect=OSError):
            result = self.sync()
        self.assertEqual("google_sync_state_write_failed", result["error"]["code"])
        self.assertEqual({}, self.backend.events)

    def test_persistence_failure_after_remote_success_recovers_next_run(self):
        def fail_save(event_id):
            self.backend.hooks.pop("inserted")
            self.patch_save = mock.patch("hylms.google_calendar.atomic_write_json", side_effect=OSError)
            self.patch_save.start()
        self.backend.hooks["inserted"] = fail_save
        try:
            result = self.sync()
        finally:
            self.patch_save.stop()
        self.assertEqual(1, len(self.active()))
        self.assertEqual("google_sync_state_write_failed", result["error"]["code"])
        self.assertEqual("ok", self.sync()["status"])
        self.assertEqual(2, len(self.active()))

    def test_all_day_timed_and_point_transitions_clear_other_date_type(self):
        natural = event("natural", kind="action", mode="deadline", all_day=True, start=None, end="2026-09-07")
        self.state["natural_events"] = [natural]
        atomic_write_json(self.source, self.state)
        self.sync()
        original_ids = set(self.backend.events)
        natural["timing"] = {"all_day": False, "mode": "session", "start": "2026-09-07T15:00:00+09:00", "end": "2026-09-07T15:00:00+09:00", "end_inclusive": False}
        atomic_write_json(self.source, self.state)
        result = self.sync()
        self.assertEqual(1, result["counts"]["updated"])
        point = next(e for e in self.active() if e.get("endTimeUnspecified"))
        self.assertNotIn("date", point["start"])
        self.assertEqual(original_ids, set(self.backend.events))
        natural["timing"] = {"all_day": True, "mode": "deadline", "start": None, "end": "2026-09-07", "end_inclusive": True}
        atomic_write_json(self.source, self.state)
        self.assertEqual(1, self.sync()["counts"]["updated"])
        self.assertNotIn("dateTime", point["start"])
        self.assertEqual("2026-09-08", point["end"]["date"])

    def test_seven_day_periods_compact_to_deadline_without_changing_source(self):
        self.state["natural_events"] = [
            event("six", kind="activity", mode="period", all_day=True, start="2026-09-01", end="2026-09-06"),
            event("seven", kind="activity", mode="period", all_day=True, start="2026-09-01", end="2026-09-07"),
            event("report", kind="submission", mode="deadline", all_day=True, start="2026-11-02", end="2026-11-27"),
            event("timed", kind="submission", mode="deadline", start="2026-11-03T00:00:00+09:00", end="2026-11-18T23:59:00+09:00"),
        ]
        for value in self.state["natural_events"]:
            value["title"] = value["id"]
        atomic_write_json(self.source, self.state)
        before = self.source.read_bytes()
        self.assertEqual("ok", self.sync()["status"])
        events = {e["summary"]: e for e in self.active()}
        self.assertEqual("2026-09-01", events["six"]["start"]["date"])
        self.assertEqual("2026-09-07", events["seven"]["start"]["date"])
        self.assertEqual("2026-09-08", events["seven"]["end"]["date"])
        self.assertEqual("2026-11-27", events["report"]["start"]["date"])
        self.assertIn("2026-11-02 ~ 2026-11-27", events["report"]["description"])
        self.assertEqual("2026-11-18T23:59:00+09:00", events["timed"]["start"]["dateTime"])
        self.assertEqual("2026-11-19T00:00:00+09:00", events["timed"]["end"]["dateTime"])
        self.assertIn("2026-11-03 00:00:00", events["timed"]["description"])
        self.assertEqual(6, self.sync()["counts"]["unchanged"])
        self.assertEqual(before, self.source.read_bytes())

    def test_lock_and_source_path_guard(self):
        with sync_lock(self.binding):
            with self.assertRaises(GoogleError) as caught:
                self.sync()
        self.assertEqual("busy", caught.exception.code)
        with self.assertRaises(GoogleError):
            sync_google_calendar(self.term, self.source, state_path=self.source, client=self.backend, clock=lambda: NOW)

    def test_binding_repeat_wrong_marker_missing_calendar(self):
        count = sum(op == "create_calendar" for op, _ in self.backend.calls)
        initialize_calendar(self.backend, self.binding)
        self.assertEqual(count, sum(op == "create_calendar" for op, _ in self.backend.calls))
        self.backend.calendars[self.calendar_id]["description"] = "other"
        with self.assertRaises(GoogleError):
            self.sync()
        self.backend.calendars.clear()
        with self.assertRaises(GoogleError) as caught:
            self.sync()
        self.assertEqual("setup_required", caught.exception.code)

    def test_calendar_create_response_loss_requires_explicit_recovery(self):
        other_state = self.root / "new-binding.json"
        real_create = self.backend.create_calendar
        def lose(installation_id):
            real_create(installation_id)
            raise GoogleError("google_transport_error")
        with mock.patch.object(self.backend, "create_calendar", side_effect=lose):
            with self.assertRaises(GoogleError):
                initialize_calendar(self.backend, other_state)
        self.assertEqual("creating", load_sync_state(other_state)["phase"])
        with self.assertRaises(GoogleError) as caught:
            initialize_calendar(self.backend, other_state)
        self.assertEqual("google_calendar_creation_uncertain", caught.exception.code)
        result = initialize_calendar(self.backend, other_state, recover_id=self.calendar_id)
        self.assertEqual("ready", result["status"])


if __name__ == "__main__":
    unittest.main()
