from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    archive_bytes: int = 16 * MIB
    uncompressed_bytes: int = 128 * MIB
    member_bytes: int = 64 * MIB
    metadata_bytes: int = 2 * MIB
    file_count: int = 4096
    compression_ratio: int = 200
    path_characters: int = 180
    path_depth: int = 20
    pending_jobs: int = 32
    retained_bytes: int = 2 * 1024 * MIB
    artifact_bytes: int = 512 * MIB
    chunk_bytes: int = 256 * 1024
    conversion_seconds: int = 600
    queue_seconds: int = 1800
    heartbeat_seconds: int = 15
    user_pending_jobs: int = 3
    user_retained_bytes: int = 2 * 1024 * MIB
    submissions_per_hour: int = 30
    global_submissions_per_hour: int = 100
    retention_seconds: int = 24 * 60 * 60
    session_seconds: int = 60 * 60
    upload_overhead_bytes: int = 1024 * 1024
    upload_seconds: int = 120

    @property
    def base64_characters(self) -> int:
        return 4 * ((self.archive_bytes + 2) // 3)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    limits: Limits = Limits()

    @classmethod
    def load(cls, data_dir: Path, file: Path | None = None):
        import json
        from dataclasses import fields
        from .errors import DemoError

        if file is None:
            return cls(data_dir)
        try:
            values = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(values, dict) or set(values) - {field.name for field in fields(Limits)}:
                raise ValueError("Unknown limits.")
            if any(type(value) is not int or value <= 0 for value in values.values()):
                raise ValueError("Limits must be finite positive integers.")
            limits = Limits(**values)
            if limits.archive_bytes > 512 * MIB or limits.uncompressed_bytes > 2 * 1024 * MIB:
                raise ValueError("Upload/expansion hard ceiling exceeded.")
            if limits.conversion_seconds > 7200 or limits.file_count > 20000 or limits.session_seconds > 86400:
                raise ValueError("Time/count hard ceiling exceeded.")
            return cls(data_dir, limits)
        except (ValueError, TypeError, OSError) as exc:
            raise DemoError("LIMITS_CONFIG", "Invalid finite limits configuration.") from exc

    @property
    def database(self) -> Path:
        return self.data_dir / "queue.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"
