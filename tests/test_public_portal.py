import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from pbip_mcp.config import Config
from pbip_mcp.client import transport
from pbip_mcp.errors import DemoError
from pbip_mcp.portal import create_portal

from .test_portal import configure_auth


class PublicPortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth.json"
        self.tokens = configure_auth(self.auth)
        self.config = Config(self.root / "data")
        self.origin = "https://pbip-test.azurewebsites.net"

    def tearDown(self):
        self.temp.cleanup()

    def test_public_requires_explicit_opt_in_and_canonical_https(self):
        with self.assertRaises(DemoError):
            create_portal(self.config, self.auth, origin=self.origin)
        for origin in ("http://test.example", "https://10.0.0.4", "https://localhost", "https://test.example/path",
                       "https://user@test.example", "https://test.example?x=1",
                       "https://test.example#fragment", "https://test.example:444"):
            with self.subTest(origin=origin), self.assertRaises(DemoError):
                create_portal(self.config, self.auth, origin=origin, allow_public_origin=True)

    def test_public_client_requires_explicit_https_without_url_secrets_or_redirect_targets(self):
        url = self.origin + "/mcp"
        with self.assertRaises(DemoError):
            transport(None, url)
        self.assertEqual(transport(None, url, allow_public_https=True), url)
        for target in ("http://example.com/mcp", "https://10.0.0.4/mcp",
                       "https://user:password@example.com/mcp", "https://example.com/mcp?token=secret",
                       "https://example.com:8443/mcp", "https://example.com/other", "https://example.com/mcp#x"):
            with self.subTest(target=target), self.assertRaises(DemoError):
                transport(None, target, allow_public_https=True)

    def test_public_cookie_secure_and_forwarded_headers_cannot_choose_origin(self):
        app = create_portal(self.config, self.auth, origin=self.origin, allow_public_origin=True)
        with TestClient(app, base_url=self.origin) as client:
            headers = {"Authorization": "Bearer " + self.tokens["alice"], "Origin": self.origin,
                       "X-Forwarded-Proto": "http", "X-Forwarded-Host": "attacker.example"}
            response = client.post("/api/session", headers=headers)
            self.assertEqual(response.status_code, 200)
            for attribute in ("Secure", "HttpOnly", "SameSite=strict"):
                self.assertIn(attribute, response.headers["set-cookie"])
            self.assertEqual(client.get("/api/session").json()["user"]["id"], "alice")
            self.assertEqual(client.get("/api/status", headers={"Host": "10.0.0.4:8765",
                "X-Forwarded-Host": "pbip-test.azurewebsites.net"}).status_code, 403)
            self.assertEqual(client.post("/api/session", headers={
                **headers, "Origin": "https://attacker.example"}).status_code, 403)
            self.assertEqual(client.post("/api/jobs", headers={"Origin": self.origin}).status_code, 403)
            csrf = response.json()["csrf_token"]
            logout = client.delete("/api/session", headers={"Origin": self.origin, "X-CSRF-Token": csrf})
            self.assertEqual(logout.status_code, 200)
            self.assertIn("Secure", logout.headers["set-cookie"])
            self.assertEqual(client.get("/api/session").status_code, 401)

    def test_public_mcp_still_requires_bearer_and_exposes_no_anonymous_jobs(self):
        app = create_portal(self.config, self.auth, origin=self.origin, allow_public_origin=True)
        with TestClient(app, base_url=self.origin) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertIn("sha256-", client.get("/").headers["content-security-policy"])
            self.assertEqual(client.get("/api/jobs").status_code, 401)
            self.assertEqual(client.post("/mcp", json={}).status_code, 401)
