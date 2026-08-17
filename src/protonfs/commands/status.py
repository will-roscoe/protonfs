"""``protonfs status``: summarise sync state and map it to a script-friendly exit code.

.. versionadded:: 1.0.0
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from protonfs.context import RepoContext
from protonfs.diff import SyncState, classify, within_subpath
from protonfs.ignore import IgnoreMatcher
from protonfs.localscan import scan

# Exit codes for `protonfs status`, so an unattended caller (a script, a CI step) can
# branch on the outcome without parsing the printed counts. They are ordered by severity
# and documented in the CLI help and docs -- treat them as a stable contract.
STATUS_CLEAN = 0  # every file is synced or intentionally remote-only (nothing to reconcile)
STATUS_DRIFT = 1  # non-conflict divergence exists (something to push / pull / prune)
STATUS_CONFLICT = 2  # at least one genuine conflict a human or --resolve strategy must settle

# States that represent a settled, no-action-needed condition: SYNCED (in step with the
# remote), METADATA_ONLY (a remote file this device has deliberately not materialised),
# and LFS_POINTER (#32: an unmaterialised git-LFS pointer stub -- protonfs deliberately
# does nothing with it, so it is not actionable drift either).
_QUIESCENT = frozenset({SyncState.SYNCED, SyncState.METADATA_ONLY, SyncState.LFS_POINTER})
# States that represent a genuine conflict: both sides diverged, or a local change with no
# provable remote view to attribute a direction.
_CONFLICT = frozenset({SyncState.CONFLICT, SyncState.BOTH_MODIFIED})


def compute_status(ctx: RepoContext, subpath: str | None, reporter=None) -> Counter:
    """Summarise sync state as a count of files per :class:`~protonfs.diff.SyncState`.

    Scans the working tree (scoped to ``subpath`` when given), classifies each file
    against the local index, and tallies the resulting states.

    :param ctx: the loaded repo context (root, config, index, drive).
    :param subpath: repo-root-relative subtree to restrict the scan to, or ``None``
        for the whole tree.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    :returns: a :class:`collections.Counter` keyed by ``SyncState.value``.

    .. seealso:: :func:`status_exit_code` maps this summary to a process exit code.
    """
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
    reporter.phase("scanning", subpath=subpath or ".")
    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)
    # #96: classify() sees the whole repo-wide index; the scan above is scoped. Filter
    # so `status SUBPATH` never counts (or exits non-zero for) entries outside SUBPATH.
    entries = classify(local, ctx.index)
    return Counter(
        entry.state.value for entry in entries if within_subpath(entry.rel_path, subpath)
    )


def status_exit_code(counts: Counter) -> int:
    """Map a status summary to an exit code: conflict (2) outranks drift (1) outranks clean (0).

    A conflict is the most severe outcome, so it wins even when ordinary drift is also
    present. Drift is any non-quiescent, non-conflict state (local-only, remote-only,
    local/remote-modified, local/remote-deleted, remote-changed).
    """
    if any(counts.get(state.value, 0) > 0 for state in _CONFLICT):
        return STATUS_CONFLICT
    quiescent = {state.value for state in _QUIESCENT}
    if any(count > 0 for value, count in counts.items() if value not in quiescent):
        return STATUS_DRIFT
    return STATUS_CLEAN
