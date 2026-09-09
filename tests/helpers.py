import io
import json
import zipfile

from pbip_mcp.synthetic import fixture_files


def archive_bytes(files: dict[str, bytes] | None = None, *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, data in (fixture_files() if files is None else files).items():
            archive.writestr(name, data)
    return stream.getvalue()


def change_json(files: dict[str, bytes], name: str, transform) -> None:
    document = json.loads(files[name])
    transform(document)
    files[name] = json.dumps(document).encode("utf-8")


def fake_pbix(pages: list[str] | None = None, *, model: bool = True, visuals: int = 1) -> bytes:
    """Unit-only structural container. It is explicitly NOT a converted Power BI report."""
    files = {
        "Version": b"1",
        "Report/Layout": json.dumps({"sections": [
            {"displayName": page, "visualContainers": [{"config": "{}"}] * visuals}
            for page in (pages or ["Synthetic overview"])
        ]}).encode("utf-16-le"),
    }
    files["DataModel" if model else "DataModelSchema"] = b"UNIT TEST PLACEHOLDER - NOT A REAL MODEL"
    return archive_bytes(files)
