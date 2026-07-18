"""Wire the console + event-log handlers and build the process Reporter.

Console verbosity and the event-log file are independent sinks (spec): ``-v`` sets the
console threshold, while the event log -- when enabled -- always records the full
``protonfs`` logger tree at DEBUG. Level 4 additionally ungags third-party loggers and
turns on proton-drive subprocess passthrough (read via :func:`backend_passthrough_enabled`).

.. versionadded:: 1.3.0
"""
from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from protonfs.reporting import Reporter, set_reporter

EVENT_LOG_NAME = "events.log"
EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024
EVENT_LOG_BACKUPS = 1
_ALIGNED_FMT = "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"
_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"

_ROOT = "protonfs"
_EVENTS_LOGGER_NAME = "protonfs.events"
_backend_passthrough = False


class _DropEventsFilter(logging.Filter):
    """Drop ``protonfs.events`` records -- attached to the CONSOLE handler only.

    :class:`~protonfs.reporting.Reporter` renders those events itself as human
    narration; letting the console handler also render the logger's own formatted
    line double-prints every ``warn()`` call. The event-log FILE handler does not
    get this filter -- it must keep receiving the full event stream.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep every record except those from the ``protonfs.events`` logger tree."""
        return not (
            record.name == _EVENTS_LOGGER_NAME
            or record.name.startswith(f"{_EVENTS_LOGGER_NAME}.")
        )


def _console_level(verbosity: int) -> int:
    """Map a ``-v`` count to the console logging threshold (WARNING/INFO/DEBUG)."""
    if verbosity >= 4:
        return logging.DEBUG
    if verbosity == 3:
        return logging.INFO
    return logging.WARNING


def backend_passthrough_enabled() -> bool:
    """Whether ``drive.py`` should stream the proton-drive subprocess stderr (level 4)."""
    return _backend_passthrough


def _make_formatter() -> logging.Formatter:
    """The aligned-text formatter (UTC ISO-8601 timestamp, padded level + component)."""
    fmt = logging.Formatter(_ALIGNED_FMT, datefmt=_DATE_FMT)
    fmt.converter = time.gmtime  # UTC timestamps
    return fmt


def configure_logging(
    verbosity: int, *, progress_style: str, event_log: bool, root: Path, stream=None
) -> Reporter:
    """Configure handlers + build/install the Reporter for this invocation.

    :param verbosity: ``-v`` count (0–4).
    :param progress_style: ``"inline"`` | ``"lines"`` for the Reporter.
    :param event_log: when true, attach a rotating DEBUG file handler under ``.protonfs/``.
    :param root: the repo root whose ``.protonfs/`` holds the event log.
    :param stream: console/reporter stream override (tests); defaults to real stderr.
    :returns: the installed :class:`Reporter`.
    """
    global _backend_passthrough
    _backend_passthrough = verbosity >= 4

    root_logger = logging.getLogger(_ROOT)
    root_logger.handlers.clear()
    root_logger.propagate = False

    console = logging.StreamHandler(stream)
    console.setLevel(_console_level(verbosity))
    console.setFormatter(_make_formatter())
    console.addFilter(_DropEventsFilter())
    root_logger.addHandler(console)

    # Only attach the file handler when .protonfs/ already exists -- never mkdir it
    # ourselves. Otherwise `protonfs --event-log <cmd>` run outside any repo (or in
    # one not yet `setup`) would create a stray .protonfs/ wherever the process
    # happens to be standing.
    event_log_attached = False
    if event_log and (root / ".protonfs").is_dir():
        file_handler = RotatingFileHandler(
            root / ".protonfs" / EVENT_LOG_NAME,
            maxBytes=EVENT_LOG_MAX_BYTES,
            backupCount=EVENT_LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(_make_formatter())
        root_logger.addHandler(file_handler)
        event_log_attached = True

    # The logger only needs to pass DEBUG-level records through when something
    # actually wants them (the event-log file, or -vvvv's third-party passthrough);
    # otherwise every reporter.item()/progress() DEBUG call on a large batch would
    # build a LogRecord that no handler ever renders.
    root_logger.setLevel(
        logging.DEBUG if (event_log_attached or verbosity >= 4) else _console_level(verbosity)
    )

    if verbosity >= 4:
        # Ungag third-party loggers. Deliberately never re-gagged on a later, lower-
        # verbosity configure call: the CLI configures exactly once per process.
        logging.getLogger().setLevel(logging.DEBUG)

    reporter = Reporter(verbosity, progress_style=progress_style, stream=stream)
    set_reporter(reporter)
    return reporter
