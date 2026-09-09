"""Deterministic concurrency/protocol regressions; no Desktop or real data evidence."""

import base64
import hashlib
import io
import stat
import tempfile
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp import Client

from pbip_mcp import safe_paths
from pbip_mcp.archive import ValidatedArchive
from pbip_mcp.client import call, download, submit
from pbip_mcp.config import Config, Limits, MIB
from pbip_mcp.errors import DemoError
from pbip_mcp.identity import Principal
from pbip_mcp.server import create_server
from pbip_mcp.storage import JobStore

from .helpers import archive_bytes
from .test_bidirectional import input_pbix


class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root / "data", replace(Limits(), retention_seconds=1, chunk_bytes=8))
        self.store = JobStore(self.config)
        self.a = Principal("alice", "Alice")
        self.b = Principal("bob", "Bob")
        self.admin = Principal("operator", "Operator", "admin")
        self.archive = ValidatedArchive(archive_bytes())

    def tearDown(self):
        self.temp.cleanup()

    def artifact(self):
        job = self.store.submit(self.archive, principal=self.a)
        claim = self.store.claim()
        directory = self.store.job_dir(job["job_id"]) / "output"
        directory.mkdir()
        data = b"UNIT protocol bytes, not a real PBIX or GUI result"
        (directory / "report.pbix").write_bytes(data)
        self.store.finish(job["job_id"], claim["lease"], artifacts=[
            {"kind": "pbix", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        ])
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET finished=? WHERE id=?", (time.time() - 10, job["job_id"]))
        return job["job_id"], data

    def test_cross_chunk_lease_survives_maintenance_and_store_restart(self):
        job, data = self.artifact()
        token = self.store.begin_artifact_download(job, "pbix", principal=self.a)["download_id"]
        first = self.store.artifact_chunk(job, "pbix", 0, 8, principal=self.a, download_id=token)
        self.assertEqual(base64.b64decode(first["data_base64"]), data[:8])
        reopened = JobStore(self.config)
        self.assertEqual(reopened.cleanup_expired()["count"], 0)
        with reopened._connect() as db:
            db.execute("UPDATE artifact_transfers SET deadline=? WHERE token=?", (time.time() + 1, token))
        second = reopened.artifact_chunk(job, "pbix", 8, 8, principal=self.a, download_id=token)
        self.assertEqual(base64.b64decode(second["data_base64"]), data[8:16])
        with reopened._connect() as db:
            self.assertGreater(db.execute("SELECT deadline FROM artifact_transfers WHERE token=?", (token,)).fetchone()[0],
                               time.time() + 290)
        reopened.finish_artifact_download(job, "pbix", token, principal=self.a)
        self.assertEqual(reopened.cleanup_expired()["count"], 1)
        self.assertEqual(reopened.get(job, principal=self.a)["status"], "succeeded")

    def test_transfer_owner_kind_and_other_transfer_cannot_be_released(self):
        job, _ = self.artifact()
        first = self.store.begin_artifact_download(job, "pbix", principal=self.a)["download_id"]
        second = self.store.begin_artifact_download(job, "pbix", principal=self.a)["download_id"]
        for principal, kind in ((self.b, "pbix"), (self.admin, "pbix"), (self.a, "verification")):
            with self.subTest(principal=principal.id, kind=kind), self.assertRaises(DemoError):
                self.store.finish_artifact_download(job, kind, first, principal=principal)
        with self.assertRaises(DemoError):
            self.store.artifact_chunk(job, "pbix", 0, 8, principal=self.admin, download_id=first)
        self.store.finish_artifact_download(job, "pbix", first, principal=self.a)
        self.assertEqual(self.store.cleanup_expired()["count"], 0)
        self.store.finish_artifact_download(job, "pbix", second, principal=self.a)
        self.assertEqual(self.store.cleanup_expired()["count"], 1)

    def test_legacy_chunks_keep_conservative_lease_until_idle_expiry(self):
        job, _ = self.artifact()
        self.store.artifact_chunk(job, "pbix", 0, 8, principal=self.a)
        self.assertEqual(self.store.cleanup_expired()["count"], 0)
        self.store.artifact_chunk(job, "pbix", 8, 8, principal=self.a)
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0], 1)
            db.execute("UPDATE artifact_transfers SET deadline=0")
        self.assertEqual(self.store.cleanup_expired()["count"], 1)

    def test_expired_transfer_cannot_silently_resume(self):
        job, _ = self.artifact()
        token = self.store.begin_artifact_download(job, "pbix", principal=self.a)["download_id"]
        with self.store._connect() as db:
            db.execute("UPDATE artifact_transfers SET deadline=0")
        with self.assertRaises(DemoError) as error:
            self.store.artifact_chunk(job, "pbix", 0, 8, principal=self.a, download_id=token)
        self.assertEqual(error.exception.code, "DOWNLOAD_NOT_FOUND")
        self.assertEqual(self.store.cleanup_expired()["count"], 1)

    def test_invalid_read_releases_only_its_normal_transfer(self):
        job, _ = self.artifact()
        token = self.store.begin_artifact_download(job, "pbix", principal=self.a)["download_id"]
        with self.assertRaises(DemoError):
            self.store.artifact_chunk(job, "pbix", 999, 8, principal=self.a, download_id=token)
        self.assertEqual(self.store.cleanup_expired()["count"], 1)

    def test_abandoned_transfers_are_bounded(self):
        job, _ = self.artifact()
        with patch("pbip_mcp.storage._MAX_TRANSFERS", 2):
            for _ in range(2):
                self.store.begin_artifact_download(job, "pbix", principal=self.a)
            with self.assertRaises(DemoError) as error:
                self.store.begin_artifact_download(job, "pbix", principal=self.a)
        self.assertEqual(error.exception.code, "DOWNLOAD_LIMIT")
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM downloads").fetchone()[0], 0)
            db.execute("UPDATE artifact_transfers SET deadline=0")
        self.assertEqual(self.store.cleanup_expired()["count"], 1)

    async def test_client_renews_and_finishes_multichunk_transfer(self):
        job, data = self.artifact()

        async def interleaved(client, name, arguments=None):
            result = await call(client, name, arguments)
            if name == "get_artifact":
                self.assertEqual(self.store.cleanup_expired()["count"], 0)
            return result

        target = self.root / "download.pbix"
        async with Client(create_server(self.config, principal_provider=lambda: self.a), cache=None) as client:
            with patch("pbip_mcp.client.call", side_effect=interleaved):
                await download(client, job, target)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(self.store.cleanup_expired()["count"], 1)

    async def test_client_failure_releases_transfer_and_removes_partial_file(self):
        job, _ = self.artifact()

        async def corrupted(client, name, arguments=None):
            result = await call(client, name, arguments)
            if name == "get_artifact":
                result["artifact"]["sha256"] = "0" * 64
            return result

        target = self.root / "download.pbix"
        async with Client(create_server(self.config, principal_provider=lambda: self.a), cache=None) as client:
            with patch("pbip_mcp.client.call", side_effect=corrupted), self.assertRaises(DemoError):
                await download(client, job, target)
        self.assertFalse(target.exists())
        self.assertEqual(self.store.cleanup_expired()["count"], 1)

    async def test_client_works_with_legacy_download_capabilities(self):
        job, data = self.artifact()

        async def legacy(client, name, arguments=None):
            self.assertNotIn(name, ("begin_artifact_download", "finish_artifact_download"))
            result = await call(client, name, arguments)
            if name == "converter_status":
                result.pop("capabilities", None)
            return result

        target = self.root / "legacy.pbix"
        async with Client(create_server(self.config, principal_provider=lambda: self.a), cache=None) as client:
            with patch("pbip_mcp.client.call", side_effect=legacy):
                await download(client, job, target)
        self.assertEqual(target.read_bytes(), data)

    async def test_client_20mib_input_uses_raised_and_lowered_server_limits(self):
        buffer = io.BytesIO(input_pbix())
        with zipfile.ZipFile(buffer, "a", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("UnitPadding.bin", b"x" * (20 * MIB))
        path = self.root / "large.pbix"
        path.write_bytes(buffer.getvalue())
        digest = hashlib.sha256(buffer.getvalue()).hexdigest()
        advertised = {
            "archive_bytes": 32 * MIB, "uncompressed_bytes": 64 * MIB,
            "member_bytes": 32 * MIB, "metadata_bytes": 3 * MIB,
            "file_count": 100, "compression_ratio": 300, "path_characters": 200, "path_depth": 30,
        }

        async def response(client, name, arguments=None):
            if name == "converter_status":
                return {"limits": advertised}
            self.assertEqual(name, "submit_pbix")
            return {"job": {"source": {"sha256": digest}}}

        from pbip_mcp.inputs import validate_input
        with patch("pbip_mcp.client.call", side_effect=response), patch("pbip_mcp.inputs.validate_input", wraps=validate_input) as validate:
            await submit(object(), path)
            for key, value in advertised.items():
                self.assertEqual(getattr(validate.call_args.args[-1], key), value)
            advertised["member_bytes"] = MIB
            with self.assertRaises(DemoError):
                await submit(object(), path)
            advertised["member_bytes"] = 32 * MIB
            advertised["uncompressed_bytes"] = MIB
            with self.assertRaises(DemoError):
                await submit(object(), path)
            advertised["archive_bytes"] = 16 * MIB
            with self.assertRaises(DemoError) as error:
                await submit(object(), path)
            self.assertEqual(error.exception.code, "CLIENT_INPUT")

    async def test_legacy_input_limit_fields_fall_back_to_previous_defaults(self):
        path = self.root / "input.pbix"
        path.write_bytes(input_pbix())
        mock = AsyncMock(side_effect=[
            {"limits": {"archive_bytes": 16 * MIB}},
            {"job": {"source": {"sha256": hashlib.sha256(input_pbix()).hexdigest()}}},
        ])
        with patch("pbip_mcp.client.call", mock):
            await submit(object(), path)

    def test_submit_survives_finish_cleanup_between_enumeration_and_accounting(self):
        job = self.store.submit(self.archive, principal=self.a)
        claim = self.store.claim()
        directory = self.store.job_dir(job["job_id"])
        work = directory / "work"
        work.mkdir()
        victim = work / "ephemeral.bin"
        victim.write_bytes(b"unit ephemeral")
        original = safe_paths.tree_files
        removed = False

        def interleaved(root, *, missing_ok=False):
            nonlocal removed
            for path in original(root, missing_ok=missing_ok):
                if path == victim and not removed:
                    removed = True
                    safe_paths.remove_task_tree(work, directory)
                yield path

        with patch("pbip_mcp.safe_paths.tree_files", side_effect=interleaved):
            # Finish happens before submit obtains its transaction; cleanup races its accounting.
            self.store.finish(job["job_id"], claim["lease"], error=DemoError("UNIT_ONLY", "No Desktop."))
            queued = self.store.submit(self.archive, principal=self.a)
        self.assertTrue(removed)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(self.store.get(job["job_id"], principal=self.a)["status"], "failed")

    def test_accounting_does_not_swallow_permission_or_reparse_errors(self):
        root = self.config.jobs_dir
        victim = root / "unit.bin"
        victim.write_bytes(b"unit")
        original = Path.lstat
        for unsafe in ("permission", "reparse"):
            def inspect(path, *args, **kwargs):
                if path == victim:
                    if unsafe == "permission":
                        raise PermissionError("unit denied")
                    return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400, st_size=4)
                return original(path, *args, **kwargs)

            with self.subTest(unsafe=unsafe), patch.object(Path, "lstat", inspect):
                with self.assertRaises(PermissionError if unsafe == "permission" else DemoError):
                    self.store.submit(self.archive, principal=self.a)
