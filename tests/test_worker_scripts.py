"""PowerShell boundary tests, with only static session identity probes AST-substituted."""

import json
import os
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(POWERSHELL and os.name == "nt", "Windows PowerShell task-script boundary tests.")
class WorkerScriptTests(unittest.TestCase):
    def run_script(self, parameters=None, *, stop=False, **changes):
        return self.run_cases([{"parameters": parameters or {"Action": "Status"}, "stop": stop, **changes}])[0]

    def run_cases(self, cases):
        cases = [{
            "mode": "manager", "present": True, "state": "Ready", "saved_review": 20,
            "saved_idle": 1800, "extra_arguments": "", "ready": False, "running": 0,
            "parameters": {"Action": "Status"}, **case,
        } for case in cases]
        root = PROJECT / f".worker-script-tests-{uuid.uuid4().hex}"
        try:
            for index, case in enumerate(cases):
                data = root / f"case-{index}" / "data"
                data.mkdir(parents=True)
                if case.get("stop"):
                    (data / "worker.stop").write_text("UNIT stop", encoding="utf-8")
            env = {**os.environ, "PBIP_WORKER_SCRIPT_UNIT_CASE": json.dumps(cases)}
            result = subprocess.run([
                POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(PROJECT / "tests" / "worker_script_harness.ps1"),
                "-Project", str(PROJECT), "-Root", str(root),
            ], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip(), result.stderr)
            return json.loads(result.stdout)
        finally:
            shutil.rmtree(root)

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
        self.assertEqual(result["settings"]["RestartCount"], 0)
        self.assertEqual(result["triggers"], [])
        self.assertEqual(result["output"]["recovery_mode"], "Manual")
        self.assertFalse(result["output"]["stop_requested"])

    def test_status_preserves_saved_settings_and_accepts_legacy_rc6_arguments(self):
        settings = ((None, 1800), (0, 0), (7200, 7200))
        results = self.run_cases([{"saved_idle": saved} for saved, _ in settings])
        for (saved, expected), result in zip(settings, results):
            with self.subTest(saved=saved):
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["output"]["review_seconds"], 20)
                self.assertEqual(result["output"]["idle_timeout"], expected)
                self.assertEqual(result["registrations"], 0)

    def test_register_and_start_do_not_silently_change_owned_settings(self):
        cases = [{"parameters": {"Action": action, **change}}
                 for action in ("Register", "Start")
                 for change in ({"IdleTimeout": 0}, {"ReviewSeconds": 0}, {"RecoveryMode": "SessionAware"})]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
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
        changes = (
            {"state": "Running"}, {"state": "Queued"}, {"state": "Disabled"}, {"running": 1},
            {"running": None}, {"ready": True}, {"foreign": True}, {"present": False},
            {"state": "Unknown"},
            {"state": "Running", "saved_idle": 0, "saved_recovery": "SessionAware", "worker_state": "waiting_for_session"},
            {"state_on_second_query": "Running"}, {"state_on_second_query": "Queued"},
            {"state_on_second_query": "Disabled"},
        )
        cases = [{"parameters": {"Action": "Configure", "IdleTimeout": 0}, **change} for change in changes]
        for change, result in zip(changes, self.run_cases(cases)):
            with self.subTest(change=change):
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["registrations"], 0)
                self.assertEqual(result["starts"], 0)

    def test_owned_action_parser_rejects_invalid_ranges_and_extra_arguments(self):
        changes = (
            {"saved_review": 61}, {"saved_idle": 1}, {"saved_idle": 59}, {"saved_idle": 7201},
            {"saved_idle": -1}, {"extra_arguments": " -IdleTimeout 0"},
            {"extra_arguments": "; Write-Output UNEXPECTED"}, {"extra_arguments": "\n"},
            {"saved_recovery": "Auto"}, {"extra_arguments": " -RecoveryMode Manual -RecoveryMode SessionAware"},
            {"extra_arguments": " -RecoveryMode Manual -IdleTimeout 0"},
            {"saved_recovery": "SessionAware", "extra_arguments": " -Extra"},
            {"foreign_cwd": True}, {"foreign_execute": True}, {"extra_action": True},
            {"logon_type": "ServiceAccount"}, {"run_level": "Highest"},
        )
        for change, result in zip(changes, self.run_cases(changes)):
            with self.subTest(change=change):
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
        cases = []
        expected = []
        for script in ("run-worker-session.ps1", "manage-worker.ps1", "remote-worker.ps1"):
            values = [({"IdleTimeout": idle}, idle in (0, 60, 7200)) for idle in (0, 60, 7200, -1, 1, 59, 7201)]
            values += [({"RecoveryMode": mode}, mode in ("Manual", "SessionAware")) for mode in ("Manual", "SessionAware", "Auto", "SessionAware;exit")]
            for parameters, valid in values:
                if script != "run-worker-session.ps1":
                    parameters["Action"] = "Configure"
                if script == "remote-worker.ps1":
                    parameters.update(Identity="unit", KnownHosts="unit", HostKeyAlias="unit")
                cases.append({"parameters": parameters, "mode": "parameters", "script": script})
                expected.append(valid)
        for case, valid, result in zip(cases, expected, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertEqual(result["ok"], valid, result)
                if result["ok"]:
                    self.assertEqual(result["output"]["idle_timeout"], case["parameters"].get("IdleTimeout", 1800))
                    self.assertEqual(result["output"]["recovery_mode"], case["parameters"].get("RecoveryMode", "Manual"))

    def test_remote_forwards_configure_and_only_explicit_settings_without_ssh(self):
        settings = (
            ({"Action": "Configure", "IdleTimeout": 0, "ReviewSeconds": 20},
             "-Action Configure -ReviewSeconds 20 -IdleTimeout 0", None),
            ({"Action": "Start"}, "-Action Start -WaitSeconds 30", "-IdleTimeout"),
            ({"Action": "Configure", "RecoveryMode": "SessionAware"}, "-Action Configure -RecoveryMode SessionAware -WaitSeconds 30", "-IdleTimeout"),
            ({"Action": "Configure", "RecoveryMode": "Manual"}, "-RecoveryMode Manual", "-IdleTimeout"),
        )
        cases = [{"parameters": parameters, "mode": "remote"} for parameters, _, _ in settings]
        for (parameters, expected, absent), result in zip(settings, self.run_cases(cases)):
            with self.subTest(parameters=parameters):
                self.assertTrue(result["ok"], result)
                self.assertIn(expected, result["remote"])
                if absent:
                    self.assertNotIn(absent, result["remote"])
                if "RecoveryMode" not in parameters:
                    self.assertNotIn("-RecoveryMode", result["remote"])
                for option in ("StrictHostKeyChecking=yes", "IdentitiesOnly=yes", "BatchMode=yes", "HostKeyAlias=unit-only", "ServerAliveCountMax=3"):
                    self.assertIn(option, result["remote_arguments"])
                self.assertIn("pbipdemo@127.0.0.1", result["remote_arguments"])
                self.assertEqual(result["starts"], 0)

    def test_sessionaware_registration_has_only_bounded_restart_and_exact_user_logon(self):
        result = self.run_script({"Action": "Register", "RecoveryMode": "SessionAware", "IdleTimeout": 0}, present=False, stop=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["arguments"].endswith("-ReviewSeconds 0 -IdleTimeout 0 -RecoveryMode SessionAware"))
        self.assertEqual(result["settings"]["RestartCount"], 3)
        self.assertEqual(result["settings"]["RestartInterval"], "PT1M")
        self.assertEqual(result["settings"]["ExecutionTimeLimit"], "PT0S")
        self.assertEqual(result["settings"]["MultipleInstances"], "IgnoreNew")
        self.assertEqual(result["principal"]["LogonType"], "Interactive")
        self.assertEqual(result["principal"]["RunLevel"], "Limited")
        self.assertEqual(len(result["triggers"]), 1)
        self.assertEqual(result["triggers"][0]["CimClass"]["CimClassName"], "MSFT_TaskLogonTrigger")
        self.assertEqual(result["triggers"][0]["UserId"], result["principal"]["UserId"])
        self.assertTrue(result["stop_exists"])
        self.assertEqual(result["clears"], 0)
        self.assertEqual(result["starts"], 0)
        self.assertEqual(result["output"]["recovery_mode"], "SessionAware")
        self.assertEqual(result["output"]["restart_count"], 3)
        self.assertEqual(result["output"]["restart_interval"], "PT1M")

    def test_sessionaware_requires_effective_idle_zero_before_registration(self):
        cases = [
            {"parameters": {"Action": action, "RecoveryMode": "SessionAware"}, "present": False}
            for action in ("Register", "Start")
        ] + [
            {"parameters": {"Action": "Configure", "RecoveryMode": "SessionAware"}, "saved_idle": idle}
            for idle in (None, 60, 1800)
        ] + [{
            "parameters": {"Action": "Configure", "IdleTimeout": 60}, "saved_idle": 0, "saved_recovery": "SessionAware",
        }]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertFalse(result["ok"], result)
                self.assertIn("IdleTimeout 0", result["error"])
                self.assertEqual(result["registrations"], 0)
                self.assertEqual(result["starts"], 0)

    def test_saved_recovery_mode_and_review_survive_omitted_configuration(self):
        cases = [
            {"parameters": {"Action": action}, "saved_idle": 0, "saved_recovery": "SessionAware", "stop": action != "Start"}
            for action in ("Status", "Configure", "Register", "Start")
        ] + [
            {"parameters": {"Action": "Status", "RecoveryMode": "Manual"}, "saved_idle": 0, "saved_recovery": "SessionAware"},
            {"parameters": {"Action": "Status"}, "saved_recovery": "Manual"},
            {"parameters": {"Action": "Status"}, "saved_idle": 0, "saved_recovery": "SessionAware",
             "principal_user_id": "S-1-5-21-100-200-300-1001", "trigger_changes": {"UserId": "S-1-5-21-100-200-300-1001"}},
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["output"]["recovery_mode"], case["saved_recovery"])
                self.assertEqual(result["output"]["review_seconds"], 20)
                if case["saved_recovery"] == "SessionAware":
                    self.assertEqual(result["output"]["idle_timeout"], 0)
                self.assertEqual(result["registrations"], int(case["parameters"]["Action"] == "Configure"))
                self.assertEqual(result["clears"], 0)

    def test_only_configure_changes_recovery_and_preserves_unrelated_settings(self):
        cases = [
            {"parameters": {"Action": "Configure", "RecoveryMode": "SessionAware"}, "saved_idle": 0},
            {"parameters": {"Action": "Configure", "RecoveryMode": "Manual"}, "saved_idle": 0, "saved_recovery": "SessionAware"},
        ]
        for case in cases:
            case.update(stop=True, settings={"DisallowStartIfOnBatteries": True})
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertTrue(result["ok"], result)
                mode = case["parameters"]["RecoveryMode"]
                self.assertEqual(result["output"]["recovery_mode"], mode)
                self.assertEqual(result["output"]["idle_timeout"], 0)
                self.assertEqual(result["output"]["review_seconds"], 20)
                self.assertEqual(result["settings"]["RestartCount"], 3 if mode == "SessionAware" else 0)
                self.assertEqual(len(result["triggers"]), int(mode == "SessionAware"))
                self.assertTrue(result["settings"]["DisallowStartIfOnBatteries"])
                self.assertTrue(result["forced"])
                self.assertTrue(result["stop_exists"])
                self.assertEqual(result["starts"], 0)
        cases = [
            {"parameters": {"Action": action, "RecoveryMode": "Manual"}, "saved_idle": 0, "saved_recovery": "SessionAware", "stop": True}
            for action in ("Register", "Start")
        ]
        for result in self.run_cases(cases):
            self.assertFalse(result["ok"], result)
            self.assertIn("Configure", result["error"])
            self.assertEqual(result["registrations"], 0)
            self.assertEqual(result["clears"], 0)
            self.assertTrue(result["stop_exists"])

    def test_foreign_sessionaware_profile_is_never_owned_or_changed(self):
        changes = [
            {"saved_idle": 60}, {"settings": {"RestartCount": 4}}, {"settings": {"RestartCount": 0}},
            {"settings": {"RestartInterval": "PT2M"}}, {"settings": {"ExecutionTimeLimit": "PT2H"}},
            {"settings": {"MultipleInstances": "Parallel"}}, {"trigger_changes": {"UserId": "UNOWNED\\someone"}},
            {"trigger_changes": {"UserId": ""}}, {"trigger_changes": {"Enabled": False}},
            {"trigger_changes": {"UserId": "S-1-5-21-100-200-300-1002"}},
            {"trigger_changes": {"Delay": "PT1M"}}, {"trigger_changes": {"StartBoundary": "2026-09-14T10:00:00"}},
            {"trigger_changes": {"EndBoundary": "2026-09-15T10:00:00"}}, {"trigger_type": "MSFT_TaskBootTrigger"},
            {"trigger_changes": {"Repetition": {"Interval": "PT1M", "Duration": "", "StopAtDurationEnd": False}}},
            {"extra_trigger": True},
        ]
        cases = [
            {"parameters": {"Action": action}, "saved_idle": 0, "saved_recovery": "SessionAware", **change}
            for action in ("Status", "Configure", "Stop") for change in changes
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["registrations"], 0)
                self.assertEqual(result["health_calls"], 0)
                self.assertFalse(result["stop_exists"])

    def test_manual_configure_refuses_foreign_profile_instead_of_overwriting(self):
        changes = [
            {"foreign_trigger": True}, {"settings": {"RestartCount": 1}},
            {"settings": {"RestartInterval": "PT1M"}}, {"settings": {"MultipleInstances": "Parallel"}},
            {"settings": {"ExecutionTimeLimit": "PT1H"}},
        ]
        cases = [{"parameters": {"Action": "Configure", "RecoveryMode": "SessionAware", "IdleTimeout": 0}, **change} for change in changes]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertFalse(result["ok"], result)
                self.assertIn("foreign", result["error"])
                self.assertEqual(result["registrations"], 0)
        for result in self.run_cases(changes):
            self.assertTrue(result["ok"], result)  # Legacy Manual status is still readable.

    def test_configure_handles_native_null_and_empty_trigger_collections(self):
        cases = [
            {"parameters": {"Action": "Configure", "RecoveryMode": "SessionAware"}, "saved_idle": 0},
            {"parameters": {"Action": "Configure", "RecoveryMode": "SessionAware", "IdleTimeout": 0}, "saved_idle": None},
            {"parameters": {"Action": "Configure"}, "saved_idle": 0},
            {"parameters": {"Action": "Configure", "RecoveryMode": "Manual"}, "saved_idle": 0, "saved_recovery": "SessionAware"},
            {"parameters": {"Action": "Configure", "RecoveryMode": "SessionAware"}, "saved_idle": 0, "empty_triggers": True},
        ]
        cases = [{"native_trigger_shape": True, "stop": True, **case} for case in cases]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["output"]["recovery_mode"], case["parameters"].get("RecoveryMode", "Manual"))
                self.assertEqual(result["output"]["review_seconds"], 20)
                self.assertEqual(result["output"]["idle_timeout"], 0)
                self.assertEqual(result["registrations"], 1)
                self.assertTrue(result["forced"])
                self.assertTrue(result["stop_exists"])
                self.assertEqual(result["clears"], 0)
                self.assertEqual(result["starts"], 0)

    def test_null_normalization_does_not_accept_missing_or_foreign_triggers(self):
        cases = [
            {"parameters": {"Action": action}, "saved_idle": 0, "saved_recovery": "SessionAware", shape: True}
            for action in ("Status", "Configure", "Stop") for shape in ("null_triggers", "empty_triggers")
        ] + [{
            "parameters": {"Action": "Configure", "RecoveryMode": "SessionAware", "IdleTimeout": 0},
            "native_trigger_shape": True, "foreign_trigger": True,
        }]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["registrations"], 0)
                self.assertEqual(result["health_calls"], 0)
                self.assertEqual(result["starts"], 0)
                self.assertFalse(result["stop_exists"])

    def test_stop_persists_for_ready_queued_running_and_disabled_owned_tasks(self):
        cases = [
            {"parameters": {"Action": "Stop", "WaitSeconds": 3}, "state": state, "saved_idle": 0, "saved_recovery": "SessionAware"}
            for state in ("Ready", "Queued", "Running", "Disabled")
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["stop_exists"])
                self.assertTrue(result["output"]["stop_requested"])
                self.assertFalse(result["output"]["ready"])
                self.assertFalse(result["output"]["waiting_for_session"])
                self.assertEqual(result["sleeps"], int(case["state"] in ("Queued", "Running")))
                self.assertEqual(result["clears"], 0)
                self.assertEqual(result["starts"], 0)
                self.assertEqual(result["registrations"], 0)

    def test_stop_pending_is_bounded_and_absent_or_unowned_has_no_write(self):
        cases = [
            {"parameters": {"Action": "Stop", "WaitSeconds": 3}, "state": state, "stop_pending": True}
            for state in ("Queued", "Running")
        ]
        for result in self.run_cases(cases):
            self.assertFalse(result["ok"], result)
            self.assertIn("no process was killed", result["error"])
            self.assertTrue(result["stop_exists"])
            self.assertEqual(result["sleeps"], 3)
            self.assertEqual(result["starts"], 0)
        absent, foreign = self.run_cases([
            {"parameters": {"Action": "Stop"}, "present": False},
            {"parameters": {"Action": "Stop"}, "foreign": True},
        ])
        self.assertTrue(absent["ok"], absent)
        self.assertEqual(absent["output"]["state"], "Absent")
        self.assertNotIn("worker", absent["output"])
        self.assertEqual(absent["health_calls"], 0)
        self.assertFalse(foreign["ok"], foreign)
        self.assertFalse(absent["stop_exists"])
        self.assertFalse(foreign["stop_exists"])

    def test_non_start_actions_preserve_explicit_stop_including_doctor(self):
        cases = [
            {"parameters": {"Action": action}, "stop": True, "saved_idle": 0, "saved_recovery": "SessionAware"}
            for action in ("Register", "Configure", "Doctor", "Status", "Stop")
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["stop_exists"])
                self.assertEqual(result["clears"], 0)
                self.assertEqual(result["starts"], 0)
                self.assertEqual(result["doctor_calls"], int(case["parameters"]["Action"] == "Doctor"))

    def test_start_refuses_disabled_queued_unknown_and_pending_stop_races(self):
        changes = [
            {"state": "Disabled"}, {"state": "Queued"}, {"state": "Unknown"},
            {"state": "Running", "ready": True},
            {"state": "Running", "worker_state": "waiting_for_session"},
            {"state_on_second_query": "Running"}, {"state_on_second_query": "Disabled"},
            {"state_on_second_query": "Queued"},
        ]
        cases = [{"parameters": {"Action": "Start"}, "stop": True, "saved_idle": 0, "saved_recovery": "SessionAware", **change} for change in changes]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertFalse(result["ok"], result)
                self.assertTrue(result["stop_exists"])
                self.assertEqual(result["clears"], 0)
                self.assertEqual(result["starts"], 0)
                self.assertEqual(result["registrations"], 0)

    def test_start_accepts_fresh_running_waiter_without_claiming_ready(self):
        cases = [
            {"parameters": {"Action": action, "WaitSeconds": 3}, "saved_idle": 0, "saved_recovery": "SessionAware",
             "state": state, "worker_state": "waiting_for_session", "after_start_ready": False}
            for action, state in (("Start", "Running"), ("Start", "Ready"), ("Status", "Running"))
        ]
        for case in cases:
            if case["state"] == "Ready":
                case["worker_state"] = "stopped"
                case["after_start_worker_state"] = "waiting_for_session"
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertTrue(result["ok"], result)
                self.assertFalse(result["output"]["ready"])
                self.assertFalse(result["output"]["worker"]["ready"])
                self.assertTrue(result["output"]["waiting_for_session"])
                self.assertEqual(result["output"]["worker"]["message"], "UNIT boundary only")
                self.assertEqual(result["starts"], int(case["state"] == "Ready"))
                self.assertEqual(result["registrations"], 0)

    def test_ready_task_does_not_duplicate_or_reconfigure_a_separate_waiting_worker(self):
        cases = [
            {"parameters": {"Action": action}, "state": "Ready", "saved_idle": 0,
             "saved_recovery": mode, "worker_state": "waiting_for_session", "stop": True}
            for action in ("Start", "Configure") for mode in ("Manual", "SessionAware")
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertFalse(result["ok"], result)
                self.assertIn("waiting", result["error"])
                self.assertEqual(result["starts"], 0)
                self.assertEqual(result["registrations"], 0)
                self.assertEqual(result["clears"], 0)
                self.assertTrue(result["stop_exists"])

    def test_start_rejects_stale_unsafe_or_old_waiting_and_ready_heartbeats(self):
        changes = [
            {"heartbeat_age": 16}, {"worker_state": "stale"}, {"worker_state": "blocked"},
            {"worker_state": "stopped"}, {"session_id": 0}, {"session_id": None}, {"session_id": -1},
            {"session_id": "unknown"},
            {"updated_at": None}, {"updated_at": "bad"}, {"heartbeat_age": -5},
            {"saved_recovery": "Manual"},
        ]
        cases = [
            {"parameters": {"Action": "Start", "WaitSeconds": 3}, "saved_idle": 0, "saved_recovery": "SessionAware",
             "state": state, "worker_state": "waiting_for_session", "after_start_ready": False, **change}
            for state in ("Ready", "Running") for change in changes
        ] + [
            {"parameters": {"Action": "Start", "WaitSeconds": 3}, "saved_idle": 0, "saved_recovery": "SessionAware",
             "after_start_ready": ready, "after_start_worker_state": "idle" if ready else "waiting_for_session",
             **change}
            for ready in (False, True)
            for change in ({"old_heartbeat_after_start": True}, {"after_start_task_state": "Ready"},
                           {"after_start_task_state": "Queued"}, {"stop_during_start": True}, {"expire_before_report": True})
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertFalse(result["ok"], result)
                self.assertLessEqual(result["sleeps"], 3)
                self.assertEqual(result["registrations"], 0)

    def test_session_wrapper_explicit_stop_prevents_python_and_run_artifacts(self):
        cases = [
            {"mode": "session", "parameters": {"RecoveryMode": mode, "IdleTimeout": 0}, "stop": True}
            for mode in ("Manual", "SessionAware")
        ]
        for result in self.run_cases(cases):
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["exit_code"], 0)
            self.assertIn("Explicit stop is active; worker not started", result["output"])
            self.assertTrue(result["stop_exists"])
            self.assertEqual(result["python_calls"], 0)
            self.assertEqual(result["run_creations"], 0)
            self.assertEqual(result["clears"], 0)

    def test_session_wrapper_routes_argv_and_preserves_native_logging_pattern(self):
        cases = [
            {"mode": "session", "parameters": {"RecoveryMode": mode, "IdleTimeout": 0, "ReviewSeconds": 20}, "worker_exit_code": 7}
            for mode in ("Manual", "SessionAware")
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["exit_code"], 7)
                self.assertEqual(result["python_calls"], 1)
                self.assertEqual(result["run_creations"], 1)
                self.assertEqual(result["runtime_preference"], "Continue")
                self.assertEqual(result["python_arguments"][:2], ["-m", "pbip_mcp.worker"])
                self.assertEqual("--wait-for-session" in result["python_arguments"], case["parameters"]["RecoveryMode"] == "SessionAware")
                for flag, value in (("--idle-timeout", "0"), ("--review-seconds", "20"), ("--max-jobs", "0")):
                    self.assertEqual(result["python_arguments"][result["python_arguments"].index(flag) + 1], value)
        default = self.run_cases([{"mode": "session", "parameters": {}}])[0]
        self.assertTrue(default["ok"], default)
        self.assertEqual(default["python_arguments"][-4:], ["--idle-timeout", "1800", "--review-seconds", "0"])
        self.assertNotIn("--wait-for-session", default["python_arguments"])

    def test_already_running_ready_still_requires_a_fresh_heartbeat(self):
        cases = [
            {"parameters": {"Action": "Start"}, "state": "Running", "ready": True,
             "saved_idle": 0, "saved_recovery": mode, **change}
            for mode in ("Manual", "SessionAware")
            for change in ({}, {"heartbeat_age": 16}, {"worker_state": "stale"}, {"updated_at": None})
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=case):
                fresh = not any(key in case for key in ("heartbeat_age", "worker_state", "updated_at"))
                self.assertEqual(result["ok"], fresh, result)
                if fresh:
                    self.assertTrue(result["output"]["ready"])
                    self.assertTrue(result["output"]["already_running"])
                    self.assertFalse(result["output"]["waiting_for_session"])
                self.assertEqual(result["starts"], 0)
                self.assertEqual(result["clears"], 0)

    def test_session_wrapper_rejects_finite_wait_mode_and_unsafe_preflight(self):
        cases = [
            {"mode": "session", "parameters": {"RecoveryMode": "SessionAware", "IdleTimeout": idle}}
            for idle in (60, 1800)
        ] + [
            {"mode": "session", "parameters": {"RecoveryMode": "SessionAware", "IdleTimeout": 0}, "stop": True, **change}
            for change in ({"preflight_session_id": 0}, {"preflight_sid": "S-1-5-18"})
        ]
        for index, result in enumerate(self.run_cases(cases)):
            self.assertFalse(result["ok"], result)
            self.assertIn("IdleTimeout 0" if index < 2 else "never SYSTEM or Session 0", result["error"])
            self.assertEqual(result["python_calls"], 0)
            self.assertEqual(result["run_creations"], 0)
