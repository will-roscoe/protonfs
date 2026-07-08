from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexStore


@dataclass
class ScanEntry:
    rel_path: str
    size: int
    mtime: float
    sha256: str


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan(
    root: Path,
    subpath: Path,
    ignore: IgnoreMatcher,
    index: IndexStore,
    low_io: bool = False,
) -> dict[str, ScanEntry]:
    entries: dict[str, ScanEntry] = {}
    base = root / subpath if subpath != Path(".") else root
    for file_path in sorted(base.rglob("*")):
        if not file_path.is_file():
            continue
        rel_path = str(file_path.relative_to(root))
        if ignore.matches(rel_path):
            continue
        stat = file_path.stat()
        size = stat.st_size
        mtime = stat.st_mtime
        cached = index.get(rel_path)
        if low_io and cached is not None and cached.size == size and cached.mtime == mtime:
            sha256 = cached.sha256
        else:
            sha256 = hash_file(file_path)
        entries[rel_path] = ScanEntry(rel_path=rel_path, size=size, mtime=mtime, sha256=sha256)
    return entries
