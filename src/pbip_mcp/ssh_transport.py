import argparse
import os
from pathlib import Path

from mcp import StdioServerParameters


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", default="pbipdemo@127.0.0.1")
    parser.add_argument("--port", type=int, default=50022)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--host-key-alias", required=True)


def parameters(args: argparse.Namespace) -> StdioServerParameters:
    if not 1 <= args.port <= 65535 or args.target.startswith("-"):
        raise ValueError("Invalid SSH port or target.")
    if not args.identity.is_file() or not args.known_hosts.is_file():
        raise FileNotFoundError("Provide the existing approved identity and pinned known_hosts file.")
    return StdioServerParameters(
        command="ssh",
        env={"PROGRAMDATA": os.environ["PROGRAMDATA"]} if os.name == "nt" else None,
        args=[
            "-T", "-p", str(args.port), "-i", str(args.identity.resolve()),
            "-o", f"UserKnownHostsFile={args.known_hosts.resolve()}",
            "-o", f"HostKeyAlias={args.host_key_alias}",
            "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=45", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            args.target, r"C:\PBIPMCP\app\.venv\Scripts\python.exe",
            "-m", "pbip_mcp.server", "--data-dir", r"C:\PBIPMCP\data",
        ],
    )
