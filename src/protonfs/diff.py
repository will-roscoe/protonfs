# src/protonfs/diff.py
"""Three-way diff between local scan, index, and remote listing.

:func:`classify` is the entry point: given what's on disk (a local scan), what the index
last recorded, and (optionally) what the remote currently lists, it decides a
:class:`SyncState` for every known path. With no remote view it can only compare the
local files against the index; with a remote view it can additionally tell remote
deletions and remote-side changes apart, and attribute a local change to a direction
(see :func:`_classify_absent` and :func:`_classify_present`).

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

    Without a remote view every state is a statement about the local files and the
    index (what protonfs last recorded), never about Drive. Only the states marked
    *remote view* below say something checked against a live listing.

    :cvar LOCALLY_INDEXED: Local matches the index, and (with a remote view) the remote
        did not diverge from it either. Without a remote view this says nothing about
        Drive: it is the index that matches, not a verified remote copy (#150).
    :cvar LOCAL_ONLY: Present locally, never recorded in the index.
    :cvar REMOTE_ONLY: Absent locally, with the copy on Drive. With a remote view: listed
        remotely but absent both locally and in the index. Without one: a file this machine
        held that is now gone locally, whose last recorded copy is on Drive (``pull``
        restores it, unless told not to; see :func:`held_and_gone`).
    :cvar METADATA_ONLY: Index has a metadata-only record (never materialized locally)
        and, when a remote view exists, the remote still matches it.
    :cvar CONFLICT: Local diverged from the index and no remote view is available to
        attribute the divergence to a direction.
    :cvar LOCAL_MODIFIED: *Remote view.* Local diverged from the index; remote did not.
    :cvar REMOTE_MODIFIED: *Remote view.* Remote diverged from the index; local did not.
    :cvar BOTH_MODIFIED: *Remote view.* Both local and remote diverged from the index
        independently.
    :cvar LOCAL_DELETED: *Remote view.* A previously-materialized local file is now gone
        locally but still present on the remote.
    :cvar REMOTE_CHANGED: *Remote view.* No local file, but the index's remote-side
        record and the current remote listing disagree.
    :cvar REMOTE_DELETED: *Remote view.* An index entry exists but the remote no longer
        lists it.
    :cvar LFS_POINTER: Local file is an un-smudged git-LFS pointer stub (#32), short-
        circuited before any content comparison so its stub hash is never mistaken for
        the tracked file's real content.

    .. versionchanged:: 2.0.0
       ``SYNCED`` (``"synced"``) renamed to ``LOCALLY_INDEXED`` (``"locally-indexed"``),
       since it described a check against the index, not Drive (#150).
       ``SyncState.SYNCED`` remains as a deprecated alias of ``LOCALLY_INDEXED``.
    """

    LOCALLY_INDEXED = "locally-indexed"
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

    # Deprecated alias (same value, so iteration and JSON never show it): code written
    # against 1.x keeps importing. Remove in 4.0.0.
    SYNCED = "locally-indexed"


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
    from protonfs.manifest import is_control_path

    known_paths = set(local) | set(index.all())
    if remote is not None:
        known_paths |= set(remote)
    # #146: `.protonfs/` holds protonfs's own state on both sides (the remote manifest
    # lives there). It is never data, whatever a listing or an older host's index says.
    known_paths = {p for p in known_paths if not is_control_path(p)}

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
        return SyncState.REMOTE_MODIFIED if remote_changed else SyncState.LOCALLY_INDEXED
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
    """An index entry with no local file. Without a remote view a held file that is gone
    locally is ``REMOTE_ONLY`` (its recorded copy is on Drive, and ``pull`` restores it);
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


def held_and_gone(
    diff_entries: list[DiffEntry], index: IndexStore, root, subpath: str | None = None
) -> set[str]:
    """Return the paths this machine held that are now gone from disk, inside ``subpath``.

    A path qualifies when its index entry is not ``metadata-only`` (so it was downloaded
    or uploaded here, never offloaded), it classified as ``remote-only`` (no remote view)
    or ``local-deleted`` (remote view), and nothing exists at it on disk. A path the scan
    skipped but that still exists (now ignored, say) is not gone. These are the files a
    ``pull`` restores by default, ``pull --no-restore`` leaves alone, and ``push`` may
    treat as moved or (with ``deletions="trash"``) trash.

    :param diff_entries: the output of :func:`classify`.
    :param index: the repo's index.
    :param root: the repo root, a :class:`~pathlib.Path`.
    :param subpath: repo-relative scope, or ``None`` for the whole repo.
    :returns: the repo-relative paths.

    .. versionadded:: 3.0.0
    """
    gone = set()
    for e in diff_entries:
        if e.state not in (SyncState.REMOTE_ONLY, SyncState.LOCAL_DELETED):
            continue
        entry = index.get(e.rel_path)
        if (
            entry is not None
            and entry.local_state != "metadata-only"
            and within_subpath(e.rel_path, subpath)
            and not (root / e.rel_path).exists()
        ):
            gone.add(e.rel_path)
    return gone


def pair_moves(deleted: dict[str, tuple], added: dict[str, tuple]) -> list[tuple[str, str]]:
    """Return ``(old, new)`` pairs that look like one file moved or renamed locally.

    A deleted path and an added path pair when their keys are equal: the same sha256 and
    size, plus anything else the caller adds to the key (``push`` adds the whole-second
    mtime, which ``mv`` keeps and regenerating a file does not). A key with an empty
    sha256 or a size of 0 never pairs: every empty file has the same content.

    Pairing is one-to-one. Within a group of identical files, pairs are chosen in rounds:
    first a deleted and an added path that are each other's only candidate with the same
    basename, then the same for the parent directory, and finally the last deleted and
    added path when exactly one of each is left. Anything still ambiguous stays unpaired.

    :param deleted: ``{rel_path: (sha256, size, ...)}`` for paths gone locally (from the
        index).
    :param added: ``{rel_path: (sha256, size, ...)}`` for new local paths (from the scan).
    :returns: the pairs, ``old`` from ``deleted`` and ``new`` from ``added``, ordered by
        ``new``.

    .. versionadded:: 3.0.0
    """
    from pathlib import PurePosixPath

    def by_content(paths: dict[str, tuple]) -> dict[tuple, list[str]]:
        groups: dict[tuple, list[str]] = {}
        for rel, key in paths.items():
            if key[0] and key[1]:
                groups.setdefault(key, []).append(rel)
        return groups

    def unique_matches(olds: list[str], news: list[str], key) -> list[tuple[str, str]]:
        """Pairs whose ``key`` is shared by exactly one old and exactly one new path."""
        old_by: dict[str, list[str]] = {}
        new_by: dict[str, list[str]] = {}
        for rel in olds:
            old_by.setdefault(key(rel), []).append(rel)
        for rel in news:
            new_by.setdefault(key(rel), []).append(rel)
        return [
            (old_by[k][0], new_by[k][0])
            for k in old_by
            if len(old_by[k]) == 1 and len(new_by.get(k, [])) == 1
        ]

    rules = (
        lambda rel: PurePosixPath(rel).name,
        lambda rel: str(PurePosixPath(rel).parent),
    )
    olds_by_content = by_content(deleted)
    pairs: list[tuple[str, str]] = []
    for content, news in by_content(added).items():
        olds = sorted(olds_by_content.get(content, []))
        news = sorted(news)
        for rule in rules:
            if not olds or not news:
                break
            matched = unique_matches(olds, news, rule)
            pairs += matched
            olds = [r for r in olds if r not in {old for old, _ in matched}]
            news = [r for r in news if r not in {new for _, new in matched}]
        if len(olds) == 1 and len(news) == 1:
            pairs.append((olds[0], news[0]))
    return sorted(pairs, key=lambda pair: pair[1])
