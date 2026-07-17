# src/protonfs/commands/refresh.py
"""``protonfs refresh``: discover remote files and seed the local index (metadata-only)."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

from protonfs import refreshstate
from protonfs.context import RepoContext
from protonfs.diff import SyncState, classify, within_subpath
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry
from protonfs.localscan import ScanEntry, scan


@dataclass
class RefreshResult:
    """Outcome of a :func:`refresh` pass.

    :ivar seeded: metadata-only index entries created for remote-only files.
    :ivar remote_changed: files whose remote copy diverged from the index.
    :ivar remote_deleted: index entries whose remote file is gone.
    :ivar pruned: remote-deleted entries removed from the index (only with ``prune``).
    :ivar changed_paths: rel-paths counted in ``remote_changed``.
    :ivar deleted_paths: rel-paths counted in ``remote_deleted``.
    :ivar resumed: whether this pass resumed a previously-interrupted remote walk.
    """

    seeded: int = 0
    remote_changed: int = 0
    remote_deleted: int = 0
    pruned: int = 0
    changed_paths: list[str] = field(default_factory=list)
    deleted_paths: list[str] = field(default_factory=list)
    resumed: bool = False


def refresh(
    ctx: RepoContext,
    subpath: str | None,
    prune: bool,
    persist: bool = True,
    local: dict[str, ScanEntry] | None = None,
    reporter=None,
) -> RefreshResult:
    """Discover remote files and seed the local index with metadata-only entries.

    Walks the remote tree (scoped to ``subpath`` when given), creating a
    metadata-only :class:`~protonfs.index.IndexEntry` for every remote-only file and,
    on a full (non-resumed) pass, detecting remote changes and deletions. Seeding is
    persisted per-directory so an interrupted walk under API throttling keeps its
    progress; the walk itself is resumable from a saved BFS frontier.

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree to refresh, or ``None`` for everything.
    :param prune: when true, remove index entries whose remote file has been deleted.
    :param persist: when false (a dry-run preview), make no on-disk changes and leave
        no resume-state behind.
    :param local: a pre-computed local scan to reuse (e.g. from ``pull --refresh``),
        avoiding a second recursive walk + re-hash.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    :returns: a :class:`RefreshResult` tallying what was seeded/changed/deleted/pruned.

    .. note::
        On a resumed pass, change/deletion detection is skipped: the walk only
        re-listed the frontier directories, so the partial listing cannot distinguish
        a genuinely deleted remote file from one simply not re-listed yet.

    .. seealso:: :mod:`protonfs.refreshstate` for the resumable-frontier persistence.
    """
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()

    remote_root = ctx.config.remote_root
    if subpath:
        remote_root = f"{remote_root}/{subpath}"

    # `local` may be supplied by a caller (e.g. pull --refresh) that already scanned
    # the same tree, so we don't pay for a second recursive walk + re-hash. Scan BEFORE
    # the remote walk so the per-directory seeding callback can consult it.
    if local is None:
        ignore = IgnoreMatcher.from_file(ctx.root)
        scan_root = Path(subpath) if subpath else Path(".")
        local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)

    result = RefreshResult()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _seed_directory(dir_files: list) -> None:
        # #33: seed metadata-only entries for a directory's remote-only files and persist
        # immediately, so if a later directory wedges under throttle the progress so far
        # survives (crash-safe via #1's atomic writes) instead of being lost.
        seeded_here = False
        for file_entry in dir_files:
            full_rel = f"{subpath}/{file_entry.rel_path}" if subpath else file_entry.rel_path
            if ctx.index.get(full_rel) is None and full_rel not in local:
                ctx.index.set(
                    full_rel,
                    IndexEntry(
                        size=file_entry.size,
                        mtime=0.0,
                        sha256="",
                        sha1="",
                        remote_path=f"{ctx.config.remote_root}/{full_rel}",
                        origin_device="unknown",
                        local_state="metadata-only",
                        last_synced=now,
                    ),
                )
                result.seeded += 1
                seeded_here = True
                reporter.item("seed", full_rel)
        if persist and seeded_here:
            ctx.index.save()

    # #33 item 2: resume an interrupted walk from its saved frontier. A saved frontier for a
    # different root is stale for this pass (load_frontier returns None). Only touch the
    # state file when we are persisting (a dry-run preview must leave no trace).
    saved = refreshstate.load_frontier(ctx.root, remote_root) if persist else None
    result.resumed = saved is not None

    def _save_progress(frontier: list) -> None:
        if persist:
            refreshstate.save_frontier(ctx.root, remote_root, frontier)

    reporter.phase("walking remote", root=remote_root)
    entries = ctx.drive.walk(
        remote_root, on_directory=_seed_directory, frontier=saved, on_progress=_save_progress
    )
    # The pass completed (walk returned) -- drop the frontier so the next refresh starts fresh.
    if persist:
        refreshstate.clear(ctx.root)

    # Change/deletion detection needs the COMPLETE remote listing (a file is only "deleted"
    # if absent from the whole walk), so it runs after the walk finishes -- unlike seeding,
    # it cannot be incremental. On a RESUMED pass, `entries` covers only the directories
    # listed in THIS invocation (the frontier), NOT the whole tree, so running detection
    # against it would falsely flag every already-listed file as remote-deleted. Skip it on
    # resume: seeding still happened per-directory; detection waits for a fresh full pass.
    if result.resumed:
        reporter.done(
            "refreshed",
            seeded=result.seeded,
            changed=result.remote_changed,
            deleted=result.remote_deleted,
        )
        return result

    remote = {e.rel_path: e for e in entries if not e.is_dir}
    # rel_paths from the walk are relative to remote_root; if a subpath was given,
    # re-prefix so keys match the index's repo-root-relative rel_paths.
    if subpath:
        remote = {f"{subpath}/{rel}": entry for rel, entry in remote.items()}

    diff_entries = classify(local, ctx.index, remote)

    for entry in diff_entries:
        # A subpath-scoped walk only saw files under `subpath`; index entries outside
        # it were never checked, so their absence from `remote` is not a deletion.
        if not within_subpath(entry.rel_path, subpath):
            continue
        # REMOTE_CHANGED is a metadata-only entry whose remote size moved; REMOTE_MODIFIED
        # is a locally-present file whose remote copy diverged. Both are "the remote changed
        # under us" for refresh's reporting purposes.
        if entry.state in (SyncState.REMOTE_CHANGED, SyncState.REMOTE_MODIFIED):
            result.remote_changed += 1
            result.changed_paths.append(entry.rel_path)
        elif entry.state == SyncState.REMOTE_DELETED:
            result.remote_deleted += 1
            result.deleted_paths.append(entry.rel_path)
            if prune:
                ctx.index.remove(entry.rel_path)
                result.pruned += 1

    if persist:
        ctx.index.save()
    reporter.done(
        "refreshed",
        seeded=result.seeded,
        changed=result.remote_changed,
        deleted=result.remote_deleted,
    )
    return result
