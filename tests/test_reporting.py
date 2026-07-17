# tests/test_reporting.py
from __future__ import annotations

import io
import logging

from protonfs.reporting import Reporter, get_reporter, null_reporter, set_reporter


def _reporter(level, style="lines", isatty=False):
    return Reporter(level, progress_style=style, stream=io.StringIO(), isatty=isatty)


def test_level_zero_is_silent_on_stream() -> None:
    r = _reporter(0)
    r.phase("indexing")
    r.progress(1, 10)
    r.item("download", "a/b.txt")
    r.done("done")
    assert r._stream.getvalue() == ""


def test_level_one_shows_phase_and_done_but_not_items() -> None:
    r = _reporter(1)
    r.phase("downloading", files=200)
    r.item("download", "a/b.txt")   # items only at >=2
    r.done("downloaded 200 files")
    out = r._stream.getvalue()
    assert "downloading" in out
    assert "downloaded 200 files" in out
    assert "a/b.txt" not in out


def test_level_two_shows_items() -> None:
    r = _reporter(2)
    r.item("download", "a/b.txt")
    assert "a/b.txt" in r._stream.getvalue()


def test_progress_throttled_within_interval_but_forced_on_done(monkeypatch) -> None:
    r = _reporter(1)  # interval 30s
    t = [1000.0]
    monkeypatch.setattr("protonfs.reporting.time.monotonic", lambda: t[0])
    r.progress(1, 10)      # first always renders
    r.progress(2, 10)      # within 30s -> suppressed
    first = r._stream.getvalue()
    assert first.count("\n") == 1
    t[0] += 31
    r.progress(3, 10)      # now past interval -> renders
    assert r._stream.getvalue().count("\n") == 2


def test_lines_style_appends_newlines() -> None:
    r = _reporter(1, style="lines")
    r.progress(1, 10)
    r.progress(2, 10, force=True)
    out = r._stream.getvalue()
    assert "\r" not in out
    assert out.count("\n") == 2


def test_inline_style_uses_carriage_return_on_tty() -> None:
    r = _reporter(1, style="inline", isatty=True)
    r.progress(1, 10)
    r.progress(2, 10, force=True)
    out = r._stream.getvalue()
    assert "\r" in out  # rewrites the same line


def test_non_tty_forces_lines_even_when_inline_requested() -> None:
    r = _reporter(1, style="inline", isatty=False)
    r.progress(1, 10)
    r.progress(2, 10, force=True)
    assert "\r" not in r._stream.getvalue()


def test_open_progress_line_closed_before_other_output() -> None:
    r = _reporter(2, style="inline", isatty=True)
    r.progress(1, 10)          # opens an inline progress line
    r.item("download", "a.txt")  # must close it with a newline first
    out = r._stream.getvalue()
    # the progress line is terminated before the item line
    idx_nl = out.index("\n")
    assert out.index("a.txt") > idx_nl


def test_emits_to_events_logger(caplog) -> None:
    r = _reporter(1)
    with caplog.at_level(logging.DEBUG, logger="protonfs.events"):
        r.phase("indexing", files=5)
    assert any("indexing" in rec.getMessage() for rec in caplog.records)


def test_get_set_reporter_roundtrip() -> None:
    assert get_reporter().level == 0  # default null
    r = _reporter(3)
    set_reporter(r)
    assert get_reporter() is r
    set_reporter(null_reporter())  # reset for other tests


def test_timed_reports_duration() -> None:
    r = _reporter(1)
    with r.timed("scanning"):
        pass
    assert "scanning" in r._stream.getvalue()


def test_warn_renders_at_every_level_including_zero() -> None:
    # The contract's most distinctive rule: warnings are never gated by level.
    for level in (0, 1, 4):
        r = _reporter(level)
        r.warn("something odd")
        assert "something odd" in r._stream.getvalue(), f"warn hidden at level {level}"


def test_first_progress_always_renders(monkeypatch) -> None:
    # monotonic()'s epoch is arbitrary (can be < the 30s interval right after boot);
    # the first poll must render regardless of the clock's absolute value.
    monkeypatch.setattr("protonfs.reporting.time.monotonic", lambda: 3.0)
    r = _reporter(1)
    r.progress(1, 10)
    assert "1/10" in r._stream.getvalue()
