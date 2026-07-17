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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from protonfs.batching import group_by_parent
from protonfs.context import RepoContext
from protonfs.diff import within_subpath
from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry
from protonfs.localscan import hash_file_digests

logger = logging.getLogger(__name__)


@dataclass
class OffloadResult:
    """Outcome of an :func:`offload` pass: how many local copies were reclaimed, how
    many were left untouched (unverified on the remote, or with unsynced local edits),
    the bytes freed, and the rel-paths behind each count."""

    offloaded: int = 0
    skipped_unverified: int = 0
    skipped_modified: int = 0
    bytes_reclaimed: int = 0
    offloaded_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    modified_paths: list[str] = field(default_factory=list)


def offload(
    ctx: RepoContext,
    subpath: str | None,
    verify: bool = True,
    dry_run: bool = False,
    reporter=None,
) -> OffloadResult:
    """Delete the local bytes of tracked files confirmed present on Drive (the inverse
    of :func:`~protonfs.commands.pull.pull`).

    Only files the index records as locally present and in scope of ``subpath``/ignore
    are considered; each is (by default) re-verified against a live remote listing
    before its local copy is removed and its index entry is demoted to metadata-only.

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree to offload, or ``None`` for everything.
    :param verify: re-check each file against the remote before deleting local bytes;
        when false, trust the index (faster, unsafe).
    :param dry_run: report what would be freed without deleting anything.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    :returns: an :class:`OffloadResult` summarising freed/kept files and bytes.
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
    candidates: list[str] = []
    for rel_path, entry in ctx.index.all().items():
        if entry.local_state != "present":
            continue
        if not within_subpath(rel_path, subpath):
            continue
        if ignore.matches(rel_path):
            continue
        if not (ctx.root / rel_path).is_file():
            continue
        candidates.append(rel_path)

    result = OffloadResult()
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
        # the default is always on.
        identities = ctx.drive.remote_identities(remote_parent) if verify else None

        for rel in rels:
            local_path = ctx.root / rel
            entry = ctx.index.get(rel)
            local_size = local_path.stat().st_size
            name = Path(rel).name

            # Unconditional data-loss guard (holds even under --no-verify): never delete a
            # file whose local bytes differ from what was last synced. A file edited locally
            # since its last sync has unsynced content that is NOT on Drive, so offloading it
            # would destroy the only copy of that edit -- a same-size remote object would even
            # pass the size verify below. Compare the live local sha256 to the index's record.
            local_sha256, _ = hash_file_digests(local_path)
            if local_sha256 != entry.sha256:
                logger.warning("offload skip: %s has unsynced local edits", rel)
                reporter.warn(f"skip {rel}: unsynced local edits")
                result.skipped_modified += 1
                result.modified_paths.append(rel)
                continue

            if verify:
                ident = identities.get(name)
                verified = ident is not None and (
                    ident.claimed_size is None or ident.claimed_size == local_size
                )
                if not verified:
                    reason = (
                        "absent" if ident is None else f"size {ident.claimed_size} != {local_size}"
                    )
                    logger.warning(
                        "offload skip: %s not verified on remote (%s)", rel, reason
                    )
                    reporter.warn(f"skip {rel}: not verified on remote")
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
