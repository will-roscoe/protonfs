"""``du --gui --log`` target resolution (3.4.0)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from protonfs.webui.logtarget import LogTarget, resolve_log_target

START = datetime(2026, 11, 13, 13, 44, 5)
NAME = "du-gui-2026-11-13T13-44-05.log"


def test_default_is_xdg_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = resolve_log_target(None, now=START)
    assert target == LogTarget("dir", tmp_path / "state" / "protonfs" / "logs" / NAME)


def test_default_falls_back_to_local_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    target = resolve_log_target(None, now=START)
    assert target.path == tmp_path / ".local" / "state" / "protonfs" / "logs" / NAME


def test_existing_dir_gets_start_time_file(tmp_path: Path) -> None:
    target = resolve_log_target(str(tmp_path), now=START)
    assert target == LogTarget("dir", tmp_path / NAME)


def test_trailing_slash_creates_dir(tmp_path: Path) -> None:
    new = tmp_path / "new-logs"
    target = resolve_log_target(f"{new}/", now=START)
    assert target == LogTarget("dir", new / NAME)
    with target.open() as fh:
        fh.write("hello\n")
    assert (new / NAME).read_text() == "hello\n"


def test_file_is_appended(tmp_path: Path) -> None:
    log = tmp_path / "du.log"
    log.write_text("old\n")
    target = resolve_log_target(str(log), now=START)
    assert target == LogTarget("file", log)
    with target.open() as fh:
        fh.write("new\n")
    assert log.read_text() == "old\nnew\n"


def test_character_device_written_directly() -> None:
    target = resolve_log_target("/dev/null", now=START)
    assert target == LogTarget("device", Path("/dev/null"))
    with target.open() as fh:
        fh.write("discarded\n")


def test_missing_parent_for_file_is_created(tmp_path: Path) -> None:
    target = resolve_log_target(str(tmp_path / "a" / "b.log"), now=START)
    assert target.kind == "file"
    with target.open() as fh:
        fh.write("x")
    assert (tmp_path / "a" / "b.log").read_text() == "x"
