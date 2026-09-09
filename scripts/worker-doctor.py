import argparse
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path

from pbip_mcp.config import Config
from pbip_mcp.storage import JobStore
from pbip_mcp.windows_session import interactive_readiness


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only readiness diagnosis; never starts Desktop or a cloud VM.")
    parser.add_argument("--root", type=Path, default=Path(r"C:\PBIPMCP"))
    parser.add_argument("--desktop-exe", type=Path, required=True)
    args = parser.parse_args()
    checks = {
        "python_312_x64": sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32,
        "desktop_installed": args.desktop_exe.is_file(),
        "data_directory": (args.root / "data").is_dir(),
    }
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.controls.win32_controls import EditWrapper

    checks["uia_and_native_edit_import"] = UIAWrapper is not None and EditWrapper is not None
    state = {
        "checks": checks,
        "runtime_versions": {name: importlib.metadata.version(name) for name in ("mcp", "pywinauto", "comtypes")},
        "caller_session": interactive_readiness(),
        "worker": JobStore(Config(args.root / "data")).worker_status() if checks["data_directory"] else None,
        "free_bytes": shutil.disk_usage(args.root).free,
        "boundary": "An SSH caller is Session 0; use worker heartbeat and the dedicated RDP session, not caller readiness.",
    }
    print(json.dumps(state, ensure_ascii=True, indent=2))
    raise SystemExit(0 if all(checks.values()) else 2)


if __name__ == "__main__":
    main()
