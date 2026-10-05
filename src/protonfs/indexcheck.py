"""Re-verify the index against remote listings, without scanning the local tree (#169).

Every index entry was judged by the checks of the protonfs that wrote it. Releases have
since strengthened those checks (#147/#148 made size verification real; #144/#155
re-upload appended files), but nothing re-applied the stronger checks to entries already
written. An entry judged under a weaker check stays trusted indefinitely, including one
``offload`` has since deleted locally, where Drive holds the only copy.

:func:`check_index` compares each entry with its remote directory's listing: one listing
per directory, plaintext size always, sha1 where both sides have one. It reads no local
file, so it is practical on a host whose filesystem is too slow for ``status --remote``.
:func:`repair_index` then applies what the comparison proves, and only that:

- **a present entry whose file is still here**, and whose Drive copy is missing or
  different: the entry is dropped. The file becomes local-only, so it is never counted
  as synced or offloaded, and the next push uploads it (as a revision when the Drive
  copy is provably its own, #168);
- **a present entry whose file is gone**, with a larger Drive copy: the entry is
  rewritten to describe Drive's copy, metadata-only. These are the git-LFS
  pointer stubs that were hashed as content while Drive held the real files (#32);
- **a metadata-only entry** with a larger Drive copy: rewritten to describe it. One that
  matches, and has no sha1 yet, gains Drive's sha1 (the v1 -> v2 index migration seeded
  ``""``);
- **a Drive-only copy that is shorter, different or gone**: reported, and left exactly as
  it was. Drive is the only copy, so rewriting the entry to match would hide the loss.

Entries the listing cannot size, and directories whose listing failed, are reported and
never acted on.

.. versionadded:: 2.4.0
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum

from protonfs.context import RepoContext
from protonfs.diff import within_subpath
from protonfs.drive import DriveAuthError, DriveError, DriveThrottleError, RemoteIdentity
from protonfs.index import CHECK_LEVEL, IndexEntry


class Verdict(str, Enum):
    """How an index entry compares with its remote directory's listing.

    :cvar OK: listed, at the recorded size; the sha1 matches or one side has none.
    :cvar MISSING: nothing of that name in the listing.
    :cvar REMOTE_LARGER: Drive's copy is larger than the recorded size.
    :cvar REMOTE_SMALLER: Drive's copy is smaller than the recorded size.
    :cvar DIGEST_DIFFERS: same size, but both sides have a sha1 and they differ.
    :cvar UNSIZED: listed, but with no plaintext size, so nothing can be checked.
    :cvar UNLISTED: the directory's listing failed; see :attr:`IndexCheck.unlisted`.
    """

    OK = "ok"
    MISSING = "missing"
    REMOTE_LARGER = "remote-larger"
    REMOTE_SMALLER = "remote-smaller"
    DIGEST_DIFFERS = "digest-differs"
    UNSIZED = "unsized"
    UNLISTED = "unlisted"


# Verdicts that say Drive's copy is not the one the entry records.
MISMATCHES = frozenset(
    {Verdict.MISSING, Verdict.REMOTE_LARGER, Verdict.REMOTE_SMALLER, Verdict.DIGEST_DIFFERS}
)


@dataclass
class Finding:
    """One index entry's verdict, with the listing's identity for it (``None`` when the
    name is not listed or the listing failed)."""

    rel_path: str
    verdict: Verdict
    entry: IndexEntry
    ident: RemoteIdentity | None


@dataclass
class IndexCheck:
    """The result of :func:`check_index`.

    :ivar findings: one per index entry checked, in the order they were listed.
    :ivar unlisted: ``{remote directory: error}`` for each listing that failed.
    :ivar complete: whether every entry in the index got a verdict that settles it: the
        whole index was checked (no ``subpath``), every listing succeeded, and every
        listed copy had a plaintext size.
    """

    findings: list[Finding] = field(default_factory=list)
    unlisted: dict[str, str] = field(default_factory=dict)
    complete: bool = False

    def count(self, verdict: Verdict) -> int:
        """How many entries got ``verdict``."""
        return sum(1 for finding in self.findings if finding.verdict is verdict)


def _verdict(entry: IndexEntry, ident: RemoteIdentity | None) -> Verdict:
    if ident is None:
        return Verdict.MISSING
    if ident.claimed_size is None:
        return Verdict.UNSIZED
    if ident.claimed_size > entry.size:
        return Verdict.REMOTE_LARGER
    if ident.claimed_size < entry.size:
        return Verdict.REMOTE_SMALLER
    if ident.sha1 and entry.sha1 and ident.sha1 != entry.sha1:
        return Verdict.DIGEST_DIFFERS
    return Verdict.OK


def check_index(
    ctx: RepoContext, subpath: str | None = None, reporter=None
) -> IndexCheck:
    """Compare every index entry (under ``subpath``) with a live listing of its remote
    directory. Read-only: changes nothing, and reads no local file.

    Each remote directory is listed once. Directories holding metadata-only entries are
    listed first, since Drive is those files' only copy.

    :param ctx: the loaded repo context.
    :param subpath: repo-relative subtree to check, or ``None`` for the whole index.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter.
    :returns: an :class:`IndexCheck`.
    :raises protonfs.drive.DriveThrottleError: when Drive throttles a listing past its
        retry budget, and :class:`~protonfs.drive.DriveAuthError` when not logged in. A
        throttled or unauthenticated run is not evidence about any file. Any other
        failed listing marks that directory's entries :attr:`Verdict.UNLISTED`.
    """
    from protonfs.manifest import is_control_path
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
    by_dir: dict[str, list[tuple[str, IndexEntry]]] = {}
    for rel_path, entry in sorted(ctx.index.all().items()):
        if is_control_path(rel_path) or not within_subpath(rel_path, subpath):
            continue
        parent = entry.remote_path.rpartition("/")[0]
        by_dir.setdefault(parent, []).append((rel_path, entry))

    def drive_only_first(parent: str) -> tuple[bool, str]:
        has_offloaded = any(e.local_state == "metadata-only" for _, e in by_dir[parent])
        return (not has_offloaded, parent)

    check = IndexCheck()
    reporter.phase("checking index", entries=sum(map(len, by_dir.values())), dirs=len(by_dir))
    for done, parent in enumerate(sorted(by_dir, key=drive_only_first), 1):
        try:
            identities = ctx.drive.remote_identities(parent)
        except (DriveThrottleError, DriveAuthError):
            raise
        except DriveError as exc:
            check.unlisted[parent] = str(exc)
            reporter.warn(f"could not list {parent}: {exc}")
            identities = None
        for rel_path, entry in by_dir[parent]:
            if identities is None:
                check.findings.append(Finding(rel_path, Verdict.UNLISTED, entry, None))
                continue
            ident = identities.get(entry.remote_path.rpartition("/")[2])
            check.findings.append(Finding(rel_path, _verdict(entry, ident), entry, ident))
        reporter.progress(done, len(by_dir))
    check.complete = (
        subpath is None and not check.unlisted and not check.count(Verdict.UNSIZED)
    )
    return check


@dataclass
class IndexRepair:
    """What :func:`repair_index` changed (or, for ``suspect``, refused to).

    :ivar unindexed: present entries dropped because their file is still here and Drive's
        copy does not match; the next push uploads them.
    :ivar adopted_remote: entries rewritten to describe Drive's copy, metadata-only.
    :ivar digests_recorded: metadata-only entries that gained Drive's sha1.
    :ivar suspect: entries whose only copy is on Drive and does not match the record
        (shorter, different or gone); left untouched for a person to look at.
    """

    unindexed: list[str] = field(default_factory=list)
    adopted_remote: list[str] = field(default_factory=list)
    digests_recorded: list[str] = field(default_factory=list)
    suspect: list[str] = field(default_factory=list)


def _drive_copy(entry: IndexEntry, ident: RemoteIdentity, now: str) -> IndexEntry:
    """``entry`` rewritten to describe Drive's copy, as a metadata-only entry."""
    return IndexEntry(
        size=ident.claimed_size,
        mtime=0.0,
        sha256="",
        sha1=ident.sha1 or "",
        remote_path=entry.remote_path,
        origin_device=entry.origin_device,
        local_state="metadata-only",
        last_synced=now,
    )


def repair_index(ctx: RepoContext, check: IndexCheck) -> IndexRepair:
    """Apply to the index what ``check`` proves; see the module docstring for each rule.

    Changes are in memory until the caller saves the index.

    :param ctx: the loaded repo context.
    :param check: the :class:`IndexCheck` from :func:`check_index` on this index.
    :returns: an :class:`IndexRepair` listing every change, and every entry left alone
        because Drive's only copy does not match it.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    repair = IndexRepair()
    for finding in check.findings:
        rel, entry, ident, verdict = (
            finding.rel_path, finding.entry, finding.ident, finding.verdict,
        )
        if verdict is Verdict.OK:
            if entry.local_state == "metadata-only" and not entry.sha1 and ident.sha1:
                ctx.index.set(rel, IndexEntry(**{**entry.to_dict(), "sha1": ident.sha1}))
                repair.digests_recorded.append(rel)
            continue
        if verdict not in MISMATCHES:
            continue  # unsized or unlisted: no evidence either way
        if entry.local_state == "present" and (ctx.root / rel).is_file():
            ctx.index.remove(rel)
            repair.unindexed.append(rel)
        elif verdict is Verdict.REMOTE_LARGER:
            ctx.index.set(rel, _drive_copy(entry, ident, now))
            repair.adopted_remote.append(rel)
        else:
            repair.suspect.append(rel)
    return repair


def reverify_index(ctx: RepoContext, reporter=None) -> tuple[IndexCheck, IndexRepair]:
    """Check the whole index against Drive, apply the repairs, and save it.

    When every entry was settled (:attr:`IndexCheck.complete`), the index is recorded
    at the current :data:`~protonfs.index.CHECK_LEVEL`. Otherwise it keeps its old level,
    so the entries that could not be checked are tried again next time. ``protonfs
    upgrade`` runs this for an index below the current level; ``protonfs verify --index
    --repair`` runs it on demand. The caller holds the repo lock.

    :param ctx: the loaded repo context.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through.
    :returns: the :class:`IndexCheck` and the :class:`IndexRepair` applied from it.
    :raises protonfs.drive.DriveError: as :func:`check_index` does; nothing is changed.
    """
    check = check_index(ctx, reporter=reporter)
    repair = repair_index(ctx, check)
    if check.complete:
        ctx.index.set_check_level(CHECK_LEVEL)
    ctx.index.save()
    return check, repair


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def report_lines(check: IndexCheck, repair: IndexRepair | None = None) -> list[str]:
    """Render ``check`` (and, after a repair, ``repair``) as plain-text report lines.

    The first line counts every verdict. Each mismatch is listed after it, entries whose
    only copy is on Drive first, with the recorded and listed sizes.

    :param check: the result of :func:`check_index`.
    :param repair: the result of :func:`repair_index` on ``check``, if one ran.
    :returns: the lines, without trailing newlines.
    """
    counts = " ".join(
        f"{verdict.value}={check.count(verdict)}"
        for verdict in Verdict
        if check.count(verdict)
    )
    dirs = len({f.entry.remote_path.rpartition("/")[0] for f in check.findings})
    lines = [
        f"checked {_plural(len(check.findings), 'index entry', 'index entries')} in "
        f"{_plural(dirs, 'directory', 'directories')}: {counts}".rstrip(": ")
    ]
    mismatched = sorted(
        (f for f in check.findings if f.verdict in MISMATCHES or f.verdict is Verdict.UNSIZED),
        key=lambda f: (f.entry.local_state != "metadata-only", f.rel_path),
    )
    for finding in mismatched:
        listed = (
            "absent"
            if finding.ident is None
            else "no size"
            if finding.ident.claimed_size is None
            else f"{finding.ident.claimed_size} B"
        )
        lines.append(
            f"  {finding.verdict.value}: {finding.rel_path} [{finding.entry.local_state}]: "
            f"index {finding.entry.size} B, Drive {listed}"
        )
    if check.unlisted:
        lines.append(
            f"  could not list {_plural(len(check.unlisted), 'directory', 'directories')}; "
            "the entries in them were not checked:"
        )
        lines.extend(f"      {parent}: {error}" for parent, error in check.unlisted.items())
    if repair is None:
        if any(f.verdict in MISMATCHES for f in check.findings):
            lines.append("  -> run again with --repair to apply what this check proves")
        return lines
    entries = ("entry", "entries")
    if repair.unindexed:
        lines.append(
            f"repair: dropped {_plural(len(repair.unindexed), *entries)} whose local copy "
            "is kept; the next push uploads them again"
        )
    if repair.adopted_remote:
        lines.append(
            f"repair: rewrote {_plural(len(repair.adopted_remote), *entries)} to describe "
            "the larger copy on Drive (metadata-only; pull restores them)"
        )
    if repair.digests_recorded:
        lines.append(
            f"repair: recorded Drive's sha1 for {_plural(len(repair.digests_recorded), *entries)}"
        )
    if repair.suspect:
        lines.append(
            f"repair: left {_plural(len(repair.suspect), *entries)} untouched: Drive holds "
            "the only copy and it is shorter than recorded, different, or gone:"
        )
        lines.extend(f"      {rel}" for rel in repair.suspect)
    return lines
