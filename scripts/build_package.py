import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from pbip_mcp.synthetic import fixture_files
from pbip_mcp.fixture_variants import rich_fixture_files
from pbip_mcp.safe_paths import reject_links

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an allowlisted, credential-free worker package.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    required = ("pyproject.toml", "requirements.lock", "README.zh-CN.md")
    for name in required:
        if not (ROOT / name).is_file():
            raise FileNotFoundError(f"Package prerequisite missing: {name}")
    for name in ("worker.py", "windows_session.py", "windows_desktop.py", "convert_process.py", "server.py", "client.py"):
        if not (ROOT / "src" / "pbip_mcp" / name).is_file():
            raise FileNotFoundError(f"Required implementation is missing: {name}")
    names: list[str] = []
    with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for name in required:
            package.write(ROOT / name, name)
            names.append(name)
        for folder, extensions in (("src", {".py"}), ("tests", {".py"}), ("scripts", {".py", ".ps1"})):
            for path in sorted((ROOT / folder).rglob("*")):
                if path.is_file() and path.suffix in extensions and "__pycache__" not in path.parts:
                    reject_links(path)
                    relative = path.relative_to(ROOT).as_posix()
                    package.write(path, relative)
                    names.append(relative)
        for name in ("server.js", "server.test.js", "package.json", "deploy-existing-app.ps1"):
            path = ROOT / "deploy" / "public-gateway" / name
            reject_links(path)
            relative = path.relative_to(ROOT).as_posix()
            package.write(path, relative)
            names.append(relative)
        ui = ROOT / "src" / "pbip_mcp" / "web_ui" / "index.html"
        reject_links(ui)
        if not ui.is_file():
            raise FileNotFoundError("The pilot UI is missing from this deployment.")
        package.write(ui, "src/pbip_mcp/web_ui/index.html")
        names.append("src/pbip_mcp/web_ui/index.html")
        import io

        for variant, files in (("Synthetic", fixture_files()), ("Rich", rich_fixture_files())):
            for name, content in files.items():
                relative = f"sample/{variant}/" + name
                package.writestr(relative, content)
                names.append(relative)
            sample = io.BytesIO()
            with zipfile.ZipFile(sample, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
            package.writestr(f"sample/{variant}.zip", sample.getvalue())
            names.append(f"sample/{variant}.zip")
    with args.output.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    print(json.dumps({"package": str(args.output.resolve()), "sha256": digest, "bytes": args.output.stat().st_size, "files": len(names)}))


if __name__ == "__main__":
    main()
