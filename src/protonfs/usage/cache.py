"""SQLite cache of Drive usage figures and folder snapshots, each stamped with its age.

Main entry points: :func:`default_cache_path` and :class:`UsageCache`. A snapshot is a
folder's listing plus the size of every child folder, captured in one pass; it is written
atomically so an interrupted run never leaves a half-written snapshot behind.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

#: Cache schema version; a different stored version drops and recreates every table.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE passes(id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL);
CREATE TABLE figures(path TEXT PRIMARY KEY, size INTEGER, descendants INTEGER,
                     at REAL, pass_id INTEGER);
CREATE TABLE snapshots(path TEXT PRIMARY KEY, uid TEXT, at REAL, pass_id INTEGER,
                       fp_size INTEGER, fp_desc INTEGER, width INTEGER, invalid INTEGER);
CREATE TABLE rows(parent TEXT, ord INTEGER, name TEXT, kind TEXT, uid TEXT, size INTEGER,
                  descendants INTEGER, at REAL, pass_id INTEGER, mtime TEXT,
                  decrypted INTEGER, PRIMARY KEY(parent, ord));
CREATE INDEX rows_uid ON rows(uid);
CREATE TABLE trash(id INTEGER PRIMARY KEY CHECK (id = 1), summary TEXT, at REAL);
"""
_TABLES = ("meta", "passes", "figures", "snapshots", "rows", "trash")


def default_cache_path() -> Path:
    """Return the cache file path without creating anything.

    ``$PROTONFS_DU_CACHE`` if set, else ``$XDG_CACHE_HOME/protonfs/du.sqlite``, else
    ``~/.cache/protonfs/du.sqlite``.

    .. versionadded:: 3.4.0
    """
    env = os.environ.get("PROTONFS_DU_CACHE")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "protonfs" / "du.sqlite"


@dataclass(frozen=True)
class Figure:
    """A measured size and descendant count for a path, with when and in which pass.

    ``size`` and ``descendants`` are ``None`` when unknown; ``at`` is a Unix timestamp.

    .. versionadded:: 3.4.0
    """

    size: int | None
    descendants: int | None
    at: float | None
    pass_id: int | None


@dataclass(frozen=True)
class Row:
    """One entry of a folder listing.

    For folders ``size``, ``descendants``, ``at`` and ``pass_id`` are the figure captured
    in the snapshot's pass (or a cached figure for excluded rows). ``name`` is stored in
    Drive-escaped form.

    .. versionadded:: 3.4.0
    """

    name: str
    kind: str
    uid: str | None
    size: int | None
    descendants: int | None
    at: float | None
    pass_id: int | None
    mtime: str | None
    decrypted: bool


@dataclass(frozen=True)
class Snapshot:
    """A folder's listing plus its child-folder sizes, captured in one pass.

    ``fingerprint`` is the ``(size, descendants)`` pair used for cheap change checks;
    ``rows`` keeps listing order.

    .. versionadded:: 3.4.0
    """

    path: str
    uid: str | None
    at: float
    pass_id: int
    fingerprint: tuple[int, int]
    width: int
    invalid: bool
    rows: tuple[Row, ...]


def _join(parent: str, name: str) -> str:
    esc = name.replace("\\", "\\\\").replace("/", "\\/")
    return parent.rstrip("/") + "/" + esc


class UsageCache:
    """Thread-safe SQLite store for figures, snapshots, passes, trash and metadata.

    Opens with ``check_same_thread=False`` and WAL; every public method holds an internal
    lock. A stored schema version other than :data:`SCHEMA_VERSION` rebuilds all tables.

    :param path: database file; the parent directory is created.
    :raises sqlite3.Error: if the file cannot be opened as a database.

    .. versionadded:: 3.4.0
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        with self._lock:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            row = self._db.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None and row[0] == str(SCHEMA_VERSION):
            return
        self._db.execute("BEGIN")
        try:
            for t in _TABLES:
                self._db.execute(f"DROP TABLE IF EXISTS {t}")
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    self._db.execute(stmt)
            self._db.execute(
                "INSERT INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._db.close()

    def new_pass(self, now: float) -> int:
        """Record a new pass started at ``now`` and return its id."""
        with self._lock:
            cur = self._db.execute("INSERT INTO passes(started) VALUES(?)", (now,))
            return int(cur.lastrowid)

    def figure(self, path: str) -> Figure | None:
        """Return the cached :class:`Figure` for ``path``, or ``None`` if absent."""
        with self._lock:
            r = self._db.execute(
                "SELECT size, descendants, at, pass_id FROM figures WHERE path=?", (path,)
            ).fetchone()
        return Figure(*r) if r else None

    def put_figure(self, path: str, fig: Figure) -> None:
        """Store ``fig`` for ``path``, replacing any earlier figure."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO figures VALUES(?,?,?,?,?)",
                (path, fig.size, fig.descendants, fig.at, fig.pass_id),
            )

    def snapshot(self, path: str) -> Snapshot | None:
        """Return the stored :class:`Snapshot` of ``path`` with its rows, or ``None``."""
        with self._lock:
            s = self._db.execute(
                "SELECT uid, at, pass_id, fp_size, fp_desc, width, invalid "
                "FROM snapshots WHERE path=?",
                (path,),
            ).fetchone()
            if s is None:
                return None
            rows = self._db.execute(
                "SELECT name, kind, uid, size, descendants, at, pass_id, mtime, decrypted "
                "FROM rows WHERE parent=? ORDER BY ord",
                (path,),
            ).fetchall()
        return Snapshot(
            path, s[0], s[1], s[2], (s[3], s[4]), s[5], bool(s[6]),
            tuple(Row(*r[:8], bool(r[8])) for r in rows),
        )

    def _insert_row(self, conn: sqlite3.Connection, parent: str, ord_: int, row: Row) -> None:
        conn.execute(
            "INSERT INTO rows VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (parent, ord_, row.name, row.kind, row.uid, row.size, row.descendants,
             row.at, row.pass_id, row.mtime, int(row.decrypted)),
        )

    def put_snapshot(self, snap: Snapshot) -> None:
        """Replace the snapshot of ``snap.path`` and all its rows in one transaction.

        :param snap: the snapshot to store.
        :raises sqlite3.Error: on database failure; the previous snapshot is then intact.
        """
        with self._lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM rows WHERE parent=?", (snap.path,))
                db.execute(
                    "INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?,?,?)",
                    (snap.path, snap.uid, snap.at, snap.pass_id, snap.fingerprint[0],
                     snap.fingerprint[1], snap.width, int(snap.invalid)),
                )
                for i, row in enumerate(snap.rows):
                    self._insert_row(db, snap.path, i, row)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def restamp(self, path: str, pass_id: int, now: float) -> None:
        """Set the snapshot's and its rows' ``at``/``pass_id`` to ``now``/``pass_id``.

        Values are kept; a missing snapshot is a no-op.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE snapshots SET at=?, pass_id=? WHERE path=?", (now, pass_id, path)
                )
                self._db.execute(
                    "UPDATE rows SET at=?, pass_id=? WHERE parent=?", (now, pass_id, path)
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def invalidate(self, path: str) -> None:
        """Mark the snapshot of ``path`` invalid; other snapshots are untouched."""
        with self._lock:
            self._db.execute("UPDATE snapshots SET invalid=1 WHERE path=?", (path,))

    def path_for_uid(self, uid: str) -> str | None:
        """Return the Drive path of the folder with ``uid``, or ``None`` if unknown.

        Looks at folder rows of every snapshot, then at each snapshot's own uid.
        """
        with self._lock:
            r = self._db.execute(
                "SELECT parent, name FROM rows WHERE uid=? AND kind='folder' LIMIT 1", (uid,)
            ).fetchone()
            if r:
                return _join(r[0], r[1])
            s = self._db.execute(
                "SELECT path FROM snapshots WHERE uid=? LIMIT 1", (uid,)
            ).fetchone()
        return s[0] if s else None

    def get_meta(self, key: str) -> str | None:
        """Return the metadata value for ``key``, or ``None``."""
        with self._lock:
            r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        """Store metadata ``value`` under ``key``."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))

    def clear(self) -> None:
        """Delete all cached data (figures, snapshots, rows, passes, trash, metadata)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for t in ("figures", "snapshots", "rows", "passes", "trash"):
                    self._db.execute(f"DELETE FROM {t}")
                self._db.execute("DELETE FROM meta WHERE key<>'schema_version'")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def put_trash(self, summary_json: str, at: float) -> None:
        """Store the single opaque trash summary blob captured at ``at``."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO trash VALUES(1,?,?)", (summary_json, at)
            )

    def trash(self) -> tuple[str, float] | None:
        """Return ``(summary_json, at)`` for the stored trash summary, or ``None``."""
        with self._lock:
            r = self._db.execute("SELECT summary, at FROM trash WHERE id=1").fetchone()
        return (r[0], r[1]) if r else None
