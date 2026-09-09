"""Bounded filesystem operations for private task trees, not an OS sandbox."""

import os
import shutil
import stat
from pathlib import Path

from .errors import DemoError


def reject_links(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DemoError("UNSAFE_LOCAL_PATH", "A task path contains a link or reparse point.")


def tree_files(root: Path, *, missing_ok: bool = False):
    reject_links(root)
    if not root.exists():
        return

    def walk_error(error: OSError) -> None:
        if not (missing_ok and isinstance(error, FileNotFoundError)):
            raise error

    for directory, folders, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in folders + files:
            path = Path(directory) / name
            reject_links(path)
            if name in files:
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    if missing_ok:
                        continue
                    raise
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise DemoError("UNSAFE_LOCAL_PATH", "Only regular task files are supported.")
                yield path


def tree_bytes(root: Path) -> int:
    """Account for live trees; only concurrent disappearance is harmless here."""
    total = 0
    for path in tree_files(root, missing_ok=True):
        reject_links(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DemoError("UNSAFE_LOCAL_PATH", "Only regular task files are supported.")
        total += info.st_size
    return total


def remove_task_tree(path: Path, task_root: Path) -> None:
    if path == task_root or not path.is_relative_to(task_root):
        raise DemoError("UNSAFE_CLEANUP", "Cleanup must target a named child of this task.")
    reject_links(task_root)
    if path.exists():
        list(tree_files(path))
        shutil.rmtree(path)
