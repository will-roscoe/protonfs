# src/protonfs/diff.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from protonfs.index import IndexStore
from protonfs.localscan import ScanEntry


class SyncState(str, Enum):
    SYNCED = "synced"
    LOCAL_ONLY = "local-only"
    REMOTE_ONLY = "remote-only"
    METADATA_ONLY = "metadata-only"
    CONFLICT = "conflict"


@dataclass
class DiffEntry:
    rel_path: str
    state: SyncState


def classify(
    local: dict[str, ScanEntry],
    index: IndexStore,
    remote_rel_paths: set[str] | None = None,
) -> list[DiffEntry]:
    known_paths = set(local) | set(index.all())
    if remote_rel_paths is not None:
        known_paths |= remote_rel_paths

    results: list[DiffEntry] = []
    for rel_path in sorted(known_paths):
        local_entry = local.get(rel_path)
        index_entry = index.get(rel_path)

        if local_entry is not None and index_entry is None:
            state = SyncState.LOCAL_ONLY
        elif local_entry is not None and index_entry is not None:
            state = (
                SyncState.SYNCED
                if local_entry.sha256 == index_entry.sha256
                else SyncState.CONFLICT
            )
        elif local_entry is None and index_entry is not None:
            state = (
                SyncState.METADATA_ONLY
                if index_entry.local_state == "metadata-only"
                else SyncState.REMOTE_ONLY
            )
        else:
            state = SyncState.REMOTE_ONLY
        results.append(DiffEntry(rel_path, state))
    return results
