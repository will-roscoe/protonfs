# src/protonfs/commands/pull.py
from __future__ import annotations

import datetime
from pathlib import Path

from protonfs.batching import batches, group_by_parent
from protonfs.context import RepoContext
from protonfs.diff import SyncState, classify
from protonfs.drive import TransferResult
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry
from protonfs.localscan import hash_file, scan


def pull(
    ctx: RepoContext,
    subpath: str | None,
    resolve: str | None,
    dry_run: bool,
    refresh: bool = False,
) -> TransferResult:
    if refresh and not dry_run:
        from protonfs.commands.refresh import refresh as refresh_index

        refresh_index(ctx, subpath, prune=False)
    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)
    diff_entries = classify(local, ctx.index)

    to_pull = [
        e.rel_path
        for e in diff_entries
        if e.state in (SyncState.REMOTE_ONLY, SyncState.METADATA_ONLY)
    ]
    if dry_run or not to_pull:
        return TransferResult(len(to_pull), 0, 0, [])

    strategy = resolve or ctx.config.defaults.on_conflict
    groups = group_by_parent(to_pull)
    total = TransferResult(0, 0, 0, [])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for parent, rels in groups.items():
        local_folder = ctx.root if parent == "." else ctx.root / parent
        local_folder.mkdir(parents=True, exist_ok=True)
        for batch in batches(rels):
            remote_paths = []
            for rel in batch:
                entry = ctx.index.get(rel)
                default_remote = f"{ctx.config.remote_root}/{rel}"
                remote_paths.append(entry.remote_path if entry else default_remote)
            result = ctx.drive.download(remote_paths, local_folder, file_strategy=strategy)
            total.transferred_items += result.transferred_items
            total.skipped_items += result.skipped_items
            total.failed_items += result.failed_items
            total.failures += result.failures

            failed_names = {f["name"] for f in result.failures}
            for rel in batch:
                if Path(rel).name in failed_names:
                    continue
                downloaded_path = ctx.root / rel
                if not downloaded_path.exists():
                    continue
                stat = downloaded_path.stat()
                prior = ctx.index.get(rel)
                default_remote = f"{ctx.config.remote_root}/{rel}"
                ctx.index.set(
                    rel,
                    IndexEntry(
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        sha256=hash_file(downloaded_path),
                        remote_path=prior.remote_path if prior else default_remote,
                        origin_device=prior.origin_device if prior else "unknown",
                        local_state="present",
                        last_synced=now,
                    ),
                )
    ctx.index.save()
    return total
