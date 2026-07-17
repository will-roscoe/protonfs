"""Curated, level-gated progress narration for the CLI (the Reporter layer).

Commands call a :class:`Reporter` at phase boundaries (``phase``/``progress``/``item``/
``warn``/``done``); it renders human lines to stderr, gated by verbosity level and
throttled so ``-v`` stays readable, and mirrors every call to the ``protonfs.events``
logger so the event-log file captures the narration even when the console is quiet.
See ``.claude`` spec 2026-07-17-verbosity-and-event-log.
"""
from __future__ import annotations

import contextlib
import logging
import sys
import time

# Seconds between rendered progress updates, by verbosity level (0 = every update).
THROTTLE = {0: 0.0, 1: 30.0, 2: 5.0, 3: 1.0, 4: 0.0}
_MAX_LEVEL = 4

_events_logger = logging.getLogger("protonfs.events")


def _fields(fields: dict) -> str:
    """Render ``key=value`` trailing fields (sorted, space-joined); empty when none."""
    return " ".join(f"{k}={v}" for k, v in fields.items())


class Reporter:
    """Renders verbosity-gated narration to a stream and mirrors it to the event log.

    :param level: verbosity 0–4 (clamped); 0 is silent on the stream but still logs.
    :param progress_style: ``"inline"`` (rewrite one line) or ``"lines"`` (new line each).
    :param stream: where human lines go (defaults to real stderr).
    :param isatty: force TTY-ness for the inline/lines decision (defaults to the
        stream's own ``isatty()``); a non-TTY always uses ``"lines"``.
    """

    def __init__(self, level, *, progress_style="inline", stream=None, isatty=None):
        self.level = max(0, min(int(level), _MAX_LEVEL))
        self._stream = stream if stream is not None else sys.stderr
        tty = isatty if isatty is not None else getattr(self._stream, "isatty", lambda: False)()
        self._inline = progress_style == "inline" and tty
        self._interval = THROTTLE[self.level]
        # None = no progress rendered yet: the first poll must always render, and
        # monotonic()'s arbitrary epoch (can be < the interval right after boot) must
        # never be able to suppress it.
        self._last_progress: float | None = None
        self._progress_open = False  # an inline progress line awaits a newline

    def _close_progress(self) -> None:
        """End an open inline progress line so the next output starts cleanly."""
        if self._progress_open:
            self._stream.write("\n")
            self._progress_open = False

    def _emit(self, text: str) -> None:
        """Write a normal (non-progress) line, closing any open progress line first."""
        self._close_progress()
        self._stream.write(text + "\n")
        self._stream.flush()

    def phase(self, name: str, **fields) -> None:
        """Announce a new phase (e.g. ``"downloading"``). Rendered at level >= 1."""
        _events_logger.info("%s %s", name, _fields(fields))
        if self.level >= 1:
            suffix = f" {_fields(fields)}" if fields else ""
            self._emit(f"{name}{suffix}")

    def progress(self, done: int, total: int, *, force: bool = False, **fields) -> None:
        """Report throttled progress. Rendered at level >= 1; ``force`` bypasses the
        throttle (used at phase completion)."""
        pct = int(done / total * 100) if total else 100
        _events_logger.debug("progress %s/%s (%s%%) %s", done, total, pct, _fields(fields))
        if self.level < 1:
            return
        now = time.monotonic()
        if (
            not force
            and self._interval
            and self._last_progress is not None
            and (now - self._last_progress) < self._interval
        ):
            return
        self._last_progress = now
        line = f"{done}/{total} ({pct}%)"
        if fields:
            line += f" {_fields(fields)}"
        if self._inline:
            self._stream.write("\r" + line)
            self._stream.flush()
            self._progress_open = True
        else:
            self._stream.write(line + "\n")
            self._stream.flush()

    def item(self, action: str, path: str) -> None:
        """Report a single transferred/affected item. Rendered at level >= 2."""
        _events_logger.debug("%s %s", action, path)
        if self.level >= 2:
            self._emit(f"  {action} {path}")

    def warn(self, msg: str) -> None:
        """Surface a warning at every level (always shown)."""
        _events_logger.warning("%s", msg)
        self._emit(f"! {msg}")

    def done(self, msg: str, **fields) -> None:
        """Announce phase/command completion. Rendered at level >= 1."""
        _events_logger.info("done %s %s", msg, _fields(fields))
        if self.level >= 1:
            suffix = f" {_fields(fields)}" if fields else ""
            self._emit(f"{msg}{suffix}")

    @contextlib.contextmanager
    def timed(self, name: str):
        """Context manager: ``phase(name)`` on enter, ``done(... took Ns)`` on exit."""
        self.phase(name)
        start = time.monotonic()
        try:
            yield
        finally:
            self.done(f"{name} done", took=f"{time.monotonic() - start:.1f}s")


def null_reporter() -> Reporter:
    """A silent level-0 Reporter (still forwards warnings + to the event log)."""
    return Reporter(0)


_current: Reporter = null_reporter()


def get_reporter() -> Reporter:
    """Return the process-wide Reporter (a null one until :func:`set_reporter`)."""
    return _current


def set_reporter(reporter: Reporter) -> None:
    """Install the process-wide Reporter (called by the CLI group callback)."""
    global _current
    _current = reporter
