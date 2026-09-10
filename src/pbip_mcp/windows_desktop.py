"""Owned-process Power BI Desktop conversion; never a headless PBIX writer.

Supported surface: English, standard x64 Desktop, pywinauto 0.6.9, unlocked RDP.
No desktop-wide window search, keyboard injection, focus changes, or pixel input.

References:
https://learn.microsoft.com/power-bi/developer/projects/projects-overview
https://learn.microsoft.com/windows/win32/procthread/creating-processes
https://learn.microsoft.com/windows/win32/procthread/job-objects
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-enumthreadwindows
https://learn.microsoft.com/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
https://learn.microsoft.com/windows/win32/api/winbase/ns-winbase-startupinfoexw
https://learn.microsoft.com/windows/win32/fileio/file-access-rights-constants
https://learn.microsoft.com/windows/win32/api/winver/nf-winver-getfileversioninfow
https://learn.microsoft.com/windows/win32/api/winver/nf-winver-verqueryvaluew
https://learn.microsoft.com/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationtextrange-gettext
https://learn.microsoft.com/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-get_currentariarole
https://pywinauto.readthedocs.io/en/latest/code/pywinauto.controls.uiawrapper.html
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata
import json
import math
import ntpath
import os
import re
import struct
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from collections import Counter
from dataclasses import dataclass, field
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .contracts import ConversionRequest, ConversionResult, ProjectInfo
from .config import Limits
from .errors import DemoError
from .pbix import inspect_pbix
from .windows_session import _bind, desktop_session_mutex, interactive_readiness


_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_SIZE = ctypes.c_size_t
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_ATTRIBUTE_HANDLE_LIST = 0x00020002
_ATTRIBUTE_JOB_LIST = 0x0002000D
_KILL_ON_JOB_CLOSE = 0x00002000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_MAX_PROCESSES = 4096
_MAX_WINDOWS = 128
_MAX_NODES = 2500
_MAX_DEPTH = 24
_POLL_SECONDS = 0.4


class _Deadline:
    def __init__(self, seconds: float) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise DemoError("INVALID_TIMEOUT", "Conversion timeout must be a finite positive number.")
        try:
            duration = float(seconds)
        except OverflowError as exc:
            raise DemoError("INVALID_TIMEOUT", "Conversion timeout must be a finite positive number.") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise DemoError("INVALID_TIMEOUT", "Conversion timeout must be a finite positive number.")
        self.end = time.monotonic() + duration
        if not math.isfinite(self.end):
            raise DemoError("INVALID_TIMEOUT", "Conversion timeout must produce a finite deadline.")

    @property
    def remaining(self) -> float:
        return max(0.0, self.end - time.monotonic())

    def check(self, phase: str) -> None:
        if time.monotonic() >= self.end:
            raise DemoError("DESKTOP_TIMEOUT", f"Desktop conversion timed out during {phase}.")

    def pause(self, phase: str) -> None:
        self.check(phase)
        time.sleep(min(_POLL_SECONDS, self.remaining))
        self.check(phase)


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", _DWORD), ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
        ("dwX", _DWORD), ("dwY", _DWORD), ("dwXSize", _DWORD), ("dwYSize", _DWORD),
        ("dwXCountChars", _DWORD), ("dwYCountChars", _DWORD),
        ("dwFillAttribute", _DWORD), ("dwFlags", _DWORD),
        ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", _HANDLE), ("hStdOutput", _HANDLE), ("hStdError", _HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", _DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", _BOOL)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", _HANDLE), ("thread", _HANDLE), ("pid", _DWORD), ("tid", _DWORD)]


class _FixedFileInfo(ctypes.Structure):
    _fields_ = [(name, _DWORD) for name in (
        "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS",
        "dwProductVersionMS", "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags",
        "dwFileOS", "dwFileType", "dwFileSubtype", "dwFileDateMS", "dwFileDateLS",
    )]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _DWORD), ("MinimumWorkingSetSize", _SIZE), ("MaximumWorkingSetSize", _SIZE),
        ("ActiveProcessLimit", _DWORD), ("Affinity", _SIZE),
        ("PriorityClass", _DWORD), ("SchedulingClass", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", _SIZE), ("JobMemoryLimit", _SIZE),
        ("PeakProcessMemoryUsed", _SIZE), ("PeakJobMemoryUsed", _SIZE),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", _DWORD), ("cntUsage", _DWORD), ("th32ThreadID", _DWORD),
        ("th32OwnerProcessID", _DWORD), ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32), ("dwFlags", _DWORD),
    ]


class _Native:
    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        user = ctypes.WinDLL("user32", use_last_error=True)
        version = ctypes.WinDLL("version", use_last_error=True)
        pointer = ctypes.POINTER
        self._new_job = _bind(kernel, "CreateJobObjectW", _HANDLE, ctypes.c_void_p, ctypes.c_wchar_p)
        self._job_limits = _bind(
            kernel, "SetInformationJobObject", _BOOL, _HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD,
        )
        self._create = _bind(
            kernel, "CreateProcessW", _BOOL, ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_void_p, ctypes.c_void_p, _BOOL, _DWORD, ctypes.c_void_p,
            ctypes.c_wchar_p, pointer(_StartupInfo), pointer(_ProcessInfo),
        )
        self._assign = _bind(kernel, "AssignProcessToJobObject", _BOOL, _HANDLE, _HANDLE)
        self._resume = _bind(kernel, "ResumeThread", _DWORD, _HANDLE)
        self._close = _bind(kernel, "CloseHandle", _BOOL, _HANDLE)
        self._terminate_process = _bind(kernel, "TerminateProcess", _BOOL, _HANDLE, _DWORD)
        self._terminate_job = _bind(kernel, "TerminateJobObject", _BOOL, _HANDLE, _DWORD)
        self._wait = _bind(kernel, "WaitForSingleObject", _DWORD, _HANDLE, _DWORD)
        self._exit_code = _bind(kernel, "GetExitCodeProcess", _BOOL, _HANDLE, pointer(_DWORD))
        self._initialize_attributes = _bind(
            kernel, "InitializeProcThreadAttributeList", _BOOL,
            ctypes.c_void_p, _DWORD, _DWORD, pointer(_SIZE),
        )
        self._update_attribute = _bind(
            kernel, "UpdateProcThreadAttribute", _BOOL, ctypes.c_void_p, _DWORD,
            _SIZE, ctypes.c_void_p, _SIZE, ctypes.c_void_p, pointer(_SIZE),
        )
        self._delete_attributes = _bind(kernel, "DeleteProcThreadAttributeList", None, ctypes.c_void_p)
        self._query_job = _bind(
            kernel, "QueryInformationJobObject", _BOOL,
            _HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD, pointer(_DWORD),
        )
        self._open_process = _bind(kernel, "OpenProcess", _HANDLE, _DWORD, _BOOL, _DWORD)
        self._in_job = _bind(kernel, "IsProcessInJob", _BOOL, _HANDLE, _HANDLE, pointer(_BOOL))
        self._snapshot = _bind(kernel, "CreateToolhelp32Snapshot", _HANDLE, _DWORD, _DWORD)
        self._first_thread = _bind(kernel, "Thread32First", _BOOL, _HANDLE, pointer(_ThreadEntry))
        self._next_thread = _bind(kernel, "Thread32Next", _BOOL, _HANDLE, pointer(_ThreadEntry))
        self._open_thread = _bind(kernel, "OpenThread", _HANDLE, _DWORD, _BOOL, _DWORD)
        self._thread_pid = _bind(kernel, "GetProcessIdOfThread", _DWORD, _HANDLE)
        self._window_pid = _bind(user, "GetWindowThreadProcessId", _DWORD, _HANDLE, pointer(_DWORD))
        self._visible = _bind(user, "IsWindowVisible", _BOOL, _HANDLE)
        self._window_callback = ctypes.WINFUNCTYPE(_BOOL, _HANDLE, ctypes.c_ssize_t)
        self._thread_windows = _bind(
            user, "EnumThreadWindows", _BOOL, _DWORD, self._window_callback, ctypes.c_ssize_t,
        )
        self._post = _bind(user, "PostMessageW", _BOOL, _HANDLE, _DWORD, _SIZE, ctypes.c_ssize_t)
        self._open_file = _bind(
            kernel, "CreateFileW", _HANDLE, ctypes.c_wchar_p, _DWORD, _DWORD,
            ctypes.c_void_p, _DWORD, _DWORD, _HANDLE,
        )
        self._version_size = _bind(version, "GetFileVersionInfoSizeW", _DWORD, ctypes.c_wchar_p, pointer(_DWORD))
        self._version_info = _bind(
            version, "GetFileVersionInfoW", _BOOL, ctypes.c_wchar_p, _DWORD, _DWORD, ctypes.c_void_p,
        )
        self._version_value = _bind(
            version, "VerQueryValueW", _BOOL,
            ctypes.c_void_p, ctypes.c_wchar_p, pointer(ctypes.c_void_p), pointer(_DWORD),
        )

    @staticmethod
    def _error() -> OSError:
        return ctypes.WinError(ctypes.get_last_error())

    def new_job(self) -> Any:
        job = self._new_job(None, None)
        if not job:
            raise self._error()
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            if not self._job_limits(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise self._error()
            return job
        except Exception:
            self.close(job)
            raise

    def suspended_process(self, executable: Path, document: Path) -> tuple[Any, Any, int]:
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = "winsta0\\default"
        information = _ProcessInfo()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable), str(document)]))
        if not self._create(
            str(executable), command, None, None, False, _CREATE_SUSPENDED, None,
            str(document.parent), ctypes.byref(startup), ctypes.byref(information),
        ):
            raise self._error()
        return information.process, information.thread, information.pid

    def _standard_handle(self, path: Path | None, *, reading: bool) -> Any:
        security = _SecurityAttributes()
        security.nLength = ctypes.sizeof(security)
        security.bInheritHandle = True
        if path is None:
            name, access, creation = "NUL", 0x80000000 if reading else 0x40000000, 3
        elif reading:
            name, access, creation = str(path), 0x80000000, 3  # GENERIC_READ, OPEN_EXISTING
        else:
            # Append-only, synchronous, metadata-readable. Never truncate a prior diagnostic log.
            name, access, creation = str(path), 0x00100084, 4  # FILE_APPEND_DATA, OPEN_ALWAYS
        handle = self._open_file(name, access, 3, ctypes.byref(security), creation, 0x80, None)
        if handle == _INVALID_HANDLE:
            raise self._error()
        return handle

    def suspended_command(
        self, argv: tuple[str, ...], *, cwd: Path, env: Mapping[str, str] | None,
        stdin_path: Path | None, stdout_path: Path | None, stderr_path: Path | None, job: Any,
    ) -> tuple[Any, Any, int]:
        information = _ProcessInfo()
        streams: dict[tuple[bool, str], Any] = {}
        attributes: Any = None
        initialized = False
        created = False
        failure: Exception | None = None
        cleanup_failures: list[Exception] = []
        try:
            def stream(path: Path | None, reading: bool) -> Any:
                key = (reading, ntpath.normcase(str(path)) if path is not None else "NUL")
                if key not in streams:
                    streams[key] = self._standard_handle(path, reading=reading)
                return streams[key]

            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = stream(stdin_path, True)
            startup.StartupInfo.hStdOutput = stream(stdout_path, False)
            startup.StartupInfo.hStdError = stream(stderr_path, False)
            size = _SIZE()
            self._initialize_attributes(None, 2, 0, ctypes.byref(size))
            if ctypes.get_last_error() != 122 or not 1 <= size.value <= 1024 * 1024:
                raise OSError("Could not size the process creation attribute list.")
            attributes = ctypes.create_string_buffer(size.value)
            if not self._initialize_attributes(attributes, 2, 0, ctypes.byref(size)):
                raise self._error()
            initialized = True
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p).value
            inherited = (_HANDLE * len(streams))(*streams.values())
            jobs = (_HANDLE * 1)(job)
            if not self._update_attribute(
                attributes, 0, _ATTRIBUTE_HANDLE_LIST, inherited, ctypes.sizeof(inherited), None, None,
            ):
                raise self._error()
            # Atomic assignment closes even the parent-crash gap before explicit assignment.
            # The job handle is deliberately absent from the inherited-handle allowlist.
            if not self._update_attribute(
                attributes, 0, _ATTRIBUTE_JOB_LIST, jobs, ctypes.sizeof(jobs), None, None,
            ):
                raise self._error()
            environment = _environment_buffer(env)
            flags = _CREATE_SUSPENDED | _EXTENDED_STARTUPINFO_PRESENT | _CREATE_NO_WINDOW
            if environment is not None:
                flags |= _CREATE_UNICODE_ENVIRONMENT
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            if not self._create(
                argv[0], command, None, None, True, flags, environment, str(cwd),
                ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_StartupInfo)),
                ctypes.byref(information),
            ):
                raise self._error()
            created = True
        except Exception as exc:
            failure = exc
        finally:
            if initialized:
                try:
                    self._delete_attributes(attributes)
                except Exception as exc:
                    cleanup_failures.append(exc)
            for handle in streams.values():
                try:
                    self.close(handle)
                except Exception as exc:
                    cleanup_failures.append(exc)
            if created and (failure is not None or cleanup_failures):
                try:
                    self.terminate_process(information.process)
                except Exception as exc:
                    cleanup_failures.append(exc)
                for handle in (information.thread, information.process):
                    try:
                        self.close(handle)
                    except Exception as exc:
                        cleanup_failures.append(exc)
        if cleanup_failures:
            raise DemoError(
                "OWNED_PROCESS_LAUNCH_CLEANUP_FAILED", "Could not clean up the owned process's launch resources.",
            ) from cleanup_failures[0]
        if failure is not None:
            raise failure
        return information.process, information.thread, information.pid

    def assign(self, job: Any, process: Any) -> None:
        already_assigned = _BOOL()
        if not self._in_job(process, job, ctypes.byref(already_assigned)):
            raise self._error()
        if already_assigned.value:
            return
        if not self._assign(job, process):
            raise self._error()

    def resume(self, thread: Any) -> None:
        if self._resume(thread) != 1:
            raise OSError("The new primary thread did not have exactly one suspension.")

    def close(self, handle: Any) -> None:
        if handle and not self._close(handle):
            raise self._error()

    def terminate_process(self, process: Any) -> None:
        if not self.exited(process) and not self._terminate_process(process, 1):
            raise self._error()

    def terminate_job(self, job: Any) -> None:
        if not self._terminate_job(job, 1):
            raise self._error()

    def exited(self, process: Any) -> bool:
        status = self._wait(process, 0)
        if status not in (0, 258):  # WAIT_OBJECT_0, WAIT_TIMEOUT
            raise self._error()
        return status == 0

    def exit_code(self, process: Any) -> int | None:
        if not self.exited(process):
            return None
        code = _DWORD()
        if not self._exit_code(process, ctypes.byref(code)):
            raise self._error()
        return code.value

    def wait_process(self, process: Any, milliseconds: int) -> bool:
        status = self._wait(process, milliseconds)
        if status not in (0, 258):
            raise self._error()
        return status == 0

    def members(self, job: Any) -> set[int]:
        capacity = 32
        while capacity <= _MAX_PROCESSES:
            class PidList(ctypes.Structure):
                _fields_ = [("assigned", _DWORD), ("count", _DWORD), ("pids", _SIZE * capacity)]

            data = PidList()
            if self._query_job(job, 3, ctypes.byref(data), ctypes.sizeof(data), None):
                if data.count > capacity or data.assigned > capacity:
                    raise OSError("The owned job process list is incomplete.")
                return {int(data.pids[index]) for index in range(data.count)}
            if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise self._error()
            capacity *= 2
        raise DemoError("DESKTOP_PROCESS_LIMIT", "Desktop exceeded the owned-process inspection limit.")

    def belongs(self, job: Any, pid: int) -> bool:
        if pid <= 0:
            return False
        process = self._open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            if ctypes.get_last_error() == 87:  # Process exited before OpenProcess.
                return False
            raise self._error()
        try:
            value = _BOOL()
            if not self._in_job(process, job, ctypes.byref(value)):
                raise self._error()
            return bool(value.value)
        finally:
            self.close(process)

    def window_pid(self, window: int) -> int:
        pid = _DWORD()
        if not self._window_pid(window, ctypes.byref(pid)):
            return 0
        return pid.value

    def windows(self, job: Any, deadline: _Deadline) -> tuple[int, ...]:
        members = self.members(job)
        if not members:
            return ()
        snapshot = self._snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD: identifiers only.
        if snapshot == _INVALID_HANDLE:
            raise self._error()
        threads: list[int] = []
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            available = self._first_thread(snapshot, ctypes.byref(entry))
            while available:
                deadline.check("owned thread discovery")
                if entry.th32OwnerProcessID in members:
                    threads.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                available = self._next_thread(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise self._error()
        finally:
            self.close(snapshot)
        handles: list[int] = []

        @self._window_callback
        def collect(window: Any, _parameter: int) -> bool:
            handles.append(int(window))
            return len(handles) <= _MAX_WINDOWS

        for tid in threads:
            deadline.check("owned window discovery")
            thread = self._open_thread(0x0800, False, tid)  # THREAD_QUERY_LIMITED_INFORMATION
            if not thread:
                if ctypes.get_last_error() == 87:
                    continue
                raise self._error()
            try:
                pid = self._thread_pid(thread)
                if not pid:
                    raise self._error()
                if pid in members and self.belongs(job, pid):
                    # Pin the thread object to prevent TID reuse during enumeration.
                    # FALSE also means "no windows"; it is not an API failure here.
                    self._thread_windows(tid, collect, 0)
            finally:
                self.close(thread)
            if len(handles) > _MAX_WINDOWS:
                raise DemoError("DESKTOP_WINDOW_LIMIT", "Desktop exceeded the owned-window inspection limit.")
        return tuple(
            handle for handle in dict.fromkeys(handles)
            if self.belongs(job, self.window_pid(handle)) and self._visible(handle)
        )

    def post_close(self, job: Any, window: int) -> None:
        if not self.belongs(job, self.window_pid(window)):
            raise DemoError("DESKTOP_OWNERSHIP_LOST", "The target window no longer belongs to this conversion.")
        if not self._post(window, 0x0010, 0, 0):  # WM_CLOSE, only the validated owned HWND
            raise self._error()

    def file_released(self, path: Path) -> bool:
        handle = self._open_file(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if handle == _INVALID_HANDLE:
            if ctypes.get_last_error() in (32, 33):  # Sharing/lock violation
                return False
            raise self._error()
        self.close(handle)
        return True

    def file_version(self, path: Path) -> str:
        try:
            unused = _DWORD()
            size = self._version_size(str(path), ctypes.byref(unused))
            if not ctypes.sizeof(_FixedFileInfo) <= size <= 4 * 1024 * 1024:
                raise OSError("The executable has no bounded version resource.")
            buffer = ctypes.create_string_buffer(size)
            if not self._version_info(str(path), 0, size, buffer):
                raise self._error()
            pointer = ctypes.c_void_p()
            length = _DWORD()
            if not self._version_value(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
                raise OSError("The executable has no fixed version information.")
            address = pointer.value or 0
            start = ctypes.addressof(buffer)
            if (
                length.value < ctypes.sizeof(_FixedFileInfo)
                or not start <= address <= start + size - ctypes.sizeof(_FixedFileInfo)
            ):
                raise OSError("The fixed version-information block is invalid.")
            info = ctypes.cast(pointer, ctypes.POINTER(_FixedFileInfo)).contents
            if info.dwSignature != 0xFEEF04BD:
                raise OSError("The fixed version-information signature is invalid.")
            return ".".join(str(part) for part in (
                info.dwFileVersionMS >> 16, info.dwFileVersionMS & 0xFFFF,
                info.dwFileVersionLS >> 16, info.dwFileVersionLS & 0xFFFF,
            ))
        except (OSError, ValueError) as exc:
            raise DemoError("DESKTOP_VERSION_UNVERIFIED", "Could not verify the configured Desktop executable's file version.") from exc


def _environment_buffer(environment: Mapping[str, str] | None) -> Any:
    if environment is None:
        return None
    if not isinstance(environment, Mapping):
        raise DemoError("OWNED_PROCESS_ENV_INVALID", "The process environment must be a string mapping.")
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for name, value in environment.items():
        if (
            not isinstance(name, str) or not isinstance(value, str)
            or not name or "\0" in name or "\0" in value
            or ("=" in name and re.fullmatch(r"=[A-Za-z]:", name) is None)
            or name.casefold() in seen
        ):
            raise DemoError("OWNED_PROCESS_ENV_INVALID", "The process environment contains an invalid or duplicate entry.")
        seen.add(name.casefold())
        pairs.append((name, value))
    text = "\0".join(f"{name}={value}" for name, value in sorted(pairs, key=lambda item: item[0].casefold())) + "\0"
    return ctypes.create_unicode_buffer(text)


def _process_timeout(seconds: float) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise DemoError("INVALID_TIMEOUT", "Process timeout must be a finite nonnegative number.")
    try:
        duration = float(seconds)
    except OverflowError as exc:
        raise DemoError("INVALID_TIMEOUT", "Process timeout must be a finite nonnegative number.") from exc
    if not math.isfinite(duration) or duration < 0:
        raise DemoError("INVALID_TIMEOUT", "Process timeout must be a finite nonnegative number.")
    return duration


class OwnedProcess:
    """Trusted worker-only process launcher; never expose argv/environment to MCP clients.

    Construction launches suspended, atomically assigns a kill-on-close Windows
    job, verifies ownership, and resumes. stdout/stderr append to the supplied
    paths; log_path merges both. Unspecified streams use NUL, not worker stdio.
    No shell, inherited job/queue handles, process attachment, or window search.
    Keep this object alive and use its context manager or close() after waiting.
    A GUI executable can use the same API; CREATE_NO_WINDOW only affects consoles.
    """

    def __init__(
        self, argv: Sequence[str | os.PathLike[str]], *,
        cwd: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None,
        stdout_path: str | os.PathLike[str] | None = None,
        stderr_path: str | os.PathLike[str] | None = None,
        stdin_path: str | os.PathLike[str] | None = None,
        log_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.pid = 0
        self.returncode: int | None = None
        self._api: _Native | None = None
        self._job: Any = None
        self._process: Any = None
        self._thread: Any = None
        self._assigned = False
        self._closed = False
        try:
            if sys.platform != "win32":
                raise DemoError("WINDOWS_REQUIRED", "Owned process launch requires Windows Job Objects.")
            if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not 1 <= len(argv) <= 256:
                raise DemoError("OWNED_PROCESS_ARGS_INVALID", "Provide a bounded argument sequence, not a shell command.")
            self.args = tuple(os.fspath(argument) for argument in argv)
            if any(not isinstance(argument, str) or "\0" in argument for argument in self.args):
                raise DemoError("OWNED_PROCESS_ARGS_INVALID", "Process arguments must be strings without null characters.")
            if len(subprocess.list2cmdline(self.args).encode("utf-16-le")) // 2 >= 32767:
                raise DemoError("OWNED_PROCESS_ARGS_INVALID", "The process command line exceeds the Windows limit.")
            executable = Path(self.args[0])
            if not executable.is_absolute() or not executable.is_file():
                raise DemoError("OWNED_PROCESS_EXECUTABLE_INVALID", "The worker executable must be an existing absolute file path.")
            self.cwd = Path(cwd) if cwd is not None else Path.cwd()
            if not self.cwd.is_absolute() or not self.cwd.is_dir():
                raise DemoError("OWNED_PROCESS_CWD_INVALID", "The worker process needs an existing absolute working directory.")
            if log_path is not None:
                if stdout_path is not None or stderr_path is not None:
                    raise DemoError("OWNED_PROCESS_LOG_INVALID", "Use either log_path or separate stdout/stderr paths.")
                stdout_path = stderr_path = log_path

            def resolved(path: str | os.PathLike[str] | None, *, reading: bool) -> Path | None:
                if path is None:
                    return None
                result = Path(path)
                if not result.is_absolute():
                    result = self.cwd / result
                result = result.resolve()
                if not result.parent.is_dir() or (result.exists() and not result.is_file()):
                    raise DemoError("OWNED_PROCESS_LOG_INVALID", "A standard-stream path is not a regular file in an existing directory.")
                if reading and not result.is_file():
                    raise DemoError("OWNED_PROCESS_INPUT_INVALID", "The process stdin file does not exist.")
                return result

            self.stdin_path = resolved(stdin_path, reading=True)
            self.stdout_path = resolved(stdout_path, reading=False)
            self.stderr_path = resolved(stderr_path, reading=False)
            if self.stdin_path is not None and self.stdin_path in (self.stdout_path, self.stderr_path):
                raise DemoError("OWNED_PROCESS_LOG_INVALID", "Process input and diagnostic output must not share a file.")
            _environment_buffer(env)
            environment = dict(env) if env is not None else None
            self._api = _Native()
            self._job = self._api.new_job()
            self._process, self._thread, self.pid = self._api.suspended_command(
                self.args, cwd=self.cwd, env=environment, stdin_path=self.stdin_path,
                stdout_path=self.stdout_path, stderr_path=self.stderr_path, job=self._job,
            )
            self._api.assign(self._job, self._process)
            self._assigned = True
            self._api.resume(self._thread)
            self._api.close(self._thread)
            self._thread = None
        except Exception as exc:
            self.close()
            if isinstance(exc, DemoError):
                raise
            raise DemoError("OWNED_PROCESS_LAUNCH_FAILED", "Could not launch the job-owned worker process.") from exc

    def __enter__(self) -> "OwnedProcess":
        if self._closed:
            raise DemoError("OWNED_PROCESS_CLOSED", "The owned process handle has already been closed.")
        return self

    def __exit__(self, *_exception: Any) -> None:
        self.close()

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if self._closed or self._api is None or not self._process:
            raise DemoError("OWNED_PROCESS_CLOSED", "The owned process has no open handle or verified exit code.")
        try:
            self.returncode = self._api.exit_code(self._process)
            return self.returncode
        except Exception as exc:
            raise DemoError("OWNED_PROCESS_QUERY_FAILED", "Could not query the owned worker's exit status.") from exc

    def wait(self, timeout: float = 600.0) -> int:
        """Wait for the root process, not its descendants; context exit cleans the job."""
        duration = _process_timeout(timeout)
        code = self.poll()
        if code is not None:
            return code
        milliseconds = min(math.ceil(min(duration, 4294967.294) * 1000), 0xFFFFFFFE)
        try:
            completed = self._api.wait_process(self._process, milliseconds)
        except Exception as exc:
            raise DemoError("OWNED_PROCESS_WAIT_FAILED", "Could not wait for the owned worker process.") from exc
        if not completed:
            raise subprocess.TimeoutExpired(self.args, timeout)
        code = self.poll()
        if code is None:
            raise DemoError("OWNED_PROCESS_WAIT_FAILED", "The owned process signaled without a readable exit status.")
        return code

    def terminate(self, timeout: float = 5.0) -> None:
        """Terminate only this job and verify every member exits within a finite wait."""
        duration = _process_timeout(timeout)
        if self._closed or self._api is None:
            return
        try:
            until = time.monotonic() + duration
            if self._job:
                self._api.terminate_job(self._job)
            if self._process and not self._assigned:
                self._api.terminate_process(self._process)
            while True:
                members = self._api.members(self._job) if self._job else set()
                root_alive = bool(self._process and not self._api.exited(self._process))
                if not members and not root_alive:
                    break
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise DemoError("OWNED_PROCESS_CLEANUP_TIMEOUT", "The owned worker process tree did not exit within the cleanup deadline.")
                time.sleep(min(0.05, remaining))
            if self._process:
                self.returncode = self._api.exit_code(self._process)
        except DemoError:
            raise
        except Exception as exc:
            raise DemoError("OWNED_PROCESS_TERMINATE_FAILED", "Could not terminate the owned worker process tree.") from exc

    def close(self) -> None:
        """Idempotently terminate remaining descendants and release every native handle."""
        if self._closed:
            return
        failures: list[Exception] = []
        try:
            self.terminate()
        except Exception as exc:
            failures.append(exc)
        finally:
            if self._api is not None:
                for attribute in ("_thread", "_process", "_job"):
                    handle = getattr(self, attribute)
                    if handle:
                        try:
                            self._api.close(handle)
                        except Exception as exc:
                            failures.append(exc)
                        setattr(self, attribute, None)
            self._closed = True
        if failures:
            raise DemoError("OWNED_PROCESS_CLEANUP_FAILED", "Could not verify cleanup of the owned worker process tree.") from failures[0]


class _OwnedDesktop:
    def __init__(self, api: _Native, executable: Path, document: Path, deadline: _Deadline) -> None:
        self.api, self.executable, self.document, self.deadline = api, executable, document, deadline
        self.job: Any = None
        self.process: Any = None
        self.thread: Any = None
        self.pid = 0
        self.assigned = False

    def __enter__(self) -> "_OwnedDesktop":
        try:
            self.deadline.check("launch")
            self.job = self.api.new_job()
            self.process, self.thread, self.pid = self.api.suspended_process(self.executable, self.document)
            self.api.assign(self.job, self.process)
            self.assigned = True
            self.deadline.check("launch")
            self.api.resume(self.thread)
            self.api.close(self.thread)
            self.thread = None
            return self
        except Exception as exc:
            self.close()
            if isinstance(exc, DemoError):
                raise
            raise DemoError("DESKTOP_LAUNCH_FAILED", "Could not launch an isolated, job-owned Desktop process.") from exc

    def __exit__(self, *_exception: Any) -> None:
        self.close()

    def require_live(self) -> None:
        self.deadline.check("Desktop readiness")
        if self.api.exited(self.process):
            raise DemoError(
                "DESKTOP_PROCESS_EXITED",
                "Owned Desktop exited; instance forwarding or a startup failure is unsupported.",
            )

    def require_pid(self, pid: int) -> None:
        if not self.job or not self.api.belongs(self.job, pid):
            raise DemoError(
                "DESKTOP_OWNERSHIP_LOST",
                f"UI Automation encountered unowned PID {pid}; this Desktop owner is PID {self.pid}.",
            )

    def close(self) -> None:
        failures: list[Exception] = []
        try:
            if self.assigned and self.job:
                self.api.terminate_job(self.job)
                until = time.monotonic() + min(5.0, self.deadline.remaining)
                while self.api.members(self.job):
                    if time.monotonic() >= until:
                        raise OSError("The owned process tree did not exit within the cleanup deadline.")
                    time.sleep(min(0.1, max(0.0, until - time.monotonic())))
            elif self.process:
                # Assignment failed: this exact suspended process is still ours.
                self.api.terminate_process(self.process)
        except Exception as exc:
            failures.append(exc)
        finally:
            for attribute in ("thread", "process", "job"):
                handle = getattr(self, attribute)
                if handle:
                    try:
                        self.api.close(handle)
                    except Exception as exc:
                        failures.append(exc)
                    setattr(self, attribute, None)
        if failures:
            raise DemoError("DESKTOP_CLEANUP_FAILED", "Could not verify cleanup of the owned Desktop process tree.") from failures[0]


@dataclass
class _UIRuntime:
    element: Any
    wrapper: Any
    missing_pattern: type[Exception]
    native_edit: Any = None


def _load_uia() -> _UIRuntime:
    try:
        if importlib.metadata.version("pywinauto") != "0.6.9":
            raise DemoError("UIA_VERSION_UNSUPPORTED", "The worker requires pywinauto 0.6.9.")
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.controls.win32_controls import EditWrapper
        from pywinauto.uia_defines import NoPatternInterfaceError
        from pywinauto.uia_element_info import UIAElementInfo
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise DemoError("UIA_UNAVAILABLE", "Install the Windows worker's pinned UI Automation dependencies.") from exc
    return _UIRuntime(UIAElementInfo, UIAWrapper, NoPatternInterfaceError, EditWrapper)


def _normal(value: str) -> str:
    return " ".join(value.replace("&", "").replace("\u2026", "").strip().rstrip(".").split()).casefold()


class _StaleProviderError(Exception):
    pass


def _stale_element(error: Exception) -> bool:
    hresult = getattr(error, "hresult", None)
    return isinstance(error, _StaleProviderError) or (
        isinstance(hresult, int) and (hresult & 0xFFFFFFFF) in (0x80040201, 0x80010108, 0x800401FD)
    )


@dataclass
class _Node:
    wrapper: Any
    pid: int
    root: int
    parent: int | None
    name: str
    kind: str
    auto_id: str = ""
    class_name: str = ""
    enabled: bool = True
    modal: bool = False
    depth: int = 0
    focusable: bool = False


@dataclass
class _Snapshot:
    nodes: list[_Node] = field(default_factory=list)
    truncated: bool = False

    def under(self, node: _Node, ancestor: _Node) -> bool:
        current = node
        while current.parent is not None:
            current = self.nodes[current.parent]
            if current is ancestor:
                return True
        return node is ancestor

    def within(self, ancestor: _Node) -> list[_Node]:
        return [node for node in self.nodes if self.under(node, ancestor)]

    def evidence(self) -> dict[str, Any]:
        return {
            "truncated": self.truncated,
            "nodes": [
                {
                    "pid": node.pid, "root_hwnd": node.root, "parent": node.parent,
                    "name": node.name[:512], "type": node.kind, "automation_id": node.auto_id[:128],
                    "enabled": node.enabled, "modal": node.modal, "depth": node.depth,
                    "focusable": node.focusable,
                }
                for node in self.nodes
            ],
        }


class _Evidence:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.prefix = "windows-" + uuid.uuid4().hex
        self.sequence = 0
        self.stream = (directory / f"{self.prefix}-phases.jsonl").open("x", encoding="utf-8")

    def __enter__(self) -> "_Evidence":
        return self

    def __exit__(self, *_exception: Any) -> None:
        self.stream.close()

    def record(self, phase: str, details: dict[str, Any], snapshot: _Snapshot | None = None) -> None:
        self.sequence += 1
        record = {"phase": phase, "time_unix": time.time(), "sequence": self.sequence, **details}
        if snapshot is not None:
            tree_name = f"{self.prefix}-{self.sequence:02d}-tree.json"
            with (self.directory / tree_name).open("x", encoding="utf-8") as stream:
                json.dump(snapshot.evidence(), stream, ensure_ascii=False)
            record["private_tree"] = tree_name
        self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())


_FILE_LABELS = ("File",)
_ACTION_TYPES = ("Button", "MenuItem", "TabItem", "ListItem", "SplitButton")
_PBIX_TYPES = ("Power BI (*.pbix)", "Power BI file (*.pbix)", "Power BI files (*.pbix)")
_PBIP_TYPES = ("Power BI project files (*.pbip)", "Power BI Project (*.pbip)", "Power BI project (*.pbip)")
_PROGRESS_TITLES = frozenset({
    "refresh", "loading", "loading data", "loading model", "please wait",
    "working on it", "applying query changes",
})
_VISUAL_ERRORS = (
    "error fetching data for this visual", "couldn't load the data for this visual",
    "could not load the data for this visual", "can't display this visual",
    "couldn't load the model", "unable to load the model", "something went wrong",
)
_UNPROCESSED_VISUAL_ERRORS = (
    "error fetching data for this visual", "couldn't load the data for this visual",
    "could not load the data for this visual",
)


def _dialog_problem(texts: list[str]) -> tuple[str, str] | None:
    """Only called on owned dialog/alert content, not a normal Sign in button."""
    text = _normal(" ".join(texts))
    if "confirm save as" in text or ("already exists" in text and ("replace" in text or "overwrite" in text)):
        return "OUTPUT_EXISTS", "Desktop requested an overwrite; no existing output will be replaced."
    if "sensitivity label" in text or "protected by information protection" in text:
        return "PBIX_LABEL_UNSUPPORTED", "PBIP does not support this sensitivity label or protection; labels are never removed."
    if any(label in text for label in (
        "security warning", "potential security risk", "native database query",
        "run native query", "privacy levels", "data source credentials", "permission is required",
    )):
        return "DESKTOP_SECURITY_PROMPT", "Desktop requires an operator to resolve a security or data-access prompt."
    if any(label in text for label in (
        "license agreement", "software license terms", "terms of use", "accept the terms",
    )):
        return "DESKTOP_LEGAL_PROMPT", "Desktop requires an operator to review its legal or first-run prompt."
    if any(label in text for label in (
        "sign in", "sign-in", "enter your email", "enter your password", "work or school account",
        "verify your identity", "pick an account",
    )):
        return "DESKTOP_SIGN_IN_REQUIRED", "Desktop requires interactive sign-in; automation will not enter credentials."
    if any(label in text for label in (
        *_VISUAL_ERRORS, "unable to open", "couldn't open", "cannot open", "can't open",
        "an unexpected error", "an error occurred", "failed to save", "couldn't save", "could not save",
        "error loading", "failed to load", "refresh failed", "unsupported version", "newer version of power bi",
    )):
        return "DESKTOP_REPORTED_ERROR", "Desktop reported an open, model, visual, or save error; inspect private evidence."
    if "pending changes" in text or "unapplied changes" in text:
        return "DESKTOP_PENDING_CHANGES", "Desktop requires a decision about pending query changes."
    return None


def _save_changes_dialog(texts: list[str]) -> bool:
    text = _normal(" ".join(texts))
    return any(label in text for label in (
        "do you want to save changes", "do you want to save your changes", "save changes to",
    ))


def _progress_window(node: _Node) -> bool:
    return node.kind == "Window" and (
        _normal(node.name) in _PROGRESS_TITLES or node.auto_id == "KoLoadToReportDialog"
    )


def _check_problems(
    snapshot: _Snapshot, *, allow_save: bool = False, allow_discard: bool = False,
    allow_unprocessed_visuals: bool = False,
) -> list[_Node]:
    save_prompts: list[_Node] = []
    for node in snapshot.nodes:
        if node.kind == "Text" and any(phrase in _normal(node.name) for phrase in _VISUAL_ERRORS):
            unprocessed = any(phrase in _normal(node.name) for phrase in _UNPROCESSED_VISUAL_ERRORS)
            if not (allow_unprocessed_visuals and unprocessed):
                raise DemoError("DESKTOP_REPORTED_ERROR", "Desktop reported a model or visual error; inspect private evidence.")
        dialog = node.modal or node.class_name == "#32770"
        explicit_prompt = node.kind == "Window" and _normal(node.name) in {
            "sign in", "sign in to power bi", "sign in to your account", "security warning", "confirm save as",
        }
        if not dialog and not explicit_prompt:
            continue
        texts = [child.name for child in snapshot.within(node)]
        problem = _dialog_problem(texts)
        if problem:
            raise DemoError(*problem)
        if _save_changes_dialog(texts):
            if not allow_discard:
                raise DemoError("DESKTOP_UNEXPECTED_SAVE_PROMPT", "Desktop requested an unexpected save decision.")
            save_prompts.append(node)
        elif _normal(node.name) == "save as" and allow_save:
            continue
        elif _normal(node.name) in _PROGRESS_TITLES or _progress_window(node) or any(
            _progress_window(ancestor) and snapshot.under(node, ancestor) for ancestor in snapshot.nodes
        ):
            continue
        else:
            raise DemoError("DESKTOP_DIALOG_UNSUPPORTED", "An unsupported owned Desktop dialog needs operator attention.")
    return save_prompts


def _busy(snapshot: _Snapshot) -> bool:
    return any(
        node.kind == "ProgressBar"
        or _progress_window(node)
        or (node.kind == "Text" and _normal(node.name) in _PROGRESS_TITLES - {"refresh"})
        for node in snapshot.nodes
    )


def _title_matches(title: str, names: tuple[str, ...]) -> bool:
    match = re.fullmatch(r"\s*(.*?)\s+[-\u2013]\s+(?:Microsoft\s+)?Power BI Desktop\s*", title, re.IGNORECASE)
    if not match:
        return False
    document = match.group(1).strip().strip("*").strip()
    return any(document.casefold() == name.casefold() for name in names)


def _page_label(name: str, page: str) -> bool:
    return name == page or bool(re.fullmatch(
        re.escape(page) + r"(?:, page \d+ of \d+| \(Page \d+ of \d+\))", name, re.IGNORECASE,
    ))


def _report_surface(snapshot: _Snapshot, main: _Node) -> _Node:
    if main.kind != "Window":
        return main
    surfaces = [
        node for node in snapshot.within(main)
        if node.kind == "Pane" and node.name.casefold().endswith("/minerva/reportview.html")
    ]
    if len(surfaces) > 1:
        raise DemoError("DESKTOP_WINDOW_AMBIGUOUS", "More than one report-view surface is exposed.")
    # Modern Desktop also exposes the inactive dataExploreView WebView's ribbon.
    return surfaces[0] if surfaces else main


def _inactive_view(name: str) -> bool:
    match = re.fullmatch(
        r"ms-pbi(?:://|\.)(?:pbi\.microsoft\.com/)?minerva/([^/]+view)\.html(?: - Web content)?",
        name, re.IGNORECASE,
    )
    return match is not None and match.group(1).casefold() in {
        "dataexploreview", "modelview", "daxqueryview", "tmdlview",
    }


def _observed_pages(snapshot: _Snapshot, main: _Node, expected: tuple[str, ...]) -> tuple[str, ...]:
    main = _report_surface(snapshot, main)
    page_nodes: list[_Node] = []
    for node in snapshot.within(main):
        if node.kind == "TabItem":
            page_nodes.append(node)
        elif node.kind in ("Button", "ListItem"):
            parent = node.parent
            while parent is not None:
                ancestor = snapshot.nodes[parent]
                if _normal(ancestor.name) in {"pages", "report pages", "page navigation", "report page tabs"}:
                    page_nodes.append(node)
                    break
                parent = ancestor.parent
    used: set[int] = set()
    result: list[str] = []
    for page in expected:
        for index, node in enumerate(page_nodes):
            if index not in used and _page_label(node.name, page):
                result.append(page)
                used.add(index)
                break
    return tuple(result)


def _field_containers(snapshot: _Snapshot, main: _Node) -> list[_Node]:
    main = _report_surface(snapshot, main)
    containers: list[_Node] = []
    labels = {"data", "fields", "data pane", "fields pane", "model explorer"}
    ids = {"datapane", "fieldspane", "datafieldspane", "model-explorer", "modelexplorer"}
    for node in snapshot.within(main):
        if node.kind not in ("Tree", "Pane", "Group", "Custom"):
            continue
        named = _normal(node.name) in labels or node.auto_id.casefold() in ids
        header = node.depth > 1 and any(
            child.parent is not None and snapshot.nodes[child.parent] is node
            and child.kind in ("Text", "Button") and _normal(child.name) in labels
            for child in snapshot.nodes
        )
        if named or header:
            containers.append(node)
    return containers


def _field_nodes(snapshot: _Snapshot, main: _Node) -> list[_Node]:
    main = _report_surface(snapshot, main)
    containers = _field_containers(snapshot, main)
    return [
        node for node in snapshot.within(main)
        if node.kind in ("TreeItem", "DataItem") and node.name.strip()
        and any(snapshot.under(node, container) for container in containers)
    ]


def _entity_label(name: str, expected: str, entity: str) -> bool:
    return _normal(name) in {
        _normal(expected), _normal(f"{entity} {expected}"), _normal(f"{expected} ({entity})"),
        _normal(f"{expected}, {entity}"), _normal(f"{expected} {entity}"),
        _normal(f"{entity} Field {expected}"),
    }


_METRIC_LABEL = r"\bTotal\s+Amount\b"
_METRIC_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"


@dataclass(frozen=True)
class _CardObservation:
    value: Decimal
    source: str
    text: str

    @property
    def scalar(self) -> int | str:
        return int(self.value) if self.value == self.value.to_integral_value() else format(self.value, "f")


def _metric_value(content: list[str], measure: str = "Total Amount") -> Decimal | None:
    metric_label = r"(?<!\w)" + r"\s+".join(re.escape(part) for part in measure.split()) + r"(?!\w)"
    pieces = [text.strip() for text in content if len(text) < 2048]
    if not any(re.search(metric_label, text, re.IGNORECASE) for text in pieces):
        return None
    values: set[Decimal] = set()

    def add(text: str) -> None:
        if len(text) > 64:
            return
        try:
            value = Decimal(text.replace(",", ""))
        except InvalidOperation:
            return
        if value.is_finite():
            values.add(value)

    after = (
        metric_label + r"[\]'\"]*(?:\s*[:,]\s*|\s+)(?P<number>" + _METRIC_NUMBER
        + r")(?=$|\s|[,;](?!\d))"
    )
    before = (
        r"(?<![\w.,%])(?P<number>" + _METRIC_NUMBER + r")(?:\s*,\s*|\s+)" + metric_label
    )
    for text in pieces:
        if re.fullmatch(_METRIC_NUMBER, text):
            add(text)
        for pattern in (after, before):
            for match in re.finditer(pattern, text, re.IGNORECASE):
                add(match.group("number"))
    return next(iter(values)) if len(values) == 1 else None


def _card_containers(snapshot: _Snapshot, main: _Node, measure: str = "Total Amount") -> list[_Node]:
    main = _report_surface(snapshot, main)
    fields = _field_containers(snapshot, main)
    cards: list[_Node] = []
    for node in snapshot.within(main):
        if node.kind not in ("Group", "Pane", "Custom", "Document", "Text", "Image"):
            continue
        if any(snapshot.under(node, container) for container in fields):
            continue
        label = _normal(node.name)
        tagged = bool(re.search(r"\bcard(?: visual)?\b", label))
        container = node.kind in ("Group", "Pane", "Custom", "Document") and (
            label == _normal(measure) or node.auto_id.casefold() in {"visual-container", "visualcontainer"}
        )
        named_image = node.kind == "Image" and _normal(measure) in label
        if tagged or container or named_image:
            cards.append(node)
    return cards


def _named_card_observation(
    snapshot: _Snapshot, main: _Node, measure: str = "Total Amount",
) -> _CardObservation | None:
    observations: list[_CardObservation] = []
    for node in _card_containers(snapshot, main, measure):
        content = [child.name for child in snapshot.within(node)]
        value = _metric_value(content, measure)
        if value is not None:
            observations.append(_CardObservation(value, "uia_accessible_name", "\n".join(content)[:2048]))
    if observations and len({item.value for item in observations}) == 1:
        return observations[0]
    return None


def _card_value_observed(snapshot: _Snapshot, main: _Node) -> bool:
    observation = _named_card_observation(snapshot, main)
    return observation is not None and observation.value == Decimal(60)


class _Automation:
    def __init__(
        self, owned: _OwnedDesktop, deadline: _Deadline, runtime: _UIRuntime, evidence: _Evidence,
    ) -> None:
        self.owned, self.deadline, self.runtime, self.evidence = owned, deadline, runtime, evidence
        self.last_snapshot = _Snapshot()
        self.main_handle: int | None = None
        self.excluded_views: set[str] = set()

    def snapshot(self, phase: str, *, require_live: bool = True) -> _Snapshot:
        while True:
            self.deadline.check(phase)
            if require_live:
                self.owned.require_live()
            snapshot = _Snapshot()
            self.last_snapshot = snapshot
            try:
                handles = self.owned.api.windows(self.owned.job, self.deadline)
                for handle in handles:
                    window_pid = self.owned.api.window_pid(handle)
                    if window_pid == 0:
                        raise _StaleProviderError("The owned window was destroyed after enumeration.")
                    self.owned.require_pid(window_pid)
                    element = self.runtime.element(handle)
                    pending: list[tuple[Any, int | None, int]] = [(element, None, 0)]
                    while pending:
                        self.deadline.check(phase)
                        info, parent, depth = pending.pop()
                        # No text, type, or child traversal before checking this provider's PID.
                        provider_pid = info.process_id
                        if provider_pid in (None, 0):
                            # Destroyed providers return None or zero while a native dialog closes.
                            raise _StaleProviderError("The provider disappeared before its PID could be read.")
                        pid = int(provider_pid)
                        self.owned.require_pid(pid)
                        wrapper = self.runtime.wrapper(info)
                        if not wrapper.is_visible():
                            continue
                        kind = info.control_type
                        if parent is not None and info.handle != handle and info.handle in handles:
                            # Native dialogs and popup lists also appear beneath their owners.
                            continue
                        name = (info.name or "")[:2048]
                        if kind == "Pane" and _inactive_view(name):
                            if name not in self.excluded_views:
                                self.evidence.record("inactive-view-excluded", {"view": name, "pid": pid})
                                self.excluded_views.add(name)
                            continue
                        modal = False
                        if kind == "Window":
                            try:
                                modal = bool(wrapper.iface_window.CurrentIsModal)
                            except self.runtime.missing_pattern:
                                modal = False
                        if kind in ("Window", "Pane", "Group", "Custom", "Document"):
                            aria_role = str(info.element.CurrentAriaRole or "").casefold()
                            modal |= aria_role in ("dialog", "alertdialog")
                        node = _Node(
                            wrapper, pid, handle, parent, name, kind,
                            (info.automation_id or "")[:256], (info.class_name or "")[:256],
                            bool(wrapper.is_enabled()), modal, depth, bool(wrapper.is_keyboard_focusable()),
                        )
                        index = len(snapshot.nodes)
                        snapshot.nodes.append(node)
                        children = info.children()
                        if len(snapshot.nodes) >= _MAX_NODES or (depth >= _MAX_DEPTH and children):
                            snapshot.truncated = True
                            raise DemoError("UIA_TREE_LIMIT", "The owned UI exceeds the bounded inspection limit.")
                        pending.extend((child, index, depth + 1) for child in reversed(children))
                return snapshot
            except DemoError:
                raise
            except Exception as exc:
                if _stale_element(exc):
                    self.deadline.pause(phase)
                    continue
                raise DemoError("UIA_READ_FAILED", "Could not read the owned Desktop accessibility tree.") from exc

    def find(
        self, snapshot: _Snapshot, labels: tuple[str, ...], kinds: tuple[str, ...] = _ACTION_TYPES,
        *, ids: tuple[str, ...] = (), within: _Node | None = None,
    ) -> _Node | None:
        if within is not None:
            within = _report_surface(snapshot, within)
        names = {_normal(label) for label in labels}
        candidates = [
            node for node in snapshot.nodes
            if node.kind in kinds and node.enabled
            and (within is None or snapshot.under(node, within))
            and (_normal(node.name) in names or (not node.name and node.auto_id in ids))
        ]
        with_ids = [node for node in candidates if node.auto_id in ids]
        candidates = with_ids or candidates
        if len(candidates) > 1:
            outer = [
                node for node in candidates
                if not any(other is not node and snapshot.under(node, other) for other in candidates)
            ]
            if len(outer) == 1:
                leaves = [
                    node for node in candidates
                    if not any(other is not node and snapshot.under(other, node) for other in candidates)
                ]
                actions = []
                for node in leaves:
                    self.owned.require_pid(int(node.wrapper.element_info.process_id))
                    try:
                        node.wrapper.iface_invoke
                    except self.runtime.missing_pattern:
                        continue
                    try:
                        node.wrapper.iface_expand_collapse
                    except self.runtime.missing_pattern:
                        actions.append(node)
                if len(actions) == 1:
                    candidates = actions
        if len(candidates) > 1:
            raise DemoError("UIA_AMBIGUOUS_CONTROL", "A supported Desktop control is ambiguous; inspect private evidence.")
        return candidates[0] if candidates else None

    def _guard(self, node: _Node) -> None:
        self.deadline.check("UI Automation action")
        readiness = interactive_readiness()
        if not readiness["ready"]:
            raise DemoError(readiness["code"], readiness["message"])
        self.owned.require_live()
        self.owned.require_pid(self.owned.api.window_pid(node.root))
        self.owned.require_pid(int(node.wrapper.element_info.process_id))
        if not node.wrapper.is_enabled():
            raise DemoError("UIA_CONTROL_DISABLED", "The expected Desktop control is not enabled.")

    def activate(self, node: _Node, *, selection_only: bool = False) -> None:
        self._guard(node)
        try:
            if selection_only or node.kind == "TabItem":
                try:
                    node.wrapper.iface_selection_item.Select()
                    return
                except self.runtime.missing_pattern:
                    if selection_only:
                        raise
            try:
                node.wrapper.iface_invoke.Invoke()
            except self.runtime.missing_pattern:
                node.wrapper.iface_selection_item.Select()
        except self.runtime.missing_pattern as exc:
            raise DemoError("UIA_PATTERN_UNSUPPORTED", "The expected control lacks an Invoke or Selection pattern.") from exc
        except Exception as exc:
            raise DemoError("UIA_ACTION_FAILED", "An owned Desktop UI Automation action failed.") from exc

    def expand(self, node: _Node, *, expanded: bool = True) -> None:
        self._guard(node)
        try:
            pattern = node.wrapper.iface_expand_collapse
            if expanded and pattern.CurrentExpandCollapseState == 0:
                pattern.Expand()
            elif not expanded and pattern.CurrentExpandCollapseState in (1, 2):
                pattern.Collapse()
        except self.runtime.missing_pattern as exc:
            raise DemoError("UIA_PATTERN_UNSUPPORTED", "The expected control lacks an ExpandCollapse pattern.") from exc
        except Exception as exc:
            raise DemoError("UIA_ACTION_FAILED", "Could not expand the owned Desktop control.") from exc

    def focus(self, node: _Node) -> None:
        self._guard(node)
        try:
            element = node.wrapper.element_info.element
            element.SetFocus()
            until = min(self.deadline.end, time.monotonic() + 5.0)
            while not element.CurrentHasKeyboardFocus:
                if time.monotonic() >= until:
                    raise DemoError("UIA_FOCUS_UNVERIFIED", "The owned Save As control did not receive focus.")
                self.deadline.pause("Save As focus verification")
                self._guard(node)
        except DemoError:
            raise
        except Exception as exc:
            raise DemoError("UIA_ACTION_FAILED", "Could not focus the owned Save As control.") from exc

    def set_value(self, node: _Node, value: str) -> None:
        self._guard(node)
        try:
            pattern = node.wrapper.iface_value
            if pattern.CurrentIsReadOnly:
                raise DemoError("UIA_VALUE_READ_ONLY", "The Save As filename control is read-only.")
            pattern.SetValue(value)
            until = min(self.deadline.end, time.monotonic() + 5.0)
            while True:
                self._guard(node)
                actual = pattern.CurrentValue
                if ntpath.normcase(ntpath.normpath(actual)) == ntpath.normcase(ntpath.normpath(value)):
                    return
                if time.monotonic() >= until:
                    self.evidence.record("filename-value-mismatch", {
                        "expected": value, "observed": actual,
                    }, self.last_snapshot)
                    raise DemoError("UIA_VALUE_MISMATCH", "Desktop did not retain the requested Save As filename.")
                self.deadline.pause("Save As filename verification")
        except DemoError:
            raise
        except self.runtime.missing_pattern as exc:
            raise DemoError("UIA_PATTERN_UNSUPPORTED", "The Save As filename control lacks a Value pattern.") from exc
        except Exception as exc:
            raise DemoError("UIA_ACTION_FAILED", "Could not set the owned Save As filename field.") from exc

    def set_native_filename(self, node: _Node, value: str) -> None:
        self._guard(node)
        handle = node.wrapper.element_info.handle
        if node.kind != "Edit" or node.class_name != "Edit" or not handle or self.runtime.native_edit is None:
            raise DemoError("SAVE_FILENAME_UNSUPPORTED", "Save As requires a verified native Edit control.")
        self.owned.require_pid(self.owned.api.window_pid(handle))
        try:
            edit = self.runtime.native_edit(handle)
            # EM_REPLACESEL emits the native edit notifications that UIA SetValue omits here.
            edit.set_edit_text(value)
            until = min(self.deadline.end, time.monotonic() + 5.0)
            while True:
                self._guard(node)
                self.owned.require_pid(self.owned.api.window_pid(handle))
                native_value = edit.window_text()
                uia_value = str(node.wrapper.iface_value.CurrentValue)
                expected = ntpath.normcase(ntpath.normpath(value))
                if all(ntpath.normcase(ntpath.normpath(item)) == expected for item in (native_value, uia_value)):
                    self.evidence.record("native-filename-committed", {
                        "target_file": value, "native_value": native_value, "uia_value": uia_value,
                    }, self.last_snapshot)
                    return
                if time.monotonic() >= until:
                    raise DemoError("UIA_VALUE_MISMATCH", "Native and UIA filename values do not match the target.")
                self.deadline.pause("native Save As filename verification")
        except DemoError:
            raise
        except Exception as exc:
            raise DemoError("SAVE_FILENAME_COMMIT_FAILED", "Could not commit the native Save As filename.") from exc

    def selected_value(self, node: _Node) -> str:
        self.owned.require_pid(int(node.wrapper.element_info.process_id))
        try:
            try:
                value = node.wrapper.iface_value.CurrentValue
                if value:
                    return str(value)
            except self.runtime.missing_pattern:
                pass  # Selection is the other documented way to read a combo's value.
            items = node.wrapper.iface_selection.GetCurrentSelection()
            if items.Length != 1:
                return ""
            info = self.runtime.element(items.GetElement(0))
            self.owned.require_pid(int(info.process_id))
            return str(info.name or "")
        except DemoError:
            raise
        except self.runtime.missing_pattern as exc:
            raise DemoError("UIA_PATTERN_UNSUPPORTED", "Save As file type lacks readable Value and Selection patterns.") from exc
        except Exception as exc:
            raise DemoError("UIA_READ_FAILED", "Could not verify the Save As file type.") from exc

    def main(self, snapshot: _Snapshot, names: tuple[str, ...]) -> _Node | None:
        candidates = [
            node for node in snapshot.nodes if node.parent is None and node.pid == self.owned.pid
            and node.kind == "Window" and _title_matches(node.name, names)
        ]
        if len(candidates) > 1:
            raise DemoError("DESKTOP_WINDOW_AMBIGUOUS", "More than one owned report window matches this conversion.")
        if candidates:
            self.main_handle = candidates[0].root
            return candidates[0]
        return None

    def ready(
        self, snapshot: _Snapshot, names: tuple[str, ...], pages: tuple[str, ...],
    ) -> tuple[_Node, tuple[str, ...]] | None:
        main = self.main(snapshot, names)
        if main is None or not main.enabled or _busy(snapshot):
            return None
        file_button = self.find(snapshot, _FILE_LABELS, within=main)
        home_tab = self.find(snapshot, ("Home",), ("TabItem",), within=main)
        report_tab = self.find(snapshot, ("Report view",), ("TabItem",), within=main)
        if report_tab is not None:
            self.owned.require_pid(int(report_tab.wrapper.element_info.process_id))
            try:
                if not report_tab.wrapper.iface_selection_item.CurrentIsSelected:
                    return None
            except self.runtime.missing_pattern as exc:
                raise DemoError("UIA_PATTERN_UNSUPPORTED", "The active report view cannot be verified.") from exc
        observed = _observed_pages(snapshot, main, pages)
        if file_button and home_tab and observed == pages:
            return main, observed
        return None

    def wait_ready(
        self, names: tuple[str, ...], pages: tuple[str, ...], phase: str, *,
        allow_unprocessed_visuals: bool = False,
    ) -> tuple[_Node, tuple[str, ...]]:
        stable: float | None = None
        unsupported_since: float | None = None
        while True:
            snapshot = self.snapshot(phase)
            _check_problems(snapshot, allow_unprocessed_visuals=allow_unprocessed_visuals)
            ready = self.ready(snapshot, names, pages)
            if ready:
                if stable is None:
                    stable = time.monotonic()
                if time.monotonic() - stable >= 1.0:
                    return ready
            else:
                stable = None
                main = self.main(snapshot, names)
                if main and main.enabled and not _busy(snapshot):
                    if unsupported_since is None:
                        unsupported_since = time.monotonic()
                    if time.monotonic() - unsupported_since >= 25.0:
                        raise DemoError(
                            "DESKTOP_UI_UNSUPPORTED",
                            "The loaded report lacks supported ribbon/page labels; inspect the owned UI evidence.",
                        )
                else:
                    unsupported_since = None
            self.deadline.pause(phase)

    def wait_control(
        self, labels: tuple[str, ...], phase: str, *, names: tuple[str, ...] | None = None,
        kinds: tuple[str, ...] = _ACTION_TYPES, allow_save: bool = False,
        allow_unprocessed_visuals: bool = False,
    ) -> _Node:
        until = min(self.deadline.end, time.monotonic() + 20.0)
        while True:
            snapshot = self.snapshot(phase)
            _check_problems(
                snapshot, allow_save=allow_save, allow_unprocessed_visuals=allow_unprocessed_visuals,
            )
            main = self.main(snapshot, names) if names else None
            node = self.find(snapshot, labels, kinds, within=main) if not names or main else None
            if node:
                return node
            if time.monotonic() >= until:
                self.deadline.check(phase)
                raise DemoError("DESKTOP_UI_UNSUPPORTED", f"A supported control was not found during {phase}.")
            self.deadline.pause(phase)

    def card_observation(
        self, snapshot: _Snapshot, main: _Node, measure: str = "Total Amount",
    ) -> _CardObservation | None:
        named = _named_card_observation(snapshot, main, measure)
        if named is not None:
            return named
        observations: list[_CardObservation] = []
        for node in _card_containers(snapshot, main, measure):
            self.deadline.check("card value observation")
            self.owned.require_pid(int(node.wrapper.element_info.process_id))
            try:
                text = str(node.wrapper.iface_text.DocumentRange.GetText(2048) or "")
            except self.runtime.missing_pattern:
                continue
            except Exception as exc:
                if _stale_element(exc):
                    return None
                raise DemoError("UIA_READ_FAILED", "Could not read the owned card's accessible text.") from exc
            if len(text) >= 2048:
                continue
            names = [child.name for child in snapshot.within(node)]
            value = _metric_value([*names, text, *text.splitlines()], measure)
            if value is not None:
                observations.append(_CardObservation(value, "uia_text_pattern", text))
        if observations and len({item.value for item in observations}) == 1:
            return observations[0]
        return None

    def observe_model(
        self, names: tuple[str, ...], project: ProjectInfo, phase: str, *, require_card: bool = False,
        definitions_only: bool = False,
    ) -> dict[str, Any]:
        until = min(self.deadline.end, time.monotonic() + 30.0)
        expanded_table = False
        expanded_pane = False
        model_seen = False
        last_card: _CardObservation | None = None
        stable_card_since: float | None = None
        while True:
            snapshot = self.snapshot(phase)
            _check_problems(snapshot, allow_unprocessed_visuals=definitions_only)
            main = self.main(snapshot, names)
            if main and not _busy(snapshot):
                fields = _field_nodes(snapshot, main)
                if fields and not project.synthetic_fixture:
                    return {
                        "evidence": "visible UIA Data/Fields tree items",
                        "visible_field_items": len(fields),
                        "exact_model_metadata_verified": False,
                        "data_values_verified": False,
                    }
                if project.synthetic_fixture:
                    tables = [node for node in fields if _entity_label(node.name, "Sales", "table")]
                    measures = [node for node in fields if _entity_label(node.name, "Total Amount", "measure")]
                    if tables and measures:
                        model_seen = True
                        card = self.card_observation(snapshot, main)
                        last_card = card or last_card
                        if require_card:
                            if card is None or card.value != Decimal(60):
                                stable_card_since = None
                                if time.monotonic() < until:
                                    self.deadline.pause(phase)
                                    continue
                            else:
                                if stable_card_since is None:
                                    stable_card_since = time.monotonic()
                                if time.monotonic() - stable_card_since < 1.0:
                                    self.deadline.pause(phase)
                                    continue
                        if require_card and (card is None or card.value != Decimal(60)):
                            break
                        if card is not None:
                            self.evidence.record("card-value-observed", {
                                "observation_phase": phase, "value": card.scalar,
                                "source": card.source, "private_card_text": card.text,
                            }, snapshot)
                        return {
                            "evidence": "visible UIA Data/Fields tree items",
                            "visible_field_items": len(fields),
                            "table_name_observed": "Sales",
                            "measure_name_observed": "Total Amount",
                            "measure_type_label_observed": any("measure" in _normal(node.name) for node in measures),
                            "measure_under_table_observed": any(
                                snapshot.under(measure, table) for measure in measures for table in tables
                            ),
                            "card_value_60_observed": card is not None and card.value == Decimal(60),
                            "card_value_observed": card.scalar if card is not None else None,
                            "card_value_source": card.source if card is not None else None,
                            "measure_expression_verified": False,
                            "category_rows_verified": False,
                            "exact_model_metadata_verified": False,
                        }
                    if len(tables) == 1 and not measures and not expanded_table:
                        self.expand(tables[0])
                        expanded_table = True
                        self.deadline.pause(phase)
                        continue
                if not fields and not expanded_pane:
                    expand = self.find(snapshot, ("Expand Data pane", "Expand Fields pane"), within=main)
                    if expand:
                        self.activate(expand)
                        expanded_pane = True
                        self.deadline.pause(phase)
                        continue
            if time.monotonic() >= until:
                self.deadline.check(phase)
                if require_card and model_seen:
                    break
                raise DemoError(
                    "DESKTOP_MODEL_UNVERIFIED",
                    "The reopened model/field names could not be observed in the supported Data or Fields pane.",
                )
            self.deadline.pause(phase)
        self.deadline.check(phase)
        self.evidence.record("synthetic-card-unverified", {
            "value": last_card.scalar if last_card else None,
            "source": last_card.source if last_card else None,
            "private_card_text": last_card.text if last_card else None,
        }, self.last_snapshot)
        if last_card is not None and last_card.value != Decimal(60):
            raise DemoError("SYNTHETIC_CARD_MISMATCH", "The freshly reopened Total Amount card did not show the expected value 60.")
        raise DemoError("SYNTHETIC_CARD_UNVERIFIED", "The freshly reopened Total Amount card value 60 could not be observed.")

    def refresh_fixture(self, names: tuple[str, ...], project: ProjectInfo) -> dict[str, Any]:
        if not project.synthetic_fixture:
            raise DemoError("REFRESH_NOT_ALLOWED", "Only the trusted synthetic fixture may be automatically refreshed.")
        home = self.wait_control(
            ("Home",), "synthetic refresh Home tab", names=names,
            kinds=("TabItem",), allow_unprocessed_visuals=True,
        )
        self.activate(home)
        refresh = self.wait_control(
            ("Refresh",), "synthetic Refresh control", names=names, allow_unprocessed_visuals=True,
        )
        self.evidence.record("synthetic-refresh-requested", {"automatic_refresh": True}, self.last_snapshot)
        self.activate(refresh)
        until = min(self.deadline.end, time.monotonic() + 120.0)
        busy_observed = False
        stable: float | None = None
        completion_closed = False
        while True:
            snapshot = self.snapshot("synthetic refresh completion")
            # A no-cache fixture can show an inline visual error until processing finishes.
            # Modal errors/security prompts are never deferred, and completion is checked strictly.
            _check_problems(snapshot, allow_unprocessed_visuals=True)
            main = self.main(snapshot, names)
            busy_observed |= _busy(snapshot) or (main is not None and not main.enabled)
            for dialog in snapshot.nodes:
                if not (dialog.modal or dialog.class_name == "#32770") or not (
                    _normal(dialog.name) == "refresh" or dialog.auto_id == "KoLoadToReportDialog"
                ):
                    continue
                completed = any(
                    _normal(child.name) in {"refresh completed", "refresh completed successfully", "load completed"}
                    for child in snapshot.within(dialog)
                )
                if completed and not completion_closed:
                    close = self.find(snapshot, ("Close",), ("Button",), within=dialog)
                    if close:
                        self.activate(close)
                        completion_closed = True
            ready = self.ready(snapshot, names, project.pages)
            observation = self.card_observation(snapshot, main) if main else None
            card = observation is not None and observation.value == Decimal(60)
            if ready and (busy_observed or card):
                if stable is None:
                    stable = time.monotonic()
                if time.monotonic() - stable >= 1.0:
                    _check_problems(snapshot)
                    details = {
                        "requested": True,
                        "busy_transition_observed": busy_observed,
                        "card_value_60_observed_after_refresh": card,
                    }
                    self.evidence.record("synthetic-refresh-completed", details, snapshot)
                    return details
            else:
                stable = None
            if time.monotonic() >= until:
                self.deadline.check("synthetic refresh completion")
                raise DemoError(
                    "REFRESH_UNVERIFIED",
                    "Refresh completion was not observed; a busy transition or an accessible fixture card value is required.",
                )
            self.deadline.pause("synthetic refresh completion")

    def review_pause(self, seconds: int, page: str) -> None:
        if seconds == 0:
            return
        self.evidence.record("operator-review-ready", {"page": page, "seconds": seconds}, self.last_snapshot)
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            state = interactive_readiness()
            if not state["ready"]:
                raise DemoError(state["code"], state["message"])
            self.deadline.pause("bounded operator review")

    def observe_additional_fixture(
        self, names: tuple[str, ...], project: ProjectInfo, review_seconds: int,
    ) -> list[dict[str, Any]]:
        from .fixture_variants import RICH_POINTER, RICH_PAGES, WEIGHTED_MEASURE, expectations

        if not project.synthetic_fixture or Counter(project.pages) != Counter(RICH_PAGES):
            self.review_pause(review_seconds, project.pages[0])
            return []
        expected = expectations()["expected_weighted"]
        observations = []
        for page in RICH_PAGES:
            snapshot = self.snapshot("trusted fixture page selection")
            _check_problems(snapshot)
            main = self.main(snapshot, names)
            tab = self.find(snapshot, (page,), ("TabItem",), within=main) if main else None
            if tab is None:
                raise DemoError("REOPEN_PAGES_CHANGED", "The trusted fixture page tab was not available.")
            self.activate(tab)
            until = min(self.deadline.end, time.monotonic() + 30.0)
            while True:
                snapshot = self.snapshot("trusted fixture weighted measure")
                _check_problems(snapshot)
                main = self.main(snapshot, names)
                card = self.card_observation(snapshot, main, WEIGHTED_MEASURE) if main else None
                tab = self.find(snapshot, (page,), ("TabItem",), within=main) if main else None
                selected = False
                if tab:
                    self._guard(tab)
                    selected = bool(tab.wrapper.iface_selection_item.CurrentIsSelected)
                if selected and not _busy(snapshot) and card and card.value == Decimal(expected):
                    record = {
                        "page": page, "measure": WEIGHTED_MEASURE, "expected": expected,
                        "observed": card.scalar, "source": card.source,
                        "expectation_method": "Independent sum of input amount times dimension weight.",
                    }
                    self.evidence.record("additional-fixture-measure-observed", record, snapshot)
                    observations.append(record)
                    self.review_pause(review_seconds, page)
                    break
                if time.monotonic() >= until:
                    raise DemoError(
                        "SYNTHETIC_CARD_MISMATCH",
                        "A freshly reopened trusted fixture page did not show its independently expected weighted total.",
                    )
                self.deadline.pause("trusted fixture weighted measure")
        return observations

    @staticmethod
    def save_dialog(snapshot: _Snapshot) -> _Node | None:
        dialogs = [
            node for node in snapshot.nodes
            if node.kind == "Window" and _normal(node.name) == "save as"
            and node.class_name == "#32770"
        ]
        if len(dialogs) > 1:
            raise DemoError("DESKTOP_WINDOW_AMBIGUOUS", "More than one owned Windows Save As dialog is open.")
        return dialogs[0] if dialogs else None

    def _format_combo(self, snapshot: _Snapshot, dialog: _Node) -> _Node:
        combo = self.find(
            snapshot, ("Save as type:", "Save as type", "File type:", "File type"),
            ("ComboBox",), ids=("FileTypeControlHost", "1136", "FileTypeCombo"), within=dialog,
        )
        if not combo:
            raise DemoError("SAVE_FORMAT_UNSUPPORTED", "Windows Save As has no supported file-type control.")
        return combo

    def _choose_pbix(self) -> None:
        self._choose_format(_PBIX_TYPES)

    def _choose_format(self, types: tuple[str, ...]) -> None:
        snapshot = self.snapshot("PBIX format selection")
        _check_problems(snapshot, allow_save=True)
        dialog = self.save_dialog(snapshot)
        if dialog is None:
            raise DemoError("SAVE_DIALOG_LOST", "The owned Windows Save As dialog disappeared.")
        combo = self._format_combo(snapshot, dialog)
        valid = {_normal(label) for label in types}
        if _normal(self.selected_value(combo)) in valid:
            return
        self.expand(combo)
        option = self.wait_control(
            types, "Desktop format option", kinds=("ListItem", "MenuItem"), allow_save=True,
        )
        self.activate(option, selection_only=True)
        until = min(self.deadline.end, time.monotonic() + 10.0)
        while True:
            snapshot = self.snapshot("PBIX format verification")
            _check_problems(snapshot, allow_save=True)
            dialog = self.save_dialog(snapshot)
            if dialog:
                combo = self._format_combo(snapshot, dialog)
                if _normal(self.selected_value(combo)) in valid:
                    self.expand(combo, expanded=False)
                    if combo.wrapper.iface_expand_collapse.CurrentExpandCollapseState == 0:
                        return
            if time.monotonic() >= until:
                self.deadline.check("PBIX format verification")
                raise DemoError("SAVE_FORMAT_UNVERIFIED", "Windows Save As did not select the PBIX file type.")
            self.deadline.pause("PBIX format verification")

    def save_as(
        self, names: tuple[str, ...], output: Path, project: ProjectInfo,
    ) -> None:
        _require_new_output(output)
        file_button = self.wait_control(_FILE_LABELS, "File menu", names=names)
        self.evidence.record("file-menu-requested", {}, self.last_snapshot)
        self.activate(file_button)
        save_as = self.wait_control(("Save As", "Save as"), "Save As command")
        self.activate(save_as)
        until = min(self.deadline.end, time.monotonic() + 25.0)
        used: set[str] = set()
        while True:
            snapshot = self.snapshot("Windows Save As dialog")
            _check_problems(snapshot, allow_save=True)
            if self.save_dialog(snapshot):
                self.evidence.record("windows-save-dialog-opened", {}, snapshot)
                break
            browse = self.find(snapshot, ("Browse", "Browse this device"))
            this_pc = self.find(snapshot, ("This PC", "This device")) if not browse else None
            action = browse or this_pc
            if action and _normal(action.name) not in used:
                used.add(_normal(action.name))
                self.activate(action)
            if time.monotonic() >= until:
                self.deadline.check("Windows Save As dialog")
                raise DemoError("SAVE_DIALOG_UNSUPPORTED", "File > Save As did not expose a supported owned Windows dialog.")
            self.deadline.pause("Windows Save As dialog")
        types = _PBIP_TYPES if output.suffix.lower() == ".pbip" else _PBIX_TYPES
        if types == _PBIX_TYPES:
            self._choose_pbix()
        else:
            self._choose_format(types)
        self.evidence.record("file-type-verified", {"extension": output.suffix}, self.last_snapshot)
        snapshot = self.snapshot("Save As filename")
        _check_problems(snapshot, allow_save=True)
        dialog = self.save_dialog(snapshot)
        if dialog is None:
            raise DemoError("SAVE_DIALOG_LOST", "The owned Windows Save As dialog disappeared.")
        filename = self.find(
            snapshot, ("File name:", "File name", "Filename:"), ("Edit",),
            ids=("1001", "1152", "FileNameControlHost"), within=dialog,
        )
        if filename is None:
            raise DemoError("SAVE_FILENAME_UNSUPPORTED", "Windows Save As has no supported filename Value control.")
        self.focus(filename)
        self.set_native_filename(filename, str(output))
        save = self.find(snapshot, ("Save",), ("Button",), ids=("1",), within=dialog)
        if save is None:
            raise DemoError("SAVE_CONTROL_UNSUPPORTED", "Windows Save As has no supported Save button.")
        # The shell dialog commits its edited filename when the focused edit loses focus.
        self.focus(save)
        snapshot = self.snapshot("Save As confirmation")
        _check_problems(snapshot, allow_save=True)
        dialog = self.save_dialog(snapshot)
        if dialog is None:
            raise DemoError("SAVE_DIALOG_LOST", "The owned Windows Save As dialog disappeared.")
        combo = self._format_combo(snapshot, dialog)
        if _normal(self.selected_value(combo)) not in {_normal(label) for label in types}:
            raise DemoError("SAVE_FORMAT_UNVERIFIED", "The Save As file type changed before saving.")
        filename = self.find(
            snapshot, ("File name:", "File name", "Filename:"), ("Edit",),
            ids=("1001", "1152", "FileNameControlHost"), within=dialog,
        )
        if filename is None:
            raise DemoError("SAVE_FILENAME_UNSUPPORTED", "The Save As filename control disappeared before saving.")
        try:
            retained = str(filename.wrapper.iface_value.CurrentValue)
        except self.runtime.missing_pattern as exc:
            raise DemoError("UIA_PATTERN_UNSUPPORTED", "The Save As filename no longer exposes a Value pattern.") from exc
        if ntpath.normcase(ntpath.normpath(retained)) != ntpath.normcase(ntpath.normpath(str(output))):
            raise DemoError("UIA_VALUE_MISMATCH", "The requested Save As filename changed before saving.")
        filename_host = self.find(
            snapshot, ("File name:", "File name", "Filename:"), ("ComboBox",),
            ids=("FileNameControlHost",), within=dialog,
        )
        if filename_host is not None:
            self._guard(filename_host)
            try:
                committed = str(filename_host.wrapper.iface_value.CurrentValue)
            except self.runtime.missing_pattern as exc:
                raise DemoError("UIA_PATTERN_UNSUPPORTED", "The filename host lacks a readable Value pattern.") from exc
            if ntpath.normcase(ntpath.normpath(committed)) != ntpath.normcase(ntpath.normpath(str(output))):
                self.evidence.record("filename-commit-mismatch", {
                    "expected": str(output), "edit_value": retained, "host_value": committed,
                }, snapshot)
                raise DemoError("UIA_VALUE_MISMATCH", "The Save As filename host did not commit the requested path.")
        save = self.find(snapshot, ("Save",), ("Button",), ids=("1",), within=dialog)
        if save is None:
            raise DemoError("SAVE_CONTROL_UNSUPPORTED", "Windows Save As has no supported Save button.")
        _require_new_output(output)
        self.evidence.record("save-pbix-requested", {
            "file_type": types[0], "target_file": str(output),
            "filename_edit_focused_and_committed": True,
        }, snapshot)
        self.activate(save)
        stable_since: float | None = None
        previous: tuple[int, int] | None = None
        output_names = (output.stem, output.name)
        save_deadline = min(self.deadline.end, time.monotonic() + 120.0)
        while True:
            snapshot = self.snapshot("PBIX save completion")
            _check_problems(snapshot, allow_save=True)
            _reject_wrong_extension(output)
            try:
                status = output.stat()
                current = (status.st_size, status.st_mtime_ns)
            except FileNotFoundError:
                current = None
            if current and current[0] > 0 and current == previous:
                if stable_since is None:
                    stable_since = time.monotonic()
                if (
                    time.monotonic() - stable_since >= 2.0
                    and self.save_dialog(snapshot) is None
                    and self.ready(snapshot, output_names, project.pages)
                ):
                    self.evidence.record(
                        "pbix-saved", {"bytes": current[0], "stable_seconds": 2.0}, snapshot,
                    )
                    return
            else:
                stable_since = None
                previous = current
            if time.monotonic() >= save_deadline:
                self.evidence.record("save-target-not-ready", {
                    "target_file": str(output), "target_exists": current is not None,
                    "save_dialog_open": self.save_dialog(snapshot) is not None,
                }, snapshot)
                raise DemoError("SAVE_TARGET_UNVERIFIED", "The requested PBIX did not finish saving within 120 seconds.")
            self.deadline.pause("PBIX save completion")

    def close_report(self, *, allow_discard: bool = False, definitions_only: bool = False) -> None:
        if self.main_handle is None:
            raise DemoError("DESKTOP_CLOSE_FAILED", "No owned report window is available for graceful close.")
        self.evidence.record("owned-close-requested", {"pid": self.owned.pid}, self.last_snapshot)
        self.owned.require_live()
        self.owned.api.post_close(self.owned.job, self.main_handle)
        until = min(self.deadline.end, time.monotonic() + 25.0)
        discarded_roots: set[int] = set()
        discard_limit = 2 if definitions_only else 1
        while not self.owned.api.exited(self.owned.process):
            snapshot = self.snapshot("owned Desktop close", require_live=False)
            prompts = _check_problems(snapshot, allow_discard=allow_discard, allow_unprocessed_visuals=definitions_only)
            if prompts:
                if len(prompts) != 1:
                    raise DemoError("DESKTOP_UNEXPECTED_SAVE_PROMPT", "Desktop repeated a save decision while closing.")
                prompt = prompts[0]
                self.owned.require_pid(prompt.pid)
                discard = self.find(
                    snapshot, ("Don't save", "Do not save", "Discard changes"),
                    ("Button",), within=prompt,
                )
                if discard is None:
                    raise DemoError("DESKTOP_CLOSE_FAILED", "The owned save-changes dialog lacks a supported discard control.")
                if prompt.root not in discarded_roots:
                    if len(discarded_roots) >= discard_limit:
                        raise DemoError("DESKTOP_UNEXPECTED_SAVE_PROMPT", "Desktop repeated a save decision while closing.")
                    self.activate(discard)
                    discarded_roots.add(prompt.root)
                    self.evidence.record("owned-close-discard", {
                        "pid": self.owned.pid, "discard_count": len(discarded_roots),
                        "discard_limit": discard_limit,
                    })
            if time.monotonic() >= until:
                self.deadline.check("owned Desktop close")
                raise DemoError("DESKTOP_CLOSE_FAILED", "Owned Desktop did not close gracefully within the deadline.")
            self.deadline.pause("owned Desktop close")
        self.evidence.record("owned-close-completed", {
            "pid": self.owned.pid, "discard_count": len(discarded_roots),
        })


def _require_new_output(path: Path) -> None:
    if path.suffix.casefold() not in (".pbix", ".pbip"):
        raise DemoError("OUTPUT_TYPE_INVALID", "The conversion output must have a .pbix or .pbip extension.")
    if path.exists() or path.is_symlink():
        raise DemoError("OUTPUT_EXISTS", "Conversion never overwrites an existing output.")
    if not path.parent.is_dir():
        raise DemoError("OUTPUT_DIRECTORY_MISSING", "The worker output directory does not exist.")


def _reject_wrong_extension(output: Path) -> None:
    alternatives = (
        Path(str(output) + ".pbip"), Path(str(output) + ".pbit"), Path(str(output) + ".pbix"),
        output.with_suffix(".pbip"), output.with_suffix(".pbit"), output.with_suffix(".pbix"),
    )
    if any((path.exists() or path.is_symlink()) and path != output for path in alternatives):
        raise DemoError("SAVE_WRONG_FORMAT", "Desktop created a project, template, or double-extension file instead of the requested PBIX.")


def _validate_request(request: ConversionRequest) -> None:
    if type(request.review_seconds) is not int or not 0 <= request.review_seconds <= 60:
        raise DemoError("INVALID_TIMEOUT", "Operator review must be a bounded 0..60 seconds per fixture page.")
    paths = (request.project_file, request.output_file, request.evidence_dir, request.desktop_exe)
    if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
        raise DemoError("WORKER_PATH_INVALID", "The worker requires resolved absolute input and output paths.")
    reverse = request.direction == "pbix_to_pbip"
    from .inputs import validate_direction
    validate_direction(request.direction, request.export_mode)
    if request.output_file.suffix.casefold() != (".pbip" if reverse else ".pbix"):
        raise DemoError("OUTPUT_TYPE_INVALID", "Output type does not match the conversion direction.")
    if request.project_file.suffix.casefold() != (".pbix" if reverse else ".pbip") or not request.project_file.is_file():
        raise DemoError("PROJECT_FILE_MISSING", "The extracted project pointer is not a readable PBIP file.")
    if not request.project.pages or any(not isinstance(page, str) or not page for page in request.project.pages):
        raise DemoError("PROJECT_PAGES_INVALID", "The submitted project must contain named report pages.")
    if request.project.model_format not in (("binary",) if reverse else ("tmdl", "tmsl")):
        raise DemoError("MODEL_FORMAT_UNSUPPORTED", "Only TMDL and TMSL project models are supported.")
    _require_new_output(request.output_file)
    _reject_wrong_extension(request.output_file)
    if sys.maxsize <= 2**32:
        raise DemoError("WORKER_ARCHITECTURE_UNSUPPORTED", "Run the worker with 64-bit Python.")
    if request.desktop_exe.name.casefold() != "pbidesktop.exe" or not request.desktop_exe.is_file():
        raise DemoError("DESKTOP_EXECUTABLE_INVALID", "Configure the standard x64 PBIDesktop.exe executable.")
    with request.desktop_exe.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise DemoError("DESKTOP_EXECUTABLE_INVALID", "The configured Desktop executable is not a Windows PE image.")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= 1024 * 1024:
            raise DemoError("DESKTOP_EXECUTABLE_INVALID", "The configured Desktop executable has an invalid PE header.")
        stream.seek(offset)
        signature = stream.read(6)
        if len(signature) != 6 or signature[:4] != b"PE\0\0" or struct.unpack("<H", signature[4:])[0] != 0x8664:
            raise DemoError("DESKTOP_ARCHITECTURE_UNSUPPORTED", "The configured Desktop executable must be standard AMD64/x64.")


def _wait_released(api: _Native, output: Path, deadline: _Deadline) -> None:
    while True:
        deadline.check("PBIX file release")
        if api.file_released(output):
            return
        deadline.pause("PBIX file release")


def _fingerprint(path: Path, deadline: _Deadline) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            deadline.check("PBIX integrity verification")
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _verification_summary(
    desktop_version: str, project: ProjectInfo, reopened_model: dict[str, Any],
) -> dict[str, Any]:
    common = "Desktop Save As; binary DataModel/report inspection; fresh-process UIA report-page and field observations."
    if not project.synthetic_fixture:
        return {
            "desktop_version": desktop_version,
            "synthetic_total_amount": None,
            "model_tables": [],
            "model_measures": [],
            "verification_method": (
                common + " Named tables/measures, DAX expressions, and data values were not individually verified."
            ),
        }
    value = reopened_model.get("card_value_observed")
    source = reopened_model.get("card_value_source")
    if (
        reopened_model.get("card_value_60_observed") is not True
        or type(value) is not int or value != 60
        or source not in ("uia_accessible_name", "uia_text_pattern")
    ):
        raise DemoError(
            "SYNTHETIC_CARD_UNVERIFIED",
            "Freshly reopened synthetic card evidence is missing or does not verify Total Amount = 60.",
        )
    if (
        reopened_model.get("table_name_observed") != "Sales"
        or reopened_model.get("measure_name_observed") != "Total Amount"
    ):
        raise DemoError("DESKTOP_MODEL_UNVERIFIED", "Freshly reopened Sales and Total Amount field names were not verified.")
    return {
        "desktop_version": desktop_version,
        "synthetic_total_amount": value,
        "model_tables": [reopened_model["table_name_observed"]],
        "model_measures": [reopened_model["measure_name_observed"]],
        "verification_method": (
            common + f" Sales/Total Amount field names and card Total Amount={value} observed via {source}."
            " DAX expression and individual rows were not queried."
        ),
    }


class WindowsDesktopConverter:
    def convert(self, request: ConversionRequest) -> ConversionResult:
        """Return only observed UI facts in source/reopened_model_observation.

        Synthetic name observations are separate from measure-type/parentage
        observations. card_value_60_observed never implies verified DAX or rows.
        """
        deadline = _Deadline(request.timeout_seconds)
        readiness = interactive_readiness()
        if not readiness["ready"]:
            raise DemoError(readiness["code"], readiness["message"])
        try:
            _validate_request(request)
            deadline.check("session mutex acquisition")
            with desktop_session_mutex(deadline.remaining):
                deadline.check("session mutex acquisition")
                readiness = interactive_readiness()
                if not readiness["ready"]:
                    raise DemoError(readiness["code"], readiness["message"])
                _require_new_output(request.output_file)
                if request.direction == "pbix_to_pbip":
                    return self._reverse_owned(request, deadline, readiness)
                return self._convert_owned(request, deadline, readiness)
        except DemoError:
            raise
        except OSError as exc:
            raise DemoError("WORKER_IO_FAILED", "The worker could not read its inputs or acquire session ownership.") from exc
        except Exception as exc:
            raise DemoError("DESKTOP_INITIALIZATION_FAILED", "The Windows Desktop conversion backend could not initialize.") from exc

    def _convert_owned(
        self, request: ConversionRequest, deadline: _Deadline, readiness: dict[str, Any],
    ) -> ConversionResult:
        try:
            runtime = _load_uia()
            api = _Native()
            with _Evidence(request.evidence_dir) as evidence:
                active: _Automation | None = None
                try:
                    evidence.record("preflight", readiness)
                    evidence.record("session-mutex-acquired", {"scope": "Local", "session_id": readiness["session_id"]})
                    desktop_version = api.file_version(request.desktop_exe)
                    evidence.record("desktop-version", {"desktop_version": desktop_version})
                    report_stem = ntpath.basename(request.project.report).removesuffix(".Report")
                    source_names = tuple(dict.fromkeys((
                        request.project_file.stem, request.project_file.name, report_stem,
                    )))
                    refresh: dict[str, Any] = {"requested": False}
                    with _OwnedDesktop(api, request.desktop_exe, request.project_file, deadline) as owned:
                        initial_pid = owned.pid
                        evidence.record("project-launched", {"pid": initial_pid})
                        active = _Automation(owned, deadline, runtime, evidence)
                        active.wait_ready(
                            source_names, request.project.pages, "project report readiness",
                            allow_unprocessed_visuals=request.project.synthetic_fixture,
                        )
                        evidence.record("project-report-ready", {"pages": list(request.project.pages)}, active.last_snapshot)
                        model_dir = request.project_file.parent / ntpath.relpath(
                            request.project.model, ntpath.dirname(request.project.pointer) or ".",
                        )
                        if request.project.synthetic_fixture and not (model_dir / ".pbi" / "cache.abf").is_file():
                            refresh = active.refresh_fixture(source_names, request.project)
                        source_model = active.observe_model(source_names, request.project, "project model verification")
                        evidence.record("project-model-observed", source_model, active.last_snapshot)
                        active.save_as(source_names, request.output_file, request.project)
                        active.close_report(allow_discard=True)
                    evidence.record("project-process-tree-closed", {"pid": initial_pid})
                    _wait_released(api, request.output_file, deadline)
                    _reject_wrong_extension(request.output_file)
                    deadline.check("PBIX structural validation")
                    structure = inspect_pbix(request.output_file, request.project, Limits(**request.limits))
                    before = _fingerprint(request.output_file, deadline)
                    evidence.record("pbix-container-validated", {**structure, "sha256": before})
                    readiness = interactive_readiness()
                    if not readiness["ready"]:
                        raise DemoError(readiness["code"], readiness["message"])
                    if api.file_version(request.desktop_exe) != desktop_version:
                        raise DemoError("DESKTOP_VERSION_CHANGED", "The configured Desktop executable changed during conversion.")
                    with _OwnedDesktop(api, request.desktop_exe, request.output_file, deadline) as reopened:
                        reopened_pid = reopened.pid
                        if reopened_pid == initial_pid:
                            raise DemoError("DESKTOP_REOPEN_NOT_FRESH", "Fresh reopening did not obtain a different owned process ID.")
                        evidence.record("pbix-fresh-process-launched", {"pid": reopened_pid})
                        active = _Automation(reopened, deadline, runtime, evidence)
                        output_names = (request.output_file.stem, request.output_file.name)
                        _, pages = active.wait_ready(output_names, request.project.pages, "fresh PBIX report readiness")
                        reopened_model = active.observe_model(
                            output_names, request.project, "fresh PBIX model verification",
                            require_card=request.project.synthetic_fixture,
                        )
                        summary = _verification_summary(desktop_version, request.project, reopened_model)
                        evidence.record(
                            "pbix-fresh-open-verified", {"pages": list(pages), "model": reopened_model, "summary": summary},
                            active.last_snapshot,
                        )
                        additional = active.observe_additional_fixture(
                            output_names, request.project, request.review_seconds,
                        )
                        active.close_report(allow_discard=True)
                    _wait_released(api, request.output_file, deadline)
                    after = _fingerprint(request.output_file, deadline)
                    if before != after:
                        raise DemoError("PBIX_CHANGED_ON_REOPEN", "The PBIX changed during fresh-open verification.")
                    details = {
                        **summary,
                        "workflow": "Desktop File > Save As PBIX; close; fresh owned-process reopen",
                        "refresh": refresh,
                        "container": structure,
                        "source_model_observation": source_model,
                        "reopened_model_observation": reopened_model,
                        "additional_fixture_observations": additional,
                        "session_mutex_acquired": True,
                        "session_mutex_scope": "Local",
                        "fresh_process_verified": True,
                        "file_released": True,
                        "unchanged_after_reopen": True,
                        "sha256": after,
                        "verification_limit": (
                            "Binary DataModel, report metadata, ribbon/pages, and visible model fields were checked. "
                            "No row-by-row model comparison or DAX-expression verification was performed. "
                            "A synthetic card value is verified only when explicitly marked observed."
                        ),
                    }
                    deadline.check("completion evidence")
                    evidence.record("complete", {"initial_pid": initial_pid, "reopened_pid": reopened_pid, **details})
                    deadline.check("completion evidence")
                    return ConversionResult(initial_pid, reopened_pid, pages, True, details)
                except Exception as exc:
                    evidence.record(
                        "failed",
                        {
                            "code": exc.code if isinstance(exc, DemoError) else "DESKTOP_CONVERSION_FAILED",
                            "private_diagnostic": repr(exc)[:2048],
                            "private_cause": repr(exc.__cause__)[:2048] if exc.__cause__ else None,
                        },
                        active.last_snapshot if active else None,
                    )
                    if isinstance(exc, DemoError):
                        raise
                    raise DemoError(
                        "DESKTOP_CONVERSION_FAILED", "Desktop conversion failed; inspect the worker's private phase evidence.",
                    ) from exc
        except DemoError:
            raise
        except OSError as exc:
            raise DemoError("WORKER_IO_FAILED", "The worker could not read its inputs or persist conversion evidence.") from exc
        except Exception as exc:
            raise DemoError("DESKTOP_INITIALIZATION_FAILED", "The Windows Desktop conversion backend could not initialize.") from exc

    def _reverse_owned(self, request: ConversionRequest, deadline: _Deadline, readiness: dict[str, Any]) -> ConversionResult:
        from .project_export import package_project

        runtime, api = _load_uia(), _Native()
        limits = Limits(**request.limits)
        before = _fingerprint(request.project_file, deadline)
        with _Evidence(request.evidence_dir) as evidence:
            active = None
            try:
                version = api.file_version(request.desktop_exe)
                evidence.record("pbix-source-preflight", {"sha256": before, "session_id": readiness["session_id"]})
                with _OwnedDesktop(api, request.desktop_exe, request.project_file, deadline) as owned:
                    first = owned.pid
                    active = _Automation(owned, deadline, runtime, evidence)
                    names = (request.project_file.name, request.project_file.stem)
                    active.wait_ready(names, request.project.pages, "source PBIX readiness")
                    source_model = active.observe_model(names, request.project, "source PBIX fields",
                                                        require_card=request.project.synthetic_fixture)
                    active.save_as(names, request.output_file, request.project)
                    active.close_report(allow_discard=True)
                _wait_released(api, request.output_file, deadline)
                exported = package_project(request.output_file.parent, request.export_mode or "definitions", limits)
                project = replace(exported.project, synthetic_fixture=request.project.synthetic_fixture)
                if Counter(project.pages) != Counter(request.project.pages) or project.visual_count != request.project.visual_count:
                    raise DemoError("PBIP_REPORT_CHANGED", "Desktop exported different report pages or visual counts.")
                reopen_dir = request.output_file.parent.parent / "reopen"
                exported.extract(reopen_dir)
                pointer = reopen_dir.joinpath(*project.pointer.split("/"))
                definitions = request.export_mode != "portable"
                with _OwnedDesktop(api, request.desktop_exe, pointer, deadline) as reopened:
                    second = reopened.pid
                    if first == second:
                        raise DemoError("DESKTOP_REOPEN_NOT_FRESH", "PBIP verification requires a new owned Desktop process.")
                    active = _Automation(reopened, deadline, runtime, evidence)
                    names = (pointer.name, pointer.stem, ntpath.basename(project.report).removesuffix(".Report"))
                    _, pages = active.wait_ready(names, project.pages, "fresh PBIP readiness",
                                                 allow_unprocessed_visuals=definitions)
                    observed = active.observe_model(names, project, "fresh PBIP fields",
                                                    require_card=project.synthetic_fixture and not definitions,
                                                    definitions_only=definitions)
                    additional = [] if definitions else active.observe_additional_fixture(names, project, request.review_seconds)
                    summary = ({
                        "desktop_version": version, "synthetic_total_amount": None,
                        "verification_method": "Fresh owned Desktop opens definitions without refresh; no offline data/value success claimed.",
                    } if definitions else _verification_summary(version, project, observed))
                    evidence.record("pbip-fresh-open-verified", {"pages": list(pages), "model": observed, **summary}, active.last_snapshot)
                    active.close_report(allow_discard=True, definitions_only=definitions)
                if _fingerprint(request.project_file, deadline) != before:
                    raise DemoError("SOURCE_INTEGRITY", "The source PBIX changed during conversion.")
                if api.file_version(request.desktop_exe) != version:
                    raise DemoError("DESKTOP_VERSION_CHANGED", "Desktop changed during conversion.")
                return ConversionResult(first, second, pages, True, {
                    **summary, "workflow": "Desktop Save As PBIP; allowlist; fresh process opens exported copy",
                    "contains_data": not definitions, "requires_data_reload": definitions,
                    "source_model_observation": source_model, "reopened_model_observation": observed,
                    "additional_fixture_observations": additional, "project": project.as_dict(),
                    "export_sha256": hashlib.sha256(exported.data).hexdigest(),
                    "refresh": {"requested": False},
                })
            except Exception as exc:
                evidence.record("failed", {"code": exc.code if isinstance(exc, DemoError) else "DESKTOP_CONVERSION_FAILED"},
                                active.last_snapshot if active else None)
                if isinstance(exc, DemoError):
                    raise
                raise DemoError("DESKTOP_CONVERSION_FAILED", "Desktop PBIP export failed; inspect private phase evidence.") from exc
