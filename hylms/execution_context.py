"""Verify the OS process user against locally configured credential ownership.
Never trust USERNAME/USERPROFILE alone or self-elevate.
"""
from __future__ import annotations

import os

from .core import HylmsError

from .config import load_config

EXPECTED_WINDOWS_USER = load_config().get("windows_user", "")


def windows_token_user():
    if os.name != "nt":
        raise HylmsError("runtime_execution_context_unknown", "Windows user context required")
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.GetUserNameW.argtypes = (wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    api.GetUserNameW.restype = wintypes.BOOL
    size = wintypes.DWORD(257)
    name = ctypes.create_unicode_buffer(size.value)
    if not api.GetUserNameW(name, ctypes.byref(size)) or not name.value:
        raise HylmsError("runtime_execution_context_unknown", "Cannot verify Windows user context")
    return name.value


def execution_context(*, user_reader=windows_token_user):
    current = user_reader()
    matches = bool(EXPECTED_WINDOWS_USER) and isinstance(current, str) and current.casefold() == EXPECTED_WINDOWS_USER.casefold()
    return {"status": "ready" if matches else "permission_required",
            "code": None if matches else "runtime_execution_context_required",
            "current_user": current, "expected_user": EXPECTED_WINDOWS_USER}


def require_execution_owner():
    if execution_context()["status"] != "ready":
        raise HylmsError("runtime_execution_context_required",
                         "Use the host permission workflow to run as the credential owner")
