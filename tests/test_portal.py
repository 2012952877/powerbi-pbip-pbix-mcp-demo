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

from starlette.testclient import TestClient

from pbip_mcp.auth import digest, private_file
from pbip_mcp.config import Config, Limits
from pbip_mcp.identity import Principal
from pbip_mcp.portal import create_portal
from pbip_mcp.storage import JobStore
from pbip_mcp.synthetic import fixture_files

from .helpers import archive_bytes
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
        self.assertIn('attachment; filename="report.pbix"', response.headers["content-disposition"])
        with store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM downloads").fetchone()[0], 0)
            db.execute("UPDATE jobs SET finished=0 WHERE id=?", (job["job_id"],))
        store.cleanup_expired()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"]["code"], "ARTIFACT_EXPIRED")
