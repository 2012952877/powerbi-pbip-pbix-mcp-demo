"""Real ASGI/API tests using two ephemeral identities; no Desktop success claimed."""

import io
import json
import secrets
import hashlib
import time
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

from starlette.testclient import TestClient

from pbip_mcp.auth import digest, private_file
from pbip_mcp.config import Config, Limits
from pbip_mcp.identity import Principal
from pbip_mcp.portal import create_portal
from pbip_mcp.storage import JobStore
from pbip_mcp.synthetic import fixture_files

from .helpers import archive_bytes, non_fixture_files
from .test_bidirectional import input_pbix


def configure_auth(path):
    tokens = {name: secrets.token_urlsafe(32) for name in ("alice", "bob", "admin")}
    path.write_text(json.dumps({"users": [
        {"id": name, "display_name": name.title(), "role": "admin" if name == "admin" else "user",
         "token_sha256": digest(token)} for name, token in tokens.items()
    ]}), encoding="utf-8")
    private_file(path, create=True)
    return tokens


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root / "data")
        auth = self.root / "auth.json"
        self.tokens = configure_auth(auth)
        self.client = TestClient(create_portal(self.config, auth), base_url="http://127.0.0.1:8765")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, name="alice"):
        response = self.client.post("/api/session", headers={"Authorization": "Bearer " + self.tokens[name]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.csrf = response.json()["csrf_token"]
        return {"X-CSRF-Token": self.csrf}

    def submit(self, headers, *, name="project.zip", data=None, mode=None):
        fields = {"direction": "auto"}
        if mode:
            fields["export_mode"] = mode
        return self.client.post("/api/jobs", headers=headers, files={"file": (name, data or archive_bytes())}, data=fields)

    def test_session_origin_csrf_and_logout(self):
        self.assertEqual(self.client.get("/api/session").status_code, 401)
        self.assertEqual(self.client.post("/api/session", json={"owner_id": "admin"}).status_code, 401)
        headers = self.login()
        self.assertEqual(self.client.get("/api/session").json()["user"]["id"], "alice")
        self.assertEqual(self.submit({}).status_code, 403)
        self.assertEqual(self.submit({**headers, "Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/status", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/session", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)

    def test_two_users_cannot_guess_enumerate_download_cancel(self):
        headers = self.login()
        submitted = self.submit(headers)
        self.assertEqual(submitted.status_code, 201, submitted.text)
        job = submitted.json()["job"]
        bob = self.login("bob")
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])
        for method, suffix in (("get", ""), ("post", "/cancel"), ("get", "/artifacts/pbix")):
            response = getattr(self.client, method)("/api/jobs/" + job["job_id"] + suffix, headers=bob)
            self.assertEqual(response.status_code, 404, response.text)
        status = self.client.get("/api/status").json()
        self.assertIsNone(status["queue"]["pending"])
        self.assertNotIn(job["source"]["name"], json.dumps(status))
        self.login("admin")
        self.assertEqual(self.client.get("/api/jobs/" + job["job_id"]).status_code, 200)
        self.assertEqual(self.client.get("/api/status").json()["queue"]["pending"], 1)

    def test_reverse_and_folder_share_store(self):
        headers = self.login()
        reverse = self.submit(headers, name="sample.pbix", data=input_pbix(), mode="portable")
        self.assertEqual(reverse.status_code, 201, reverse.text)
        job = reverse.json()["job"]
        self.assertEqual(job["direction"], "pbix_to_pbip")
        self.assertEqual(job["export_mode"], "portable")
        files = [("project_files", ("folder/" + name, value)) for name, value in fixture_files().items()]
        response = self.client.post("/api/jobs", headers=headers, files=files, data={"direction": "auto"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(JobStore(self.config).list_jobs(principal=Principal("alice", "Alice"))), 2)

    def test_status_retains_stale_worker_diagnostics_without_asserting_readiness(self):
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.login()
        store = JobStore(self.config)
        reason = "Keep the worker's RDP session connected and active."
        store.heartbeat(state="blocked", ready=False, reason=reason, session_id=2)
        with store._connect() as db:
            db.execute("UPDATE worker SET updated=?", (time.time() - 60,))
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        state = response.json()["worker"]
        self.assertFalse(state["ready"])
        self.assertEqual(state["state"], "stale")
        self.assertEqual(state["last_reported_state"], "blocked")
        self.assertEqual(state["last_reported_message"], reason)
        self.assertIn("expired", state["message"])
        self.assertNotIn("pid", state)
        self.assertIsNone(response.json()["queue"]["pending"])

    def test_anonymous_readiness_probe_exposes_only_current_conversion_boolean(self):
        store = JobStore(self.config)
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"ready": False})
        for state, ready, expected in (("idle", True, True), ("busy", True, True),
                                        ("waiting_for_session", False, False), ("busy", False, False),
                                        ("stopped", False, False), ("blocked", False, False)):
            with self.subTest(state=state, ready=ready):
                store.heartbeat(state=state, ready=ready, reason="PRIVATE operational reason", session_id=2)
                response = self.client.get("/readyz")
                self.assertEqual(response.status_code, 200 if expected else 503)
                self.assertEqual(response.json(), {"ready": expected})
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("PRIVATE", response.text)
                self.assertNotIn("set-cookie", response.headers)
        store.heartbeat(state="idle", ready=True, reason="PRIVATE", session_id=2)
        with store._connect() as db:
            db.execute("UPDATE worker SET updated=?", (time.time() - 60,))
        self.assertEqual(self.client.get("/readyz").json(), {"ready": False})
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)
        # The root MCP mount may handle a method mismatch as an unknown route.
        self.assertIn(self.client.post("/readyz").status_code, (404, 405))
        self.assertEqual(self.client.get("/readyz", headers={"Host": "unapproved.example"}).status_code, 403)
        self.assertEqual(self.client.get("/readyz", headers={"Origin": "https://unapproved.example"}).status_code, 403)

    def test_readiness_probe_never_expires_jobs_or_writes_a_worker_heartbeat(self):
        headers = self.login()
        job = self.submit(headers).json()["job"]
        store = JobStore(self.config)
        with store._connect() as db:
            db.execute("UPDATE jobs SET expires=0 WHERE id=?", (job["job_id"],))
            before = tuple(db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone())
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        with store._connect() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()), before)
            self.assertEqual(db.execute("SELECT count(*) FROM worker").fetchone()[0], 0)

    def test_zip_and_folder_cache_preflight_reject_without_creating_a_job(self):
        headers = self.login()
        store = JobStore(self.config)
        for cache in (None, b""):
            files = non_fixture_files(cache)
            for form in ("zip", "folder"):
                with self.subTest(cache=cache, form=form):
                    if form == "zip":
                        response = self.submit(headers, data=archive_bytes(files))
                    else:
                        response = self.client.post("/api/jobs", headers=headers,
                            files=[("project_files", ("folder/" + name, data)) for name, data in files.items()],
                            data={"direction": "pbip_to_pbix"})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["error"]["code"], "DATA_CACHE_REQUIRED")
                    with store._connect() as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
                    self.assertEqual(list(self.config.jobs_dir.iterdir()), [])

    def test_zip_and_folder_nonempty_cache_are_queued(self):
        headers = self.login()
        files = non_fixture_files(b"UNIT cache, never opened by Desktop")
        response = self.submit(headers, data=archive_bytes(files))
        self.assertEqual(response.status_code, 201, response.text)
        response = self.client.post("/api/jobs", headers=headers,
            files=[("project_files", ("folder/" + name, data)) for name, data in files.items()],
            data={"direction": "pbip_to_pbix"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(JobStore(self.config).list_jobs(principal=Principal("alice", "Alice"))), 2)

    def test_hostile_upload_paths_and_owner_spoof(self):
        headers = self.login()
        for name in ("../secret", "C:/secret", "root/../secret", "root/NUL.txt"):
            response = self.client.post("/api/jobs", headers=headers, files=[("project_files", (name, b"bad"))])
            self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/jobs", headers=headers, files={"file": ("test.zip", archive_bytes())},
                                    data={"owner_id": "bob"})
        self.assertEqual(response.json()["error"]["code"], "INPUT_FIELDS")
        response = self.submit(headers, name="only.pbip", data=b"{}")
        self.assertEqual(response.status_code, 400)

    def test_actual_body_limit_without_trusting_content_length(self):
        headers = self.login()
        response = self.client.post("/api/session", headers={
            **headers, "Authorization": "Bearer " + self.tokens["alice"], "Content-Length": "1"
        }, content=b"x" * 5000)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "BODY_SIZE")

    def test_static_csp_and_mcp_requires_bearer_not_cookie(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("sha256-", response.headers["content-security-policy"])
        self.login()
        response = self.client.post("/mcp", json={})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(str(self.root), response.text)

    def test_authorized_download_lease_and_expiry_are_explicit(self):
        headers = self.login()
        job = self.submit(headers).json()["job"]
        store = JobStore(self.config)
        claim = store.claim()
        directory = store.job_dir(job["job_id"]) / "output"
        directory.mkdir()
        data = b"UNIT STREAM TEST ONLY; NOT GUI"
        (directory / "report.pbix").write_bytes(data)
        store.finish(job["job_id"], claim["lease"], artifacts=[
            {"kind": "pbix", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        ])
        url = "/api/jobs/" + job["job_id"] + "/artifacts/pbix"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, data)
        self.assertIn('attachment; filename="project.pbix"', response.headers["content-disposition"])
        with store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM downloads").fetchone()[0], 0)
            db.execute("UPDATE jobs SET finished=0 WHERE id=?", (job["job_id"],))
        store.cleanup_expired()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"]["code"], "ARTIFACT_EXPIRED")

    def test_original_names_reach_titles_artifacts_and_unicode_download_headers(self):
        headers = self.login()
        store = JobStore(self.config)
        cases = (
            ("客户 报表.v2.zip", "zip", "客户 报表.v2"),
            ("Customer.Q1'100%.ZIP", "zip", "Customer.Q1'100%"),
            ("客户 工程.v2.zip", "folder", "客户 工程.v2.zip"),
            ("rootless", "rootless", "Synthetic"),
            ("客户 报表.PBIX", "definitions", "客户 报表"),
            ("Customer.v2.pbix", "portable", "Customer.v2"),
        )
        for name, mode, base in cases:
            with self.subTest(name=name, mode=mode):
                if mode in ("folder", "rootless"):
                    prefix = name + "/" if mode == "folder" else ""
                    submitted = self.client.post("/api/jobs", headers=headers,
                        files=[("project_files", (prefix + path, value)) for path, value in fixture_files().items()],
                        data={"direction": "pbip_to_pbix"})
                elif mode == "zip":
                    submitted = self.submit(headers, name=name)
                else:
                    submitted = self.submit(headers, name=name, data=input_pbix(), mode=mode)
                self.assertEqual(submitted.status_code, 201, submitted.text)
                job = submitted.json()["job"]
                self.assertEqual(job["source"]["name"], "Synthetic" if mode == "rootless" else name)
                claim = store.claim()
                self.assertEqual(claim["job_id"], job["job_id"])
                output = store.job_dir(job["job_id"]) / "output"
                output.mkdir()
                main = ("pbip", "report.pbip.zip", ".pbip.zip") if mode in ("definitions", "portable") \
                    else ("pbix", "report.pbix", ".pbix")
                artifacts = []
                for kind, internal, _ in (main, ("verification", "verification.json", ".verification.json")):
                    payload = ("UNIT NAMING ONLY " + kind).encode()
                    (output / internal).write_bytes(payload)
                    artifacts.append({"kind": kind, "filename": internal, "bytes": len(payload),
                                      "sha256": hashlib.sha256(payload).hexdigest()})
                store.finish(job["job_id"], claim["lease"], artifacts=artifacts)
                result = self.client.get("/api/jobs/" + job["job_id"]).json()["job"]
                for kind, internal, suffix in (main, ("verification", "verification.json", ".verification.json")):
                    expected = base + suffix
                    metadata = next(item for item in result["artifacts"] if item["kind"] == kind)
                    self.assertEqual(metadata["filename"], expected)
                    response = self.client.get(f"/api/jobs/{job['job_id']}/artifacts/{kind}")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.content, (output / internal).read_bytes())
                    disposition = response.headers["content-disposition"]
                    disposition.encode("ascii")
                    self.assertEqual(unquote(disposition.split("filename*=UTF-8''", 1)[1]), expected)
                    fallback = expected if expected.isascii() else internal
                    self.assertTrue(disposition.startswith(f'attachment; filename="{fallback}";'))
                with store._connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM downloads").fetchone()[0], 0)

    def test_unsafe_download_name_is_rejected_before_queue_creation(self):
        headers = self.login()
        response = self.submit(headers, name="NUL.zip")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "INPUT_NAME")
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])
