import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .archive import ValidatedArchive
from .config import Config
from .errors import DemoError
from .identity import LOCAL_OPERATOR, Principal
from .safe_paths import reject_links, remove_task_tree, tree_bytes

_ID = re.compile(r"^[0-9a-f]{32}$")
_DOWNLOAD_SECONDS = 300
_MAX_TRANSFERS = 64


def utc(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_replace(source: Path, target: Path) -> None:
    deadline = time.monotonic() + 3
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            # Antivirus/indexer handles can briefly block a Windows directory rename.
            if getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        atomic_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JobStore:
    def __init__(self, config: Config):
        self.config = config
        reject_links(config.data_dir)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        reject_links(config.jobs_dir)
        reject_links(config.database)
        config.jobs_dir.mkdir(exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    expires REAL NOT NULL,
                    started REAL,
                    finished REAL,
                    lease TEXT,
                    source_sha256 TEXT NOT NULL,
                    source_bytes INTEGER NOT NULL,
                    reserved_bytes INTEGER NOT NULL,
                    project_json TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    artifact_json TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status,created);
                CREATE TABLE IF NOT EXISTS worker (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    updated REAL NOT NULL,
                    state TEXT NOT NULL,
                    ready INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    session_id INTEGER,
                    pid INTEGER NOT NULL
                );
            """)
            # Old rows remain in their original paths and are never retention-cleaned.
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, declaration in {
                "owner": "TEXT NOT NULL DEFAULT 'legacy'",
                "storage_key": "TEXT",
                "direction": "TEXT NOT NULL DEFAULT 'pbip_to_pbix'",
                "export_mode": "TEXT",
                "source_name": "TEXT NOT NULL DEFAULT 'source.zip'",
                "contains_data": "INTEGER",
                "retention_seconds": "INTEGER",
                "purged": "REAL",
                "phase": "TEXT",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            db.executescript("""
                CREATE INDEX IF NOT EXISTS jobs_owner_created ON jobs(owner,created);
                CREATE TABLE IF NOT EXISTS downloads (
                    token TEXT PRIMARY KEY, job_id TEXT NOT NULL, deadline REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_transfers (
                    token TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, deadline REAL NOT NULL, legacy INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS transfers_job ON artifact_transfers(job_id);
                CREATE INDEX IF NOT EXISTS transfers_deadline ON artifact_transfers(deadline);
                CREATE TABLE IF NOT EXISTS maintenance (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, at REAL NOT NULL, action TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.config.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def job_dir(self, job_id: str) -> Path:
        self._validate_id(job_id)
        with self._connect() as db:
            row = db.execute("SELECT storage_key FROM jobs WHERE id=?", (job_id,)).fetchone()
        directory = self.config.jobs_dir
        if row and row["storage_key"]:
            directory = directory / "owned" / row["storage_key"]
        directory = directory / job_id
        reject_links(directory)
        return directory

    @staticmethod
    def _validate_id(job_id: str) -> None:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise DemoError("INVALID_JOB_ID", "job_id must be the opaque identifier returned by submit_project.")

    def _row(self, db: sqlite3.Connection, job_id: str, principal: Principal = LOCAL_OPERATOR) -> sqlite3.Row:
        self._validate_id(job_id)
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or (principal.role != "admin" and row["owner"] != principal.id):
            raise DemoError("JOB_NOT_FOUND", "No job exists with this identifier.")
        return row

    def _expire(self, db: sqlite3.Connection, now: float) -> None:
        db.execute(
            """UPDATE jobs SET status='failed', updated=?, finished=?,
            error_code='QUEUE_TIMEOUT',
            error_message='No interactive worker claimed the job before its queue deadline.'
            WHERE status='queued' AND expires<=?""",
            (now, now, now),
        )
        db.execute(
            """UPDATE jobs SET status='failed', updated=?, finished=?,
            error_code='WORKER_TIMEOUT',
            error_message='The running job exceeded its deadline and the worker heartbeat expired; originals are retained.'
            WHERE status='running' AND started<=?
            AND NOT EXISTS(SELECT 1 FROM worker WHERE updated>?)""",
            (now, now, now - self.config.limits.conversion_seconds - 45,
             now - self.config.limits.heartbeat_seconds),
        )

    def submit(
        self, archive: ValidatedArchive, expected_sha256: str | None = None, *,
        principal: Principal = LOCAL_OPERATOR, source_name: str = "source.zip",
        direction: str = "pbip_to_pbix", export_mode: str | None = None,
    ) -> dict[str, Any]:
        from .inputs import ValidatedPBIX, validate_direction, safe_source_name

        validate_direction(direction, export_mode)
        if (direction == "pbix_to_pbip") != isinstance(archive, ValidatedPBIX):
            raise DemoError("INPUT_DIRECTION", "Input format does not match conversion direction.")
        export_mode = (export_mode or "definitions") if direction == "pbix_to_pbip" else None
        source_name = safe_source_name(source_name)
        digest = hashlib.sha256(archive.data).hexdigest()
        if expected_sha256 is not None and (
            not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)
            or digest != expected_sha256.lower()
        ):
            raise DemoError("SOURCE_HASH_MISMATCH", "The submitted ZIP does not match sha256.")
        job_id = uuid.uuid4().hex
        owner_dir = self.config.jobs_dir / "owned" / principal.storage_key
        reject_links(owner_dir)
        owner_dir.mkdir(parents=True, exist_ok=True)
        directory = owner_dir / job_id
        staging = owner_dir / ("staging-" + job_id)
        now = time.time()
        renamed = False
        committed = False
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._expire(db, now)
                pending = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
                if pending >= self.config.limits.pending_jobs:
                    raise DemoError("QUEUE_FULL", "The bounded queue is full; try later.")
                user_pending = db.execute(
                    "SELECT count(*) FROM jobs WHERE owner=? AND status IN ('queued','running')", (principal.id,)
                ).fetchone()[0]
                if user_pending >= self.config.limits.user_pending_jobs:
                    raise DemoError("USER_QUEUE_FULL", "Your active task limit has been reached.")
                recent = db.execute(
                    "SELECT count(*) FROM jobs WHERE owner=? AND created>?", (principal.id, now - 3600)
                ).fetchone()[0]
                if recent >= self.config.limits.submissions_per_hour:
                    raise DemoError("SUBMISSION_RATE", "Your hourly submission limit has been reached.")
                global_recent = db.execute("SELECT count(*) FROM jobs WHERE created>?", (now - 3600,)).fetchone()[0]
                if global_recent >= self.config.limits.global_submissions_per_hour:
                    raise DemoError("SUBMISSION_RATE", "The service hourly submission limit has been reached.")
                project = archive.project
                previous = db.execute(
                    """SELECT project_json,artifact_json FROM jobs
                    WHERE owner=? AND status='succeeded' AND artifact_json IS NOT NULL""", (principal.id,)
                )
                for old in previous:
                    if any(item.get("sha256") == digest and item.get("kind") in ("pbix", "pbip")
                           for item in json.loads(old["artifact_json"])):
                        project = replace(project, synthetic_fixture=json.loads(old["project_json"]).get("synthetic_fixture", False))
                        break
                used = tree_bytes(self.config.jobs_dir)
                reservation = len(archive.data) + archive.total_bytes * 2 + self.config.limits.artifact_bytes
                reserved = db.execute(
                    "SELECT coalesce(sum(reserved_bytes),0) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()[0]
                if used + reserved + reservation > self.config.limits.retained_bytes:
                    raise DemoError("STORAGE_QUOTA", "Retained jobs reached the local quota; an operator must archive them.")
                user_used = tree_bytes(owner_dir)
                user_reserved = db.execute(
                    "SELECT coalesce(sum(reserved_bytes),0) FROM jobs WHERE owner=? AND status IN ('queued','running')",
                    (principal.id,),
                ).fetchone()[0]
                if user_used + user_reserved + reservation > self.config.limits.user_retained_bytes:
                    raise DemoError("USER_STORAGE_QUOTA", "Your retained task storage limit has been reached.")
                staging.mkdir()
                with (staging / ("source.pbix" if direction == "pbix_to_pbip" else "source.zip")).open("xb") as output:
                    output.write(archive.data)
                    output.flush()
                    os.fsync(output.fileno())
                archive.extract(staging / "original")
                atomic_json(staging / "project.json", project.as_dict())
                atomic_replace(staging, directory)
                renamed = True
                db.execute(
                    """INSERT INTO jobs(id,status,created,updated,expires,source_sha256,source_bytes,reserved_bytes,project_json,
                    owner,storage_key,direction,export_mode,source_name,retention_seconds,phase)
                    VALUES(?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?,'queued')""",
                    (job_id, now, now, now + self.config.limits.queue_seconds, digest,
                     len(archive.data), reservation, json.dumps(project.as_dict()), principal.id,
                     principal.storage_key, direction, export_mode, source_name, self.config.limits.retention_seconds),
                )
            committed = True
        finally:
            if staging.exists():
                remove_task_tree(staging, owner_dir)
            if renamed and not committed:
                remove_task_tree(directory, owner_dir)
        return self.get(job_id, principal=principal)

    def get(self, job_id: str, *, principal: Principal = LOCAL_OPERATOR) -> dict[str, Any]:
        with self._connect() as db:
            self._expire(db, time.time())
            row = self._row(db, job_id, principal)
            project = json.loads(row["project_json"])
            return {
                "job_id": row["id"],
                "status": row["status"],
                "direction": row["direction"],
                "export_mode": row["export_mode"],
                "contains_data": bool(row["contains_data"]) if row["contains_data"] is not None else None,
                "requires_data_reload": row["direction"] == "pbix_to_pbip" and row["export_mode"] == "definitions",
                "phase": row["phase"],
                "artifact_status": "expired" if row["purged"] else ("available" if row["status"] == "succeeded" else "pending"),
                "retention_deadline": utc(row["finished"] + row["retention_seconds"])
                if row["finished"] and row["retention_seconds"] is not None else None,
                "warnings": [
                    "Shared Windows workers are not a sandbox for untrusted M queries or separate customer security domains.",
                    "Model definitions may contain embedded data, sensitive text and connection information; none is automatically redacted.",
                ] + (["Definitions export omits the data cache; loading data again may be necessary. No automatic refresh is performed."]
                     if row["export_mode"] == "definitions" else []),
                "created_at": utc(row["created"]),
                "updated_at": utc(row["updated"]),
                "queue_deadline": utc(row["expires"]),
                "started_at": utc(row["started"]),
                "finished_at": utc(row["finished"]),
                "source": {"name": row["source_name"], "sha256": row["source_sha256"], "bytes": row["source_bytes"]},
                "project": {
                    "page_count": len(project["pages"]),
                    "visual_count": project["visual_count"],
                    "model_format": project["model_format"],
                    "synthetic_fixture": project["synthetic_fixture"],
                },
                "error": (
                    {"code": row["error_code"], "message": row["error_message"]}
                    if row["error_code"] else None
                ),
                "artifacts": [
                    {**item, "status": "expired" if row["purged"] else "available"}
                    for item in (json.loads(row["artifact_json"]) if row["artifact_json"] else [])
                ],
            }

    def list_jobs(self, *, principal: Principal = LOCAL_OPERATOR, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM jobs WHERE owner=? ORDER BY created DESC LIMIT ?", (principal.id, min(100, max(1, limit)))
            ).fetchall()
        return [self.get(row["id"], principal=principal) for row in rows]

    def queue_status(self, principal: Principal = LOCAL_OPERATOR) -> dict[str, Any]:
        with self._connect() as db:
            self._expire(db, time.time())
            mine = db.execute("SELECT count(*) FROM jobs WHERE owner=? AND status='queued'", (principal.id,)).fetchone()[0]
            result = {"mine_pending": mine}
            if principal.role == "admin":
                result.update(
                    pending=db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0],
                    running=db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0],
                )
            else:
                result.update(pending=None, running=None)
            return result

    def cancel(self, job_id: str, *, principal: Principal = LOCAL_OPERATOR) -> dict[str, Any]:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, job_id, principal)
            if row["status"] == "running":
                raise DemoError("JOB_ALREADY_RUNNING", "Running GUI jobs cannot be cancelled; the finite timeout still applies.")
            if row["status"] == "queued":
                db.execute(
                    """UPDATE jobs SET status='cancelled', updated=?, finished=?,
                    error_code='CANCELLED', error_message='Cancelled before the worker claimed this job.' WHERE id=?""",
                    (now, now, job_id),
                )
        return self.get(job_id, principal=principal)

    def claim(self) -> dict[str, Any] | None:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            if db.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                return None
            lease = uuid.uuid4().hex
            db.execute(
                "UPDATE jobs SET status='running',updated=?,started=?,lease=? WHERE id=? AND status='queued'",
                (now, now, lease, row["id"]),
            )
            return {"job_id": row["id"], "lease": lease, "project": json.loads(row["project_json"]),
                    "direction": row["direction"], "export_mode": row["export_mode"]}

    def phase(self, claim: dict[str, Any], phase: str) -> None:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET phase=?,updated=? WHERE id=? AND lease=? AND status='running'",
                (phase, time.time(), claim["job_id"], claim["lease"]),
            )
            if cursor.rowcount != 1:
                raise DemoError("STALE_JOB_LEASE", "The worker no longer owns this task.")

    def finish(
        self, job_id: str, lease: str, *, artifacts: list[dict[str, Any]] | None = None,
        error: DemoError | None = None,
    ) -> None:
        if (artifacts is None) == (error is None):
            raise ValueError("Supply either verified artifacts or an explicit error.")
        if artifacts is not None and not artifacts:
            raise ValueError("A successful job must expose verified artifacts.")
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, job_id)
            if row["status"] != "running" or row["lease"] != lease:
                raise DemoError("STALE_JOB_LEASE", "This worker no longer owns the running job.")
            db.execute(
                """UPDATE jobs SET status=?, updated=?, finished=?, artifact_json=?,
                error_code=?, error_message=?,phase=?,contains_data=? WHERE id=? AND lease=? AND status='running'""",
                ("failed" if error else "succeeded", now, now, json.dumps(artifacts) if artifacts else None,
                 error.code if error else None, error.message if error else None,
                 "failed" if error else "completed", None if error else int(row["export_mode"] != "definitions"),
                 job_id, lease),
            )

    def recover_interrupted(self) -> int:
        """Only the worker holding the exclusive OS lock may call this."""
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE jobs SET status='failed', updated=?, finished=?,
                error_code='WORKER_INTERRUPTED',
                error_message='Worker stopped during conversion. Originals are retained; submit again after inspection.'
                WHERE status='running'""",
                (now, now),
            )
            return cursor.rowcount

    def heartbeat(self, *, state: str, ready: bool, reason: str, session_id: int | None) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO worker VALUES(1,?,?,?,?,?,?)
                ON CONFLICT(singleton) DO UPDATE SET updated=excluded.updated,
                state=excluded.state,ready=excluded.ready,reason=excluded.reason,
                session_id=excluded.session_id,pid=excluded.pid""",
                (time.time(), state, int(ready), reason, session_id, os.getpid()),
            )

    def worker_status(self) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM worker WHERE singleton=1").fetchone()
        if row is None:
            return {"ready": False, "state": "absent", "code": "NO_INTERACTIVE_WORKER", "message": "No interactive worker has started."}
        fresh = time.time() - row["updated"] <= self.config.limits.heartbeat_seconds
        return {
            "ready": fresh and bool(row["ready"]),
            "state": row["state"] if fresh else "stale",
            "code": None if fresh and row["ready"] else "WORKER_NOT_READY",
            "message": row["reason"] if fresh else "Worker heartbeat expired; controller cannot assert Desktop readiness.",
            "updated_at": utc(row["updated"]),
            "session_id": row["session_id"],
        }

    @contextmanager
    def artifact_file(self, job_id: str, kind: str, *, principal: Principal = LOCAL_OPERATOR, verify_hash: bool = True):
        if kind not in ("pbix", "pbip", "verification"):
            raise DemoError("INVALID_ARTIFACT", "Artifact kind must be pbix, pbip or verification.")
        token = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, job_id, principal)
            if row["purged"]:
                raise DemoError("ARTIFACT_EXPIRED", "Task files expired and were removed by controlled maintenance.")
            if row["status"] != "succeeded":
                raise DemoError("ARTIFACT_NOT_READY", "Artifacts require successful Desktop conversion and reopening.")
            artifact = next((item for item in json.loads(row["artifact_json"]) if item["kind"] == kind), None)
            if artifact is None:
                raise DemoError("ARTIFACT_NOT_FOUND", "This job has no artifact of the requested kind.")
            # A live file handle plus a renewable persisted lease protects active downloads.
            db.execute("INSERT INTO downloads VALUES(?,?,?)", (token, job_id, time.time() + 300))
        try:
            filename = {"pbix": "report.pbix", "pbip": "report.pbip.zip", "verification": "verification.json"}[kind]
            path = self.job_dir(job_id) / "output" / filename
            reject_links(path)
            if not path.is_file() or path.stat().st_size != artifact["bytes"]:
                raise DemoError("ARTIFACT_INTEGRITY", "The stored artifact is missing or its size has changed.")
            with path.open("rb") as source:
                if verify_hash:
                    if hashlib.file_digest(source, "sha256").hexdigest() != artifact["sha256"]:
                        raise DemoError("ARTIFACT_INTEGRITY", "The artifact no longer matches its recorded hash.")
                    source.seek(0)
                yield source, artifact, token
        finally:
            with self._connect() as db:
                db.execute("DELETE FROM downloads WHERE token=?", (token,))

    def renew_download(self, token: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE downloads SET deadline=? WHERE token=?", (time.time() + 300, token))

    def _transfer(self, db: sqlite3.Connection, job_id: str, kind: str, principal: Principal,
                  token: str | None = None, *, create: bool = False, legacy: bool = False) -> str:
        now = time.time()
        db.execute("DELETE FROM artifact_transfers WHERE deadline<=?", (now,))
        if legacy:
            token = "legacy:" + hashlib.sha256(f"{principal.id}:{job_id}:{kind}".encode()).hexdigest()
        row = db.execute("SELECT * FROM artifact_transfers WHERE token=?", (token,)).fetchone()
        if row is not None:
            if (row["owner"], row["job_id"], row["kind"], bool(row["legacy"])) != (
                principal.id, job_id, kind, legacy,
            ):
                raise DemoError("DOWNLOAD_NOT_FOUND", "No active download matches this user, job and artifact.")
            db.execute("UPDATE artifact_transfers SET deadline=? WHERE token=?", (now + _DOWNLOAD_SECONDS, token))
            return token
        if not create:
            raise DemoError("DOWNLOAD_NOT_FOUND", "The download lease expired or does not match this request.")
        active = db.execute("SELECT count(*) FROM artifact_transfers WHERE owner=?", (principal.id,)).fetchone()[0]
        if active >= _MAX_TRANSFERS:
            raise DemoError("DOWNLOAD_LIMIT", "Too many active downloads; finish a transfer or wait for its bounded idle expiry.")
        token = token if legacy else uuid.uuid4().hex
        db.execute("INSERT INTO artifact_transfers VALUES(?,?,?,?,?,?)",
                   (token, principal.id, job_id, kind, now + _DOWNLOAD_SECONDS, int(legacy)))
        return token

    def begin_artifact_download(self, job_id: str, kind: str, *, principal: Principal = LOCAL_OPERATOR) -> dict[str, Any]:
        with self.artifact_file(job_id, kind, principal=principal, verify_hash=False):
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                token = self._transfer(db, job_id, kind, principal, create=True)
        return {"download_id": token, "idle_seconds": _DOWNLOAD_SECONDS, "job_id": job_id, "kind": kind}

    def finish_artifact_download(self, job_id: str, kind: str, download_id: str,
                                 *, principal: Principal = LOCAL_OPERATOR) -> dict[str, bool]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._row(db, job_id, principal)
            row = db.execute("SELECT * FROM artifact_transfers WHERE token=?", (download_id,)).fetchone()
            if row is None:
                return {"released": False}
            if (row["owner"], row["job_id"], row["kind"], row["legacy"]) != (principal.id, job_id, kind, 0):
                raise DemoError("DOWNLOAD_NOT_FOUND", "No download matches this user, job and artifact.")
            db.execute("DELETE FROM artifact_transfers WHERE token=?", (download_id,))
        return {"released": True}

    def cleanup_expired(self, *, principal: Principal = LOCAL_OPERATOR, limit: int = 100) -> dict[str, Any]:
        if principal.role != "admin":
            raise DemoError("ADMIN_REQUIRED", "Only an administrator can perform retention maintenance.")
        removed = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute("DELETE FROM artifact_transfers WHERE deadline<=?", (now,))
            rows = db.execute(
                """SELECT * FROM jobs WHERE storage_key IS NOT NULL AND retention_seconds IS NOT NULL
                AND status IN ('succeeded','failed','cancelled') AND finished+retention_seconds<=?
                AND purged IS NULL AND NOT EXISTS(SELECT 1 FROM downloads WHERE job_id=jobs.id)
                AND NOT EXISTS(SELECT 1 FROM artifact_transfers WHERE job_id=jobs.id)
                ORDER BY finished LIMIT ?""", (now, min(100, max(1, limit))),
            ).fetchall()
            for row in rows:
                directory = self.job_dir(row["id"])
                # Only named, implementation-owned children of new task directories.
                for name in ("work", "original", "output", "evidence", "reopen", "export"):
                    remove_task_tree(directory / name, directory)
                for name in ("source.zip", "source.pbix", "project.json"):
                    path = directory / name
                    reject_links(path)
                    path.unlink(missing_ok=True)
                db.execute("UPDATE jobs SET purged=? WHERE id=?", (now, row["id"]))
                db.execute("INSERT INTO maintenance(job_id,at,action) VALUES(?,?,'expire-task-files')", (row["id"], now))
                removed.append(row["id"])
        return {"removed_job_ids": removed, "count": len(removed)}

    def artifact_chunk(
        self, job_id: str, kind: str, offset: int, length: int, *, principal: Principal = LOCAL_OPERATOR,
        download_id: str | None = None,
    ) -> dict[str, Any]:
        if type(offset) is not int or type(length) is not int or offset < 0 or not 1 <= length <= self.config.limits.chunk_bytes:
            raise DemoError("INVALID_RANGE", "Use a nonnegative offset and a length within the chunk-size limit.")
        with self.artifact_file(job_id, kind, principal=principal, verify_hash=False) as (source, artifact, _):
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._transfer(db, job_id, kind, principal, download_id,
                               create=download_id is None, legacy=download_id is None)
            read_complete = False
            try:
                if offset > artifact["bytes"]:
                    raise DemoError("INVALID_RANGE", "Offset is beyond the artifact.")
                source.seek(offset)
                data = source.read(length)
                read_complete = True
            finally:
                if not read_complete and download_id is not None:
                    self.finish_artifact_download(job_id, kind, download_id, principal=principal)
        return {
            "job_id": job_id, "kind": kind, "offset": offset, "next_offset": offset + len(data),
            "eof": offset + len(data) == artifact["bytes"], "sha256": artifact["sha256"],
            "total_bytes": artifact["bytes"], "data_base64": base64.b64encode(data).decode("ascii"),
        }
