"""Background Drive work for ``du --gui``: deduplicated breakdown jobs and one walk at a time.

Request threads never call Drive; they queue work here and the page hears the results as
events. Events are plain dicts carrying Python objects (``{"event": "breakdown", "path": p,
"breakdown": <Breakdown>}``); :class:`~protonfs.webui.api.Api` turns them into JSON.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import queue
import threading
import time
from typing import TYPE_CHECKING, Callable

from protonfs.usage.model import Breakdown, NodeState

if TYPE_CHECKING:
    from protonfs.usage.engine import UsageEngine
    from protonfs.usage.policy import RefreshPolicy

    from .settings import GuiSettings

_STOP = object()


def _flat(text) -> str:
    return str(text).replace("\n", "\\n").replace("\r", "\\r")


def _describe(policy) -> str:
    return "never" if policy.never else "0" if policy.fresh_only else "threshold"

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
      ``done`` and ``total`` (``error`` adds ``message``). The last event also carries
      ``failed``: how many folders could not be measured (their subtrees were not walked).
    - ``error``: ``path`` and ``message`` for a job that raised, or for a folder a walk
      could not measure; the work carries on.

    :param engine: the usage engine the jobs run on.
    :param settings: returns the current :class:`~protonfs.webui.settings.GuiSettings`; read
        at the start of every job, so live edits apply to the next job.
    :param workers: number of breakdown worker threads.
    :param log: called with one line of text for each job queued, started, finished or
        failed, each walk start and end, each walk error and each notice; ``None`` for no
        logging. Called from any thread.

    A subscriber whose queue holds :data:`MAX_BACKLOG` unread events is dropped: its queue
    receives a final ``None`` after the backlog is discarded.

    .. versionadded:: 3.4.0
    """

    def __init__(
        self,
        engine: UsageEngine,
        settings: Callable[[], GuiSettings],
        workers: int = 2,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.engine = engine
        self._settings = settings
        self._log_fn = log
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue()
        self._active: dict = {}
        self._running: set = set()
        self._rerun: dict = {}
        self._tried: set = set()
        self._subs: list = []
        self._walking = False
        self._closed = False
        self._threads = [
            threading.Thread(target=self._worker, name=f"du-job-{i}", daemon=True)
            for i in range(max(1, workers))
        ]
        for t in self._threads:
            t.start()

    def _log(self, text: str) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(text)
            except Exception:  # noqa: BLE001 - logging must never break a job
                pass

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
        self._log(f"notice: {message}")
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

    def submit(
        self, kind: str, path: str, policy: RefreshPolicy, *, force: bool = False
    ) -> bool:
        """Queue a job unless the same ``(kind, path)`` is already queued or running.

        With ``force`` (a user's "refresh now") a duplicate is not dropped: a queued job is
        upgraded to run under ``policy``, and a running one is followed by a second run
        under ``policy``, so the refresh always starts after this call.

        :param kind: ``"breakdown"``.
        :param path: escaped Drive path.
        :param policy: the refresh policy the job runs under.
        :param force: make sure a run under ``policy`` starts after this call, even when the
            path is already queued or running.
        :returns: ``True`` if a job was queued (or upgraded, or a re-run queued), ``False``
            if it was a duplicate (without ``force``) or the queue is closed.
        :raises ValueError: for an unknown ``kind``.

        .. versionchanged:: 3.4.0
           Added ``force``.
        """
        if kind != "breakdown":
            raise ValueError(f"unknown job kind {kind!r}")
        key = (kind, path)
        with self._lock:
            if self._closed:
                return False
            if key in self._active:
                if not force:
                    return False
                if key in self._running:
                    self._rerun[key] = policy
                else:
                    self._active[key] = policy
                how = "after the running job" if key in self._running else "upgrading the queued job"
                self._log(f"job queued {path} refresh={_describe(policy)} (forced, {how})")
                return True
            self._active[key] = policy
        self._log(f"job queued {path} refresh={_describe(policy)}")
        self._jobs.put((kind, path, policy))
        return True

    def pending(self) -> list:
        """Return the paths of the jobs queued or running, in submission order."""
        with self._lock:
            return [path for (_kind, path) in self._active]

    def tried(self, path: str) -> bool:
        """Return whether a breakdown job for ``path`` has finished, either way, since start."""
        with self._lock:
            return path in self._tried

    def _worker(self) -> None:
        while True:
            item = self._jobs.get()
            if item is _STOP:
                return
            kind, path, policy = item
            key = (kind, path)
            with self._lock:
                policy = self._active.get(key, policy)  # a forced submit may have upgraded it
                self._running.add(key)
            events: list = []
            start = time.monotonic()
            self._log(f"job started {path} refresh={_describe(policy)}")
            try:
                events = self._run_breakdown(path, policy)
                took = time.monotonic() - start
                bd = events[0].get("breakdown")
                if bd is not None and bd.state is NodeState.ERROR:
                    self._log(f"job failed {path} in {took:.2f}s: {_flat(bd.error)}")
                else:
                    self._log(f"job finished {path} in {took:.2f}s")
            except Exception as exc:  # noqa: BLE001 - a job must never kill its worker
                msg = str(exc) or repr(exc)
                events = [{"event": "error", "path": path, "message": msg}]
                self._log(f"job failed {path} in {time.monotonic() - start:.2f}s: {_flat(msg)}")
            # leave pending() before anyone hears the result, so a reader reacting to the
            # event never still sees the path as pending (unless a forced re-run follows)
            with self._lock:
                self._running.discard(key)
                again = self._rerun.pop(key, None)
                if again is None:
                    self._active.pop(key, None)
                else:
                    self._active[key] = again
                self._tried.add(path)
            for ev in events:
                self._publish(ev)
            if again is not None:
                self._jobs.put((kind, path, again))

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
        failed = [0]
        self._log(f"walk started {', '.join(paths)}")
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

                def failed_one(fpath: str, text: str) -> None:
                    failed[0] += 1
                    self._log(f"walk error {fpath}: {_flat(text)}")
                    self._publish({"event": "error", "path": fpath, "message": text})

                finished += self.engine.walk(
                    path, s.policy(), s.rules(), target=s.target, on_progress=tick,
                    cancel=cancel, on_error=failed_one,
                )
                known[0], known[1] = finished, max(finished, known[1])
            if cancel.is_set():
                state = "cancelled"
        except Exception as exc:  # noqa: BLE001
            state = "error"
            message = str(exc) or repr(exc)
            self._log(f"walk failed at {last or ''}: {_flat(message)}")
            self._publish({"event": "error", "path": last or "", "message": message})
        finally:
            with self._lock:
                self._walking = False
            final = {
                "event": "progress", "done": known[0],
                "total": known[0] if state == "done" else known[1],
                "path": last or "", "state": state, "failed": failed[0],
            }
            if message is not None:
                final["message"] = message
            self._log(f"walk {state}: {known[0]} folders, {failed[0]} failed")
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
