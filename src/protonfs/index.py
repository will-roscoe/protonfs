from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

INDEX_FILE_NAME = "index.json"


@dataclass
class IndexEntry:
    size: int
    mtime: float
    sha256: str
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
        self._entries = {rel_path: IndexEntry.from_dict(data) for rel_path, data in raw.items()}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        raw = {rel_path: entry.to_dict() for rel_path, entry in self._entries.items()}
        self._path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")

    def get(self, rel_path: str) -> IndexEntry | None:
        return self._entries.get(rel_path)

    def set(self, rel_path: str, entry: IndexEntry) -> None:
        self._entries[rel_path] = entry

    def remove(self, rel_path: str) -> None:
        self._entries.pop(rel_path, None)

    def all(self) -> dict[str, IndexEntry]:
        return dict(self._entries)
