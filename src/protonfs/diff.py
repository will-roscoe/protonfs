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
    REMOTE_CHANGED = "remote-changed"
    REMOTE_DELETED = "remote-deleted"


@dataclass
class DiffEntry:
    rel_path: str
    state: SyncState


def within_subpath(rel_path: str, subpath: str | None) -> bool:
    """True when rel_path lies inside `subpath` (or when there is no subpath).

    `classify` reasons over the whole repo-wide index, so a caller that scoped its
    local scan and remote walk to a subpath MUST filter classify's output with this
    before acting on it -- otherwise index entries outside the subpath (never
    scanned, never walked) are misread as remote-deleted / remote-only.
    """
    if not subpath:
        return True
    return rel_path == subpath or rel_path.startswith(f"{subpath}/")


def classify(
    local: dict[str, ScanEntry],
    index: IndexStore,
    remote: dict[str, int] | None = None,
) -> list[DiffEntry]:
    known_paths = set(local) | set(index.all())
    if remote is not None:
        known_paths |= set(remote)

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
            if remote is not None:
                if rel_path not in remote:
                    state = SyncState.REMOTE_DELETED
                elif remote[rel_path] != index_entry.size:
                    state = SyncState.REMOTE_CHANGED
                else:
                    state = (
                        SyncState.METADATA_ONLY
                        if index_entry.local_state == "metadata-only"
                        else SyncState.REMOTE_ONLY
                    )
            else:
                state = (
                    SyncState.METADATA_ONLY
                    if index_entry.local_state == "metadata-only"
                    else SyncState.REMOTE_ONLY
                )
        else:
            state = SyncState.REMOTE_ONLY
        results.append(DiffEntry(rel_path, state))
    return results
