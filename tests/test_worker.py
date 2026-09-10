import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pbip_mcp.archive import ValidatedArchive
from pbip_mcp.config import Config
from pbip_mcp.errors import DemoError
from pbip_mcp.worker import QueueLock, Worker, main, validate_conversion_evidence

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

    def test_idle_zero_waits_beyond_finite_limit_then_stops_without_clearing_request(self):
        ready = {"ready": True, "code": None, "message": "UNIT TEST ONLY", "session_id": 1}
        now = 0
        sleeps = 0

        def sleep(_seconds):
            nonlocal now, sleeps
            now += 8000
            sleeps += 1
            if sleeps == 2:
                (self.root / "worker.stop").write_text("unit stop", encoding="utf-8")
            if sleeps > 2:
                self.fail("Idle-zero worker ignored graceful stop.")

        with patch("pbip_mcp.worker.readiness", return_value=ready), \
             patch("pbip_mcp.worker.time.monotonic", side_effect=lambda: now), \
             patch("pbip_mcp.worker.time.sleep", side_effect=sleep):
            self.assertEqual(self.worker.run(idle_timeout=0), 0)
        self.assertEqual(sleeps, 2)
        self.assertTrue((self.root / "worker.stop").is_file())
        self.assertEqual(self.worker.store.worker_status()["state"], "stopped")
        with QueueLock(self.root):
            pass

    def test_finite_idle_timeout_exits_at_its_bound(self):
        ready = {"ready": True, "code": None, "message": "UNIT TEST ONLY", "session_id": 1}
        for timeout in (60, 1800, 7200):
            with self.subTest(timeout=timeout):
                now = 0
                sleeps = 0

                def sleep(_seconds):
                    nonlocal now, sleeps
                    now += timeout
                    sleeps += 1
                    if sleeps > 1:
                        self.fail("Finite idle timeout was ignored.")

                with patch("pbip_mcp.worker.readiness", return_value=ready), \
                     patch("pbip_mcp.worker.time.monotonic", side_effect=lambda: now), \
                     patch("pbip_mcp.worker.time.sleep", side_effect=sleep):
                    self.assertEqual(self.worker.run(idle_timeout=timeout), 0)
                self.assertEqual(sleeps, 1)
                self.assertFalse((self.root / "worker.stop").exists())

    def test_idle_zero_stop_during_conversion_preserves_the_next_queued_job(self):
        first = self.worker.store.submit(self.archive)
        second = self.worker.store.submit(self.archive)
        ready = {"ready": True, "code": None, "message": "UNIT TEST ONLY", "session_id": 1}

        def convert(claim):
            self.assertEqual(claim["job_id"], first["job_id"])
            (self.root / "worker.stop").write_text("unit stop", encoding="utf-8")
            raise DemoError("UNIT_ONLY_FAILURE", "Boundary double, no Desktop launched.")

        with patch("pbip_mcp.worker.readiness", return_value=ready), \
             patch.object(self.worker, "_convert", side_effect=convert) as converter:
            self.assertEqual(self.worker.run(idle_timeout=0), 0)
        self.assertEqual(converter.call_count, 1)
        self.assertEqual(self.worker.store.get(first["job_id"])["status"], "failed")
        self.assertEqual(self.worker.store.get(second["job_id"])["status"], "queued")
        self.assertTrue((self.root / "worker.stop").exists())

    def test_idle_zero_still_exits_when_the_interactive_session_is_lost(self):
        states = [
            {"ready": True, "code": None, "message": "UNIT", "session_id": 1},
            {"ready": False, "code": "SESSION_LOCKED", "message": "Unit locked session.", "session_id": 1},
        ]
        with patch("pbip_mcp.worker.readiness", side_effect=states), patch("pbip_mcp.worker.time.sleep"):
            self.assertEqual(self.worker.run(idle_timeout=0), 2)
        self.assertEqual(self.worker.store.worker_status()["state"], "blocked")

    def test_worker_and_cli_reject_invalid_idle_timeout(self):
        for timeout in (-1, 1, 59, 7201):
            with self.subTest(timeout=timeout):
                with self.assertRaises(DemoError) as error:
                    self.worker.run(idle_timeout=timeout)
                self.assertEqual(error.exception.code, "WORKER_IDLE_TIMEOUT")
                args = ["pbip-worker", "--data-dir", str(self.root), "--desktop-exe", "not-launched.exe",
                        "--idle-timeout", str(timeout)]
                with patch.object(sys, "argv", args), contextlib.redirect_stderr(io.StringIO()), \
                     self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)

    def test_cli_idle_zero_reaches_real_worker_loop_and_preserves_stop(self):
        (self.root / "worker.stop").write_text("unit stop", encoding="utf-8")
        args = ["pbip-worker", "--data-dir", str(self.root), "--desktop-exe", "not-launched.exe", "--idle-timeout", "0"]
        with patch.object(sys, "argv", args), patch("pbip_mcp.worker.logging.basicConfig"), \
             self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 0)
        self.assertTrue((self.root / "worker.stop").exists())

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
