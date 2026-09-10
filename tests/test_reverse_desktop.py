"""Owned-process reverse orchestration doubles; never real GUI evidence."""

import json
import unittest
from dataclasses import replace
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pbip_mcp import windows_desktop as desktop
from pbip_mcp.errors import DemoError
from pbip_mcp.synthetic import fixture_files

from .test_windows_backend import (
    WorkspaceCase, FakeNative, FakeConversionAutomation, FakeClock, FakeInfo, FakeWrapper, MissingPattern, READY,
)
from .test_bidirectional import input_pbix


class ReverseAutomation(FakeConversionAutomation):
    cache = True
    reopened_with_cache = None
    reopened_definitions = None

    def save_as(self, names, output, project):
        self.owned.api.events.append(("save_as_pbip", self.owned.pid))
        for name, data in fixture_files().items():
            path = output if name == "Synthetic.pbip" else output.parent.joinpath(*name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        pbi = output.parent / "Synthetic.SemanticModel" / ".pbi"
        pbi.mkdir()
        (pbi / "localSettings.json").write_text('{"unitOnly":"private"}')
        if self.cache:
            (pbi / "cache.abf").write_bytes(b"UNIT-ONLY cache")

    def observe_model(self, names, project, phase, *, require_card=False, definitions_only=False):
        if self.owned.document.suffix == ".pbip":
            type(self).reopened_with_cache = (self.owned.document.parent / "Synthetic.SemanticModel" / ".pbi" / "cache.abf").exists()
            type(self).reopened_definitions = definitions_only
            if (self.owned.document.parent / "Synthetic.SemanticModel" / ".pbi" / "localSettings.json").exists():
                raise AssertionError("Private settings leaked into reopening copy.")
        return super().observe_model(names, project, phase, require_card=require_card)

    def close_report(self, *, allow_discard=False, definitions_only=False):
        self.owned.api.events.append(("close_flags", self.owned.pid, allow_discard, definitions_only))
        return super().close_report(allow_discard=allow_discard)


class ReverseWorkflowTests(WorkspaceCase):
    def run_reverse(self, mode="portable", cache=True):
        source = self.workspace / "source.pbix"
        source.write_bytes(input_pbix())
        export = self.workspace / "export"
        export.mkdir()
        request = replace(self.request, project_file=source, output_file=export / "report.pbip",
                          project=replace(self.request.project, model_format="binary", pages=("Synthetic overview",),
                                          visual_count=1, synthetic_fixture=False),
                          direction="pbix_to_pbip", export_mode=mode)
        self.api = FakeNative()
        self.automation_type = type("ReverseUnitAutomation", (ReverseAutomation,), {"cache": cache})
        with (
            patch.object(desktop, "interactive_readiness", return_value=READY),
            patch.object(desktop, "desktop_session_mutex", return_value=nullcontext()),
            patch.object(desktop, "_load_uia", return_value=object()),
            patch.object(desktop, "_Native", return_value=self.api),
            patch.object(desktop, "_Automation", self.automation_type),
        ):
            result = desktop.WindowsDesktopConverter().convert(request)
        self.assertEqual(source.read_bytes(), input_pbix())
        return result

    def test_portable_exports_cache_and_reopens_independent_process(self):
        result = self.run_reverse()
        self.assertNotEqual(result.desktop_pid, result.reopened_pid)
        self.assertTrue(result.details["contains_data"])
        self.assertTrue(self.automation_type.reopened_with_cache)
        self.assertFalse(self.automation_type.reopened_definitions)
        self.assertNotIn("fixture_refresh", [event[0] for event in self.api.events])
        self.assertTrue(all(not members for members in self.api.jobs.values()))
        self.assertEqual([event[3] for event in self.api.events if event[0] == "close_flags"], [False, False])

    def test_definitions_reopen_without_cache_and_without_data_claim(self):
        result = self.run_reverse("definitions")
        self.assertFalse(result.details["contains_data"])
        self.assertTrue(result.details["requires_data_reload"])
        self.assertFalse(self.automation_type.reopened_with_cache)
        self.assertTrue(self.automation_type.reopened_definitions)
        self.assertIsNone(result.details["synthetic_total_amount"])
        self.assertEqual([event[3] for event in self.api.events if event[0] == "close_flags"], [False, True])

    def test_missing_portable_cache_fails_and_closes_source_process(self):
        with self.assertRaises(DemoError) as error:
            self.run_reverse(cache=False)
        self.assertEqual(error.exception.code, "PORTABLE_CACHE_MISSING")
        self.assertTrue(all(not members for members in self.api.jobs.values()))
        self.assertEqual([event[0] for event in self.api.events].count("suspended"), 1)


class ClosePromptNative(FakeNative):
    """Native/UI provider boundaries only; snapshot, ownership, classification and activation stay real."""

    def __init__(self, screens, clock, *, linger=False):
        super().__init__()
        self.screens, self.clock, self.linger = screens, clock, linger
        self.index = 0
        self.clicks = []

    def post_close(self, job, window):
        self.events.append(("graceful_close", job, window))
        if not self.screens:
            self.jobs[job].clear()

    def windows(self, _job, _deadline):
        return tuple(self.screens[self.index])

    def window_pid(self, handle):
        return self.screens[self.index][handle].process_id

    def pause(self, seconds):
        self.clock.sleep(seconds)
        if self.index + 1 < len(self.screens):
            self.index += 1
        elif not self.linger:
            for members in self.jobs.values():
                members.clear()

    def wrapper(self, info):
        wrapper = FakeWrapper(
            info, iface_window=SimpleNamespace(CurrentIsModal=info.control_type == "Window"),
            iface_invoke=SimpleNamespace(Invoke=lambda: self.clicks.append((info.unit_root, info.name))),
        )
        wrapper.is_enabled = lambda: info.unit_enabled
        return wrapper


class ClosePromptTests(unittest.TestCase):
    def dialog(self, root, *, label="Don't save", enabled=True,
               text="Do you want to save your changes?", pid=101, button_pid=101):
        children = [FakeInfo(text, "Text", pid=pid)]
        if label is not None:
            children.append(FakeInfo(label, "Button", pid=button_pid))
        prompt = FakeInfo("MessageDialog", "Window", pid=pid, handle=root, children=children)
        for info in [prompt, *children]:
            info.unit_root, info.unit_enabled = root, enabled
        return {root: prompt}

    def close(self, screens, *, definitions=True, allow=True, linger=False):
        clock = FakeClock()
        self.api = ClosePromptNative(screens, clock, linger=linger)
        self.records = []
        evidence = SimpleNamespace(record=lambda phase, details, *_: self.records.append((phase, details)))
        with patch.object(desktop.time, "monotonic", side_effect=clock.monotonic), \
             patch.object(desktop.time, "sleep", side_effect=self.api.pause), \
             patch.object(desktop, "interactive_readiness", return_value=READY):
            deadline = desktop._Deadline(60)
            with desktop._OwnedDesktop(self.api, Path("unit-not-executable"), Path("unit.pbip"), deadline) as owned:
                runtime = desktop._UIRuntime(
                    lambda handle: self.api.screens[self.api.index][handle], self.api.wrapper, MissingPattern,
                )
                ui = desktop._Automation(owned, deadline, runtime, evidence)
                ui.main_handle = 1010
                ui.close_report(allow_discard=allow, definitions_only=definitions)

    def test_definitions_closes_two_distinct_recognized_prompts_once_each(self):
        self.close([self.dialog(10), self.dialog(10), self.dialog(20), self.dialog(20), self.dialog(10)])
        self.assertEqual(self.api.clicks, [(10, "Don't save"), (20, "Don't save")])
        counts = [details["discard_count"] for phase, details in self.records if phase == "owned-close-discard"]
        self.assertEqual(counts, [1, 2])
        self.assertEqual(self.records[-1], ("owned-close-completed", {"pid": 101, "discard_count": 2}))
        self.assertNotIn("Do you want", json.dumps(self.records))

    def test_definitions_accepts_each_exact_enabled_discard_label(self):
        for label in ("Don't save", "Do not save", "Discard changes"):
            with self.subTest(label=label):
                self.close([self.dialog(10, label=label)])
                self.assertEqual(self.api.clicks, [(10, label)])

    def test_ordinary_and_portable_close_keep_one_distinct_prompt_limit(self):
        for screens, expected in (
            ([self.dialog(10), self.dialog(10)], None),
            ([self.dialog(10), self.dialog(20)], "DESKTOP_UNEXPECTED_SAVE_PROMPT"),
        ):
            with self.subTest(repeated_root=expected is None):
                if expected:
                    with self.assertRaises(DemoError) as error:
                        self.close(screens, definitions=False)
                    self.assertEqual(error.exception.code, expected)
                else:
                    self.close(screens, definitions=False)
                self.assertEqual(self.api.clicks, [(10, "Don't save")])

    def test_third_distinct_prompt_fails_without_a_third_click(self):
        with self.assertRaises(DemoError) as error:
            self.close([self.dialog(10), self.dialog(20), self.dialog(30)])
        self.assertEqual(error.exception.code, "DESKTOP_UNEXPECTED_SAVE_PROMPT")
        self.assertEqual(self.api.clicks, [(10, "Don't save"), (20, "Don't save")])

    def test_simultaneous_prompts_fail_before_any_click(self):
        with self.assertRaises(DemoError) as error:
            self.close([{**self.dialog(10), **self.dialog(20)}])
        self.assertEqual(error.exception.code, "DESKTOP_UNEXPECTED_SAVE_PROMPT")
        self.assertEqual(self.api.clicks, [])

    def test_unknown_missing_disabled_and_nonexact_controls_fail_closed(self):
        for options, code in (
            ({"text": "Unknown warning"}, "DESKTOP_DIALOG_UNSUPPORTED"),
            ({"label": None}, "DESKTOP_CLOSE_FAILED"),
            ({"enabled": False}, "DESKTOP_CLOSE_FAILED"),
            ({"label": "Discard changes and refresh"}, "DESKTOP_CLOSE_FAILED"),
            ({"text": "Privacy levels require permission"}, "DESKTOP_SECURITY_PROMPT"),
        ):
            with self.subTest(options=options), self.assertRaises(DemoError) as error:
                self.close([self.dialog(10, **options)])
            self.assertEqual(error.exception.code, code)
            self.assertEqual(self.api.clicks, [])

    def test_foreign_prompt_or_button_pid_is_rejected_by_real_snapshot(self):
        for changes in ({"pid": 999}, {"button_pid": 999}):
            with self.subTest(changes=changes), self.assertRaises(DemoError) as error:
                self.close([self.dialog(10, **changes)])
            self.assertEqual(error.exception.code, "DESKTOP_OWNERSHIP_LOST")
            self.assertEqual(self.api.clicks, [])

    def test_definitions_flag_without_discard_permission_does_not_allow_a_prompt(self):
        with self.assertRaises(DemoError) as error:
            self.close([self.dialog(10)], allow=False)
        self.assertEqual(error.exception.code, "DESKTOP_UNEXPECTED_SAVE_PROMPT")
        self.assertEqual(self.api.clicks, [])

    def test_same_root_propagation_does_not_repeat_click_and_still_has_a_deadline(self):
        with self.assertRaises(DemoError) as error:
            self.close([self.dialog(10)], linger=True)
        self.assertEqual(error.exception.code, "DESKTOP_CLOSE_FAILED")
        self.assertEqual(self.api.clicks, [(10, "Don't save")])
