"""``protonfs push``: upload local-only/changed files to Drive, creating remote dirs.

.. versionadded:: 1.0.0
"""
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


# Proton Drive's true root only holds special areas; user files must live under one of these
# (in practice /my-files). `create-folder /` is rejected, so a remote_root that does not start
# with a known area can never be created and every upload would fail deep in a batch (#17).
KNOWN_TOP_LEVEL_AREAS = ("/my-files",)


def ensure_remote_root(ctx: RepoContext) -> None:
    """Ensure the configured `remote_root` itself exists on Drive, creating each missing
    segment beneath its top-level area. `_ensure_remote_dir` only creates directories BELOW
    remote_root and assumes remote_root already exists; this creates remote_root itself so a
    first push against a brand-new Drive location works without hand-creating folders (#17).

    Raises DriveError with a precise message when remote_root does not live under a known
    area (e.g. `/myproject` instead of `/my-files/myproject`), rather than letting uploads
    fail obscurely later.
    """
    remote_root = ctx.config.remote_root.rstrip("/")
    area = next(
        (a for a in KNOWN_TOP_LEVEL_AREAS if remote_root == a or remote_root.startswith(a + "/")),
        None,
    )
    if area is None:
        raise DriveError(
            f"remote_root {remote_root!r} must live under {' or '.join(KNOWN_TOP_LEVEL_AREAS)} "
            f"(e.g. /my-files/myproject). Proton Drive's root only holds special areas and "
            f"cannot store files directly, so this path can never be created."
        )
    relative = remote_root[len(area) :].strip("/")
    if not relative:
        return  # the area itself always exists
    current = area
    for segment in relative.split("/"):
        try:
            ctx.drive.create_folder(current, segment)
        except DriveError:
            pass  # already exists -- a real failure surfaces on the upload/create below
        current = f"{current}/{segment}"


def _ensure_remote_dir(ctx: RepoContext, remote_dir: str) -> None:
    """Create every missing intermediate Drive folder from ``remote_root`` to ``remote_dir``.

    Walks the path segment by segment, creating each; an "already exists" error is
    ignored (a genuine failure surfaces on the subsequent upload). No-op when
    ``remote_dir`` is outside ``remote_root`` or is the root itself.

    :param ctx: the loaded repo context.
    :param remote_dir: absolute Drive directory the upload targets.
    """
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
    reporter=None,
) -> TransferResult:
    """Upload local-only and locally-changed files to Drive.

    Scans the tree, classifies each file against the index, and uploads everything
    local-only or changed, creating any missing remote directories first. On success
    the index is updated to reflect the new synced state.

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree to push, or ``None`` for everything.
    :param resolve: conflict policy ``merge`` | ``keep-both`` | ``replace`` | ``skip``
        for files that diverged on both sides, or ``None`` to surface them unresolved.
    :param dry_run: when true, report what would upload without transferring or
        persisting anything.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    :returns: a :class:`~protonfs.drive.TransferResult` of what was uploaded/skipped.
    :raises protonfs.drive.DriveError: on a Drive or lock failure.

    .. seealso:: :func:`protonfs.commands.pull.pull` for the download direction.
    """
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
    reporter.phase("scanning local", subpath=subpath or ".")
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

    reporter.phase("uploading", files=len(to_push))

    # #17: make sure remote_root itself exists (and is a valid path) before uploading, so a
    # first push to a brand-new Drive location works instead of failing on every file.
    ensure_remote_root(ctx)

    # D2.1: default push applies NO conflict strategy so the CLI surfaces conflicts as
    # named per-file failures (never a silent skip that we'd falsely index). A strategy
    # is used only when the user explicitly asks via --resolve.
    strategy = resolve
    batch_size = ctx.config.defaults.batch_size
    groups = group_by_parent(to_push)
    total = TransferResult(0, 0, 0, [])
    done = 0  # files handed to proton-drive so far, for reporter.progress (#93)
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
        # #93: refused stubs still count as processed, so progress never stalls short.
        done += len(pointer_rels)
        if pointer_rels:
            reporter.progress(done, len(to_push))
        rels = [rel for rel in rels if rel not in pointer_rels]
        if not rels:
            continue

        # Files proton-drive did not report as failed/skipped -- candidates to VERIFY
        # against the remote before we trust them as delivered (#22).
        candidates: list[str] = []
        for batch in batches(rels, batch_size):
            local_paths = [ctx.root / rel for rel in batch]
            result = ctx.drive.upload(local_paths, remote_parent, file_strategy=strategy)
            total.skipped_items += result.skipped_items
            total.failed_items += result.failed_items
            total.failures += result.failures
            done += len(batch)
            reporter.progress(done, len(to_push))
            failed_names = {f["name"] for f in result.failures}
            for rel in batch:
                if Path(rel).name not in failed_names:
                    reporter.item("^", rel)

            # D2.1: a skip is reported only as an aggregate count, so we cannot tell
            # WHICH files in the batch were skipped. Rather than falsely record an
            # unconfirmed hash, index none of this batch's non-failed files and leave
            # them for the next push.
            if result.skipped_items > 0:
                continue
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
    reporter.progress(len(to_push), len(to_push), force=True)
    reporter.done("uploaded", transferred=total.transferred_items, failed=total.failed_items)
    return total
