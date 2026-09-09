import io
import unittest
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from pbip_mcp.archive import ValidatedArchive
from pbip_mcp.fixture_variants import RICH_PAGES, WEIGHTED_MEASURE, expectations, rich_fixture_files
from pbip_mcp.windows_desktop import _metric_value, _Automation
from pbip_mcp.errors import DemoError


def zipped(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


class FixtureVariantTests(unittest.TestCase):
    def test_rich_exact_fixture_and_independent_arithmetic(self):
        project = ValidatedArchive(zipped(rich_fixture_files())).project
        self.assertTrue(project.synthetic_fixture)
        self.assertEqual(set(project.pages), set(RICH_PAGES))
        self.assertEqual(project.visual_count, 5)
        self.assertEqual(expectations()["expected_total"], 60)
        self.assertEqual(expectations()["expected_weighted"], 200)

    def test_changed_rich_m_is_not_refresh_allowlisted(self):
        files = rich_fixture_files()
        name = "Synthetic.SemanticModel/definition/tables/Product.tmdl"
        files[name] = files[name].replace(b'{"A", 2}', b'{"A", 999}')
        self.assertFalse(ValidatedArchive(zipped(files)).project.synthetic_fixture)

    def test_unicode_card_observation_is_not_an_expected_value_fallback(self):
        self.assertEqual(_metric_value([WEIGHTED_MEASURE + " 200.", "200", WEIGHTED_MEASURE], WEIGHTED_MEASURE), 200)
        self.assertEqual(_metric_value([WEIGHTED_MEASURE + " 999.", "999", WEIGHTED_MEASURE], WEIGHTED_MEASURE), 999)
        self.assertIsNone(_metric_value(["200", "Other Amount"], WEIGHTED_MEASURE))
        self.assertIsNone(_metric_value([WEIGHTED_MEASURE, "200", "999"], WEIGHTED_MEASURE))

    def test_rich_value_observation_survives_renamed_pointer_and_page_order(self):
        project = ValidatedArchive(zipped(rich_fixture_files())).project
        project = replace(project, pointer="report.pbip", pages=tuple(reversed(RICH_PAGES)))
        automation = SimpleNamespace(snapshot=Mock(side_effect=DemoError("UNIT_ENTERED", "Rich value observation was selected.")))
        with self.assertRaises(DemoError) as error:
            _Automation.observe_additional_fixture(automation, ("report",), project, 0)
        self.assertEqual(error.exception.code, "UNIT_ENTERED")
        automation.snapshot.assert_called_once()
