from __future__ import annotations

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
