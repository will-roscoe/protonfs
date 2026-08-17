# src/protonfs/commands/pull.py
"""``protonfs pull``: download remote-only/changed files from Drive into the working tree.

.. versionadded:: 1.0.0
"""
from __future__ import annotations

import datetime
import tempfile
from pathlib import Path, PurePosixPath

from protonfs.batching import batches, group_by_parent
from protonfs.context import RepoContext
from protonfs.diff import SyncState, classify, within_subpath
from protonfs.drive import RemoteEntry, TransferResult
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry
from protonfs.localscan import hash_file_digests, scan

# --resolve policy for a file that diverged on BOTH sides (local edited AND remote changed
# since the last sync). protonfs-semantic, decided at this layer (not proton-drive transfer
# strategies) so the outcome is testable and identical regardless of the backend:
#   remote -> overwrite the local copy with the remote one
#   local  -> keep the local copy untouched (it stays queued for the next push)
#   both   -> fetch the remote copy alongside the local one under a suffix, for a manual merge
RESOLVE_CHOICES = ("remote", "local", "both")

# Suffix for the remote copy fetched next to a locally-modified file under --resolve=both.
REMOTE_COPY_SUFFIX = ".remote"

# Failure kind for a diverged file pull would not touch because no --resolve policy was given
# (or because the remote no longer has a copy to reconcile against). The CLI surfaces these
# and exits non-zero -- pull never silently discards a local edit.
CONFLICT_KIND = "conflict"
CONFLICT_ERROR = "local and remote diverged; re-run with --resolve=remote|local|both"

# #132: proton-drive can report a file as transferred that never lands locally (the
# download analogue of #22 on push). Files that fail this post-download verification are
# tagged with this so the CLI can tell them apart from genuine conflicts.
UNDERDELIVERED_KIND = "under-delivered"
UNDERDELIVERED_ERROR = "claimed transferred but not found locally after download (under-delivered)"


def _names_a_file(ctx: RepoContext, subpath: str) -> bool:
    """Whether ``subpath`` names a tracked file rather than a directory."""
    return ctx.index.get(subpath) is not None or (ctx.root / subpath).is_file()


def _remote_view(ctx: RepoContext, subpath: str | None) -> dict[str, RemoteEntry]:
    """Walk the remote (scoped to `subpath`) into a rel_path -> RemoteEntry map, so classify
    can attribute modification DIRECTION (remote-modified vs both-modified) to diverged files.
    Only paid for when a --resolve policy is in play."""
    # A FILE pathspec must not be walked: `filesystem list` against a file does not
    # return the JSON array the parser expects, and the bare `[` surfaces as a generic
    # unparseable-output error naming neither the file nor the cause (#142). Walk its
    # parent directory instead and keep only that one entry.
    prefix = subpath
    wanted: str | None = None
    if subpath and _names_a_file(ctx, subpath):
        parent = str(PurePosixPath(subpath).parent)
        prefix = "" if parent == "." else parent
        wanted = subpath

    remote_root = ctx.config.remote_root
    if prefix:
        remote_root = f"{remote_root}/{prefix}"
    entries = ctx.drive.walk(remote_root)
    remote = {e.rel_path: e for e in entries if not e.is_dir}
    # walk rel_paths are relative to remote_root; re-prefix so keys match the index's
    # repo-root-relative rel_paths (mirrors refresh.py).
    if prefix:
        remote = {f"{prefix}/{rel}": e for rel, e in remote.items()}
    if wanted is not None:
        remote = {k: v for k, v in remote.items() if k == wanted}
    return remote


def _download_and_index(
    ctx: RepoContext, rels: list[str], now: str, reporter, progress_total: int = 0
) -> TransferResult:
    """Download `rels` (grouped by parent) with overwrite, updating the index to present.

    Shared by the clean bring-down (remote-only / metadata-only), the safe remote-moved
    bring-down (local unchanged), and --resolve=remote (remote wins over a local edit).
    Reports ``reporter.progress(done, progress_total)`` and ``reporter.item("v", rel)``
    per downloaded batch.
    """
    total = TransferResult(0, 0, 0, [])
    done = 0
    # #139: size each `filesystem download` call from config, exactly as push does. A
    # batch that cannot finish inside PROTONFS_TRANSFER_TIMEOUT is retried WHOLE, so on a
    # slow link (or with large files) the only way to make progress is a smaller batch --
    # which is what defaults.batch_size is documented to be for.
    batch_size = ctx.config.defaults.batch_size
    for parent, group in group_by_parent(rels).items():
        local_folder = ctx.root if parent == "." else ctx.root / parent
        local_folder.mkdir(parents=True, exist_ok=True)
        for batch in batches(group, batch_size):
            remote_paths = []
            for rel in batch:
                entry = ctx.index.get(rel)
                default_remote = f"{ctx.config.remote_root}/{rel}"
                remote_paths.append(entry.remote_path if entry else default_remote)
            # "replace": pull intentionally brings remote content down, overwriting any local
            # copy at the destination (that is what remote-wins / remote-moved mean).
            result = ctx.drive.download(remote_paths, local_folder, file_strategy="replace")
            total.skipped_items += result.skipped_items
            total.failed_items += result.failed_items
            total.failures += result.failures
            done += len(batch)
            reporter.progress(done, progress_total or len(rels))
            failed_names = {f["name"] for f in result.failures}
            for rel in batch:
                if Path(rel).name not in failed_names:
                    reporter.item("v", rel)

            # #132: do NOT trust result.transferred_items -- verify each non-failed
            # candidate actually landed locally before counting it transferred and
            # indexing it. A file proton-drive did not report as a failure but that is
            # absent afterward is a silent under-delivery: report it as failed and leave
            # it unindexed so the next pull retries it, mirroring #22 on push.
            for rel in batch:
                name = Path(rel).name
                if name in failed_names:
                    continue
                downloaded_path = ctx.root / rel
                if not downloaded_path.exists():
                    total.failed_items += 1
                    total.failures.append(
                        {"name": name, "error": UNDERDELIVERED_ERROR, "kind": UNDERDELIVERED_KIND}
                    )
                    continue
                stat = downloaded_path.stat()
                prior = ctx.index.get(rel)
                default_remote = f"{ctx.config.remote_root}/{rel}"
                sha256, sha1 = hash_file_digests(downloaded_path)
                ctx.index.set(
                    rel,
                    IndexEntry(
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        sha256=sha256,
                        sha1=sha1,
                        remote_path=prior.remote_path if prior else default_remote,
                        origin_device=prior.origin_device if prior else "unknown",
                        local_state="present",
                        last_synced=now,
                    ),
                )
                total.transferred_items += 1
        # #3: persist after each parent group so an interrupted pull resumes from here
        # rather than restarting. Crash-safe once composed with #1's atomic writes.
        ctx.index.save()
    return total


def _fetch_remote_copies(ctx: RepoContext, rels: list[str]) -> int:
    """--resolve=both: fetch each diverged file's remote copy next to the local one under a
    suffix (e.g. dump.remote), leaving the local file untouched for a manual merge. The
    suffixed copy is deliberately NOT indexed -- it is scratch for the user to reconcile."""
    fetched = 0
    for rel in rels:
        entry = ctx.index.get(rel)
        remote_path = entry.remote_path if entry else f"{ctx.config.remote_root}/{rel}"
        dest = ctx.root / (rel + REMOTE_COPY_SUFFIX)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            ctx.drive.download([remote_path], Path(tmp), file_strategy="replace")
            landed = Path(tmp) / Path(rel).name
            if landed.exists():
                landed.replace(dest)
                fetched += 1
    return fetched


def pull(
    ctx: RepoContext,
    subpath: str | None,
    resolve: str | None,
    dry_run: bool,
    refresh: bool = False,
    reporter=None,
) -> TransferResult:
    """Download remote-only (and, with ``resolve``, diverged) files into the tree.

    Without ``resolve``, pull is conservative: it only brings down files absent
    locally and never overwrites a local file, so it cannot clobber a local edit. A
    live remote walk (needed to attribute a direction to diverged files) is paid for
    only when a ``resolve`` policy is supplied.

    .. versionchanged:: 1.10.2
       Each download is now verified against the local filesystem afterward instead of
       trusting proton-drive's reported transfer count (#132, the download analogue of
       #22 on push). A file proton-drive did not report as a failure but that is absent
       locally afterward is now counted in ``failed_items`` and left unindexed for the
       next pull to retry, instead of silently vanishing from both tallies.

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree to pull, or ``None`` for everything.
    :param resolve: divergence policy ``remote`` | ``local`` | ``both``, or ``None``
        to skip diverged files entirely.
    :param dry_run: when true, report what would transfer without downloading or
        persisting anything.
    :param refresh: when true, seed the index from a remote walk first (reusing the
        local scan already computed here) so a fresh repo has entries to pull.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    :returns: a :class:`~protonfs.drive.TransferResult` of what was fetched/skipped.
    :raises protonfs.drive.DriveError: on a Drive or lock failure.

    .. seealso:: :func:`protonfs.commands.push.push` for the upload direction.
    """
    from protonfs.reporting import get_reporter

    # #124: `replace` is an accepted alias for `remote` (replace the local copy), so the
    # remote|local|both vocabulary lines up with push's `--resolve`.
    if resolve == "replace":
        resolve = "remote"
    reporter = reporter or get_reporter()
    reporter.phase("scanning local", subpath=subpath or ".")
    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    from protonfs.hashcache import HashCache

    local = scan(
        ctx.root, scan_root, ignore, ctx.index,
        low_io=ctx.config.defaults.low_io, reporter=reporter,
        hash_cache=HashCache(ctx.root),
    )
    if refresh:
        from protonfs.commands.refresh import refresh as refresh_index

        # Reuse the scan we just did; on a dry run seed only in memory so the preview
        # is accurate without persisting metadata-only entries to index.json.
        refresh_index(ctx, subpath, prune=False, persist=not dry_run, local=local)

    # A live remote view (a walk) is only needed to attribute direction to diverged files,
    # so it is paid for only when a --resolve policy is given. Without --resolve, pull stays
    # lean and brings down only files absent locally -- it never overwrites a local file, so
    # it cannot clobber a local edit.
    remote = _remote_view(ctx, subpath) if resolve is not None else None
    # #96: classify() reasons over the whole repo-wide index, but the scan (and walk)
    # above are scoped to `subpath` -- without this filter every metadata-only index
    # entry elsewhere in the repo classifies as pullable and `pull SUBPATH` downloads
    # unrelated directories (and rate-limits itself doing so).
    diff_entries = [
        e for e in classify(local, ctx.index, remote) if within_subpath(e.rel_path, subpath)
    ]

    # Safe to bring down as-is: absent locally (remote-only / metadata-only), plus -- when we
    # have a remote view -- files the remote changed while the local copy stayed in sync.
    to_pull = [
        e.rel_path
        for e in diff_entries
        if e.state in (SyncState.REMOTE_ONLY, SyncState.METADATA_ONLY)
    ]
    if resolve is not None:
        to_pull += [e.rel_path for e in diff_entries if e.state == SyncState.REMOTE_MODIFIED]

    # Diverged on both sides (a remote copy exists to reconcile against): resolvable.
    both_modified = [e.rel_path for e in diff_entries if e.state == SyncState.BOTH_MODIFIED]
    # A local change we cannot attribute a direction to (no remote view, or the remote no
    # longer lists it): never auto-resolved -- always reported so the user decides.
    conflicts = [e.rel_path for e in diff_entries if e.state == SyncState.CONFLICT]

    resolve_remote = both_modified if resolve == "remote" else []
    resolve_both = both_modified if resolve == "both" else []
    resolve_local = both_modified if resolve == "local" else []
    to_pull += resolve_remote  # remote wins -> overwrite the local copy on download

    # Unresolved divergence: the always-conflict set, plus both-modified with no policy given.
    unresolved = list(conflicts)
    if resolve is None:
        unresolved += both_modified

    if dry_run:
        previewed = len(to_pull) + len(resolve_both)
        failures = [
            {"name": Path(r).name, "error": CONFLICT_ERROR, "kind": CONFLICT_KIND}
            for r in unresolved
        ]
        return TransferResult(previewed, len(resolve_local), len(unresolved), failures)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if to_pull:
        reporter.phase("downloading", files=len(to_pull))
        total = _download_and_index(ctx, to_pull, now, reporter, progress_total=len(to_pull))
        reporter.progress(len(to_pull), len(to_pull), force=True)
    else:
        total = TransferResult(0, 0, 0, [])
    total.transferred_items += _fetch_remote_copies(ctx, resolve_both)
    total.skipped_items += len(resolve_local)  # local kept; stays queued for the next push
    for rel in unresolved:
        total.failed_items += 1
        total.failures.append(
            {"name": Path(rel).name, "error": CONFLICT_ERROR, "kind": CONFLICT_KIND}
        )
    ctx.index.save()
    reporter.done("downloaded", transferred=total.transferred_items, failed=total.failed_items)
    return total
