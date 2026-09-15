import io
import hashlib
import re
import zlib
import binascii
import zipfile
from pathlib import Path, PurePosixPath

from .archive import ValidatedArchive, _member_name
from .config import Limits
from .contracts import ProjectInfo
from .errors import DemoError
from .pbix import inspect_container

DIRECTIONS = ("pbip_to_pbix", "pbix_to_pbip")
EXPORT_MODES = ("definitions", "portable")
ARTIFACT_FILENAMES = {"pbix": "report.pbix", "pbip": "report.pbip.zip", "verification": "verification.json"}
_ARTIFACT_SUFFIXES = {"pbix": ".pbix", "pbip": ".pbip.zip", "verification": ".verification.json"}
_DOWNLOAD_NAME_LIMITS = Limits(path_characters=255, path_depth=1)
_TRUSTED_BASELINE_HASHES = {
    "861aedcdc71ea34e2b4f80f0a4eede8ebc0a05e6f57d002e49f9c839c495cd3d",
    "f4b9ee628496897ea4ffba921100be8e2d4f431d08ce67cc025f9a9150c7bc79",
    "92b21965917e068331e352e59f1c0da0e1c25eb8095390682d2705c00a5c8f73",
}


def safe_source_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 180 or any(
        ord(character) < 32 or character in '/\\<>:"|?*' for character in name
    ):
        raise DemoError("INPUT_NAME", "Upload a file with a simple, bounded filename.")
    return name


def artifact_filename(basename: str, kind: str) -> str:
    if kind not in _ARTIFACT_SUFFIXES:
        raise DemoError("INVALID_ARTIFACT", "Unknown artifact kind.")
    if not isinstance(basename, str) or not basename:
        raise DemoError("INPUT_NAME", "A nonempty name is required for downloaded files.")
    filename = basename + _ARTIFACT_SUFFIXES[kind]
    try:
        _member_name(filename, _DOWNLOAD_NAME_LIMITS)
        if len(filename.encode("utf-16-le")) // 2 > 255:
            raise DemoError("INPUT_NAME", "The name is too long for a Windows download filename.")
    except (DemoError, UnicodeEncodeError) as exc:
        raise DemoError("INPUT_NAME", "Use a shorter name without unsafe characters or Windows-reserved names.") from exc
    return filename


def download_basename(source_name: str, *, is_folder: bool = False) -> str:
    safe_source_name(source_name)
    basename = source_name
    if not is_folder:
        for suffix in (".pbip.zip", ".pbix", ".zip"):
            if basename.lower().endswith(suffix):
                basename = basename[:-len(suffix)]
                break
    for kind in _ARTIFACT_SUFFIXES:
        artifact_filename(basename, kind)
    return basename


def folder_source_name(paths: list[str], pointer: str) -> str:
    parts = [PurePosixPath(path).parts for path in paths]
    if parts and all(len(path) > 1 and path[0] == parts[0][0] for path in parts):
        return safe_source_name(parts[0][0])
    return safe_source_name(PurePosixPath(pointer).stem)


def validate_direction(direction: str, export_mode: str | None) -> None:
    if direction not in DIRECTIONS:
        raise DemoError("INPUT_DIRECTION", "Use pbip_to_pbix or pbix_to_pbip.")
    if export_mode not in (None, *EXPORT_MODES):
        raise DemoError("EXPORT_MODE", "Use definitions or portable.")
    if direction == "pbip_to_pbix" and export_mode is not None:
        raise DemoError("EXPORT_MODE", "export_mode is only valid for PBIX to PBIP.")


class ValidatedPBIX(ValidatedArchive):
    """Bounded container preflight, never proof that Desktop can open the file."""

    def __init__(self, data: bytes, limits: Limits = Limits()):
        self.data, self.limits, self.members, self.total_bytes = data, limits, {}, 0
        if not data or len(data) > limits.archive_bytes:
            raise DemoError("ARCHIVE_SIZE", "The PBIX is empty or exceeds the upload limit.")
        if data[:4] != b"PK\x03\x04":
            raise DemoError("PBIX_UNSUPPORTED", "Encrypted, unknown or non-ZIP PBIX files are not supported.")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self._validate_members(archive)
                names = {info.filename.casefold() for info in archive.infolist()}
                if not {"version", "[content_types].xml", "datamodel"} <= names:
                    raise DemoError("PBIX_UNSUPPORTED", "Input is not a supported full-model PBIX container.")
                # SecurityBindings also exists in ordinary unlabeled Desktop files.
                # It is not decoded or removed; Desktop enforces label restrictions.
                if any("sensitivitylabel" in name for name in names):
                    raise DemoError("PBIX_LABEL_UNSUPPORTED", "PBIP does not support sensitivity labels; labels will not be removed.")
                for info in archive.infolist():
                    # Stream every member to enforce CRC and declared bounds before Desktop sees it.
                    count = 0
                    with archive.open(info) as stream:
                        while chunk := stream.read(1024 * 1024):
                            count += len(chunk)
                            if count > info.file_size:
                                raise DemoError("PBIX_INVALID", "PBIX entries exceed their declared sizes.")
                    if count != info.file_size:
                        raise DemoError("PBIX_INVALID", "PBIX entry size mismatch.")
            info = inspect_container(io.BytesIO(data), len(data), None, limits)
            self.project = ProjectInfo(
                "source.pbix", "", "", tuple(info["pages"]), info["visual_count"], "binary",
                hashlib.sha256(data).hexdigest() in _TRUSTED_BASELINE_HASHES,
            )
        except (zipfile.BadZipFile, EOFError, NotImplementedError, RuntimeError, UnicodeError, zlib.error, binascii.Error) as exc:
            raise DemoError("PBIX_INVALID", "PBIX is corrupt, encrypted or incompatible.") from exc

    def extract(self, destination: Path) -> None:
        destination.mkdir()
        with (destination / "source.pbix").open("xb") as output:
            output.write(self.data)


def validate_input(data: bytes, name: str, direction: str = "auto", export_mode: str | None = None,
                   limits: Limits = Limits()):
    safe_source_name(name)
    suffix = Path(name).suffix.lower()
    inferred = {".pbix": "pbix_to_pbip", ".zip": "pbip_to_pbix"}.get(suffix)
    if inferred is None:
        raise DemoError("INPUT_FORMAT", "Upload a PBIX or a complete PBIP ZIP, not an isolated .pbip pointer.")
    if direction == "auto":
        direction = inferred
    if direction != inferred:
        raise DemoError("INPUT_DIRECTION", "The filename format does not match the requested direction.")
    validate_direction(direction, export_mode)
    validated = ValidatedPBIX(data, limits) if direction == "pbix_to_pbip" else ValidatedArchive(data, limits)
    return validated, direction
