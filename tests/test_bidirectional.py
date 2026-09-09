"""Input, export and queue tests; synthetic containers are NOT Desktop evidence."""

import hashlib
import io
import json
import tempfile
import time
import unittest
import zipfile
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pbip_mcp.archive import ValidatedArchive
from pbip_mcp.config import Config, Limits
from pbip_mcp.errors import DemoError
from pbip_mcp.identity import Principal, LOCAL_OPERATOR
from pbip_mcp.inputs import ValidatedPBIX, validate_input
from pbip_mcp.project_export import package_project
from pbip_mcp.storage import JobStore
from pbip_mcp.synthetic import fixture_files
from pbip_mcp.worker import Worker

from .helpers import archive_bytes, fake_pbix


def input_pbix():
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(fake_pbix())) as source, zipfile.ZipFile(buffer, "w") as target:
        for info in source.infolist():
            target.writestr(zipfile.ZipInfo(info.filename), source.read(info))
        for name, content in {"Version": b"1.0", "[Content_Types].xml": b"<Types/>"}.items():
            if name not in source.namelist():
                target.writestr(zipfile.ZipInfo(name), content)
    return buffer.getvalue()


class BidirectionalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = JobStore(Config(self.root / "data"))
        self.a, self.b = Principal("a", "A"), Principal("b", "B")

    def tearDown(self):
        self.temp.cleanup()

    def test_input_directions_and_malformed_pbix(self):
        validated, direction = validate_input(input_pbix(), "example.pbix")
        self.assertEqual(direction, "pbix_to_pbip")
        self.assertEqual(validated.project.pages, ("Synthetic overview",))
        for data, name, direction, mode in (
            (b"bad", "x.pbix", "auto", None),
            (archive_bytes(), "x.pbix", "auto", None),
            (archive_bytes(), "x.pbip", "auto", None),
            (archive_bytes(), "x.zip", "pbix_to_pbip", None),
            (archive_bytes(), "x.zip", "auto", "portable"),
        ):
            with self.subTest(name=name, direction=direction), self.assertRaises(DemoError):
                validate_input(data, name, direction, mode)

    def test_mixed_queue_ownership_and_new_layout(self):
        forward = self.store.submit(ValidatedArchive(archive_bytes()), principal=self.a)
        reverse = self.store.submit(ValidatedPBIX(input_pbix()), principal=self.b,
                                    direction="pbix_to_pbip", source_name="b.pbix", export_mode="portable")
        self.assertNotEqual(self.store.job_dir(forward["job_id"]).parent, self.store.job_dir(reverse["job_id"]).parent)
        self.assertEqual(self.store.list_jobs(principal=self.a), [forward])
        for operation in (
            lambda: self.store.get(reverse["job_id"], principal=self.a),
            lambda: self.store.cancel(reverse["job_id"], principal=self.a),
            lambda: self.store.artifact_chunk(reverse["job_id"], "pbip", 0, 10, principal=self.a),
        ):
            with self.assertRaises(DemoError) as error:
                operation()
            self.assertEqual(error.exception.code, "JOB_NOT_FOUND")
        first = self.store.claim()
        self.assertEqual(first["direction"], "pbip_to_pbix")
        self.assertIsNone(self.store.claim())
        self.store.finish(first["job_id"], first["lease"], error=DemoError("UNIT_ONLY", "Not Desktop."))
        second = self.store.claim()
        self.assertEqual(second["direction"], "pbix_to_pbip")
        self.assertEqual(second["export_mode"], "portable")

    def test_export_cache_whitelist_and_reopen_copy(self):
        root = self.root / "export"
        ValidatedArchive(archive_bytes()).extract(root)
        pbi = root / "Synthetic.SemanticModel" / ".pbi"
        pbi.mkdir()
        (pbi / "cache.abf").write_bytes(b"unit-only cached data")
        (pbi / "localSettings.json").write_text('{"unit_only":"private settings"}')
        (root / "private.clixml").write_bytes(b"unit-private")
        other = root / "other-job"
        other.mkdir()
        (other / "cache.abf").write_bytes(b"not ours")
        definitions = package_project(root, "definitions")
        portable = package_project(root, "portable")
        for archive in (definitions, portable):
            with zipfile.ZipFile(io.BytesIO(archive.data)) as content:
                self.assertFalse(any("localsettings" in name.lower() or "private" in name or "other-job" in name for name in content.namelist()))
        with zipfile.ZipFile(io.BytesIO(definitions.data)) as content:
            self.assertFalse(any(name.endswith("cache.abf") for name in content.namelist()))
        with zipfile.ZipFile(io.BytesIO(portable.data)) as content:
            self.assertEqual(content.read("Synthetic.SemanticModel/.pbi/cache.abf"), b"unit-only cached data")
        self.assertEqual(portable.project.pages, definitions.project.pages)
        (pbi / "cache.abf").unlink()
        with self.assertRaises(DemoError) as error:
            package_project(root, "portable")
        self.assertEqual(error.exception.code, "PORTABLE_CACHE_MISSING")

    def test_retention_keeps_success_history_and_live_download(self):
        store = JobStore(Config(self.root / "retention", replace(Limits(), retention_seconds=1)))
        job = store.submit(ValidatedArchive(archive_bytes()), principal=self.a)
        claim = store.claim()
        output = store.job_dir(job["job_id"]) / "output"
        output.mkdir()
        data = b"unit-only protocol data"
        (output / "report.pbix").write_bytes(data)
        store.finish(job["job_id"], claim["lease"], artifacts=[
            {"kind": "pbix", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        ])
        with store._connect() as db:
            db.execute("UPDATE jobs SET finished=? WHERE id=?", (time.time() - 10, job["job_id"]))
        with store.artifact_file(job["job_id"], "pbix", principal=self.a) as (stream, _, _):
            self.assertEqual(store.cleanup_expired()["count"], 0)
            self.assertEqual(stream.read(), data)
        self.assertEqual(store.cleanup_expired()["count"], 1)
        expired = store.get(job["job_id"], principal=self.a)
        self.assertEqual(expired["status"], "succeeded")
        self.assertEqual(expired["artifact_status"], "expired")
        with self.assertRaises(DemoError) as error:
            store.artifact_chunk(job["job_id"], "pbix", 0, 10, principal=self.a)
        self.assertEqual(error.exception.code, "ARTIFACT_EXPIRED")
        self.assertEqual(store.cleanup_expired()["count"], 0)

    def test_legacy_rows_never_purged_and_hidden_from_portal(self):
        job = self.store.submit(ValidatedArchive(archive_bytes()))
        self.store.cancel(job["job_id"])
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET storage_key=NULL,retention_seconds=NULL,finished=0 WHERE id=?", (job["job_id"],))
        self.assertEqual(JobStore(self.store.config).cleanup_expired()["count"], 0)
        with self.assertRaises(DemoError):
            self.store.get(job["job_id"], principal=self.a)

    def test_actual_old_database_migrates_without_moving_originals(self):
        root = self.root / "old"
        root.mkdir()
        job_id = "a" * 32
        directory = root / "jobs" / job_id
        directory.mkdir(parents=True)
        original = directory / "source.zip"
        original.write_bytes(archive_bytes())
        db = sqlite3.connect(root / "queue.sqlite3")
        try:
            db.execute("""CREATE TABLE jobs(
                id TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL,updated REAL,expires REAL,
                started REAL,finished REAL,lease TEXT,source_sha256 TEXT,source_bytes INTEGER,
                reserved_bytes INTEGER,project_json TEXT,error_code TEXT,error_message TEXT,artifact_json TEXT
            )""")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                job_id, "failed", 1, 2, 3, 1, 2, "old-lease", hashlib.sha256(original.read_bytes()).hexdigest(),
                original.stat().st_size, 10, json.dumps(ValidatedArchive(original.read_bytes()).project.as_dict()),
                "OLD_FAILURE", "Preserved prior failure.", None,
            ))
            db.commit()
        finally:
            db.close()
        migrated = JobStore(Config(root))
        self.assertEqual(migrated.job_dir(job_id), directory)
        self.assertEqual(migrated.get(job_id)["error"]["code"], "OLD_FAILURE")
        self.assertEqual(migrated.cleanup_expired()["count"], 0)
        self.assertTrue(original.exists())
        with self.assertRaises(DemoError):
            migrated.get(job_id, principal=self.a)

    def test_user_queue_rate_and_byte_limits(self):
        limits = replace(Limits(), user_pending_jobs=1)
        store = JobStore(Config(self.root / "quota", limits))
        store.submit(ValidatedArchive(archive_bytes()), principal=self.a)
        with self.assertRaises(DemoError) as error:
            store.submit(ValidatedArchive(archive_bytes()), principal=self.a)
        self.assertEqual(error.exception.code, "USER_QUEUE_FULL")
        store.submit(ValidatedArchive(archive_bytes()), principal=self.b)
        store = JobStore(Config(self.root / "bytes", replace(Limits(), user_retained_bytes=1)))
        with self.assertRaises(DemoError) as error:
            store.submit(ValidatedArchive(archive_bytes()), principal=self.a)
        self.assertEqual(error.exception.code, "USER_STORAGE_QUOTA")
        store = JobStore(Config(self.root / "rate", replace(Limits(), global_submissions_per_hour=1)))
        store.submit(ValidatedArchive(archive_bytes()), principal=self.a)
        with self.assertRaises(DemoError) as error:
            store.submit(ValidatedArchive(archive_bytes()), principal=self.b)
        self.assertEqual(error.exception.code, "SUBMISSION_RATE")

    def test_reverse_failed_worker_cleans_owned_work_and_keeps_original(self):
        worker = Worker(self.store.config, self.root / "not-launched.exe")
        job = self.store.submit(ValidatedPBIX(input_pbix()), principal=self.a, direction="pbix_to_pbip")
        claim = self.store.claim()
        with patch.object(worker, "_supervise", side_effect=DemoError("UNIT_FAILURE", "No GUI started.")):
            worker.execute(claim)
        directory = self.store.job_dir(job["job_id"])
        self.assertTrue((directory / "source.pbix").exists())
        self.assertFalse((directory / "work").exists())
        self.assertFalse((directory / "export").exists())
        self.assertEqual(self.store.get(job["job_id"])["error"]["code"], "UNIT_FAILURE")

    def test_reverse_worker_packages_only_verified_unit_result(self):
        worker = Worker(self.store.config, self.root / "not-launched.exe")
        job = self.store.submit(ValidatedPBIX(input_pbix()), principal=self.a, direction="pbix_to_pbip",
                                export_mode="portable")
        claim = self.store.claim()

        def unit_result(_claim, request, _request_path, _target):
            export = Path(request["output_file"]).parent
            for name, data in fixture_files().items():
                path = export.joinpath(*name.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            cache = export / "Synthetic.SemanticModel" / ".pbi" / "cache.abf"
            cache.parent.mkdir()
            cache.write_bytes(b"UNIT ONLY CACHE; NOT REAL GUI")
            archive = package_project(export, "portable")
            return {
                "desktop_pid": 123, "reopened_pid": 456, "model_present": True,
                "pages": ["Synthetic overview"], "details": {
                    "verification_method": "UNIT DOUBLE ONLY, NOT DESKTOP",
                    "export_sha256": hashlib.sha256(archive.data).hexdigest(),
                },
            }

        with patch.object(worker, "_supervise", side_effect=unit_result):
            worker.execute(claim)
        result = self.store.get(job["job_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual({item["kind"] for item in result["artifacts"]}, {"pbip", "verification"})
        self.assertTrue(result["contains_data"])
        self.assertFalse((self.store.job_dir(job["job_id"]) / "export").exists())
        self.assertEqual((self.store.job_dir(job["job_id"]) / "source.pbix").read_bytes(), input_pbix())

    def test_non_fixture_definitions_never_refresh_or_claim_data(self):
        files = fixture_files()
        name = "Synthetic.SemanticModel/definition/tables/Sales.tmdl"
        files[name] = files[name].replace(b'{"A", 10}', b'{"A", 999}')
        worker = Worker(self.store.config, self.root / "not-launched.exe")
        job = self.store.submit(ValidatedArchive(archive_bytes(files)))
        with patch.object(worker, "_supervise") as supervise:
            worker.execute(self.store.claim())
        supervise.assert_not_called()
        result = self.store.get(job["job_id"])
        self.assertEqual(result["error"]["code"], "DATA_CACHE_REQUIRED")
        self.assertIsNone(result["contains_data"])
