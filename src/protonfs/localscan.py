"""Walks the local filesystem to produce the "local" side of a three-way diff.

:func:`scan` rglobs a repo (or subpath), skipping ignored files and the ``.protonfs``
control directory, hashing each remaining file into a :class:`ScanEntry`. Its output
feeds :func:`~protonfs.diff.classify` as the ``local`` argument.

.. versionadded:: 1.0.0
"""

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
    """One file's state as observed by a local filesystem scan.

    :ivar rel_path: Repo-relative path.
    :ivar size: File size in bytes, from ``stat()``.
    :ivar mtime: Modification time (seconds since epoch), from ``stat()``.
    :ivar sha256: protonfs's own content checksum.
    :ivar sha1: Matches proton's plaintext ``claimedDigests.sha1``, for comparison against
        a :class:`~protonfs.drive.RemoteEntry` without needing a second hash pass.
    :ivar is_lfs_pointer: True if this is an un-smudged git-LFS pointer stub rather than
        real content (#32); :func:`~protonfs.diff.classify` short-circuits on this flag.
    """

    rel_path: str
    size: int
    mtime: float
    sha256: str  # protonfs's own content checksum
    sha1: str  # matches proton's plaintext `claimedDigests.sha1`
    is_lfs_pointer: bool = False  # #32: an unmaterialised git-LFS pointer stub, not content


def hash_file(path: Path) -> str:
    """Compute the sha256 hex digest of ``path``'s contents.

    :param path: File to hash.
    :returns: Hex-encoded sha256 digest.

    .. note::
       Reads in 1 MiB chunks to bound memory use on large files. When both sha256 and
       sha1 are needed (as in :func:`scan`), prefer :func:`hash_file_digests`, which
       computes both in a single read pass.
    """
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
    """Walk ``root / subpath`` and build a :class:`ScanEntry` for every synced file.

    :param root: Repo root.
    :param subpath: Subdirectory to scan, relative to ``root`` (``Path(".")`` for the
        whole repo).
    :param ignore: Matcher used to exclude files (see :class:`~protonfs.ignore.IgnoreMatcher`).
    :param index: The repo's :class:`~protonfs.index.IndexStore`, consulted for cached
        hashes when ``low_io`` is set.
    :param low_io: If True, reuse a file's previously indexed sha256/sha1 instead of
        rehashing it, whenever the index has an entry with matching ``size`` and
        ``mtime``. Trades a small risk of missing a same-size/same-mtime content change
        for avoiding a full read of every file on each scan.
    :returns: Dict of :class:`ScanEntry` keyed by repo-relative path. The ``.protonfs``
        control directory and any path matched by ``ignore`` are excluded.

    .. seealso:: :func:`~protonfs.diff.classify`, which consumes this as its ``local`` arg.

    .. versionchanged:: 1.5.2
       ``subpath`` may now name a single file, not just a directory or ``.``; a file
       subpath scans exactly that file. A nonexistent subpath still returns ``{}``.
    """
    entries: dict[str, ScanEntry] = {}
    base = root / subpath if subpath != Path(".") else root
    # A subpath may name a single file, a directory, or `.` (the whole repo). rglob on a
    # file (or a nonexistent path) yields nothing, so a file pathspec would otherwise scan
    # to {} -- treat a file `base` as the one candidate. A nonexistent `base` is not a file
    # and rglobs to nothing, so scan() still returns {} for it (pull/status/ls rely on
    # that; push validates existence at the CLI layer).
    candidates = [base] if base.is_file() else sorted(base.rglob("*"))
    for file_path in candidates:
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
