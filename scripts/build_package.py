import argparse
import hashlib
import io
import json
import os
import stat
import tomllib
import zipfile
from pathlib import Path

from pbip_mcp import __version__
from pbip_mcp.synthetic import fixture_files
from pbip_mcp.fixture_variants import rich_fixture_files
from pbip_mcp.safe_paths import reject_links

ROOT = Path(__file__).resolve().parents[1]
ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            package.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return stream.getvalue()


def package_entries() -> dict[str, bytes]:
    required = ("pyproject.toml", "requirements.lock", "README.zh-CN.md")
    for name in required:
        if not (ROOT / name).is_file():
            raise FileNotFoundError(f"Package prerequisite missing: {name}")
    for name in ("worker.py", "windows_session.py", "windows_desktop.py", "convert_process.py", "server.py", "client.py"):
        if not (ROOT / "src" / "pbip_mcp" / name).is_file():
            raise FileNotFoundError(f"Required implementation is missing: {name}")
    entries: dict[str, bytes] = {}

    def include(path: Path) -> None:
        reject_links(path)
        entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()

    for name in required:
        include(ROOT / name)
    for folder, extensions in (("src", {".py"}), ("tests", {".py", ".ps1"}), ("scripts", {".py", ".ps1"})):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and path.suffix in extensions and "__pycache__" not in path.parts:
                include(path)
    for name in ("server.js", "server.test.js", "package.json", "deploy-existing-app.ps1"):
        include(ROOT / "deploy" / "public-gateway" / name)
    include(ROOT / "src" / "pbip_mcp" / "web_ui" / "index.html")
    for variant, files in (("Synthetic", fixture_files()), ("Rich", rich_fixture_files())):
        for name, content in files.items():
            entries[f"sample/{variant}/" + name] = content
        entries[f"sample/{variant}.zip"] = zip_bytes(files)
    return entries


def build_package(output: Path, manifest: Path | None = None) -> dict:
    manifest = manifest or output.with_suffix(".manifest.json")
    if output.resolve() == manifest.resolve():
        raise ValueError("Package and manifest require different paths.")
    for path in (output, manifest):
        reject_links(path)
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite an existing release file: {path.name}")
    entries = package_entries()
    version = tomllib.loads(entries["pyproject.toml"].decode("utf-8"))["project"]["version"]
    if version != __version__:
        raise ValueError("Builder runtime version differs from the package source version.")
    data = zip_bytes(entries)
    digest = hashlib.sha256(data).hexdigest()
    records = [{"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
               for name, content in sorted(entries.items())]
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        if package.namelist() != [record["path"] for record in records]:
            raise ValueError("Package entries differ from the manifest.")
        for record in records:
            content = package.read(record["path"])
            if len(content) != record["bytes"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError("A package entry failed its manifest check.")
    document = {"format": 1, "package": output.name, "version": version, "bytes": len(data),
                "sha256": digest, "entry_count": len(records), "entries": records}
    manifest_data = (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    for path, content in ((output, data), (manifest, manifest_data)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return {"package": str(output.resolve()), "version": version, "sha256": digest, "bytes": len(data),
            "files": len(records), "manifest": str(manifest.resolve()),
            "manifest_sha256": hashlib.sha256(manifest_data).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an allowlisted worker package and per-entry SHA-256 manifest.")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_package(args.output, args.manifest)))


if __name__ == "__main__":
    main()
