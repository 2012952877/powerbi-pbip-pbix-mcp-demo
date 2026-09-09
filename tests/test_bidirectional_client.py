import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp import Client

from pbip_mcp.client import submit, run_connected, call
from pbip_mcp.config import Config
from pbip_mcp.errors import DemoError
from pbip_mcp.server import create_server
from pbip_mcp.storage import JobStore

from .helpers import archive_bytes
from .test_bidirectional import input_pbix


class BidirectionalClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_dispatch_preserves_zip_and_explicit_pbix_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forward, reverse = root / "input.zip", root / "input.pbix"
            forward.write_bytes(archive_bytes())
            reverse.write_bytes(input_pbix())
            async with Client(create_server(Config(root / "data")), cache=None) as client:
                first = await submit(client, forward)
                second = await run_connected(client, argparse.Namespace(
                    action="submit", zip=None, pbix=reverse, direction="pbix_to_pbip", export_mode="portable",
                ))
                self.assertEqual(first["job"]["direction"], "pbip_to_pbix")
                self.assertEqual(second["job"]["export_mode"], "portable")
                self.assertEqual(len((await call(client, "list_jobs"))["jobs"]), 2)
                with self.assertRaises(DemoError) as error:
                    await submit(client, forward, "pbix_to_pbip")
                self.assertEqual(error.exception.code, "INPUT_DIRECTION")

    async def test_remote_batch_preenqueues_mixed_directions_and_selects_artifacts(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "run-remote-demo.py"
        spec = importlib.util.spec_from_file_location("batch_unit", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "batch.json"
            manifest.write_text(json.dumps([
                {"zip": str(root / "a.zip"), "output": str(root / "a.pbix"), "verification": str(root / "a.json")},
                {"pbix": str(root / "b.pbix"), "output": str(root / "b.pbip.zip"), "verification": str(root / "b.json"),
                 "export_mode": "portable"},
            ]))
            events = []

            async def queued(_client, path, direction, mode):
                events.append(("submit", path.suffix, mode))
                return {"job": {"job_id": str(len(events)), "source": {}}}

            async def wait(_client, job_id, timeout):
                self.assertEqual([event[0] for event in events[:2]], ["submit", "submit"])
                return {"ok": True, "job": {"direction": "pbip_to_pbix" if job_id == "1" else "pbix_to_pbip"}}

            download = AsyncMock(return_value={"unit": "not a real artifact"})
            with patch.object(module, "call", AsyncMock(return_value={"worker": {"ready": True}})), \
                 patch.object(module, "submit", side_effect=queued), patch.object(module, "wait_for_job", side_effect=wait), \
                 patch.object(module, "download", download):
                result = await module.batch(object(), argparse.Namespace(batch=manifest, timeout=1))
            self.assertTrue(result["ok"])
            self.assertEqual([call.args[-1] for call in download.call_args_list], ["pbix", "verification", "pbip", "verification"])
            self.assertEqual(events[1], ("submit", ".pbix", "portable"))
