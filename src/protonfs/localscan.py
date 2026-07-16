from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from protonfs.config import CONFIG_DIR_NAME
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexStore
from protonfs.lfs import is_pointer_stub

# Files smaller than this are candidates for a git-LFS pointer stub check (matches the
# heuristic `lfs.find_pointer_stubs` already uses: real pointer files are ~130 bytes).
POINTER_STUB_MAX_SIZE = 200


@dataclass
class ScanEntry:
    rel_path: str
    size: int
    mtime: float
    sha256: str  # protonfs's own content checksum
    sha1: str  # matches proton's plaintext `claimedDigests.sha1`
    is_lfs_pointer: bool = False  # #32: an unmaterialised git-LFS pointer stub, not content


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
        # #32: a small file may be an un-smudged git-LFS pointer stub rather than real
        # content. We still hash it as today -- classify() short-circuits on the flag so
        # the stub's hash is never mistaken for the tracked file's content.
        is_lfs_pointer = size < POINTER_STUB_MAX_SIZE and is_pointer_stub(file_path)
        entries[rel_path] = ScanEntry(
            rel_path=rel_path,
            size=size,
            mtime=mtime,
            sha256=sha256,
            sha1=sha1,
            is_lfs_pointer=is_lfs_pointer,
        )
    return entries
