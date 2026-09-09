"""Run explicitly selected real MCP scenarios against the dedicated demo queue."""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import struct
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from mcp import Client

from pbip_mcp.client import call, download, run_connected, submit, wait_for_job
from pbip_mcp.errors import DemoError
from pbip_mcp.ssh_transport import add_arguments, parameters
from pbip_mcp.storage import atomic_json
from pbip_mcp.synthetic import fixture_files


def archive(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in files.items():
            target.writestr(name, content)
    return stream.getvalue()


class Recorder:
    def __init__(self, output: Path):
        self.path = output
        self.document = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {
            "scope": "Dedicated existing demo VM; real MCP calls and explicitly identified real Desktop conversions.",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "cases": [],
        }

    def add(self, case_id: str, expected: str, observed: dict, passed: bool, started: float, **details):
        record = {
            "case_id": case_id, "timestamp": datetime.now(timezone.utc).isoformat(),
            "expected": expected, "observed": observed, "status": "passed" if passed else "failed",
            "duration_seconds": round(time.monotonic() - started, 3), "screenshot_paths": [],
            **details,
        }
        self.document["cases"].append(record)
        atomic_json(self.path, self.document)
        print(json.dumps(record, ensure_ascii=True), flush=True)


async def negative(client: Client, recorder: Recorder, case: str, data: bytes, expected: str):
    start = time.monotonic()
    result = await client.call_tool("submit_project", {
        "archive_base64": base64.b64encode(data).decode("ascii"), "sha256": hashlib.sha256(data).hexdigest(),
    })
    payload = result.structured_content
    passed = result.is_error and isinstance(payload, dict) and payload.get("error", {}).get("code") == expected
    recorder.add(
        case, expected, payload or {"unstructured": True}, passed, start,
        execution_kind="real external MCP validation; no Desktop launch",
        input_schema={"zip_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        actual_command="MCP submit_project(archive_base64=<tiny probe>, sha256=<input hash>)",
        boundary="Reject before accepting a durable GUI job.",
    )


async def execute(client: Client, args, recorder: Recorder):
    if args.mode == "protocol":
        if (await call(client, "converter_status"))["worker"]["ready"]:
            raise DemoError("SCENARIO_PRECONDITION", "Stop the owned worker before the protocol-only probe batch.")
        files = fixture_files()
        await negative(client, recorder, "reject-malformed-zip", b"not a zip", "INVALID_ZIP")
        await negative(client, recorder, "reject-incomplete-project", archive({"Synthetic.pbip": files["Synthetic.pbip"]}), "INCOMPLETE_PROJECT")
        await negative(client, recorder, "reject-ambiguous-entry", archive({**files, "second.pbip": files["Synthetic.pbip"]}), "AMBIGUOUS_PROJECT")
        await negative(client, recorder, "reject-path-traversal", archive({"../outside.txt": b"safe tiny probe"}), "UNSAFE_ARCHIVE_PATH")
        await negative(client, recorder, "reject-compression-ratio", archive({"tiny-bomb.txt": b"0" * 100_000}), "ZIP_BOMB")
        size = bytearray(archive({"oversize-metadata-only.txt": b"x"}))
        central = size.index(b"PK\x01\x02")
        struct.pack_into("<I", size, central + 24, 64 * 1024 * 1024 + 1)
        await negative(client, recorder, "reject-declared-member-size", bytes(size), "ARCHIVE_MEMBER_SIZE")
        modified = fixture_files()
        modified["Synthetic.SemanticModel/definition/tables/Sales.tmdl"] += b"\n\tannotation untrusted_change = true\n"
        start = time.monotonic()
        queued = await call(client, "submit_project", {"archive_base64": base64.b64encode(archive(modified)).decode("ascii")})
        job_id = queued["job"]["job_id"]
        result = await call(client, "get_job", {"job_id": job_id})
        recorder.add("modified-fixture-not-refresh-trusted", "synthetic_fixture=false", result,
                     not result["job"]["project"]["synthetic_fixture"], start,
                     run_id=job_id, execution_kind="real MCP metadata validation",
                     boundary="Modified embedded M is never granted automatic fixture refresh.")
        if result["job"]["status"] == "queued":
            await call(client, "cancel_job", {"job_id": job_id})
    elif args.mode == "offline-cancel":
        start = time.monotonic()
        health = await call(client, "converter_status")
        recorder.add("worker-offline-status", "ready=false", health, not health["worker"]["ready"], start,
                     execution_kind="real MCP status", precondition="Owned persistent worker stopped")
        if health["worker"]["ready"]:
            raise DemoError("SCENARIO_PRECONDITION", "Stop the owned worker before the queued cancellation scenario.")
        start = time.monotonic()
        queued = await submit(client, args.zip)
        job_id = queued["job"]["job_id"]
        cancelled = await call(client, "cancel_job", {"job_id": job_id})
        recorder.add("cancel-queued-job", "queued -> cancelled; no artifacts", cancelled,
                     cancelled["job"]["status"] == "cancelled" and not cancelled["job"]["artifacts"], start,
                     run_id=job_id, execution_kind="real MCP queued cancellation",
                     actual_command="submit_project(complete baseline ZIP); cancel_job(job_id)",
                     boundary="Only queued jobs may be cancelled through MCP.")
        start = time.monotonic()
        args.action = "convert"
        args.output = args.outputs / "offline-should-not-exist.pbix"
        args.verification = args.outputs / "offline-should-not-exist.verification.json"
        args.wait_ready = 0
        try:
            await run_connected(client, args)
        except DemoError as exc:
            recorder.add("offline-convert-refused", "WORKER_NOT_READY; no output", {"error": exc.as_dict()},
                         exc.code == "WORKER_NOT_READY" and not args.output.exists(), start,
                         execution_kind="real external client plus MCP preflight")
        else:
            raise DemoError("SCENARIO_FAILED", "Offline convert unexpectedly succeeded.")
    elif args.mode == "collision":
        start = time.monotonic()
        expected_hash = hashlib.sha256(args.existing.read_bytes()).hexdigest()
        args.action = "convert"
        args.output = args.existing
        args.verification = args.outputs / "collision-should-not-exist.json"
        args.wait_ready = 0
        try:
            await run_connected(client, args)
        except DemoError as exc:
            observed_hash = hashlib.sha256(args.existing.read_bytes()).hexdigest()
            recorder.add("existing-local-output-refused", "CLIENT_OUTPUT_EXISTS; original hash unchanged",
                         {"error": exc.as_dict(), "before_sha256": expected_hash, "after_sha256": observed_hash},
                         exc.code == "CLIENT_OUTPUT_EXISTS" and expected_hash == observed_hash, start,
                         execution_kind="real external client collision guard; no GUI job submitted",
                         output_path=str(args.existing))
        else:
            raise DemoError("SCENARIO_FAILED", "Existing output was not rejected.")
    elif args.mode == "invalid-tmdl":
        start = time.monotonic()
        files = fixture_files()
        files["Synthetic.SemanticModel/definition/model.tmdl"] += b"\n\tref table InvalidSyntax\n"
        data = archive(files)
        queued = await call(client, "submit_project", {"archive_base64": base64.b64encode(data).decode("ascii")})
        job_id = queued["job"]["job_id"]
        print(json.dumps({"event": "invalid_tmdl_submitted", "job_id": job_id}), flush=True)
        try:
            await wait_for_job(client, job_id, 180)
        except DemoError:
            result = await call(client, "get_job", {"job_id": job_id})
            expected_codes = {"DESKTOP_DIALOG_UNSUPPORTED", "DESKTOP_REPORTED_ERROR"}
            observed_code = (result["job"].get("error") or {}).get("code")
            recorder.add("invalid-tmdl-real-desktop-failure", "Desktop reports an open/parser failure; no successful artifact", result,
                         result["job"]["status"] == "failed" and not result["job"]["artifacts"]
                         and observed_code in expected_codes, start,
                         run_id=job_id, input_schema={"zip_bytes": len(data), "synthetic_fixture": False},
                         execution_kind="real Desktop open failure through external MCP",
                         boundary="Invalid TMDL is not silently repaired or renamed to PBIX.")
        else:
            raise DemoError("SCENARIO_FAILED", "Invalid TMDL was accepted as a successful conversion.")
    elif args.mode == "recovery-status":
        start = time.monotonic()
        result = await call(client, "get_job", {"job_id": args.job_id})
        health = await call(client, "converter_status")
        recorder.add("interrupted-worker-recovery", "old job failed WORKER_INTERRUPTED; worker ready",
                     {"job": result["job"], "worker": health["worker"]},
                     result["job"]["status"] == "failed" and result["job"]["error"]["code"] == "WORKER_INTERRUPTED"
                     and health["worker"]["ready"], start, run_id=args.job_id,
                     execution_kind="real restart/recovery after controlled owned-worker interruption",
                     boundary="Old running job is preserved as failed; recovery does not manufacture its output.")
    elif args.mode == "session-loss-status":
        start = time.monotonic()
        result = await call(client, "get_job", {"job_id": args.job_id})
        artifact = await client.call_tool("get_artifact", {"job_id": args.job_id, "kind": "pbix", "offset": 0})
        recorder.add(
            "controller-restart-preserves-session-failure",
            "failed job still queryable; artifact download isError",
            {"job": result["job"], "artifact_response": artifact.structured_content},
            result["job"]["status"] == "failed" and not result["job"]["artifacts"] and artifact.is_error,
            start, run_id=args.job_id,
            execution_kind="real new SSH stdio MCP controller after previous controller exited",
            boundary="Neither controller restart nor an artifact request changes a failed GUI job into success.",
        )


async def run(args):
    recorder = Recorder(args.results)
    async with Client(parameters(args), read_timeout_seconds=60) as client:
        for mode in args.mode:
            selected = argparse.Namespace(**{**vars(args), "mode": mode})
            await execute(client, selected, recorder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    parser.add_argument("--mode", nargs="+", choices=("protocol", "offline-cancel", "collision", "invalid-tmdl", "recovery-status", "session-loss-status"), required=True)
    parser.add_argument("--zip", type=Path, default=Path("sample") / "Synthetic.zip")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--existing", type=Path)
    parser.add_argument("--job-id")
    args = parser.parse_args()
    if "collision" in args.mode and args.existing is None:
        parser.error("--existing is required for collision")
    if any(mode in args.mode for mode in ("recovery-status", "session-loss-status")) and args.job_id is None:
        parser.error("--job-id is required for recovery/session-loss status")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
