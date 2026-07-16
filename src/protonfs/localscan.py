from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from protonfs.config import CONFIG_DIR_NAME
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexStore


@dataclass
class ScanEntry:
    rel_path: str
    size: int
    mtime: float
    sha256: str  # protonfs's own content checksum
    sha1: str  # matches proton's plaintext `claimedDigests.sha1`


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_file_digests(path: Path) -> tuple[str, str]:
    """Return (sha256, sha1) for `path`, computed in a single pass over the file so a
    scan pays one read, not two, for the two digests it needs."""
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha256.update(chunk)
            sha1.update(chunk)
    return sha256.hexdigest(), sha1.hexdigest()


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
        if rel_path == CONFIG_DIR_NAME or rel_path.startswith(f"{CONFIG_DIR_NAME}/"):
            continue
        if ignore.matches(rel_path):
            continue
        stat = file_path.stat()
        size = stat.st_size
        mtime = stat.st_mtime
        cached = index.get(rel_path)
        if low_io and cached is not None and cached.size == size and cached.mtime == mtime:
            sha256 = cached.sha256
            sha1 = cached.sha1
        else:
            sha256, sha1 = hash_file_digests(file_path)
        entries[rel_path] = ScanEntry(
            rel_path=rel_path, size=size, mtime=mtime, sha256=sha256, sha1=sha1
        )
    return entries
