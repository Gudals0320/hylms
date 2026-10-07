"""Windows Credential Manager persistence for the Canvas PAT."""

from __future__ import annotations

import ctypes
import json
import os
import secrets
from dataclasses import dataclass

from .core import CREDENTIAL_TARGET, CredentialStoreError, parse_iso_datetime

@dataclass(frozen=True)
class CredentialRecord:
    token: str
    token_id: str | None
    expires_at: str
    version: int = 1

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "version": self.version,
                "token": self.token,
                "token_id": self.token_id,
                "expires_at": self.expires_at,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, value: bytes) -> "CredentialRecord":
        try:
            payload = json.loads(value.decode("utf-8"))
            token = payload["token"]
            expires_at = payload["expires_at"]
            version = payload.get("version", 1)
            token_id = payload.get("token_id")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CredentialStoreError(
                "credential_invalid", "저장된 Canvas credential 형식이 올바르지 않습니다."
            ) from exc
        if version != 1 or not isinstance(token, str) or not token:
            raise CredentialStoreError(
                "credential_invalid", "저장된 Canvas credential 형식이 올바르지 않습니다."
            )
        try:
            parse_iso_datetime(expires_at)
        except ValueError as exc:
            raise CredentialStoreError(
                "credential_invalid", "저장된 Canvas credential의 만료일 형식이 올바르지 않습니다."
            ) from exc
        return cls(token=token, token_id=str(token_id) if token_id is not None else None,
                   expires_at=expires_at, version=version)


class WindowsCredentialStore:
    """Small ctypes wrapper around Windows Credential Manager."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self, target: str = CREDENTIAL_TARGET, *,
                 comment: str = "Hanyang Canvas PAT for hylms_snapshot.py",
                 username: str = "Canvas PAT") -> None:
        if os.name != "nt":
            raise CredentialStoreError(
                "credential_platform_unsupported", "Windows Credential Manager는 Windows에서만 사용할 수 있습니다."
            )
        self.target = target
        self.comment = comment
        self.username = username
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._configure_types()

    def _configure_types(self) -> None:
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
            _fields_ = [
                ("Keyword", wintypes.LPWSTR),
                ("Flags", wintypes.DWORD),
                ("ValueSize", wintypes.DWORD),
                ("Value", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.POINTER(CREDENTIAL_ATTRIBUTEW)),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._CREDENTIALW = CREDENTIALW
        self._PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(self._PCREDENTIALW),
        ]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi32.CredFree.restype = None

    def _read_bytes(self, target: str) -> bytes | None:
        pointer = self._PCREDENTIALW()
        if not self._advapi32.CredReadW(target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError(
                "credential_read_failed", f"Windows Credential Manager 읽기에 실패했습니다(Win32 {error})."
            )
        try:
            credential = pointer.contents
            if not credential.CredentialBlobSize:
                return b""
            return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        finally:
            self._advapi32.CredFree(pointer)

    def _write_bytes(self, target: str, value: bytes) -> None:
        blob = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        credential = self._CREDENTIALW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.Comment = self.comment
        credential.CredentialBlobSize = len(value)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = self.username
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()
            raise CredentialStoreError(
                "credential_write_failed", f"Windows Credential Manager 쓰기에 실패했습니다(Win32 {error})."
            )

    def _delete_target(self, target: str, missing_ok: bool = True) -> None:
        if self._advapi32.CredDeleteW(target, self.CRED_TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if missing_ok and error == self.ERROR_NOT_FOUND:
            return
        raise CredentialStoreError(
            "credential_delete_failed", f"Windows Credential Manager 삭제에 실패했습니다(Win32 {error})."
        )

    def read(self) -> CredentialRecord | None:
        value = self._read_bytes(self.target)
        return None if value is None else CredentialRecord.from_bytes(value)

    def write(self, record: CredentialRecord) -> None:
        self._write_bytes(self.target, record.to_bytes())

    def delete(self) -> None:
        self._delete_target(self.target)

    def self_test(self) -> None:
        target = f"{self.target}:self-test:{secrets.token_hex(8)}"
        marker = secrets.token_bytes(32)
        primary_error: BaseException | None = None
        try:
            self._write_bytes(target, marker)
            if self._read_bytes(target) != marker:
                raise CredentialStoreError(
                    "credential_self_test_failed", "Credential Manager readback self-test가 일치하지 않습니다."
                )
        except BaseException as exc:
            primary_error = exc
        finally:
            try:
                self._delete_target(target)
            except CredentialStoreError as cleanup_error:
                if primary_error is None:
                    raise CredentialStoreError(
                        "credential_self_test_failed", "Credential Manager delete self-test에 실패했습니다."
                    ) from cleanup_error
        if primary_error is not None:
            raise primary_error
