import asyncio
import base64
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mcp import Client

from pbip_mcp.archive import ValidatedArchive
from pbip_mcp.client import call, download, transport, wait_for_job, authenticated_http_transport
from pbip_mcp.config import Config
from pbip_mcp.errors import DemoError
from pbip_mcp.server import create_server
from pbip_mcp.storage import JobStore

from .helpers import archive_bytes, non_fixture_files
from .test_portal import configure_auth
from .test_bidirectional import input_pbix


class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temporary.name))
        self.server = create_server(self.config)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_real_sdk_tools_submission_status_cancel_and_resource(self):
        async with Client(self.server, raise_exceptions=True) as client:
            listing = await client.list_tools()
            self.assertEqual(
                {tool.name for tool in listing.tools},
                {"converter_status", "submit_project", "submit_pbix", "list_jobs", "get_job", "cancel_job", "get_artifact",
                 "begin_artifact_download", "finish_artifact_download"},
            )
            health = await call(client, "converter_status")
            self.assertFalse(health["worker"]["ready"])
            queued = await call(client, "submit_project", {"archive_base64": base64.b64encode(archive_bytes()).decode()})
            job = queued["job"]
            self.assertEqual(job["status"], "queued")
            status = await call(client, "get_job", {"job_id": job["job_id"]})
            self.assertEqual(status["job"], job)
            resource = await client.read_resource(f"pbip://jobs/{job['job_id']}")
            self.assertIn(job["job_id"], resource.contents[0].text)
            cancelled = await call(client, "cancel_job", {"job_id": job["job_id"]})
            self.assertEqual(cancelled["job"]["status"], "cancelled")

    async def test_errors_have_is_error_and_safe_machine_codes(self):
        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.call_tool("submit_project", {"archive_base64": "invalid !"})
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content["error"]["code"], "INVALID_BASE64")
            self.assertNotIn(str(self.config.data_dir), str(result))
            with self.assertRaises(DemoError) as caught:
                await call(client, "get_job", {"job_id": "..\\source.zip"})
            self.assertEqual(caught.exception.code, "INVALID_JOB_ID")
            result = await client.call_tool("run_shell", {"command": "not executed"})
            self.assertTrue(result.is_error)

    async def test_status_preserves_last_worker_report_after_heartbeat_expires(self):
        store = JobStore(self.config)
        reason = "Keep the worker's RDP session connected and active."
        store.heartbeat(state="blocked", ready=False, reason=reason, session_id=2)
        with store._connect() as db:
            db.execute("UPDATE worker SET updated=?", (time.time() - 60,))
        async with Client(self.server, raise_exceptions=True) as client:
            health = await call(client, "converter_status")
        worker = health["worker"]
        self.assertFalse(worker["ready"])
        self.assertEqual(worker["state"], "stale")
        self.assertEqual(worker["last_reported_state"], "blocked")
        self.assertEqual(worker["last_reported_message"], reason)
        self.assertIn("expired", worker["message"])
        self.assertNotIn("pid", worker)

    async def test_customer_names_match_mcp_metadata_and_preserve_explicit_client_output(self):
        store = JobStore(self.config)
        async with Client(self.server, raise_exceptions=True, cache=None) as client:
            response = await call(client, "submit_pbix", {
                "pbix_base64": base64.b64encode(input_pbix()).decode(),
                "source_name": "客户 报表.v2.pbix", "export_mode": "portable",
            })
            job = response["job"]
            self.assertEqual(job["source"]["name"], "客户 报表.v2.pbix")
            claim = store.claim()
            output = store.job_dir(job["job_id"]) / "output"
            output.mkdir()
            payload = b"UNIT PROTOCOL ONLY; NOT A REAL PBIP ZIP"
            (output / "report.pbip.zip").write_bytes(payload)
            store.finish(job["job_id"], claim["lease"], artifacts=[{
                "kind": "pbip", "filename": "report.pbip.zip", "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }])
            result = await call(client, "get_job", {"job_id": job["job_id"]})
            self.assertEqual(result["job"]["artifacts"][0]["filename"], "客户 报表.v2.pbip.zip")
            resource = await client.read_resource(f"pbip://jobs/{job['job_id']}")
            self.assertEqual(json.loads(resource.contents[0].text)["job"]["artifacts"][0]["filename"],
                             "客户 报表.v2.pbip.zip")
            transfer = (await call(client, "begin_artifact_download", {"job_id": job["job_id"], "kind": "pbip"}))["download"]
            try:
                chunk = await call(client, "get_artifact", {"job_id": job["job_id"], "kind": "pbip",
                    "download_id": transfer["download_id"]})
                self.assertEqual(chunk["artifact"]["filename"], "客户 报表.v2.pbip.zip")
            finally:
                await call(client, "finish_artifact_download", {"job_id": job["job_id"], "kind": "pbip",
                    "download_id": transfer["download_id"]})
            chosen = self.config.data_dir / "explicit-user-choice.zip"
            result = await download(client, job["job_id"], chosen, "pbip")
            self.assertEqual(result["file"], str(chosen.resolve()))
            self.assertEqual(chosen.read_bytes(), payload)

    async def test_actual_stdio_sdk_transport(self):
        async with Client(transport(self.config.data_dir, None), read_timeout_seconds=20) as client:
            self.assertEqual(client.server_info.name, "PBIP Desktop converter")
            queued = await call(client, "submit_project", {"archive_base64": base64.b64encode(archive_bytes()).decode()})
            self.assertEqual(queued["job"]["status"], "queued")
            self.assertEqual((await call(client, "get_job", {"job_id": queued["job"]["job_id"]}))["job"]["source"], queued["job"]["source"])
            await call(client, "cancel_job", {"job_id": queued["job"]["job_id"]})
        self.assertEqual(JobStore(self.config).get(queued["job"]["job_id"])["status"], "cancelled")

    async def test_sdk_and_stdio_cache_preflight_is_a_typed_error_without_new_jobs(self):
        store = JobStore(self.config)
        for name, connection in (("sdk", self.server), ("stdio", transport(self.config.data_dir, None))):
            async with Client(connection, read_timeout_seconds=20, cache=None) as client:
                for cache in (None, b""):
                    with self.subTest(transport=name, cache=cache):
                        before = len(store.list_jobs())
                        result = await client.call_tool("submit_project", {
                            "archive_base64": base64.b64encode(archive_bytes(non_fixture_files(cache))).decode(),
                        })
                        self.assertTrue(result.is_error)
                        self.assertFalse(result.structured_content["ok"])
                        self.assertEqual(result.structured_content["error"]["code"], "DATA_CACHE_REQUIRED")
                        self.assertNotIn("job", result.structured_content)
                        self.assertEqual(len(store.list_jobs()), before)
                queued = await call(client, "submit_project", {
                    "archive_base64": base64.b64encode(archive_bytes(non_fixture_files(b"UNIT cache"))).decode(),
                })
                self.assertEqual(queued["job"]["status"], "queued")
                await call(client, "cancel_job", {"job_id": queued["job"]["job_id"]})

    async def test_real_loopback_http_transport(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        log_path = self.config.data_dir / "http-test.log"
        auth_path = self.config.data_dir / "auth.json"
        tokens = configure_auth(auth_path)
        from pbip_mcp.auth import private_file
        private_file(self.config.data_dir, create=True)
        with log_path.open("wb") as log:
            arguments = [sys.executable, "-m", "pbip_mcp.server", "--data-dir", str(self.config.data_dir),
                         "--transport", "streamable-http", "--port", str(port), "--auth-config", str(auth_path)]
            if sys.platform == "win32":
                from pbip_mcp.windows_session import OwnedProcess
                process = OwnedProcess(arguments, cwd=self.config.data_dir, log_path=log_path)
            else:
                process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        self.fail(log_path.read_text(encoding="utf-8"))
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail("Loopback MCP server did not become responsive before the deadline.")
                        await asyncio.sleep(0.1)
                url = f"http://127.0.0.1:{port}/mcp"
                async with Client(authenticated_http_transport(url, tokens["alice"]), read_timeout_seconds=10, cache=None) as client:
                    self.assertFalse((await call(client, "converter_status"))["worker"]["ready"])
                    queued = await call(client, "submit_project", {"archive_base64": base64.b64encode(archive_bytes()).decode()})
                    self.assertEqual(queued["job"]["status"], "queued")
                    reverse = await call(client, "submit_pbix", {
                        "pbix_base64": base64.b64encode(input_pbix()).decode(), "export_mode": "portable",
                    })
                    self.assertEqual(reverse["job"]["direction"], "pbix_to_pbip")
                    await call(client, "cancel_job", {"job_id": queued["job"]["job_id"]})
                async with Client(authenticated_http_transport(url, tokens["bob"]), read_timeout_seconds=10, cache=None) as client:
                    self.assertEqual((await call(client, "list_jobs"))["jobs"], [])
                    for tool in ("get_job", "cancel_job", "get_artifact"):
                        with self.assertRaises(DemoError) as error:
                            await call(client, tool, {"job_id": queued["job"]["job_id"]})
                        self.assertEqual(error.exception.code, "JOB_NOT_FOUND")
                    resource = await client.read_resource(f"pbip://jobs/{queued['job']['job_id']}")
                    self.assertEqual(json.loads(resource.contents[0].text)["error"]["code"], "JOB_NOT_FOUND")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
                if sys.platform == "win32":
                    process.close()

    async def test_download_protocol_only_not_a_conversion(self):
        store = JobStore(self.config)
        job = store.submit(ValidatedArchive(archive_bytes()))
        claim = store.claim()
        directory = store.job_dir(job["job_id"]) / "output"
        directory.mkdir()
        data = b"unit-test-only chunk protocol" * 15000
        (directory / "report.pbix").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        store.finish(job["job_id"], claim["lease"], artifacts=[{"kind": "pbix", "bytes": len(data), "sha256": digest}])
        async with Client(self.server, raise_exceptions=True) as client:
            path = self.config.data_dir / "download.bin"
            result = await download(client, job["job_id"], path)
            self.assertEqual(result["sha256"], digest)
            self.assertEqual(path.read_bytes(), data)
            with self.assertRaises(DemoError) as caught:
                await download(client, job["job_id"], path)
            self.assertEqual(caught.exception.code, "CLIENT_OUTPUT_EXISTS")
            self.assertEqual(path.read_bytes(), data)

    async def test_client_finite_wait_and_server_failure(self):
        store = JobStore(self.config)
        job = store.submit(ValidatedArchive(archive_bytes()))
        async with Client(self.server, raise_exceptions=True) as client:
            with self.assertRaises(DemoError) as caught:
                await wait_for_job(client, job["job_id"], 0)
            self.assertEqual(caught.exception.code, "CLIENT_TIMEOUT")
            claim = store.claim()
            store.finish(job["job_id"], claim["lease"], error=DemoError("DESKTOP_NOT_READY", "Unit-test injected error."))
            with self.assertRaises(DemoError) as caught:
                await wait_for_job(client, job["job_id"], 1)
            self.assertEqual(caught.exception.code, "DESKTOP_NOT_READY")

    def test_stdio_cli_error_writes_result_json(self):
        result_path = self.config.data_dir / "client-error.json"
        completed = subprocess.run(
            [sys.executable, "-m", "pbip_mcp.client", "--data-dir", str(self.config.data_dir),
             "--result-json", str(result_path), "job", "--job-id", "invalid"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_JOB_ID")
        self.assertNotIn("ExceptionGroup", completed.stderr)

    def test_no_public_http_target(self):
        for url in ("http://0.0.0.0:8765/mcp", "http://example.com/mcp", "file:///c:/secret", "http://user:password@localhost/mcp"):
            with self.subTest(url=url), self.assertRaises(DemoError):
                transport(None, url)
        self.assertEqual(transport(None, "http://127.0.0.1:8765/mcp"), "http://127.0.0.1:8765/mcp")
