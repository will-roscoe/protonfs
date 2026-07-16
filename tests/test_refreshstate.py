from __future__ import annotations

from pathlib import Path

from protonfs import refreshstate


def test_save_then_load_roundtrips_frontier(tmp_path: Path) -> None:
    (tmp_path / ".protonfs").mkdir()
    frontier = [("/my-files/test/run1", "run1/"), ("/my-files/test/run2", "run2/")]

    refreshstate.save_frontier(tmp_path, "/my-files/test", frontier)
    loaded = refreshstate.load_frontier(tmp_path, "/my-files/test")

    assert loaded == frontier  # tuples restored


def test_load_returns_none_for_a_different_root(tmp_path: Path) -> None:
    (tmp_path / ".protonfs").mkdir()
    refreshstate.save_frontier(tmp_path, "/my-files/test", [("/my-files/test/a", "a/")])

    # A frontier saved for a different pass root is stale for this one.
    assert refreshstate.load_frontier(tmp_path, "/my-files/other") is None


def test_load_returns_none_when_absent(tmp_path: Path) -> None:
    assert refreshstate.load_frontier(tmp_path, "/my-files/test") is None


def test_clear_removes_state(tmp_path: Path) -> None:
    (tmp_path / ".protonfs").mkdir()
    refreshstate.save_frontier(tmp_path, "/my-files/test", [("/x", "")])

    refreshstate.clear(tmp_path)

    assert refreshstate.load_frontier(tmp_path, "/my-files/test") is None
    # clear() is idempotent -- a second call on an absent file does not raise.
    refreshstate.clear(tmp_path)
