# src/protonfs/commands/pull.py
"""``protonfs pull``: download remote-only/changed files from Drive into the working tree.

.. versionadded:: 1.0.0
"""
from __future__ import annotations

import datetime
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from protonfs.batching import batches, group_by_parent
from protonfs.config import CONFIG_DIR_NAME
from protonfs.context import RepoContext
from protonfs.diff import SyncState, classify, held_and_gone
from protonfs.drive import RemoteEntry, TransferResult, downloaded_name
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
UNMATCHED_ERROR = (
    "downloaded copy does not match Drive's listed size or sha1; the local file was left "
    "as it was (under-delivered)"
)


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


def _staging_dir(ctx: RepoContext) -> Path:
    """Directory downloads land in before being moved into the tree.

    Under ``.protonfs/`` so a leftover partial from a killed process is never scanned as
    a tracked file, and under the repo root so moving a completed file into place is a
    rename rather than a copy. Stale directories from a previous kill are swept on the
    way in, since a process that died mid-download had no chance to clean up after
    itself. Called once per pull rather than once per batch: the repo lock keeps
    concurrent pulls out, and sweeping repeatedly would be wasted work.
    """
    staging = ctx.root / CONFIG_DIR_NAME / "download"
    staging.mkdir(parents=True, exist_ok=True)
    for stale in staging.iterdir():
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    return staging


def _remote_path(ctx: RepoContext, rel: str) -> str:
    """The Drive path ``rel`` is downloaded from: its indexed one, else the default."""
    entry = ctx.index.get(rel)
    return entry.remote_path if entry else f"{ctx.config.remote_root}/{rel}"


def _landed_name(ctx: RepoContext, rel: str) -> str:
    """The name ``rel``'s download lands under (proton-drive renames ``q?`` to ``q_``)."""
    return downloaded_name(PurePosixPath(_remote_path(ctx, rel)).name)


def _apart_by_landed_name(batch: list[str], landed: dict[str, str]) -> list[list[str]]:
    """Split ``batch`` so no two of its files land under one name in a single call.

    :param batch: repo-relative paths of one folder's files.
    :param landed: each path's landed name, from :func:`_landed_name`.
    :returns: ``[batch]`` when every landed name is distinct (the usual case); otherwise
        the fewest calls, in order, each holding at most one file per landed name.
    """
    calls: list[list[str]] = []
    names: list[set[str]] = []
    for rel in batch:
        for call, taken in zip(calls, names):
            if landed[rel] not in taken:
                call.append(rel)
                taken.add(landed[rel])
                break
        else:
            calls.append([rel])
            names.append({landed[rel]})
    return calls


def _incoming_size(ctx: RepoContext, rel: str, remote: dict[str, RemoteEntry] | None) -> int:
    """Bytes ``rel``'s download moves: Drive's size when the walk listed it, else the
    indexed size (which, for a file being replaced, is the old copy's)."""
    listed = remote.get(rel) if remote is not None else None
    if listed is not None:
        return listed.claimed_size if listed.claimed_size is not None else listed.size
    entry = ctx.index.get(rel)
    return entry.size if entry else 0


def _staged_digests(staged: Path, listed: RemoteEntry | None) -> tuple[str, str] | None:
    """Return the ``(sha256, sha1)`` of a staged download that matches Drive's listing.

    :param staged: the file the download left in its staging directory; it must exist.
    :param listed: the file's entry from the walk, or ``None`` when pull took none. Its
        ``claimed_size`` and ``sha1`` are checked when Drive reports them.
    :returns: the digests, or ``None`` when the staged file's size or sha1 differs from
        the listing (a short or wrong copy).
    """
    if (
        listed is not None
        and listed.claimed_size is not None
        and staged.stat().st_size != listed.claimed_size
    ):
        return None
    sha256, sha1 = hash_file_digests(staged)
    if listed is not None and listed.sha1 and listed.sha1 != sha1:
        return None
    return sha256, sha1


def _download_and_index(
    ctx: RepoContext,
    rels: list[str],
    now: str,
    reporter,
    progress_total: int = 0,
    remote: dict[str, RemoteEntry] | None = None,
) -> TransferResult:
    """Download `rels` (grouped by parent) with overwrite, updating the index to present.

    Shared by the clean bring-down (remote-only / metadata-only), the safe remote-moved
    bring-down (local unchanged), and --resolve=remote (remote wins over a local edit).
    Each file downloads into a fresh staging directory and replaces the local path only
    when it landed there, was not reported failed, and matches its ``remote`` listing
    (see :func:`_staged_digests`); otherwise the local path is left as it was and the
    file is counted failed (``under-delivered`` when it did not land or did not match).
    ``remote`` (the walk, under ``--resolve``) also sizes each call's timeout from
    Drive's copy rather than the indexed one. Reports ``reporter.progress(done,
    progress_total)`` per batch and ``reporter.item("v", rel)`` per delivered file.
    """
    total = TransferResult(0, 0, 0, [])
    done = 0
    # #139: size each `filesystem download` call from config, exactly as push does. A
    # batch that cannot finish inside PROTONFS_TRANSFER_TIMEOUT is retried WHOLE, so on a
    # slow link (or with large files) the only way to make progress is a smaller batch --
    # which is what defaults.batch_size is documented to be for.
    batch_size = ctx.config.defaults.batch_size
    staging = _staging_dir(ctx)
    for parent, group in group_by_parent(rels).items():
        local_folder = ctx.root if parent == "." else ctx.root / parent
        local_folder.mkdir(parents=True, exist_ok=True)
        landed = {rel: _landed_name(ctx, rel) for rel in group}
        # Two files proton-drive would land under one name ("a?" and "a*" both become
        # "a_") would overwrite each other in staging, so they go in separate calls.
        calls = [
            call
            for batch in batches(group, batch_size)
            for call in _apart_by_landed_name(batch, landed)
        ]
        for batch in calls:
            remote_paths = [_remote_path(ctx, rel) for rel in batch]
            # #184: sizes the download's timeout
            payload = sum(_incoming_size(ctx, rel, remote) for rel in batch)
            # Download into a staging directory and move each file into the tree only
            # once the transfer returns. Downloading straight to the destination left a
            # TRUNCATED file there when a pull was interrupted, and the next pull could
            # not tell it from a local edit: it reported "local and remote diverged" and
            # offered --resolve=local, which would upload the partial over the good
            # remote copy (#141). Staging lives under .protonfs/, which the scan skips,
            # and on the same filesystem, so the move is a rename.
            tmp = Path(tempfile.mkdtemp(dir=staging))
            delivered, undelivered = [], []
            try:
                # "replace": pull intentionally brings remote content down, overwriting any
                # local copy at the destination (that is what remote-wins / remote-moved mean).
                result = ctx.drive.download(
                    remote_paths, tmp, file_strategy="replace", payload_bytes=payload or None
                )
                failed_names = {f["name"] for f in result.failures}
                # #132: do NOT trust result.transferred_items. Only what landed in this
                # call's own staging directory proves a download: the destination may still
                # hold the old copy. A file not reported failed that is missing there, or
                # does not match the walk's listing, is under-delivered; it is left
                # unindexed and the local path untouched, so the next pull retries it.
                for rel in batch:
                    if landed[rel] in failed_names:
                        continue
                    staged = tmp / landed[rel]
                    listed = remote.get(rel) if remote is not None else None
                    digests = _staged_digests(staged, listed) if staged.is_file() else None
                    if digests is None:
                        undelivered.append({
                            "name": Path(rel).name,
                            "error": UNMATCHED_ERROR if staged.is_file() else UNDERDELIVERED_ERROR,
                            "kind": UNDERDELIVERED_KIND,
                            "path": rel,
                        })
                        continue
                    downloaded_path = ctx.root / rel
                    staged.replace(downloaded_path)
                    stat = downloaded_path.stat()
                    prior = ctx.index.get(rel)
                    default_remote = f"{ctx.config.remote_root}/{rel}"
                    ctx.index.set(
                        rel,
                        IndexEntry(
                            size=stat.st_size,
                            mtime=stat.st_mtime,
                            sha256=digests[0],
                            sha1=digests[1],
                            remote_path=prior.remote_path if prior else default_remote,
                            origin_device=prior.origin_device if prior else "unknown",
                            local_state="present",
                            last_synced=now,
                        ),
                    )
                    total.transferred_items += 1
                    delivered.append(rel)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            # proton-drive names a failure by its landed name only; a call is one folder
            # with distinct landed names, so the name settles the path, which callers
            # matching failures need. Report the file's own name, not the landed one.
            by_name = {landed[rel]: rel for rel in batch}
            failures = []
            for f in result.failures:
                rel = by_name.get(f["name"])
                name = Path(rel).name if rel else f["name"]
                failures.append({**f, "name": name, "path": rel})
            total.skipped_items += result.skipped_items
            total.failed_items += result.failed_items + len(undelivered)
            total.failures += failures + undelivered
            done += len(batch)
            reporter.progress(done, progress_total or len(rels))
            for rel in delivered:
                reporter.item("v", rel)
        # #3: persist after each parent group so an interrupted pull resumes from here
        # rather than restarting. Crash-safe once composed with #1's atomic writes.
        # #170: a failed one is not fatal; pull's final save persists everything.
        ctx.index.checkpoint()
    return total


def _fetch_remote_copies(ctx: RepoContext, rels: list[str]) -> int:
    """--resolve=both: fetch each diverged file's remote copy next to the local one under a
    suffix (e.g. dump.remote), leaving the local file untouched for a manual merge. The
    suffixed copy is deliberately NOT indexed -- it is scratch for the user to reconcile."""
    fetched = 0
    for rel in rels:
        remote_path = _remote_path(ctx, rel)
        dest = ctx.root / (rel + REMOTE_COPY_SUFFIX)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            ctx.drive.download([remote_path], Path(tmp), file_strategy="replace")
            landed = Path(tmp) / _landed_name(ctx, rel)
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
    no_restore: bool = False,
    only: set[str] | None = None,
) -> TransferResult:
    """Download remote-only (and, with ``resolve``, remote-changed or diverged) files.

    Without ``resolve``, pull is conservative: it only brings down files absent
    locally and never overwrites a local file, so it cannot clobber a local edit. A
    live remote walk (needed to attribute a direction to diverged files) is paid for
    only when a ``resolve`` policy is supplied. With one, a file Drive changed while
    the local copy still matches the index (``remote-modified``) is downloaded over the
    local copy under every policy, since no local edit is at risk; ``resolve`` itself
    decides only files changed on both sides.

    A file this machine held that is now gone locally is restored, unless ``no_restore``
    is set: then it is left alone (it was deleted, or moved away, on purpose) and counted
    in ``local_deleted_items``. See :func:`~protonfs.diff.held_and_gone`.

    .. versionchanged:: 1.10.2
       Each download is now verified against the local filesystem afterward instead of
       trusting proton-drive's reported transfer count (#132, the download analogue of
       #22 on push). A file proton-drive did not report as a failure but that is absent
       locally afterward is now counted in ``failed_items`` and left unindexed for the
       next pull to retry, instead of silently vanishing from both tallies.

    .. versionchanged:: 3.0.0
       Added ``no_restore``, so a scheduled sync can leave local deletions and moves in
       place for push to act on (#197). A path that still exists on disk but was hidden
       from the scan (now ignored, say) is never downloaded over.

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
    :param no_restore: when true, do not download files this machine held that are now
        gone locally; count them in ``local_deleted_items`` instead.
    :param only: pull just these repo-relative indexed files, in one pass, instead of
        ``subpath`` (pass ``None``): each folder's files download together and the index
        is saved once per folder, not once per file. Not combinable with ``refresh`` or
        ``resolve``, whose Drive walks are scoped to ``subpath``.
    :returns: a :class:`~protonfs.drive.TransferResult` of what was fetched/skipped;
        ``local_deleted_items`` counts the files ``no_restore`` left alone, and
        ``remote_modified_items`` the files ``refresh`` found changed on Drive that a pull
        without ``resolve`` left alone.
    :raises protonfs.drive.DriveError: on a Drive or lock failure.
    :raises ValueError: when ``only`` is combined with ``subpath``, ``refresh`` or
        ``resolve``.

    .. versionchanged:: 3.1.0
       Added ``only``, so the CLI pulls N named or matched files in one pass rather
       than N. Never downloads a file the store's ``.protonfs/ignore``/``include`` rules
       exclude (which covers ``.git`` and files ``protonfs export`` moved into git), and
       counts them in ``excluded_items``.

    .. versionchanged:: 3.3.2
       With ``resolve``, downloads ``remote-modified`` files again, as documented; 3.0.0
       to 3.3.1 dropped them because they exist on disk. Also downloads
       ``remote-changed`` files (metadata-only, with a newer copy on Drive), which no
       earlier release did under ``resolve``. With ``refresh`` and no ``resolve``, counts
       the ``remote-modified`` files it left alone in ``remote_modified_items``.

    .. versionchanged:: 3.3.3
       With ``resolve``, restores a file this machine held that is gone locally
       (``local-deleted``), as without it; earlier releases left it. A download counts
       in ``transferred_items`` only when the file landed in staging and, with
       ``resolve``, matches the walk's plaintext size and sha1; otherwise the local file
       is left as it was and the file counts in ``failed_items`` (``under-delivered``).
       Earlier releases counted a dropped download of a file already on disk as
       transferred, and moved a reported-failed download's partial file into place. With
       ``resolve``, a download's timeout is sized from Drive's copy, not the indexed one.

    .. seealso:: :func:`protonfs.commands.push.push` for the upload direction.
    """
    from protonfs.reporting import get_reporter

    # #124: `replace` is an accepted alias for `remote` (replace the local copy), so the
    # remote|local|both vocabulary lines up with push's `--resolve`.
    if resolve == "replace":
        resolve = "remote"
    if only is not None and (subpath is not None or refresh or resolve is not None):
        raise ValueError("pull: `only` cannot be combined with subpath, refresh or resolve")
    reporter = reporter or get_reporter()
    if only is None:
        reporter.phase("scanning local", subpath=subpath or ".")
    else:
        reporter.phase("scanning local", files=len(only))
    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")

    local = scan(
        ctx.root, scan_root, ignore, ctx.index,
        low_io=ctx.config.defaults.low_io, reporter=reporter,
        hash_cache=ctx.hash_cache, only=only,
    )
    refreshed = None
    if refresh:
        from protonfs.commands.refresh import refresh as refresh_index

        # Reuse the scan we just did; on a dry run seed only in memory so the preview
        # is accurate without persisting metadata-only entries to index.json.
        refreshed = refresh_index(ctx, subpath, prune=False, persist=not dry_run, local=local)

    # A live remote view (a walk) is only needed to attribute direction to diverged files,
    # so it is paid for only when a --resolve policy is given. Without --resolve, pull stays
    # lean and brings down only files absent locally -- it never overwrites a local file, so
    # it cannot clobber a local edit.
    remote = _remote_view(ctx, subpath) if resolve is not None else None
    # #96: the scan (and walk) above are scoped to `subpath`, so classify must be too --
    # otherwise every metadata-only index entry elsewhere in the repo classifies as
    # pullable and `pull SUBPATH` downloads unrelated directories (and rate-limits
    # itself doing so).
    diff_entries = classify(local, ctx.index, remote, subpath=subpath)
    if only is not None:
        diff_entries = [e for e in diff_entries if e.rel_path in only]

    # Safe to bring down as-is: absent locally (remote-only / metadata-only, and -- with a
    # remote view -- a metadata-only file whose Drive copy changed since it was recorded,
    # or a held file gone locally, which the walk calls local-deleted, not remote-only).
    absent = (
        SyncState.REMOTE_ONLY, SyncState.METADATA_ONLY, SyncState.REMOTE_CHANGED,
        SyncState.LOCAL_DELETED,
    )
    to_pull = [e.rel_path for e in diff_entries if e.state in absent]
    # A path still on disk was only hidden from the scan (now ignored, say): it is not
    # absent, and downloading over it would overwrite it.
    to_pull = [rel for rel in to_pull if not (ctx.root / rel).exists()]
    # With a remote view, also a file Drive changed while the local copy stayed in sync:
    # replacing it loses no local edit. It is on disk by definition (the scan found it), so
    # it must not go through the filter above.
    remote_modified: list[str] = []
    if resolve is not None:
        to_pull += [e.rel_path for e in diff_entries if e.state == SyncState.REMOTE_MODIFIED]
    elif refreshed is not None:
        # Without --resolve pull never overwrites a local file, so it leaves alone the ones
        # the refresh just found (its changed paths that are on disk); count them.
        remote_modified = [rel for rel in refreshed.changed_paths if rel in local]
    # What the store's rules exclude is never downloaded: an include allowlist means
    # "only these here", a .git path must not land in a live repository, and a file
    # `export` moved into git (listed in .protonfs/ignore) must not come back. Their
    # index entries stay, so bringing a path back into scope resumes where it left off.
    excluded = [rel for rel in to_pull if ignore.matches(rel)]
    to_pull = [rel for rel in to_pull if not ignore.matches(rel)]
    # #197: a file this machine held that is gone locally is restored by default (the
    # git-LFS reading: its copy is on Drive). With no_restore it was deleted or moved away
    # on purpose; leave it for push to act on and count it.
    local_deleted: list[str] = []
    if no_restore:
        gone = held_and_gone(diff_entries, ctx.index, ctx.root)
        local_deleted = sorted(gone)
        to_pull = [rel for rel in to_pull if rel not in gone]

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
        return TransferResult(
            previewed, len(resolve_local), len(unresolved), failures,
            local_deleted_items=len(local_deleted), excluded_items=len(excluded),
            remote_modified_items=len(remote_modified),
        )

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if to_pull:
        reporter.phase("downloading", files=len(to_pull))
        total = _download_and_index(
            ctx, to_pull, now, reporter, progress_total=len(to_pull), remote=remote
        )
        reporter.progress(len(to_pull), len(to_pull), force=True)
    else:
        total = TransferResult(0, 0, 0, [])
    total.local_deleted_items = len(local_deleted)
    total.excluded_items = len(excluded)
    total.remote_modified_items = len(remote_modified)
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
