"""Background Drive work for ``du --gui``: deduplicated breakdown jobs and one walk at a time.

Request threads never call Drive; they queue work here and the page hears the results as
events. Events are plain dicts carrying Python objects (``{"event": "breakdown", "path": p,
"breakdown": <Breakdown>}``); :class:`~protonfs.webui.api.Api` turns them into JSON.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Callable

from protonfs.usage.model import Breakdown

if TYPE_CHECKING:
    from protonfs.usage.engine import UsageEngine
    from protonfs.usage.policy import RefreshPolicy

    from .settings import GuiSettings

_STOP = object()

#: Unread events a subscriber may have queued before it is dropped.
MAX_BACKLOG = 1000


class JobQueue:
    """Runs breakdown jobs on worker threads and one walk on its own thread.

    Events published to subscribers (each a dict with an ``"event"`` key):

    - ``breakdown``: ``path`` and the finished ``breakdown`` (a
      :class:`~protonfs.usage.model.Breakdown`).
    - ``trash``: ``trash``, the engine's trash summary or ``None`` (after a breakdown of ``/``).
    - ``progress``: ``done``, ``total``, ``path`` and ``state`` (``running``, ``done``,
      ``cancelled`` or ``error``), aggregated over every path of the walk. The last event of
      a walk is never ``running``; a ``cancelled`` or ``error`` one keeps the last known
      ``done`` and ``total`` (``error`` adds ``message``).
    - ``error``: ``path`` and ``message`` for a job that raised; the worker carries on.

    :param engine: the usage engine the jobs run on.
    :param settings: returns the current :class:`~protonfs.webui.settings.GuiSettings`; read
        at the start of every job, so live edits apply to the next job.
    :param workers: number of breakdown worker threads.

    A subscriber whose queue holds :data:`MAX_BACKLOG` unread events is dropped: its queue
    receives a final ``None`` after the backlog is discarded.

    .. versionadded:: 3.4.0
    """

    def __init__(
        self, engine: UsageEngine, settings: Callable[[], GuiSettings], workers: int = 2
    ) -> None:
        self.engine = engine
        self._settings = settings
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue()
        self._active: dict = {}
        self._subs: list = []
        self._walking = False
        self._closed = False
        self._threads = [
            threading.Thread(target=self._worker, name=f"du-job-{i}", daemon=True)
            for i in range(max(1, workers))
        ]
        for t in self._threads:
            t.start()

    # ------------------------------------------------------------------ events

    def subscribe(self) -> queue.Queue:
        """Return a new queue that receives every event published from now on."""
        q: queue.Queue = queue.Queue(maxsize=MAX_BACKLOG)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Stop sending events to ``q`` (a no-op if it is not subscribed)."""
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def notify(self, message: str) -> None:
        """Publish ``{"event": "notice", "message": message}`` to every subscriber.

        .. versionadded:: 3.4.0
        """
        self._publish({"event": "notice", "message": message})

    def _publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                self.unsubscribe(q)
                with q.mutex:
                    q.queue.clear()
                q.put_nowait(None)

    # -------------------------------------------------------------- breakdowns

    def submit(self, kind: str, path: str, policy: RefreshPolicy) -> bool:
        """Queue a job unless the same ``(kind, path)`` is already queued or running.

        :param kind: ``"breakdown"``.
        :param path: escaped Drive path.
        :param policy: the refresh policy the job runs under.
        :returns: ``True`` if queued, ``False`` if it was a duplicate or the queue is closed.
        :raises ValueError: for an unknown ``kind``.
        """
        if kind != "breakdown":
            raise ValueError(f"unknown job kind {kind!r}")
        with self._lock:
            if self._closed or (kind, path) in self._active:
                return False
            self._active[(kind, path)] = policy
        self._jobs.put((kind, path, policy))
        return True

    def pending(self) -> list:
        """Return the paths of the jobs queued or running, in submission order."""
        with self._lock:
            return [path for (_kind, path) in self._active]

    def _worker(self) -> None:
        while True:
            item = self._jobs.get()
            if item is _STOP:
                return
            kind, path, policy = item
            events: list = []
            try:
                events = self._run_breakdown(path, policy)
            except Exception as exc:  # noqa: BLE001 - a job must never kill its worker
                events = [{"event": "error", "path": path, "message": str(exc) or repr(exc)}]
            # leave pending() before anyone hears the result, so a reader reacting to the
            # event never still sees the path as pending
            with self._lock:
                self._active.pop((kind, path), None)
            for ev in events:
                self._publish(ev)

    def _run_breakdown(self, path: str, policy: RefreshPolicy) -> list:
        s = self._settings()
        bd: Breakdown = self.engine.breakdown(path, policy, s.rules(), s.target)
        events = [{"event": "breakdown", "path": path, "breakdown": bd}]
        if path == "/":
            events.append({"event": "trash", "trash": self.engine.trash_summary()})
        return events

    # -------------------------------------------------------------------- walk

    def walk(self, paths: list, cancel: threading.Event) -> bool:
        """Start walking ``paths`` one after another on a new thread.

        :param paths: escaped Drive paths; each is walked with the current settings.
        :param cancel: set it to stop before the next folder.
        :returns: ``False`` if a walk is already running (or the queue is closed).
        """
        with self._lock:
            if self._walking or self._closed:
                return False
            self._walking = True
        threading.Thread(
            target=self._run_walk, args=(list(paths), cancel), name="du-walk", daemon=True
        ).start()
        return True

    def _run_walk(self, paths: list, cancel: threading.Event) -> None:
        finished = 0
        known = [0, 0]  # last reported (done, total) across all paths
        state = "done"
        message = None
        last = None
        try:
            for path in paths:
                if cancel.is_set():
                    break
                last = path
                s = self._settings()

                def tick(done: int, total: int, _base: int = finished, _path: str = path) -> None:
                    known[0], known[1] = _base + done, _base + total
                    self._publish({
                        "event": "progress", "done": known[0], "total": known[1],
                        "path": _path, "state": "running",
                    })

                finished += self.engine.walk(
                    path, s.policy(), s.rules(), target=s.target, on_progress=tick, cancel=cancel
                )
                known[0], known[1] = finished, max(finished, known[1])
            if cancel.is_set():
                state = "cancelled"
        except Exception as exc:  # noqa: BLE001
            state = "error"
            message = str(exc) or repr(exc)
            self._publish({"event": "error", "path": last or "", "message": message})
        finally:
            with self._lock:
                self._walking = False
            final = {
                "event": "progress", "done": known[0],
                "total": known[0] if state == "done" else known[1],
                "path": last or "", "state": state,
            }
            if message is not None:
                final["message"] = message
            self._publish(final)

    # ------------------------------------------------------------------- close

    def close(self) -> None:
        """Stop the workers and end every subscriber's stream (a ``None`` is queued to each)."""
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for _ in self._threads:
            self._jobs.put(_STOP)
        for q in subs:
            q.put(None)
