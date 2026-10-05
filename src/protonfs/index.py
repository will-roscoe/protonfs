"""The local sync manifest: ``.protonfs/index.json`` and its schema-versioned store.

The index records, per tracked file, what protonfs last knew about it (size, mtimes,
content digests, remote path, sync state). It is the source of truth every command
diffs the working tree and the remote against. On-disk it is schema-versioned and
migrated forward transparently on load; it is written atomically so a crash never
leaves a torn manifest.

.. versionadded:: 1.0.0
"""
from __future__ import annotations

import errno
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

INDEX_FILE_NAME = "index.json"

# #170: on a glusterfs FUSE mount the temp file save() has just written and fsynced is
# sometimes missing at the rename (ENOENT; ESTALE is the NFS-style form of the same
# thing). The content is still in memory, so save() writes a fresh temp file and tries
# again, this many times in all, sleeping _SAVE_BACKOFF_S * 2**n between attempts.
# Any other error is not about a lost temp file, and fails at once.
_SAVE_ATTEMPTS = 5
_SAVE_BACKOFF_S = 0.1
_VANISHED_ERRNOS = frozenset({errno.ENOENT, errno.ESTALE})

# On-disk schema version. Bump this whenever the persisted shape changes, and register a
# forward migration below so existing repos upgrade transparently on their next save.
#   v0 = legacy pre-versioning format: the document IS the bare {rel_path: entry} map.
#   v1 = {"schema_version": 1, "entries": {rel_path: entry}}.
#   v2 = each entry gains a `sha1` field (proton's plaintext content digest; "" = unknown).
INDEX_SCHEMA_VERSION = 2

# How strict the checks are that this release applies before it records an index entry.
# Bump it in the same change that makes a check stricter (see CONTRIBUTING.md): an index
# recorded under a lower level is then re-verified against Drive by
# `protonfs upgrade` (#169; see protonfs.indexcheck).
#   0 -- anything before 2.4.0, including entries verified by name only before 1.11.3
#        (#147/#148), appended files left at their first upload before 1.12.3 (#144/#155),
#        and git-LFS pointer stubs indexed as content before the #32 fix.
#   1 -- 2.4.0: plaintext size always, sha1 where Drive has one; a changed file and this
#        file's own short upload are replaced as revisions (#144, #168).
CHECK_LEVEL = 1


class IndexSchemaError(RuntimeError):
    """The on-disk index uses a schema this build of protonfs does not understand.

    Raised only for a *newer* schema than we know how to read: an older index is migrated
    forward transparently, but a newer one cannot be safely downgraded, so we refuse rather
    than silently drop fields. The remedy is to upgrade protonfs.
    """


def _split_document(raw: dict) -> tuple[int, dict]:
    """Return (schema_version, entries) for either the versioned or legacy on-disk format."""
    if isinstance(raw.get("schema_version"), int) and isinstance(raw.get("entries"), dict):
        return raw["schema_version"], raw["entries"]
    # Legacy v0: the whole document is the entries map (no wrapper).
    return 0, raw


def _add_sha1(entries: dict) -> dict:
    """v1 -> v2: inject an empty `sha1` into every entry. `IndexEntry.from_dict` does
    `cls(**data)`, so an entry dict missing the new required key would raise a TypeError;
    seeding "" (unknown / trust-on-first-use) keeps every pre-v2 entry loadable."""
    for data in entries.values():
        data.setdefault("sha1", "")
    return entries


# Forward migrations, keyed by the version they upgrade FROM (n -> n+1). v0 -> v1 only added
# the wrapper, so the entries themselves are unchanged; v1 -> v2 adds the `sha1` field.
_MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    0: lambda entries: entries,
    1: _add_sha1,
}


def _migrate(version: int, entries: dict) -> dict:
    """Apply every forward migration from ``version`` up to :data:`INDEX_SCHEMA_VERSION`.

    :param version: the on-disk schema version the entries were loaded at.
    :param entries: the ``{rel_path: entry-dict}`` map to migrate in place.
    :returns: the entries at the current schema version.
    """
    while version < INDEX_SCHEMA_VERSION:
        entries = _MIGRATIONS[version](entries)
        version += 1
    return entries


@dataclass
class IndexEntry:
    """One tracked file's last-known sync state.

    :ivar size: the file's byte size as protonfs last saw it.
    :ivar mtime: the local mtime (POSIX seconds); ``0.0`` for a metadata-only entry.
    :ivar sha256: protonfs's own content checksum (``""`` when not yet computed).
    :ivar sha1: proton's plaintext content digest (``""`` = unknown / trust-on-first-use).
    :ivar remote_path: the file's absolute path on Drive.
    :ivar origin_device: the device that last wrote this entry.
    :ivar local_state: ``"present"`` (materialised locally) or ``"metadata-only"``.
    :ivar last_synced: ISO-8601 timestamp of the last sync of this entry.
    """

    size: int
    mtime: float
    sha256: str  # protonfs's own content checksum
    sha1: str  # proton's plaintext content digest ("" = unknown / trust-on-first-use)
    remote_path: str
    origin_device: str
    local_state: str  # "present" | "metadata-only"
    last_synced: str  # ISO-8601 timestamp

    def to_dict(self) -> dict:
        """Return this entry as a plain JSON-serialisable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> IndexEntry:
        """Build an :class:`IndexEntry` from a persisted dict.

        :param data: a dict with exactly this dataclass's fields (post-migration).
        :raises TypeError: if ``data`` is missing a field or carries an unknown one.
        """
        return cls(**data)


class IndexStore:
    """Load, mutate, and atomically persist the ``.protonfs/index.json`` manifest.

    Loading migrates an older on-disk schema forward transparently (in memory);
    :meth:`save` always writes the current schema atomically. Mutations are in-memory
    until :meth:`save` is called.

    .. seealso:: :func:`protonfs.migrations.run_migrations` persists a stale on-disk
        index at the current schema as one of the repo-state migrations.

    .. versionchanged:: 2.1.0
       Records which remote-manifest generation the index was last reconciled with
       (:attr:`manifest_state`), as an optional top-level ``manifest`` key. It is not a
       schema change: an older protonfs ignores the key, and dropping it on save only
       makes the index look stale, never current (#146).

    .. versionchanged:: 2.4.0
       Records push's own uploads that failed verification (:meth:`underdelivered`), as
       an optional top-level ``underdelivered`` key, so the next push can replace them
       (#168). Not a schema change either: an older protonfs drops the key, and that
       push then reports the file as a conflict, as it did before.

       Records the :data:`CHECK_LEVEL` its entries were verified under
       (:attr:`check_level`), as an optional top-level ``check_level`` key: an index an
       earlier release wrote has none, and reads as level 0 (#169).
    """

    def __init__(self, repo_root: Path) -> None:
        """Open (and load, if present) the index for ``repo_root``.

        :param repo_root: the protonfs root whose ``.protonfs/index.json`` to manage.
        :raises IndexSchemaError: if the on-disk index is a newer schema than this build.
        """
        self._path = repo_root / ".protonfs" / INDEX_FILE_NAME
        self._entries: dict[str, IndexEntry] = {}
        self._manifest: dict | None = None
        self._underdelivered: dict[str, dict] = {}
        # A new index holds only what this release records; an existing one is read below.
        self._check_level = CHECK_LEVEL
        self._load()

    def _load(self) -> None:
        """Read + migrate the on-disk index into memory (no-op when the file is absent).

        :raises IndexSchemaError: when the file's schema is newer than this build.
        """
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        version, entries = _split_document(raw)
        if version > INDEX_SCHEMA_VERSION:
            raise IndexSchemaError(
                f"{self._path} is schema v{version}, but this protonfs understands up to "
                f"v{INDEX_SCHEMA_VERSION}. Upgrade protonfs to read this index."
            )
        entries = _migrate(version, entries)
        level = raw.get("check_level") if version else None
        self._check_level = level if isinstance(level, int) else 0
        self._entries = {rel_path: IndexEntry.from_dict(data) for rel_path, data in entries.items()}
        manifest = raw.get("manifest") if version else None
        if isinstance(manifest, dict) and isinstance(manifest.get("generation"), int):
            self._manifest = {
                "generation": manifest["generation"],
                "revision": str(manifest.get("revision") or ""),
            }
        underdelivered = raw.get("underdelivered") if version else None
        if isinstance(underdelivered, dict):
            self._underdelivered = {
                rel_path: {"remote_path": record["remote_path"], "revision": record["revision"]}
                for rel_path, record in underdelivered.items()
                if isinstance(record, dict)
                and isinstance(record.get("remote_path"), str)
                and isinstance(record.get("revision"), str)
            }

    def save(self) -> None:
        """Persist the index atomically at the current schema version.

        Writes to a temp file on the same filesystem and ``os.replace``\\ s it onto the
        real path, so a reader (or a crash mid-write) sees either the old file or the
        new one, never a torn one.

        :raises OSError: when the write or rename fails; a temp file that vanished
            before its rename is first rewritten and retried a few times.

        .. versionchanged:: 2.4.0
           Retries, with a fresh temp file, when the temp file is gone by the time of the
           rename, as happens on some network/FUSE mounts (#170).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "check_level": self._check_level,
            "entries": {rel_path: entry.to_dict() for rel_path, entry in self._entries.items()},
        }
        if self._manifest is not None:
            document["manifest"] = dict(self._manifest)
        if self._underdelivered:
            document["underdelivered"] = {
                rel_path: dict(record) for rel_path, record in self._underdelivered.items()
            }
        data = json.dumps(document, indent=2, sort_keys=True) + "\n"
        for attempt in range(_SAVE_ATTEMPTS):
            try:
                self._write_atomically(data)
                return
            except OSError as exc:
                if exc.errno not in _VANISHED_ERRNOS or attempt == _SAVE_ATTEMPTS - 1:
                    raise
                logger.warning(
                    "index save: %s (attempt %d of %d); writing it again",
                    exc, attempt + 1, _SAVE_ATTEMPTS,
                )
                time.sleep(_SAVE_BACKOFF_S * 2**attempt)

    def checkpoint(self) -> bool:
        """Save progress part-way through a command, without letting a failure abort it.

        A failed save leaves the in-memory index intact, so the command carries on and
        its final :meth:`save` persists everything; that one raises if it fails too.

        :returns: whether the index was saved.

        .. versionadded:: 2.4.0
        """
        try:
            self.save()
        except OSError as exc:
            logger.warning("index progress save failed, will retry at the end: %s", exc)
            return False
        return True

    def _write_atomically(self, data: str) -> None:
        """Write ``data`` to a fresh temp file beside the index and rename it into place."""
        # Write to a temp file in the SAME directory (same filesystem, so os.replace is a
        # true atomic rename) and swap it onto the real path. A reader — or a crash — never
        # sees a torn or truncated index: it sees either the old file or the new one.
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=".index.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def get(self, rel_path: str) -> IndexEntry | None:
        """Return the entry for ``rel_path``, or ``None`` if it is not tracked."""
        return self._entries.get(rel_path)

    def set(self, rel_path: str, entry: IndexEntry) -> None:
        """Add or replace the entry for ``rel_path`` (in memory until :meth:`save`).

        An indexed file has nothing left to replace, so this clears its
        :meth:`underdelivered` record.
        """
        self._entries[rel_path] = entry
        self._underdelivered.pop(rel_path, None)

    def remove(self, rel_path: str) -> None:
        """Drop ``rel_path`` from the index if present (in memory until :meth:`save`),
        with its :meth:`underdelivered` record."""
        self._entries.pop(rel_path, None)
        self._underdelivered.pop(rel_path, None)

    def all(self) -> dict[str, IndexEntry]:
        """Return a shallow copy of the full ``{rel_path: entry}`` map."""
        return dict(self._entries)

    @property
    def manifest_state(self) -> dict | None:
        """``{"generation": int, "revision": str}`` of the remote manifest this index was
        last reconciled with, or ``None`` if it never was (#146).

        .. versionadded:: 2.1.0
        """
        return dict(self._manifest) if self._manifest is not None else None

    def set_manifest_state(self, generation: int, revision: str) -> None:
        """Record the manifest generation/revision this index now reflects (in memory
        until :meth:`save`).

        .. versionadded:: 2.1.0
        """
        self._manifest = {"generation": int(generation), "revision": revision or ""}

    def underdelivered(self, rel_path: str) -> dict | None:
        """``{"remote_path": str, "revision": str}`` for an upload of ``rel_path`` that
        push could not verify, or ``None``.

        The record says the node at ``remote_path`` holds push's own short upload of the
        file, while its active revision is still ``revision``. The file stays out of the
        index: nothing about it is synced. The next push may then replace that copy with
        a revision, instead of reporting it as a conflict (#168).

        .. versionadded:: 2.4.0
        """
        record = self._underdelivered.get(rel_path)
        return dict(record) if record is not None else None

    def mark_underdelivered(self, rel_path: str, remote_path: str, revision: str) -> None:
        """Record that ``remote_path`` holds push's own unverified upload of ``rel_path``,
        as ``revision`` (in memory until :meth:`save`). See :meth:`underdelivered`.

        .. versionadded:: 2.4.0
        """
        self._underdelivered[rel_path] = {"remote_path": remote_path, "revision": revision}

    @property
    def check_level(self) -> int:
        """The :data:`CHECK_LEVEL` every entry in this index is known to meet: the current
        level for an index this release created, ``0`` for one an earlier release wrote,
        and raised when :func:`protonfs.indexcheck.reverify_index` has re-verified a
        whole index against Drive (#169).

        .. versionadded:: 2.4.0
        """
        return self._check_level

    def set_check_level(self, level: int) -> None:
        """Record the check level this index's entries meet (in memory until :meth:`save`).

        .. versionadded:: 2.4.0
        """
        self._check_level = int(level)
