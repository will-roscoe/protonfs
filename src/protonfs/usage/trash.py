"""The trash pass of ``protonfs du``: how many bytes are in the Drive trash, and where from.

A trashed *file* carries its size in the trash listing. A trashed *folder* carries none and
Drive will not size or list it, so its bytes are only knowable as the remainder of its
original parent, ``size(P) - live files - live child folders``, measured in one consistent
pass by :meth:`~protonfs.usage.engine.UsageEngine.breakdown`. Several trashed folders under
one parent cannot be told apart and are reported as one group.

:func:`run_trash_pass` produces a :class:`TrashSummary`;
:class:`~protonfs.usage.engine.UsageEngine` stores it and answers per-subtree questions with
:class:`TrashFigure` (:meth:`~protonfs.usage.engine.UsageEngine.trash_for`, or
:func:`figures_for` for many folders at once), combining the pass with any folder listed
since.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .. import drive as _drive
from ..drive import DriveError, DriveTrashListTimeoutError
from .excludes import ExcludeRules, Exclusion
from .model import Breakdown, GapKind, NodeState
from .paths import child_path, join, relative, split
from .policy import RefreshPolicy

if TYPE_CHECKING:
    from .engine import UsageEngine

#: :attr:`TrashGroup.reason` of a group whose original parent matched a ``-x``/``-X`` rule,
#: so the trash pass made no Drive calls for it.
EXCLUDED_REASON = "excluded by -x/-X, so it was not measured"

#: :attr:`TrashGroup.reason` (and ``du /``'s line) when a remainder is unknown only because a
#: folder excluded with ``-x``/``-X`` keeps a figure from an earlier pass.
FROZEN_OLDER_REASON = "a folder excluded with -x/-X keeps an older figure"
#: The same when an excluded folder has no figure at all.
FROZEN_NONE_REASON = "a folder excluded with -x/-X has no figure"
#: The same when excluded folders of both kinds are involved.
FROZEN_BOTH_REASON = "folders excluded with -x/-X keep older figures or have none"
FROZEN_REASONS = frozenset({FROZEN_OLDER_REASON, FROZEN_NONE_REASON, FROZEN_BOTH_REASON})

# Threshold 0 without the shortcut: the parent is re-listed and every child folder re-sized
# in one pass, so the remainder is measured fresh rather than from reused child sizes.
_FULL = RefreshPolicy(0.0, allow_shortcut=False)


@dataclass(frozen=True)
class TrashGroup:
    """Trashed items that shared one original parent folder.

    :ivar parent_path: escaped Drive path of the original parent, or ``None`` when it
        could not be resolved from the cache.
    :ivar files: number of trashed files in the group (``0`` for a folder group).
    :ivar folders: number of trashed folders in the group (``0`` for a file group).
    :ivar bytes: bytes in trash, or ``None`` for a folder group whose size could not be
        attributed (see ``reason``).
    :ivar reason: why ``bytes`` is ``None`` (e.g. a subfolder of the parent has no size),
        else ``None``.
    :ivar parent_uid: uid of the original parent, so a group whose parent was not cached
        (``parent_path`` ``None``) can be placed once it is; ``None`` when unknown. Not
        part of equality: two groups with the same parent, counts and bytes are equal.

    .. versionadded:: 3.4.0
    """

    parent_path: str | None
    files: int
    folders: int
    bytes: int | None
    reason: str | None = None
    parent_uid: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TrashSummary:
    """The result of one trash pass.

    :ivar at: Unix time the pass started.
    :ivar groups: file groups and folder groups, one of each per original parent.
    :ivar unresolved_folders: trashed folders whose original parent is not in the cache,
        so their bytes are unknown.
    :ivar partial: why the listing is incomplete (timeout or error text), else ``None``.
    :ivar unresolved_parents: the uids of those original parents, so a later run can tell
        when one has been listed; ``None`` for a summary stored before they were recorded.
    :ivar ended_at: Unix time the pass finished, or ``None`` for a summary stored before it
        was recorded. Only listings made after it count as newer than the pass; the
        pass's own breakdowns fall between :attr:`at` and this.
    :ivar timed_out_after: the listing timeout in seconds when ``partial`` is because the
        listing timed out, else ``None``. Such a summary is retried before it is stale
        only with a longer timeout (see
        :meth:`~protonfs.usage.engine.UsageEngine.refresh_trash`).

    .. versionadded:: 3.4.0
    """

    at: float
    groups: tuple[TrashGroup, ...]
    unresolved_folders: int
    partial: str | None
    unresolved_parents: tuple[str, ...] | None = ()
    ended_at: float | None = None
    timed_out_after: float | None = None

    @property
    def listed_after(self) -> float:
        """Listings made after this time are newer than the pass: :attr:`ended_at`, else
        :attr:`at`."""
        return self.at if self.ended_at is None else self.ended_at

    @property
    def total_known(self) -> int:
        """Bytes in trash that were measured; a lower bound unless :attr:`complete`."""
        return sum(g.bytes or 0 for g in self.groups)

    @property
    def complete(self) -> bool:
        """Whether :attr:`total_known` is the whole trash.

        ``False`` when the listing was partial, a trashed folder's parent is unresolved,
        or any group has no byte figure.
        """
        return (
            self.partial is None
            and self.unresolved_folders == 0
            and all(g.bytes is not None for g in self.groups)
        )

    def to_json(self) -> str:
        """Return the summary as JSON text, for :meth:`from_json`."""
        return json.dumps({
            "at": self.at,
            "groups": [[g.parent_path, g.files, g.folders, g.bytes, g.reason, g.parent_uid]
                       for g in self.groups],
            "unresolved_folders": self.unresolved_folders,
            "partial": self.partial,
            "unresolved_parents": (None if self.unresolved_parents is None
                                   else list(self.unresolved_parents)),
            "ended_at": self.ended_at,
            "timed_out_after": self.timed_out_after,
        })

    @classmethod
    def from_json(cls, text: str) -> TrashSummary:
        """Rebuild a summary from :meth:`to_json` text.

        :param text: JSON produced by :meth:`to_json`.
        :returns: the summary.
        :raises ValueError: if ``text`` is not a stored summary.
        """
        try:
            d = json.loads(text)
            parents = d.get("unresolved_parents")
            return cls(
                float(d["at"]),
                tuple(_group(g) for g in d["groups"]),
                int(d["unresolved_folders"]),
                d["partial"],
                None if parents is None else tuple(str(u) for u in parents),
                None if d.get("ended_at") is None else float(d["ended_at"]),
                None if d.get("timed_out_after") is None else float(d["timed_out_after"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"not a trash summary: {exc}") from exc


def _group(item: list) -> TrashGroup:
    """Rebuild one stored group; older summaries have no ``reason`` or ``parent_uid``."""
    parent, files, folders, size = item[:4]
    reason = item[4] if len(item) > 4 else None
    uid = item[5] if len(item) > 5 else None
    return TrashGroup(parent, int(files), int(folders), size, reason, uid)


@dataclass(frozen=True)
class TrashFigure:
    """Trash bytes attributed to one subtree.

    :ivar known: measured bytes in trash whose original parent is within the subtree; a
        lower bound unless ``complete``.
    :ivar complete: whether ``known`` is the whole answer.
    :ivar unchecked_bytes: when not ``complete``, an upper bound on the trash ``known``
        leaves out: the bytes of the folders whose trash is unknown; ``None`` when
        complete, or when no bound exists (a partial trash listing, or trashed items whose
        original folder is not in the cache, which could be anywhere).
    :ivar unchecked_share: ``unchecked_bytes`` as a fraction of the subtree's size (at
        most 1: the bound is capped at that size).
    :ivar at: Unix time of the oldest measurement behind ``known``: the trash pass, or,
        when every folder in the subtree was listed after it, the oldest of those
        listings; ``None`` without a stored pass.
    :ivar unknown: ``(folder, reason)`` for each folder under the subtree whose trash is
        unknown, and ``(None, reason)`` when trashed files of an unknown folder may already
        be counted (``known`` then counts only the larger of the two figures). Empty when
        ``complete``; the summary's own partial listing or unresolved folders are not
        repeated here.

    .. versionadded:: 3.4.0
    """

    known: int
    complete: bool
    unchecked_bytes: int | None
    unchecked_share: float | None
    at: float | None
    unknown: tuple[tuple[str | None, str], ...] = ()


def trash_list_timeout() -> float:
    """Return the trash listing timeout now in effect, in seconds.

    That is :data:`~protonfs.drive.TRASH_LIST_TIMEOUT_SECONDS`
    (:envvar:`PROTONFS_TRASH_LIST_TIMEOUT`, default 900), read when called.

    .. versionadded:: 3.4.0
    """
    return float(_drive.TRASH_LIST_TIMEOUT_SECONDS)


def run_trash_pass(
    engine: UsageEngine,
    policy: RefreshPolicy,
    *,
    timeout: float | None = None,
    rules: ExcludeRules | None = None,
) -> TrashSummary:
    """List the trash and attribute its bytes to original parent folders.

    Files take their listed size. For each parent holding trashed folders, one
    :meth:`~protonfs.usage.engine.UsageEngine.breakdown` that re-sizes everything gives
    the parent's remainder; when it is labelled ``in_trash``, or is exactly ``0`` with
    every input from that one pass, the folders' bytes are that remainder minus the
    trashed files of the same parent. Otherwise the group's ``bytes`` is ``None`` with a
    ``reason``. Parents are resolved with
    :meth:`~protonfs.usage.cache.UsageCache.path_for_uid`, so they must have been listed.
    A parent that ``rules`` exclude gets no calls at all (its group's ``reason`` is
    :data:`EXCLUDED_REASON`), and the breakdowns size no excluded child either.

    Issues ``filesystem list /trash`` plus the breakdown calls; stores nothing itself.

    :param engine: the engine to measure with.
    :param policy: ``policy.never`` makes no calls and returns an empty partial summary.
    :param rules: freeze/hide rules relative to ``/`` (none by default).
    :param timeout: seconds allowed for the trash listing, or ``None`` for
        :func:`trash_list_timeout`.
    :returns: the summary. A timed-out or failed listing gives ``partial`` set (the error
        text, which for a timeout says how to wait longer) and only what could be
        measured; it never looks complete. A timeout also sets ``timed_out_after``.

    .. versionadded:: 3.4.0
    """
    now = engine.clock()
    rules = rules if rules is not None else ExcludeRules()
    if policy.never:
        return TrashSummary(now, (), 0, "trash not refreshed (refresh policy is never)",
                            (), now)
    limit = trash_list_timeout() if timeout is None else timeout
    try:
        entries = engine.source.list_trash(timeout=limit)
    except subprocess.TimeoutExpired as exc:
        return TrashSummary(now, (), 0, f"trash listing timed out after {exc.timeout:.0f} s",
                            (), engine.clock(), float(exc.timeout))
    except DriveTrashListTimeoutError as exc:
        return TrashSummary(now, (), 0, str(exc), (), engine.clock(), limit)
    except DriveError as exc:
        return TrashSummary(now, (), 0, str(exc) or type(exc).__name__, (), engine.clock())

    files: dict[str | None, list[int]] = {}
    folders: dict[str | None, int] = {}
    for e in entries:
        if e.kind == "file":
            slot = files.setdefault(e.parent_uid, [0, 0])
            slot[0] += 1
            slot[1] += e.size or 0
        else:
            folders[e.parent_uid] = folders.get(e.parent_uid, 0) + 1

    groups: list[TrashGroup] = []
    unresolved = 0
    unresolved_uids: list[str] = []
    paths_by_uid: dict[str | None, str | None] = {}
    for uid in {*files, *folders}:
        paths_by_uid[uid] = engine.cache.path_for_uid(uid) if uid is not None else None
    for uid, (count, size) in files.items():
        groups.append(TrashGroup(paths_by_uid[uid], count, 0, size, None, uid))
    for uid, count in folders.items():
        parent = paths_by_uid[uid]
        if parent is None:
            unresolved += count
            if uid is not None:
                unresolved_uids.append(uid)
            continue
        if excluded(rules, parent):
            groups.append(TrashGroup(parent, 0, count, None, EXCLUDED_REASON, uid))
            continue
        file_bytes = files[uid][1] if uid in files else 0
        bd = engine.breakdown(parent, _FULL, rules, target="/")
        folder_bytes, reason = _folder_bytes(engine, bd, file_bytes, rules)
        groups.append(TrashGroup(parent, 0, count, folder_bytes, reason, uid))
    groups.sort(key=lambda g: (g.parent_path or "", g.folders))
    return TrashSummary(now, tuple(groups), unresolved, None, tuple(sorted(unresolved_uids)),
                        engine.clock())


def excluded(rules: ExcludeRules, path: str) -> bool:
    """Return whether ``rules`` (relative to ``/``) freeze or hide the folder ``path``.

    :param rules: freeze/hide rules relative to ``/``.
    :param path: escaped Drive path of a folder.
    :returns: ``True`` for ``FREEZE`` or ``HIDE``, inherited from an ancestor included.

    .. versionadded:: 3.4.0
    """
    return rules.match(relative(path, "/"), True) is not Exclusion.NONE


def frozen_reason(engine: UsageEngine, path: str, rules: ExcludeRules) -> str | None:
    """Return why the cached remainder of ``path`` is unknown, when that is only because
    folders ``rules`` exclude keep an older figure or have none; else ``None``.

    Reads the cache only. The remainder is then the user's own exclusion: every other
    input (the folder's own figure, its files, every child folder ``rules`` do not
    exclude) has a size from the listing's own pass.

    :param engine: the engine holding the cache.
    :param path: escaped Drive path of a listed folder.
    :param rules: freeze/hide rules relative to ``/``.
    :returns: :data:`FROZEN_OLDER_REASON`, :data:`FROZEN_NONE_REASON` or
        :data:`FROZEN_BOTH_REASON`; ``None`` without a listing, when nothing excluded is
        out of step, or when anything else is.

    .. versionadded:: 3.4.0
    """
    snap = engine.cache.snapshot(path)
    if snap is None:
        return None
    fig = engine.cache.figure(path)
    if fig is not None and (fig.size, fig.descendants) != tuple(snap.fingerprint):
        return None
    older = none = False
    for r in snap.rows:
        if r.size is not None and r.pass_id == snap.pass_id:
            continue
        if r.kind != "folder" or not excluded(rules, child_path(path, r.name, r.uid,
                                                                  r.decrypted)):
            return None
        if r.size is None:
            none = True
        else:
            older = True
    if older and none:
        return FROZEN_BOTH_REASON
    return FROZEN_OLDER_REASON if older else FROZEN_NONE_REASON if none else None


def _folder_bytes(
    engine: UsageEngine, bd: Breakdown, file_bytes: int, rules: ExcludeRules
) -> tuple[int | None, str | None]:
    """Return ``(bytes, reason)`` for the trashed folders whose original parent is ``bd``.

    ``bytes`` is ``bd``'s remainder minus the parent's trashed ``file_bytes`` when the
    remainder is trash; else ``None`` and ``reason`` says why.
    """
    if bd.state is NodeState.ERROR:
        return None, f"measuring the folder failed: {bd.error or 'unknown error'}"
    if bd.gap is None:
        return None, "the folder could not be listed"
    trash = bd.gap_kind is GapKind.TRASH or (bd.gap == 0 and _one_pass(engine, bd.path))
    if not trash or bd.gap < 0:
        frozen = frozen_reason(engine, bd.path, rules)
        if frozen is not None:
            return None, frozen
    if bd.gap_kind is GapKind.UNATTRIBUTED:
        return None, "a subfolder has no size, so the remainder includes its bytes"
    if bd.gap < 0:
        return None, "the folder changed while it was measured"
    if not trash:
        return None, "its figures come from different passes"
    if bd.gap < file_bytes:
        return None, "the remainder is smaller than the trashed files in the folder"
    return bd.gap - file_bytes, None


def _one_pass(engine: UsageEngine, path: str) -> bool:
    """Whether the cached breakdown of ``path`` has every figure from its own pass."""
    snap = engine.cache.snapshot(path)
    if snap is None:
        return False
    fig = engine.cache.figure(path)
    if fig is not None and (fig.size, fig.descendants) != tuple(snap.fingerprint):
        return False
    return all(r.pass_id == snap.pass_id for r in snap.rows if r.size is not None)


def figure_for(engine: UsageEngine, path: str) -> TrashFigure:
    """Return the trash bytes whose original parent is within ``path``.

    Reads the stored summary and cache only; makes no Drive calls. With a stored trash
    pass, the figure sums, for each folder ``D`` under ``path``, the trash whose original
    parent is ``D``: from ``D``'s cached listing when that listing was made after the pass
    ended (:attr:`TrashSummary.listed_after`; its remainder: ``in_trash`` counts, a zero
    remainder is 0, any other label leaves it unknown), else from the pass's groups for
    ``D``. A folder whose newer-listed parent no
    longer lists it is not counted again, since that parent's remainder holds it. When
    incomplete, ``unchecked_bytes`` bounds what is missing (see :class:`TrashFigure`).

    Without a stored pass, ``known`` is the sum of the ``in_trash`` remainders of the
    cached listings under ``path``, ``complete`` is ``False``, ``unchecked_bytes`` is the
    size of the folders under ``path`` with no listing of their own, and ``at`` is
    ``None``.

    .. seealso:: :func:`figures_for` for many paths at once.

    :param engine: the engine holding the cache.
    :param path: escaped Drive path, or ``"/"`` for the whole account.
    :returns: the figure.

    .. versionadded:: 3.4.0
    """
    return figures_for(engine, [path])[path]


def figures_for(engine: UsageEngine, paths: Sequence[str]) -> dict[str, TrashFigure]:
    """Return :func:`figure_for` of each path, reading the cache once for all of them.

    The listings made since the trash pass are read in bulk under the paths' common
    ancestor, so a report of many rows costs one read, not one walk per row.

    :param engine: the engine holding the cache.
    :param paths: escaped Drive paths (``"/"`` for the whole account).
    :returns: ``{path: figure}`` for every path given.

    .. versionadded:: 3.4.0
    """
    summary = engine.trash_summary()
    if summary is None:
        return {p: _figure_without_pass(engine, p) for p in paths}
    return _PassFigures(engine, summary, _common_base(paths)).figures(paths)


_NEVER = RefreshPolicy(None)


def _under(path: str, base: str) -> bool:
    """Whether the canonical escaped ``path`` is ``base`` or below it."""
    return base == "/" or path == base or path.startswith(base + "/")


def _chain(path: str) -> list[str]:
    """The paths from the top segment down to ``path`` itself; ``chain[i]`` has depth i+1."""
    out = []
    cur = "/"
    for seg in split(path):
        cur = join(cur, seg)
        out.append(cur)
    return out


def _common_base(paths: Sequence[str]) -> str:
    common: list[str] | None = None
    for p in paths:
        segs = split(p)
        if common is None:
            common = segs
        else:
            n = 0
            while n < min(len(common), len(segs)) and common[n] == segs[n]:
                n += 1
            common = common[:n]
    base = "/"
    for seg in common or []:
        base = join(base, seg)
    return base


_LISTED_UNATTRIBUTED = "its listing has a subfolder with no size"
_LISTED_UNRECONCILED = ("its listing's remainder is unreconciled: figures from different "
                        "passes, or it changed while measured")


def loose_file_groups(engine: UsageEngine, summary: TrashSummary) -> list[TrashGroup]:
    """Return the file groups of ``summary`` whose original folder is still not cached.

    A file group stored without a path is placed through
    :meth:`~protonfs.usage.cache.UsageCache.path_for_uid` once its folder is cached; the
    ones returned could not be.

    :param engine: the engine holding the cache.
    :param summary: the stored summary.
    :returns: the groups with neither a stored nor a cached path.

    .. versionadded:: 3.4.0
    """
    return [g for g in summary.groups
            if g.parent_path is None and _placed(engine, g) is None]


def _placed(engine: UsageEngine, group: TrashGroup) -> str | None:
    if group.parent_path is not None:
        return group.parent_path
    return engine.cache.path_for_uid(group.parent_uid) if group.parent_uid else None


class _PassFigures:
    """Trash figures under ``base`` from one stored pass plus the listings made since."""

    def __init__(self, engine: UsageEngine, summary: TrashSummary, base: str) -> None:
        self.engine = engine
        self.summary = summary
        # Listings the pass made itself are not newer than it: they back its own verdict.
        self.newer = engine.cache.snapshots_newer_than(summary.listed_after, under=base)
        self.children = {
            p: [child_path(p, r.name, r.uid, r.decrypted) for r in snap.rows
                if r.kind == "folder"]
            for p, snap in self.newer.items()
        }
        listed = {p: set(c) for p, c in self.children.items()}
        # folder -> (trash directly under it by the pass, or None; why unknown)
        from_pass: dict[str, tuple[int | None, str | None]] = {}
        self.loose = 0  # trashed files whose folder is still unknown: could be anywhere
        self.loose_files = 0
        for g in summary.groups:
            parent = _placed(engine, g)  # a file group placed once its folder is cached
            if parent is None:
                self.loose += g.bytes or 0
                self.loose_files += g.files
            elif _under(parent, base):
                prev, why = from_pass.get(parent, (0, None))
                from_pass[parent] = (None if prev is None or g.bytes is None
                                     else prev + g.bytes, why or g.reason)
        # (folder, its chain, trash directly under it or None, why unknown, whether it
        # comes from a listing since the pass, depth of the deepest ancestor listed since
        # the pass that no longer lists the way down, or -1)
        self.direct: list[tuple[str, list[str], int | None, str | None, bool, int]] = []
        for folder in {*from_pass, *self.newer}:
            chain = _chain(folder)
            cover = -1
            for depth, (a, b) in enumerate(zip(chain, chain[1:]), start=1):
                if a in listed and b not in listed[a]:
                    cover = depth
            listed_since = folder in self.newer
            value, why = self._listed(folder) if listed_since else from_pass[folder]
            self.direct.append((folder, chain, value, why, listed_since, cover))
        self._since: dict[str, float | None] = {}

    def _listed(self, folder: str) -> tuple[int | None, str | None]:
        """Trash directly under ``folder`` from its newer listing's remainder, or why not."""
        snap = self.newer[folder]
        if snap.invalid:  # a later figure may contradict it: let the breakdown decide
            bd = self.engine.breakdown(folder, _NEVER, ExcludeRules(), target=folder)
            gap, kind = bd.gap, bd.gap_kind
        else:  # valid: its own figure equals the fingerprint, as the breakdown assumes
            from .engine import _label  # the breakdown's own labelling rule

            size = snap.fingerprint[0]
            known = sum(r.size for r in snap.rows if r.size is not None)
            holes = any(r.size is None for r in snap.rows)
            mixed = any(r.pass_id != snap.pass_id for r in snap.rows)
            gap = size - known
            kind = _label(gap, holes, mixed)
        if gap is not None and kind is GapKind.TRASH:
            return gap, None
        if gap is not None and kind is GapKind.NONE:
            return 0, None
        if kind is GapKind.UNATTRIBUTED:
            return None, _LISTED_UNATTRIBUTED
        return None, _LISTED_UNRECONCILED

    def _listed_since_at(self, root: str) -> float | None:
        """The oldest listing under ``root`` if every folder there was listed since the
        pass, else ``None``."""
        memo = self._since
        stack = [(root, False)]
        while stack:
            p, ready = stack.pop()
            if p in memo:
                continue
            kids = self.children[p]
            if not ready:
                stack.append((p, True))
                stack.extend((c, False) for c in kids if c in self.newer and c not in memo)
                continue
            oldest: float | None = self.newer[p].at
            for c in kids:
                sub = memo.get(c) if c in self.newer else None
                if sub is None or oldest is None:
                    oldest = None
                    break
                oldest = min(oldest, sub)
            memo[p] = oldest
        return memo[root]

    def figures(self, paths: Sequence[str]) -> dict[str, TrashFigure]:
        known = dict.fromkeys(paths, 0)
        unknown: dict[str, list[tuple[str | None, str]]] = {p: [] for p in paths}
        # whether / counts a listing since the pass with trash in it, which could already
        # hold a loose file (its folder trashed and an ancestor re-listed since)
        overlap = False
        for folder, chain, value, why, listed_since, cover in self.direct:
            # the folder counts for a row above its covering ancestor, not at or below it
            hits = [p for depth, p in enumerate(chain, start=1) if depth > cover and p in known]
            if cover < 0 and "/" in known:
                hits.append("/")
                overlap = overlap or (listed_since and bool(value))
            for p in hits:
                if value is None:
                    unknown[p].append((folder, why or "its trash could not be measured"))
                else:
                    known[p] += value
        s = self.summary
        out = {}
        for p in paths:
            whole = p == "/"
            total = known[p]
            if whole and self.loose:
                if overlap:
                    # either may hold the other: only the larger is a lower bound
                    total = max(total, self.loose)
                    files = "file" if self.loose_files == 1 else "files"
                    unknown[p].append((None, (
                        f"{self.loose_files} trashed {files} whose folder is not cached may "
                        "already be counted in a folder listed since the last trash pass")))
                else:
                    total += self.loose
            since = None if whole or p not in self.newer else self._listed_since_at(p)
            at = s.at if since is None else since
            unbounded = (s.partial is not None or s.unresolved_folders > 0
                         or (self.loose_files > 0 and not whole))
            if not unknown[p] and not unbounded:
                out[p] = TrashFigure(total, True, None, None, at)
                continue
            folders = [f for f, _ in unknown[p] if f is not None]
            unchecked = None if unbounded or len(folders) < len(unknown[p]) \
                else _unknown_bytes(self.engine, folders)
            size = self.engine.used_total() if whole else _size(self.engine, p)
            if unchecked is not None and size is not None:
                # the folders behind the bound may have been measured after this one
                unchecked = min(unchecked, size)
            share = unchecked / size if unchecked is not None and size else None
            out[p] = TrashFigure(total, False, unchecked, share, at, tuple(unknown[p]))
        return out


def _unknown_bytes(engine: UsageEngine, folders: list[str]) -> int | None:
    """Sum the sizes of ``folders``, counting a folder inside another only once."""
    total = 0
    kept: list[str] = []
    for folder in sorted(folders, key=lambda f: len(split(f))):
        if any(_under(folder, k) for k in kept):
            continue
        kept.append(folder)
        size = _size(engine, folder)
        if size is None:
            return None
        total += size
    return total


def _figure_without_pass(engine: UsageEngine, path: str) -> TrashFigure:
    known = _cached_gaps(engine, path)
    unchecked, size = _unchecked(engine, path)
    if unchecked is None or size is None:
        return TrashFigure(known, False, None, None, None)
    return TrashFigure(known, False, unchecked, unchecked / size if size else None, None)


def _roots(engine: UsageEngine, path: str) -> list[str]:
    if path != "/":
        return [path]
    return ["/my-files", *(engine.device_paths() or [])]


def _unchecked(engine: UsageEngine, path: str) -> tuple[int | None, int | None]:
    """Return ``(bytes of sized-only folders under path, size of path)``.

    The first item is ``None`` when any such folder has no known size.
    """
    size = engine.used_total() if path == "/" else _size(engine, path)
    total = 0
    unknown = False
    stack = _roots(engine, path)
    while stack:
        p = stack.pop()
        snap = engine.cache.snapshot(p)
        if snap is None or snap.invalid:
            fig = engine.cache.figure(p)
            if fig is not None and fig.size is not None:
                total += fig.size
            else:
                unknown = True
            continue
        for row in snap.rows:
            if row.kind == "file":
                continue
            child = child_path(p, row.name, row.uid, row.decrypted)
            if not engine.cache.has_snapshot(child):
                if row.size is None:
                    unknown = True
                else:
                    total += row.size
            else:
                stack.append(child)
    return (None if unknown else total), size


def _size(engine: UsageEngine, path: str) -> int | None:
    fig = engine.cache.figure(path)
    return fig.size if fig is not None else None


def _cached_gaps(engine: UsageEngine, path: str) -> int:
    """Sum the ``in_trash`` remainders of the valid cached listings under ``path``."""
    never = RefreshPolicy(None)
    known = 0
    stack = _roots(engine, path)
    while stack:
        p = stack.pop()
        snap = engine.cache.snapshot(p)
        if snap is None or snap.invalid:
            continue
        bd = engine.breakdown(p, never, ExcludeRules(), target=p)
        if bd.gap_kind is GapKind.TRASH and bd.gap is not None:
            known += bd.gap
        stack.extend(
            child_path(p, r.name, r.uid, r.decrypted) for r in snap.rows if r.kind == "folder"
        )
    return known
