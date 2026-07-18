# src/protonfs/diff.py
"""Three-way diff between local scan, index, and remote listing.

:func:`classify` is the entry point: given what's on disk (a local scan), what the index
last recorded, and (optionally) what the remote currently lists, it decides a
:class:`SyncState` for every known path. With no remote view it can only distinguish
"local has it" / "index has it" / neither; with a remote view it can additionally tell
local deletions, remote deletions, and remote-side changes apart (see
:func:`_classify_absent` and :func:`_classify_present`).

.. versionadded:: 1.0.0
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from protonfs.drive import RemoteEntry
from protonfs.index import IndexEntry, IndexStore
from protonfs.localscan import ScanEntry


class SyncState(str, Enum):
    """Classification of a single path's sync status, as produced by :func:`classify`.

    :cvar SYNCED: Local matches the index; no remote divergence detected.
    :cvar LOCAL_ONLY: Present locally, never recorded in the index.
    :cvar REMOTE_ONLY: Listed remotely but absent both locally and in the index.
    :cvar METADATA_ONLY: Index has a metadata-only record (never materialized locally)
        and, when a remote view exists, the remote still matches it.
    :cvar CONFLICT: Local diverged from the index and no remote view is available to
        attribute the divergence to a direction.
    :cvar LOCAL_MODIFIED: Local diverged from the index; remote did not.
    :cvar REMOTE_MODIFIED: Remote diverged from the index; local did not.
    :cvar BOTH_MODIFIED: Both local and remote diverged from the index independently.
    :cvar LOCAL_DELETED: A previously-materialized local file is now gone locally but
        still present on the remote.
    :cvar REMOTE_CHANGED: No local file, but the index's remote-side record and the
        current remote listing disagree.
    :cvar REMOTE_DELETED: An index entry exists but the remote no longer lists it.
    :cvar LFS_POINTER: Local file is an un-smudged git-LFS pointer stub (#32), short-
        circuited before any content comparison so its stub hash is never mistaken for
        the tracked file's real content.
    """

    SYNCED = "synced"
    LOCAL_ONLY = "local-only"
    REMOTE_ONLY = "remote-only"
    METADATA_ONLY = "metadata-only"
    CONFLICT = "conflict"
    LOCAL_MODIFIED = "local-modified"
    REMOTE_MODIFIED = "remote-modified"
    BOTH_MODIFIED = "both-modified"
    LOCAL_DELETED = "local-deleted"
    REMOTE_CHANGED = "remote-changed"
    REMOTE_DELETED = "remote-deleted"
    LFS_POINTER = "lfs-pointer"


@dataclass
class DiffEntry:
    """One path's classification result.

    :ivar rel_path: Repo-relative path.
    :ivar state: The :class:`SyncState` assigned by :func:`classify`.
    """

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


def _remote_diverged(remote_entry: RemoteEntry, index_entry: IndexEntry) -> bool:
    """Whether the remote copy differs from what the index last recorded for it.

    Prefer the plaintext sha1 when BOTH sides know it (proton's `claimedDigests.sha1`
    vs the index's stored sha1); an unknown/empty sha1 on either side is trust-on-first-
    use, so we must NOT force a conflict on it -- fall back to size instead. For size we
    compare the plaintext `claimed_size` (falling back to the encrypted `size` only when
    proton did not report a claimed size), never the encrypted size against a plaintext
    index size when we can avoid it.
    """
    if remote_entry.sha1 and index_entry.sha1:
        return remote_entry.sha1 != index_entry.sha1
    remote_size = (
        remote_entry.claimed_size if remote_entry.claimed_size is not None else remote_entry.size
    )
    return remote_size != index_entry.size


def classify(
    local: dict[str, ScanEntry],
    index: IndexStore,
    remote: dict[str, RemoteEntry] | None = None,
) -> list[DiffEntry]:
    """Classify every known path's :class:`SyncState` across local, index, and remote.

    :param local: Local scan results, keyed by relative path (see
        :func:`~protonfs.localscan.scan`).
    :param index: The repo's :class:`~protonfs.index.IndexStore`.
    :param remote: Current remote listing keyed by relative path, or ``None`` if no
        remote walk was performed. Without it, deletions/changes on the remote side
        cannot be distinguished -- see :func:`_classify_absent`.
    :returns: One :class:`DiffEntry` per path in ``local | index.all() | (remote or {})``,
        sorted by ``rel_path``.

    .. seealso::
       :func:`within_subpath` -- filter this result when the scan/walk that produced
       ``local``/``remote`` was itself scoped to a subpath.
    """
    known_paths = set(local) | set(index.all())
    if remote is not None:
        known_paths |= set(remote)

    results: list[DiffEntry] = []
    for rel_path in sorted(known_paths):
        local_entry = local.get(rel_path)
        index_entry = index.get(rel_path)
        remote_entry = remote.get(rel_path) if remote is not None else None

        # #32: an un-smudged git-LFS pointer stub's sha256 is the stub's own hash, not
        # the tracked file's -- comparing it against the index/remote would either mass-
        # false-conflict (8024 seen in the wild) or, worse, classify it LOCAL_ONLY and let
        # push clobber the real remote object with a 131-byte stub. Short-circuit before
        # any hash comparison so an unmaterialised file is never conflict/pushable.
        if local_entry is not None and local_entry.is_lfs_pointer:
            results.append(DiffEntry(rel_path, SyncState.LFS_POINTER))
            continue

        if local_entry is not None and index_entry is None:
            state = SyncState.LOCAL_ONLY
        elif local_entry is not None and index_entry is not None:
            state = _classify_present(local_entry, index_entry, remote_entry)
        elif local_entry is None and index_entry is not None:
            state = _classify_absent(index_entry, remote, remote_entry, rel_path)
        else:
            state = SyncState.REMOTE_ONLY
        results.append(DiffEntry(rel_path, state))
    return results


def _classify_present(
    local_entry: ScanEntry,
    index_entry: IndexEntry,
    remote_entry: RemoteEntry | None,
) -> SyncState:
    """A file present both locally and in the index. Direction is decided by comparing
    the local content against the index, and (when a remote view is available) the remote
    content against the index."""
    local_changed = local_entry.sha256 != index_entry.sha256
    remote_changed = remote_entry is not None and _remote_diverged(remote_entry, index_entry)

    if not local_changed:
        # local matches the index; only the remote could have moved.
        return SyncState.REMOTE_MODIFIED if remote_changed else SyncState.SYNCED
    # local diverged from the index.
    if remote_entry is None:
        # No provable remote view (no walk, or the remote no longer lists it): we cannot
        # attribute a direction, so fall back conservatively to a conflict-class state.
        return SyncState.CONFLICT
    return SyncState.BOTH_MODIFIED if remote_changed else SyncState.LOCAL_MODIFIED


def _classify_absent(
    index_entry: IndexEntry,
    remote: dict[str, RemoteEntry] | None,
    remote_entry: RemoteEntry | None,
    rel_path: str,
) -> SyncState:
    """An index entry with no local file. Without a remote view we keep v0.1 behaviour;
    with one we can tell a local deletion, a remote deletion, and a remote change apart."""
    if remote is None:
        return (
            SyncState.METADATA_ONLY
            if index_entry.local_state == "metadata-only"
            else SyncState.REMOTE_ONLY
        )
    if remote_entry is None:
        return SyncState.REMOTE_DELETED
    if index_entry.local_state == "present":
        # It was synced down as a real local file and is now gone locally, yet still on
        # the remote: a local deletion, distinct from a metadata-only remote file.
        return SyncState.LOCAL_DELETED
    if _remote_diverged(remote_entry, index_entry):
        return SyncState.REMOTE_CHANGED
    return SyncState.METADATA_ONLY
