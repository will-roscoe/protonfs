"""``protonfs prune``: a retention policy that frees local disk through ``offload``.

Per directory, the ``keep`` most recently modified tracked files stay local; any other
tracked file that has not been modified for ``min_age`` is offloaded (see
:func:`protonfs.retention.select_prune_candidates`). New files are pushed first, so
they are on Drive before anything is considered, and the whole run holds the repo lock
(taken by the CLI), so it can never overlap a push (#158).

Every deletion goes through :func:`~protonfs.commands.offload.offload`, with its live
verification and all of its guards: prune can narrow what offload considers, never
widen it. A file offload would refuse -- unverified on Drive, edited since its last
push, or not yet settled -- is refused here too.

.. versionadded:: 2.2.0
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from protonfs.commands.offload import OffloadResult, offload
from protonfs.context import RepoContext
from protonfs.diff import within_subpath
from protonfs.drive import TransferResult
from protonfs.ignore import IgnoreMatcher
from protonfs.manifest import is_control_path
from protonfs.retention import select_prune_candidates


@dataclass
class PruneResult:
    """Outcome of a :func:`prune` pass.

    :ivar pushed: the result of the push that ran first (``None`` when skipped).
    :ivar considered: tracked files in scope with a local copy.
    :ivar candidates: files the retention policy allowed offload to consider.
    :ivar offload: what offload then did with them.
    """

    pushed: TransferResult | None = None
    considered: int = 0
    candidates: list[str] = field(default_factory=list)
    offload: OffloadResult = field(default_factory=OffloadResult)


def local_tracked_files(ctx: RepoContext, subpath: str | None) -> dict[str, float]:
    """``{rel path: local mtime}`` for tracked, in-scope files that have a local copy.

    "Tracked" means the index records the file as present locally, it is not excluded
    by ``.protonfs/ignore``/``include``, and it lies within ``subpath``.
    """
    ignore = IgnoreMatcher.from_file(ctx.root)
    files: dict[str, float] = {}
    for rel, entry in ctx.index.all().items():
        if entry.local_state != "present" or is_control_path(rel):
            continue
        if not within_subpath(rel, subpath) or ignore.matches(rel):
            continue
        path = ctx.root / rel
        if path.is_file():
            files[rel] = path.stat().st_mtime
    return files


def prune(
    ctx: RepoContext,
    subpath: str | None,
    keep: int,
    min_age: float,
    push_first: bool = True,
    dry_run: bool = False,
    reporter=None,
    now: float | None = None,
) -> PruneResult:
    """Push, then offload whatever the retention policy releases.

    :param ctx: the loaded repo context (the caller holds the repo lock).
    :param subpath: repo-root-relative subtree, or ``None`` for everything.
    :param keep: newest files kept per directory.
    :param min_age: settle window in seconds; also passed to offload.
    :param push_first: push ``subpath`` before choosing candidates (skip with ``False``).
    :param dry_run: report what would be offloaded; push and delete nothing.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through.
    :param now: the current time, for tests; defaults to :func:`time.time`.
    :returns: a :class:`PruneResult`.
    :raises protonfs.retention.RetentionError: on a negative ``keep`` or ``min_age``.
    """
    from protonfs.commands.push import push
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
    now = time.time() if now is None else now
    result = PruneResult()
    if push_first and not dry_run:
        result.pushed = push(ctx, subpath, None, False, reporter=reporter)
    files = local_tracked_files(ctx, subpath)
    result.considered = len(files)
    result.candidates = select_prune_candidates(files, keep, min_age, now)
    reporter.phase("pruning", considered=result.considered, candidates=len(result.candidates))
    if result.candidates:
        result.offload = offload(
            ctx,
            subpath,
            verify=True,
            dry_run=dry_run,
            reporter=reporter,
            min_age=min_age,
            only=set(result.candidates),
            now=now,
        )
    return result
