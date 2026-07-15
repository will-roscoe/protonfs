# src/protonfs/commands/refresh.py
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

from protonfs.context import RepoContext
from protonfs.diff import SyncState, classify, within_subpath
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry
from protonfs.localscan import ScanEntry, scan


@dataclass
class RefreshResult:
    seeded: int = 0
    remote_changed: int = 0
    remote_deleted: int = 0
    pruned: int = 0
    changed_paths: list[str] = field(default_factory=list)
    deleted_paths: list[str] = field(default_factory=list)


def refresh(
    ctx: RepoContext,
    subpath: str | None,
    prune: bool,
    persist: bool = True,
    local: dict[str, ScanEntry] | None = None,
) -> RefreshResult:
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
                        remote_path=f"{ctx.config.remote_root}/{full_rel}",
                        origin_device="unknown",
                        local_state="metadata-only",
                        last_synced=now,
                    ),
                )
                result.seeded += 1
                seeded_here = True
        if persist and seeded_here:
            ctx.index.save()

    entries = ctx.drive.walk(remote_root, on_directory=_seed_directory)
    remote = {e.rel_path: e.size for e in entries if not e.is_dir}
    # rel_paths from the walk are relative to remote_root; if a subpath was given,
    # re-prefix so keys match the index's repo-root-relative rel_paths.
    if subpath:
        remote = {f"{subpath}/{rel}": size for rel, size in remote.items()}

    # Change/deletion detection needs the COMPLETE remote listing (a file is only "deleted"
    # if absent from the whole walk), so it runs after the walk finishes -- unlike seeding,
    # it cannot be incremental.
    diff_entries = classify(local, ctx.index, remote)

    for entry in diff_entries:
        # A subpath-scoped walk only saw files under `subpath`; index entries outside
        # it were never checked, so their absence from `remote` is not a deletion.
        if not within_subpath(entry.rel_path, subpath):
            continue
        if entry.state == SyncState.REMOTE_CHANGED:
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
    return result
