"""The ``du`` engine: consistent-set folder breakdowns over a cached Drive tree.

:class:`UsageEngine` answers "what takes the space under this folder" from a
:class:`~protonfs.usage.source.DriveSource` and a :class:`~protonfs.usage.cache.UsageCache`.
Its main entry points are :meth:`UsageEngine.breakdown` (one folder),
:meth:`UsageEngine.tree` (to a depth), :meth:`UsageEngine.walk` (a whole subtree) and
:meth:`UsageEngine.root` (the account view).

A breakdown is refreshed as a unit: the folder's ``size`` and listing are fetched in one
pass, with every child folder's ``size`` (or, when nothing about the folder's direct
children changed, the previous pass's child sizes), and all of them share that pass's id.
The remainder ``size - Σ children`` is labelled ``in trash`` only when every input comes
from that pass and every counted child has a figure (see
:class:`~protonfs.usage.model.GapKind`). Call :meth:`UsageEngine.check_account` once
before answering, so a cache written for another account is never shown.

.. code-block:: python

    engine = UsageEngine(CliDriveSource(client), UsageCache(default_cache_path()))
    policy = RefreshPolicy.parse("5d")
    engine.check_account(policy)
    bd = engine.breakdown("/my-files/sph-dev", policy, ExcludeRules(),
                          target="/my-files/sph-dev")
    print(bd.size, bd.gap, bd.gap_kind)

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Callable

from ..drive import DriveError, SizeResult
from .cache import Figure, Row, Snapshot, UsageCache
from .excludes import ExcludeRules, Exclusion
from .model import Breakdown, Entry, GapKind, NodeState
from .paths import (
    DU_SECTIONS,
    child_path,
    is_within,
    node_segment,
    parent_of,
    relative,
    split,
)
from .policy import RefreshPolicy
from .source import DriveSource, RawEntry
from .trash import TrashFigure, TrashSummary, figure_for, run_trash_pass

_SCOPE_KEY = "scope"
_DEVICES_KEY = "devices"
_DEVICES_AT_KEY = "devices_at"
_CACHE_ONLY = RefreshPolicy(None)

#: Drive path of the photo library, which ``filesystem size`` cannot measure.
PHOTOS_PATH = "/photos"


def _error_text(exc: BaseException) -> str:
    text = str(exc)
    if isinstance(exc, DriveError):
        return text or type(exc).__name__
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _row_path(parent: str, row: Row) -> str:
    return child_path(parent, row.name, row.uid, row.decrypted)


def _measurable(path: str) -> bool:
    segs = split(path)
    return bool(segs) and segs[0] in DU_SECTIONS


def _unmeasured(path: str) -> Breakdown:
    return Breakdown(path, None, None, None, NodeState.UNMEASURED, (), None, GapKind.NONE,
                     None, None)


def _label(gap: int, holes: bool, mixed: bool) -> GapKind:
    if gap == 0:
        return GapKind.NONE
    if gap < 0:
        return GapKind.UNRECONCILED
    if holes:
        return GapKind.UNATTRIBUTED
    if mixed:
        return GapKind.UNRECONCILED
    return GapKind.TRASH


class UsageEngine:
    """Measures Drive folders through a source, caching every figure with its age.

    Source calls for one breakdown's child folders run in parallel, up to
    ``source.concurrency`` at a time; breakdowns themselves run one after another.

    :param source: the Drive to read from.
    :param cache: where figures and snapshots are stored.
    :param clock: returns the current Unix time; every figure is stamped with it.
    :param on_call: called as ``on_call(op, path)`` just before each source call, with
        ``op`` one of ``"size"`` or ``"list"`` and ``path`` the escaped Drive path. It may
        be called from worker threads.

    Each argument is kept as the public attribute of the same name; ``on_call`` may be
    replaced after construction.

    .. versionadded:: 3.4.0
    """

    def __init__(
        self,
        source: DriveSource,
        cache: UsageCache,
        *,
        clock: Callable[[], float] = time.time,
        on_call: Callable[[str, str], None] | None = None,
    ) -> None:
        self.source = source
        self.cache = cache
        self.clock = clock
        self.on_call = on_call
        self._lock = threading.Lock()
        self._scope_checked = False
        self._errors: dict[str, str] = {}

    # ----------------------------------------------------------------- public API

    def breakdown(
        self, path: str, policy: RefreshPolicy, rules: ExcludeRules, target: str
    ) -> Breakdown:
        """Return ``path`` split into its listed children plus a labelled remainder.

        Uses the cached snapshot when it is fresh under ``policy`` (or ``policy.never``);
        otherwise runs one pass: ``size(path)``, a fresh listing, and ``size`` of every
        child folder that ``rules`` do not exclude. The child-folder sizes of the cached
        snapshot are reused instead (re-stamped into the pass) only when
        ``policy.allow_shortcut``, the snapshot is valid, the ``(size, descendants)``
        fingerprint of ``path`` is unchanged, and the fresh listing has the same
        child-folder uids and the same summed file bytes; a stale unchanged folder then
        costs one ``size`` and one ``list``. Excluded folders get no Drive calls; their
        cached figure, if any, is shown. ``"/"`` returns :meth:`root`; a path outside
        ``/my-files`` and ``/devices`` is ``UNMEASURED`` with no calls; a path that
        ``rules`` exclude is answered from the cache with state ``EXCLUDED``.

        Side effects: writes the snapshot of ``path`` (atomically) and the figures of
        ``path`` and each sized child, and invalidates any cached snapshot those new
        figures contradict (a child's own, or ``path``'s parent's). On the first listing
        whose volume scope differs from the cached one, the whole cache is cleared and
        the breakdown restarts once.

        :param path: escaped Drive path of the folder.
        :param policy: how old a cached figure may be.
        :param rules: freeze/hide rules, matched against paths relative to ``target``.
        :param target: escaped Drive path the rules are relative to; must contain ``path``.
        :returns: the breakdown. A failed ``size`` or ``list`` of ``path`` itself gives
            state ``ERROR`` with the error text; a failed child ``size`` gives that entry
            state ``ERROR`` and leaves the rest of the breakdown intact.
        :raises ValueError: if ``path`` is not within ``target``.

        .. versionadded:: 3.4.0
        """
        if path == "/":
            return self.root(policy, rules)
        if not is_within(path, target):
            raise ValueError(f"{path!r} is not within the du target {target!r}")
        return self._breakdown(path, policy, rules, target, restarted=False)

    def figure(self, path: str, policy: RefreshPolicy) -> Entry:
        """Return the size figure of the folder ``path``, re-measuring it only when stale.

        A stale or missing figure costs one ``size`` call (none under ``policy.never``).
        A new figure that differs from the cached snapshot of ``path``, or from the row
        for ``path`` in its parent's snapshot, invalidates that snapshot.

        :param path: escaped Drive path of the folder.
        :param policy: how old the cached figure may be.
        :returns: an entry of kind ``"folder"`` with state ``LISTED`` (a snapshot is
            cached) or ``SIZED``; ``UNMEASURED`` when nothing is known or ``path`` cannot
            be sized (``/``, ``/photos``, ...); ``ERROR`` with the text when ``size``
            failed.

        .. versionadded:: 3.4.0
        """
        if not _measurable(path):
            return self._figure_entry(path, None)
        now = self.clock()
        fig = self.cache.figure(path)
        if policy.never or (
            fig is not None and fig.size is not None and not policy.is_stale(fig.at, now)
        ):
            return self._figure_entry(path, fig)
        pass_id = self.cache.new_pass(now)
        try:
            result = self._size(path)
        except Exception as exc:  # noqa: BLE001 - reported as an ERROR entry
            text = _error_text(exc)
            self._errors[path] = text
            return replace(self._figure_entry(path, None), state=NodeState.ERROR, error=text)
        self._errors.pop(path, None)
        self._record_figure(path, result, now, pass_id)
        return self._figure_entry(path, self.cache.figure(path))

    def tree(
        self,
        target: str,
        policy: RefreshPolicy,
        rules: ExcludeRules,
        *,
        maxdepth: int,
        base: str | None = None,
    ) -> dict[str, Breakdown]:
        """Break down ``target`` and every non-excluded folder above ``maxdepth``.

        ``target`` is depth 0 and is always broken down; a child folder at depth ``d`` is
        broken down when ``d < maxdepth``, so ``maxdepth=1`` gives ``target`` alone.
        Excluded folders (and everything under them) are not broken down.

        :param target: escaped Drive path; ``rules`` are relative to it.
        :param policy: refresh policy for every breakdown.
        :param rules: freeze/hide rules.
        :param maxdepth: depth limit as above.
        :param base: escaped Drive path ``rules`` are matched relative to; defaults to
            ``target``. Must contain ``target``.
        :returns: the breakdowns keyed by escaped Drive path, in breadth-first order.
        :raises ValueError: if ``target`` is not within ``base``.

        .. versionadded:: 3.4.0
        """
        base = target if base is None else base
        if not is_within(target, base):
            raise ValueError(f"{target!r} is not within the rules base {base!r}")
        out: dict[str, Breakdown] = {}
        queue: deque[tuple[str, int]] = deque([(target, 0)])
        while queue:
            path, depth = queue.popleft()
            if path in out:
                continue
            bd = self.breakdown(path, policy, rules, base)
            out[path] = bd
            if depth + 1 < maxdepth:
                queue.extend((p, depth + 1) for p in self._subfolders(bd))
        return out

    def walk(
        self,
        path: str,
        policy: RefreshPolicy,
        rules: ExcludeRules,
        *,
        target: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Break down ``path`` and every non-excluded folder beneath it, breadth first.

        Folders whose snapshot is fresh under ``policy`` cost no calls, so an interrupted
        walk resumes cheaply.

        :param path: escaped Drive path to start from.
        :param policy: refresh policy for every breakdown.
        :param rules: freeze/hide rules.
        :param target: escaped Drive path the rules are relative to; defaults to ``path``.
        :param on_progress: called after each folder as ``on_progress(done, known)``,
            where ``known`` is ``done`` plus the folders found but not yet broken down.
        :param cancel: when set, the walk stops before the next folder.
        :returns: the number of folders broken down.
        :raises ValueError: if ``path`` is not within ``target``.

        .. versionadded:: 3.4.0
        """
        base = path if target is None else target
        queue: deque[str] = deque([path])
        seen = {path}
        done = 0
        while queue:
            if cancel is not None and cancel.is_set():
                break
            bd = self.breakdown(queue.popleft(), policy, rules, base)
            done += 1
            for child in self._subfolders(bd):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
            if on_progress is not None:
                on_progress(done, done + len(queue))
        return done

    def root(self, policy: RefreshPolicy, rules: ExcludeRules) -> Breakdown:
        """Return the account view ``/``: ``/my-files``, each device, and the photo library.

        The device list comes from ``list /devices`` (cached, refreshed under ``policy``);
        ``/my-files`` and each device are measured like :meth:`figure`. ``rules`` are
        relative to ``/`` (e.g. ``my-files``, ``devices/laptop``): a hidden section is left
        out with no calls, a frozen one shows its cached figure.

        :param policy: refresh policy for the device list and each figure.
        :param rules: freeze/hide rules relative to ``/``.
        :returns: a breakdown at ``"/"`` whose entries all have kind ``"section"``:
            ``/my-files`` and each ``/devices/<name>`` (``SIZED`` when measured), then
            ``/photos`` (always ``UNMEASURED``). When the device list is unknown (nothing
            cached and no listing made, e.g. under ``never`` or ``-x devices``) a
            ``/devices`` entry stands in, ``UNMEASURED`` (``EXCLUDED`` when frozen); a
            failed device listing adds a ``/devices`` entry with state ``ERROR`` and keeps
            any cached device list. ``size`` is the sum of the measured entries (``None``
            if none is), ``at`` and ``oldest_at`` the oldest of their times, and ``gap`` is
            ``None`` with ``gap_kind`` ``NONE``. ``state`` is ``PARTIAL`` when a section
            other than ``/photos`` is hidden or has no figure, so ``size`` understates
            "used"; otherwise ``LISTED``.

        Side effect: when no trash summary is stored, the stored one is partial, or it is
        stale under ``policy``, runs the trash pass (``filesystem list /trash`` plus
        breakdowns) and stores the summary; read it with :meth:`trash_summary`. Nothing
        runs under ``policy.never``.

        .. versionadded:: 3.4.0
        """
        entries: list[Entry] = []
        partial = False
        x_devices = rules.match("devices", True)
        devices: list[tuple[str, str | None, bool]] | None = None
        devices_error = None
        if x_devices is Exclusion.HIDE:
            partial = True
        else:
            # Listed first so that a volume-scope change clears the cache before any
            # cached figure is read.
            devices, devices_error = self._device_list(
                policy, frozen=x_devices is Exclusion.FREEZE
            )
        x_my = rules.match("my-files", True)
        if x_my is Exclusion.HIDE:
            partial = True
        else:
            entries.append(self._section_entry("/my-files", policy, x_my))
        if devices_error is not None:
            entries.append(Entry("devices", "/devices", "section", None, None, None, None,
                                 NodeState.ERROR, x_devices, None, True, devices_error))
        elif devices is None and x_devices is not Exclusion.HIDE:
            state = (NodeState.EXCLUDED if x_devices is Exclusion.FREEZE
                     else NodeState.UNMEASURED)
            entries.append(Entry("devices", "/devices", "section", None, None, None, None,
                                 state, x_devices, None, True, None))
        for name, uid, decrypted in devices or []:
            dpath = child_path("/devices", name, uid, decrypted)
            x = rules.match(relative(dpath, "/"), True)
            if x is Exclusion.HIDE:
                partial = True
                continue
            entry = self._section_entry(dpath, policy, x)
            entries.append(replace(entry, name=name, uid=uid, decrypted=decrypted))
        partial = partial or any(e.size is None for e in entries)
        x_photos = rules.match("photos", True)
        if x_photos is not Exclusion.HIDE:
            entries.append(Entry("photos", PHOTOS_PATH, "section", None, None, None, None,
                                 NodeState.UNMEASURED, x_photos, None, True, None))
        self._refresh_trash(policy)
        measured = [e for e in entries if e.size is not None]
        sizes = [e.size for e in measured if e.size is not None]
        times = [e.at for e in measured if e.at is not None]
        oldest = min(times) if times else None
        return Breakdown(
            "/",
            sum(sizes) if sizes else None,
            sum(e.descendants or 0 for e in measured) if measured else None,
            oldest,
            NodeState.PARTIAL if partial else NodeState.LISTED,
            tuple(entries),
            None,
            GapKind.NONE,
            oldest,
            max(times) if times else None,
        )

    def trash_summary(self) -> TrashSummary | None:
        """Return the stored trash summary, or ``None`` if no trash pass has run.

        No Drive calls. :meth:`root` runs and stores the pass when it is missing or stale.

        .. versionadded:: 3.4.0
        """
        stored = self.cache.trash()
        if stored is None:
            return None
        try:
            return TrashSummary.from_json(stored[0])
        except ValueError:
            return None

    def trash_for(self, path: str) -> TrashFigure:
        """Return the trash bytes whose original parent is within ``path``.

        No Drive calls; see :func:`protonfs.usage.trash.figure_for`. The figure is a lower
        bound unless ``complete``; its ``unchecked_bytes`` says how much of the subtree
        was not broken down.

        :param path: escaped Drive path, or ``"/"``.
        :returns: the figure.

        .. versionadded:: 3.4.0
        """
        return figure_for(self, path)

    def _refresh_trash(self, policy: RefreshPolicy) -> None:
        """Run and store the trash pass unless a complete, fresh summary is stored.

        A stored summary whose listing was partial (timed out or failed) is always stale.
        """
        if policy.never:
            return
        stored = self.cache.trash()
        if stored is not None and not policy.is_stale(stored[1], self.clock()):
            summary = self.trash_summary()
            if summary is not None and summary.partial is None:
                return
        summary = run_trash_pass(self, policy)
        self.cache.put_trash(summary.to_json(), summary.at)

    def device_paths(self) -> list[str] | None:
        """Return the escaped Drive paths of the devices in the cached device list.

        No Drive calls; :meth:`root` caches the list.

        :returns: one ``/devices/<name>`` path per device (possibly empty), or ``None``
            when no device list is cached.

        .. versionadded:: 3.4.0
        """
        devices = self._cached_devices()
        if devices is None:
            return None
        return [child_path("/devices", n, u, d) for n, u, d in devices]

    def used_total(self) -> int | None:
        """Return the cached ``/my-files`` figure plus every cached device figure.

        :returns: bytes, or ``None`` if the ``/my-files`` figure, the device list (cached
            by :meth:`root`) or any device's figure is missing. No Drive calls are made.

        .. versionadded:: 3.4.0
        """
        fig = self.cache.figure("/my-files")
        devices = self.device_paths()
        if fig is None or fig.size is None or devices is None:
            return None
        total = fig.size
        for dpath in devices:
            dfig = self.cache.figure(dpath)
            if dfig is None or dfig.size is None:
                return None
            total += dfig.size
        return total

    def check_account(self, policy: RefreshPolicy) -> bool:
        """Clear the cache if it belongs to a different account; run once before answering.

        Unless ``policy.never`` or this engine has already read a volume scope from a
        listing, lists ``/my-files`` (one call) and compares its scope with the cached
        one. A different scope, or a cache that holds figures, snapshots or a trash
        summary but has no recorded scope, clears the whole cache; an empty cache just
        records the scope. A failed or empty listing changes nothing, and the next listing
        that carries a scope checks again.

        :param policy: the run's refresh policy.
        :returns: whether the cache was cleared.

        .. versionadded:: 3.4.0
        """
        if policy.never or self._scope_checked:
            return False
        try:
            raw = self._list("/my-files", None)
        except Exception:  # noqa: BLE001 - unverified; the next listing checks again
            return False
        return self._check_scope(raw)

    # ------------------------------------------------------------- breakdown pass

    def _breakdown(
        self,
        path: str,
        policy: RefreshPolicy,
        rules: ExcludeRules,
        target: str,
        *,
        restarted: bool,
    ) -> Breakdown:
        if not _measurable(path):
            return _unmeasured(path)
        if rules.match(relative(path, target), True) is not Exclusion.NONE:
            cached = self._assemble(path, self.cache.snapshot(path), rules, target)
            return replace(cached, state=NodeState.EXCLUDED)
        now = self.clock()
        snap = self.cache.snapshot(path)
        if policy.never or self._is_fresh(snap, path, policy, rules, target, now):
            return self._assemble(path, snap, rules, target)
        pass_id = self.cache.new_pass(now)
        try:
            measured = self._size(path)
        except Exception as exc:  # noqa: BLE001 - reported as an ERROR breakdown
            text = _error_text(exc)
            self._errors[path] = text
            return Breakdown(path, None, None, None, NodeState.ERROR, (), None, GapKind.NONE,
                             None, None, text)
        self._errors.pop(path, None)
        expected = snap.width if snap is not None else measured.descendants
        try:
            raw = self._list(path, expected)
        except Exception as exc:  # noqa: BLE001 - reported as an ERROR breakdown
            self._record_figure(path, measured, now, pass_id)
            return Breakdown(path, measured.size, measured.descendants, now,
                             NodeState.ERROR, (), None, GapKind.NONE, now, now,
                             _error_text(exc))
        if self._check_scope(raw) and not restarted:
            return self._breakdown(path, policy, rules, target, restarted=True)
        # Built from this pass's listing and the snapshot object checked above, never
        # from whatever another writer stored meanwhile.
        written = self._store_pass(path, snap, raw, measured, policy, rules, target, now,
                                   pass_id)
        self._record_figure(path, measured, now, pass_id)
        return self._assemble(path, written, rules, target)

    def _live_folder_rows(
        self, snap: Snapshot, path: str, rules: ExcludeRules, target: str
    ) -> list[Row]:
        """Folder rows that ``rules`` do not exclude, i.e. those a pass would size."""
        return [
            r for r in snap.rows
            if r.kind == "folder"
            and rules.match(relative(_row_path(path, r), target), True) is Exclusion.NONE
        ]

    def _is_fresh(
        self,
        snap: Snapshot | None,
        path: str,
        policy: RefreshPolicy,
        rules: ExcludeRules,
        target: str,
        now: float,
    ) -> bool:
        """Whether ``snap`` can answer without calls: valid, recent, and no live holes."""
        if snap is None or snap.invalid or policy.is_stale(snap.at, now):
            return False
        return all(
            r.size is not None and not policy.is_stale(r.at, now)
            for r in self._live_folder_rows(snap, path, rules, target)
        )

    @staticmethod
    def _reusable_rows(
        snap: Snapshot | None,
        raw: Sequence[RawEntry],
        measured: SizeResult,
        policy: RefreshPolicy,
    ) -> dict[str, Row]:
        """Return the cached child-folder rows, by uid, whose sizes this pass may reuse.

        Empty unless the policy allows the shortcut, ``snap`` is valid, the folder's
        fingerprint is unchanged, and ``raw`` has the same child-folder uids and the same
        summed file bytes as ``snap``. Trashing, restoring, adding or removing a direct
        child changes the uid set or the file bytes, and any change below a child changes
        the fingerprint, so only bytes shifting between siblings go unseen. Only rows
        measured in ``snap``'s own pass are returned.
        """
        if snap is None or snap.invalid or not policy.allow_shortcut:
            return {}
        if tuple(snap.fingerprint) != (measured.size, measured.descendants):
            return {}
        old = [r.uid for r in snap.rows if r.kind == "folder"]
        new = [e.uid for e in raw if e.kind == "folder"]
        if None in old or None in new or len(set(old)) != len(old) or set(old) != set(new):
            return {}
        old_bytes = sum(r.size or 0 for r in snap.rows if r.kind != "folder")
        new_bytes = sum(e.size or 0 for e in raw if e.kind != "folder")
        if old_bytes != new_bytes or len(new) != len(old):
            return {}
        return {
            r.uid: r for r in snap.rows
            if r.kind == "folder" and r.uid is not None and r.size is not None
            and r.pass_id == snap.pass_id
        }

    def _store_pass(
        self,
        path: str,
        snap: Snapshot | None,
        raw: Sequence[RawEntry],
        measured: SizeResult,
        policy: RefreshPolicy,
        rules: ExcludeRules,
        target: str,
        now: float,
        pass_id: int,
    ) -> Snapshot:
        """Size or reuse the live child folders, then write the snapshot and child figures.

        Returns the snapshot written (one ``put_snapshot``).
        """
        children = [(e, child_path(path, e.name, e.uid, e.decrypted)) for e in raw]
        reusable = self._reusable_rows(snap, raw, measured, policy)
        live = {
            p for e, p in children
            if e.kind == "folder"
            and rules.match(relative(p, target), True) is Exclusion.NONE
        }
        to_size = [
            p for e, p in children
            if p in live and (e.uid is None or e.uid not in reusable)
        ]
        results = self._parallel(self._size, to_size)
        sized: dict[str, SizeResult] = {}
        failed: dict[str, str] = {}
        for p, res in zip(to_size, results):
            if isinstance(res, BaseException):
                failed[p] = _error_text(res)
            else:
                sized[p] = res
        rows: list[Row] = []
        for e, p in children:
            name = node_segment(e.name, e.uid, e.decrypted)
            if e.kind != "folder":
                rows.append(Row(name, "file", e.uid, e.size, None, now, pass_id, e.mtime,
                                e.decrypted))
                continue
            if p in sized:
                res = sized[p]
                rows.append(Row(name, "folder", e.uid, res.size, res.descendants, now,
                                pass_id, e.mtime, e.decrypted))
                continue
            if p in live and e.uid is not None and e.uid in reusable:
                old = reusable[e.uid]
                rows.append(Row(name, "folder", e.uid, old.size, old.descendants, now,
                                pass_id, e.mtime, e.decrypted))
                continue
            fig = None if p in failed else self.cache.figure(p)
            if fig is not None and fig.size is not None:
                rows.append(Row(name, "folder", e.uid, fig.size, fig.descendants, fig.at,
                                fig.pass_id, e.mtime, e.decrypted))
            else:
                rows.append(Row(name, "folder", e.uid, None, None, None, None, e.mtime,
                                e.decrypted))
        uid = (
            next((e.parent_uid for e in raw if e.parent_uid), None)
            or (snap.uid if snap is not None else None)
            or self._uid_from_parent(path)
        )
        written = Snapshot(
            path, uid, now, pass_id, (measured.size, measured.descendants), len(raw), False,
            tuple(rows),
        )
        self.cache.put_snapshot(written)
        for p, res in sized.items():
            self._errors.pop(p, None)
            self._record_figure(p, res, now, pass_id, check_parent=False)
        self._errors.update(failed)
        return written

    def _record_figure(
        self, path: str, result: SizeResult, now: float, pass_id: int, *, check_parent: bool = True
    ) -> None:
        """Store a new figure for ``path`` and invalidate snapshots it contradicts."""
        fp = (result.size, result.descendants)
        self.cache.put_figure(path, Figure(result.size, result.descendants, now, pass_id))
        own = self.cache.snapshot(path)
        if own is not None and tuple(own.fingerprint) != fp:
            self.cache.invalidate(path)
        if check_parent:
            found = self._parent_row(path)
            if found is not None and (found[1].size, found[1].descendants) != fp:
                self.cache.invalidate(found[0])

    # ----------------------------------------------------------------- assembling

    def _assemble(
        self, path: str, snap: Snapshot | None, rules: ExcludeRules, target: str
    ) -> Breakdown:
        """Build the breakdown of ``path`` from the cache alone (no Drive calls)."""
        fig = self.cache.figure(path)
        if snap is None:
            if fig is None or fig.size is None:
                return _unmeasured(path)
            return Breakdown(path, fig.size, fig.descendants, fig.at, NodeState.SIZED, (),
                             None, GapKind.NONE, fig.at, fig.at)
        fingerprint = tuple(snap.fingerprint)
        if fig is not None and fig.size is not None:
            size, descendants, at = fig.size, fig.descendants, fig.at
            # A newer figure equal to the fingerprint is the same measurement as far as
            # the gap is concerned; a different one means the listing is outdated.
            mixed = (fig.size, fig.descendants) != fingerprint
        else:
            (size, descendants), at, mixed = fingerprint, snap.at, False
        times = [t for t in (at, snap.at) if t is not None]
        known = 0
        holes = False
        entries: list[Entry] = []
        for row in snap.rows:
            child = _row_path(path, row)
            excl = rules.match(relative(child, target), row.kind == "folder")
            if row.size is None:
                holes = True
            else:
                known += row.size
            if row.pass_id != snap.pass_id:
                mixed = True
            if row.at is not None:
                times.append(row.at)
            if excl is not Exclusion.HIDE:
                entries.append(self._row_entry(child, row, excl))
        gap = size - known
        return Breakdown(path, size, descendants, at, NodeState.LISTED, tuple(entries), gap,
                         _label(gap, holes, mixed), min(times), max(times))

    def _row_entry(self, child: str, row: Row, excl: Exclusion) -> Entry:
        error = None
        if row.kind != "folder":
            state = NodeState.SIZED if row.size is not None else NodeState.UNMEASURED
        elif excl is not Exclusion.NONE:
            state = NodeState.EXCLUDED
        elif row.size is None:
            error = self._errors.get(child)
            state = NodeState.ERROR if error is not None else NodeState.UNMEASURED
        elif self.cache.has_snapshot(child):
            state = NodeState.LISTED
        else:
            state = NodeState.SIZED
        return Entry(row.name, child, row.kind, row.uid, row.size, row.descendants, row.at,
                     state, excl, row.mtime, row.decrypted, error)

    def _figure_entry(self, path: str, fig: Figure | None) -> Entry:
        segs = split(path)
        found = self._parent_row(path) if segs else None
        row = found[1] if found is not None else None
        listed = bool(segs) and self.cache.has_snapshot(path)
        uid = row.uid if row is not None else None
        if uid is None and listed:
            snap = self.cache.snapshot(path)
            uid = snap.uid if snap is not None else None
        if fig is None or fig.size is None:
            size = descendants = at = None
            state = NodeState.UNMEASURED
        else:
            size, descendants, at = fig.size, fig.descendants, fig.at
            state = NodeState.LISTED if listed else NodeState.SIZED
        return Entry(
            row.name if row is not None else (segs[-1] if segs else "/"),
            path, "folder", uid, size, descendants, at, state, Exclusion.NONE,
            row.mtime if row is not None else None,
            row.decrypted if row is not None else True,
            None,
        )

    def _section_entry(self, path: str, policy: RefreshPolicy, excl: Exclusion) -> Entry:
        if excl is Exclusion.FREEZE:
            entry = replace(self.figure(path, _CACHE_ONLY), state=NodeState.EXCLUDED)
        else:
            entry = self.figure(path, policy)
        return replace(entry, kind="section", exclusion=excl)

    @staticmethod
    def _subfolders(bd: Breakdown) -> list[str]:
        """Entries worth breaking down next: measured, non-excluded, measurable folders.

        Skips files, excluded entries, ``ERROR`` and ``UNMEASURED`` entries (a failed
        child, the ``/devices`` placeholder, ``/photos``) and paths ``size`` cannot take.
        """
        skip = (NodeState.ERROR, NodeState.UNMEASURED)
        return [e.path for e in bd.entries
                if e.kind != "file" and e.exclusion is Exclusion.NONE
                and e.state not in skip and _measurable(e.path)]

    def _parent_row(self, path: str) -> tuple[str, Row] | None:
        """Return ``(parent_path, row)`` for ``path`` in its parent's snapshot, if cached."""
        parent = parent_of(path)
        if parent is None:
            return None
        psnap = self.cache.snapshot(parent)
        if psnap is None:
            return None
        for row in psnap.rows:
            if row.kind == "folder" and _row_path(parent, row) == path:
                return parent, row
        return None

    def _uid_from_parent(self, path: str) -> str | None:
        found = self._parent_row(path)
        return found[1].uid if found is not None else None

    # ------------------------------------------------------------- devices, scope

    def _cached_devices(self) -> list[tuple[str, str | None, bool]] | None:
        text = self.cache.get_meta(_DEVICES_KEY)
        if text is None:
            return None
        try:
            return [(str(n), u, bool(d)) for n, u, d in json.loads(text)]
        except (ValueError, TypeError):
            return None

    def _device_list(
        self, policy: RefreshPolicy, *, frozen: bool
    ) -> tuple[list[tuple[str, str | None, bool]] | None, str | None]:
        """Return ``(devices, error)``: the cached list, re-listed when stale.

        ``devices`` is ``None`` when no list is cached and none was fetched.
        """
        now = self.clock()
        cached = self._cached_devices()
        at_text = self.cache.get_meta(_DEVICES_AT_KEY)
        at = float(at_text) if at_text else None
        if policy.never or frozen or (cached is not None and not policy.is_stale(at, now)):
            return cached, None
        try:
            raw = self._list("/devices", None)
        except Exception as exc:  # noqa: BLE001 - reported as an ERROR entry
            return cached, _error_text(exc)
        self._check_scope(raw)
        devices = [(e.name, e.uid, e.decrypted) for e in raw if e.kind == "folder"]
        self.cache.set_meta(_DEVICES_KEY, json.dumps(devices))
        self.cache.set_meta(_DEVICES_AT_KEY, repr(now))
        return devices, None

    def _check_scope(self, raw: Sequence[RawEntry]) -> bool:
        """Compare the first listed volume scope with the cached one; True if it wiped.

        Runs once per engine, on the first listing that carries a scope. A different
        recorded scope, or cached data with no recorded scope (its account is unknown),
        clears the cache; an empty cache just records the scope.
        """
        if self._scope_checked:
            return False
        scope = next((e.scope for e in raw if e.scope), None)
        if scope is None:
            return False
        with self._lock:
            if self._scope_checked:
                return False
            self._scope_checked = True
            stored = self.cache.get_meta(_SCOPE_KEY)
            if stored == scope:
                return False
            wipe = stored is not None or self.cache.has_data()
            if wipe:
                self.cache.clear()
                self._errors.clear()
            self.cache.set_meta(_SCOPE_KEY, scope)
            return wipe

    # ---------------------------------------------------------------- source calls

    def _size(self, path: str) -> SizeResult:
        if self.on_call is not None:
            self.on_call("size", path)
        return self.source.size(path)

    def _list(self, path: str, expected: int | None) -> list[RawEntry]:
        if self.on_call is not None:
            self.on_call("list", path)
        return self.source.list_dir(path, expected_entries=expected)

    def _parallel(
        self, fn: Callable[[str], SizeResult], items: Sequence[str]
    ) -> list[SizeResult | BaseException]:
        """Run ``fn`` over ``items`` on up to ``source.concurrency`` threads.

        Returns one result per item, in order; an item whose call raised gets the
        exception instead, so one failure never stops its siblings.
        """
        if not items:
            return []
        workers = max(1, min(int(self.source.concurrency or 1), len(items)))
        out: list[SizeResult | BaseException] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fn, item) for item in items]
            for fut in futures:
                try:
                    out.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - returned to the caller
                    out.append(exc)
        return out
