"""Personal Calendar service-account authentication; no user impersonation."""
from __future__ import annotations

import copy
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .google_auth import GoogleError, GoogleTransport, TOKEN_URL, utc_now
from .execution_context import require_execution_owner

from .config import load_config

PROJECT = load_config().get("google_project", "")
SCOPES = ("https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.calendars.readonly")
LIBS = Path(__file__).resolve().parent.parent / ".hylms-runtime/service-account-libs"


def library():
    if str(LIBS) not in sys.path:
        sys.path.insert(0, str(LIBS))
    try:
        from google.oauth2 import service_account
        return service_account
    except ImportError:
        raise GoogleError("google_sa_dependency_missing") from None


def validate_key(value):
    if not isinstance(value, dict):
        raise GoogleError("google_sa_key_invalid")
    required = ("client_email", "private_key", "private_key_id", "client_id")
    if value.get("type") != "service_account" or any(not isinstance(value.get(k), str) or not value[k] for k in required):
        raise GoogleError("google_sa_key_invalid")
    if not PROJECT or value.get("project_id") != PROJECT:
        raise GoogleError("google_sa_project_mismatch")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@" + re.escape(PROJECT) + r"\.iam\.gserviceaccount\.com", value["client_email"]):
        raise GoogleError("google_sa_key_invalid")
    if value.get("token_uri") != TOKEN_URL or value.get("universe_domain", "googleapis.com") != "googleapis.com":
        raise GoogleError("google_sa_key_invalid")
    try:
        library().Credentials.from_service_account_info(value, scopes=SCOPES)
    except GoogleError:
        raise
    except Exception:
        raise GoogleError("google_sa_key_invalid") from None
    return copy.deepcopy(value)


def dpapi(data: bytes, *, decrypt=False):
    if os.name != "nt":
        raise GoogleError("google_sa_platform_unsupported")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    # UI forbidden, CURRENT USER protection (never CRYPTPROTECT_LOCAL_MACHINE).
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise GoogleError("google_sa_decryption_failed" if decrypt else "google_sa_encryption_failed")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class ServiceAccountStore:
    def __init__(self, path=None, *, protect=None, unprotect=None, owner_check=require_execution_owner):
        if path is None:
            base = os.environ.get("LOCALAPPDATA")
            if not base:
                raise GoogleError("google_sa_storage_unavailable")
            path = Path(base) / "HY-LMS/credentials/google-service-account.dpapi"
        self.path = Path(path)
        self.protect = protect or dpapi
        self.unprotect = unprotect or (lambda value: dpapi(value, decrypt=True))
        self.owner_check = owner_check

    def read(self):
        self.owner_check()
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raise GoogleError("google_sa_key_missing") from None
        except OSError:
            raise GoogleError("google_sa_storage_unavailable") from None
        try:
            value = json.loads(self.unprotect(raw))
        except GoogleError:
            raise
        except Exception:
            raise GoogleError("google_sa_decryption_failed") from None
        return validate_key(value)

    def write(self, value):
        self.owner_check()
        value = validate_key(value)
        raw = json.dumps(value, separators=(",", ":")).encode()
        encrypted = self.protect(raw)
        if self.unprotect(encrypted) != raw:
            raise GoogleError("google_sa_readback_failed")
        temp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                temp = Path(stream.name)
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            if self.unprotect(temp.read_bytes()) != raw:
                raise GoogleError("google_sa_readback_failed")
            os.replace(temp, self.path)
        except OSError:
            raise GoogleError("google_sa_storage_write_failed") from None
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)


class TokenRequest:
    """google-auth transport restricted to Google's token endpoint."""
    def __init__(self, transport=None):
        self.transport = transport or GoogleTransport()

    def __call__(self, url, method="GET", body=None, headers=None, timeout=10, **kwargs):
        if url != TOKEN_URL or method != "POST":
            raise GoogleError("google_sa_token_endpoint_invalid")
        result = self.transport.request(method, url, headers or {}, body, timeout=min(timeout or 10, 30))
        class Response:
            status = result.status
            data = result.body
            headers = result.headers
        return Response()


class ServiceAccountAuth:
    auth_type = "service_account"
    allowed_roles = {"writer", "owner"}

    def __init__(self, store=None, *, request=None, clock=utc_now):
        self.store = store or ServiceAccountStore()
        self.request, self.clock = request or TokenRequest(), clock
        self.record = None
        self.credentials = None

    def load(self):
        if self.record is None:
            self.record = self.store.read()
            self.credentials = library().Credentials.from_service_account_info(self.record, scopes=SCOPES)
        return self.record

    def access_token(self, *, force=False):
        self.load()
        expiry = self.credentials.expiry
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=dt.timezone.utc)
        if force or not self.credentials.token or expiry is None or expiry <= self.clock() + dt.timedelta(seconds=60):
            try:
                self.credentials.refresh(self.request)
            except GoogleError:
                raise
            except Exception as exc:
                code = "google_sa_transient_error" if getattr(exc, "retryable", False) else "google_sa_token_failed"
                raise GoogleError(code) from None
        return self.credentials.token

    def status(self):
        record = self.load()
        return {"status": "configured", "auth_type": self.auth_type,
                "principal_id": record["client_email"], "project_id": record["project_id"],
                "scopes": list(SCOPES), "browser_login_required": False}

    def check(self):
        self.access_token(force=True)
        return {**self.status(), "token_verified": True}
