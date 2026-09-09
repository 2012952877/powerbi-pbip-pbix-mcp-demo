import json
import io
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Limits
from .contracts import ProjectInfo
from .errors import DemoError


def _document(archive: zipfile.ZipFile, name: str, limit: int) -> dict[str, Any]:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise DemoError("PBIX_REPORT_SIZE", "Saved report metadata exceeds the inspection limit.")
    data = archive.read(info)
    try:
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else (
            "utf-16-le" if len(data) > 1 and data[1] == 0 else "utf-8-sig"
        )
        value = json.loads(data.decode(encoding))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise DemoError("PBIX_REPORT_INVALID", "Desktop output report metadata could not be decoded.") from exc
    if not isinstance(value, dict):
        raise DemoError("PBIX_REPORT_INVALID", "Desktop output report metadata is not an object.")
    return value


def inspect_pbix(path: Path, project: ProjectInfo | None, limits: Limits = Limits()) -> dict[str, Any]:
    """Structural guard only; a fresh Desktop reopen is separately mandatory."""
    if path.suffix.lower() != ".pbix" or not path.is_file():
        raise DemoError("PBIX_MISSING", "Desktop did not create the requested PBIX.")
    size = path.stat().st_size
    if not 1 <= size <= limits.artifact_bytes:
        raise DemoError("PBIX_SIZE", "The output PBIX is empty or exceeds the artifact-size limit.")
    with path.open("rb") as source:
        return inspect_container(source, size, project, limits)


def inspect_container(source, size: int, project: ProjectInfo | None, limits: Limits = Limits()) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(source) as archive:
            names = {info.filename.replace("\\", "/"): info.filename for info in archive.infolist()}
            if len(names) != len(archive.infolist()):
                raise DemoError("PBIX_INVALID", "Desktop output contains duplicate container entries.")
            if "DataModel" not in names or archive.getinfo(names["DataModel"]).file_size == 0:
                raise DemoError("PBIX_MODEL_MISSING", "Output has no binary DataModel; a PBIT/template is not a PBIX result.")
            if "Report/Layout" in names:
                document = _document(archive, names["Report/Layout"], 8 * 1024 * 1024)
                sections = document.get("sections")
                if not isinstance(sections, list) or any(not isinstance(item, dict) for item in sections):
                    raise DemoError("PBIX_REPORT_INVALID", "Saved report does not contain a valid page collection.")
                pages = [section.get("displayName") for section in sections]
                visuals = 0
                for section in sections:
                    containers = section.get("visualContainers", [])
                    if not isinstance(containers, list):
                        raise DemoError("PBIX_REPORT_INVALID", "Saved report has an invalid visual collection.")
                    for container in containers:
                        if not isinstance(container, dict):
                            raise DemoError("PBIX_REPORT_INVALID", "Saved report has an invalid visual.")
                        raw_config = container.get("config", "{}")
                        try:
                            config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
                        except (ValueError, RecursionError) as exc:
                            raise DemoError("PBIX_REPORT_INVALID", "Saved visual configuration is invalid.") from exc
                        if not isinstance(config, dict):
                            raise DemoError("PBIX_REPORT_INVALID", "Saved visual configuration must be an object.")
                        if "singleVisualGroup" not in config:
                            visuals += 1
            else:
                page_names = [name for name in names if name.startswith("Report/definition/pages/") and name.endswith("/page.json")]
                if not page_names:
                    raise DemoError("PBIX_REPORT_MISSING", "Saved output contains neither report layout nor PBIR pages.")
                pages = [_document(archive, names[name], limits.metadata_bytes).get("displayName") for name in page_names]
                visuals = sum(
                    isinstance(_document(archive, names[name], limits.metadata_bytes).get("visual"), dict)
                    for name in names if name.startswith("Report/definition/pages/") and name.endswith("/visual.json")
                )
            if not pages or any(not isinstance(page, str) or not page for page in pages) or len(set(pages)) != len(pages):
                raise DemoError("PBIX_REPORT_INVALID", "The PBIX requires nonempty distinct named pages.")
            if project is not None and Counter(pages) != Counter(project.pages):
                raise DemoError("PBIX_PAGES_CHANGED", "Output report pages differ from the submitted project.")
            if project is not None and visuals != project.visual_count:
                raise DemoError("PBIX_VISUALS_CHANGED", "Output visual count differs from the submitted project.")
            if visuals < 1:
                raise DemoError("PBIX_REPORT_INVALID", "The PBIX must contain report visuals.")
            return {
                "binary_model_present": True,
                "pages": pages,
                "visual_count": visuals,
                "bytes": size,
                "inspection": "container metadata, not a substitute for Desktop reopening",
            }
    except (zipfile.BadZipFile, KeyError, EOFError) as exc:
        raise DemoError("PBIX_INVALID", "Desktop output is not a readable PBIX container.") from exc
