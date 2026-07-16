from __future__ import annotations

import datetime
import logging
from pathlib import Path

from protonfs.batching import batches, group_by_parent
from protonfs.context import RepoContext
from protonfs.diff import SyncState, classify
from protonfs.drive import DriveError, TransferResult
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry
from protonfs.lfs import is_pointer_stub
from protonfs.localscan import scan

logger = logging.getLogger(__name__)

# #22: proton-drive can report a file as transferred that never lands on the remote.
# Files that fail post-upload verification are tagged with this on their failure entry so
# the CLI can tell them apart from genuine conflicts (the remedy is a plain retry, not
# --resolve). See `UNDERDELIVERED_KIND`.
UNDERDELIVERED_KIND = "under-delivered"
UNDERDELIVERED_ERROR = "claimed transferred but not verified on the remote (under-delivered)"

# #32: defense-in-depth against classify() ever mis-attributing an un-smudged git-LFS
# pointer stub as pushable (LOCAL_ONLY/CONFLICT/etc). Even if that happens, push refuses
# to upload a file whose bytes parse as a pointer stub rather than risk clobbering the
# real object on Drive with a 131-byte placeholder.
LFS_POINTER_KIND = "lfs-pointer"
LFS_POINTER_ERROR = "refusing to push a git-LFS pointer stub over remote content"


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

    # Push uploads every local change. Without a remote view (classify is called with no
    # remote here) a local!=index diff surfaces as CONFLICT; the direction-aware states are
    # included so behaviour is preserved if a remote view is ever wired in. BOTH_MODIFIED is
    # surfaced exactly as CONFLICT was -- no auto-resolve (that is #5/#7's job).
    to_push = [
        e.rel_path
        for e in diff_entries
        if e.state
        in (
            SyncState.LOCAL_ONLY,
            SyncState.LOCAL_MODIFIED,
            SyncState.CONFLICT,
            SyncState.BOTH_MODIFIED,
        )
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

        # #32 hard guard: refuse to upload any file whose bytes parse as an LFS pointer
        # stub, independent of how it got into `rels` -- classification should already
        # exclude these (see diff.SyncState.LFS_POINTER), but this is the last line of
        # defense against ever overwriting real remote content with a 131-byte placeholder.
        pointer_rels = [rel for rel in rels if is_pointer_stub(ctx.root / rel)]
        for rel in pointer_rels:
            logger.warning("push refused: %s is a git-LFS pointer stub", rel)
            total.failed_items += 1
            total.failures.append(
                {"name": Path(rel).name, "error": LFS_POINTER_ERROR, "kind": LFS_POINTER_KIND}
            )
        rels = [rel for rel in rels if rel not in pointer_rels]
        if not rels:
            continue

        # Files proton-drive did not report as failed/skipped -- candidates to VERIFY
        # against the remote before we trust them as delivered (#22).
        candidates: list[str] = []
        for batch in batches(rels):
            local_paths = [ctx.root / rel for rel in batch]
            result = ctx.drive.upload(local_paths, remote_parent, file_strategy=strategy)
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
            candidates += [rel for rel in batch if Path(rel).name not in failed_names]

        if not candidates:
            continue

        # #22: do NOT trust proton-drive's transferred count -- re-list the remote parent
        # and confirm each candidate actually landed (present, and plaintext claimedSize
        # matches). A file that was claimed-transferred but is absent/short is a silent
        # under-delivery: report it as failed and leave it unindexed so the next push
        # retries it, instead of recording false success and risking data loss on offload.
        identities = ctx.drive.remote_identities(remote_parent)
        for rel in candidates:
            entry = local[rel]
            name = Path(rel).name
            ident = identities.get(name)
            verified = ident is not None and (
                ident.claimed_size is None or ident.claimed_size == entry.size
            )
            if not verified:
                reason = "absent" if ident is None else f"size {ident.claimed_size} != {entry.size}"
                logger.warning("push under-delivery: %s not verified on remote (%s)", rel, reason)
                total.failed_items += 1
                total.failures.append(
                    {"name": name, "error": UNDERDELIVERED_ERROR, "kind": UNDERDELIVERED_KIND}
                )
                continue
            ctx.index.set(
                rel,
                IndexEntry(
                    size=entry.size,
                    mtime=entry.mtime,
                    sha256=entry.sha256,
                    sha1=entry.sha1,
                    remote_path=f"{remote_parent}/{name}",
                    origin_device=ctx.config.device_id,
                    local_state="present",
                    last_synced=now,
                ),
            )
            total.transferred_items += 1

        # #3: persist after each parent group so an interruption (Ctrl-C, dropped
        # connection) resumes from here on the next run instead of re-doing everything.
        # Composed with #1's atomic writes, each of these saves is crash-safe.
        ctx.index.save()
    ctx.index.save()
    return total
