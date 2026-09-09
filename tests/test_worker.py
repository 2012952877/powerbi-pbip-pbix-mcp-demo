import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pbip_mcp.archive import ValidatedArchive
from pbip_mcp.config import Config
from pbip_mcp.errors import DemoError
from pbip_mcp.worker import QueueLock, Worker, validate_conversion_evidence

from .helpers import archive_bytes


class WorkerQueueUnitTests(unittest.TestCase):
    """Only orchestration tests. All desktop readiness in this class is mocked."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worker = Worker(Config(self.root), self.root / "not-launched-PBIDesktop.exe")
        self.archive = ValidatedArchive(archive_bytes())

    def tearDown(self):
        self.temporary.cleanup()

    def test_queue_lock_rejects_second_worker_and_releases(self):
        with QueueLock(self.root):
            with self.assertRaises(DemoError) as caught:
                with QueueLock(self.root):
                    self.fail("A second lock must not succeed")
            self.assertEqual(caught.exception.code, "WORKER_ALREADY_RUNNING")
        with QueueLock(self.root):
            pass

    def test_no_worker_claim_when_readiness_is_false(self):
        job = self.worker.store.submit(self.archive)
        not_ready = {"ready": False, "code": "SESSION_ZERO", "message": "Unit-test session zero.", "session_id": 0}
        with patch("pbip_mcp.worker.readiness", return_value=not_ready), patch.object(self.worker, "_convert") as converter:
            self.assertEqual(self.worker.run(max_jobs=1), 2)
        converter.assert_not_called()
        self.assertEqual(self.worker.store.get(job["job_id"])["status"], "queued")
        self.assertFalse(self.worker.store.worker_status()["ready"])
        self.assertEqual(self.worker.store.worker_status()["message"], "Unit-test session zero.")

    def test_graceful_stop_preserves_queued_input_without_launching_desktop(self):
        job = self.worker.store.submit(self.archive)
        (self.root / "worker.stop").write_text("unit stop", encoding="utf-8")
        with patch.object(self.worker, "_convert") as converter:
            self.assertEqual(self.worker.run(), 0)
        converter.assert_not_called()
        self.assertEqual(self.worker.store.get(job["job_id"])["status"], "queued")
        self.assertTrue((self.worker.store.job_dir(job["job_id"]) / "source.zip").is_file())

    def test_converter_error_becomes_durable_failure_not_mock_success(self):
        job = self.worker.store.submit(self.archive)
        ready = {"ready": True, "code": None, "message": "UNIT TEST ONLY", "session_id": 1}
        with patch("pbip_mcp.worker.readiness", return_value=ready), patch.object(
            self.worker, "_convert", side_effect=DemoError("UNIT_ONLY_FAILURE", "No actual Desktop launched.")
        ):
            self.worker.run(max_jobs=1)
        result = self.worker.store.get(job["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "UNIT_ONLY_FAILURE")
        self.assertEqual(result["artifacts"], [])
        self.assertTrue((self.worker.store.job_dir(job["job_id"]) / "source.zip").is_file())

    def test_reopen_evidence_rejects_single_pid_no_model_and_wrong_pages(self):
        valid_shape = {"desktop_pid": 100, "reopened_pid": 101, "model_present": True, "pages": ["Synthetic overview"]}
        for change in (
            {"reopened_pid": 100}, {"desktop_pid": 0}, {"desktop_pid": True},
            {"model_present": False}, {"pages": ["Wrong"]}, {"pages": "not a list"},
        ):
            with self.subTest(change=change), self.assertRaises(DemoError):
                validate_conversion_evidence({**valid_shape, **change}, self.archive.project)
