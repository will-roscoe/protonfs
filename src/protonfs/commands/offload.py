# src/protonfs/commands/offload.py
"""Drop the LOCAL bytes of protonfs-tracked files that are safely on Drive.

The inverse of ``pull``: ``push`` uploads, ``pull`` downloads, ``rm`` deletes the
REMOTE copy, ``offload`` deletes the LOCAL copy only (to reclaim disk space) while
leaving the index entry as ``local_state="metadata-only"`` -- the file is still
known and a subsequent ``pull`` restores it in full, so offload is reversible.

Safety (the #22/#3 lesson)
---------------------------
An index that trusts an unverified push is not enough: proton-drive can report a
transfer as successful without the bytes actually landing (#22), and an index
entry could in principle grow stale relative to what is really on the remote. So
before deleting *any* local file, this module re-lists the remote parent via
``ctx.drive.remote_identities`` and only offloads a file that is confirmed present
there with a plaintext ``claimed_size`` matching the local file's byte size --
mirroring the exact verify-against-remote idiom `commands/push.py` uses after
upload. Any file that fails this check is left untouched locally and reported as
``skipped_unverified``; nothing is ever deleted based on the index alone.

Settle rule (#158)
------------------
A file can still be in use with no process holding it open -- a simulation closes a
dump and keeps appending to its time series for days. So a file modified within the
last ``min_age`` seconds (see :mod:`protonfs.retention`) is never offloaded, whatever
the remote holds; it is reported as ``skipped_unsettled``. And a file that was pushed
while still inside that window is re-verified against the live listing before its
local copy is removed, even under ``--no-verify``: the index entry written at push
time described a file that was still changing.

.. versionadded:: 1.0.0
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from pathlib import Path

from protonfs.batching import group_by_parent
from protonfs.context import RepoContext
from protonfs.diff import within_subpath
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry
from protonfs.localscan import hash_file_digests
from protonfs.retention import DEFAULT_MIN_AGE, format_age, is_settled, parse_duration

DEFAULT_MIN_AGE_SECONDS = parse_duration(DEFAULT_MIN_AGE)


@dataclass
class OffloadResult:
    """Outcome of an :func:`offload` pass: how many local copies were reclaimed, how
    many were left untouched (unverified on the remote, with unsynced local edits, or
    modified too recently to be settled), the bytes freed, and the rel-paths behind
    each count.

    .. versionchanged:: 2.2.0
       Added ``skipped_unsettled``/``unsettled_paths`` (#158).
    """

    offloaded: int = 0
    skipped_unverified: int = 0
    skipped_modified: int = 0
    bytes_reclaimed: int = 0
    offloaded_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    modified_paths: list[str] = field(default_factory=list)
    skipped_unsettled: int = 0
    unsettled_paths: list[str] = field(default_factory=list)

    def merge(self, other: OffloadResult) -> None:
        """Add ``other``'s counts and paths into this result."""
        for name in (
            "offloaded", "skipped_unverified", "skipped_modified", "bytes_reclaimed",
            "skipped_unsettled",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for name in ("offloaded_paths", "skipped_paths", "modified_paths", "unsettled_paths"):
            getattr(self, name).extend(getattr(other, name))


def _pushed_unsettled(entry: IndexEntry, min_age: float) -> bool:
    """Whether the index entry was written while its file was still inside the settle
    window (last sync less than ``min_age`` after the file's last modification). An
    unreadable timestamp counts as unsettled -- the safe reading."""
    try:
        synced = datetime.datetime.fromisoformat(entry.last_synced).timestamp()
    except (TypeError, ValueError):
        return True
    return synced - entry.mtime < min_age


def offload(
    ctx: RepoContext,
    subpath: str | None,
    verify: bool = True,
    dry_run: bool = False,
    reporter=None,
    min_age: float = DEFAULT_MIN_AGE_SECONDS,
    only: set[str] | None = None,
    now: float | None = None,
) -> OffloadResult:
    """Delete the local bytes of tracked files confirmed present on Drive (the inverse
    of :func:`~protonfs.commands.pull.pull`).

    Only files the index records as locally present and in scope of ``subpath``/ignore
    are considered; each is (by default) re-verified against a live remote listing
    before its local copy is removed and its index entry is demoted to metadata-only.

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree to offload, or ``None`` for everything.
    :param verify: re-check each file against the remote before deleting local bytes.
        A file whose remote copy cannot be verified is skipped, not deleted;
        when false, trust the index (faster, unsafe).
    :param dry_run: report what would be freed without deleting anything.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    :param min_age: settle window in seconds: a file modified more recently than this is
        left alone (``0`` disables it). Defaults to one day.
    :param only: restrict the pass to these repo-relative paths (``prune`` passes its
        retention candidates); every check below still applies to each of them.
    :param now: the current time, for tests; defaults to :func:`time.time`.
    :returns: an :class:`OffloadResult` summarising freed/kept files and bytes.

    .. versionchanged:: 2.2.0
       Added the settle rule (``min_age``, default one day) and ``only`` (#158).
    """
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
    reporter.phase("scanning candidates", subpath=subpath or ".")
    ignore = IgnoreMatcher.from_file(ctx.root)

    # Only consider files that are: recorded in the index as locally present, in
    # scope of the ignore matcher, within the requested subpath, and that actually
    # exist locally right now. This is what keeps offload from ever touching a
    # git-tracked source file, a config, or any other file the index has never
    # heard of -- those simply never make it into `candidates`.
    from protonfs.manifest import is_control_path

    candidates: list[str] = []
    for rel_path, entry in ctx.index.all().items():
        if entry.local_state != "present":
            continue
        if is_control_path(rel_path):
            continue  # #146: protonfs's own state (an older host may have indexed it)
        if not within_subpath(rel_path, subpath):
            continue
        if ignore.matches(rel_path):
            continue
        if not (ctx.root / rel_path).is_file():
            continue
        if only is not None and rel_path not in only:
            continue
        candidates.append(rel_path)

    result = OffloadResult()
    now = time.time() if now is None else now
    if not candidates:
        reporter.done("offloaded", files=result.offloaded, reclaimed=result.bytes_reclaimed)
        return result

    for parent, rels in group_by_parent(candidates).items():
        remote_parent = (
            f"{ctx.config.remote_root}/{parent}" if parent != "." else ctx.config.remote_root
        )
        # #22/#3: never trust the index alone -- re-list the remote parent and require
        # each candidate to appear there with a matching plaintext size before its local
        # bytes are deleted. `verify=False` is an explicit opt-out (--no-verify) only;
        # the default is always on. #158: even then, a file pushed while still unsettled
        # is re-verified, so the listing is fetched lazily for those.
        identities = ctx.drive.remote_identities(remote_parent) if verify else None

        for rel in rels:
            local_path = ctx.root / rel
            entry = ctx.index.get(rel)
            stat = local_path.stat()
            local_size = stat.st_size
            name = Path(rel).name

            # #158 settle rule, before anything else: a file modified within the window
            # may still be being written, so it is never a candidate, whatever else holds.
            if not is_settled(stat.st_mtime, min_age, now):
                reporter.warn(
                    f"skip {rel}: modified {format_age(now - stat.st_mtime)} ago, not yet "
                    f"settled (min-age {format_age(min_age)})"
                )
                result.skipped_unsettled += 1
                result.unsettled_paths.append(rel)
                continue

            # Unconditional data-loss guard (holds even under --no-verify): never delete a
            # file whose local bytes differ from what was last synced. A file edited locally
            # since its last sync has unsynced content that is NOT on Drive, so offloading it
            # would destroy the only copy of that edit -- a same-size remote object would even
            # pass the size verify below. Compare the live local sha256 to the index's record.
            local_sha256, _ = hash_file_digests(local_path)
            if local_sha256 != entry.sha256:
                reporter.warn(f"skip {rel}: unsynced local edits")
                result.skipped_modified += 1
                result.modified_paths.append(rel)
                continue

            must_verify = verify or _pushed_unsettled(entry, min_age)
            if must_verify:
                if identities is None:
                    identities = ctx.drive.remote_identities(remote_parent)
                ident = identities.get(name)
                # An unverifiable identity is NOT a pass. Treating a missing claimed_size
                # as "fine" is what let a stale remote copy look verified (#147) while
                # this deleted the only full one; deletion is the operation with no undo,
                # so it is the one that must refuse when it cannot check.
                if ident is None:
                    reason = "absent from the remote listing"
                elif ident.claimed_size is None:
                    reason = "remote reports no size, so it cannot be verified"
                elif ident.claimed_size != local_size:
                    reason = f"remote size {ident.claimed_size} != local {local_size}"
                elif ident.sha1 and entry.sha1 and ident.sha1 != entry.sha1:
                    reason = "remote digest differs from the indexed copy"
                else:
                    reason = None
                if reason is not None:
                    reporter.warn(f"skip {rel}: {reason}")
                    result.skipped_unverified += 1
                    result.skipped_paths.append(rel)
                    continue

            result.offloaded += 1
            result.bytes_reclaimed += local_size
            result.offloaded_paths.append(rel)

            if dry_run:
                continue

            reporter.item("x", rel)
            local_path.unlink()
            ctx.index.set(
                rel,
                IndexEntry(
                    size=entry.size,
                    mtime=entry.mtime,
                    sha256=entry.sha256,
                    sha1=entry.sha1,
                    remote_path=entry.remote_path,
                    origin_device=entry.origin_device,
                    local_state="metadata-only",
                    last_synced=entry.last_synced,
                ),
            )

        # #3: persist after each parent group so an interruption resumes from here
        # rather than re-deleting/re-verifying everything (mirrors push/pull).
        if not dry_run:
            ctx.index.save()
    if not dry_run:
        ctx.index.save()
    reporter.done("offloaded", files=result.offloaded, reclaimed=result.bytes_reclaimed)
    return result
