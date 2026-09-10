import base64
import binascii
import io
import json
import ntpath
import os
import posixpath
import re
import stat
import unicodedata
import zipfile
import zlib
from pathlib import Path
from typing import Any

from .config import Limits
from .contracts import ProjectInfo
from .errors import DemoError

_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\.|$)", re.I)
_FORBIDDEN = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')


def decode_archive(value: str, limits: Limits) -> bytes:
    if not value or len(value) > limits.base64_characters:
        raise DemoError("ARCHIVE_SIZE", "ZIP payload is empty or exceeds the compressed-size limit.")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DemoError("INVALID_BASE64", "archive_base64 must be strict base64 for a complete ZIP.") from exc
    if len(data) > limits.archive_bytes:
        raise DemoError("ARCHIVE_SIZE", "ZIP exceeds the compressed-size limit.")
    return data


def _key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _member_name(name: str, limits: Limits) -> str:
    if not name or "\\" in name or name.startswith("/") or ntpath.isabs(name):
        raise DemoError("UNSAFE_ARCHIVE_PATH", "ZIP members must use relative, slash-separated names.")
    clean = name[:-1] if name.endswith("/") else name
    parts = clean.split("/")
    if len(parts) > limits.path_depth or len(clean) > limits.path_characters:
        raise DemoError("UNSAFE_ARCHIVE_PATH", "ZIP member path exceeds depth or length limits.")
    for part in parts:
        if (
            part in ("", ".", "..")
            or _FORBIDDEN.search(part)
            or part.endswith((".", " "))
            or _RESERVED.match(part)
        ):
            raise DemoError("UNSAFE_ARCHIVE_PATH", "ZIP contains an unsafe or Windows-reserved member name.")
    return clean


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON key")
        result[name] = value
    return result


class ValidatedArchive:
    def __init__(self, data: bytes, limits: Limits = Limits()):
        self.data = data
        self.limits = limits
        self.members: dict[str, zipfile.ZipInfo] = {}
        self.total_bytes = 0
        if not data or len(data) > limits.archive_bytes:
            raise DemoError("ARCHIVE_SIZE", "ZIP payload is empty or exceeds the compressed-size limit.")
        if data[:4] not in (b"PK\x03\x04", b"PK\x05\x06"):
            raise DemoError("INVALID_ZIP", "Input must be a ZIP archive, not a pointer or executable.")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self._validate_members(archive)
                self.project = self._inspect_project(archive)
                from .synthetic import matches_fixture

                if matches_fixture(archive, self.project):
                    self.project = ProjectInfo(**{**self.project.__dict__, "synthetic_fixture": True})
        except (zipfile.BadZipFile, NotImplementedError, EOFError, binascii.Error, zlib.error, UnicodeError) as exc:
            raise DemoError("INVALID_ZIP", "ZIP is corrupt or uses an unsupported ZIP feature.") from exc

    def _validate_members(self, archive: zipfile.ZipFile) -> None:
        infos = archive.infolist()
        if not infos or len(infos) > self.limits.file_count:
            raise DemoError("ARCHIVE_FILE_COUNT", "ZIP is empty or exceeds the member-count limit.")
        files: set[str] = set()
        ancestors: set[str] = set()
        for info in infos:
            name = _member_name(info.orig_filename, self.limits)
            key = _key(name)
            if key in self.members:
                raise DemoError("DUPLICATE_ARCHIVE_PATH", "ZIP has duplicate or case-colliding paths.")
            self.members[key] = info
            mode = (info.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.external_attr & 0x400:
                raise DemoError("ARCHIVE_LINK", "Symlinks, reparse points and special files are forbidden.")
            if kind == stat.S_IFDIR and not info.is_dir():
                raise DemoError("INVALID_ZIP", "ZIP directory attributes disagree with its member name.")
            if info.flag_bits & 1:
                raise DemoError("ENCRYPTED_ZIP", "Encrypted archives are not supported.")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise DemoError("ZIP_COMPRESSION", "Only stored and Deflate ZIP members are supported.")
            if info.file_size > self.limits.member_bytes:
                raise DemoError("ARCHIVE_MEMBER_SIZE", "An uncompressed ZIP member exceeds the size limit.")
            if info.file_size > max(1, info.compress_size) * self.limits.compression_ratio:
                raise DemoError("ZIP_BOMB", "A ZIP member exceeds the compression-ratio limit.")
            self.total_bytes += info.file_size
            if self.total_bytes > self.limits.uncompressed_bytes:
                raise DemoError("ARCHIVE_EXPANDED_SIZE", "ZIP exceeds the total uncompressed-size limit.")
            if not info.is_dir():
                files.add(key)
            parts = key.split("/")
            ancestors.update("/".join(parts[:i]) for i in range(1, len(parts)))
        if files & ancestors:
            raise DemoError("DUPLICATE_ARCHIVE_PATH", "A ZIP path is both a file and a parent directory.")

    def _file(self, name: str) -> zipfile.ZipInfo:
        info = self.members.get(_key(name))
        if info is None or info.is_dir():
            raise DemoError("INCOMPLETE_PROJECT", "The ZIP is missing a required project definition file.")
        return info

    @property
    def has_data_cache(self) -> bool:
        cache = self.members.get(_key(self.project.model + "/.pbi/cache.abf"))
        return cache is not None and not cache.is_dir() and cache.file_size > 0

    def require_data_cache(self) -> None:
        if not self.project.synthetic_fixture and not self.has_data_cache:
            raise DemoError(
                "DATA_CACHE_REQUIRED",
                "This non-bundled PBIP has no nonempty data cache. Load data manually in Desktop "
                "and include the local model's .pbi/cache.abf; arbitrary queries are never refreshed automatically.",
            )

    def _json(self, archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
        info = self._file(name)
        if info.file_size > self.limits.metadata_bytes:
            raise DemoError("METADATA_SIZE", "A project metadata document exceeds the limit.")
        try:
            value = json.loads(archive.read(info).decode("utf-8-sig"), object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise DemoError("INVALID_PROJECT_JSON", "Project metadata is not a valid unique-key JSON object.") from exc
        if not isinstance(value, dict):
            raise DemoError("INVALID_PROJECT_JSON", "Project metadata must be a JSON object.")
        return value

    def _reference(self, parent: str, reference: Any) -> str:
        if (
            not isinstance(reference, str)
            or not reference
            or "\\" in reference
            or reference.startswith("/")
            or ntpath.isabs(reference)
            or _FORBIDDEN.search(reference)
        ):
            raise DemoError("UNSAFE_PROJECT_REFERENCE", "Project references must be local relative paths.")
        resolved = posixpath.normpath(posixpath.join(parent, reference))
        if resolved in (".", "..") or resolved.startswith("../"):
            raise DemoError("UNSAFE_PROJECT_REFERENCE", "A project reference escapes the submitted ZIP.")
        _member_name(resolved, self.limits)
        return resolved

    def _inspect_project(self, archive: zipfile.ZipFile) -> ProjectInfo:
        pointers = [i.filename for i in self.members.values() if i.filename.lower().endswith(".pbip")]
        if len(pointers) != 1:
            raise DemoError("AMBIGUOUS_PROJECT", "Submit a complete ZIP containing exactly one .pbip pointer.")
        pointer = pointers[0]
        shortcut = self._json(archive, pointer)
        artifacts = shortcut.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 1:
            raise DemoError("AMBIGUOUS_PROJECT", "The .pbip pointer must reference exactly one report.")
        item = artifacts[0]
        report_ref = item.get("report") if isinstance(item, dict) else None
        if not isinstance(report_ref, dict):
            raise DemoError("INCOMPLETE_PROJECT", "The .pbip pointer needs a local report reference.")
        report = self._reference(posixpath.dirname(pointer), report_ref.get("path"))
        definition = self._json(archive, report + "/definition.pbir")
        dataset = definition.get("datasetReference")
        if not isinstance(dataset, dict) or set(dataset) != {"byPath"} or not isinstance(dataset["byPath"], dict):
            raise DemoError("REMOTE_MODEL_UNSUPPORTED", "This demo requires a complete local model, not a service connection.")
        model = self._reference(report, dataset["byPath"].get("path"))
        self._json(archive, model + "/definition.pbism")
        if _key(model + "/.pbi/unappliedChanges.json") in self.members:
            raise DemoError("PENDING_MODEL_CHANGES", "Apply or discard pending Power Query changes before packaging.")
        has_tmdl = _key(model + "/definition/model.tmdl") in self.members
        has_bim = _key(model + "/model.bim") in self.members
        if has_tmdl == has_bim:
            raise DemoError("INCOMPLETE_PROJECT", "Supply one complete model definition: TMDL or model.bim.")
        if has_tmdl:
            self._file(model + "/definition/database.tmdl")
            table_prefix = _key(model + "/definition/tables/")
            if not any(key.startswith(table_prefix) and key.endswith(".tmdl") for key in self.members):
                raise DemoError("INCOMPLETE_PROJECT", "The TMDL model must include at least one table.")
        else:
            model_json = self._json(archive, model + "/model.bim").get("model")
            if not isinstance(model_json, dict) or not model_json.get("tables"):
                raise DemoError("INCOMPLETE_PROJECT", "The model.bim must include model tables.")
        pages, visual_count = self._inspect_report(archive, report)
        return ProjectInfo(pointer, report, model, tuple(pages), visual_count, "tmdl" if has_tmdl else "tmsl")

    def _inspect_report(self, archive: zipfile.ZipFile, report: str) -> tuple[list[str], int]:
        legacy = _key(report + "/report.json") in self.members
        enhanced = _key(report + "/definition/report.json") in self.members
        if legacy == enhanced:
            raise DemoError("INCOMPLETE_PROJECT", "Supply one complete report definition: PBIR or PBIR-Legacy.")
        if legacy:
            document = self._json(archive, report + "/report.json")
            sections = document.get("sections")
            if not isinstance(sections, list) or not sections:
                raise DemoError("INCOMPLETE_PROJECT", "The report must contain at least one page.")
            pages = []
            count = 0
            for page in sections:
                if not isinstance(page, dict) or not isinstance(page.get("displayName"), str) or not page["displayName"]:
                    raise DemoError("INVALID_REPORT", "Each report page needs a display name.")
                visuals = page.get("visualContainers", [])
                if not isinstance(visuals, list):
                    raise DemoError("INVALID_REPORT", "Report visualContainers must be an array.")
                pages.append(page["displayName"])
                count += len(visuals)
        else:
            self._json(archive, report + "/definition/report.json")
            self._json(archive, report + "/definition/version.json")
            self._json(archive, report + "/definition/pages/pages.json")
            prefix = _key(report + "/definition/pages/")
            pages = []
            count = 0
            for key, info in sorted(self.members.items()):
                if key.startswith(prefix) and key.endswith("/page.json"):
                    page = self._json(archive, info.filename)
                    if not isinstance(page.get("displayName"), str) or not page["displayName"]:
                        raise DemoError("INVALID_REPORT", "Each report page needs a nonempty display name.")
                    pages.append(page["displayName"])
                    visual_prefix = _key(posixpath.dirname(info.filename) + "/visuals/")
                    for visual_key, visual_info in self.members.items():
                        if visual_key.startswith(visual_prefix) and visual_key.endswith("/visual.json"):
                            visual = self._json(archive, visual_info.filename)
                            if isinstance(visual.get("visual"), dict):
                                count += 1
        if not pages or count < 1:
            raise DemoError("INCOMPLETE_PROJECT", "The complete report must contain a page and a visual.")
        if len(set(pages)) != len(pages):
            raise DemoError("AMBIGUOUS_PAGES", "This demo requires distinct page display names for reopening verification.")
        return pages, count

    def extract(self, destination: Path) -> None:
        if destination.exists():
            raise DemoError("EXTRACTION_TARGET_EXISTS", "Refusing to replace an existing extraction target.")
        destination.mkdir(parents=True)
        written_total = 0
        try:
            with zipfile.ZipFile(io.BytesIO(self.data)) as archive:
                for info in self.members.values():
                    target = destination.joinpath(*info.filename.rstrip("/").split("/"))
                    if len(str(target)) >= 248:
                        raise DemoError("WORKER_PATH_LENGTH", "Use a shorter worker data directory for this project.")
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with archive.open(info) as source, target.open("xb") as output:
                        while chunk := source.read(1024 * 1024):
                            written += len(chunk)
                            written_total += len(chunk)
                            if written > info.file_size or written_total > self.limits.uncompressed_bytes:
                                raise DemoError("ZIP_BOMB", "Actual extracted bytes exceed declared ZIP sizes.")
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if written != info.file_size:
                        raise DemoError("INVALID_ZIP", "ZIP member size does not match its extracted data.")
        except (zipfile.BadZipFile, NotImplementedError, EOFError, binascii.Error, zlib.error, UnicodeError) as exc:
            raise DemoError("INVALID_ZIP", "ZIP data is corrupt or failed its CRC check.") from exc
