"""The trash pass of ``protonfs du``: how many bytes are in the Drive trash, and where from.

A trashed *file* carries its size in the trash listing. A trashed *folder* carries none and
Drive will not size or list it, so its bytes are only knowable as the remainder of its
original parent, ``size(P) - live files - live child folders``, measured in one consistent
pass by :meth:`~protonfs.usage.engine.UsageEngine.breakdown`. Several trashed folders under
one parent cannot be told apart and are reported as one group.

:func:`run_trash_pass` produces a :class:`TrashSummary`;
:class:`~protonfs.usage.engine.UsageEngine` stores it and answers per-subtree questions with
:class:`TrashFigure` (:meth:`~protonfs.usage.engine.UsageEngine.trash_for`).

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..drive import DriveError
from .excludes import ExcludeRules
from .model import GapKind
from .paths import is_within
from .policy import RefreshPolicy

if TYPE_CHECKING:
    from .engine import UsageEngine

#: Seconds allowed for the ``filesystem list /trash`` call.
DEFAULT_TIMEOUT = 300.0

# Threshold 0 without the fingerprint shortcut: trashing a node leaves every ancestor's
# (size, descendants) unchanged, so a restamped listing would still show the trashed node as
# live and hide its bytes from the remainder.
_FULL = RefreshPolicy(0.0, allow_shortcut=False)


@dataclass(frozen=True)
class TrashGroup:
    """Trashed items that shared one original parent folder.

    :ivar parent_path: escaped Drive path of the original parent, or ``None`` when it
        could not be resolved from the cache.
    :ivar files: number of trashed files in the group (``0`` for a folder group).
    :ivar folders: number of trashed folders in the group (``0`` for a file group).
    :ivar bytes: bytes in trash, or ``None`` for a folder group whose size could not be
        attributed (the parent's remainder was not labelled ``in_trash``).

    .. versionadded:: 3.4.0
    """

    parent_path: str | None
    files: int
    folders: int
    bytes: int | None


@dataclass(frozen=True)
class TrashSummary:
    """The result of one trash pass.

    :ivar at: Unix time the pass started.
    :ivar groups: file groups and folder groups, one of each per original parent.
    :ivar unresolved_folders: trashed folders whose original parent is not in the cache,
        so their bytes are unknown.
    :ivar partial: why the listing is incomplete (timeout or error text), else ``None``.

    .. versionadded:: 3.4.0
    """

    at: float
    groups: tuple[TrashGroup, ...]
    unresolved_folders: int
    partial: str | None

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
            "groups": [[g.parent_path, g.files, g.folders, g.bytes] for g in self.groups],
            "unresolved_folders": self.unresolved_folders,
            "partial": self.partial,
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
            return cls(
                float(d["at"]),
                tuple(TrashGroup(p, int(f), int(o), b) for p, f, o, b in d["groups"]),
                int(d["unresolved_folders"]),
                d["partial"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a trash summary: {exc}") from exc


@dataclass(frozen=True)
class TrashFigure:
    """Trash bytes attributed to one subtree.

    :ivar known: measured bytes in trash whose original parent is within the subtree; a
        lower bound unless ``complete``.
    :ivar complete: whether ``known`` is the whole answer.
    :ivar unchecked_bytes: when not ``complete``, the bytes of folders under the subtree
        that were never broken down (sized only) and so may hold more trash; ``None``
        when complete or the subtree size is unknown.
    :ivar unchecked_share: ``unchecked_bytes`` as a fraction of the subtree's size.
    :ivar at: Unix time of the trash pass behind ``known``, or ``None`` without one.

    .. versionadded:: 3.4.0
    """

    known: int
    complete: bool
    unchecked_bytes: int | None
    unchecked_share: float | None
    at: float | None


def run_trash_pass(
    engine: UsageEngine, policy: RefreshPolicy, *, timeout: float = DEFAULT_TIMEOUT
) -> TrashSummary:
    """List the trash and attribute its bytes to original parent folders.

    Files take their listed size. For each parent holding trashed folders, one
    :meth:`~protonfs.usage.engine.UsageEngine.breakdown` with a revalidating policy gives
    the parent's remainder; when it is labelled ``in_trash`` the folders' bytes are that
    remainder minus the trashed files of the same parent. Parents are resolved with
    :meth:`~protonfs.usage.cache.UsageCache.path_for_uid`, so they must have been listed.

    Issues ``filesystem list /trash`` plus the breakdown calls; stores nothing itself.

    :param engine: the engine to measure with.
    :param policy: ``policy.never`` makes no calls and returns an empty partial summary.
    :param timeout: seconds allowed for the trash listing.
    :returns: the summary. A timed-out or failed listing gives ``partial`` set and only
        what could be measured; it never looks complete.
    """
    now = engine.clock()
    if policy.never:
        return TrashSummary(now, (), 0, "trash not refreshed (refresh policy is never)")
    try:
        entries = engine.source.list_trash(timeout=timeout)
    except subprocess.TimeoutExpired:
        return TrashSummary(now, (), 0, f"trash listing timed out after {timeout:.0f} s")
    except DriveError as exc:
        return TrashSummary(now, (), 0, str(exc) or type(exc).__name__)

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
    paths_by_uid: dict[str | None, str | None] = {}
    for uid in {*files, *folders}:
        paths_by_uid[uid] = engine.cache.path_for_uid(uid) if uid is not None else None
    for uid, (count, size) in files.items():
        groups.append(TrashGroup(paths_by_uid[uid], count, 0, size))
    for uid, count in folders.items():
        parent = paths_by_uid[uid]
        if parent is None:
            unresolved += count
            continue
        file_bytes = files[uid][1] if uid in files else 0
        bd = engine.breakdown(parent, _FULL, ExcludeRules(), target=parent)
        usable = bd.gap_kind is GapKind.TRASH and bd.gap is not None
        folder_bytes = bd.gap - file_bytes if usable and bd.gap is not None else None
        if folder_bytes is not None and folder_bytes < 0:
            folder_bytes = None
        groups.append(TrashGroup(parent, 0, count, folder_bytes))
    groups.sort(key=lambda g: (g.parent_path or "", g.folders))
    return TrashSummary(now, tuple(groups), unresolved, None)


def figure_for(engine: UsageEngine, path: str) -> TrashFigure:
    """Return the trash bytes whose original parent is within ``path``.

    Reads the stored summary and cache only; makes no Drive calls. Without a stored
    summary, ``known`` is the sum of the ``in_trash`` remainders of the cached listings
    under ``path`` and ``complete`` is ``False``. When incomplete, ``unchecked_bytes`` is
    the size of the folders under ``path`` that have no listing of their own, which is where
    unseen trash could be.

    :param engine: the engine holding the cache.
    :param path: escaped Drive path, or ``"/"`` for the whole account.
    :returns: the figure.
    """
    stored = engine.cache.trash()
    summary: TrashSummary | None = None
    if stored is not None:
        try:
            summary = TrashSummary.from_json(stored[0])
        except ValueError:
            summary = None
    unchecked, size = _unchecked(engine, path)
    if summary is not None:
        known = sum(
            g.bytes or 0
            for g in summary.groups
            if (g.parent_path is None and path == "/")
            or (g.parent_path is not None and is_within(g.parent_path, path))
        )
        # A group with an unknown parent might belong anywhere below a narrower path.
        complete = summary.complete and (path == "/" or all(
            g.parent_path is not None for g in summary.groups))
        at: float | None = stored[1] if stored is not None else None
    else:
        known = _cached_gaps(engine, path)
        complete = False
        at = None
    if complete:
        return TrashFigure(known, True, None, None, at)
    if unchecked is None or size is None:
        return TrashFigure(known, False, None, None, at)
    return TrashFigure(known, False, unchecked, unchecked / size if size else None, at)


def _roots(engine: UsageEngine, path: str) -> list[str]:
    if path != "/":
        return [path]
    from .engine import _segment
    from .paths import join

    out = ["/my-files"]
    for name, uid, decrypted in engine._cached_devices() or []:
        out.append(join("/devices", _segment(name, uid, decrypted)))
    return out


def _unchecked(engine: UsageEngine, path: str) -> tuple[int | None, int | None]:
    """Return ``(bytes of sized-only folders under path, size of path)``.

    The first item is ``None`` when any such folder has no known size.
    """
    from .engine import _row_path

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
            child = _row_path(p, row)
            if engine.cache.snapshot(child) is None:
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
    from .engine import _row_path

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
        stack.extend(_row_path(p, r) for r in snap.rows if r.kind == "folder")
    return known
