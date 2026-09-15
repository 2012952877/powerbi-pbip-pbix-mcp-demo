"""Same-origin authenticated pilot UI and MCP over the same durable JobStore."""

import argparse
import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import re
import sqlite3
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route, Mount

from .archive import _member_name
from .auth import PilotAuth, private_file
from .config import Config
from .errors import DemoError
from .identity import current_principal, authenticated_principal
from .inputs import ARTIFACT_FILENAMES, DIRECTIONS, EXPORT_MODES, folder_source_name, validate_input
from .storage import JobStore

COOKIE = "pbip_session"


def error_response(error: DemoError):
    code = error.code
    status = 401 if code == "AUTH_REQUIRED" else (
        403 if code in ("CSRF", "ORIGIN", "ADMIN_REQUIRED") else (
            404 if code in ("JOB_NOT_FOUND", "ARTIFACT_NOT_FOUND") else (
                410 if code == "ARTIFACT_EXPIRED" else (
                    413 if code in ("BODY_SIZE", "ARCHIVE_SIZE") else (
                        429 if code in ("AUTH_RATE", "SUBMISSION_RATE", "USER_QUEUE_FULL", "QUEUE_FULL") else 400
                    )
                )
            )
        )
    )
    return JSONResponse({"ok": False, "error": error.as_dict()}, status_code=status)


def capabilities():
    return {
        "directions": list(DIRECTIONS), "export_modes": list(EXPORT_MODES),
        "folder_upload": True, "authentication": "pilot-token", "enterprise_sso": False,
        "untrusted_code_sandbox": False,
    }


class PilotBoundary:
    def __init__(self, app, *, auth: PilotAuth, origin: str, limit: int, upload_seconds: int):
        self.app, self.auth, self.origin, self.limit = app, auth, origin, limit
        self.upload_slots = asyncio.Semaphore(2)
        self.login_attempts = []
        self.upload_seconds = upload_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        token = None
        try:
            if headers.get("host") != urlsplit(self.origin).netloc:
                raise DemoError("ORIGIN", "Use the configured same-origin endpoint.")
            origin = headers.get("origin")
            if origin and origin != self.origin:
                raise DemoError("ORIGIN", "Cross-origin requests are not permitted.")
            path, method = scope["path"], scope["method"]
            request = Request(scope)
            if path.startswith("/mcp"):
                principal = self.auth.bearer(headers.get("authorization"))
                token = current_principal.set(principal)
            elif path.startswith("/api/"):
                if path == "/api/session" and method == "POST":
                    now = time.monotonic()
                    self.login_attempts = [at for at in self.login_attempts if at > now - 60]
                    if len(self.login_attempts) >= 30:
                        raise DemoError("AUTH_RATE", "Too many sign-in attempts; wait one minute.")
                    self.login_attempts.append(now)
                else:
                    principal, csrf = self.auth.session(request.cookies.get(COOKIE))
                    if method not in ("GET", "HEAD") and not hmac.compare_digest(headers.get("x-csrf-token", "").encode(), csrf.encode()):
                        raise DemoError("CSRF", "A valid session CSRF token is required.")
                    token = current_principal.set(principal)
            if method in ("POST", "PUT", "PATCH"):
                limit = self.limit if path in ("/api/jobs", "/mcp") else 4096
                async with self.upload_slots:
                    body = bytearray()
                    deadline = time.monotonic() + self.upload_seconds
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise DemoError("UPLOAD_TIMEOUT", "The request body exceeded the upload deadline.")
                        message = await asyncio.wait_for(receive(), timeout=min(30, remaining))
                        if message["type"] == "http.disconnect":
                            raise DemoError("UPLOAD_INTERRUPTED", "The upload connection was closed.")
                        body.extend(message.get("body", b""))
                        if len(body) > limit:
                            raise DemoError("BODY_SIZE", "Actual request body exceeds the configured limit.")
                        if not message.get("more_body", False):
                            break
                    delivered = False

                    async def bounded_receive():
                        nonlocal delivered
                        if not delivered:
                            delivered = True
                            return {"type": "http.request", "body": bytes(body), "more_body": False}
                        return await receive()

                    await self.app(scope, bounded_receive, send)
            else:
                await self.app(scope, receive, send)
        except DemoError as exc:
            await error_response(exc)(scope, receive, send)
        except TimeoutError:
            await error_response(DemoError("UPLOAD_TIMEOUT", "The request body timed out."))(scope, receive, send)
        finally:
            if token is not None:
                current_principal.reset(token)


def create_portal(config: Config, auth_config: Path, *, origin: str = "http://127.0.0.1:8765",
                  allow_public_origin: bool = False):
    from .server import create_server

    parts = urlsplit(origin)
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if allow_public_origin and loopback:
        raise DemoError("PORTAL_ORIGIN", "A public HTTPS origin must be a non-loopback DNS name.")
    if (parts.scheme not in ("http", "https") or parts.path or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None):
        raise DemoError("PORTAL_ORIGIN", "Supply an HTTP(S) origin without credentials, path, query or fragment.")
    if not loopback:
        if not allow_public_origin:
            raise DemoError("PORTAL_LOOPBACK", "A public origin requires explicit operator opt-in.")
        if (parts.scheme != "https" or not parts.hostname
                or origin != f"https://{parts.hostname}"
                or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", parts.hostname)):
            raise DemoError("PORTAL_ORIGIN", "The public origin must be a canonical HTTPS DNS origin on port 443.")
    if not config.data_dir.exists():
        config.data_dir.mkdir(parents=True)
        private_file(config.data_dir, create=True)
    private_file(config.data_dir)
    store = JobStore(config)
    auth = PilotAuth(auth_config, config.data_dir, config.limits.session_seconds)
    server = create_server(config, principal_provider=authenticated_principal)
    mcp_app = server.streamable_http_app(
        stateless_http=True, json_response=True, host=parts.hostname,
        max_request_body_size=config.limits.base64_characters + config.limits.upload_overhead_bytes,
    )

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    async def session(request):
        if request.method == "POST":
            user, session_id, csrf = auth.login(request.headers.get("authorization"))
            response = JSONResponse({"ok": True, "user": user.as_dict(), "csrf_token": csrf})
            response.set_cookie(COOKIE, session_id, max_age=config.limits.session_seconds,
                                httponly=True, secure=parts.scheme == "https", samesite="strict", path="/")
        elif request.method == "DELETE":
            auth.logout(request.cookies[COOKIE])
            response = JSONResponse({"ok": True})
            response.delete_cookie(COOKIE, path="/", httponly=True,
                                   secure=parts.scheme == "https", samesite="strict")
        else:
            user, csrf = auth.session(request.cookies.get(COOKIE))
            response = JSONResponse({"ok": True, "user": user.as_dict(), "csrf_token": csrf})
        response.headers["Cache-Control"] = "no-store"
        return response

    async def status(request):
        return JSONResponse({"ok": True, "worker": store.worker_status(),
                             "queue": store.queue_status(authenticated_principal()),
                             "limits": asdict(config.limits), "capabilities": capabilities()})

    async def readiness_probe(request):
        worker = await asyncio.to_thread(store.worker_status)
        ready = worker["ready"] is True and worker["state"] in ("idle", "busy")
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503,
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def jobs(request):
        principal = authenticated_principal()
        if request.method == "GET":
            return JSONResponse({"ok": True, "jobs": store.list_jobs(principal=principal)})
        async with request.form(max_files=config.limits.file_count, max_fields=3,
                                max_part_size=config.limits.archive_bytes) as form:
            if set(form) - {"file", "project_files", "direction", "export_mode"}:
                raise DemoError("INPUT_FIELDS", "Only file/project_files, direction and export_mode are accepted.")
            for key in ("direction", "export_mode", "file"):
                if len(form.getlist(key)) > 1:
                    raise DemoError("INPUT_FIELDS", "Duplicate single-value upload fields are not allowed.")
            direction, mode = form.get("direction", "auto"), form.get("export_mode") or None
            if not isinstance(direction, str) or (mode is not None and not isinstance(mode, str)):
                raise DemoError("INPUT_FIELDS", "Direction and export mode must be text.")
            single, folder = form.get("file"), form.getlist("project_files")
            folder_paths = []
            if bool(single) == bool(folder):
                raise DemoError("INPUT_FORMAT", "Upload exactly one file or one complete project folder.")
            if single:
                if not isinstance(single, UploadFile):
                    raise DemoError("INPUT_FORMAT", "file must be an uploaded file.")
                name = single.filename or ""
                data = await single.read(config.limits.archive_bytes + 1)
            else:
                stream, expanded = io.BytesIO(), 0
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for upload in folder:
                        if not isinstance(upload, UploadFile):
                            raise DemoError("INPUT_FORMAT", "project_files must contain uploaded files.")
                        name = _member_name(upload.filename or "", config.limits)
                        folder_paths.append(name)
                        if name.endswith(".pbix"):
                            raise DemoError("INPUT_FORMAT", "Folder uploads require a complete PBIP project.")
                        content = await upload.read(config.limits.member_bytes + 1)
                        expanded += len(content)
                        if len(content) > config.limits.member_bytes or expanded > config.limits.uncompressed_bytes:
                            raise DemoError("BODY_SIZE", "Project folder exceeds the upload budget.")
                        archive.writestr(name, content)
                data, name = stream.getvalue(), "uploaded-project.zip"
            validated, direction = await asyncio.to_thread(validate_input, data, name, direction, mode, config.limits)
            source_name = folder_source_name(folder_paths, validated.project.pointer) if folder else name
            job = await asyncio.to_thread(store.submit, validated, principal=principal, source_name=source_name,
                                          direction=direction, export_mode=mode, source_is_folder=bool(folder))
        return JSONResponse({"ok": True, "job": job}, status_code=201)

    async def job(request):
        operation = store.cancel if request.method == "POST" else store.get
        return JSONResponse({"ok": True, "job": operation(request.path_params["job_id"], principal=authenticated_principal())})

    async def artifact(request):
        job_id, kind = request.path_params["job_id"], request.path_params["kind"]
        principal = authenticated_principal()
        context = store.artifact_file(job_id, kind, principal=principal)
        stream, info, lease = context.__enter__()

        async def chunks():
            while data := stream.read(config.limits.chunk_bytes):
                store.renew_download(lease)
                yield data

        class LeasedDownload(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    context.__exit__(None, None, None)

        filename = info["filename"]
        fallback = filename if filename.isascii() else ARTIFACT_FILENAMES[kind]
        disposition = f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'
        return LeasedDownload(chunks(), media_type=info.get("media_type", "application/octet-stream"),
                                 headers={"Content-Disposition": disposition,
                                          "Content-Length": str(info["bytes"]), "Cache-Control": "no-store",
                                          "X-Content-Type-Options": "nosniff"})

    async def index(request):
        path = Path(__file__).parent / "web_ui" / "index.html"
        if not path.is_file():
            raise DemoError("UI_NOT_INSTALLED", "The portal UI was not included in this installation.")
        content = path.read_text(encoding="utf-8")
        policies = []
        for kind in ("script", "style"):
            hashes = [
                "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"
                for text in re.findall(rf"<{kind}(?:\s[^>]*)?>(.*?)</{kind}>", content, re.S | re.I)
            ]
            policies.append(f"{kind}-src " + (" ".join(hashes) if hashes else "'none'"))
        return HTMLResponse(content, headers={
            "Content-Security-Policy": "default-src 'none'; connect-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; " + "; ".join(policies),
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        })

    async def demo_error(request, exc):
        return error_response(exc)

    async def io_error(request, exc):
        return JSONResponse({"ok": False, "error": {"code": "STORAGE_ERROR", "message": "Local storage failed; contact the operator."}}, status_code=500)

    async def unexpected_error(request, exc):
        import logging
        logging.getLogger(__name__).error("Portal request failed: %s", type(exc).__name__)
        return JSONResponse({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "The request failed; contact the operator."}}, status_code=500)

    async def http_error(request, exc):
        return JSONResponse({"ok": False, "error": {"code": "HTTP_ERROR", "message": "Request is invalid or route unavailable."}}, status_code=exc.status_code)

    from starlette.exceptions import HTTPException

    app = Starlette(routes=[
        Route("/", index), Route("/api/session", session, methods=["GET", "POST", "DELETE"]),
        Route("/readyz", readiness_probe),
        Route("/api/status", status), Route("/api/jobs", jobs, methods=["GET", "POST"]),
        Route("/api/jobs/{job_id}", job), Route("/api/jobs/{job_id}/cancel", job, methods=["POST"]),
        Route("/api/jobs/{job_id}/artifacts/{kind}", artifact),
        Mount("/", app=mcp_app),
    ], lifespan=lifespan, exception_handlers={
        DemoError: demo_error, OSError: io_error, sqlite3.Error: io_error, HTTPException: http_error, Exception: unexpected_error,
    })
    return PilotBoundary(app, auth=auth, origin=origin,
                         limit=config.limits.base64_characters + config.limits.upload_overhead_bytes,
                         upload_seconds=config.limits.upload_seconds)


def main():
    parser = argparse.ArgumentParser(description="Authenticated pilot portal; loopback unless a public HTTPS origin is explicitly configured.")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--auth-config", required=True, type=Path)
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--limits-config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--public-origin")
    parser.add_argument("--allow-public-https", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    if bool(args.public_origin) != args.allow_public_https:
        parser.error("--public-origin and --allow-public-https must be explicitly supplied together")
    try:
        bind = ipaddress.ip_address(args.host)
    except ValueError:
        parser.error("host must be a literal loopback or RFC1918 IPv4 address")
    private_bind = bind.version == 4 and any(bind in ipaddress.ip_network(network)
                                            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    if not bind.is_loopback and not (args.public_origin and private_bind):
        parser.error("A non-loopback bind requires --public-origin and a private RFC1918 IPv4 address")
    if args.public_origin and not private_bind:
        parser.error("A public HTTPS portal must bind an explicit RFC1918 IPv4 address behind the gateway")
    import uvicorn

    uvicorn.run(create_portal(Config.load(args.data_dir.resolve(), args.limits_config), args.auth_config.resolve(),
                             origin=args.public_origin or f"http://127.0.0.1:{args.port}",
                             allow_public_origin=bool(args.public_origin)), host=args.host, port=args.port,
                proxy_headers=False, access_log=False, limit_concurrency=20)


if __name__ == "__main__":
    main()
