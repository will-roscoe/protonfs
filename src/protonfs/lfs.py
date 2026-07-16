# src/protonfs/lfs.py
"""git-LFS awareness: detect LFS tracking and the tiny pointer-stub files it leaves.

protonfs must never hash or upload a 130-byte git-LFS pointer stub as if it were real
content (#32) -- doing so would clobber the real object on Drive. These helpers let
setup/scan recognise LFS-managed repos and their un-materialised stubs.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

#: First line every git-LFS pointer file carries; its presence identifies a stub.
POINTER_SIGNATURE = "version https://git-lfs.github.com/spec/v1"


def is_lfs_tracked(repo_root: Path) -> bool:
    """Return whether ``repo_root`` uses git-LFS.

    True when ``.gitattributes`` declares a ``filter=lfs`` rule, or ``git lfs
    ls-files`` reports tracked objects.

    :param repo_root: the git repo root to inspect.
    :returns: ``True`` if the repo tracks anything through git-LFS.
    """
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
    """Return whether ``path`` is an un-materialised git-LFS pointer file.

    :param path: the file to test.
    :returns: ``True`` when its first line is the :data:`POINTER_SIGNATURE` (an
        unreadable file returns ``False``).
    """
    try:
        with path.open("r", errors="ignore") as fh:
            first_line = fh.readline()
    except OSError:
        return False
    return first_line.strip() == POINTER_SIGNATURE


def find_pointer_stubs(root: Path, subpath: Path) -> list[Path]:
    """Recursively find git-LFS pointer-stub files under ``root`` (optionally scoped).

    Only files under 200 bytes are inspected (a real pointer is ~130 bytes), keeping
    the scan cheap.

    :param root: the repo root.
    :param subpath: subtree to scope the search to, or ``Path(".")`` for the whole root.
    :returns: the stub files found, sorted.
    """
    base = root / subpath if subpath != Path(".") else root
    stubs = []
    for file_path in sorted(base.rglob("*")):
        if file_path.is_file() and file_path.stat().st_size < 200 and is_pointer_stub(file_path):
            stubs.append(file_path)
    return stubs
