"""The ``du --gui`` operations as plain functions of dicts; no HTTP in here.

:class:`Api` has one method per route. Every method returns JSON-able data and raises
``ValueError`` for a bad request, so the server only parses, authenticates and maps routes.
Request-thread data is cache-only: anything stale is queued on the
:class:`~protonfs.webui.jobs.JobQueue` and arrives later as an event.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import dataclasses
import enum
import queue as queue_mod
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING

from protonfs.usage.model import NodeState
from protonfs.usage.paths import is_within
from protonfs.usage.policy import RefreshPolicy
from protonfs.usage.report import JSON_ROW_KEYS, collect_rows

from .settings import DEPTH_RANGE, GuiSettings

if TYPE_CHECKING:
    from protonfs.usage.engine import UsageEngine

    from .jobs import JobQueue

_NEVER = RefreshPolicy.parse("never")
_FORCE = RefreshPolicy.parse("0")

#: The values :meth:`Api.tree`'s ``queue`` takes, and the ``queue=`` query parameter of
#: ``/api/tree`` and ``/api/root`` (which also accepts ``1`` for ``stale`` and ``0`` for
#: ``none``).
QUEUE_MODES = ("stale", "new", "none")


def to_jsonable(obj):
    """Convert dataclasses, enums, tuples and dicts to plain JSON-able values.

    :param obj: a value built from those types plus str, int, float, bool and ``None``.
    :returns: the same structure as dicts, lists and scalars; enums become their values.

    .. versionadded:: 3.4.0
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj


class Api:
    """What the page can ask of the server.

    :param engine: the usage engine (read through its cache only on request threads).
    :param jobs: the job queue; its settings callable should read :attr:`settings`.
    :param settings: the initial settings; replaced (they are immutable) by
        :meth:`set_settings`.
    :param plan_size: plan size in bytes for the ``pct_plan`` column, or ``None``.

    :ivar settings: the current :class:`~protonfs.webui.settings.GuiSettings`.

    .. versionadded:: 3.4.0
    """

    def __init__(
        self,
        engine: UsageEngine,
        jobs: JobQueue,
        settings: GuiSettings,
        *,
        plan_size: int | None = None,
    ) -> None:
        self.engine = engine
        self.jobs = jobs
        self.settings = settings
        self.plan_size = plan_size
        self._invalid_queued: set = set()
        self._trash_rerun_for: float | None = None  # summary.at a re-run was queued for
        self._walk_cancel = threading.Event()
        self._walk_lock = threading.Lock()

    def _within(self, path: str) -> str:
        if not isinstance(path, str) or not is_within(path, self.settings.target):
            raise ValueError(f"{path!r} is not within {self.settings.target!r}")
        return path

    # ------------------------------------------------------------------ reading

    def tree(self, path: str, depth: int, *, queue: str = "stale") -> dict:
        """Return the cached rows and breakdowns under ``path``, queueing the ones to fetch.

        Makes no Drive calls; whatever ``queue`` selects is submitted for a background
        refresh, and the page hears each result as a ``breakdown`` event. A breakdown that is
        excluded or failed is never queued, nothing is queued under refresh ``never``, and a
        path already queued or running is not queued twice. Exclusion patterns are matched
        relative to the settings' target, whatever ``path`` is.

        :param path: escaped Drive path, within the settings' target.
        :param depth: levels to include, within :data:`~protonfs.webui.settings.DEPTH_RANGE`.
        :param queue: which breakdowns to queue, one of :data:`QUEUE_MODES`: ``"stale"``
            (user navigation) queues every one shown that is stale under the current refresh
            setting or has no listing; ``"new"`` (a refetch triggered by an event) queues
            only those with no listing that no job has tried since the page started, so the
            subfolders a finished job revealed get listed but refetches cannot feed
            themselves; ``"none"`` queues nothing.
        :returns: ``{"rows": [...], "breakdowns": {path: {...}}, "pending": [paths],
            "now": seconds}`` where rows have the :data:`~protonfs.usage.report.JSON_ROW_KEYS`
            keys and ``now`` is the engine's clock (Unix time), the reference for every
            ``age_seconds`` and timestamp in the answer.
        :raises ValueError: if ``path`` is outside the target, ``depth`` is out of range or
            ``queue`` is not a known mode.
        """
        self._within(path)
        if type(depth) is not int or not DEPTH_RANGE[0] <= depth <= DEPTH_RANGE[1]:
            raise ValueError(f"depth must be an int from {DEPTH_RANGE[0]} to {DEPTH_RANGE[1]}")
        if queue not in QUEUE_MODES:
            raise ValueError(f"queue must be one of {', '.join(QUEUE_MODES)}, not {queue!r}")
        s = self.settings
        rules = s.rules()
        now = self.engine.clock()
        rows = collect_rows(
            self.engine, path, _NEVER, rules, maxdepth=depth, plan_size=self.plan_size,
            now=now, base=s.target,
        )
        if s.dirs_only:
            rows = [r for r in rows if r.kind != "file"]
        bds = self.engine.tree(path, _NEVER, rules, maxdepth=depth, base=s.target)
        policy = s.policy()
        if queue != "none" and not policy.never:
            for bd in bds.values():
                self._queue_if_due(bd, policy, now, only_new=queue == "new")
            self._queue_trash_if_resolvable(policy)
        return {
            "rows": [{k: getattr(r, k) for k in JSON_ROW_KEYS} for r in rows],
            "breakdowns": {p: to_jsonable(bd) for p, bd in bds.items()},
            "pending": self.jobs.pending(),
            "now": now,
        }

    def _queue_trash_if_resolvable(self, policy: RefreshPolicy) -> None:
        # The trash pass runs inside the "/" job. When the page has since cached the
        # original parent of a trashed folder the last pass could not place, re-run it, at
        # most once per stored summary: a pass that still cannot place it must not loop.
        if self.settings.target != "/":
            return
        summary = self.engine.trash_summary()
        if summary is None or summary.at == self._trash_rerun_for:
            return
        if self.engine.trash_parent_now_cached():
            self._trash_rerun_for = summary.at
            self.jobs.submit("breakdown", "/", policy)

    def _queue_if_due(self, bd, policy: RefreshPolicy, now: float, *, only_new: bool) -> None:
        if bd.state in (NodeState.EXCLUDED, NodeState.ERROR):
            return
        if not bd.invalid:
            self._invalid_queued.discard(bd.path)
        if only_new:
            due = bd.state is not NodeState.LISTED and not self.jobs.tried(bd.path)
            if bd.invalid and bd.path not in self._invalid_queued:
                # once per invalidation: a job that fails leaves it invalid, and refetches
                # must not retry it (navigation does)
                self._invalid_queued.add(bd.path)
                due = True
        else:
            at = bd.oldest_at if bd.oldest_at is not None else bd.at
            due = bd.state is not NodeState.LISTED or bd.invalid or policy.is_stale(at, now)
        if due:
            self.jobs.submit("breakdown", bd.path, policy)

    def root(self, *, queue: str = "stale") -> dict:
        """Return :meth:`tree` for the settings' target at the current depth, plus trash.

        :param queue: as for :meth:`tree`.
        :returns: the :meth:`tree` dict with a ``"trash"`` key: the cached trash summary as
            a dict, or ``None`` when no trash pass has run. A summary dict also carries
            ``total_known`` (measured bytes) and ``complete`` (whether that is the whole
            trash).
        """
        out = self.tree(self.settings.target, self.settings.maxdepth, queue=queue)
        summary = self.engine.trash_summary()
        trash = to_jsonable(summary)
        if trash is not None:
            trash["total_known"] = summary.total_known
            trash["complete"] = summary.complete
        out["trash"] = trash
        return out

    # ----------------------------------------------------------------- commands

    def refresh(self, path: str) -> dict:
        """Queue a fresh breakdown of ``path`` (refresh ``0``: everything re-measured).

        The refresh always starts after this call: a navigation job already queued for
        ``path`` is upgraded to refresh ``0``, and one already running is followed by a
        second, forced run.

        :param path: escaped Drive path within the target.
        :returns: ``{"queued": bool, "pending": [paths]}``; ``queued`` is ``False`` only when
            the job queue is closed.
        :raises ValueError: if ``path`` is outside the target.
        """
        self._within(path)
        queued = self.jobs.submit("breakdown", path, _FORCE, force=True)
        return {"queued": queued, "pending": self.jobs.pending()}

    def walk(self, paths: list) -> dict:
        """Start a background walk of ``paths`` under the current refresh setting.

        :param paths: escaped Drive paths within the target.
        :returns: ``{"started": bool}``; ``False`` when a walk is already running.
        :raises ValueError: if ``paths`` is not a list of in-target paths.
        """
        if not isinstance(paths, list) or not paths:
            raise ValueError("paths must be a non-empty list")
        for p in paths:
            self._within(p)
        with self._walk_lock:
            cancel = threading.Event()
            started = self.jobs.walk(paths, cancel)
            if started:
                self._walk_cancel = cancel
        return {"started": started}

    def cancel_walk(self) -> dict:
        """Ask the running walk (if any) to stop before its next folder.

        :returns: ``{"cancelled": True}``.
        """
        with self._walk_lock:
            self._walk_cancel.set()
        return {"cancelled": True}

    def _settings_dict(self) -> dict:
        out = self.settings.to_dict()
        out["refresh_seconds"] = self.settings.policy().threshold  # None under ``never``
        return out

    def get_settings(self) -> dict:
        """Return the current settings as a dict.

        Besides the :class:`~protonfs.webui.settings.GuiSettings` fields it has
        ``refresh_seconds``: the refresh threshold in seconds (``0`` for ``0``, ``None`` for
        ``never``), parsed as the command line parses it.
        """
        return self._settings_dict()

    def set_settings(self, data: dict) -> dict:
        """Apply the editable fields of ``data`` and return the new settings.

        :param data: a patch of ``refresh``, ``maxdepth``, ``dirs_only``, ``freeze``,
            ``hide``; ``target`` may be present only if unchanged.
        :returns: the new settings as a dict, as :meth:`get_settings` returns it.
        :raises ValueError: if ``data`` is not a dict or a field is invalid; nothing changes.
        """
        if not isinstance(data, dict):
            raise ValueError("settings must be a JSON object")
        patch = dict(data)
        if patch.get("target") == self.settings.target:
            del patch["target"]
        try:
            new = self.settings.updated(patch)  # validates types, ranges, policy and patterns
            new.policy()
        except ValueError:
            raise
        except (TypeError, AttributeError) as exc:
            raise ValueError(f"invalid settings: {exc}") from exc
        self.settings = new
        return self._settings_dict()

    # ------------------------------------------------------------------- events

    def events(self, timeout: float = 15.0) -> Iterator[dict]:
        """Subscribe now and return an iterator of ``{"event": name, "data": {...}}`` dicts.

        The subscription exists when this method returns, so no event published after the
        call is missed. The iterator ends when the job queue closes (or drops this
        subscriber for falling behind); ``close()`` unsubscribes. A
        ``{"event": "keepalive", "data": {}}`` is yielded after ``timeout`` seconds without an
        event. Names are ``breakdown``, ``progress``, ``trash``, ``error`` and ``notice``.

        :param timeout: seconds of silence before a keepalive.
        :returns: an iterator with a ``close()`` method.
        """
        return _Events(self.jobs, timeout)


class _Events:
    """Iterator over one subscription's events; :meth:`close` unsubscribes (idempotent)."""

    def __init__(self, jobs: JobQueue, timeout: float) -> None:
        self._jobs = jobs
        self._timeout = timeout
        self._q = jobs.subscribe()
        self._open = True

    def __iter__(self) -> _Events:
        return self

    def __next__(self) -> dict:
        if not self._open:
            raise StopIteration
        try:
            ev = self._q.get(timeout=self._timeout)
        except queue_mod.Empty:
            return {"event": "keepalive", "data": {}}
        if ev is None:
            self.close()
            raise StopIteration
        data = {k: to_jsonable(v) for k, v in ev.items() if k != "event"}
        return {"event": ev["event"], "data": data}

    def close(self) -> None:
        """Unsubscribe; later iteration stops."""
        self._open = False
        self._jobs.unsubscribe(self._q)
