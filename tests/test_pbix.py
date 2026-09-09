import tempfile
import unittest
from pathlib import Path

from pbip_mcp.archive import ValidatedArchive
from pbip_mcp.errors import DemoError
from pbip_mcp.pbix import inspect_pbix

from .helpers import archive_bytes, fake_pbix


class PBIXStructureTests(unittest.TestCase):
    """These check rejection/metadata logic, never actual Desktop conversion."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "unit-fixture.pbix"
        self.project = ValidatedArchive(archive_bytes()).project

    def tearDown(self):
        self.temporary.cleanup()

    def test_reads_utf16_layout_metadata(self):
        self.path.write_bytes(fake_pbix())
        info = inspect_pbix(self.path, self.project)
        self.assertEqual(info["pages"], ["Synthetic overview"])
        self.assertEqual(info["visual_count"], 1)
        self.assertIn("not a substitute", info["inspection"])

    def test_rejects_renamed_input_zip(self):
        self.path.write_bytes(archive_bytes())
        with self.assertRaises(DemoError) as caught:
            inspect_pbix(self.path, self.project)
        self.assertEqual(caught.exception.code, "PBIX_MODEL_MISSING")

    def test_rejects_template_changed_pages_changed_visuals_and_corrupt_file(self):
        for data, code in (
            (fake_pbix(model=False), "PBIX_MODEL_MISSING"),
            (fake_pbix(["Wrong page"]), "PBIX_PAGES_CHANGED"),
            (fake_pbix(visuals=0), "PBIX_VISUALS_CHANGED"),
            (b"not a PBIX", "PBIX_INVALID"),
        ):
            with self.subTest(code=code):
                self.path.write_bytes(data)
                with self.assertRaises(DemoError) as caught:
                    inspect_pbix(self.path, self.project)
                self.assertEqual(caught.exception.code, code)
