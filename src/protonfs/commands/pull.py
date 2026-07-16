# src/protonfs/commands/pull.py
"""``protonfs pull``: download remote-only/changed files from Drive into the working tree."""
from __future__ import annotations

import datetime
import tempfile
from pathlib import Path

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


def _remote_view(ctx: RepoContext, subpath: str | None) -> dict[str, RemoteEntry]:
    """Walk the remote (scoped to `subpath`) into a rel_path -> RemoteEntry map, so classify
    can attribute modification DIRECTION (remote-modified vs both-modified) to diverged files.
    Only paid for when a --resolve policy is in play."""
    remote_root = ctx.config.remote_root
    if subpath:
        remote_root = f"{remote_root}/{subpath}"
    entries = ctx.drive.walk(remote_root)
    remote = {e.rel_path: e for e in entries if not e.is_dir}
    # walk rel_paths are relative to remote_root; re-prefix so keys match the index's
    # repo-root-relative rel_paths (mirrors refresh.py).
    if subpath:
        remote = {f"{subpath}/{rel}": e for rel, e in remote.items()}
    return remote


def _download_and_index(ctx: RepoContext, rels: list[str], now: str) -> TransferResult:
    """Download `rels` (grouped by parent) with overwrite, updating the index to present.

    Shared by the clean bring-down (remote-only / metadata-only), the safe remote-moved
    bring-down (local unchanged), and --resolve=remote (remote wins over a local edit).
    """
    total = TransferResult(0, 0, 0, [])
    for parent, group in group_by_parent(rels).items():
        local_folder = ctx.root if parent == "." else ctx.root / parent
        local_folder.mkdir(parents=True, exist_ok=True)
        for batch in batches(group):
            remote_paths = []
            for rel in batch:
                entry = ctx.index.get(rel)
                default_remote = f"{ctx.config.remote_root}/{rel}"
                remote_paths.append(entry.remote_path if entry else default_remote)
            # "replace": pull intentionally brings remote content down, overwriting any local
            # copy at the destination (that is what remote-wins / remote-moved mean).
            result = ctx.drive.download(remote_paths, local_folder, file_strategy="replace")
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
) -> TransferResult:
    """Download remote-only (and, with ``resolve``, diverged) files into the tree.

    Without ``resolve``, pull is conservative: it only brings down files absent
    locally and never overwrites a local file, so it cannot clobber a local edit. A
    live remote walk (needed to attribute a direction to diverged files) is paid for
    only when a ``resolve`` policy is supplied.

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree to pull, or ``None`` for everything.
    :param resolve: divergence policy ``remote`` | ``local`` | ``both``, or ``None``
        to skip diverged files entirely.
    :param dry_run: when true, report what would transfer without downloading or
        persisting anything.
    :param refresh: when true, seed the index from a remote walk first (reusing the
        local scan already computed here) so a fresh repo has entries to pull.
    :returns: a :class:`~protonfs.drive.TransferResult` of what was fetched/skipped.
    :raises protonfs.drive.DriveError: on a Drive or lock failure.

    .. seealso:: :func:`protonfs.commands.push.push` for the upload direction.
    """
    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)
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
    total = _download_and_index(ctx, to_pull, now) if to_pull else TransferResult(0, 0, 0, [])
    total.transferred_items += _fetch_remote_copies(ctx, resolve_both)
    total.skipped_items += len(resolve_local)  # local kept; stays queued for the next push
    for rel in unresolved:
        total.failed_items += 1
        total.failures.append(
            {"name": Path(rel).name, "error": CONFLICT_ERROR, "kind": CONFLICT_KIND}
        )
    ctx.index.save()
    return total
