import base64
import io
import json
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path

from pbip_mcp.archive import ValidatedArchive, decode_archive
from pbip_mcp.config import Limits
from pbip_mcp.errors import DemoError
from pbip_mcp.synthetic import fixture_files

from .helpers import archive_bytes, change_json


class ArchiveTests(unittest.TestCase):
    def assert_rejected(self, data: bytes, code: str, limits: Limits = Limits()) -> None:
        with self.assertRaises(DemoError) as caught:
            ValidatedArchive(data, limits)
        self.assertEqual(caught.exception.code, code)

    def test_complete_synthetic_project(self):
        validated = ValidatedArchive(archive_bytes())
        self.assertTrue(validated.project.synthetic_fixture)
        self.assertEqual(validated.project.pages, ("Synthetic overview",))
        self.assertEqual(validated.project.visual_count, 1)
        self.assertEqual(validated.project.model_format, "tmdl")
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "unpacked"
            validated.extract(target)
            for name, data in fixture_files().items():
                self.assertEqual(target.joinpath(*name.split("/")).read_bytes(), data)
            with self.assertRaises(DemoError):
                validated.extract(target)

    def test_nested_root_is_allowed(self):
        data = archive_bytes({"wrapper/" + name: value for name, value in fixture_files().items()})
        validated = ValidatedArchive(data)
        self.assertTrue(validated.project.synthetic_fixture)
        self.assertEqual(validated.project.pointer, "wrapper/Synthetic.pbip")

    def test_fixture_uses_native_report_version_and_top_level_tmdl_reference(self):
        files = fixture_files()
        version = json.loads(files["Synthetic.Report/definition/version.json"])
        self.assertEqual(version["version"], "2.0.0")
        model = files["Synthetic.SemanticModel/definition/model.tmdl"].decode("utf-8")
        self.assertIn("\nref table Sales\n", model)
        self.assertNotIn("\n\tref table Sales", model)

    def test_base64_input(self):
        value = archive_bytes()
        self.assertEqual(decode_archive(base64.b64encode(value).decode(), Limits()), value)
        for invalid in ("", "not base64 !", "a" * 50):
            with self.subTest(invalid=invalid), self.assertRaises(DemoError):
                decode_archive(invalid, replace(Limits(), archive_bytes=8))
        with self.assertRaises(DemoError) as caught:
            decode_archive("!", Limits())
        self.assertEqual(caught.exception.code, "INVALID_BASE64")

    def test_rejects_non_zip_and_empty_zip(self):
        self.assert_rejected(b"not a zip", "INVALID_ZIP")
        self.assert_rejected(archive_bytes({}), "ARCHIVE_FILE_COUNT")
        self.assert_rejected(b"MZ" + archive_bytes(), "INVALID_ZIP")

    def test_rejects_unsafe_windows_names(self):
        names = [
            "../bad", "a/../../bad", "/root", "C:/bad", "C:bad", "\\\\host\\share\\bad",
            "a\\bad", "a/./bad", "a//bad", "NUL.txt", "aux", "COM1.json", "LPT9",
            "a./bad", "a /bad", "a/file:stream", "a/<bad>", "a/que?st", "a/sta*r",
        ]
        for name in names:
            with self.subTest(name=name):
                files = fixture_files()
                files[name] = b"bad"
                data = archive_bytes(files)
                if name == "a\\bad":
                    # ZipInfo normalizes backslashes on Windows; construct the raw hostile member.
                    data = data.replace(b"a/bad", b"a\\bad")
                self.assert_rejected(data, "UNSAFE_ARCHIVE_PATH")

    def test_links_and_reparse_points_are_rejected(self):
        for attributes in ((stat.S_IFLNK | 0o777) << 16, (stat.S_IFIFO | 0o644) << 16, 0x400):
            with self.subTest(attributes=attributes):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w") as archive:
                    member = zipfile.ZipInfo("linked")
                    member.create_system = 3
                    member.external_attr = attributes
                    archive.writestr(member, b"elsewhere")
                self.assert_rejected(stream.getvalue(), "ARCHIVE_LINK")

    def test_case_collision_and_parent_file_are_rejected(self):
        for additions in (
            {"SYNTHETIC.PBIP": b"duplicate"},
            {"Synthetic.Report": b"parent file"},
            {"same.txt": b"a", "SAME.TXT": b"b"},
            {"caf\u00e9.txt": b"a", "cafe\u0301.txt": b"b"},
        ):
            with self.subTest(additions=additions):
                self.assert_rejected(archive_bytes({**fixture_files(), **additions}), "DUPLICATE_ARCHIVE_PATH")

    def test_exact_duplicate_is_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive, warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("dup", "one")
            archive.writestr("dup", "two")
        self.assert_rejected(stream.getvalue(), "DUPLICATE_ARCHIVE_PATH")

    def test_size_ratio_count_and_depth_limits(self):
        data = archive_bytes()
        for limits, code in (
            (replace(Limits(), archive_bytes=10), "ARCHIVE_SIZE"),
            (replace(Limits(), member_bytes=10), "ARCHIVE_MEMBER_SIZE"),
            (replace(Limits(), uncompressed_bytes=500), "ARCHIVE_EXPANDED_SIZE"),
            (replace(Limits(), file_count=2), "ARCHIVE_FILE_COUNT"),
            (replace(Limits(), path_depth=2), "UNSAFE_ARCHIVE_PATH"),
            (replace(Limits(), path_characters=5), "UNSAFE_ARCHIVE_PATH"),
        ):
            with self.subTest(code=code):
                self.assert_rejected(data, code, limits)
        self.assert_rejected(archive_bytes({"bomb": b"0" * 100_000}), "ZIP_BOMB")

    def test_unsupported_compression(self):
        self.assert_rejected(archive_bytes(compression=zipfile.ZIP_BZIP2), "ZIP_COMPRESSION")

    def test_pointer_only_and_multiple_pointers(self):
        files = fixture_files()
        self.assert_rejected(archive_bytes({"Synthetic.pbip": files["Synthetic.pbip"]}), "INCOMPLETE_PROJECT")
        self.assert_rejected(archive_bytes({**files, "second.pbip": files["Synthetic.pbip"]}), "AMBIGUOUS_PROJECT")
        del files["Synthetic.pbip"]
        self.assert_rejected(archive_bytes(files), "AMBIGUOUS_PROJECT")

    def test_ambiguous_report_and_model(self):
        files = fixture_files()
        change_json(files, "Synthetic.pbip", lambda d: d["artifacts"].append(d["artifacts"][0]))
        self.assert_rejected(archive_bytes(files), "AMBIGUOUS_PROJECT")
        files = fixture_files()
        files["Synthetic.SemanticModel/model.bim"] = b"{}"
        self.assert_rejected(archive_bytes(files), "INCOMPLETE_PROJECT")
        files = fixture_files()
        files["Synthetic.Report/report.json"] = b"{}"
        self.assert_rejected(archive_bytes(files), "INCOMPLETE_PROJECT")

    def test_unsafe_and_remote_references(self):
        for path in ("../../outside", "/root/model", "C:/model", "\\\\server\\share"):
            with self.subTest(path=path):
                files = fixture_files()
                change_json(files, "Synthetic.Report/definition.pbir", lambda d: d["datasetReference"]["byPath"].update(path=path))
                self.assert_rejected(archive_bytes(files), "UNSAFE_PROJECT_REFERENCE")
        files = fixture_files()
        change_json(files, "Synthetic.Report/definition.pbir", lambda d: d.update(datasetReference={"byConnection": {"connectionString": "not a credential"}}))
        self.assert_rejected(archive_bytes(files), "REMOTE_MODEL_UNSUPPORTED")

    def test_missing_model_page_visual_and_pending_changes(self):
        for suffix in ("/definition.pbism", "/definition/database.tmdl", "/tables/Sales.tmdl", "/page.json", "/visual.json"):
            with self.subTest(suffix=suffix):
                files = {name: data for name, data in fixture_files().items() if not name.endswith(suffix)}
                self.assert_rejected(archive_bytes(files), "INCOMPLETE_PROJECT")
        files = fixture_files()
        files["Synthetic.SemanticModel/.pbi/unappliedChanges.json"] = b"{}"
        self.assert_rejected(archive_bytes(files), "PENDING_MODEL_CHANGES")

    def test_invalid_duplicate_key_and_non_object_json(self):
        for data in (b"{broken", b"[]", b'{"artifacts":[],"artifacts":[]}'):
            with self.subTest(data=data):
                files = fixture_files()
                files["Synthetic.pbip"] = data
                self.assert_rejected(archive_bytes(files), "INVALID_PROJECT_JSON")

    def test_fixture_flag_is_not_caller_controlled(self):
        files = fixture_files()
        files["extra.json"] = b'{"synthetic_fixture":true}'
        self.assertFalse(ValidatedArchive(archive_bytes(files)).project.synthetic_fixture)
        files = fixture_files()
        name = "Synthetic.SemanticModel/definition/tables/Sales.tmdl"
        files[name] = files[name].replace(b'{"A", 10}', b'{"A", 999}')
        self.assertFalse(ValidatedArchive(archive_bytes(files)).project.synthetic_fixture)

    def test_crc_failure_is_an_input_error(self):
        data = bytearray(archive_bytes(compression=zipfile.ZIP_STORED))
        offset = data.index(b'"enableAutoRecovery"')
        data[offset] = ord("X")
        self.assert_rejected(bytes(data), "INVALID_ZIP")

    def test_encrypted_flag_and_invalid_utf8_name(self):
        data = bytearray(archive_bytes())
        local = data.index(b"PK\x03\x04")
        central = data.index(b"PK\x01\x02")
        for offset in (local + 6, central + 8):
            flags = struct.unpack_from("<H", data, offset)[0]
            struct.pack_into("<H", data, offset, flags | 1)
        self.assert_rejected(bytes(data), "ENCRYPTED_ZIP")
        data = archive_bytes({**fixture_files(), "bad\u00e9": b"data"})
        data = data.replace("bad\u00e9".encode("utf-8"), b"bad\xff\xa9")
        self.assert_rejected(data, "INVALID_ZIP")
