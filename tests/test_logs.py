from __future__ import annotations

import io
import logging

import pytest

from protonfs.logs import (
    EVENT_LOG_NAME,
    _console_level,
    backend_passthrough_enabled,
    configure_logging,
)
from protonfs.reporting import get_reporter, null_reporter, set_reporter


@pytest.fixture(autouse=True)
def _reset_logging():
    yield
    protonfs_logger = logging.getLogger("protonfs")
    protonfs_logger.handlers.clear()
    protonfs_logger.setLevel(logging.NOTSET)  # reset to inherit from root
    protonfs_logger.propagate = True  # reset propagation
    logging.getLogger().setLevel(logging.WARNING)  # reset root logger
    set_reporter(null_reporter())


def test_console_level_mapping() -> None:
    assert _console_level(0) == logging.WARNING
    assert _console_level(2) == logging.WARNING
    assert _console_level(3) == logging.INFO
    assert _console_level(4) == logging.DEBUG


def test_configure_returns_reporter_at_level(tmp_path) -> None:
    r = configure_logging(2, progress_style="lines", event_log=False, root=tmp_path)
    assert r.level == 2
    assert get_reporter() is r


def test_event_log_file_created_only_when_enabled(tmp_path) -> None:
    (tmp_path / ".protonfs").mkdir()
    configure_logging(1, progress_style="lines", event_log=False, root=tmp_path)
    logging.getLogger("protonfs.events").info("hello")
    assert not (tmp_path / ".protonfs" / EVENT_LOG_NAME).exists()

    logging.getLogger("protonfs").handlers.clear()
    configure_logging(1, progress_style="lines", event_log=True, root=tmp_path)
    logging.getLogger("protonfs.events").info("world")
    log = tmp_path / ".protonfs" / EVENT_LOG_NAME
    assert log.exists()
    assert "world" in log.read_text()


def test_event_log_uses_aligned_format(tmp_path) -> None:
    (tmp_path / ".protonfs").mkdir()
    configure_logging(1, progress_style="lines", event_log=True, root=tmp_path)
    logging.getLogger("protonfs.events").info("start subpath=a")
    text = (tmp_path / ".protonfs" / EVENT_LOG_NAME).read_text()
    assert "INFO" in text and "start subpath=a" in text


def test_backend_passthrough_only_at_level_4(tmp_path) -> None:
    configure_logging(3, progress_style="lines", event_log=False, root=tmp_path)
    assert backend_passthrough_enabled() is False
    logging.getLogger("protonfs").handlers.clear()
    configure_logging(4, progress_style="lines", event_log=False, root=tmp_path)
    assert backend_passthrough_enabled() is True


# --- F1: a warning renders exactly once on the console -----------------------------------


def test_warn_renders_exactly_once_on_console(tmp_path) -> None:
    # Before the fix: the events-logger mirror (protonfs.events -> propagates to the
    # console handler) rendered a second, formatted line alongside the Reporter's own
    # "! msg" line. The console handler must drop protonfs.events records; the Reporter
    # is the only thing that renders them to the console.
    buf = io.StringIO()
    reporter = configure_logging(0, progress_style="lines", event_log=False, root=tmp_path,
                                  stream=buf)
    reporter.warn("x")
    lines = [line for line in buf.getvalue().split("\n") if line]
    assert len(lines) == 1
    assert "x" in lines[0]


def test_warn_still_reaches_event_log_file_despite_console_filter(tmp_path) -> None:
    (tmp_path / ".protonfs").mkdir()
    buf = io.StringIO()
    reporter = configure_logging(0, progress_style="lines", event_log=True, root=tmp_path,
                                  stream=buf)
    reporter.warn("x")
    log_text = (tmp_path / ".protonfs" / EVENT_LOG_NAME).read_text()
    assert "x" in log_text


# --- F3: --event-log outside a repo never mkdirs .protonfs/ ------------------------------


def test_event_log_true_without_existing_protonfs_dir_creates_nothing(tmp_path) -> None:
    configure_logging(1, progress_style="lines", event_log=True, root=tmp_path)
    assert not (tmp_path / ".protonfs").exists()
    # And logging still works without crashing (no handler was attached).
    logging.getLogger("protonfs.events").info("hello")


# --- F4: the "protonfs" logger only sits at DEBUG when something wants DEBUG -------------


def test_root_logger_level_skips_debug_when_no_sink_wants_it(tmp_path) -> None:
    configure_logging(0, progress_style="lines", event_log=False, root=tmp_path)
    assert logging.getLogger("protonfs.events").isEnabledFor(logging.DEBUG) is False


def test_root_logger_level_is_debug_when_event_log_attached(tmp_path) -> None:
    (tmp_path / ".protonfs").mkdir()
    configure_logging(0, progress_style="lines", event_log=True, root=tmp_path)
    assert logging.getLogger("protonfs.events").isEnabledFor(logging.DEBUG) is True


# --- F6: the level field aligns WARNING (7 chars) same as the rest -----------------------


def test_aligned_format_uses_seven_char_level_field() -> None:
    from protonfs.logs import _ALIGNED_FMT

    assert "%(levelname)-7s" in _ALIGNED_FMT
