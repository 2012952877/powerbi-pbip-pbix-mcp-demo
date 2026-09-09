import base64
import hashlib
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from pbip_mcp.archive import ValidatedArchive
from pbip_mcp.config import Config, Limits
from pbip_mcp.errors import DemoError
from pbip_mcp.storage import JobStore

from .helpers import archive_bytes


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temporary.name))
        self.store = JobStore(self.config)
        self.archive = ValidatedArchive(archive_bytes())

    def tearDown(self):
        self.temporary.cleanup()

    def test_job_survives_new_store(self):
        job = self.store.submit(self.archive)
        self.assertEqual(job["status"], "queued")
        reopened = JobStore(self.config)
        self.assertEqual(reopened.get(job["job_id"]), job)
        directory = reopened.job_dir(job["job_id"])
        self.assertEqual((directory / "source.zip").read_bytes(), self.archive.data)
        self.assertTrue((directory / "original" / "Synthetic.pbip").is_file())
        self.assertNotIn(str(directory), str(job))
        self.assertFalse(self.store.worker_status()["ready"])

    def test_hash_error_and_bad_job_id(self):
        with self.assertRaises(DemoError) as caught:
            self.store.submit(self.archive, "0" * 64)
        self.assertEqual(caught.exception.code, "SOURCE_HASH_MISMATCH")
        for invalid in ("../other", "C:\\outside", "not-an-id"):
            with self.subTest(invalid=invalid), self.assertRaises(DemoError):
                self.store.get(invalid)
        with self.assertRaises(DemoError) as caught:
            self.store.get("0" * 32)
        self.assertEqual(caught.exception.code, "JOB_NOT_FOUND")

    def test_exclusive_claim_and_stale_lease(self):
        job = self.store.submit(self.archive)
        self.store.submit(self.archive)
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: self.store.claim(), range(4)))
        active = [claim for claim in claims if claim]
        self.assertEqual(len(active), 1)
        claimed = active[0]
        self.assertEqual(claimed["job_id"], job["job_id"])
        with self.assertRaises(DemoError) as caught:
            self.store.finish(job["job_id"], "wrong", error=DemoError("TEST", "Unit test failure."))
        self.assertEqual(caught.exception.code, "STALE_JOB_LEASE")
        self.store.finish(job["job_id"], claimed["lease"], error=DemoError("TEST", "Unit test failure."))
        self.assertEqual(self.store.get(job["job_id"])["status"], "failed")
        self.assertIsNotNone(self.store.claim())

    def test_cancel_only_queued(self):
        job = self.store.submit(self.archive)
        self.assertEqual(self.store.cancel(job["job_id"])["status"], "cancelled")
        self.assertIsNone(self.store.claim())
        running = self.store.submit(self.archive)
        self.store.claim()
        with self.assertRaises(DemoError) as caught:
            self.store.cancel(running["job_id"])
        self.assertEqual(caught.exception.code, "JOB_ALREADY_RUNNING")

    def test_recovery_never_requeues_or_fakes_success(self):
        job = self.store.submit(self.archive)
        self.store.claim()
        self.assertEqual(self.store.recover_interrupted(), 1)
        recovered = self.store.get(job["job_id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error"]["code"], "WORKER_INTERRUPTED")
        self.assertIsNone(self.store.claim())
        self.assertTrue((self.store.job_dir(job["job_id"]) / "source.zip").exists())

    def test_queue_expiration_without_worker(self):
        self.store = JobStore(Config(self.config.data_dir, replace(Limits(), queue_seconds=-1)))
        job = self.store.submit(self.archive)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "QUEUE_TIMEOUT")

    def test_expired_running_job_with_dead_worker_is_fenced(self):
        job = self.store.submit(self.archive)
        claim = self.store.claim()
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET started=? WHERE id=?", (time.time() - 1000, job["job_id"]))
        result = self.store.get(job["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "WORKER_TIMEOUT")
        with self.assertRaises(DemoError) as caught:
            self.store.finish(job["job_id"], claim["lease"], artifacts=[{"kind": "pbix"}])
        self.assertEqual(caught.exception.code, "STALE_JOB_LEASE")

    def test_controller_does_not_expire_a_fresh_worker_heartbeat(self):
        job = self.store.submit(self.archive)
        self.store.claim()
        self.store.heartbeat(state="busy", ready=True, reason="UNIT TEST", session_id=1)
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET started=? WHERE id=?", (time.time() - 1000, job["job_id"]))
        self.assertEqual(self.store.get(job["job_id"])["status"], "running")

    def test_queue_and_retained_quotas(self):
        self.store = JobStore(Config(self.config.data_dir, replace(Limits(), pending_jobs=1)))
        self.store.submit(self.archive)
        with self.assertRaises(DemoError) as caught:
            self.store.submit(self.archive)
        self.assertEqual(caught.exception.code, "QUEUE_FULL")
        self.store = JobStore(Config(self.config.data_dir, replace(Limits(), retained_bytes=1)))
        with self.assertRaises(DemoError) as caught:
            self.store.submit(self.archive)
        self.assertEqual(caught.exception.code, "STORAGE_QUOTA")

    def test_worker_readiness_is_finite(self):
        self.store.heartbeat(state="idle", ready=True, reason="Unit test heartbeat, not Desktop evidence.", session_id=1)
        self.assertTrue(self.store.worker_status()["ready"])
        with self.store._connect() as db:
            db.execute("UPDATE worker SET updated=?", (time.time() - 60,))
        state = self.store.worker_status()
        self.assertFalse(state["ready"])
        self.assertEqual(state["state"], "stale")

    def test_artifact_protocol_ranges_and_hash(self):
        job = self.store.submit(self.archive)
        with self.assertRaises(DemoError):
            self.store.artifact_chunk(job["job_id"], "pbix", 0, 10)
        claim = self.store.claim()
        output = self.store.job_dir(job["job_id"]) / "output"
        output.mkdir()
        data = b"unit test protocol bytes, NOT A REAL PBIX"
        (output / "report.pbix").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.store.finish(job["job_id"], claim["lease"], artifacts=[{"kind": "pbix", "bytes": len(data), "sha256": digest}])
        first = self.store.artifact_chunk(job["job_id"], "pbix", 0, 5)
        self.assertEqual(base64.b64decode(first["data_base64"]), data[:5])
        self.assertFalse(first["eof"])
        last = self.store.artifact_chunk(job["job_id"], "pbix", 5, 256)
        self.assertTrue(last["eof"])
        self.assertEqual(last["sha256"], digest)
        for kind, offset, length in (("../source.zip", 0, 10), ("pbix", -1, 10), ("pbix", 0, 0), ("pbix", 999, 1), ("pbix", 0, 999999)):
            with self.subTest(kind=kind, offset=offset, length=length), self.assertRaises(DemoError):
                self.store.artifact_chunk(job["job_id"], kind, offset, length)
        (output / "report.pbix").write_bytes(b"truncated")
        with self.assertRaises(DemoError) as caught:
            self.store.artifact_chunk(job["job_id"], "pbix", 0, 10)
        self.assertEqual(caught.exception.code, "ARTIFACT_INTEGRITY")
