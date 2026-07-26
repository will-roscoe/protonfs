"""Persistent per-file content-hash cache (``.protonfs/hashcache.json``, gitignored).

Separate from the sync index on purpose: the index records "this file is synced to Drive
at ``remote_path``" -- a fact that needs a remote verify before it can be written -- whereas
the hash cache records the cheap, purely-local fact "the content at ``(rel_path, size,
mtime)`` hashes to ``(sha256, sha1)``". Persisting that lets a re-run, or a resumed
interrupted scan, skip re-hashing unchanged files that are **not yet in the index** (the
index-based ``low_io`` reuse only covers already-synced files, so without this a resumed
push re-hashes the whole tree).

Keyed by ``(rel_path, size, mtime)``: a size or mtime change is a cache miss, so a stale
entry only ever costs a recompute, never a wrong result -- exactly the same trust model as
``low_io``. A corrupt or wrong-schema cache is treated as empty (rebuilt), never fatal.

.. versionadded:: 1.8.0
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

HASHCACHE_FILE_NAME = "hashcache.json"
HASHCACHE_SCHEMA_VERSION = 1


class HashCache:
    """Load, query, and atomically persist the per-file content-hash cache.

    Entries are ``rel_path -> [size, mtime, sha256, sha1]``. Mutations are in memory until
    :meth:`save`. Consulted only when ``low_io`` is set (same trust model as the index-hash
    reuse); always safe to *write* after a real hash, regardless of ``low_io``.
    """

    def __init__(self, repo_root: Path) -> None:
        self._path = repo_root / ".protonfs" / HASHCACHE_FILE_NAME
        self._entries: dict[str, list] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        """Read the cache into memory. A missing, unreadable, or wrong-schema file is
        treated as an empty cache (the cache is a pure optimization -- never fatal)."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (ValueError, OSError):
            return
        if not isinstance(raw, dict) or raw.get("schema_version") != HASHCACHE_SCHEMA_VERSION:
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            self._entries = entries

    def get(self, rel_path: str, size: int, mtime: float) -> tuple[str, str] | None:
        """Return ``(sha256, sha1)`` when a cached entry matches ``size`` and ``mtime``.

        A missing entry, or one whose size/mtime differs (the file changed), is a miss
        (``None``) -- the caller then hashes and :meth:`set`\\ s the fresh value.
        """
        entry = self._entries.get(rel_path)
        if entry is None:
            return None
        c_size, c_mtime, sha256, sha1 = entry
        if c_size == size and c_mtime == mtime:
            return sha256, sha1
        return None

    def set(self, rel_path: str, size: int, mtime: float, sha256: str, sha1: str) -> None:
        """Record the freshly-computed hash for ``rel_path`` (in memory until :meth:`save`)."""
        self._entries[rel_path] = [size, mtime, sha256, sha1]
        self._dirty = True

    def save(self) -> None:
        """Persist the cache atomically (temp file + fsync + os.replace), if dirty.

        Same crash-safe write as :meth:`protonfs.index.IndexStore.save`: a reader (or a
        crash mid-write) sees either the old file or the new one, never a torn one.
        """
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {"schema_version": HASHCACHE_SCHEMA_VERSION, "entries": self._entries}
        data = json.dumps(document, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".hashcache.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
            self._dirty = False
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
