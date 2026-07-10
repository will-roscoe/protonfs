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
    entries = ctx.drive.walk(remote_root)
    remote = {e.rel_path: e.size for e in entries if not e.is_dir}
    # rel_paths from the walk are relative to remote_root; if a subpath was given,
    # re-prefix so keys match the index's repo-root-relative rel_paths.
    if subpath:
        remote = {f"{subpath}/{rel}": size for rel, size in remote.items()}

    # `local` may be supplied by a caller (e.g. pull --refresh) that already scanned
    # the same tree, so we don't pay for a second recursive walk + re-hash.
    if local is None:
        ignore = IgnoreMatcher.from_file(ctx.root)
        scan_root = Path(subpath) if subpath else Path(".")
        local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)
    diff_entries = classify(local, ctx.index, remote)

    result = RefreshResult()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for rel, size in remote.items():
        if ctx.index.get(rel) is None and rel not in local:
            ctx.index.set(
                rel,
                IndexEntry(
                    size=size,
                    mtime=0.0,
                    sha256="",
                    remote_path=f"{ctx.config.remote_root}/{rel}",
                    origin_device="unknown",
                    local_state="metadata-only",
                    last_synced=now,
                ),
            )
            result.seeded += 1

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
