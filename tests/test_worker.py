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

    def test_session_recovery_waits_without_claiming_then_resumes(self):
        job = self.worker.store.submit(self.archive)
        blocked = {"ready": False, "code": "RDP_SESSION_INACTIVE", "message": "UNIT disconnected", "session_id": 2}
        ready = {"ready": True, "code": "READY", "message": "UNIT ready", "session_id": 2}
        sleeps = 0

        def sleep(seconds):
            nonlocal sleeps
            self.assertEqual(seconds, 1)
            sleeps += 1
            status = self.worker.store.worker_status()
            self.assertFalse(status["ready"])
            self.assertEqual(status["state"], "waiting_for_session")
            self.assertEqual(status["last_reported_message"], "UNIT disconnected")
            self.assertEqual(self.worker.store.get(job["job_id"])["status"], "queued")
            converter.assert_not_called()
            claim.assert_not_called()
            with self.assertRaises(DemoError) as error:
                with QueueLock(self.root):
                    self.fail("Paused worker lost its queue lock.")
            self.assertEqual(error.exception.code, "WORKER_ALREADY_RUNNING")

        with patch("pbip_mcp.worker.readiness", side_effect=[blocked, blocked, ready]), \
             patch("pbip_mcp.worker.time.sleep", side_effect=sleep), \
             patch.object(self.worker.store, "claim", wraps=self.worker.store.claim) as claim, \
             patch.object(self.worker, "_convert", side_effect=DemoError("UNIT_ONLY_FAILURE", "No Desktop")) as converter, \
             self.assertLogs("pbip_mcp.worker", level="INFO") as logs:
            self.assertEqual(self.worker.run(max_jobs=1, wait_for_session=True), 0)
        self.assertEqual(sleeps, 2)
        claim.assert_called_once()
        converter.assert_called_once()
        self.assertEqual(sum("waiting for its interactive session" in line for line in logs.output), 1)
        self.assertTrue(any("Interactive session recovered" in line for line in logs.output))
        self.assertEqual(self.worker.store.get(job["job_id"])["error"]["code"], "UNIT_ONLY_FAILURE")

    def test_recoverable_reasons_and_explicit_stop_while_waiting(self):
        for code in ("RDP_SESSION_INACTIVE", "DESKTOP_LOCKED", "SESSION_PROBE_FAILED"):
            with self.subTest(code=code):
                stop = self.root / "worker.stop"
                stop.unlink(missing_ok=True)
                blocked = {"ready": False, "code": code, "message": "UNIT blocked", "session_id": 2}

                def sleep(_seconds):
                    self.assertEqual(self.worker.store.worker_status()["state"], "waiting_for_session")
                    stop.write_text("UNIT explicit stop", encoding="utf-8")

                with patch("pbip_mcp.worker.readiness", return_value=blocked), \
                     patch("pbip_mcp.worker.time.sleep", side_effect=sleep), \
                     patch.object(self.worker.store, "claim") as claim:
                    self.assertEqual(self.worker.run(wait_for_session=True), 0)
                claim.assert_not_called()
                status = self.worker.store.worker_status()
                self.assertFalse(status["ready"])
                self.assertEqual(status["state"], "stopped")
                self.assertTrue(stop.exists())

    def test_recovery_never_waits_in_an_unsafe_or_unknown_execution_context(self):
        cases = [(code, 2) for code in (
            "SYSTEM_ACCOUNT_UNSUPPORTED", "INTERACTIVE_SESSION_REQUIRED", "WINDOWS_REQUIRED",
            "WORKER_DESKTOP_UNSUPPORTED", "DESKTOP_NOT_INSTALLED", "UNKNOWN_ERROR",
        )] + [("RDP_SESSION_INACTIVE", session) for session in (0, -1, None, True, "2")]
        for code, session in cases:
            with self.subTest(code=code, session=session):
                state = {"ready": False, "code": code, "message": "UNIT unsafe", "session_id": session}
                with patch("pbip_mcp.worker.readiness", return_value=state), \
                     patch("pbip_mcp.worker.time.sleep") as sleep, \
                     patch.object(self.worker.store, "claim") as claim:
                    self.assertEqual(self.worker.run(wait_for_session=True), 2)
                sleep.assert_not_called()
                claim.assert_not_called()
                self.assertEqual(self.worker.store.worker_status()["state"], "blocked")

    def test_waiting_does_not_extend_queue_deadlines_or_create_artifacts(self):
        job = self.worker.store.submit(self.archive)
        source = self.worker.store.job_dir(job["job_id"]) / "source.zip"
        blocked = {"ready": False, "code": "DESKTOP_LOCKED", "message": "UNIT locked", "session_id": 2}
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                with self.worker.store._connect() as db:
                    db.execute("UPDATE jobs SET expires=0 WHERE id=?", (job["job_id"],))
            else:
                with self.worker.store._connect() as db:
                    row = db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["error_code"], "QUEUE_TIMEOUT")
                self.assertEqual(row["expires"], 0)
                self.assertIsNone(row["started"])
                self.assertIsNone(row["artifact_json"])
                (self.root / "worker.stop").write_text("UNIT stop", encoding="utf-8")

        with patch("pbip_mcp.worker.readiness", return_value=blocked), \
             patch("pbip_mcp.worker.time.sleep", side_effect=sleep), \
             patch.object(self.worker.store, "claim") as claim:
            self.assertEqual(self.worker.run(wait_for_session=True), 0)
        claim.assert_not_called()
        self.assertEqual(source.read_bytes(), self.archive.data)
        self.assertEqual(sleeps, 2)

    def test_session_loss_failure_is_not_replayed_after_recovery(self):
        first = self.worker.store.submit(self.archive)
        second = self.worker.store.submit(self.archive)
        ready = {"ready": True, "code": "READY", "message": "UNIT ready", "session_id": 2}
        blocked = {"ready": False, "code": "RDP_SESSION_INACTIVE", "message": "UNIT disconnected", "session_id": 2}
        converted = []

        def convert(claim):
            converted.append(claim["job_id"])
            if claim["job_id"] == first["job_id"]:
                raise DemoError("INTERACTIVE_SESSION_LOST", "UNIT session loss, no GUI")
            raise DemoError("UNIT_ONLY_FAILURE", "No real Desktop")

        def sleep(_seconds):
            self.assertEqual(converted, [first["job_id"]])
            self.assertEqual(self.worker.store.get(first["job_id"])["error"]["code"], "INTERACTIVE_SESSION_LOST")
            self.assertEqual(self.worker.store.get(second["job_id"])["status"], "queued")

        with patch("pbip_mcp.worker.readiness", side_effect=[ready, blocked, ready]), \
             patch("pbip_mcp.worker.time.sleep", side_effect=sleep), \
             patch.object(self.worker, "_convert", side_effect=convert):
            self.assertEqual(self.worker.run(max_jobs=2, wait_for_session=True), 0)
        self.assertEqual(converted, [first["job_id"], second["job_id"]])
        self.assertEqual(self.worker.store.get(first["job_id"])["error"]["code"], "INTERACTIVE_SESSION_LOST")

    def test_stop_at_start_prevents_interruption_recovery_and_all_claims(self):
        job = self.worker.store.submit(self.archive)
        self.worker.store.claim()
        with self.worker.store._connect() as db:
            before = tuple(db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone())
        (self.root / "worker.stop").write_text("UNIT explicit stop", encoding="utf-8")
        with patch.object(self.worker.store, "recover_interrupted") as recover, \
             patch.object(self.worker.store, "claim") as claim, patch("pbip_mcp.worker.readiness") as probe:
            self.assertEqual(self.worker.run(wait_for_session=True), 0)
        recover.assert_not_called()
        claim.assert_not_called()
        probe.assert_not_called()
        with self.worker.store._connect() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()), before)

    def test_stop_arriving_during_readiness_prevents_next_claim(self):
        def probe(_desktop):
            (self.root / "worker.stop").write_text("UNIT stop", encoding="utf-8")
            return {"ready": True, "code": "READY", "message": "UNIT ready", "session_id": 2}

        with patch("pbip_mcp.worker.readiness", side_effect=probe), patch.object(self.worker.store, "claim") as claim:
            self.assertEqual(self.worker.run(wait_for_session=True), 0)
        claim.assert_not_called()
        self.assertEqual(self.worker.store.worker_status()["state"], "stopped")

    def test_wait_mode_cannot_mask_storage_failure_as_a_recovering_worker(self):
        blocked = {"ready": False, "code": "DESKTOP_LOCKED", "message": "UNIT locked", "session_id": 2}
        with patch("pbip_mcp.worker.readiness", return_value=blocked), \
             patch.object(self.worker.store, "queue_status", side_effect=OSError("UNIT disk failure")), \
             self.assertRaisesRegex(OSError, "UNIT disk failure"):
            self.worker.run(wait_for_session=True)
        self.assertEqual(self.worker.store.worker_status()["state"], "blocked")

    def test_recovery_configuration_is_explicit_and_requires_idle_zero(self):
        for wait, idle in ((True, 60), (True, 1800), ("true", 0), (1, 0)):
            with self.subTest(wait=wait, idle=idle), self.assertRaises(DemoError) as error:
                self.worker.run(wait_for_session=wait, idle_timeout=idle)
            self.assertEqual(error.exception.code, "WORKER_RECOVERY_CONFIG")
        args = ["pbip-worker", "--data-dir", str(self.root), "--desktop-exe", "not-launched.exe",
                "--wait-for-session", "--idle-timeout", "60"]
        with patch.object(sys, "argv", args), contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)

    def test_cli_routes_wait_flag_without_bypassing_persistent_stop(self):
        (self.root / "worker.stop").write_text("UNIT stop", encoding="utf-8")
        args = ["pbip-worker", "--data-dir", str(self.root), "--desktop-exe", "not-launched.exe", "--wait-for-session"]
        with patch.object(sys, "argv", args), patch("pbip_mcp.worker.logging.basicConfig"), \
             patch.object(Worker, "run", return_value=0) as run, self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 0)
        run.assert_called_once_with(max_jobs=0, idle_timeout=0, wait_for_session=True)
        self.assertTrue((self.root / "worker.stop").exists())

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
