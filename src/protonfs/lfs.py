# src/protonfs/lfs.py
from __future__ import annotations

import subprocess
from pathlib import Path

POINTER_SIGNATURE = "version https://git-lfs.github.com/spec/v1"


def is_lfs_tracked(repo_root: Path) -> bool:
    gitattributes = repo_root / ".gitattributes"
    if gitattributes.exists() and "filter=lfs" in gitattributes.read_text():
        return True
    result = subprocess.run(
        ["git", "-C", str(repo_root), "lfs", "ls-files"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def is_pointer_stub(path: Path) -> bool:
    try:
        with path.open("r", errors="ignore") as fh:
            first_line = fh.readline()
    except OSError:
        return False
    return first_line.strip() == POINTER_SIGNATURE


def find_pointer_stubs(root: Path, subpath: Path) -> list[Path]:
    base = root / subpath if subpath != Path(".") else root
    stubs = []
    for file_path in sorted(base.rglob("*")):
        if file_path.is_file() and file_path.stat().st_size < 200 and is_pointer_stub(file_path):
            stubs.append(file_path)
    return stubs
