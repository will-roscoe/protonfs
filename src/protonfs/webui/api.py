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


def to_jsonable(obj):
    """Convert dataclasses, enums, tuples and dicts to plain JSON-able values.

    :param obj: a value built from those types plus str, int, float, bool and ``None``.
    :returns: the same structure as dicts, lists and scalars; enums become their values.
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
        self._walk_cancel = threading.Event()
        self._auto: set = set()

    def _within(self, path: str) -> str:
        if not isinstance(path, str) or not is_within(path, self.settings.target):
            raise ValueError(f"{path!r} is not within {self.settings.target!r}")
        return path

    # ------------------------------------------------------------------ reading

    def tree(self, path: str, depth: int) -> dict:
        """Return the cached rows and breakdowns under ``path``, queueing stale ones.

        Makes no Drive calls. Every breakdown shown that is stale under the current refresh
        setting, or has no listing, is queued once for a background refresh (not for a
        failed, excluded or unmeasurable folder, and never under refresh ``never``); the
        page hears the result as a ``breakdown`` event. Exclusion patterns are matched
        relative to ``path``.

        :param path: escaped Drive path, within the settings' target.
        :param depth: levels to include, within :data:`~protonfs.webui.settings.DEPTH_RANGE`.
        :returns: ``{"rows": [...], "breakdowns": {path: {...}}, "pending": [paths]}`` where
            rows have the :data:`~protonfs.usage.report.JSON_ROW_KEYS` keys.
        :raises ValueError: if ``path`` is outside the target or ``depth`` is out of range.
        """
        self._within(path)
        if type(depth) is not int or not DEPTH_RANGE[0] <= depth <= DEPTH_RANGE[1]:
            raise ValueError(f"depth must be an int from {DEPTH_RANGE[0]} to {DEPTH_RANGE[1]}")
        s = self.settings
        rules = s.rules()
        now = self.engine.clock()
        rows = collect_rows(
            self.engine, path, _NEVER, rules, maxdepth=depth, plan_size=self.plan_size, now=now
        )
        if s.dirs_only:
            rows = [r for r in rows if r.kind != "file"]
        bds = self.engine.tree(path, _NEVER, rules, maxdepth=depth)
        policy = s.policy()
        if not policy.never:
            for bd in bds.values():
                self._queue_if_stale(bd, policy, now)
        return {
            "rows": [{k: getattr(r, k) for k in JSON_ROW_KEYS} for r in rows],
            "breakdowns": {p: to_jsonable(bd) for p, bd in bds.items()},
            "pending": self.jobs.pending(),
        }

    def _queue_if_stale(self, bd, policy: RefreshPolicy, now: float) -> None:
        if bd.path in self._auto or bd.state in (NodeState.EXCLUDED, NodeState.ERROR):
            return
        at = bd.oldest_at if bd.oldest_at is not None else bd.at
        if bd.state is not NodeState.LISTED or policy.is_stale(at, now):
            self._auto.add(bd.path)
            self.jobs.submit("breakdown", bd.path, policy)

    def root(self) -> dict:
        """Return :meth:`tree` for the settings' target at the current depth, plus trash.

        :returns: the :meth:`tree` dict with a ``"trash"`` key: the cached trash summary as
            a dict, or ``None`` when no trash pass has run.
        """
        out = self.tree(self.settings.target, self.settings.maxdepth)
        out["trash"] = to_jsonable(self.engine.trash_summary())
        return out

    # ----------------------------------------------------------------- commands

    def refresh(self, path: str) -> dict:
        """Queue a fresh breakdown of ``path`` (refresh ``0``: everything re-measured).

        :param path: escaped Drive path within the target.
        :returns: ``{"queued": bool, "pending": [paths]}``; ``queued`` is ``False`` when the
            same path was already queued or running.
        :raises ValueError: if ``path`` is outside the target.
        """
        self._within(path)
        self._auto.discard(path)
        queued = self.jobs.submit("breakdown", path, _FORCE)
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
        self._walk_cancel = threading.Event()
        return {"started": self.jobs.walk(paths, self._walk_cancel)}

    def cancel_walk(self) -> dict:
        """Ask the running walk (if any) to stop before its next folder.

        :returns: ``{"cancelled": True}``.
        """
        self._walk_cancel.set()
        return {"cancelled": True}

    def get_settings(self) -> dict:
        """Return the current settings as a dict."""
        return self.settings.to_dict()

    def set_settings(self, data: dict) -> dict:
        """Apply the editable fields of ``data`` and return the new settings.

        :param data: a patch of ``refresh``, ``maxdepth``, ``dirs_only``, ``freeze``,
            ``hide``; ``target`` may be present only if unchanged.
        :returns: the new settings as a dict.
        :raises ValueError: if ``data`` is not a dict or a field is invalid; nothing changes.
        """
        if not isinstance(data, dict):
            raise ValueError("settings must be a JSON object")
        patch = dict(data)
        if patch.get("target") == self.settings.target:
            del patch["target"]
        self.settings = self.settings.updated(patch)
        self._auto.clear()
        return self.settings.to_dict()

    # ------------------------------------------------------------------- events

    def events(self, timeout: float = 15.0) -> Iterator[dict]:
        """Yield ``{"event": name, "data": {...}}`` dicts until the job queue closes.

        A ``{"event": "keepalive", "data": {}}`` is yielded after ``timeout`` seconds
        without an event. Names are ``breakdown``, ``progress``, ``trash`` and ``error``.

        :param timeout: seconds of silence before a keepalive.
        :returns: a generator; closing it unsubscribes.
        """
        import queue

        q = self.jobs.subscribe()
        try:
            while True:
                try:
                    ev = q.get(timeout=timeout)
                except queue.Empty:
                    yield {"event": "keepalive", "data": {}}
                    continue
                if ev is None:
                    return
                name = ev["event"]
                data = {k: to_jsonable(v) for k, v in ev.items() if k != "event"}
                yield {"event": name, "data": data}
        finally:
            self.jobs.unsubscribe(q)
