"""Export only files needed by one Desktop-generated local project."""

import io
import copy
import zipfile
from pathlib import Path

from .archive import ValidatedArchive, _key, _member_name
from .config import Limits
from .errors import DemoError
from .safe_paths import tree_files

_RESOURCE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico", ".json", ".pbiviz"}
_PRIVATE_NAMES = {
    "localsettings.json", "unappliedchanges.json", "credentials.json", "token.json", "tokens.json",
    "id_rsa", "id_ed25519", "known_hosts", "worker_ssh",
}


def allowed_project_file(name: str, pointer: str, report: str, model: str, portable: bool) -> bool:
    key, leaf = _key(name), Path(name).name.casefold()
    parts = key.split("/")
    if leaf in _PRIVATE_NAMES or any(
        part in (".ssh", ".azure", ".identityservice", "credentials", "tokens") for part in parts
    ) or key.endswith((".pem", ".key", ".pfx", ".p12", ".clixml", ".ps1", ".exe", ".dll")):
        return False
    pointer_parent = pointer.rpartition("/")[0]
    gitignore = (pointer_parent + "/" if pointer_parent else "") + ".gitignore"
    if key == _key(pointer) or key == _key(gitignore):
        return True
    report_prefix, model_prefix = _key(report) + "/", _key(model) + "/"
    if key.startswith(report_prefix):
        relative = key[len(report_prefix):]
        if relative in {"definition.pbir", "report.json", "mobilestate.json", "semanticmodeldiagramlayout.json", ".platform"}:
            return True
        if relative.startswith("definition/"):
            return relative.endswith(".json")
        if relative.startswith(("staticresources/", "customvisuals/")):
            return Path(leaf).suffix in _RESOURCE_EXTENSIONS
    if key.startswith(model_prefix):
        relative = key[len(model_prefix):]
        if relative in {"definition.pbism", "model.bim", "diagramlayout.json", ".platform"}:
            return True
        if relative.startswith("definition/"):
            return relative.endswith(".tmdl")
        if relative == ".pbi/cache.abf":
            return portable
        if relative == ".pbi/editorSettings.json".lower():
            return False
    return False


def package_project(root: Path, export_mode: str, limits: Limits = Limits()) -> ValidatedArchive:
    if export_mode not in ("definitions", "portable"):
        raise DemoError("EXPORT_MODE", "Use definitions or portable.")
    files = {}
    total = 0
    for path in tree_files(root):
        name = _member_name(path.relative_to(root).as_posix(), limits)
        total += path.stat().st_size
        if len(files) >= limits.file_count or total > limits.uncompressed_bytes:
            raise DemoError("EXPORT_SIZE", "Desktop project exceeds the bounded export budget.")
        if path.stat().st_size > limits.member_bytes:
            raise DemoError("EXPORT_SIZE", "A Desktop project file exceeds the member budget.")
        files[name] = path
    # Build a bounded inspection archive first; references identify the sole owning report/model.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, path in files.items():
            archive.write(path, name)
    from dataclasses import replace

    inspect_limits = replace(limits, archive_bytes=limits.uncompressed_bytes)
    inspected = ValidatedArchive(buffer.getvalue(), inspect_limits)
    return filter_archive(inspected, export_mode, limits)


def filter_archive(source: ValidatedArchive, export_mode: str, limits: Limits = Limits()) -> ValidatedArchive:
    project = source.project
    buffer = io.BytesIO()
    cache = _key(project.model + "/.pbi/cache.abf")
    has_cache = any(key == cache and info.file_size > 0 for key, info in source.members.items())
    if export_mode == "portable" and not has_cache:
        raise DemoError("PORTABLE_CACHE_MISSING", "Desktop did not export a nonempty task-owned data cache; no offline result is claimed.")
    with zipfile.ZipFile(io.BytesIO(source.data)) as original, zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info in original.infolist():
            if not info.is_dir() and allowed_project_file(info.filename, project.pointer, project.report, project.model, export_mode == "portable"):
                exported_info = copy.copy(info)
                exported_info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(exported_info, original.read(info))
    data = buffer.getvalue()
    return ValidatedArchive(data, limits)
