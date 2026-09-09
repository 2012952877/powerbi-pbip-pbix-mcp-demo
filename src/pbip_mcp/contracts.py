from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProjectInfo:
    pointer: str
    report: str
    model: str
    pages: tuple[str, ...]
    visual_count: int
    model_format: str
    synthetic_fixture: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "pointer": self.pointer,
            "report": self.report,
            "model": self.model,
            "pages": list(self.pages),
            "visual_count": self.visual_count,
            "model_format": self.model_format,
            "synthetic_fixture": self.synthetic_fixture,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProjectInfo":
        return cls(
            pointer=value["pointer"],
            report=value["report"],
            model=value["model"],
            pages=tuple(value["pages"]),
            visual_count=value["visual_count"],
            model_format=value["model_format"],
            synthetic_fixture=value.get("synthetic_fixture", False),
        )


@dataclass(frozen=True)
class ConversionRequest:
    project_file: Path
    output_file: Path
    evidence_dir: Path
    desktop_exe: Path
    project: ProjectInfo
    timeout_seconds: int = 600
    review_seconds: int = 0
    direction: str = "pbip_to_pbix"
    export_mode: str | None = None
    limits: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversionResult:
    """Returned only after Save As and a fresh Desktop reopening succeeded."""

    desktop_pid: int
    reopened_pid: int
    pages: tuple[str, ...]
    model_present: bool
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "desktop_pid": self.desktop_pid,
            "reopened_pid": self.reopened_pid,
            "pages": list(self.pages),
            "model_present": self.model_present,
            "details": self.details,
        }
