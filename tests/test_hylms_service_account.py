import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hylms.google_auth import GoogleError, TOKEN_URL
from hylms.google_service_account import (PROJECT, SCOPES, ServiceAccountAuth,
    ServiceAccountStore, TokenRequest, library, validate_key)
from hylms.google_calendar import (GoogleCalendarClient, auth_for_binding, binding_identity,
    calendar_marker, google_event_id, initialize_calendar, load_sync_state,
    service_account_setup, validate_sync_state, main)
from hylms.http import HttpResponse
from tests.test_hylms_google_calendar import Transport, response


def key():
    library()
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {"type": "service_account", "project_id": PROJECT,
            "client_email": f"hylms-calendar-sync@{PROJECT}.iam.gserviceaccount.com",
            "client_id": "123456789", "private_key_id": "test-key",
            "private_key": private.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
            "token_uri": TOKEN_URL}


class ServiceAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = key()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "key.dpapi"
        self.store = ServiceAccountStore(self.path, protect=lambda b: b"encrypted:" + b,
            unprotect=lambda b: b.removeprefix(b"encrypted:"), owner_check=lambda: None)

    def test_key_validation_rejects_wrong_project_endpoint_and_key(self):
        for field, value in (("project_id", "other"), ("token_uri", "https://example.com"),
                             ("private_key", "bad"), ("type", "authorized_user"),
                             ("client_email", "somebody@gmail.com")):
            candidate = {**self.key, field: value}
            with self.subTest(field=field), self.assertRaises(GoogleError):
                validate_key(candidate)

    def test_store_roundtrip_and_corruption(self):
        self.store.write(self.key)
        self.assertEqual(self.store.read(), self.key)
        self.path.write_bytes(b"bad")
        with self.assertRaises(GoogleError) as caught:
            self.store.read()
        self.assertEqual(caught.exception.code, "google_sa_decryption_failed")

    def test_missing_key_and_wrong_user(self):
        with self.assertRaises(GoogleError) as caught:
            self.store.read()
        self.assertEqual(caught.exception.code, "google_sa_key_missing")
        self.store.owner_check = mock.Mock(side_effect=GoogleError("runtime_execution_context_required"))
        with self.assertRaises(GoogleError):
            self.store.write(self.key)
        self.assertFalse(self.path.exists())

    def test_write_failure_preserves_previous_ciphertext(self):
        self.store.write(self.key)
        previous = self.path.read_bytes()
        with mock.patch("hylms.google_service_account.os.replace", side_effect=OSError):
            with self.assertRaises(GoogleError):
                self.store.write(self.key)
        self.assertEqual(self.path.read_bytes(), previous)

    def test_readback_failure_never_installs(self):
        self.store.unprotect = lambda b: b"wrong"
        with self.assertRaises(GoogleError):
            self.store.write(self.key)
        self.assertFalse(self.path.exists())

    def test_real_library_refresh_and_memory_cache(self):
        self.store.write(self.key)
        transport = Transport([response(body={"access_token": "TOKEN", "expires_in": 3600, "token_type": "Bearer"})])
        auth = ServiceAccountAuth(self.store, request=TokenRequest(transport))
        self.assertEqual(auth.access_token(), "TOKEN")
        self.assertEqual(auth.access_token(), "TOKEN")
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("TOKEN", json.dumps(auth.status()))
        self.assertNotIn(self.key["private_key"], json.dumps(auth.status()))

    def test_token_failure_redacts_server_body(self):
        self.store.write(self.key)
        transport = Transport([response(400, {"error": "invalid_grant", "error_description": "SECRET"})])
        auth = ServiceAccountAuth(self.store, request=TokenRequest(transport))
        with self.assertRaises(GoogleError) as caught:
            auth.access_token()
        self.assertEqual(str(caught.exception), "google_sa_token_failed")
        self.assertEqual(self.store.read(), self.key)

    def test_transport_rejects_other_endpoints(self):
        transport = Transport([])
        with self.assertRaises(GoogleError):
            TokenRequest(transport)("https://example.com", method="POST")
        self.assertFalse(transport.requests)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "binding.json"
        self.uid = "a" * 64 + "@hylms.local"
        self.binding = {"schema_version": 1, "client_id": "old.apps.googleusercontent.com",
            "installation_id": "a" * 32, "calendar_id": "calendar", "phase": "ready",
            "events": {self.uid: {"id": google_event_id(self.uid), "term": "26-2", "generation": 0, "confirmed": True}},
            "last_result": None}
        self.path.write_text(json.dumps(self.binding))
        self.original = self.path.read_bytes()
        self.auth = mock.Mock(auth_type="service_account", allowed_roles={"writer", "owner"})
        self.auth.load.return_value = {"client_email": f"hylms-calendar-sync@{PROJECT}.iam.gserviceaccount.com"}
        self.auth.status.return_value = {"status": "configured", "auth_type": "service_account"}
        self.remote = {"id": google_event_id(self.uid), "status": "confirmed", "extendedProperties": {"private": {
            "hylms_uid": self.uid, "hylms_owner": "a" * 32, "hylms_term": "26-2", "hylms_generation": "0"}}}
        self.client = mock.Mock(auth=self.auth)
        self.client.get_calendar.return_value = {"id": "calendar", "description": calendar_marker("a" * 32)}
        self.client.list_events.return_value = [self.remote]
        patch = mock.patch("hylms.google_calendar.GoogleCalendarClient", return_value=self.client)
        patch.start()
        self.addCleanup(patch.stop)

    def test_readonly_check_does_not_migrate(self):
        result = service_account_setup("check", self.path, auth=self.auth)
        self.assertFalse(result["activated"])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.glob("*.bak")), [])

    def test_migration_preserves_every_nonidentity_field_and_backup(self):
        result = service_account_setup("activate", self.path, auth=self.auth)
        migrated = load_sync_state(self.path)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(binding_identity(migrated)["type"], "service_account")
        for field in self.binding.keys() - {"schema_version", "client_id"}:
            self.assertEqual(migrated[field], self.binding[field])
        self.assertEqual(Path(result["backup"]).read_bytes(), self.original)

    def test_invisible_or_mismatched_event_does_not_activate(self):
        self.client.list_events.return_value = []
        self.client.get_event.return_value = {"id": google_event_id(self.uid), "status": "missing"}
        with self.assertRaises(GoogleError):
            service_account_setup("activate", self.path, auth=self.auth)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.client.list_events.return_value = [{**self.remote, "extendedProperties": {}}]
        with self.assertRaises(GoogleError):
            service_account_setup("activate", self.path, auth=self.auth)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_wrong_calendar_and_save_failure_preserve_state(self):
        self.client.get_calendar.return_value["description"] = "other"
        with self.assertRaises(GoogleError):
            service_account_setup("activate", self.path, auth=self.auth)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.client.get_calendar.return_value["description"] = calendar_marker("a" * 32)
        with mock.patch("hylms.google_calendar.save_sync_state", side_effect=GoogleError("google_sync_state_write_failed")):
            with self.assertRaises(GoogleError):
                service_account_setup("activate", self.path, auth=self.auth)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_service_account_cannot_create_calendar(self):
        with self.assertRaises(GoogleError) as caught:
            initialize_calendar(self.client, self.path)
        self.assertEqual(caught.exception.code, "google_sa_creation_forbidden")
        self.client.create_calendar.assert_not_called()


class AccessTests(unittest.TestCase):
    def test_writer_allowed_but_reader_rejected(self):
        auth = mock.Mock(auth_type="service_account", allowed_roles={"writer", "owner"})
        auth.access_token.return_value = "test-token"
        for role in ["writer", "owner", "reader", "none", "writerWithoutPrivateAccess"]:
            client = GoogleCalendarClient(auth, transport=Transport([response(body={"accessRole": role, "items": []})]))
            if role in {"writer", "owner"}:
                self.assertEqual(client.list_events("calendar"), [])
            else:
                with self.assertRaises(GoogleError):
                    client.list_events("calendar")

    def test_v2_selector_never_falls_back_to_oauth(self):
        binding = {"schema_version": 2, "auth": {"type": "service_account", "principal_id": "test"}}
        with mock.patch("hylms.google_service_account.ServiceAccountAuth", side_effect=GoogleError("google_sa_key_missing")), mock.patch("hylms.google_calendar.GoogleAuth") as oauth:
            with self.assertRaises(GoogleError):
                auth_for_binding(binding)
            oauth.assert_not_called()

    def test_sa_login_rejected_without_browser(self):
        auth = mock.Mock(auth_type="service_account")
        with tempfile.TemporaryDirectory() as folder:
            output = []
            self.assertEqual(main(["auth", "login", "--sync-state", str(Path(folder)/"binding.json")], auth=auth, out=output.append), 1)
            self.assertIn("google_sa_login_not_applicable", output[0])
            auth.login.assert_not_called()
