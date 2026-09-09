"""Run the explicitly opted-in public-origin portal with a guarded owned PID file."""

import os
import sys
from pathlib import Path

from pbip_mcp.auth import private_file
from pbip_mcp.portal import main


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "--owned-pid-file":
        raise SystemExit("Supply --owned-pid-file <restricted path> before portal arguments.")
    pid_file = Path(sys.argv[2])
    del sys.argv[1:3]
    private_file(pid_file.parent)
    with pid_file.open("x", encoding="ascii") as file:
        file.write(str(os.getpid()))
    private_file(pid_file, create=True)
    try:
        main()
    finally:
        pid_file.unlink()
