import argparse
import json
import traceback
from pathlib import Path

from .contracts import ConversionRequest, ProjectInfo
from .errors import DemoError
from .storage import atomic_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Private worker subprocess, not an MCP client interface.")
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    request_data = json.loads(args.request.read_text(encoding="utf-8"))
    request = ConversionRequest(
        project_file=Path(request_data["project_file"]),
        output_file=Path(request_data["output_file"]),
        evidence_dir=Path(request_data["evidence_dir"]),
        desktop_exe=Path(request_data["desktop_exe"]),
        project=ProjectInfo.from_dict(request_data["project"]),
        timeout_seconds=request_data["timeout_seconds"],
        review_seconds=request_data.get("review_seconds", 0),
        direction=request_data.get("direction", "pbip_to_pbix"),
        export_mode=request_data.get("export_mode"),
        limits=request_data.get("limits", {}),
    )
    try:
        from .windows_desktop import WindowsDesktopConverter

        result = WindowsDesktopConverter().convert(request)
        payload = {"ok": True, "result": result.as_dict()}
        code = 0
    except DemoError as exc:
        traceback.print_exc()
        payload = {"ok": False, "error": exc.as_dict()}
        code = 1
    except Exception:
        # A process boundary must durably report failure, never leave success-shaped output.
        traceback.print_exc()
        payload = {"ok": False, "error": {
            "code": "CONVERTER_INTERNAL_ERROR",
            "message": "Converter failed unexpectedly; an operator must inspect this job's private diagnostics.",
        }}
        code = 1
    atomic_json(request.evidence_dir / "result.json", payload)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
