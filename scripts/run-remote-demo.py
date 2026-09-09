"""Run the existing MCP conversion client through an authenticated SSH stdio transport."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import Client
from mcp.shared.exceptions import MCPError

from pbip_mcp.client import call, download, run_connected, submit, wait_for_job
from pbip_mcp.errors import DemoError
from pbip_mcp.ssh_transport import add_arguments, parameters


async def batch(client: Client, args: argparse.Namespace) -> dict:
    try:
        rows = json.loads(args.batch.read_text(encoding="utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise DemoError("CLIENT_BATCH_INPUT", "The batch manifest must be valid UTF-8 JSON.") from exc
    if not isinstance(rows, list) or not 1 <= len(rows) <= 3:
        raise DemoError("CLIENT_BATCH_INPUT", "The demo batch accepts 1..3 jobs within its artifact reservations.")
    outputs = set()
    for row in rows:
        if (not isinstance(row, dict) or not all(isinstance(row.get(key), str) for key in ("output", "verification"))
                or ("zip" in row) == ("pbix" in row) or not isinstance(row.get("zip", row.get("pbix")), str)):
            raise DemoError("CLIENT_BATCH_INPUT", "Each batch item requires exactly one zip/pbix, output and verification.")
        for key in ("output", "verification"):
            target = Path(row[key]).resolve()
            if target.exists() or str(target).casefold() in outputs:
                raise DemoError("CLIENT_OUTPUT_EXISTS", "Batch output exists or is duplicated; no jobs were submitted.")
            outputs.add(str(target).casefold())
    health = await call(client, "converter_status")
    if not health["worker"]["ready"]:
        raise DemoError("WORKER_NOT_READY", health["worker"]["message"])
    jobs = []
    for row in rows:
        try:
            queued = await submit(client, Path(row.get("zip", row.get("pbix"))),
                                  row.get("direction", "auto"), row.get("export_mode"))
        except DemoError as exc:
            return {
                "ok": False, "error": exc.as_dict(), "submitted_jobs": [job_id for job_id, _ in jobs],
                "recovery": "Earlier jobs remain durable; query their IDs and cancel only if still queued.",
            }
        job_id = queued["job"]["job_id"]
        jobs.append((job_id, row))
        print(json.dumps({"event": "submitted", "job_id": job_id, "source": queued["job"]["source"]}), file=sys.stderr, flush=True)
    results = []
    for job_id, row in jobs:
        try:
            result = await wait_for_job(client, job_id, args.timeout)
            result["downloads"] = [
                await download(client, job_id, Path(row["output"]),
                               "pbip" if result["job"]["direction"] == "pbix_to_pbip" else "pbix"),
                await download(client, job_id, Path(row["verification"]), "verification"),
            ]
        except DemoError as exc:
            result = {"ok": False, "job_id": job_id, "error": exc.as_dict()}
        results.append(result)
    return {"ok": all(result["ok"] for result in results), "results": results}


async def run(args: argparse.Namespace) -> dict:
    try:
        return await run_transport(args)
    except* MCPError as exc:
        raise DemoError(
            "CLIENT_TRANSPORT_ERROR",
            "SSH MCP connection failed. Check the authenticated tunnel before resubmitting; "
            "any previously submitted job remains queryable by its logged ID.",
        ) from exc


async def run_transport(args: argparse.Namespace) -> dict:
    failure: DemoError | OSError | TimeoutError
    async with Client(parameters(args), read_timeout_seconds=60) as client:
        try:
            result = await batch(client, args) if args.batch else await run_connected(client, args)
            result["client_transport"] = "external-machine SSH stdio"
            return result
        except (DemoError, OSError, TimeoutError) as exc:
            failure = exc
    raise failure


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    parser.add_argument("--action", choices=("convert", "status", "list", "submit", "job", "cancel", "download"), default="convert")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--zip", type=Path)
    source.add_argument("--pbix", type=Path)
    parser.add_argument("--direction", choices=("auto", "pbip_to_pbix", "pbix_to_pbip"), default="auto")
    parser.add_argument("--export-mode", choices=("definitions", "portable"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--kind", choices=("pbix", "pbip", "verification"), default="pbix")
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--wait-ready", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.timeout <= 3600 or not 0 <= args.wait_ready <= 120:
        parser.error("port, timeout, or wait-ready is outside the supported range")
    if args.target.startswith("-") or not args.identity.is_file() or not args.known_hosts.is_file():
        parser.error("Supply a valid SSH target, existing identity file, and existing known_hosts file")
    if not args.batch:
        required = {
            "convert": ("output", "verification"), "submit": (),
            "job": ("job_id",), "cancel": ("job_id",), "download": ("job_id", "output"), "status": (), "list": (),
        }[args.action]
        if args.action in ("convert", "submit") and not (args.zip or args.pbix):
            parser.error("This action requires --zip or --pbix")
        if any(getattr(args, name) is None for name in required):
            parser.error("This action requires: " + ", ".join(required))
    args.result_json.parent.mkdir(parents=True, exist_ok=True)
    with args.result_json.open("x", encoding="utf-8") as stream:
        try:
            result = asyncio.run(run(args))
            exit_code = 0 if result["ok"] else 2
        except DemoError as exc:
            result = {"ok": False, "error": exc.as_dict()}
            exit_code = 2
        except (OSError, TimeoutError) as exc:
            result = {"ok": False, "error": {"code": "CLIENT_IO_ERROR", "message": str(exc)}}
            exit_code = 2
        rendered = json.dumps(result, ensure_ascii=True, indent=2)
        stream.write(rendered + "\n")
        stream.flush()
        print(rendered)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
