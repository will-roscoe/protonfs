"""Wire the console + event-log handlers and build the process Reporter.

Console verbosity and the event-log file are independent sinks (spec): ``-v`` sets the
console threshold, while the event log -- when enabled -- always records the full
``protonfs`` logger tree at DEBUG. Level 4 additionally ungags third-party loggers and
turns on proton-drive subprocess passthrough (read via :func:`backend_passthrough_enabled`).
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
_ALIGNED_FMT = "%(asctime)s %(levelname)-5s %(name)-24s %(message)s"
_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"

_ROOT = "protonfs"
_backend_passthrough = False


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
    root_logger.setLevel(logging.DEBUG)  # handlers filter; logger passes everything
    root_logger.handlers.clear()
    root_logger.propagate = False

    console = logging.StreamHandler(stream)
    console.setLevel(_console_level(verbosity))
    console.setFormatter(_make_formatter())
    root_logger.addHandler(console)

    if event_log:
        (root / ".protonfs").mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            root / ".protonfs" / EVENT_LOG_NAME,
            maxBytes=EVENT_LOG_MAX_BYTES,
            backupCount=EVENT_LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(_make_formatter())
        root_logger.addHandler(file_handler)

    if verbosity >= 4:
        # Ungag third-party loggers. Deliberately never re-gagged on a later, lower-
        # verbosity configure call: the CLI configures exactly once per process.
        logging.getLogger().setLevel(logging.DEBUG)

    reporter = Reporter(verbosity, progress_style=progress_style, stream=stream)
    set_reporter(reporter)
    return reporter
