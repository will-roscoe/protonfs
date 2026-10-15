"""``protonfs push``: upload local-only/changed files to Drive, creating remote dirs.

.. versionadded:: 1.0.0
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from protonfs import manifest
from protonfs.batching import batches, group_by_parent
from protonfs.context import RepoContext
from protonfs.diff import SyncState, classify
from protonfs.drive import DriveError, TransferResult, is_thumbnail_failure
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry
from protonfs.lfs import is_pointer_stub
from protonfs.localscan import scan

logger = logging.getLogger(__name__)

# #124: `--resolve` uses a shared remote|local|both vocabulary across push and pull. On
# push those map to proton-drive's `-f` upload strategies; the proton-drive strategy names
# (merge|keep-both|replace|skip) are also accepted and pass straight through.
#   local  -> replace   (overwrite the remote with our local copy)
#   remote -> skip      (keep the remote copy; skip the conflicting upload)
#   both   -> keep-both (upload our local copy alongside the remote)
_RESOLVE_TO_STRATEGY = {"local": "replace", "remote": "skip", "both": "keep-both"}

# #144: proton-drive's upload conflict strategies, as implemented by cli-drive@0.5.0:
#   merge     -> uploads a NEW REVISION of the existing file node (its version history
#                keeps every earlier revision)
#   replace   -> TRASHES the existing node and uploads a fresh one in its place
#   keep-both -> uploads under a free alternative name
#   skip      -> leaves the existing node alone
# With --json (which protonfs always passes) and no strategy, an existing name fails the
# upload as a name conflict. So re-pushing a file that changed locally needs `merge`, and
# push picks it automatically when the change is provably this device's alone.
REVISION_STRATEGY = "merge"

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

# Self-heal: proton-drive refuses to overwrite an existing remote file, so a file already
# on Drive but missing from the local index (an earlier push uploaded it but never saved
# the index -- e.g. it was interrupted before the group's verify) fails upload with a
# name-conflict. Rather than fail that file forever, push VERIFIES the remote copy matches
# the local one and, if so, records it in the index without re-uploading ("adopts" it). A
# remote copy that does NOT match is a genuine conflict, reported with CONFLICT_KIND.
CONFLICT_KIND = "conflict"
CONFLICT_ERROR = "a different file already exists on the remote (name conflict; not adopted)"

# #138: proton-drive can reject an upload as a name conflict for a file that is NOT in the
# remote listing at all -- a "phantom" node (most often a partial/draft upload) that holds
# the name while being invisible and undownloadable. That is NOT the CONFLICT_KIND case: no
# different file occupies the name, so telling the user to keep the remote copy
# (--resolve=remote) would preserve the phantom and lose the local data. The remedy is to
# overwrite it (--resolve=local, i.e. proton-drive's `-f replace`), which does not ask the
# existing node's permission. Observed on a file that no listing and no trash lookup could
# find, yet no upload could replace (sph-dev#47).
PHANTOM_KIND = "phantom"
PHANTOM_ERROR = (
    "upload rejected as a name conflict, but no file of that name is listable on the "
    "remote -- usually a partial/draft upload holding the name. Re-run with "
    "--resolve=local to overwrite it"
)


# #144: a remote identity with no plaintext `claimedSize` cannot confirm anything beyond
# "a file of this name exists". offload already refuses to delete on such an identity;
# push must not record one as delivered either, or it writes the very index entry offload
# would then decline to act on. The upload still happens -- only the claim of delivery is
# withheld -- and the file is reported with this kind, left unindexed (or at its previous
# entry) so status never counts it and the next push retries it.
UNVERIFIED_KIND = "unverified"
UNVERIFIED_ERROR = (
    "on the remote, but its listing reports no plaintext size, so delivery could not be "
    "verified (unverified; NOT indexed)"
)


class _Verdict(Enum):
    """Outcome of checking one push candidate against its remote identity."""

    VERIFIED = "verified"
    REFUTED = "refuted"  # absent, or positively different from the local file
    UNVERIFIABLE = "unverifiable"  # present, but the listing carries no plaintext size


def _is_already_exists(failure: dict) -> bool:
    """True if an upload failure is proton-drive's "file already exists" name-conflict.

    Matched on the message text (proton-drive has no machine-readable error code here):
    e.g. ``ValidationError: Name conflict on "dump_0001" (file) already exists``.
    """
    message = str(failure.get("error", "")).lower()
    return "already exist" in message or "name conflict" in message


def _verify_remote(ident, entry, *, strict_sha1: bool) -> _Verdict:
    """Whether the remote identity ``ident`` confirms the local ``entry`` is on Drive.

    The plaintext size must match (#22). When ``strict_sha1`` (adopting a pre-existing
    remote file rather than one we just uploaded) and both sides expose a sha1, the
    digests must match too -- size alone can collide, and adoption must never record
    diverged content as synced.

    An identity with no ``claimed_size`` is :attr:`_Verdict.UNVERIFIABLE`, not a pass
    (#144): it proves only that a file of that name exists, which is the check that let
    a stale remote copy be indexed as delivered. A sha1 that positively disagrees still
    refutes it, since that is evidence of a difference rather than an absence of evidence.
    """
    if ident is None:
        return _Verdict.REFUTED
    if strict_sha1 and ident.sha1 and entry.sha1 and ident.sha1 != entry.sha1:
        return _Verdict.REFUTED
    if ident.claimed_size is None:
        return _Verdict.UNVERIFIABLE
    if ident.claimed_size != entry.size:
        return _Verdict.REFUTED
    return _Verdict.VERIFIED


def _is_older_copy(path: Path, ident) -> bool:
    """Whether the remote copy ``ident`` is a byte-prefix of the local file at ``path``.

    A file pushed while it was still being written leaves its first bytes on Drive, and
    the post-upload check rightly refuses to index that (#148). The file is then not in
    the index, so the next push meets "already exists" and the adoption check refutes
    the shorter copy, as a conflict on every push from then on (#168). A remote copy
    whose content equals the start of the local file is an older copy of this same
    file, and the local file can replace it as a new revision. A revision destroys
    nothing, since Drive's version history keeps the copy it replaces.

    Needs the remote's plaintext size and sha1. Without either it proves nothing, and the
    file stays a conflict.
    """
    if ident is None or ident.claimed_size is None or not ident.sha1:
        return False
    digest = hashlib.sha1()
    remaining = ident.claimed_size
    try:
        if remaining > path.stat().st_size:
            return False
        with path.open("rb") as handle:
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return False
    return digest.hexdigest() == ident.sha1


def _is_own_upload(record: dict | None, remote_path: str, ident) -> bool:
    """Whether the remote copy ``ident`` at ``remote_path`` is the unverified upload an
    earlier push recorded (:meth:`~protonfs.index.IndexStore.underdelivered`).

    It is, while the node's active revision is still the one recorded: nobody has
    uploaded over it since. This covers what :func:`_is_older_copy` cannot prove, such
    as a listing with no sha1, or a file rewritten rather than appended to (#168).
    """
    return (
        record is not None
        and ident is not None
        and bool(ident.revision)
        and record["remote_path"] == remote_path
        and record["revision"] == ident.revision
    )


def _revision_rels(ctx: RepoContext, rels: list[str], remote_parent: str) -> set[str]:
    """The files in ``rels`` to upload as a new revision of their existing remote node.

    A file qualifies when this device indexed its content before (``local_state`` is
    ``present`` with a recorded sha256, at this same remote path) and the remote copy is
    not provably different from that record. The file is in ``rels`` only because its
    local content changed, so the change is ours: uploading it as a revision keeps the
    node, and Drive's version history keeps the copy it replaces.

    A remote copy that is provably different -- its plaintext size or sha1 disagrees with
    the index -- changed on the remote as well. That is a real conflict and takes the
    ordinary path, where it surfaces as one. A remote copy whose listing carries no size
    or digest is not provably different, and a revision destroys nothing, so it is sent;
    it is then reported unverified by the post-upload check rather than indexed.

    Lists ``remote_parent`` only when at least one file in ``rels`` is indexed.
    """
    indexed = {}
    for rel in rels:
        entry = ctx.index.get(rel)
        if (
            entry is not None
            and entry.local_state == "present"
            and entry.sha256
            and entry.remote_path == f"{remote_parent}/{Path(rel).name}"
        ):
            indexed[rel] = entry
    if not indexed:
        return set()
    identities = ctx.drive.remote_identities(remote_parent)
    revisions = set()
    for rel, entry in indexed.items():
        ident = identities.get(Path(rel).name)
        if ident is None:
            continue  # nothing of that name remains: an ordinary upload recreates it
        if ident.claimed_size is not None and ident.claimed_size != entry.size:
            continue
        if ident.sha1 and entry.sha1 and ident.sha1 != entry.sha1:
            continue
        revisions.add(rel)
    return revisions


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


def _ensure_remote_dir(ctx: RepoContext, remote_dir: str, known: set[str]) -> None:
    """Create every missing intermediate Drive folder from ``remote_root`` to ``remote_dir``.

    Walks the path segment by segment, creating each folder not in ``known``; an
    "already exists" error is ignored (a genuine failure surfaces on the subsequent
    upload). No-op when ``remote_dir`` is outside ``remote_root`` or is the root itself.

    :param ctx: the loaded repo context.
    :param remote_dir: absolute Drive directory the upload targets.
    :param known: Drive folders already created or confirmed in this push; each folder
        this call creates is added, so it is never created twice (#185).
    """
    root = ctx.config.remote_root.rstrip("/")
    if not remote_dir.startswith(root):
        return
    relative = remote_dir[len(root) :].strip("/")
    if not relative:
        return
    current = root
    for segment in relative.split("/"):
        child = f"{current}/{segment}"
        if child not in known:
            try:
                ctx.drive.create_folder(current, segment)
                known.add(child)
            except DriveError as exc:
                # Already there is fine. Any other failure surfaces on the upload call,
                # and the folder is tried again for the next one.
                if "already exist" in str(exc).lower():
                    known.add(child)
        current = child


def push(
    ctx: RepoContext,
    subpath: str | None,
    resolve: str | None,
    dry_run: bool,
    reporter=None,
    manifest_updates: dict | None = None,
    min_age: float = 0.0,
    now: float | None = None,
    only: set[str] | None = None,
) -> TransferResult:
    """Upload local-only and locally-changed files to Drive.

    Scans the tree, classifies each file against the index, and uploads everything
    local-only or changed, creating any missing remote directories first. On success
    the index is updated to reflect the new synced state.

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree to push, or ``None`` for everything.
    :param resolve: divergence policy ``remote`` | ``local`` | ``both`` (mapped to a
        proton-drive ``-f`` strategy via :data:`_RESOLVE_TO_STRATEGY`), or a raw
        proton-drive strategy name ``merge`` | ``keep-both`` | ``replace`` | ``skip``,
        for files that diverged on both sides; ``None`` to surface them unresolved.
    :param dry_run: when true, report what would upload without transferring or
        persisting anything.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    :param manifest_updates: a dict to collect the remote-manifest entries for every file
        this push verified, for the caller to write once (#146; the CLI pushes several
        paths per run). ``None`` writes them at the end of this call instead, when the
        repo maintains a manifest.
    :param min_age: settle window in seconds: a file modified more recently than this is
        held back, reported, and counted in ``unsettled_items`` instead of uploaded, so a
        file still being written is never sent part-way (#168). ``0`` (the default) holds
        nothing back.
    :param now: the current time, for tests; defaults to :func:`time.time`.
    :param only: push just these repo-relative files (those inside ``subpath``), in one
        pass, instead of scanning all of ``subpath``.
    :returns: a :class:`~protonfs.drive.TransferResult` of what was uploaded/skipped.
    :raises protonfs.drive.DriveError: on a Drive or lock failure.

    .. versionchanged:: 1.11.0
       A name conflict for a file that is absent from the remote listing is now reported
       as a distinct "phantom" failure rather than as a different-file conflict (#138).

    .. versionchanged:: 1.12.3
       A file that changed locally since this device indexed it is uploaded as a new
       revision of its remote node (``merge``) when the remote copy is not provably
       different from the index; before, proton-drive rejected it as a name conflict
       and it was never re-uploaded without ``--resolve`` (#144). A remote copy whose
       listing reports no plaintext size is no longer accepted as verified: the file
       is still uploaded, but reported as an ``unverified`` failure and not indexed
       (nor adopted), so ``status`` does not count it and the next push retries it.

    .. versionchanged:: 2.1.0
       Every verified upload or adoption is recorded in the remote manifest when the repo
       maintains one (``defaults.manifest``); a failure to update it is a warning, never
       a push failure (#146).

    .. versionchanged:: 2.4.0
       Added the settle window (``min_age``, off by default) and ``now`` (#168), and
       ``only``, so the CLI pushes N named files in one pass rather than N (#171). A name
       conflict with this file's own older copy on Drive (a byte-prefix of the local
       file, or an unverified upload recorded by an earlier push) is resolved by uploading
       a new revision of it, not reported as a conflict on every push (#168).

    .. versionchanged:: 2.5.0
       A file proton-drive cannot thumbnail on this host is uploaded again without a
       thumbnail, after a warning, instead of failing; ``defaults.skip_thumbnails`` sends
       every file without one (#183). A directory whose every file is new and being
       pushed goes up whole in one ``filesystem upload -d merge`` call, several per call
       up to ``defaults.batch_size`` files, and each remote folder is created once per
       push rather than once per folder that needs it (#185).

    .. seealso:: :func:`protonfs.commands.pull.pull` for the download direction.
    """
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
    if only is None:
        reporter.phase("scanning local", subpath=subpath or ".")
    else:
        reporter.phase("scanning local", files=len(only))
    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    from protonfs.hashcache import HashCache

    local = scan(
        ctx.root, scan_root, ignore, ctx.index,
        low_io=ctx.config.defaults.low_io, reporter=reporter,
        hash_cache=HashCache(ctx.root), only=only,
    )
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
    held: list[str] = []
    if min_age > 0:
        from protonfs.retention import format_age, is_settled

        clock = time.time() if now is None else now
        held = [rel for rel in to_push if not is_settled(local[rel].mtime, min_age, clock)]
        for rel in held:
            reporter.warn(
                f"hold {rel}: modified within the settle window "
                f"(min-age {format_age(min_age)}), not pushed yet"
            )
        held_set = set(held)
        to_push = [rel for rel in to_push if rel not in held_set]
    if dry_run or not to_push:
        return TransferResult(len(to_push), 0, 0, [], unsettled_items=len(held))

    reporter.phase("uploading", files=len(to_push))

    # #17: make sure remote_root itself exists (and is a valid path) before uploading, so a
    # first push to a brand-new Drive location works instead of failing on every file.
    ensure_remote_root(ctx)

    # D2.1: default push applies NO conflict strategy so the CLI surfaces conflicts as
    # named per-file failures (never a silent skip that we'd falsely index). A strategy
    # is used only when the user explicitly asks via --resolve. Canonical remote|local|both
    # map to proton-drive `-f` strategies (#124); legacy strategy names pass through.
    strategy = _RESOLVE_TO_STRATEGY.get(resolve, resolve)
    run = _PushRun(
        ctx=ctx,
        local=local,
        reporter=reporter,
        total=TransferResult(0, 0, 0, [], unsettled_items=len(held)),
        planned=len(to_push),
        planned_bytes=sum(local[rel].size for rel in to_push),
        synced_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        verified={} if manifest_updates is None else manifest_updates,
        created={ctx.config.remote_root.rstrip("/")},
    )
    total = run.total

    # #32 hard guard: refuse to upload any file whose bytes parse as an LFS pointer stub,
    # independent of how it got into `to_push` -- classification should already exclude
    # these (see diff.SyncState.LFS_POINTER), but this is the last line of defense against
    # ever overwriting real remote content with a 131-byte placeholder.
    pointer_rels = {rel for rel in to_push if is_pointer_stub(ctx.root / rel)}
    for rel in sorted(pointer_rels):
        logger.warning("push refused: %s is a git-LFS pointer stub", rel)
        total.failed_items += 1
        total.failures.append(
            {"name": Path(rel).name, "error": LFS_POINTER_ERROR, "kind": LFS_POINTER_KIND}
        )
    if pointer_rels:  # #93: refused stubs still count as processed
        run.advance(sorted(pointer_rels))
    rels = [rel for rel in to_push if rel not in pointer_rels]

    # #185: a directory whose every file is new and being pushed goes up in one call, with
    # proton-drive creating its subfolders, instead of a create-folder per path segment and
    # an upload process per folder. Several such directories under one parent share a call.
    whole_dirs, rels = _whole_dirs(ctx, rels, local, run.batch_size)
    changed: list[str] = []
    for parent, dirs in group_by_parent(list(whole_dirs)).items():
        remote_parent = _remote_dir(ctx, parent)
        run.ensure_dir(remote_parent)
        for chunk in _pack(dirs, whole_dirs, run.batch_size):
            # Checked again just before the call: a file that appeared since the scan
            # (a dump still being written, a temp file) must not ride along unchosen.
            ready = []
            for directory in chunk:
                if _holds_exactly(ctx.root / directory, ctx.root, set(whole_dirs[directory])):
                    ready.append(directory)
                else:
                    changed += whole_dirs[directory]
            if ready:
                run.execute(
                    [_Job([rel for d in ready for rel in whole_dirs[d]],
                          [ctx.root / d for d in ready], remote_parent, Path(parent),
                          strategy, run.skip_thumbnails, whole_dirs=True)]
                )
    rels = changed + rels

    for parent, group in group_by_parent(rels).items():
        remote_parent = _remote_dir(ctx, parent)
        run.ensure_dir(remote_parent)
        # #144: a changed file this device uploaded before is a new revision of that node,
        # not a new file -- see _revision_rels. Only when the user chose no strategy.
        revision_rels = (
            _revision_rels(ctx, group, remote_parent) if strategy is None else set()
        )
        run.execute(
            run.file_jobs([r for r in group if r not in revision_rels], strategy)
            + run.file_jobs([r for r in group if r in revision_rels], REVISION_STRATEGY)
        )

    if manifest_updates is None and run.verified:
        manifest.update(ctx, record=run.verified, reporter=reporter)
    ctx.index.save()
    run.finish()
    reporter.done(
        "uploaded",
        transferred=total.transferred_items,
        adopted=total.adopted_items,
        failed=total.failed_items,
    )
    return total


def _remote_dir(ctx: RepoContext, parent: str) -> str:
    """The Drive directory for the repo-relative directory ``parent`` (``"."`` = root)."""
    return f"{ctx.config.remote_root}/{parent}" if parent != "." else ctx.config.remote_root


def _ancestors(rel: str) -> list[str]:
    """The directories containing ``rel``, innermost first, excluding the repo root."""
    return [parent.as_posix() for parent in list(Path(rel).parents)[:-1]]


def _whole_dirs(
    ctx: RepoContext, rels: list[str], local: dict, limit: int
) -> tuple[dict[str, list[str]], list[str]]:
    """Split ``rels`` into directories to upload whole and the files left over (#185).

    A directory is uploaded whole when it holds at most ``limit`` files with no two
    sharing a name, every entry in it is a regular file or a directory, and every file
    is in ``rels`` and absent from the index (new on this device). Nothing ignored, held
    back, unchanged or needing a revision can then ride along, and a failure proton-drive
    reports by file name names exactly one file. Only the outermost such directory is
    taken, never the repo root. Directories holding a scanned or indexed file not being
    sent are rejected without touching the disk; the rest are read once to rule out
    ignored files, symlinks and empty folders.

    :param ctx: the loaded repo context.
    :param rels: repo-relative files to push.
    :param local: the scan, keyed by repo-relative path (every non-ignored file in scope).
    :param limit: the most files one whole directory may hold (``defaults.batch_size``).
    :returns: ``(dirs, rest)``: each repo-relative directory to upload whole, mapped to
        its files in ``rels`` order, and the files of ``rels`` in none of them, in order.
    """
    pending = {rel for rel in rels if ctx.index.get(rel) is None}
    blocked = {
        ancestor
        for rel in [*(r for r in local if r not in pending), *ctx.index.all()]
        for ancestor in _ancestors(rel)
    }
    members: dict[str, list[str]] = {}
    for rel in pending:
        for ancestor in _ancestors(rel):
            members.setdefault(ancestor, []).append(rel)
    chosen: list[str] = []
    # Outermost first: a directory sorts before everything beneath it.
    for directory in sorted(members, key=lambda d: (d.count("/"), d)):
        files = members[directory]
        if (
            directory in blocked
            or len(files) > limit
            or len({Path(rel).name for rel in files}) < len(files)
            or any(directory.startswith(c + "/") for c in chosen)
        ):
            continue
        if _holds_exactly(ctx.root / directory, ctx.root, set(files)):
            chosen.append(directory)
    dirs: dict[str, list[str]] = {d: [] for d in chosen}
    rest = []
    for rel in rels:
        owner = next((d for d in Path(rel).parents if d.as_posix() in dirs), None)
        if owner is None:
            rest.append(rel)
        else:
            dirs[owner.as_posix()].append(rel)
    return dirs, rest


def _holds_exactly(directory: Path, root: Path, files: set[str]) -> bool:
    """Whether ``directory`` holds only directories and exactly the repo-relative ``files``.

    False on any symlink, special file, empty directory or unreadable entry, so a whole-
    directory upload never carries something push did not choose to send.
    """
    seen = 0
    stack = [directory]
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                empty = True
                for entry in entries:
                    empty = False
                    if entry.is_symlink():
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        if Path(entry.path).relative_to(root).as_posix() not in files:
                            return False
                        seen += 1
                    else:
                        return False
                if empty:
                    return False
    except OSError:
        return False
    return seen == len(files)


def _pack(dirs: list[str], files: dict[str, list[str]], limit: int) -> list[list[str]]:
    """Group ``dirs`` into runs of at most ``limit`` files with no file name repeated.

    A name shared by two files in one call would leave a failure proton-drive reports
    by name pointing at both, so such directories go in separate calls.
    """
    chunks: list[list[str]] = []
    names: set[str] = set()
    for directory in dirs:
        own = {Path(rel).name for rel in files[directory]}
        if chunks and len(names) + len(own) <= limit and not names & own:
            chunks[-1].append(directory)
            names |= own
        else:
            chunks.append([directory])
            names = set(own)
    return chunks


@dataclass
class _Job:
    """One ``filesystem upload`` call and the repo-relative files it delivers.

    ``paths`` are files, or with ``whole_dirs`` directories, sent into ``remote_parent``;
    ``base`` is the repo-relative directory that ``remote_parent`` mirrors.
    """

    rels: list[str]
    paths: list[Path]
    remote_parent: str
    base: Path
    strategy: str | None
    skip_thumbnails: bool
    whole_dirs: bool = False

    def named(self, name: str) -> tuple[set[str], set[str]]:
        """Files proton-drive may mean by a failure named ``name``.

        proton-drive names a failed file by its basename alone, and a failed folder by
        its own name. A call never holds two files of one name (see :func:`_pack`), but a
        folder name can still match several files, and every one is returned, so none is
        trusted on the strength of it.

        :returns: ``(files, beneath)``: the files with that basename, and the files
            inside a folder of that name.
        """
        files, beneath = set(), set()
        for rel in self.rels:
            parts = Path(rel).relative_to(self.base).parts
            if parts[-1] == name:
                files.add(rel)
            elif name in parts[:-1]:
                beneath.add(rel)
        return files, beneath


@dataclass
class _PushRun:
    """State shared by every upload call of one push: totals, progress, folders made."""

    ctx: RepoContext
    local: dict
    reporter: object
    total: TransferResult
    planned: int
    planned_bytes: int
    synced_at: str
    verified: dict
    created: set[str]
    done: int = 0
    done_bytes: int = 0

    @property
    def skip_thumbnails(self) -> bool:
        """Whether every upload passes ``-t`` (``defaults.skip_thumbnails``, #183)."""
        return self.ctx.config.defaults.skip_thumbnails

    @property
    def batch_size(self) -> int:
        """Files per upload call (``defaults.batch_size``)."""
        return self.ctx.config.defaults.batch_size

    def advance(self, rels: list[str]) -> None:
        """Count ``rels`` as handed to proton-drive and report progress (#93, #185)."""
        from protonfs.commands.ls import human_size

        self.done += len(rels)
        self.done_bytes += sum(self.local[rel].size for rel in rels)
        self.reporter.progress(
            self.done, self.planned,
            size=f"{human_size(self.done_bytes)}/{human_size(self.planned_bytes)}",
        )

    def finish(self) -> None:
        """Report the final, unthrottled progress line, in the same shape as the others."""
        from protonfs.commands.ls import human_size

        size = human_size(self.planned_bytes)
        self.reporter.progress(self.planned, self.planned, force=True, size=f"{size}/{size}")

    def ensure_dir(self, remote_dir: str) -> None:
        """Create ``remote_dir`` and its missing ancestors, once per push (#185)."""
        _ensure_remote_dir(self.ctx, remote_dir, self.created)

    def file_jobs(
        self, rels: list[str], strategy: str | None, *, skip_thumbnails: bool | None = None
    ) -> list[_Job]:
        """Per-folder upload calls for ``rels``, at most ``batch_size`` files each."""
        skip = self.skip_thumbnails if skip_thumbnails is None else skip_thumbnails
        jobs = []
        for parent, group in group_by_parent(rels).items():
            remote_parent = _remote_dir(self.ctx, parent)
            for batch in batches(group, self.batch_size):
                jobs.append(
                    _Job(batch, [self.ctx.root / rel for rel in batch], remote_parent,
                         Path(parent), strategy, skip)
                )
        return jobs

    def execute(self, jobs: list[_Job]) -> None:
        """Run ``jobs``, verify every file against the remote, and index what landed.

        Follow-up rounds resend files proton-drive could not thumbnail (#183) and files
        whose remote copy proves to be an older copy of themselves (#168). The index is
        checkpointed at the end.
        """
        retry_round = False
        while jobs:
            jobs = self._round(jobs, retry_round)
            retry_round = True
        # #3: persist after each group of calls so an interruption (Ctrl-C, dropped
        # connection) resumes from here on the next run instead of re-doing everything.
        # Composed with #1's atomic writes, each of these saves is crash-safe. #170: a
        # failed one is not fatal; the final save persists everything.
        self.ctx.index.checkpoint()

    def _round(self, jobs: list[_Job], retry_round: bool) -> list[_Job]:
        """Upload ``jobs``, verify their files, and return the follow-up jobs, if any.

        ``retry_round`` marks a follow-up round: its files were counted for progress
        already, and a remote copy is no longer checked for being an older copy.
        """
        ctx, total = self.ctx, self.total
        # Files proton-drive did not report as a (real) failure or skip -- candidates to
        # VERIFY against the remote before we trust them as delivered (#22). A file that
        # failed upload with an "already exists" name-conflict is ALSO a candidate, but to
        # ADOPT: verify the remote copy matches and record it without re-uploading.
        candidates: list[str] = []
        adopt_rels: set[str] = set()
        # Files in a call proton-drive reported skips for: possibly uploaded, possibly
        # skipped as already-present. Verified strictly, and reported as an
        # under-delivery rather than a conflict when they fail -- "we could not confirm
        # this" is what actually happened, and it must not read as "the remote differs".
        unattributed_rels: set[str] = set()
        # Files sent with a strategy that writes to the node at their own name (none,
        # merge, replace); skip and keep-both leave whatever holds that name alone.
        # Only these may be recorded as push's own short upload (#168).
        written_rels: set[str] = set()
        no_thumbnail: list[tuple[str, str | None]] = []
        for job in jobs:
            result = ctx.drive.upload(
                job.paths, job.remote_parent, file_strategy=job.strategy,
                folder_strategy="merge" if job.whole_dirs else None,
                skip_thumbnails=job.skip_thumbnails,
            )
            total.skipped_items += result.skipped_items
            if not retry_round:  # a retried file was already counted once (#93)
                self.advance(job.rels)

            # Partition the call's failures. An "already exists" conflict is an adoption
            # candidate (verified below). #183: a file this host cannot thumbnail goes
            # again without a thumbnail, rather than failing on every push; sent with -t
            # already, it is a failure. Everything else is a real failure now, and so is
            # every file a failure might name (see _Job.named).
            conflict_rels: set[str] = set()
            thumbnail_rels: set[str] = set()
            failed_rels: set[str] = set()
            real_failures = []
            for failure in result.failures:
                files, beneath = job.named(str(failure.get("name", "")))
                if _is_already_exists(failure) and not files and not beneath:
                    continue  # names nothing sent: verification settles every file
                if files and _is_already_exists(failure):
                    conflict_rels |= files
                elif files and not job.skip_thumbnails and is_thumbnail_failure(failure):
                    thumbnail_rels |= files
                else:
                    real_failures.append(failure)
                    failed_rels |= files | beneath
            total.failed_items += len(real_failures)
            total.failures += real_failures
            for rel in sorted(thumbnail_rels - failed_rels):
                logger.warning(
                    "push: no thumbnail codec on this host for %s; "
                    "uploading it without a thumbnail", rel
                )
                no_thumbnail.append((rel, job.strategy))
            failed_rels |= thumbnail_rels
            if job.whole_dirs:  # proton-drive created the folders of what it took (#185)
                for rel in set(job.rels) - failed_rels:
                    for folder in Path(rel).parents:
                        if folder == job.base:
                            break
                        self.created.add(_remote_dir(ctx, folder.as_posix()))
            for rel in job.rels:
                # A conflict was not transferred (it already existed) -- no "^" upload line.
                if rel not in failed_rels and rel not in conflict_rels:
                    self.reporter.item("^", rel)

            # D2.1: a skip is reported only as an aggregate count, so we cannot tell
            # WHICH files in the call were skipped. Indexing an unconfirmed hash would
            # be wrong, but so was the original response of indexing none of them and
            # "leaving them for the next push": the next push takes this same branch, so
            # a file proton-drive keeps skipping never converges (#145). Not knowing
            # which file was skipped is a reason to verify all of them against the
            # remote, not to verify none -- the per-file check below settles each one.
            unattributed = result.skipped_items > 0
            for rel in job.rels:
                if rel in failed_rels:
                    continue
                candidates.append(rel)
                if job.strategy not in ("skip", "keep-both"):
                    written_rels.add(rel)
                if rel in conflict_rels:
                    adopt_rels.add(rel)
                elif unattributed:
                    unattributed_rels.add(rel)

        retry_jobs = []
        for job_strategy in dict.fromkeys(s for _, s in no_thumbnail):
            group = [rel for rel, s in no_thumbnail if s == job_strategy]
            for parent in group_by_parent(group):
                self.ensure_dir(_remote_dir(ctx, parent))
            retry_jobs += self.file_jobs(group, job_strategy, skip_thumbnails=True)
        if not candidates:
            return retry_jobs

        # #22: do NOT trust proton-drive's transferred count -- re-list each remote folder
        # and confirm each candidate actually landed (present, and plaintext claimedSize
        # matches). A file that was claimed-transferred but is absent/short is a silent
        # under-delivery: report it as failed and leave it unindexed so the next push
        # retries it, instead of recording false success and risking data loss on offload.
        # Adoption candidates (#self-heal) run the same verify, stricter (sha1 too): a
        # remote copy that matches is recorded without re-upload; one that differs is a
        # real name-conflict, reported as such rather than silently overwritten.
        # #144: an identity with no plaintext size can confirm neither, so it is neither
        # indexed nor adopted -- see UNVERIFIED_KIND. #185: each file is checked at its own
        # path; one listing per folder, whichever call delivered it.
        identities: dict[str, dict] = {}
        older_copies: list[str] = []
        for rel in candidates:
            entry = self.local[rel]
            name = Path(rel).name
            remote_parent = _remote_dir(ctx, str(Path(rel).parent))
            if remote_parent not in identities:
                identities[remote_parent] = ctx.drive.remote_identities(remote_parent)
            ident = identities[remote_parent].get(name)
            is_adopt = rel in adopt_rels
            strict = is_adopt or rel in unattributed_rels
            verdict = _verify_remote(ident, entry, strict_sha1=strict)
            if verdict is _Verdict.UNVERIFIABLE:
                total.failed_items += 1
                logger.warning(
                    "push unverified: %s is on the remote but the listing reports no "
                    "plaintext size for it, so delivery cannot be verified; it was NOT "
                    "indexed and will be retried on the next push", rel
                )
                total.failures.append(
                    {"name": name, "error": UNVERIFIED_ERROR, "kind": UNVERIFIED_KIND}
                )
                continue
            if verdict is not _Verdict.VERIFIED:
                remote_path = f"{remote_parent}/{name}"
                if is_adopt and not retry_round:
                    if _is_older_copy(ctx.root / rel, ident):
                        why = "is a byte-prefix of the local file"
                    elif _is_own_upload(ctx.index.underdelivered(rel), remote_path, ident):
                        why = "is the unverified upload an earlier push recorded"
                    else:
                        why = None
                    if why is not None:
                        logger.info(
                            "push: %s on the remote %s; uploading the local file as a "
                            "new revision of it", rel, why
                        )
                        older_copies.append(rel)
                        continue
                total.failed_items += 1
                if is_adopt and ident is None:
                    # #138: the name is taken but nothing of that name is listable -- a
                    # phantom node, not a different file. Reported separately so the CLI
                    # never offers --resolve=remote here (that would keep the phantom).
                    logger.warning(
                        "push phantom: %s was rejected as a name conflict but is absent "
                        "from the remote listing", rel
                    )
                    total.failures.append(
                        {"name": name, "error": PHANTOM_ERROR, "kind": PHANTOM_KIND}
                    )
                elif is_adopt:
                    logger.warning(
                        "push conflict: %s already on remote but remote differs from local", rel
                    )
                    total.failures.append(
                        {"name": name, "error": CONFLICT_ERROR, "kind": CONFLICT_KIND}
                    )
                else:
                    if ident is None:
                        reason = "absent"
                    elif ident.claimed_size != entry.size:
                        reason = f"size {ident.claimed_size} != {entry.size}"
                    else:
                        reason = "sha1 differs"
                    logger.warning("push under-delivery: %s not verified (%s)", rel, reason)
                    if (
                        rel in written_rels
                        and rel not in unattributed_rels
                        and ident is not None
                        and ident.revision
                    ):
                        # #168: the node now holds this push's own short upload. Say
                        # so, or the next push can only call it a foreign conflict.
                        ctx.index.mark_underdelivered(rel, remote_path, ident.revision)
                    total.failures.append(
                        {
                            "name": name,
                            "error": UNDERDELIVERED_ERROR,
                            "kind": UNDERDELIVERED_KIND,
                        }
                    )
                continue
            indexed = IndexEntry(
                size=entry.size,
                mtime=entry.mtime,
                sha256=entry.sha256,
                sha1=entry.sha1,
                remote_path=f"{remote_parent}/{name}",
                origin_device=ctx.config.device_id,
                local_state="present",
                last_synced=self.synced_at,
            )
            ctx.index.set(rel, indexed)
            # #146: only now -- verified against a live listing -- may the manifest
            # promise this file; it records the Drive revision that was verified.
            self.verified[rel] = manifest.entry_for_index(
                indexed, ident.revision, self.synced_at
            )
            if is_adopt:
                total.adopted_items += 1
            else:
                total.transferred_items += 1

        # #168: a remote copy that proves to be an older copy of the same file is replaced
        # with a revision in one more round; see _is_older_copy.
        return retry_jobs + self.file_jobs(older_copies, REVISION_STRATEGY)
