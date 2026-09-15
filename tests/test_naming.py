import hashlib
import tempfile
import unittest
from pathlib import Path

from pbip_mcp.archive import ValidatedArchive
from pbip_mcp.config import Config
from pbip_mcp.errors import DemoError
from pbip_mcp.inputs import artifact_filename, download_basename, folder_source_name
from pbip_mcp.storage import JobStore

from .helpers import archive_bytes


class FilenameTests(unittest.TestCase):
    def test_file_suffixes_are_replaced_once_and_folder_names_are_literal(self):
        cases = (
            ("Customer.Report.v2.PBIX", False, "Customer.Report.v2"),
            ("Customer.Report.PBIP.ZIP", False, "Customer.Report"),
            ("Customer.Report.zip", False, "Customer.Report"),
            ("客户 报表.v2.zip", False, "客户 报表.v2"),
            ("客户 工程.zip", True, "客户 工程.zip"),
            ("Customer.pbip.zip", True, "Customer.pbip.zip"),
            ("report.v2", True, "report.v2"),
            ("100%2F完成.zip", False, "100%2F完成"),
        )
        for name, folder, expected in cases:
            with self.subTest(name=name, folder=folder):
                base = download_basename(name, is_folder=folder)
                self.assertEqual(base, expected)
                for kind, suffix in (("pbix", ".pbix"), ("pbip", ".pbip.zip"),
                                     ("verification", ".verification.json")):
                    self.assertEqual(artifact_filename(base, kind), expected + suffix)

    def test_unsafe_and_unrepresentable_download_names_are_rejected(self):
        for name in ("", ".zip", ".pbip.zip", "../report.zip", r"..\report.zip", "NUL.zip", "con.pbix",
                     "COM1.report.zip", 'bad"name.zip', "header\r\ninjected.zip", "bad\x7fname.zip",
                     "bad\ud800name.zip", "x" * 181 + ".zip", "😀" * 119 + ".zip"):
            with self.subTest(name=repr(name)), self.assertRaises(DemoError) as error:
                download_basename(name)
            self.assertEqual(error.exception.code, "INPUT_NAME")

    def test_folder_name_uses_shared_upload_root_or_rootless_pointer(self):
        self.assertEqual(folder_source_name(
            ["客户 工程.v2/Report.pbip", "客户 工程.v2/Report.Report/definition.pbir"], "客户 工程.v2/Report.pbip"),
            "客户 工程.v2")
        self.assertEqual(folder_source_name(
            ["Customer.pbip", "Customer.Report/definition.pbir"], "Customer.pbip"), "Customer")
        self.assertEqual(folder_source_name(
            ["Project/Customer.pbip", "Reports/definition.pbir"], "Project/Customer.pbip"), "Customer")


class StoredFilenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name))
        self.store = JobStore(self.config)
        self.archive = ValidatedArchive(archive_bytes())

    def tearDown(self):
        self.temp.cleanup()

    def finish(self, job):
        claim = self.store.claim()
        self.assertEqual(claim["job_id"], job["job_id"])
        output = self.store.job_dir(job["job_id"]) / "output"
        output.mkdir()
        payload = b"UNIT ONLY: naming and download bytes, not a real Desktop result"
        (output / "report.pbix").write_bytes(payload)
        self.store.finish(job["job_id"], claim["lease"], artifacts=[{
            "kind": "pbix", "filename": "report.pbix", "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }])
        return output, payload

    def test_folder_name_survives_restart_without_becoming_a_storage_path(self):
        job = self.store.submit(self.archive, source_name="客户 工程.zip", source_is_folder=True)
        self.assertEqual(job["source"]["name"], "客户 工程.zip")
        output, payload = self.finish(job)
        reopened = JobStore(self.config)
        result = reopened.get(job["job_id"])
        self.assertEqual(result["artifacts"][0]["filename"], "客户 工程.zip.pbix")
        self.assertFalse((output / "客户 工程.zip.pbix").exists())
        with reopened.artifact_file(job["job_id"], "pbix") as (stream, info, _):
            self.assertEqual(stream.read(), payload)
            self.assertEqual(info["filename"], "客户 工程.zip.pbix")
        chunk = reopened.artifact_chunk(job["job_id"], "pbix", 0, 256)
        self.assertEqual(chunk["filename"], "客户 工程.zip.pbix")

    def test_legacy_migration_preserves_existing_fields_and_fixed_names(self):
        job = self.store.submit(self.archive, source_name="uploaded-project.zip")
        _, payload = self.finish(job)
        with self.store._connect() as db:
            db.execute("ALTER TABLE jobs DROP COLUMN artifact_basename")
            before = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone())
        reopened = JobStore(self.config)
        with reopened._connect() as db:
            after = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone())
        self.assertIsNone(after.pop("artifact_basename"))
        self.assertEqual(after, before)
        result = reopened.get(job["job_id"])
        self.assertEqual(result["source"]["name"], "uploaded-project.zip")
        self.assertEqual(result["artifacts"][0]["filename"], "report.pbix")
        with reopened.artifact_file(job["job_id"], "pbix") as (stream, info, _):
            self.assertEqual(stream.read(), payload)
            self.assertEqual(info["filename"], "report.pbix")

    def test_invalid_names_do_not_create_jobs_or_files(self):
        for name in ("NUL.zip", "bad\x7fname.zip", "bad\ud800name.zip"):
            with self.subTest(name=repr(name)), self.assertRaises(DemoError) as error:
                self.store.submit(self.archive, source_name=name)
            self.assertEqual(error.exception.code, "INPUT_NAME")
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(list(self.config.jobs_dir.iterdir()), [])
