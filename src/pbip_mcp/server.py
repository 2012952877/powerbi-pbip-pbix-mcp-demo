import argparse
import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import __version__
from .archive import ValidatedArchive, decode_archive
from .config import Config
from .errors import DemoError
from .storage import JobStore
from .identity import LOCAL_OPERATOR, Principal
from .inputs import ValidatedPBIX

LOG = logging.getLogger(__name__)


def _reply(operation: Callable[[], Any]) -> CallToolResult:
    try:
        payload = {"ok": True, **operation()}
        failed = False
    except DemoError as exc:
        payload = {"ok": False, "error": exc.as_dict()}
        failed = True
    except (OSError, sqlite3.Error) as exc:
        LOG.exception("Local storage operation failed")
        payload = {"ok": False, "error": {"code": "STORAGE_ERROR", "message": "Local storage failed; an operator must inspect controller logs."}}
        failed = True
    return CallToolResult(
        is_error=failed,
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=True))],
        structured_content=payload,
    )


def create_server(config: Config, *, principal_provider: Callable[[], Principal] = lambda: LOCAL_OPERATOR) -> MCPServer:
    store = JobStore(config)
    server = MCPServer(
        "PBIP Desktop converter", version=__version__,
        instructions=(
            "Submit a COMPLETE trusted synthetic PBIP ZIP, not just a .pbip pointer. "
            "No LLM or cloud publishing is used. A separate unlocked interactive Windows worker must perform "
            "Power BI Desktop Save As and reopen the PBIX. Never submit credentials or customer data."
        ),
        log_level="WARNING",
    )
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

    @server.tool(annotations=read_only)
    def converter_status() -> CallToolResult:
        """Read honest worker readiness and finite input/output limits; the controller cannot start Desktop."""
        return _reply(lambda: {
            "worker": store.worker_status(),
            "queue": store.queue_status(principal_provider()),
            "capabilities": {"directions": ["pbip_to_pbix", "pbix_to_pbip"], "export_modes": ["definitions", "portable"],
                             "artifact_transfer_leases": True},
            "limits": {
                "archive_bytes": config.limits.archive_bytes,
                "uncompressed_bytes": config.limits.uncompressed_bytes,
                "member_bytes": config.limits.member_bytes,
                "metadata_bytes": config.limits.metadata_bytes,
                "path_characters": config.limits.path_characters,
                "path_depth": config.limits.path_depth,
                "file_count": config.limits.file_count,
                "compression_ratio": config.limits.compression_ratio,
                "artifact_bytes": config.limits.artifact_bytes,
                "chunk_bytes": config.limits.chunk_bytes,
                "queue_seconds": config.limits.queue_seconds,
                "conversion_seconds": config.limits.conversion_seconds,
            },
        })

    @server.tool(annotations=write)
    def submit_project(archive_base64: str, sha256: str | None = None, source_name: str = "source.zip") -> CallToolResult:
        """Durably queue one complete local-model PBIP ZIP; returns an opaque job ID and source hash."""
        def operation() -> dict[str, Any]:
            archive = ValidatedArchive(decode_archive(archive_base64, config.limits), config.limits)
            return {"job": store.submit(archive, sha256, principal=principal_provider(), source_name=source_name), "worker": store.worker_status()}
        return _reply(operation)

    @server.tool(annotations=write)
    def submit_pbix(pbix_base64: str, sha256: str | None = None, export_mode: Literal["definitions", "portable"] = "definitions",
                    source_name: str = "source.pbix") -> CallToolResult:
        """Queue real Desktop PBIX Save As PBIP, fresh reopen, and an allowlisted PBIP ZIP export."""
        def operation():
            source = ValidatedPBIX(decode_archive(pbix_base64, config.limits), config.limits)
            return {"job": store.submit(source, sha256, principal=principal_provider(), source_name=source_name,
                                       direction="pbix_to_pbip", export_mode=export_mode), "worker": store.worker_status()}
        return _reply(operation)

    @server.tool(annotations=read_only)
    def list_jobs() -> CallToolResult:
        """List only this authenticated user's latest tasks."""
        return _reply(lambda: {"jobs": store.list_jobs(principal=principal_provider())})

    @server.tool(annotations=read_only)
    def get_job(job_id: str) -> CallToolResult:
        """Read queued/running/succeeded/failed/cancelled state, explicit errors and artifact metadata."""
        return _reply(lambda: {"job": store.get(job_id, principal=principal_provider()), "worker": store.worker_status()})

    @server.tool(annotations=write)
    def cancel_job(job_id: str) -> CallToolResult:
        """Cancel a queued job only. Running GUI work remains subject to its finite timeout."""
        return _reply(lambda: {"job": store.cancel(job_id, principal=principal_provider())})

    @server.tool(annotations=read_only)
    def get_artifact(
        job_id: str, kind: Literal["pbix", "pbip", "verification"] = "pbix", offset: int = 0,
        length: int = 262144,
        download_id: str | None = None,
    ) -> CallToolResult:
        """Read a bounded base64 artifact chunk by opaque job ID, never by filesystem path."""
        return _reply(lambda: {"artifact": store.artifact_chunk(job_id, kind, offset, length, principal=principal_provider(),
                                                               download_id=download_id)})

    @server.tool(annotations=write)
    def begin_artifact_download(job_id: str, kind: Literal["pbix", "pbip", "verification"] = "pbix") -> CallToolResult:
        """Start an owner/job/artifact-bound transfer lease, renewed by each chunk and idle-expiring after 300 seconds."""
        return _reply(lambda: {"download": store.begin_artifact_download(job_id, kind, principal=principal_provider())})

    @server.tool(annotations=write)
    def finish_artifact_download(job_id: str, kind: Literal["pbix", "pbip", "verification"],
                                 download_id: str) -> CallToolResult:
        """Release only this authenticated user's matching transfer, including after a failed download."""
        return _reply(lambda: store.finish_artifact_download(job_id, kind, download_id, principal=principal_provider()))

    @server.resource("pbip://jobs/{job_id}", mime_type="application/json")
    def job_resource(job_id: str) -> str:
        """Small job metadata only. PBIX bytes are retrieved with the bounded get_artifact tool."""
        result = _reply(lambda: {"job": store.get(job_id, principal=principal_provider()), "worker": store.worker_status()})
        return json.dumps(result.structured_content, ensure_ascii=True)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Durable MCP controller; does not operate the desktop.")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--auth-config", type=Path)
    parser.add_argument("--limits-config", type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    config = Config.load(args.data_dir.resolve(), args.limits_config)
    if args.transport == "stdio":
        create_server(config).run(transport="stdio")
    else:
        if args.auth_config is None:
            parser.error("HTTP requires --auth-config with individual pilot identities; anonymous HTTP is disabled")
        import uvicorn
        from .portal import create_portal

        uvicorn.run(create_portal(config, args.auth_config, origin=f"http://127.0.0.1:{args.port}"),
                    host="127.0.0.1", port=args.port, access_log=False, limit_concurrency=20)


if __name__ == "__main__":
    main()
