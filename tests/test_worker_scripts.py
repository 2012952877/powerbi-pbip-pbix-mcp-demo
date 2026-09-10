"""Execute unchanged PowerShell scripts with task/runtime/network boundaries intercepted."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(POWERSHELL and os.name == "nt", "Windows PowerShell task-script boundary tests.")
class WorkerScriptTests(unittest.TestCase):
    def run_script(self, parameters=None, *, stop=False, **changes):
        case = {
            "mode": "manager", "present": True, "state": "Ready", "saved_review": 20,
            "saved_idle": 1800, "extra_arguments": "", "ready": False, "running": 0,
            "parameters": parameters or {"Action": "Status"}, **changes,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            if stop:
                (root / "data" / "worker.stop").write_text("UNIT stop", encoding="utf-8")
            env = {**os.environ, "PBIP_WORKER_SCRIPT_UNIT_CASE": json.dumps(case)}
            result = subprocess.run([
                POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(PROJECT / "tests" / "worker_script_harness.ps1"),
                "-Project", str(PROJECT), "-Root", str(root),
            ], env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip(), result.stderr)
            return json.loads(result.stdout)

    def test_register_idle_zero_persists_argument_and_removes_execution_limit(self):
        result = self.run_script({"Action": "Register", "IdleTimeout": 0}, present=False, stop=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["registrations"], 1)
        self.assertFalse(result["forced"])
        self.assertTrue(result["arguments"].endswith("-ReviewSeconds 0 -IdleTimeout 0"))
        self.assertEqual(result["execution_limit"], "PT0S")
        self.assertEqual(result["output"]["idle_timeout"], 0)
        self.assertTrue(result["stop_exists"])
        self.assertEqual(result["clears"], 0)
        self.assertEqual(result["starts"], 0)

    def test_register_finite_keeps_two_hour_execution_limit(self):
        result = self.run_script({"Action": "Register", "IdleTimeout": 60}, present=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["execution_limit"], "PT2H")
        self.assertTrue(result["arguments"].endswith("-ReviewSeconds 0 -IdleTimeout 60"))

    def test_status_preserves_saved_settings_and_accepts_legacy_rc6_arguments(self):
        for saved, expected in ((None, 1800), (0, 0), (7200, 7200)):
            with self.subTest(saved=saved):
                result = self.run_script(saved_idle=saved)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["output"]["review_seconds"], 20)
                self.assertEqual(result["output"]["idle_timeout"], expected)
                self.assertEqual(result["registrations"], 0)

    def test_register_and_start_do_not_silently_change_owned_settings(self):
        for action in ("Register", "Start"):
            for change in ({"IdleTimeout": 0}, {"ReviewSeconds": 0}):
                with self.subTest(action=action, change=change):
                    result = self.run_script({"Action": action, **change})
                    self.assertFalse(result["ok"], result)
                    self.assertIn("Configure", result["error"])
                    self.assertEqual(result["registrations"], 0)
                    self.assertEqual(result["starts"], 0)

    def test_configure_legacy_task_persists_idle_zero_and_preserves_review_and_stop(self):
        result = self.run_script({"Action": "Configure", "IdleTimeout": 0}, saved_idle=None, stop=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["arguments"].endswith("-ReviewSeconds 20 -IdleTimeout 0"))
        self.assertEqual(result["execution_limit"], "PT0S")
        self.assertEqual(result["registrations"], 1)
        self.assertTrue(result["forced"])
        self.assertEqual(result["starts"], 0)
        self.assertEqual(result["clears"], 0)
        self.assertTrue(result["stop_exists"])

    def test_configure_updates_both_settings_and_finite_limit_explicitly(self):
        result = self.run_script({"Action": "Configure", "IdleTimeout": 60, "ReviewSeconds": 0}, saved_idle=0)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["arguments"].endswith("-ReviewSeconds 0 -IdleTimeout 60"))
        self.assertEqual(result["execution_limit"], "PT2H")

    def test_configure_refuses_active_unowned_unknown_or_absent_tasks(self):
        for change in (
            {"state": "Running"}, {"state": "Queued"}, {"state": "Disabled"}, {"running": 1},
            {"running": None}, {"ready": True}, {"foreign": True}, {"present": False},
        ):
            with self.subTest(change=change):
                result = self.run_script({"Action": "Configure", "IdleTimeout": 0}, **change)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["registrations"], 0)
                self.assertEqual(result["starts"], 0)

    def test_owned_action_parser_rejects_invalid_ranges_and_extra_arguments(self):
        for change in (
            {"saved_review": 61}, {"saved_idle": 1}, {"saved_idle": 59}, {"saved_idle": 7201},
            {"saved_idle": -1}, {"extra_arguments": " -IdleTimeout 0"},
            {"extra_arguments": "; Write-Output UNEXPECTED"}, {"extra_arguments": "\n"},
        ):
            with self.subTest(change=change):
                result = self.run_script(**change)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["registrations"], 0)
                self.assertEqual(result["health_calls"], 0)

    def test_start_preserves_idle_zero_and_is_the_only_stop_file_clearer(self):
        result = self.run_script({"Action": "Start"}, saved_idle=0, stop=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"]["idle_timeout"], 0)
        self.assertEqual(result["registrations"], 0)
        self.assertEqual(result["starts"], 1)
        self.assertEqual(result["clears"], 1)
        self.assertFalse(result["stop_exists"])

    def test_stop_idle_zero_requests_graceful_exit_without_killing_or_reconfiguring(self):
        result = self.run_script({"Action": "Stop"}, saved_idle=0, state="Running", ready=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"]["state"], "Ready")
        self.assertTrue(result["stop_exists"])
        self.assertEqual(result["clears"], 0)
        self.assertEqual(result["starts"], 0)
        self.assertEqual(result["registrations"], 0)

    def test_actual_parameter_validation_for_all_worker_scripts(self):
        for script in ("run-worker-session.ps1", "manage-worker.ps1", "remote-worker.ps1"):
            for idle in (0, 60, 7200, -1, 1, 59, 7201):
                with self.subTest(script=script, idle=idle):
                    parameters = {"IdleTimeout": idle}
                    if script != "run-worker-session.ps1":
                        parameters["Action"] = "Configure"
                    if script == "remote-worker.ps1":
                        parameters.update(Identity="unit", KnownHosts="unit", HostKeyAlias="unit")
                    result = self.run_script(parameters, mode="parameters", script=script)
                    self.assertEqual(result["ok"], idle in (0, 60, 7200), result)
                    if result["ok"]:
                        self.assertEqual(result["output"]["idle_timeout"], idle)

    def test_remote_forwards_configure_and_only_explicit_settings_without_ssh(self):
        for parameters, expected, absent in (
            ({"Action": "Configure", "IdleTimeout": 0, "ReviewSeconds": 20},
             "-Action Configure -ReviewSeconds 20 -IdleTimeout 0", None),
            ({"Action": "Start"}, "-Action Start -WaitSeconds 30", "-IdleTimeout"),
        ):
            with self.subTest(parameters=parameters):
                result = self.run_script(parameters, mode="remote")
                self.assertTrue(result["ok"], result)
                self.assertIn(expected, result["remote"])
                if absent:
                    self.assertNotIn(absent, result["remote"])
