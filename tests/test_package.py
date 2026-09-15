import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pbip_mcp import __version__
from pbip_mcp.synthetic import fixture_files


SOURCE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("package_builder_unit", SOURCE / "scripts" / "build_package.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class PackageTests(unittest.TestCase):
    def test_real_allowlisted_package_is_reproducible_and_every_entry_matches_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.zip"
            second = Path(directory) / "second.zip"
            result = BUILDER.build_package(first)
            BUILDER.build_package(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(result["version"], __version__)
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), result["sha256"])
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["entry_count"], result["files"])
            self.assertEqual(manifest["bytes"], first.stat().st_size)
            self.assertEqual(manifest["sha256"], result["sha256"])
            with zipfile.ZipFile(first) as package:
                self.assertEqual(package.namelist(), [record["path"] for record in manifest["entries"]])
                for record in manifest["entries"]:
                    content = package.read(record["path"])
                    self.assertEqual(len(content), record["bytes"])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), record["sha256"])
                    path = Path(record["path"])
                    self.assertTrue(path.parts[0] in {"src", "scripts", "tests", "sample", "deploy"}
                                    or record["path"] in {"pyproject.toml", "requirements.lock", "README.zh-CN.md"})
                    self.assertFalse({".venv", "__pycache__", "jobs", "runtime", "authdb", "build"} & set(path.parts))
                self.assertIn("tests/worker_script_harness.ps1", package.namelist())
                for name in ("test_portal_ui.py", "portal_ui_harness.js"):
                    self.assertEqual(package.read("tests/" + name), (SOURCE / "tests" / name).read_bytes())
                self.assertEqual(package.read("src/pbip_mcp/windows_desktop.py"),
                                 (SOURCE / "src" / "pbip_mcp" / "windows_desktop.py").read_bytes())
                for name, content in fixture_files().items():
                    self.assertEqual(package.read("sample/Synthetic/" + name), content)

    def test_release_or_manifest_collision_never_overwrites_or_creates_another_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "release.zip"
            manifest = output.with_suffix(".manifest.json")
            manifest.write_text("existing manifest", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                BUILDER.build_package(output)
            self.assertFalse(output.exists())
            self.assertEqual(manifest.read_text(), "existing manifest")
            manifest.unlink()
            output.write_bytes(b"existing release")
            with self.assertRaises(FileExistsError):
                BUILDER.build_package(output)
            self.assertFalse(manifest.exists())
            self.assertEqual(output.read_bytes(), b"existing release")

    def test_stale_runtime_version_is_rejected_before_release_files_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "release.zip"
            with patch.object(BUILDER, "__version__", "0.0.0"), self.assertRaises(ValueError):
                BUILDER.build_package(output)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".manifest.json").exists())
