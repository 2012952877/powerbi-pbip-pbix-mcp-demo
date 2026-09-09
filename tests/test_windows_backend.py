"""Unit-only fakes. These tests NEVER start Desktop, inspect host UI, or prove a real conversion."""

from __future__ import annotations

import ctypes
import json
import shutil
import struct
import unittest
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pbip_mcp import windows_desktop as desktop
from pbip_mcp import windows_session as session
from pbip_mcp.contracts import ConversionRequest, ProjectInfo
from pbip_mcp.errors import DemoError
from tests.helpers import fake_pbix


READY = {"ready": True, "code": "READY", "message": "Unit fake session.", "session_id": 7}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class MissingPattern(Exception):
    pass


class FakeInfo:
    def __init__(
        self, name: str = "", kind: str = "Window", pid: int = 101,
        children: list | None = None, auto_id: str = "", class_name: str = "", handle: int = 0,
    ) -> None:
        self.name, self.control_type, self.process_id = name, kind, pid
        self.automation_id, self.class_name = auto_id, class_name
        self.handle = handle
        self.element = SimpleNamespace(CurrentAriaRole="", CurrentHasKeyboardFocus=False)
        self.element.SetFocus = lambda: setattr(self.element, "CurrentHasKeyboardFocus", True)
        self._children = children or []

    def children(self) -> list:
        return self._children


class FakeWrapper:
    def __init__(self, info: FakeInfo, **patterns) -> None:
        self.element_info = info
        self.patterns = patterns

    def __getattr__(self, name: str):
        if name.startswith("iface_"):
            if name in self.patterns:
                return self.patterns[name]
            raise MissingPattern(name)
        raise AssertionError(f"Forbidden or unimplemented unit UI operation: {name}")

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def is_keyboard_focusable(self) -> bool:
        return True


class FakeValue:
    CurrentIsReadOnly = False

    def __init__(self, value: str = "") -> None:
        self.CurrentValue = value
        self.writes: list[str] = []

    def SetValue(self, value: str) -> None:
        self.writes.append(value)
        self.CurrentValue = value


def node(
    name: str, kind: str = "Button", *, parent: int | None = 0, root: int = 1010,
    auto_id: str = "", class_name: str = "", modal: bool = False, **patterns,
) -> desktop._Node:
    info = FakeInfo(name, kind, auto_id=auto_id, class_name=class_name)
    return desktop._Node(
        FakeWrapper(info, **patterns), 101, root, parent, name, kind, auto_id,
        class_name, True, modal, 0 if parent is None else 1,
    )


def report_snapshot(name: str = "Example") -> desktop._Snapshot:
    return desktop._Snapshot([
        node(f"{name} - Power BI Desktop", "Window", parent=None),
        node("File"),
        node("Home", "TabItem"),
        node("Synthetic overview", "TabItem"),
    ])


class FakeNative:
    """Only in-memory handles/processes; there is no operating-system delegation."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.next_pid = 101
        self.jobs: dict[str, set[int]] = {}
        self.processes: dict[str, int] = {}
        self.assign_error = False
        self.forward = False

    def new_job(self):
        job = f"unit-job-{len(self.jobs) + 1}"
        self.jobs[job] = set()
        self.events.append(("new_job", job))
        return job

    def suspended_process(self, executable, document):
        pid = self.next_pid
        self.next_pid += 1
        process, thread = f"unit-process-{pid}", f"unit-thread-{pid}"
        self.processes[process] = pid
        self.events.append(("suspended", pid, document.name))
        return process, thread, pid

    def assign(self, job, process):
        self.events.append(("assign", job, process))
        if self.assign_error:
            raise OSError("Unit-only assignment failure with a private path.")
        self.jobs[job].add(self.processes[process])

    def resume(self, thread):
        self.events.append(("resume", thread))
        if self.forward:
            for members in self.jobs.values():
                members.clear()

    def close(self, handle):
        self.events.append(("close_handle", handle))

    def terminate_job(self, job):
        self.events.append(("terminate_job", job))
        self.jobs[job].clear()

    def terminate_process(self, process):
        self.events.append(("terminate_suspended", process))

    def exited(self, process):
        return not any(self.processes[process] in members for members in self.jobs.values())

    def members(self, job):
        return self.jobs[job].copy()

    def belongs(self, job, pid):
        return pid in self.jobs[job]

    def window_pid(self, handle):
        return 101

    def post_close(self, job, window):
        self.events.append(("graceful_close", job, window))
        self.jobs[job].clear()

    def file_released(self, output):
        self.events.append(("file_released", output.name))
        return True

    def file_version(self, executable):
        self.events.append(("file_version", executable.name))
        return "2.157.1354.0"


class FakeProcessNative(FakeNative):
    def __init__(self):
        super().__init__()
        self.codes: dict[int, int] = {}
        self.finish_on_wait = False
        self.spawn_options = {}

    def suspended_command(self, argv, **options):
        self.spawn_options = options
        process, thread, pid = self.suspended_process(Path(argv[0]), Path(argv[-1]))
        self.jobs[options["job"]].add(pid)
        self.events.append(("atomic_job_assignment", options["job"], pid))
        return process, thread, pid

    def exit_code(self, process):
        return self.codes.get(self.processes[process], 0) if self.exited(process) else None

    def wait_process(self, process, milliseconds):
        self.events.append(("wait_process", process, milliseconds))
        if self.finish_on_wait:
            pid = self.processes[process]
            for members in self.jobs.values():
                members.discard(pid)
            self.codes[pid] = 0
        return self.exited(process)

    def terminate_job(self, job):
        for pid in self.jobs[job]:
            self.codes[pid] = 1
        super().terminate_job(job)

    def close(self, handle):
        if handle in self.jobs:
            self.jobs[handle].clear()  # Unit model of KILL_ON_JOB_CLOSE.
        super().close(handle)


class FakeConversionAutomation:
    """A workflow unit double, not production conversion or Desktop evidence."""

    fail_reopen = False
    template_output = False
    mutate_reopen = False
    missing_card = False
    card_value = 60

    def __init__(self, owned, deadline, runtime, evidence):
        self.owned, self.deadline = owned, deadline
        self.last_snapshot = report_snapshot()

    def wait_ready(self, names, pages, phase, **_kwargs):
        self.owned.require_live()
        self.owned.api.events.append(("report_ready", self.owned.pid))
        return self.last_snapshot.nodes[0], pages

    def observe_model(self, names, project, phase, *, require_card=False):
        self.owned.api.events.append(("model_observed", self.owned.pid))
        if self.fail_reopen and self.owned.document.suffix == ".pbix":
            raise DemoError("DESKTOP_MODEL_UNVERIFIED", "Unit fresh-model failure.")
        value = self.card_value if project.synthetic_fixture and not self.missing_card else None
        return {
            "evidence": "UNIT FAKE, NOT REAL UI",
            "visible_field_items": 4,
            "exact_model_metadata_verified": False,
            "table_name_observed": "Sales" if project.synthetic_fixture else None,
            "measure_name_observed": "Total Amount" if project.synthetic_fixture else None,
            "card_value_60_observed": value == 60,
            "card_value_observed": value,
            "card_value_source": "uia_accessible_name" if value is not None else None,
        }

    def refresh_fixture(self, names, project):
        if not project.synthetic_fixture:
            raise AssertionError("Untrusted automatic refresh")
        self.owned.api.events.append(("fixture_refresh", self.owned.pid))
        return {"requested": True, "busy_transition_observed": True}

    def observe_additional_fixture(self, names, project, review_seconds):
        return []

    def save_as(self, names, output, project):
        self.owned.api.events.append(("save_as", self.owned.pid))
        output.write_bytes(fake_pbix(list(project.pages), model=not self.template_output))

    def close_report(self, *, allow_discard=False):
        if not allow_discard:
            raise AssertionError("Workflow must explicitly identify an already-verified saved artifact.")
        if self.mutate_reopen and self.owned.document.suffix == ".pbix":
            with self.owned.document.open("ab") as stream:
                stream.write(b"unit-only changed content")
        self.owned.api.post_close(self.owned.job, self.owned.pid * 10)


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        # Deliberately not tempfile: all unit artifacts remain below the project.
        self.workspace = Path.cwd() / (".windows-backend-test-" + uuid.uuid4().hex)
        self.workspace.mkdir()
        self.addCleanup(shutil.rmtree, self.workspace)
        pointer = self.workspace / "Example.pbip"
        pointer.write_text("{}", encoding="utf-8")
        executable = self.workspace / "PBIDesktop.exe"
        header = bytearray(64)
        header[:2] = b"MZ"
        struct.pack_into("<I", header, 60, 64)
        executable.write_bytes(header + b"PE\0\0\x64\x86")  # NOT an executable; never launched.
        self.request = ConversionRequest(
            pointer, self.workspace / "Converted.pbix", self.workspace / "evidence", executable,
            ProjectInfo(
                "Example.pbip", "Example.Report", "Example.SemanticModel",
                ("Synthetic overview",), 1, "tmdl",
            ),
        )


class SessionReadinessTests(unittest.TestCase):
    def test_session_owned_process_export_is_the_same_implementation(self):
        from pbip_mcp.windows_session import OwnedProcess

        self.assertIs(OwnedProcess, desktop.OwnedProcess)

    def fake_api(self, **changes):
        defaults = {
            "session_id.return_value": 7, "is_system.return_value": False,
            "connection_state.return_value": 0, "input_desktop_name.return_value": "Default",
            "worker_desktop_name.return_value": "Default", "worker_station_name.return_value": "WinSta0",
        }
        defaults.update(changes)
        return Mock(**defaults)

    def probe(self, api):
        with patch.object(session.sys, "platform", "win32"), patch.object(session, "_SessionAPI", return_value=api):
            return session.interactive_readiness()

    def test_non_windows_never_loads_win32(self):
        with patch.object(session.sys, "platform", "linux"), patch.object(session, "_SessionAPI") as api:
            result = session.interactive_readiness()
        self.assertEqual(result["code"], "WINDOWS_REQUIRED")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["session_id"])
        api.assert_not_called()

    def test_system_identity_is_rejected_before_desktop_access(self):
        api = self.fake_api(**{"is_system.return_value": True})
        result = self.probe(api)
        self.assertEqual(result["code"], "SYSTEM_ACCOUNT_UNSUPPORTED")
        api.input_desktop_name.assert_not_called()
        api.connection_state.assert_not_called()

    def test_session_zero_is_rejected(self):
        api = self.fake_api(**{"session_id.return_value": 0})
        result = self.probe(api)
        self.assertEqual(result["session_id"], 0)
        self.assertFalse(result["ready"])
        api.input_desktop_name.assert_not_called()

    def test_disconnected_session_is_rejected_before_input_desktop_probe(self):
        api = self.fake_api(**{"connection_state.return_value": 4})
        self.assertEqual(self.probe(api)["code"], "RDP_SESSION_INACTIVE")
        api.connection_state.assert_called_once_with(7)
        api.input_desktop_name.assert_not_called()

    def test_lock_desktop_and_noninteractive_station_are_rejected(self):
        self.assertEqual(
            self.probe(self.fake_api(**{"input_desktop_name.return_value": "Winlogon"}))["code"], "DESKTOP_LOCKED",
        )
        self.assertEqual(
            self.probe(self.fake_api(**{"worker_station_name.return_value": "Service-0x0-3e7$"}))["code"],
            "WORKER_DESKTOP_UNSUPPORTED",
        )

    def test_ready_has_contract_keys(self):
        result = self.probe(self.fake_api())
        self.assertTrue(result["ready"])
        self.assertEqual(set(result), {"ready", "code", "message", "session_id"})

    def test_probe_failure_does_not_leak_identity_or_native_error(self):
        result = self.probe(self.fake_api(**{
            "input_desktop_name.side_effect": OSError("private-user private-path native error"),
        }))
        self.assertEqual(result["code"], "SESSION_PROBE_FAILED")
        self.assertNotIn("private", json.dumps(result))

    def test_system_check_uses_token_user_sid_and_closes_token(self):
        api = session._SessionAPI.__new__(session._SessionAPI)
        api.current_process = Mock(return_value=90)

        def open_token(process, access, destination):
            self.assertEqual((process, access), (90, 8))
            destination._obj.value = 91
            return True

        def token_information(token, information_class, buffer, size, length):
            self.assertEqual(information_class, 1)
            length._obj.value = ctypes.sizeof(session._TokenUser)
            if buffer is None:
                return False
            ctypes.cast(buffer, ctypes.POINTER(session._TokenUser)).contents.User.Sid = 0x1234
            return True

        api.open_token, api.token_information = open_token, token_information
        api.valid_sid = Mock(return_value=True)
        api.well_known_sid = Mock(return_value=True)
        api.close_handle = Mock(return_value=True)
        with patch.object(session.ctypes, "get_last_error", return_value=122, create=True):
            self.assertTrue(api.is_system())
        api.well_known_sid.assert_called_once_with(0x1234, 22)
        self.assertEqual(api.close_handle.call_args.args[0].value, 91)


class SessionMutexTests(unittest.TestCase):
    def fake_api(self, status=0):
        return Mock(**{"create.return_value": 41, "wait.return_value": status})

    def test_mutex_name_is_session_scoped_and_independent_of_worker_roots(self):
        api = self.fake_api()
        with patch.object(session.sys, "platform", "win32"), patch.object(session, "_MutexAPI", return_value=api):
            with session.desktop_session_mutex(1.25):
                api.create.assert_called_once_with("Local\\PBIPMCP.PowerBIDesktop.Conversion")
                api.wait.assert_called_once_with(41, 1250)
                api.release.assert_not_called()
            api.release.assert_called_once_with(41)
            api.close.assert_called_once_with(41)

    def test_timeout_never_releases_a_mutex_we_do_not_own(self):
        api = self.fake_api(258)
        with (
            patch.object(session.sys, "platform", "win32"),
            patch.object(session, "_MutexAPI", return_value=api),
            self.assertRaises(DemoError) as error,
        ):
            with session.desktop_session_mutex(0):
                self.fail("A busy mutex must not enter the conversion body.")
        self.assertEqual(error.exception.code, "DESKTOP_SESSION_BUSY")
        api.wait.assert_called_once_with(41, 0)
        api.release.assert_not_called()
        api.close.assert_called_once_with(41)

    def test_abandoned_mutex_fails_closed_but_releases_granted_ownership(self):
        api = self.fake_api(128)
        with (
            patch.object(session.sys, "platform", "win32"),
            patch.object(session, "_MutexAPI", return_value=api),
            self.assertRaises(DemoError) as error,
        ):
            with session.desktop_session_mutex(1):
                self.fail("Abandoned ownership requires an operator cleanup check.")
        self.assertEqual(error.exception.code, "DESKTOP_SESSION_ABANDONED")
        api.release.assert_called_once_with(41)
        api.close.assert_called_once_with(41)

    def test_body_exception_propagates_and_releases_the_mutex(self):
        api = self.fake_api()
        original = DemoError("UNIT_BODY", "Unit conversion failure.")
        with (
            patch.object(session.sys, "platform", "win32"),
            patch.object(session, "_MutexAPI", return_value=api),
            self.assertRaises(DemoError) as error,
        ):
            with session.desktop_session_mutex(1):
                raise original
        self.assertIs(error.exception, original)
        api.release.assert_called_once_with(41)
        api.close.assert_called_once_with(41)

    def test_native_wait_failure_is_safe_and_closes_the_unowned_handle(self):
        api = self.fake_api()
        api.wait.side_effect = OSError("unit private native error")
        with (
            patch.object(session.sys, "platform", "win32"),
            patch.object(session, "_MutexAPI", return_value=api),
            self.assertRaises(DemoError) as error,
        ):
            with session.desktop_session_mutex(1):
                self.fail("Failed waiting must not enter the body.")
        self.assertEqual(error.exception.code, "DESKTOP_MUTEX_FAILED")
        self.assertNotIn("private", error.exception.message)
        api.release.assert_not_called()
        api.close.assert_called_once_with(41)

    def test_release_failure_is_not_swallowed_and_still_closes_handle(self):
        api = self.fake_api()
        api.release.side_effect = OSError("unit-only release error")
        with (
            patch.object(session.sys, "platform", "win32"),
            patch.object(session, "_MutexAPI", return_value=api),
            self.assertRaises(DemoError) as error,
        ):
            with session.desktop_session_mutex(1):
                pass
        self.assertEqual(error.exception.code, "DESKTOP_MUTEX_CLEANUP_FAILED")
        api.close.assert_called_once_with(41)

    def test_invalid_and_non_windows_mutex_calls_do_not_touch_native_api(self):
        with patch.object(session.sys, "platform", "linux"), patch.object(session, "_MutexAPI") as api:
            with self.assertRaises(DemoError) as error:
                with session.desktop_session_mutex():
                    pass
            self.assertEqual(error.exception.code, "WINDOWS_REQUIRED")
            api.assert_not_called()
        for timeout in (-1, float("inf"), float("nan"), True, "1", 10**1000):
            with (
                self.subTest(timeout=timeout),
                patch.object(session.sys, "platform", "win32"),
                patch.object(session, "_MutexAPI") as api,
                self.assertRaises(DemoError) as error,
            ):
                with session.desktop_session_mutex(timeout):
                    pass
            self.assertEqual(error.exception.code, "INVALID_TIMEOUT")
            api.assert_not_called()

    def test_large_finite_wait_never_becomes_infinite_win32_sentinel(self):
        api = self.fake_api()
        with patch.object(session.sys, "platform", "win32"), patch.object(session, "_MutexAPI", return_value=api):
            with session.desktop_session_mutex(1e308):
                pass
        self.assertLess(api.wait.call_args.args[1], 0xFFFFFFFF)

    def test_native_mutex_creation_does_not_inherit_or_assume_initial_ownership(self):
        api = session._MutexAPI.__new__(session._MutexAPI)
        api._create = Mock(return_value=42)
        self.assertEqual(api.create(session._SESSION_MUTEX_NAME), 42)
        api._create.assert_called_once_with(None, False, session._SESSION_MUTEX_NAME)


class DeadlineAndParsingTests(unittest.TestCase):
    def test_invalid_deadlines_fail_closed(self):
        for seconds in (0, -1, float("inf"), float("nan"), True, "600", 10**1000):
            with self.subTest(seconds=seconds), self.assertRaises(DemoError) as error:
                desktop._Deadline(seconds)
            self.assertEqual(error.exception.code, "INVALID_TIMEOUT")

    def test_pause_never_exceeds_remaining_budget(self):
        clock = FakeClock()
        with patch.object(desktop.time, "monotonic", clock.monotonic), patch.object(desktop.time, "sleep", clock.sleep):
            deadline = desktop._Deadline(0.2)
            with self.assertRaises(DemoError) as error:
                deadline.pause("unit phase")
        self.assertAlmostEqual(clock.now, 0.2)
        self.assertEqual(error.exception.code, "DESKTOP_TIMEOUT")
        self.assertIn("unit phase", error.exception.message)

    def test_dialog_parser_has_safe_specific_codes(self):
        cases = (
            ("Confirm Save As: this already exists; replace it?", "OUTPUT_EXISTS"),
            ("Native database query: run native query?", "DESKTOP_SECURITY_PROMPT"),
            ("Microsoft software license terms", "DESKTOP_LEGAL_PROMPT"),
            ("Sign in to your account. Enter your password.", "DESKTOP_SIGN_IN_REQUIRED"),
            ("Unable to open private-path.pbip", "DESKTOP_REPORTED_ERROR"),
            ("There are unapplied changes", "DESKTOP_PENDING_CHANGES"),
        )
        for text, code in cases:
            with self.subTest(text=text):
                problem = desktop._dialog_problem([text])
                self.assertIsNotNone(problem)
                self.assertEqual(problem[0], code)
                self.assertNotIn("private-path", problem[1])

    def test_normal_sign_in_and_refresh_buttons_are_not_blocking_prompts(self):
        snapshot = report_snapshot()
        snapshot.nodes.extend([node("Sign in"), node("Refresh"), node("Refresh", "Text")])
        self.assertEqual(desktop._check_problems(snapshot), [])
        self.assertFalse(desktop._busy(snapshot))

    def test_unknown_modal_fails_and_never_automatically_accepts(self):
        snapshot = desktop._Snapshot([node("Unknown warning", "Window", parent=None, modal=True)])
        with self.assertRaises(DemoError) as error:
            desktop._check_problems(snapshot)
        self.assertEqual(error.exception.code, "DESKTOP_DIALOG_UNSUPPORTED")

    def test_only_initial_inline_unprocessed_visual_errors_can_be_deferred(self):
        snapshot = report_snapshot()
        snapshot.nodes.append(node("Couldn't load the data for this visual", "Text"))
        with self.assertRaises(DemoError):
            desktop._check_problems(snapshot)
        self.assertEqual(desktop._check_problems(snapshot, allow_unprocessed_visuals=True), [])
        snapshot.nodes.append(node(
            "Couldn't load the data for this visual", "Window", parent=None, modal=True,
        ))
        with self.assertRaises(DemoError):
            desktop._check_problems(snapshot, allow_unprocessed_visuals=True)

    def test_only_known_stale_hresult_is_retryable(self):
        self.assertTrue(desktop._stale_element(SimpleNamespace(hresult=0x80040201)))
        self.assertFalse(desktop._stale_element(SimpleNamespace(hresult=0x80070005)))
        self.assertFalse(desktop._stale_element(RuntimeError("element not available")))

    def test_report_title_is_exact_not_arbitrary_nonempty_or_suffix_match(self):
        self.assertTrue(desktop._title_matches("Example * - Power BI Desktop", ("Example",)))
        self.assertFalse(desktop._title_matches("Other Example - Power BI Desktop", ("Example",)))
        self.assertFalse(desktop._title_matches("Example", ("Example",)))


class OwnershipTests(unittest.TestCase):
    def test_job_assignment_precedes_resume_and_only_owned_job_is_killed(self):
        api = FakeNative()
        api.jobs["unrelated"] = {999}
        with desktop._OwnedDesktop(api, Path("PBIDesktop.exe"), Path("Example.pbip"), desktop._Deadline(10)) as owned:
            self.assertEqual(owned.pid, 101)
            owned.require_pid(101)
            with self.assertRaises(DemoError):
                owned.require_pid(999)
        operations = [event[0] for event in api.events]
        self.assertLess(operations.index("suspended"), operations.index("assign"))
        self.assertLess(operations.index("assign"), operations.index("resume"))
        self.assertEqual(api.jobs["unrelated"], {999})
        self.assertEqual([event for event in api.events if event[0] == "terminate_job"], [("terminate_job", "unit-job-2")])

    def test_assignment_failure_never_resumes_and_terminates_exact_suspended_process(self):
        api = FakeNative()
        api.assign_error = True
        with self.assertRaises(DemoError) as error:
            with desktop._OwnedDesktop(api, Path("PBIDesktop.exe"), Path("Example.pbip"), desktop._Deadline(10)):
                self.fail("Assignment must fail before returning ownership.")
        self.assertEqual(error.exception.code, "DESKTOP_LAUNCH_FAILED")
        self.assertNotIn("resume", [event[0] for event in api.events])
        self.assertIn(("terminate_suspended", "unit-process-101"), api.events)
        self.assertIn(("close_handle", "unit-job-1"), api.events)
        self.assertNotIn("private path", error.exception.message)

    def test_forwarded_process_is_not_attached_to_an_unrelated_instance(self):
        api = FakeNative()
        api.forward = True
        with desktop._OwnedDesktop(api, Path("PBIDesktop.exe"), Path("Example.pbip"), desktop._Deadline(10)) as owned:
            with self.assertRaises(DemoError) as error:
                owned.require_live()
            self.assertEqual(error.exception.code, "DESKTOP_PROCESS_EXITED")

    def test_create_process_uses_explicit_executable_suspension_and_no_handle_inheritance(self):
        api = desktop._Native.__new__(desktop._Native)
        observed = {}

        def create(executable, command, process_security, thread_security, inherit, flags, environment, directory, startup, result):
            observed.update(executable=executable, command=command.value, inherit=inherit, flags=flags)
            self.assertEqual(startup._obj.lpDesktop, "winsta0\\default")
            result._obj.process, result._obj.thread, result._obj.pid = 10, 11, 12
            return True

        api._create = create
        executable = Path(r"C:\Program Files\Power BI\PBIDesktop.exe")
        document = Path(r"C:\Demo Files\Example.pbip")
        self.assertEqual(api.suspended_process(executable, document), (10, 11, 12))
        self.assertEqual(observed["executable"], str(executable))
        self.assertEqual(observed["flags"], desktop._CREATE_SUSPENDED)
        self.assertFalse(observed["inherit"])
        self.assertIn('"', observed["command"])

    def test_job_limit_is_kill_on_close_without_breakaway(self):
        api = desktop._Native.__new__(desktop._Native)
        api._new_job = Mock(return_value=44)

        def limits(handle, information_class, information, size):
            self.assertEqual((handle, information_class), (44, 9))
            self.assertEqual(information._obj.BasicLimitInformation.LimitFlags, desktop._KILL_ON_JOB_CLOSE)
            return True

        api._job_limits = limits
        self.assertEqual(api.new_job(), 44)

    def test_only_pinned_owned_threads_have_windows_enumerated(self):
        api = desktop._Native.__new__(desktop._Native)
        api.members = Mock(return_value={101})
        api._snapshot = Mock(return_value=77)
        entries = iter(((201, 101), (202, 999), (203, 101)))

        def next_entry(_snapshot, pointer):
            try:
                tid, pid = next(entries)
            except StopIteration:
                return False
            pointer._obj.th32ThreadID = tid
            pointer._obj.th32OwnerProcessID = pid
            return True

        api._first_thread = api._next_thread = next_entry
        api._open_thread = Mock(side_effect=lambda access, inherit, tid: tid + 1000)
        # Thread 203 changed ownership before it could be pinned: never enumerate its windows.
        api._thread_pid = Mock(side_effect=lambda handle: 101 if handle == 1201 else 999)
        api.belongs = Mock(side_effect=lambda job, pid: pid == 101)
        api._window_callback = lambda callback: callback
        api._thread_windows = Mock(side_effect=lambda tid, callback, parameter: callback(1010, parameter))
        api.window_pid = Mock(return_value=101)
        api._visible = Mock(return_value=True)
        api._close = Mock(return_value=True)
        with patch.object(desktop.ctypes, "get_last_error", return_value=18, create=True):
            result = api.windows("unit-job", desktop._Deadline(10))
        self.assertEqual(result, (1010,))
        self.assertEqual([call.args[0] for call in api._thread_windows.call_args_list], [201])
        self.assertEqual([call.args[2] for call in api._open_thread.call_args_list], [201, 203])
        self.assertEqual({call.args[0] for call in api._close.call_args_list}, {77, 1201, 1203})

    def test_cleanup_error_still_closes_job_handle_and_is_not_swallowed(self):
        api = FakeNative()
        api.terminate_job = Mock(side_effect=OSError("unit-only native cleanup error"))
        with self.assertRaises(DemoError) as error:
            with desktop._OwnedDesktop(api, Path("PBIDesktop.exe"), Path("Example.pbip"), desktop._Deadline(10)):
                pass
        self.assertEqual(error.exception.code, "DESKTOP_CLEANUP_FAILED")
        self.assertIn(("close_handle", "unit-job-1"), api.events)


class OwnedProcessTests(WorkspaceCase):
    def launch(self, api, **kwargs):
        return desktop.OwnedProcess(
            [str(self.request.desktop_exe), "-m", "unit_module"],
            cwd=self.workspace, **kwargs,
        )

    def test_public_launcher_supports_main_workers_log_path_and_finite_poll_wait(self):
        api = FakeProcessNative()
        api.finish_on_wait = True
        log = self.workspace / "converter.log"
        with patch.object(desktop.sys, "platform", "win32"), patch.object(desktop, "_Native", return_value=api):
            with self.launch(api, log_path=log) as process:
                self.assertEqual(process.pid, 101)
                self.assertIsNone(process.poll())
                self.assertEqual(process.wait(timeout=0.25), 0)
                self.assertEqual(api.spawn_options["stdout_path"], log)
                self.assertEqual(api.spawn_options["stderr_path"], log)
                self.assertIsNone(api.spawn_options["stdin_path"])
            self.assertEqual(process.poll(), 0)
            process.close()
        operations = [event[0] for event in api.events]
        self.assertLess(operations.index("atomic_job_assignment"), operations.index("assign"))
        self.assertLess(operations.index("assign"), operations.index("resume"))
        self.assertEqual([event[2] for event in api.events if event[0] == "wait_process"], [250])
        self.assertEqual(operations.count("terminate_job"), 1)

    def test_wait_timeout_context_still_kills_only_its_own_tree(self):
        api = FakeProcessNative()
        api.jobs["unrelated"] = {999}
        with (
            patch.object(desktop.sys, "platform", "win32"),
            patch.object(desktop, "_Native", return_value=api),
            self.assertRaises(desktop.subprocess.TimeoutExpired),
        ):
            with self.launch(api) as process:
                process.wait(timeout=0)
        self.assertEqual(api.jobs["unrelated"], {999})
        self.assertEqual(api.jobs["unit-job-2"], set())
        self.assertIn(("close_handle", "unit-job-2"), api.events)

    def test_assignment_failure_never_resumes_even_with_atomic_job_creation(self):
        api = FakeProcessNative()
        api.assign_error = True
        with (
            patch.object(desktop.sys, "platform", "win32"),
            patch.object(desktop, "_Native", return_value=api),
            self.assertRaises(DemoError) as error,
        ):
            self.launch(api)
        self.assertEqual(error.exception.code, "OWNED_PROCESS_LAUNCH_FAILED")
        self.assertNotIn("resume", [event[0] for event in api.events])
        self.assertTrue(all(not members for members in api.jobs.values()))
        self.assertIn(("close_handle", "unit-process-101"), api.events)
        self.assertIn(("close_handle", "unit-thread-101"), api.events)

    def test_context_closes_descendants_even_after_root_exit(self):
        api = FakeProcessNative()
        api.finish_on_wait = True
        with patch.object(desktop.sys, "platform", "win32"), patch.object(desktop, "_Native", return_value=api):
            with self.launch(api) as process:
                api.jobs["unit-job-1"].add(202)
                self.assertEqual(process.wait(timeout=1), 0)
                self.assertEqual(api.jobs["unit-job-1"], {202})
        self.assertEqual(api.jobs["unit-job-1"], set())
        self.assertEqual(process.returncode, 0)

    def test_terminate_and_query_preserve_real_exit_code(self):
        api = FakeProcessNative()
        with patch.object(desktop.sys, "platform", "win32"), patch.object(desktop, "_Native", return_value=api):
            with self.launch(api) as process:
                process.terminate(timeout=1)
                self.assertEqual(process.poll(), 1)
                self.assertEqual(process.wait(timeout=0), 1)
        self.assertEqual(process.poll(), 1)

    def test_launch_validation_never_uses_shell_or_native_on_invalid_arguments(self):
        with patch.object(desktop.sys, "platform", "win32"), patch.object(desktop, "_Native") as native:
            for argv in ("python -m unit", [], ["python.exe"], [str(self.request.desktop_exe), "x\0y"]):
                with self.subTest(argv=argv), self.assertRaises(DemoError):
                    desktop.OwnedProcess(argv, cwd=self.workspace)
            with self.assertRaises(DemoError):
                self.launch(None, log_path=self.workspace / "a.log", stdout_path=self.workspace / "b.log")
            with self.assertRaises(DemoError):
                self.launch(None, stdin_path=self.request.project_file, log_path=self.request.project_file)
            native.assert_not_called()

    def test_non_windows_never_loads_native_api(self):
        with (
            patch.object(desktop.sys, "platform", "linux"), patch.object(desktop, "_Native") as native,
            self.assertRaises(DemoError) as error,
        ):
            desktop.OwnedProcess([str(self.request.desktop_exe)])
        self.assertEqual(error.exception.code, "WINDOWS_REQUIRED")
        native.assert_not_called()

    def test_environment_block_is_unicode_sorted_and_double_terminated(self):
        buffer = desktop._environment_buffer({"z": "last", "A": "中文", "=C:": r"C:\Demo"})
        self.assertEqual("".join(buffer), "=C:=C:\\Demo\0A=中文\0z=last\0\0")
        self.assertEqual("".join(desktop._environment_buffer({})), "\0\0")
        self.assertIsNone(desktop._environment_buffer(None))
        for environment in ({"a": "1", "A": "2"}, {"x": "bad\0value"}, {"bad=name": "1"}):
            with self.subTest(environment=environment), self.assertRaises(DemoError):
                desktop._environment_buffer(environment)

    def test_native_launch_uses_handle_allowlist_and_atomic_job_list(self):
        api = desktop._Native.__new__(desktop._Native)
        api._standard_handle = Mock(side_effect=[11, 12])
        api.close = Mock()
        api._delete_attributes = Mock()
        captured = {}

        def initialize(attributes, count, flags, size):
            self.assertEqual(count, 2)
            size._obj.value = 256
            return attributes is not None

        def update(attributes, flags, kind, values, size, previous, returned):
            captured[kind] = tuple(values)
            return True

        def create(executable, command, process_security, thread_security, inherit, flags, environment, cwd, startup, result):
            startup_ex = ctypes.cast(startup, ctypes.POINTER(desktop._StartupInfoEx)).contents
            captured["flags"], captured["inherit"], captured["command"] = flags, inherit, command.value
            captured["environment"] = "".join(environment)
            self.assertEqual(startup_ex.StartupInfo.cb, ctypes.sizeof(desktop._StartupInfoEx))
            self.assertEqual(startup_ex.StartupInfo.dwFlags, 0x100)
            self.assertEqual(
                (startup_ex.StartupInfo.hStdInput, startup_ex.StartupInfo.hStdOutput, startup_ex.StartupInfo.hStdError),
                (11, 12, 12),
            )
            result._obj.process, result._obj.thread, result._obj.pid = 61, 62, 63
            return True

        api._initialize_attributes, api._update_attribute, api._create = initialize, update, create
        with patch.object(desktop.ctypes, "get_last_error", return_value=122, create=True):
            result = api.suspended_command(
                (str(self.request.desktop_exe), "argument with spaces"), cwd=self.workspace,
                env={"A": "value"}, stdin_path=None, stdout_path=self.workspace / "log.txt",
                stderr_path=self.workspace / "log.txt", job=900,
            )
        self.assertEqual(result, (61, 62, 63))
        self.assertEqual(captured[desktop._ATTRIBUTE_HANDLE_LIST], (11, 12))
        self.assertEqual(captured[desktop._ATTRIBUTE_JOB_LIST], (900,))
        self.assertNotIn(900, captured[desktop._ATTRIBUTE_HANDLE_LIST])
        self.assertTrue(captured["inherit"])
        self.assertTrue(captured["flags"] & desktop._CREATE_SUSPENDED)
        self.assertTrue(captured["flags"] & desktop._EXTENDED_STARTUPINFO_PRESENT)
        self.assertTrue(captured["flags"] & desktop._CREATE_NO_WINDOW)
        self.assertTrue(captured["flags"] & desktop._CREATE_UNICODE_ENVIRONMENT)
        self.assertEqual(captured["environment"], "A=value\0\0")
        self.assertEqual([call.args[0] for call in api.close.call_args_list], [11, 12])
        api._delete_attributes.assert_called_once()

    def test_native_assignment_verifies_atomic_membership_without_reassigning(self):
        api = desktop._Native.__new__(desktop._Native)
        api._assign = Mock(return_value=True)

        def in_job(process, job, result):
            result._obj.value = 1
            return True

        api._in_job = in_job
        api.assign(40, 41)
        api._assign.assert_not_called()

    def test_native_poll_distinguishes_exit_code_259_from_still_running(self):
        api = desktop._Native.__new__(desktop._Native)
        api._wait = Mock(return_value=0)

        def exit_code(process, result):
            result._obj.value = 259
            return True

        api._exit_code = Mock(side_effect=exit_code)
        self.assertEqual(api.exit_code(40), 259)
        api._wait.return_value = 258
        self.assertIsNone(api.exit_code(40))
        api._exit_code.assert_called_once()

    def test_log_handle_is_append_only_and_inheritable(self):
        api = desktop._Native.__new__(desktop._Native)
        captured = {}

        def open_file(name, access, share, security, disposition, flags, template):
            captured.update(access=access, disposition=disposition, inherit=security._obj.bInheritHandle)
            return 18

        api._open_file = open_file
        self.assertEqual(api._standard_handle(self.workspace / "log.txt", reading=False), 18)
        self.assertTrue(captured["access"] & 4)  # FILE_APPEND_DATA
        self.assertFalse(captured["access"] & 2)  # Never FILE_WRITE_DATA / overwrite.
        self.assertEqual(captured["disposition"], 4)  # OPEN_ALWAYS, not CREATE_ALWAYS.
        self.assertTrue(captured["inherit"])

    def test_fixed_file_version_is_read_and_not_hardcoded(self):
        api = desktop._Native.__new__(desktop._Native)
        api._version_size = Mock(return_value=256)
        api._version_info = Mock(return_value=True)

        def query(buffer, key, pointer, length):
            self.assertEqual(key, "\\")
            info = desktop._FixedFileInfo.from_buffer(buffer)
            info.dwSignature = 0xFEEF04BD
            info.dwFileVersionMS = (2 << 16) | 157
            info.dwFileVersionLS = (1354 << 16) | 9
            pointer._obj.value = ctypes.addressof(buffer)
            length._obj.value = ctypes.sizeof(info)
            return True

        api._version_value = query
        self.assertEqual(api.file_version(self.request.desktop_exe), "2.157.1354.9")
        api._version_size.return_value = 0
        with self.assertRaises(DemoError) as error:
            api.file_version(self.request.desktop_exe)
        self.assertEqual(error.exception.code, "DESKTOP_VERSION_UNVERIFIED")


@unittest.skipUnless(
    desktop.sys.platform == "win32" and desktop.os.environ.get("PBIP_NATIVE_PROCESS_SMOKE") == "1",
    "Opt-in native smoke tests launch only owned, no-window Python helpers; never Desktop or UIA.",
)
class NativeOwnedProcessSmokeTests(WorkspaceCase):
    def test_python_file_version_can_be_read_without_desktop_or_uia(self):
        version = desktop._Native().file_version(Path(desktop.sys.executable))
        self.assertEqual(tuple(int(part) for part in version.split(".")[:2]), desktop.sys.version_info[:2])

    def test_redirected_python_process_appends_and_exits(self):
        log = self.workspace / "native.log"
        log.write_text("existing log\n", encoding="utf-8")
        with desktop.OwnedProcess(
            [
                desktop.sys.executable, "-I", "-c",
                "import sys; print('owned stdout'); print('owned stderr', file=sys.stderr)",
            ],
            cwd=self.workspace, log_path=log,
        ) as process:
            self.assertEqual(process.wait(timeout=10), 0)
        text = log.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("existing log\n"))
        self.assertIn("owned stdout", text)
        self.assertIn("owned stderr", text)
        self.assertEqual(process.poll(), 0)

    def test_crashed_owner_kills_child_without_terminating_outer_job(self):
        source = Path(__file__).resolve().parents[1] / "src"
        child_pid_file = self.workspace / "child.pid"
        owner_code = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from pbip_mcp.windows_desktop import OwnedProcess\n"
            "root = Path(sys.argv[2])\n"
            "child = OwnedProcess([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], "
            "cwd=root, log_path=root / 'child.log')\n"
            "Path(sys.argv[3]).write_text(str(child.pid), encoding='ascii')\n"
            "time.sleep(30)\n"
        )
        with desktop.OwnedProcess(
            [
                desktop.sys.executable, "-I", "-c", owner_code,
                str(source), str(self.workspace), str(child_pid_file),
            ],
            cwd=self.workspace, log_path=self.workspace / "owner.log",
        ) as owner:
            until = desktop.time.monotonic() + 10
            while not child_pid_file.is_file():
                self.assertIsNone(owner.poll(), "The owned Python helper exited before publishing its child PID.")
                self.assertLess(desktop.time.monotonic(), until, "Timed out waiting for the owned child PID.")
                desktop.time.sleep(0.05)
            child_pid = int(child_pid_file.read_text(encoding="ascii"))
            self.assertTrue(owner._api.belongs(owner._job, child_pid))
            child_handle = owner._api._open_process(0x00101000, False, child_pid)
            self.assertTrue(child_handle)
            try:
                # Kill only the owner process, leaving our outer job handle open.
                # Its nested job handle must close automatically and kill its child.
                self.assertTrue(owner._api._terminate_process(owner._process, 77))
                self.assertTrue(owner._api.wait_process(child_handle, 5000), "A child survived its owner's crash.")
                self.assertEqual(owner.wait(timeout=5), 77)
            finally:
                owner._api.close(child_handle)


class AutomationTests(WorkspaceCase):
    def make_ui(self, deadline=None):
        owned = SimpleNamespace(pid=101, job="unit-job", process="unit-process")
        owned.api = SimpleNamespace(window_pid=lambda handle: 101)
        owned.require_live = lambda: None

        def require_pid(pid):
            if pid != 101:
                raise DemoError("DESKTOP_OWNERSHIP_LOST", "Unit unowned process.")

        owned.require_pid = require_pid
        runtime = desktop._UIRuntime(FakeInfo, FakeWrapper, MissingPattern)
        evidence = Mock()
        return desktop._Automation(owned, deadline or desktop._Deadline(20), runtime, evidence)

    def test_nonempty_title_is_not_report_readiness(self):
        ui = self.make_ui()
        snapshot = report_snapshot()
        self.assertIsNone(ui.ready(desktop._Snapshot(snapshot.nodes[:1]), ("Example",), self.request.project.pages))
        self.assertIsNotNone(ui.ready(snapshot, ("Example",), self.request.project.pages))
        self.assertIsNone(ui.ready(snapshot, ("Example",), ("Missing page",)))

    def test_report_readiness_excludes_inactive_data_explorer_ribbon(self):
        ui = self.make_ui()
        snapshot = desktop._Snapshot([
            node("Example - Power BI Desktop", "Window", parent=None),
            node("ms-pbi.pbi.microsoft.com/minerva/reportView.html", "Pane", parent=0),
            node("File", "TabItem", parent=1),
            node("Home", "TabItem", parent=1),
            node("Synthetic overview", "TabItem", parent=1),
            node("ms-pbi.pbi.microsoft.com/minerva/dataExploreView.html", "Pane", parent=0),
            node("File", "TabItem", parent=5),
            node("Home", "TabItem", parent=5),
        ])
        self.assertIsNotNone(ui.ready(snapshot, ("Example",), self.request.project.pages))

    def test_nested_split_button_uses_its_unique_invoke_only_leaf(self):
        snapshot = desktop._Snapshot([
            node("Refresh", parent=None),
            node("Refresh", parent=0, iface_invoke=Mock()),
            node("Refresh", parent=0, iface_invoke=Mock(), iface_expand_collapse=Mock()),
        ])
        self.assertIs(self.make_ui().find(snapshot, ("Refresh",)), snapshot.nodes[1])

    def test_snapshot_excludes_only_known_inactive_internal_view_branches(self):
        ui = self.make_ui()
        inactive = FakeInfo(
            "ms-pbi.pbi.microsoft.com/minerva/daxQueryView.html", kind="Pane",
            children=[FakeInfo("Run DAX queries on your model")],
        )
        root = FakeInfo("Example - Power BI Desktop", children=[inactive])
        ui.owned.api.windows = lambda job, deadline: (1010,)
        ui.runtime.element = lambda handle: root
        snapshot = ui.snapshot("unit inactive view")
        self.assertEqual(len(snapshot.nodes), 1)
        ui.evidence.record.assert_called_once()
        self.assertFalse(desktop._inactive_view("ms-pbi.pbi.microsoft.com/minerva/reportView.html"))
        self.assertFalse(desktop._inactive_view("ms-pbi.pbi.microsoft.com/minerva/fileMenuView.html"))
        self.assertFalse(desktop._inactive_view("ms-pbi://pbi.microsoft.com/Views/FloatingDialog/FloatingDialog.htm"))

    def test_untitled_refresh_progress_is_busy_but_still_checks_security_content(self):
        snapshot = desktop._Snapshot([
            node("", "Window", parent=None, auto_id="KoLoadToReportDialog", modal=True),
            node("Starting refresh", "Text", parent=0),
        ])
        self.assertTrue(desktop._busy(snapshot))
        self.assertEqual(desktop._check_problems(snapshot), [])
        snapshot.nodes[1].name = "Privacy levels require operator review"
        with self.assertRaises(DemoError) as error:
            desktop._check_problems(snapshot)
        self.assertEqual(error.exception.code, "DESKTOP_SECURITY_PROMPT")

    def test_current_card_image_uses_named_visible_children(self):
        snapshot = desktop._Snapshot([
            node("Example - Power BI Desktop", "Window", parent=None),
            node("Total Amount 60.", "Image", parent=0),
            node("60", "Text", parent=1),
            node("Total Amount", "Text", parent=1),
        ])
        self.assertTrue(desktop._card_value_observed(snapshot, snapshot.nodes[0]))
        self.assertTrue(desktop._entity_label("Measure Field Total Amount", "Total Amount", "measure"))
        self.assertFalse(desktop._entity_label("Measure Field Other Amount", "Total Amount", "measure"))

    def test_data_pane_button_header_contains_real_table_tree_items(self):
        snapshot = desktop._Snapshot([
            node("Example - Power BI Desktop", "Window", parent=None),
            node("", "Pane", parent=0),
            node("Data", "Button", parent=1),
            node("", "Tree", parent=1),
            node("Table Sales", "TreeItem", parent=3),
        ])
        snapshot.nodes[1].depth = 2
        self.assertEqual(desktop._field_nodes(snapshot, snapshot.nodes[0]), [snapshot.nodes[4]])

    def test_rooted_snapshot_rejects_foreign_provider_before_reading_its_name(self):
        ui = self.make_ui()

        class ForeignInfo:
            process_id = 999

            @property
            def name(self):
                raise AssertionError("Foreign window text must never be read.")

        root = FakeInfo("Example - Power BI Desktop", children=[ForeignInfo()])
        ui.owned.api.windows = lambda job, deadline: (1010,)
        ui.runtime.element = lambda handle: root
        with self.assertRaises(DemoError) as error:
            ui.snapshot("unit scope")
        self.assertEqual(error.exception.code, "DESKTOP_OWNERSHIP_LOST")
        self.assertEqual(len(ui.last_snapshot.nodes), 1)

    def test_snapshot_retries_destroyed_provider_without_reading_unowned_text(self):
        ui = self.make_ui()

        class DestroyedInfo:
            process_id = None

            @property
            def name(self):
                raise AssertionError("A provider with no verified PID must not be read.")

        root = FakeInfo("Example - Power BI Desktop")
        ui.owned.api.windows = lambda job, deadline: (1010,)
        ui.runtime.element = Mock(side_effect=[DestroyedInfo(), root])
        with patch.object(ui.deadline, "pause") as pause:
            snapshot = ui.snapshot("unit destroyed splash")
        self.assertEqual(ui.runtime.element.call_count, 2)
        pause.assert_called_once_with("unit destroyed splash")
        self.assertEqual([item.name for item in snapshot.nodes], ["Example - Power BI Desktop"])

    def test_snapshot_retries_destroyed_window_before_creating_its_provider(self):
        ui = self.make_ui()
        ui.owned.api.windows = lambda job, deadline: (1010,)
        ui.owned.api.window_pid = Mock(side_effect=[0, 101])
        ui.runtime.element = Mock(return_value=FakeInfo("Example - Power BI Desktop"))
        with patch.object(ui.deadline, "pause") as pause:
            snapshot = ui.snapshot("unit destroyed window")
        ui.runtime.element.assert_called_once_with(1010)
        pause.assert_called_once_with("unit destroyed window")
        self.assertEqual(len(snapshot.nodes), 1)

    def test_snapshot_retries_zero_pid_when_save_dialog_is_destroyed(self):
        ui = self.make_ui()
        ui.owned.api.windows = lambda job, deadline: (1010,)
        ui.runtime.element = Mock(side_effect=[FakeInfo(pid=0), FakeInfo("Example - Power BI Desktop")])
        with patch.object(ui.deadline, "pause") as pause:
            snapshot = ui.snapshot("unit closing Save As")
        pause.assert_called_once_with("unit closing Save As")
        self.assertEqual(len(snapshot.nodes), 1)
        self.assertEqual(snapshot.nodes[0].pid, 101)

    def test_native_dialog_is_not_counted_again_under_its_owner(self):
        ui = self.make_ui()
        dialog = FakeInfo("Save As", class_name="#32770", handle=2020)
        main = FakeInfo("Example - Power BI Desktop", children=[dialog], handle=1010)
        ui.owned.api.windows = lambda job, deadline: (1010, 2020)
        ui.runtime.element = lambda handle: main if handle == 1010 else dialog
        snapshot = ui.snapshot("unit duplicated native dialog")
        self.assertEqual(len(snapshot.nodes), 2)
        self.assertIs(ui.save_dialog(snapshot), snapshot.nodes[1])
        self.assertIsNone(snapshot.nodes[1].parent)

    def test_native_combo_popup_is_not_counted_again_under_its_combo(self):
        ui = self.make_ui()
        option = FakeInfo("Power BI file (*.pbix)", kind="ListItem")
        popup = FakeInfo("Save as type:", kind="List", children=[option], handle=2020)
        client = FakeInfo("", kind="Pane", children=[popup], handle=1010)
        dialog = FakeInfo("Save As", children=[client], class_name="#32770", handle=1010)
        ui.owned.api.windows = lambda job, deadline: (1010, 2020)
        ui.runtime.element = lambda handle: dialog if handle == 1010 else popup
        snapshot = ui.snapshot("unit duplicated popup")
        self.assertEqual(len(snapshot.nodes), 4)
        self.assertEqual(
            ui.find(snapshot, desktop._PBIX_TYPES, ("ListItem",)).name,
            "Power BI file (*.pbix)",
        )

    def test_webview_aria_dialog_is_not_mistaken_for_ready_report_content(self):
        ui = self.make_ui()
        dialog = FakeInfo("Sign in to Power BI", kind="Group")
        dialog.element.CurrentAriaRole = "dialog"
        root = FakeInfo("Example - Power BI Desktop", children=[dialog])
        ui.owned.api.windows = lambda job, deadline: (1010,)
        ui.runtime.element = lambda handle: root
        snapshot = ui.snapshot("unit ARIA dialog")
        self.assertTrue(snapshot.nodes[1].modal)
        with self.assertRaises(DemoError) as error:
            desktop._check_problems(snapshot)
        self.assertEqual(error.exception.code, "DESKTOP_SIGN_IN_REQUIRED")

    def test_only_value_pattern_sets_filename(self):
        ui = self.make_ui()
        value = FakeValue()
        edit = node("File name:", "Edit", iface_value=value)
        with patch.object(desktop, "interactive_readiness", return_value=READY):
            ui.set_value(edit, str(self.request.output_file))
        self.assertEqual(value.writes, [str(self.request.output_file)])

    def test_file_type_selection_closes_popup_before_filename_editing(self):
        ui = self.make_ui()
        value = FakeValue("Power BI project files (*.pbip)")
        expand = SimpleNamespace(CurrentExpandCollapseState=0)
        expand.Expand = Mock(side_effect=lambda: setattr(expand, "CurrentExpandCollapseState", 1))
        expand.Collapse = Mock(side_effect=lambda: setattr(expand, "CurrentExpandCollapseState", 0))
        selection = SimpleNamespace(
            Select=lambda: setattr(value, "CurrentValue", "Power BI file (*.pbix)"),
        )
        snapshot = desktop._Snapshot([
            node("Save As", "Window", parent=None, class_name="#32770", modal=True),
            node("Save as type:", "ComboBox", parent=0, auto_id="FileTypeControlHost",
                 iface_value=value, iface_expand_collapse=expand),
            node("Power BI file (*.pbix)", "ListItem", parent=1, iface_selection_item=selection),
        ])
        with patch.object(ui, "snapshot", return_value=snapshot), patch.object(
            desktop, "interactive_readiness", return_value=READY,
        ):
            ui._choose_pbix()
        expand.Expand.assert_called_once()
        expand.Collapse.assert_called_once()
        self.assertEqual(expand.CurrentExpandCollapseState, 0)
        self.assertEqual(value.CurrentValue, "Power BI file (*.pbix)")

    def test_missing_action_pattern_fails_without_keyboard_fallback(self):
        ui = self.make_ui()
        with patch.object(desktop, "interactive_readiness", return_value=READY), self.assertRaises(DemoError) as error:
            ui.activate(node("Save"))
        self.assertEqual(error.exception.code, "UIA_PATTERN_UNSUPPORTED")

    def test_lock_or_disconnect_between_phases_prevents_the_next_action(self):
        ui = self.make_ui()
        invoke = Mock()
        target = node("Save", iface_invoke=SimpleNamespace(Invoke=invoke))
        inactive = {"ready": False, "code": "RDP_SESSION_INACTIVE", "message": "Unit disconnected.", "session_id": 7}
        with patch.object(desktop, "interactive_readiness", return_value=inactive), self.assertRaises(DemoError) as error:
            ui.activate(target)
        self.assertEqual(error.exception.code, "RDP_SESSION_INACTIVE")
        invoke.assert_not_called()

    def test_file_type_selection_is_read_without_value_pattern_or_input_fallback(self):
        ui = self.make_ui()
        selected = FakeInfo("Power BI (*.pbix)", kind="ListItem")
        ui.runtime.element = lambda element: element
        combo = node(
            "Save as type:", "ComboBox",
            iface_selection=SimpleNamespace(
                GetCurrentSelection=lambda: SimpleNamespace(Length=1, GetElement=lambda index: selected),
            ),
        )
        self.assertEqual(ui.selected_value(combo), "Power BI (*.pbix)")

    def test_ordinary_project_cannot_use_fixture_refresh_even_when_called_directly(self):
        ui = self.make_ui()
        with patch.object(ui, "wait_control") as control, self.assertRaises(DemoError) as error:
            ui.refresh_fixture(("Example",), self.request.project)
        self.assertEqual(error.exception.code, "REFRESH_NOT_ALLOWED")
        control.assert_not_called()

    def test_ambiguous_controls_fail_instead_of_first_match(self):
        snapshot = desktop._Snapshot([node("Save", parent=None), node("Save", parent=None)])
        with self.assertRaises(DemoError) as error:
            self.make_ui().find(snapshot, ("Save",))
        self.assertEqual(error.exception.code, "UIA_AMBIGUOUS_CONTROL")

    def test_card_number_must_be_inside_a_measure_labeled_visual(self):
        snapshot = report_snapshot()
        main = snapshot.nodes[0]
        snapshot.nodes.extend([node("60", "Text"), node("Total Amount", "TreeItem")])
        self.assertFalse(desktop._card_value_observed(snapshot, main))
        card_index = len(snapshot.nodes)
        snapshot.nodes.extend([
            node("Card", "Group"), node("Total Amount", "Text", parent=card_index),
            node("60", "Text", parent=card_index),
        ])
        self.assertTrue(desktop._card_value_observed(snapshot, main))

    def test_card_60_prefix_is_not_evidence_of_exact_60(self):
        for value in ("60.25", "60,000", "60%", "600"):
            with self.subTest(value=value):
                snapshot = report_snapshot()
                snapshot.nodes.extend([
                    node("Card", "Group"), node(f"Total Amount: {value}", "Text", parent=4),
                ])
                self.assertFalse(desktop._card_value_observed(snapshot, snapshot.nodes[0]))

    def test_synthetic_model_observation_has_exact_keys_and_does_not_overclaim(self):
        ui = self.make_ui()
        snapshot = report_snapshot()
        snapshot.nodes.extend([
            node("Data", "Pane"), node("Sales", "TreeItem", parent=4),
            node("Total Amount", "TreeItem", parent=5), node("Card", "Group"),
            node("Total Amount", "Text", parent=7), node("60", "Text", parent=7),
        ])
        project = replace(self.request.project, synthetic_fixture=True)
        with patch.object(ui, "snapshot", return_value=snapshot):
            observed = ui.observe_model(("Example",), project, "unit synthetic evidence")
        self.assertEqual(set(observed), {
            "evidence", "visible_field_items", "table_name_observed", "measure_name_observed",
            "measure_type_label_observed", "measure_under_table_observed", "card_value_60_observed",
            "card_value_observed", "card_value_source",
            "measure_expression_verified", "category_rows_verified", "exact_model_metadata_verified",
        })
        self.assertEqual(observed["table_name_observed"], "Sales")
        self.assertEqual(observed["measure_name_observed"], "Total Amount")
        self.assertTrue(observed["measure_under_table_observed"])
        self.assertTrue(observed["card_value_60_observed"])
        self.assertEqual(observed["card_value_observed"], 60)
        self.assertEqual(observed["card_value_source"], "uia_accessible_name")
        self.assertFalse(observed["measure_type_label_observed"])
        self.assertFalse(observed["measure_expression_verified"])
        self.assertFalse(observed["category_rows_verified"])
        self.assertFalse(observed["exact_model_metadata_verified"])

    def test_card_text_pattern_is_scoped_bounded_and_returns_observed_number(self):
        ui = self.make_ui()
        get_text = Mock(return_value="60\nTotal Amount")
        snapshot = report_snapshot()
        snapshot.nodes.append(node(
            "Card", "Group",
            iface_text=SimpleNamespace(DocumentRange=SimpleNamespace(GetText=get_text)),
        ))
        observation = ui.card_observation(snapshot, snapshot.nodes[0])
        self.assertIsNotNone(observation)
        self.assertEqual(observation.scalar, 60)
        self.assertEqual(observation.source, "uia_text_pattern")
        get_text.assert_called_once_with(2048)

    def test_metric_parser_handles_card_summary_order_and_rejects_rounded_or_ambiguous_values(self):
        self.assertEqual(desktop._metric_value(["60, Total Amount, Card"]), desktop.Decimal(60))
        self.assertEqual(desktop._metric_value(["Card, Total Amount: 60.00"]), desktop.Decimal(60))
        self.assertNotEqual(desktop._metric_value(["Total Amount: 60.000000000000000001"]), desktop.Decimal(60))
        self.assertIsNone(desktop._metric_value(["Card", "Total Amount", "60", "61"]))

    def test_required_synthetic_card_waits_then_fails_if_value_is_absent_or_wrong(self):
        for value, code in ((None, "SYNTHETIC_CARD_UNVERIFIED"), ("61", "SYNTHETIC_CARD_MISMATCH")):
            clock = FakeClock()
            with (
                self.subTest(value=value),
                patch.object(desktop.time, "monotonic", clock.monotonic),
                patch.object(desktop.time, "sleep", clock.sleep),
            ):
                ui = self.make_ui(desktop._Deadline(60))
                snapshot = report_snapshot()
                snapshot.nodes.extend([
                    node("Data", "Pane"), node("Sales", "TreeItem", parent=4),
                    node("Total Amount", "TreeItem", parent=5), node("Card", "Group"),
                    node("Total Amount", "Text", parent=7),
                ])
                if value is not None:
                    snapshot.nodes.append(node(value, "Text", parent=7))
                with patch.object(ui, "snapshot", return_value=snapshot), self.assertRaises(DemoError) as error:
                    ui.observe_model(
                        ("Example",), replace(self.request.project, synthetic_fixture=True),
                        "unit required card", require_card=True,
                    )
                self.assertEqual(error.exception.code, code)
                self.assertLessEqual(clock.now, 31)

    def test_required_synthetic_card_succeeds_only_after_repeated_observation(self):
        clock = FakeClock()
        with patch.object(desktop.time, "monotonic", clock.monotonic), patch.object(desktop.time, "sleep", clock.sleep):
            ui = self.make_ui(desktop._Deadline(60))
            snapshot = report_snapshot()
            snapshot.nodes.extend([
                node("Data", "Pane"), node("Sales", "TreeItem", parent=4),
                node("Total Amount", "TreeItem", parent=5), node("Card", "Group"),
                node("Total Amount", "Text", parent=7), node("60.00", "Text", parent=7),
            ])
            with patch.object(ui, "snapshot", return_value=snapshot):
                observed = ui.observe_model(
                    ("Example",), replace(self.request.project, synthetic_fixture=True),
                    "fresh unit PBIX", require_card=True,
                )
            self.assertEqual(observed["card_value_observed"], 60)
            self.assertTrue(observed["card_value_60_observed"])
            self.assertGreaterEqual(clock.now, 1)
            ui.evidence.record.assert_called_once()

    def test_save_as_drives_invoke_selection_value_and_explicit_pbix_not_template(self):
        clock = FakeClock()
        calls: list[str] = []
        state = {"stage": "report"}
        filename = FakeValue()
        file_type = FakeValue("Power BI template (*.pbit)")
        output = self.request.output_file

        def invoked(action, next_stage):
            calls.append(action)
            state["stage"] = next_stage

        def save():
            calls.append("save")
            self.assertEqual(filename.CurrentValue, str(output))
            self.assertEqual(file_type.CurrentValue, "Power BI (*.pbix)")
            output.write_bytes(fake_pbix())
            state["stage"] = "saved"

        def select_pbix():
            calls.append("select_pbix")
            file_type.CurrentValue = "Power BI (*.pbix)"
            state["stage"] = "dialog"

        def snapshot(_phase, **_kwargs):
            stage = state["stage"]
            if stage in ("report", "saved"):
                result = report_snapshot("Converted" if stage == "saved" else "Example")
                result.nodes[1].wrapper.patterns["iface_invoke"] = SimpleNamespace(
                    Invoke=lambda: invoked("file", "backstage"),
                )
            elif stage == "backstage":
                result = desktop._Snapshot([
                    node("Save As", parent=None, iface_invoke=SimpleNamespace(Invoke=lambda: invoked("save_as", "dialog"))),
                ])
            else:
                result = desktop._Snapshot([
                    node("Save As", "Window", parent=None, class_name="#32770", modal=True),
                    node(
                        "Save as type:", "ComboBox", auto_id="FileTypeControlHost", iface_value=file_type,
                        iface_expand_collapse=SimpleNamespace(
                            CurrentExpandCollapseState=0, Expand=lambda: invoked("expand_types", "options"),
                        ),
                    ),
                    node("File name:", "Edit", auto_id="1001", class_name="Edit", iface_value=filename),
                    node("Save", auto_id="1", iface_invoke=SimpleNamespace(Invoke=save)),
                    node("File name:", "ComboBox", auto_id="FileNameControlHost", iface_value=filename),
                ])
                result.nodes[2].wrapper.element_info.handle = 3030
                result.nodes[2].wrapper.element_info.element.SetFocus = lambda: (
                    calls.append("focus_filename"),
                    setattr(result.nodes[2].wrapper.element_info.element, "CurrentHasKeyboardFocus", True),
                )
                result.nodes[3].wrapper.element_info.element.SetFocus = lambda: (
                    calls.append("commit_filename"),
                    setattr(result.nodes[3].wrapper.element_info.element, "CurrentHasKeyboardFocus", True),
                )
                if stage == "options":
                    result.nodes.append(node(
                        "Power BI (*.pbix)", "ListItem", iface_selection_item=SimpleNamespace(Select=select_pbix),
                    ))
            ui.last_snapshot = result
            return result

        with patch.object(desktop.time, "monotonic", clock.monotonic), patch.object(desktop.time, "sleep", clock.sleep):
            ui = self.make_ui(desktop._Deadline(30))
            ui.runtime.native_edit = lambda handle: SimpleNamespace(
                set_edit_text=filename.SetValue, window_text=lambda: filename.CurrentValue,
            )
            with patch.object(ui, "snapshot", side_effect=snapshot), patch.object(desktop, "interactive_readiness", return_value=READY):
                ui.save_as(("Example",), output, self.request.project)
        self.assertEqual(calls, [
            "file", "save_as", "expand_types", "select_pbix", "focus_filename", "commit_filename", "save",
        ])
        self.assertTrue(output.is_file())
        self.assertGreaterEqual(clock.now, 2.0)

    def test_native_filename_rejects_nonowned_handle_before_editing(self):
        ui = self.make_ui()
        filename = node("File name:", "Edit", class_name="Edit", iface_value=FakeValue())
        filename.wrapper.element_info.handle = 3030
        ui.owned.api.window_pid = lambda handle: 999 if handle == 3030 else 101
        ui.runtime.native_edit = Mock()
        with patch.object(desktop, "interactive_readiness", return_value=READY), self.assertRaises(DemoError) as error:
            ui.set_native_filename(filename, str(self.request.output_file))
        self.assertEqual(error.exception.code, "DESKTOP_OWNERSHIP_LOST")
        ui.runtime.native_edit.assert_not_called()

    def test_output_collision_is_rejected_before_ui_action(self):
        self.request.output_file.write_bytes(b"existing output must survive")
        ui = self.make_ui()
        with patch.object(ui, "wait_control") as control, self.assertRaises(DemoError) as error:
            ui.save_as(("Example",), self.request.output_file, self.request.project)
        self.assertEqual(error.exception.code, "OUTPUT_EXISTS")
        control.assert_not_called()
        self.assertEqual(self.request.output_file.read_bytes(), b"existing output must survive")

    def test_double_extension_is_rejected(self):
        Path(str(self.request.output_file) + ".pbip").write_text("{}", encoding="utf-8")
        with self.assertRaises(DemoError) as error:
            desktop._reject_wrong_extension(self.request.output_file)
        self.assertEqual(error.exception.code, "SAVE_WRONG_FORMAT")


class ConversionWorkflowTests(WorkspaceCase):
    def run_fake(self, *, synthetic=False, **automation_flags):
        self.api = FakeNative()
        self.automation_type = type("ConfiguredUnitAutomation", (FakeConversionAutomation,), automation_flags)
        request = replace(self.request, project=replace(self.request.project, synthetic_fixture=synthetic))

        @contextmanager
        def fake_mutex(timeout):
            self.api.events.append(("mutex_enter", timeout))
            try:
                yield
            finally:
                self.api.events.append(("mutex_exit",))

        with (
            patch.object(desktop, "interactive_readiness", return_value=READY),
            patch.object(desktop, "desktop_session_mutex", side_effect=fake_mutex),
            patch.object(desktop, "_load_uia", return_value=object()),
            patch.object(desktop, "_Native", return_value=self.api),
            patch.object(desktop, "_Automation", self.automation_type),
        ):
            return desktop.WindowsDesktopConverter().convert(request)

    def test_normal_conversion_validates_container_and_reopens_in_fresh_process_without_refresh(self):
        original = self.request.project_file.read_bytes()
        result = self.run_fake()
        self.assertNotEqual(result.desktop_pid, result.reopened_pid)
        self.assertTrue(result.model_present)
        self.assertEqual(result.pages, self.request.project.pages)
        self.assertTrue(result.details["container"]["binary_model_present"])
        self.assertTrue(result.details["session_mutex_acquired"])
        self.assertFalse(result.details["refresh"]["requested"])
        self.assertFalse(result.details["reopened_model_observation"]["exact_model_metadata_verified"])
        self.assertFalse(result.details["reopened_model_observation"]["card_value_60_observed"])
        self.assertTrue(all(not members for members in self.api.jobs.values()))
        self.assertEqual(self.request.project_file.read_bytes(), original)
        operations = [event[0] for event in self.api.events]
        self.assertEqual(operations.count("suspended"), 2)
        self.assertEqual(operations.count("graceful_close"), 2)
        self.assertNotIn("fixture_refresh", operations)
        self.assertEqual(operations[0], "mutex_enter")
        self.assertEqual(operations[-1], "mutex_exit")
        launches = [index for index, operation in enumerate(operations) if operation == "suspended"]
        self.assertLess(operations.index("file_released"), launches[1])
        phases = [json.loads(line)["phase"] for path in self.request.evidence_dir.glob("*-phases.jsonl") for line in path.read_text().splitlines()]
        self.assertIn("pbix-container-validated", phases)
        self.assertIn("pbix-fresh-open-verified", phases)
        self.assertEqual(phases[-1], "complete")
        self.assertNotIn(str(self.workspace), json.dumps(result.as_dict()))

    def test_portable_existing_cache_is_never_refreshed(self):
        model = self.request.project_file.parent / self.request.project.model
        cache = model / ".pbi" / "cache.abf"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"UNIT-only cache")
        result = self.run_fake(synthetic=True)
        self.assertFalse(result.details["refresh"]["requested"])
        self.assertNotIn("fixture_refresh", [event[0] for event in self.api.events])

    def test_synthetic_refresh_happens_once_and_only_before_save(self):
        result = self.run_fake(synthetic=True)
        operations = [event[0] for event in self.api.events]
        self.assertEqual(operations.count("fixture_refresh"), 1)
        self.assertLess(operations.index("fixture_refresh"), operations.index("save_as"))
        self.assertTrue(result.details["refresh"]["requested"])
        published = {key: result.details[key] for key in (
            "desktop_version", "synthetic_total_amount", "model_tables", "model_measures", "verification_method",
        )}
        self.assertEqual(published["desktop_version"], "2.157.1354.0")
        self.assertEqual(published["synthetic_total_amount"], 60)
        self.assertEqual(published["model_tables"], ["Sales"])
        self.assertEqual(published["model_measures"], ["Total Amount"])
        self.assertIn("DAX expression and individual rows were not queried", published["verification_method"])

    def test_synthetic_public_success_requires_fresh_card_value_and_provenance(self):
        with self.assertRaises(DemoError) as error:
            self.run_fake(synthetic=True, missing_card=True)
        self.assertEqual(error.exception.code, "SYNTHETIC_CARD_UNVERIFIED")
        self.assertTrue(all(not members for members in self.api.jobs.values()))

    def test_public_summary_rejects_expected_only_or_unproven_card_values(self):
        project = replace(self.request.project, synthetic_fixture=True)
        valid = {
            "card_value_observed": 60, "card_value_60_observed": True,
            "card_value_source": "uia_accessible_name", "table_name_observed": "Sales",
            "measure_name_observed": "Total Amount",
        }
        for update in (
            {"card_value_source": "expected_fixture_value"},
            {"card_value_60_observed": False},
            {"card_value_observed": "60"},
            {"card_value_observed": 60.25},
        ):
            with self.subTest(update=update), self.assertRaises(DemoError):
                desktop._verification_summary("2.157.1354.0", project, {**valid, **update})

    def test_tmsl_and_tmdl_share_the_same_gui_workflow_contract(self):
        self.request = replace(self.request, project=replace(self.request.project, model_format="tmsl"))
        result = self.run_fake(synthetic=True)
        self.assertTrue(result.model_present)
        self.assertEqual(result.pages, ("Synthetic overview",))
        self.assertTrue(result.details["refresh"]["requested"])

    def test_template_container_fails_before_fresh_launch(self):
        with self.assertRaises(DemoError) as error:
            self.run_fake(template_output=True)
        self.assertEqual(error.exception.code, "PBIX_MODEL_MISSING")
        self.assertEqual(sum(event[0] == "suspended" for event in self.api.events), 1)
        self.assertTrue(all(not members for members in self.api.jobs.values()))

    def test_missing_fresh_model_cannot_return_success_and_both_jobs_are_closed(self):
        with self.assertRaises(DemoError) as error:
            self.run_fake(fail_reopen=True)
        self.assertEqual(error.exception.code, "DESKTOP_MODEL_UNVERIFIED")
        self.assertTrue(all(not members for members in self.api.jobs.values()))
        phases = [json.loads(line)["phase"] for path in self.request.evidence_dir.glob("*-phases.jsonl") for line in path.read_text().splitlines()]
        self.assertEqual(phases[-1], "failed")
        self.assertNotIn("complete", phases)

    def test_fresh_reopen_must_not_mutate_the_saved_artifact(self):
        with self.assertRaises(DemoError) as error:
            self.run_fake(mutate_reopen=True)
        self.assertEqual(error.exception.code, "PBIX_CHANGED_ON_REOPEN")
        self.assertTrue(all(not members for members in self.api.jobs.values()))

    def test_expired_completion_deadline_cannot_return_success(self):
        clock = FakeClock()
        original_record = desktop._Evidence.record

        def record(evidence, phase, details, snapshot=None):
            original_record(evidence, phase, details, snapshot)
            if phase == "complete":
                clock.now = 601.0

        with (
            patch.object(desktop.time, "monotonic", clock.monotonic),
            patch.object(desktop._Evidence, "record", record),
            self.assertRaises(DemoError) as error,
        ):
            self.run_fake()
        self.assertEqual(error.exception.code, "DESKTOP_TIMEOUT")
        self.assertTrue(all(not members for members in self.api.jobs.values()))

    def test_failed_preflight_never_loads_uia_or_launches_processes(self):
        failed = {"ready": False, "code": "DESKTOP_LOCKED", "message": "Unlock the worker.", "session_id": 7}
        with (
            patch.object(desktop, "interactive_readiness", return_value=failed),
            patch.object(desktop, "desktop_session_mutex") as mutex,
            patch.object(desktop, "_load_uia") as uia,
            patch.object(desktop, "_Native") as native,
            self.assertRaises(DemoError) as error,
        ):
            desktop.WindowsDesktopConverter().convert(self.request)
        self.assertEqual(error.exception.code, "DESKTOP_LOCKED")
        uia.assert_not_called()
        native.assert_not_called()
        mutex.assert_not_called()

    def test_busy_session_never_initializes_uia_or_launches_desktop(self):
        with (
            patch.object(desktop, "interactive_readiness", return_value=READY),
            patch.object(desktop, "desktop_session_mutex", side_effect=DemoError("DESKTOP_SESSION_BUSY", "Unit busy.")),
            patch.object(desktop, "_load_uia") as uia,
            patch.object(desktop, "_Native") as native,
            self.assertRaises(DemoError) as error,
        ):
            desktop.WindowsDesktopConverter().convert(self.request)
        self.assertEqual(error.exception.code, "DESKTOP_SESSION_BUSY")
        uia.assert_not_called()
        native.assert_not_called()

    def test_session_readiness_is_rechecked_after_mutex_acquisition(self):
        inactive = {"ready": False, "code": "RDP_SESSION_INACTIVE", "message": "Unit disconnected.", "session_id": 7}
        with (
            patch.object(desktop, "interactive_readiness", side_effect=[READY, inactive]),
            patch.object(desktop, "desktop_session_mutex", return_value=nullcontext()),
            patch.object(desktop, "_load_uia") as uia,
            patch.object(desktop, "_Native") as native,
            self.assertRaises(DemoError) as error,
        ):
            desktop.WindowsDesktopConverter().convert(self.request)
        self.assertEqual(error.exception.code, "RDP_SESSION_INACTIVE")
        uia.assert_not_called()
        native.assert_not_called()

    def test_dependency_version_mismatch_fails_before_importing_uia(self):
        with patch.object(desktop.importlib.metadata, "version", return_value="0.6.8"), self.assertRaises(DemoError) as error:
            desktop._load_uia()
        self.assertEqual(error.exception.code, "UIA_VERSION_UNSUPPORTED")

    def test_arbitrary_backend_exception_is_safe_and_cleanup_is_not_skipped(self):
        self.api = FakeNative()
        private = str(self.workspace / "private-detail")
        with (
            patch.object(desktop, "interactive_readiness", return_value=READY),
            patch.object(desktop, "desktop_session_mutex", return_value=nullcontext()),
            patch.object(desktop, "_load_uia", return_value=object()),
            patch.object(desktop, "_Native", return_value=self.api),
            patch.object(desktop, "_Automation", side_effect=RuntimeError(private)),
            self.assertRaises(DemoError) as error,
        ):
            desktop.WindowsDesktopConverter().convert(self.request)
        self.assertEqual(error.exception.code, "DESKTOP_CONVERSION_FAILED")
        self.assertNotIn(private, error.exception.message)
        self.assertTrue(all(not members for members in self.api.jobs.values()))
        evidence = next(self.request.evidence_dir.glob("*-phases.jsonl")).read_text()
        self.assertIn("private-detail", evidence)


if __name__ == "__main__":
    unittest.main()
