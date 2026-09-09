"""Owned-process reverse orchestration doubles; never real GUI evidence."""

import json
from dataclasses import replace
from contextlib import nullcontext
from unittest.mock import patch

from pbip_mcp import windows_desktop as desktop
from pbip_mcp.errors import DemoError
from pbip_mcp.synthetic import fixture_files

from .test_windows_backend import WorkspaceCase, FakeNative, FakeConversionAutomation, READY
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

    def test_definitions_reopen_without_cache_and_without_data_claim(self):
        result = self.run_reverse("definitions")
        self.assertFalse(result.details["contains_data"])
        self.assertTrue(result.details["requires_data_reload"])
        self.assertFalse(self.automation_type.reopened_with_cache)
        self.assertTrue(self.automation_type.reopened_definitions)
        self.assertIsNone(result.details["synthetic_total_amount"])

    def test_missing_portable_cache_fails_and_closes_source_process(self):
        with self.assertRaises(DemoError) as error:
            self.run_reverse(cache=False)
        self.assertEqual(error.exception.code, "PORTABLE_CACHE_MISSING")
        self.assertTrue(all(not members for members in self.api.jobs.values()))
        self.assertEqual([event[0] for event in self.api.events].count("suspended"), 1)
