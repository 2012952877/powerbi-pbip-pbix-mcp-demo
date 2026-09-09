import argparse
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from mcp.shared.exceptions import MCPError
from pbip_mcp.errors import DemoError

from pbip_mcp.ssh_transport import parameters


class SSHParametersTests(unittest.TestCase):
    def test_only_programdata_is_added_to_sdk_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "identity"
            hosts = Path(directory) / "hosts"
            identity.write_text("unit-only not a usable key", encoding="utf-8")
            hosts.write_text("unit-only not a host key", encoding="utf-8")
            args = argparse.Namespace(identity=identity, known_hosts=hosts, target="demo@127.0.0.1",
                                      port=50024, host_key_alias="unit-host")
            with patch.dict(os.environ, {"PROGRAMDATA": r"C:\ProgramData", "AZURE_CLIENT_SECRET": "unit-value-not-a-secret"}):
                result = parameters(args)
            self.assertIn("StrictHostKeyChecking=yes", result.args)
            self.assertIn("IdentitiesOnly=yes", result.args)
            if os.name == "nt":
                self.assertEqual(result.env, {"PROGRAMDATA": r"C:\ProgramData"})
            else:
                self.assertIsNone(result.env)


class RemoteClientFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_transport_exception_group_remains_a_typed_failure(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "run-remote-demo.py"
        spec = importlib.util.spec_from_file_location("remote_demo_unit", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        async def fail(_args):
            raise ExceptionGroup("unit transport failure", [MCPError(code=-32000, message="unit closed connection")])

        with patch.object(module, "run_transport", side_effect=fail), self.assertRaises(DemoError) as error:
            await module.run(argparse.Namespace())
        self.assertEqual(error.exception.code, "CLIENT_TRANSPORT_ERROR")
