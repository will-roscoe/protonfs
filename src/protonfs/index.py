from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

INDEX_FILE_NAME = "index.json"

# On-disk schema version. Bump this whenever the persisted shape changes, and register a
# forward migration below so existing repos upgrade transparently on their next save.
#   v0 = legacy pre-versioning format: the document IS the bare {rel_path: entry} map.
#   v1 = {"schema_version": 1, "entries": {rel_path: entry}}.
#   v2 = each entry gains a `sha1` field (proton's plaintext content digest; "" = unknown).
INDEX_SCHEMA_VERSION = 2


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
    while version < INDEX_SCHEMA_VERSION:
        entries = _MIGRATIONS[version](entries)
        version += 1
    return entries


@dataclass
class IndexEntry:
    size: int
    mtime: float
    sha256: str  # protonfs's own content checksum
    sha1: str  # proton's plaintext content digest ("" = unknown / trust-on-first-use)
    remote_path: str
    origin_device: str
    local_state: str  # "present" | "metadata-only"
    last_synced: str  # ISO-8601 timestamp

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> IndexEntry:
        return cls(**data)


class IndexStore:
    def __init__(self, repo_root: Path) -> None:
        self._path = repo_root / ".protonfs" / INDEX_FILE_NAME
        self._entries: dict[str, IndexEntry] = {}
        self._load()

    def _load(self) -> None:
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
        self._entries = {rel_path: IndexEntry.from_dict(data) for rel_path, data in entries.items()}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "entries": {rel_path: entry.to_dict() for rel_path, entry in self._entries.items()},
        }
        data = json.dumps(document, indent=2, sort_keys=True) + "\n"
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
        return self._entries.get(rel_path)

    def set(self, rel_path: str, entry: IndexEntry) -> None:
        self._entries[rel_path] = entry

    def remove(self, rel_path: str) -> None:
        self._entries.pop(rel_path, None)

    def all(self) -> dict[str, IndexEntry]:
        return dict(self._entries)
