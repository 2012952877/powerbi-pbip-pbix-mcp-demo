import argparse
import asyncio
import base64
import hashlib
import json
import sys
import time
import os
import re
from contextlib import asynccontextmanager
from dataclasses import fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from .errors import DemoError
from .config import Limits


async def call(client: Client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(name, arguments or {})
    payload = result.structured_content
    if payload is None:
        text = next((item.text for item in result.content if isinstance(item, TextContent)), None)
        if text is not None:
            try:
                payload = json.loads(text)
            except (ValueError, RecursionError) as exc:
                raise DemoError("CLIENT_PROTOCOL", "MCP returned non-JSON tool content.") from exc
    if not isinstance(payload, dict):
        raise DemoError("CLIENT_PROTOCOL", "MCP did not return structured job data.")
    if result.is_error or payload.get("ok") is not True:
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
            raise DemoError(error["code"], error["message"])
        raise DemoError("CLIENT_TOOL_ERROR", "MCP reported a tool failure without a structured error.")
    return payload


async def submit(client: Client, path: Path, direction: str = "auto", export_mode: str | None = None) -> dict[str, Any]:
    health = await call(client, "converter_status")
    advertised = health["limits"]
    values = {field.name: advertised[field.name] for field in fields(Limits) if field.name in advertised}
    if any(type(value) is not int or value <= 0 for value in values.values()):
        raise DemoError("CLIENT_PROTOCOL", "The server did not advertise finite positive limits.")
    limits = Limits(**values)
    if not path.is_file() or not 1 <= path.stat().st_size <= limits.archive_bytes:
        raise DemoError("CLIENT_INPUT", "Choose a nonempty complete project ZIP within the server size limit.")
    with path.open("rb") as source:
        data = source.read(limits.archive_bytes + 1)
    digest = hashlib.sha256(data).hexdigest()
    from .inputs import validate_input
    _, direction = validate_input(data, path.name, direction, export_mode, limits)
    arguments = {"sha256": digest, "source_name": path.name}
    if direction == "pbix_to_pbip":
        arguments.update(pbix_base64=base64.b64encode(data).decode("ascii"), export_mode=export_mode or "definitions")
        tool = "submit_pbix"
    else:
        arguments.update(archive_base64=base64.b64encode(data).decode("ascii"))
        tool = "submit_project"
    result = await call(client, tool, arguments)
    if result["job"]["source"]["sha256"] != digest:
        raise DemoError("CLIENT_SOURCE_HASH", "Server acknowledgement did not preserve the submitted ZIP hash.")
    return result


async def wait_for_job(client: Client, job_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    previous: str | None = None
    while True:
        result = await call(client, "get_job", {"job_id": job_id})
        state = result["job"]["status"]
        if state != previous:
            print(json.dumps({"event": "job_status", "job_id": job_id, "status": state}), file=sys.stderr, flush=True)
            previous = state
        if state == "succeeded":
            return result
        if state in ("failed", "cancelled"):
            error = result["job"]["error"]
            raise DemoError(error["code"], f"{error['message']} Job: {job_id}")
        if state not in ("queued", "running"):
            raise DemoError("CLIENT_PROTOCOL", "Server returned an unknown job state.")
        if time.monotonic() >= deadline:
            raise DemoError("CLIENT_TIMEOUT", f"Polling deadline elapsed. The durable job can still be queried: {job_id}")
        await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))


async def download(client: Client, job_id: str, output: Path, kind: str = "pbix") -> dict[str, Any]:
    health = await call(client, "converter_status")
    job = (await call(client, "get_job", {"job_id": job_id}))["job"]
    if job["status"] != "succeeded":
        raise DemoError("ARTIFACT_NOT_READY", "The job has not completed Desktop conversion and reopening.")
    expected = next((artifact for artifact in job["artifacts"] if artifact["kind"] == kind), None)
    if expected is None:
        raise DemoError("ARTIFACT_NOT_FOUND", "Requested artifact is not in the job result.")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = output.open("xb")
    except FileExistsError as exc:
        raise DemoError("CLIENT_OUTPUT_EXISTS", "Output already exists. Choose a new path; nothing was overwritten.") from exc
    complete = False
    download_id = None
    try:
        if health.get("capabilities", {}).get("artifact_transfer_leases"):
            download_id = (await call(client, "begin_artifact_download", {"job_id": job_id, "kind": kind}))["download"]["download_id"]
        digest = hashlib.sha256()
        offset = 0
        with stream:
            while True:
                arguments = {"job_id": job_id, "kind": kind, "offset": offset,
                             "length": health["limits"].get("chunk_bytes", 262144)}
                if download_id is not None:
                    arguments["download_id"] = download_id
                result = await call(client, "get_artifact", arguments)
                chunk = result["artifact"]
                try:
                    data = base64.b64decode(chunk["data_base64"], validate=True)
                except (ValueError, KeyError) as exc:
                    raise DemoError("CLIENT_PROTOCOL", "Artifact chunk is not valid base64.") from exc
                if (
                    chunk["offset"] != offset or chunk["next_offset"] != offset + len(data)
                    or chunk["sha256"] != expected["sha256"] or chunk["total_bytes"] != expected["bytes"]
                    or offset + len(data) > expected["bytes"]
                    or (not data and not chunk["eof"])
                ):
                    raise DemoError("CLIENT_ARTIFACT_INTEGRITY", "Artifact chunk offsets, lengths or hashes do not agree.")
                stream.write(data)
                digest.update(data)
                offset += len(data)
                if chunk["eof"]:
                    break
            if offset != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
                raise DemoError("CLIENT_ARTIFACT_INTEGRITY", "Downloaded artifact failed its final size or SHA-256 check.")
            stream.flush()
            import os

            os.fsync(stream.fileno())
        complete = True
    finally:
        try:
            if download_id is not None:
                await call(client, "finish_artifact_download", {"job_id": job_id, "kind": kind, "download_id": download_id})
        finally:
            stream.close()
            if not complete:
                output.unlink(missing_ok=True)
    return {"kind": kind, "file": str(output.resolve()), "bytes": expected["bytes"], "sha256": expected["sha256"]}


def transport(data_dir: Path | None, url: str | None, *, allow_public_https: bool = False) -> StdioServerParameters | str:
    if url:
        parts = urlsplit(url)
        public_https = (
            allow_public_https and parts.scheme == "https" and parts.hostname
            and parts.netloc == parts.hostname and parts.path == "/mcp"
            and re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", parts.hostname)
        )
        if (parts.scheme not in ("http", "https")
                or (parts.hostname not in ("127.0.0.1", "localhost", "::1") and not public_https)
                or parts.username is not None or parts.password is not None or parts.fragment or parts.query):
            raise DemoError("CLIENT_LOOPBACK_ONLY", "Use a loopback MCP URL, or explicitly allow a public HTTPS DNS /mcp endpoint.")
        return url
    if data_dir is None:
        raise DemoError("CLIENT_CONFIG", "Supply --data-dir for stdio, or an explicitly configured --url.")
    return StdioServerParameters(
        command=sys.executable, args=["-m", "pbip_mcp.server", "--data-dir", str(data_dir.resolve())],
    )

@asynccontextmanager
async def authenticated_http_transport(url: str, credential: str, *, allow_public_https: bool = False):
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    transport(None, url, allow_public_https=allow_public_https)
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + credential},
                                  timeout=60, follow_redirects=False) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            yield streams


async def run(args: argparse.Namespace) -> dict[str, Any]:
    failure: DemoError | OSError | TimeoutError
    allow_public_https = getattr(args, "allow_public_https", False)
    target = transport(args.data_dir, args.url, allow_public_https=allow_public_https)
    if args.url:
        if args.token_file:
            from .auth import private_file
            private_file(args.token_file)
            credential = args.token_file.read_text(encoding="utf-8").strip()
        else:
            credential = os.environ.get("PBIP_ACCESS_TOKEN", "")
        if not credential:
            raise DemoError("AUTH_REQUIRED", "HTTP requires --token-file or PBIP_ACCESS_TOKEN; never put credentials in URLs.")
        target = authenticated_http_transport(args.url, credential, allow_public_https=allow_public_https)
    async with Client(target, read_timeout_seconds=60, cache=None) as client:
        try:
            return await run_connected(client, args)
        except (DemoError, OSError, TimeoutError) as exc:
            # Leave SDK task groups normally so they do not wrap the job error.
            failure = exc
    raise failure


async def run_connected(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "status":
        return await call(client, "converter_status")
    if args.action == "list":
        return await call(client, "list_jobs")
    if args.action == "submit":
        return await submit(client, getattr(args, "pbix", None) or args.zip,
                            getattr(args, "direction", "auto"), getattr(args, "export_mode", None))
    if args.action == "job":
        return await call(client, "get_job", {"job_id": args.job_id})
    if args.action == "cancel":
        return await call(client, "cancel_job", {"job_id": args.job_id})
    if args.action == "download":
        return {"ok": True, "download": await download(client, args.job_id, args.output, args.kind)}
    if args.output.exists() or args.verification.exists() or args.output.resolve() == args.verification.resolve():
        raise DemoError("CLIENT_OUTPUT_EXISTS", "A target output already exists. Choose new filenames before submitting.")
    ready_deadline = time.monotonic() + args.wait_ready
    while True:
        health = await call(client, "converter_status")
        if health["worker"]["ready"]:
            break
        if time.monotonic() >= ready_deadline:
            raise DemoError("WORKER_NOT_READY", health["worker"]["message"])
        await asyncio.sleep(1)
    queued = await submit(client, getattr(args, "pbix", None) or args.zip,
                          getattr(args, "direction", "auto"), getattr(args, "export_mode", None))
    job_id = queued["job"]["job_id"]
    print(json.dumps({"event": "submitted", "job_id": job_id}), file=sys.stderr, flush=True)
    result = await wait_for_job(client, job_id, args.timeout)
    result["downloads"] = [
        await download(client, job_id, args.output, "pbip" if queued["job"]["direction"] == "pbix_to_pbip" else "pbix"),
        await download(client, job_id, args.verification, "verification"),
    ]
    evidence = json.loads(args.verification.read_text(encoding="utf-8"))
    if evidence.get("converter") != "power-bi-desktop-ui" or evidence.get("reopened_in_fresh_desktop") is not True:
        raise DemoError("CLIENT_REOPEN_MISSING", "Job evidence does not confirm an actual fresh Desktop reopening.")
    result["verification"] = evidence
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Official SDK MCP client; no model or LLM is involved.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--data-dir", type=Path)
    group.add_argument("--url")
    parser.add_argument("--result-json", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--allow-public-https", action="store_true",
                        help="Explicitly allow an authenticated public HTTPS DNS /mcp endpoint; redirects stay disabled.")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    actions.add_parser("list")
    submit_parser = actions.add_parser("submit")
    inputs = submit_parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--zip", type=Path)
    inputs.add_argument("--pbix", type=Path)
    submit_parser.add_argument("--direction", choices=("auto", "pbip_to_pbix", "pbix_to_pbip"), default="auto")
    submit_parser.add_argument("--export-mode", choices=("definitions", "portable"))
    for command in ("job", "cancel"):
        sub = actions.add_parser(command)
        sub.add_argument("--job-id", required=True)
    download_parser = actions.add_parser("download")
    download_parser.add_argument("--job-id", required=True)
    download_parser.add_argument("--kind", choices=("pbix", "pbip", "verification"), default="pbix")
    download_parser.add_argument("--output", type=Path, required=True)
    convert_parser = actions.add_parser("convert")
    inputs = convert_parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--zip", type=Path)
    inputs.add_argument("--pbix", type=Path)
    convert_parser.add_argument("--direction", choices=("auto", "pbip_to_pbix", "pbix_to_pbip"), default="auto")
    convert_parser.add_argument("--export-mode", choices=("definitions", "portable"))
    convert_parser.add_argument("--output", required=True, type=Path)
    convert_parser.add_argument("--verification", required=True, type=Path)
    convert_parser.add_argument("--timeout", type=int, default=900)
    convert_parser.add_argument("--wait-ready", type=int, default=30)
    args = parser.parse_args()
    if args.action == "convert" and (not 1 <= args.timeout <= 3600 or not 0 <= args.wait_ready <= 120):
        parser.error("timeout must be 1..3600 seconds and wait-ready must be 0..120")
    result_stream = None
    if args.result_json:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        try:
            result_stream = args.result_json.open("x", encoding="utf-8")
        except FileExistsError:
            parser.error("--result-json already exists; choose a new filename")
    try:
        result = asyncio.run(run(args))
        exit_code = 0
    except DemoError as exc:
        result = {"ok": False, "error": exc.as_dict()}
        exit_code = 2
    except (OSError, TimeoutError) as exc:
        result = {"ok": False, "error": {"code": "CLIENT_IO_ERROR", "message": str(exc)}}
        exit_code = 2
    finally:
        if result_stream is not None and "result" not in locals():
            result_stream.close()
    rendered = json.dumps(result, ensure_ascii=True, indent=2)
    print(rendered)
    if result_stream is not None:
        with result_stream:
            result_stream.write(rendered + "\n")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
