import argparse
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter
from dataclasses import asdict, replace
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .config import Config
from .contracts import ProjectInfo
from .errors import DemoError
from .pbix import inspect_pbix
from .storage import JobStore, atomic_json, file_sha256
from .safe_paths import remove_task_tree, tree_files

LOG = logging.getLogger(__name__)


class QueueLock(AbstractContextManager):
    def __init__(self, data_dir: Path):
        self.path = data_dir / "worker.lock"
        self.stream = None

    def __enter__(self):
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"1")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise DemoError("WORKER_ALREADY_RUNNING", "Another worker already owns this data directory.") from exc
        self.stream = stream
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.stream is not None:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None
        return False


def readiness(desktop_exe: Path) -> dict[str, Any]:
    from .windows_session import interactive_readiness

    state = interactive_readiness()
    if state["ready"] and (desktop_exe.name.lower() != "pbidesktop.exe" or not desktop_exe.is_file()):
        return {
            "ready": False, "code": "DESKTOP_NOT_INSTALLED",
            "message": "Configure the installed standard x64 PBIDesktop.exe path.",
            "session_id": state.get("session_id"),
        }
    return state


def validate_conversion_evidence(result: dict[str, Any], project: ProjectInfo) -> None:
    first = result.get("desktop_pid")
    second = result.get("reopened_pid")
    if type(first) is not int or type(second) is not int or first <= 0 or second <= 0 or first == second:
        raise DemoError("REOPEN_NOT_CONFIRMED", "Converter did not report two distinct owned Desktop processes.")
    if result.get("model_present") is not True or not isinstance(result.get("pages"), list):
        raise DemoError("REOPEN_NOT_CONFIRMED", "Converter did not confirm report and model availability after reopening.")
    if Counter(result["pages"]) != Counter(project.pages):
        raise DemoError("REOPEN_PAGES_CHANGED", "Desktop reopening did not observe the expected pages.")


class Worker:
    def __init__(self, config: Config, desktop_exe: Path, *, review_seconds: int = 0):
        self.config = config
        self.desktop_exe = desktop_exe
        self.store = JobStore(config)
        self.review_seconds = review_seconds

    def _heartbeat(self, state: dict[str, Any], *, busy: bool = False) -> None:
        self.store.heartbeat(
            state="busy" if busy else ("idle" if state["ready"] else "blocked"),
            ready=state["ready"], reason=state["message"], session_id=state.get("session_id"),
        )

    def _convert(self, claim: dict[str, Any]) -> list[dict[str, Any]]:
        if claim.get("direction") == "pbix_to_pbip":
            return self._reverse(claim)

        job_id = claim["job_id"]
        directory = self.store.job_dir(job_id)
        project = ProjectInfo.from_dict(claim["project"])
        self.store.phase(claim, "preparing")
        source_hash = file_sha256(directory / "source.zip")
        if source_hash != self.store.get(job_id)["source"]["sha256"]:
            raise DemoError("SOURCE_INTEGRITY", "The retained input ZIP changed after submission.")
        work = directory / "work"
        output = directory / "output"
        evidence = directory / "evidence"
        from .archive import ValidatedArchive, _key
        from .project_export import filter_archive

        source_archive = ValidatedArchive((directory / "source.zip").read_bytes(), self.config.limits)
        cache = source_archive.members.get(_key(project.model + "/.pbi/cache.abf"))
        has_cache = cache is not None and cache.file_size > 0
        if not project.synthetic_fixture and not has_cache:
            raise DemoError("DATA_CACHE_REQUIRED", "This non-bundled PBIP has no data cache. Load data manually in Desktop; arbitrary queries are never refreshed automatically.")
        sanitized = filter_archive(source_archive, "portable" if has_cache else "definitions", self.config.limits)
        sanitized.extract(work)
        output.mkdir()
        evidence.mkdir()
        pbix = output / "report.pbix"
        request_path = evidence / "request.json"
        request = {
            "project_file": str(work.joinpath(*project.pointer.split("/"))),
            "output_file": str(pbix),
            "evidence_dir": str(evidence),
            "desktop_exe": str(self.desktop_exe),
            "project": project.as_dict(),
            "timeout_seconds": self.config.limits.conversion_seconds,
            "review_seconds": self.review_seconds,
            "limits": asdict(self.config.limits),
        }
        result = self._supervise(claim, request, request_path, pbix)
        validate_conversion_evidence(result, project)
        metadata = inspect_pbix(pbix, project, self.config.limits)
        if file_sha256(directory / "source.zip") != source_hash:
            raise DemoError("SOURCE_INTEGRITY", "The original ZIP changed during conversion.")
        verification = {
            "converter": "power-bi-desktop-ui",
            "job_id": job_id,
            "direction": "pbip_to_pbix",
            "contains_data": True,
            "source_sha256": source_hash,
            "reopened_in_fresh_desktop": True,
            "desktop_pid": result["desktop_pid"],
            "reopened_pid": result["reopened_pid"],
            "pages": list(project.pages),
            "visual_count": project.visual_count,
            "binary_model_present": metadata["binary_model_present"],
            "synthetic_fixture": project.synthetic_fixture,
            "original_zip_preserved": True,
            "data_refresh_policy": "exact-bundled-synthetic-only; never refresh an existing portable cache",
            "data_values_verified": bool(project.synthetic_fixture),
        }
        return self._artifacts(claim, verification, result, "pbix", "report.pbix")

    def _supervise(self, claim, request, request_path, target):
        from .windows_session import OwnedProcess

        directory = self.store.job_dir(claim["job_id"])
        evidence = request_path.parent
        atomic_json(request_path, request)
        self.store.phase(claim, "desktop-conversion-and-fresh-reopen")
        deadline = time.monotonic() + self.config.limits.conversion_seconds + 15
        arguments = [sys.executable, "-m", "pbip_mcp.convert_process", "--request", str(request_path)]
        with OwnedProcess(arguments, cwd=directory, log_path=evidence / "converter.log") as process:
            while process.poll() is None:
                state = readiness(self.desktop_exe)
                self._heartbeat(state, busy=True)
                if not state["ready"]:
                    raise DemoError("INTERACTIVE_SESSION_LOST", "The worker session became disconnected or locked during conversion.")
                if time.monotonic() >= deadline:
                    raise DemoError("CONVERSION_TIMEOUT", "Desktop conversion exceeded its finite supervised deadline.")
                if target.exists() and target.stat().st_size > self.config.limits.artifact_bytes:
                    raise DemoError("PBIX_SIZE", "Desktop output exceeded the reserved artifact limit.")
                used = sum(path.stat().st_size for path in tree_files(directory))
                if used > self.config.limits.artifact_bytes + 4 * self.config.limits.uncompressed_bytes:
                    raise DemoError("TASK_DISK_BUDGET", "Desktop exceeded this task's bounded disk budget.")
                time.sleep(2)
            exit_code = process.poll()
        result_path = evidence / "result.json"
        if not result_path.is_file() or result_path.stat().st_size > self.config.limits.metadata_bytes:
            raise DemoError("CONVERTER_EXITED", "Converter exited without a bounded durable result; inspect its private log.")
        response = json.loads(result_path.read_text(encoding="utf-8"))
        if not response.get("ok"):
            error = response.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                raise DemoError("CONVERTER_RESULT_INVALID", "Converter returned an invalid error result.")
            raise DemoError(error["code"], error["message"])
        if exit_code != 0:
            raise DemoError("CONVERTER_EXITED", "Converter returned a nonzero process exit code.")
        result = response["result"]
        return result

    def _reverse(self, claim):
        from .project_export import package_project

        directory = self.store.job_dir(claim["job_id"])
        source = directory / "source.pbix"
        source_hash = file_sha256(source)
        if source_hash != self.store.get(claim["job_id"])["source"]["sha256"]:
            raise DemoError("SOURCE_INTEGRITY", "The retained PBIX changed after submission.")
        project = ProjectInfo.from_dict(claim["project"])
        work, export, output, evidence = (directory / name for name in ("work", "export", "output", "evidence"))
        self.store.phase(claim, "preparing")
        work.mkdir()
        shutil.copyfile(source, work / "source.pbix")
        for folder in (export, output, evidence):
            folder.mkdir()
        request = {
            "direction": "pbix_to_pbip", "export_mode": claim["export_mode"],
            "project_file": str(work / "source.pbix"), "output_file": str(export / "report.pbip"),
            "evidence_dir": str(evidence), "desktop_exe": str(self.desktop_exe),
            "project": project.as_dict(), "timeout_seconds": self.config.limits.conversion_seconds,
            "review_seconds": self.review_seconds, "limits": asdict(self.config.limits),
        }
        result = self._supervise(claim, request, evidence / "request.json", export / "report.pbip")
        validate_conversion_evidence(result, project)
        self.store.phase(claim, "packaging")
        package = package_project(export, claim["export_mode"], self.config.limits)
        if Counter(package.project.pages) != Counter(project.pages) or package.project.visual_count != project.visual_count:
            raise DemoError("PBIP_REPORT_CHANGED", "Exported PBIP report differs from the PBIX input.")
        import hashlib

        if hashlib.sha256(package.data).hexdigest() != result.get("details", {}).get("export_sha256"):
            raise DemoError("EXPORT_INTEGRITY", "The exported project changed after fresh Desktop verification.")
        if file_sha256(source) != source_hash:
            raise DemoError("SOURCE_INTEGRITY", "The original PBIX changed during conversion.")
        with (output / "report.pbip.zip").open("xb") as stream:
            stream.write(package.data)
        verification = {
            "converter": "power-bi-desktop-ui", "job_id": claim["job_id"], "direction": "pbix_to_pbip",
            "export_mode": claim["export_mode"], "source_sha256": source_hash,
            "contains_data": claim["export_mode"] == "portable", "requires_data_reload": claim["export_mode"] != "portable",
            "reopened_in_fresh_desktop": True, "desktop_pid": result["desktop_pid"], "reopened_pid": result["reopened_pid"],
            "pages": list(project.pages), "visual_count": project.visual_count, "original_pbix_preserved": True,
            "synthetic_fixture": project.synthetic_fixture, "data_refresh_policy": "never",
            "data_values_verified": project.synthetic_fixture and claim["export_mode"] == "portable",
            "connection_information_redacted": False,
        }
        return self._artifacts(claim, verification, result, "pbip", "report.pbip.zip")

    def _artifacts(self, claim, verification, result, output_kind, output_name):
        job_id = claim["job_id"]
        output = self.store.job_dir(job_id) / "output"
        details = result.get("details", {})
        for key in (
            "desktop_version", "synthetic_total_amount", "model_tables", "model_measures",
            "verification_method", "additional_fixture_observations",
        ):
            if key in details:
                verification[key] = details[key]
        atomic_json(output / "verification.json", verification)
        artifacts = []
        for kind, filename, media_type in (
            (output_kind, output_name, "application/zip" if output_kind == "pbip" else "application/octet-stream"),
            ("verification", "verification.json", "application/json"),
        ):
            path = output / filename
            with path.open("rb+") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            artifacts.append({
                "artifact_id": f"{job_id}:{kind}", "kind": kind, "filename": filename,
                "media_type": media_type, "bytes": path.stat().st_size,
                "sha256": file_sha256(path), "download_tool": "get_artifact",
            })
        return artifacts

    def execute(self, claim: dict[str, Any]) -> None:
        try:
            artifacts = self._convert(claim)
        except DemoError as exc:
            LOG.error("Job %s failed: %s", claim["job_id"], exc.code)
            self.store.finish(claim["job_id"], claim["lease"], error=exc)
        except Exception:
            # Durable failure is required even for unexpected private converter/storage exceptions.
            LOG.exception("Job %s failed unexpectedly", claim["job_id"])
            self.store.finish(claim["job_id"], claim["lease"], error=DemoError(
                "WORKER_INTERNAL_ERROR", "Worker failed unexpectedly; inspect the private worker log.",
            ))
        else:
            self.store.finish(claim["job_id"], claim["lease"], artifacts=artifacts)
        finally:
            directory = self.store.job_dir(claim["job_id"])
            for name in ("work", "reopen", "export"):
                try:
                    remove_task_tree(directory / name, directory)
                except (OSError, DemoError):
                    LOG.exception("Task %s temporary cleanup requires operator attention", claim["job_id"])

    def run(self, *, max_jobs: int = 0, idle_timeout: int = 0) -> int:
        with QueueLock(self.config.data_dir):
            recovered = self.store.recover_interrupted()
            if recovered:
                LOG.warning("Marked %s interrupted jobs as failed; originals retained.", recovered)
            count = 0
            idle_since = time.monotonic()
            state = {"ready": False, "message": "Worker exited before readiness was established.", "session_id": None}
            try:
                while max_jobs == 0 or count < max_jobs:
                    if (self.config.data_dir / "worker.stop").exists():
                        LOG.info("Graceful stop requested; queued originals remain available.")
                        return 0
                    state = readiness(self.desktop_exe)
                    self._heartbeat(state)
                    if not state["ready"]:
                        LOG.error("Worker not ready: %s", state["code"])
                        return 2
                    claim = self.store.claim()
                    if claim:
                        self.execute(claim)
                        count += 1
                        idle_since = time.monotonic()
                    else:
                        if idle_timeout and time.monotonic() - idle_since >= idle_timeout:
                            return 0
                        time.sleep(1)
                return 0
            finally:
                self.store.heartbeat(
                    state="stopped" if state["ready"] else "blocked", ready=False,
                    reason="Interactive worker stopped." if state["ready"] else state["message"],
                    session_id=state.get("session_id"),
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Real interactive Windows worker; never run as SYSTEM or session 0.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--desktop-exe", type=Path, required=True)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=0)
    parser.add_argument("--idle-timeout", type=int, default=0)
    parser.add_argument("--review-seconds", type=int, default=0)
    parser.add_argument("--limits-config", type=Path)
    args = parser.parse_args()
    if args.max_jobs < 0 or args.idle_timeout < 0:
        parser.error("max-jobs and idle-timeout cannot be negative")
    if not 0 <= args.review_seconds <= 60:
        parser.error("review-seconds must be 0..60")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    if args.probe:
        state = readiness(args.desktop_exe)
        print(json.dumps(state, ensure_ascii=True))
        raise SystemExit(0 if state["ready"] else 2)
    try:
        worker = Worker(Config.load(args.data_dir.resolve(), args.limits_config), args.desktop_exe.resolve(), review_seconds=args.review_seconds)
        code = worker.run(max_jobs=args.max_jobs, idle_timeout=args.idle_timeout)
    except DemoError as exc:
        print(json.dumps({"ok": False, "error": exc.as_dict()}), file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
