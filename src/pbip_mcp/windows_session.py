"""Read-only session checks and optional per-session GUI-worker serialization.

Win32 contracts:
https://learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation
https://learn.microsoft.com/windows/win32/api/wtsapi32/nf-wtsapi32-wtsquerysessioninformationw
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-openinputdesktop
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw
https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-createmutexw
https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject
https://learn.microsoft.com/windows/win32/termserv/kernel-object-namespaces
"""

from __future__ import annotations

import ctypes
import math
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .errors import DemoError


_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_ERROR_INSUFFICIENT_BUFFER = 122
_SESSION_MUTEX_NAME = "Local\\PBIPMCP.PowerBIDesktop.Conversion"
__all__ = ["interactive_readiness", "desktop_session_mutex", "OwnedProcess"]


def __getattr__(name: str) -> Any:
    if name == "OwnedProcess":
        # Keep both public import locations without eagerly creating a circular import.
        from .windows_desktop import OwnedProcess

        return OwnedProcess
    raise AttributeError(f"Module {__name__!r} has no attribute {name!r}.")


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", _DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


def _bind(dll: Any, name: str, result: Any, *arguments: Any) -> Any:
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = list(arguments)
    return function


class _SessionAPI:
    """Lazy native binding; importing this module does not inspect the host."""

    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        user = ctypes.WinDLL("user32", use_last_error=True)
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        pointer = ctypes.POINTER
        self.current_process = _bind(kernel, "GetCurrentProcess", _HANDLE)
        self.current_pid = _bind(kernel, "GetCurrentProcessId", _DWORD)
        self.current_tid = _bind(kernel, "GetCurrentThreadId", _DWORD)
        self.pid_session = _bind(kernel, "ProcessIdToSessionId", _BOOL, _DWORD, pointer(_DWORD))
        self.close_handle = _bind(kernel, "CloseHandle", _BOOL, _HANDLE)
        self.open_token = _bind(advapi, "OpenProcessToken", _BOOL, _HANDLE, _DWORD, pointer(_HANDLE))
        self.token_information = _bind(
            advapi, "GetTokenInformation", _BOOL,
            _HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD, pointer(_DWORD),
        )
        self.valid_sid = _bind(advapi, "IsValidSid", _BOOL, ctypes.c_void_p)
        self.well_known_sid = _bind(advapi, "IsWellKnownSid", _BOOL, ctypes.c_void_p, ctypes.c_int)
        self.wts_query = _bind(
            wts, "WTSQuerySessionInformationW", _BOOL,
            _HANDLE, _DWORD, ctypes.c_int, pointer(ctypes.c_void_p), pointer(_DWORD),
        )
        self.wts_free = _bind(wts, "WTSFreeMemory", None, ctypes.c_void_p)
        self.open_input = _bind(user, "OpenInputDesktop", _HANDLE, _DWORD, _BOOL, _DWORD)
        self.close_desktop = _bind(user, "CloseDesktop", _BOOL, _HANDLE)
        self.thread_desktop = _bind(user, "GetThreadDesktop", _HANDLE, _DWORD)
        self.window_station = _bind(user, "GetProcessWindowStation", _HANDLE)
        self.object_information = _bind(
            user, "GetUserObjectInformationW", _BOOL,
            _HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD, pointer(_DWORD),
        )

    def is_system(self) -> bool:
        token = _HANDLE()
        if not self.open_token(self.current_process(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            length = _DWORD()
            self.token_information(token, 1, None, 0, ctypes.byref(length))  # TokenUser
            if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not 1 <= length.value <= 65536:
                raise OSError("Token identity query failed.")
            buffer = ctypes.create_string_buffer(length.value)
            if not self.token_information(token, 1, buffer, len(buffer), ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.User.Sid
            if not self.valid_sid(sid):
                raise OSError("Invalid token SID.")
            return bool(self.well_known_sid(sid, 22))  # WinLocalSystemSid, not an environment variable
        finally:
            if not self.close_handle(token):
                raise ctypes.WinError(ctypes.get_last_error())

    def session_id(self) -> int:
        value = _DWORD()
        if not self.pid_session(self.current_pid(), ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.value

    def connection_state(self, session_id: int) -> int:
        buffer = ctypes.c_void_p()
        length = _DWORD()
        if not self.wts_query(None, session_id, 8, ctypes.byref(buffer), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())  # WTSConnectState
        try:
            if not buffer.value or length.value < ctypes.sizeof(ctypes.c_int):
                raise OSError("Incomplete WTS state.")
            return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int)).contents.value
        finally:
            self.wts_free(buffer)

    def _object_name(self, handle: Any) -> str:
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        length = _DWORD()
        self.object_information(handle, 2, None, 0, ctypes.byref(length))  # UOI_NAME
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not 2 <= length.value <= 65536:
            raise OSError("Desktop name query failed.")
        characters = (length.value + ctypes.sizeof(ctypes.c_wchar) - 1) // ctypes.sizeof(ctypes.c_wchar)
        buffer = ctypes.create_unicode_buffer(characters)
        if not self.object_information(handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    def input_desktop_name(self) -> str:
        desktop = self.open_input(0, False, 0x0001)  # DESKTOP_READOBJECTS, never SwitchDesktop
        if not desktop:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return self._object_name(desktop)
        finally:
            if not self.close_desktop(desktop):
                raise ctypes.WinError(ctypes.get_last_error())

    def worker_desktop_name(self) -> str:
        # GetThreadDesktop/GetProcessWindowStation return borrowed handles.
        return self._object_name(self.thread_desktop(self.current_tid()))

    def worker_station_name(self) -> str:
        return self._object_name(self.window_station())


def interactive_readiness() -> dict[str, Any]:
    """Fail closed without enumerating sessions, processes, or user windows."""
    session_id: int | None = None

    def result(ready: bool, code: str, message: str) -> dict[str, Any]:
        return {"ready": ready, "code": code, "message": message, "session_id": session_id}

    if sys.platform != "win32":
        return result(False, "WINDOWS_REQUIRED", "Conversion requires an interactive Windows worker.")
    try:
        api = _SessionAPI()
        session_id = api.session_id()
        if api.is_system():
            return result(False, "SYSTEM_ACCOUNT_UNSUPPORTED", "Run the worker as a normal user, not SYSTEM.")
        if session_id <= 0:
            return result(False, "INTERACTIVE_SESSION_REQUIRED", "Session 0 cannot run Desktop conversion.")
        if api.connection_state(session_id) != 0:  # WTSActive
            return result(False, "RDP_SESSION_INACTIVE", "Keep the worker's RDP session connected and active.")
        if api.input_desktop_name().casefold() != "default":
            return result(False, "DESKTOP_LOCKED", "Unlock the worker's interactive Default desktop.")
        if (
            api.worker_desktop_name().casefold() != "default"
            or api.worker_station_name().casefold() != "winsta0"
        ):
            return result(False, "WORKER_DESKTOP_UNSUPPORTED", "Start the worker on the user's interactive desktop.")
        return result(True, "READY", "The worker has an active, unlocked interactive Windows session.")
    except (OSError, ValueError, AttributeError):
        return result(False, "SESSION_PROBE_FAILED", "Windows could not verify an active, unlocked user desktop.")


class _MutexAPI:
    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._create = _bind(kernel, "CreateMutexW", _HANDLE, ctypes.c_void_p, _BOOL, ctypes.c_wchar_p)
        self._wait = _bind(kernel, "WaitForSingleObject", _DWORD, _HANDLE, _DWORD)
        self._release = _bind(kernel, "ReleaseMutex", _BOOL, _HANDLE)
        self._close = _bind(kernel, "CloseHandle", _BOOL, _HANDLE)

    def create(self, name: str) -> Any:
        handle = self._create(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def wait(self, handle: Any, milliseconds: int) -> int:
        result = self._wait(handle, milliseconds)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def release(self, handle: Any) -> None:
        if not self._release(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self, handle: Any) -> None:
        if not self._close(handle):
            raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def desktop_session_mutex(timeout_seconds: float = 600.0) -> Iterator[None]:
    """Serialize cooperating GUI workers across all roots in the current RDP session.

    The fixed Local namespace name is independent of paths and user input.
    Acquire before initializing UIA/COM. This is coordination, not a security
    boundary against other software in the dedicated user's logon session.
    """
    if sys.platform != "win32":
        raise DemoError("WINDOWS_REQUIRED", "The GUI-worker mutex requires Windows.")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise DemoError("INVALID_TIMEOUT", "Mutex timeout must be a finite nonnegative number.")
    try:
        seconds = float(timeout_seconds)
    except OverflowError as exc:
        raise DemoError("INVALID_TIMEOUT", "Mutex timeout must be a finite nonnegative number.") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise DemoError("INVALID_TIMEOUT", "Mutex timeout must be a finite nonnegative number.")
    milliseconds = min(math.ceil(min(seconds, 4294967.294) * 1000), 0xFFFFFFFE)
    api: _MutexAPI | None = None
    handle: Any = None
    owned = False
    try:
        try:
            api = _MutexAPI()
            handle = api.create(_SESSION_MUTEX_NAME)
            status = api.wait(handle, milliseconds)
        except (OSError, AttributeError) as exc:
            raise DemoError("DESKTOP_MUTEX_FAILED", "Could not acquire the session's GUI-worker mutex.") from exc
        if status == 258:
            raise DemoError("DESKTOP_SESSION_BUSY", "Another GUI conversion owns this RDP session; the bounded wait expired.")
        if status in (0, 128):
            owned = True
        if status == 128:
            raise DemoError(
                "DESKTOP_SESSION_ABANDONED",
                "A previous GUI worker ended unexpectedly; verify owned-process cleanup before retrying.",
            )
        if status != 0:
            raise DemoError("DESKTOP_MUTEX_FAILED", "The session's GUI-worker mutex returned an unexpected wait result.")
        yield
    finally:
        failures: list[OSError] = []
        if api is not None and handle:
            if owned:
                try:
                    api.release(handle)
                except OSError as exc:
                    failures.append(exc)
            try:
                api.close(handle)
            except OSError as exc:
                failures.append(exc)
        if failures:
            raise DemoError("DESKTOP_MUTEX_CLEANUP_FAILED", "Could not release the session's GUI-worker mutex cleanly.") from failures[0]
