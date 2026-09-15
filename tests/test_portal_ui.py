"""Execute the shipped portal JavaScript without a browser or live services."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
if not NODE:
    candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe"
    NODE = str(candidate) if candidate.is_file() else None


@unittest.skipUnless(NODE, "Node.js is required for the portal JavaScript behavior tests")
class PortalUIBehaviorTests(unittest.TestCase):
    def run_group(self, group):
        result = subprocess.run(
            [NODE, str(ROOT / "tests" / "portal_ui_harness.js"), group],
            cwd=ROOT,
            env={**os.environ, "TZ": "Asia/Shanghai"},
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"PASS {group}", result.stdout)

    def test_worker_submission_gate(self):
        self.run_group("worker-gating")

    def test_last_heartbeat_diagnostics_are_not_current_readiness(self):
        self.run_group("worker-history")

    def test_offline_acknowledgement_scope_and_reset(self):
        self.run_group("acknowledgement-reset")

    def test_submit_preflight_and_duplicate_protection(self):
        self.run_group("submission")

    def test_status_response_ordering(self):
        self.run_group("submission-races")

    def test_uncertain_post_requires_manual_reconciliation(self):
        self.run_group("uncertain")

    def test_queue_deadline_and_localized_failures(self):
        self.run_group("deadlines-errors")

    def test_safety_warnings_and_retention_are_not_failure_causes(self):
        self.run_group("warnings-retention")

    def test_existing_tasks_remain_usable_when_worker_unavailable(self):
        self.run_group("ongoing-tasks")

    def test_customer_names_are_preserved_in_titles_and_download_attributes(self):
        self.run_group("customer-names")

    def test_waiting_worker_is_not_presented_as_ready_or_a_live_stale_monitor(self):
        self.run_group("waiting-worker")
