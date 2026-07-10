from __future__ import annotations

import datetime
from pathlib import Path

from protonfs.batching import batches, group_by_parent
from protonfs.context import RepoContext
from protonfs.diff import SyncState, classify
from protonfs.drive import DriveError, TransferResult
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry
from protonfs.localscan import scan


def _ensure_remote_dir(ctx: RepoContext, remote_dir: str) -> None:
    root = ctx.config.remote_root.rstrip("/")
    if not remote_dir.startswith(root):
        return
    relative = remote_dir[len(root) :].strip("/")
    if not relative:
        return
    current = root
    for segment in relative.split("/"):
        try:
            ctx.drive.create_folder(current, segment)
        except DriveError:
            pass  # already exists -- a real failure will surface on the upload call below
        current = f"{current}/{segment}"


def push(
    ctx: RepoContext,
    subpath: str | None,
    resolve: str | None,
    dry_run: bool,
) -> TransferResult:
    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)
    diff_entries = classify(local, ctx.index)

    to_push = [
        e.rel_path for e in diff_entries if e.state in (SyncState.LOCAL_ONLY, SyncState.CONFLICT)
    ]
    if dry_run or not to_push:
        return TransferResult(len(to_push), 0, 0, [])

    # D2.1: default push applies NO conflict strategy so the CLI surfaces conflicts as
    # named per-file failures (never a silent skip that we'd falsely index). A strategy
    # is used only when the user explicitly asks via --resolve.
    strategy = resolve
    groups = group_by_parent(to_push)
    total = TransferResult(0, 0, 0, [])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for parent, rels in groups.items():
        remote_parent = (
            f"{ctx.config.remote_root}/{parent}" if parent != "." else ctx.config.remote_root
        )
        _ensure_remote_dir(ctx, remote_parent)
        for batch in batches(rels):
            local_paths = [ctx.root / rel for rel in batch]
            result = ctx.drive.upload(local_paths, remote_parent, file_strategy=strategy)
            total.transferred_items += result.transferred_items
            total.skipped_items += result.skipped_items
            total.failed_items += result.failed_items
            total.failures += result.failures

            # D2.1: a skip is reported only as an aggregate count, so we cannot tell
            # WHICH files in the batch were skipped. Rather than falsely record an
            # unconfirmed hash, index none of this batch's non-failed files and leave
            # them for the next push.
            if result.skipped_items > 0:
                continue
            failed_names = {f["name"] for f in result.failures}
            for rel in batch:
                if Path(rel).name in failed_names:
                    continue
                entry = local[rel]
                remote_path = f"{remote_parent}/{Path(rel).name}"
                ctx.index.set(
                    rel,
                    IndexEntry(
                        size=entry.size,
                        mtime=entry.mtime,
                        sha256=entry.sha256,
                        remote_path=remote_path,
                        origin_device=ctx.config.device_id,
                        local_state="present",
                        last_synced=now,
                    ),
                )
    ctx.index.save()
    return total
