"""Tests for the SQLite usage cache."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from protonfs.usage import cache as cache_mod
from protonfs.usage.cache import (
    Figure,
    Row,
    Snapshot,
    UsageCache,
    default_cache_path,
)


def _row(name, kind="file", uid=None, size=1, desc=None, at=None, pass_id=None):
    return Row(name, kind, uid, size, desc, at, pass_id, "2026-01-01", True)


def _snap(path="/a", rows=None, pass_id=1, at=10.0, uid="U-a"):
    if rows is None:
        rows = (
            _row("z.bin", size=5),
            _row("sub", "folder", "U-sub", 100, 7, 10.0, pass_id),
            _row("a/b", "folder", "U-ab", 2),
        )
    return Snapshot(path, uid, at, pass_id, (123, 4), 3, False, tuple(rows))


@pytest.fixture()
def cache(tmp_path):
    c = UsageCache(tmp_path / "sub" / "du.sqlite")
    yield c
    c.close()


def test_figure_roundtrip(cache):
    assert cache.figure("/a") is None
    cache.put_figure("/a", Figure(10, 3, 5.0, 1))
    assert cache.figure("/a") == Figure(10, 3, 5.0, 1)
    cache.put_figure("/a", Figure(None, None, None, None))
    assert cache.figure("/a") == Figure(None, None, None, None)


def test_snapshot_roundtrip_preserves_row_order_and_escaped_names(cache):
    s = _snap()
    cache.put_snapshot(s)
    got = cache.snapshot("/a")
    assert got == s
    assert [r.name for r in got.rows] == ["z.bin", "sub", "a/b"]
    assert cache.snapshot("/missing") is None


def test_put_snapshot_is_atomic(cache, monkeypatch):
    old = _snap()
    cache.put_snapshot(old)
    calls = []
    real = cache_mod.UsageCache._insert_row

    def flaky(self, conn, parent, ord_, row):
        calls.append(ord_)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(self, conn, parent, ord_, row)

    monkeypatch.setattr(cache_mod.UsageCache, "_insert_row", flaky)
    new = _snap(rows=(_row("x"), _row("y"), _row("w")), pass_id=2, at=20.0)
    with pytest.raises(RuntimeError):
        cache.put_snapshot(new)
    monkeypatch.undo()
    assert cache.snapshot("/a") == old
    cache.put_snapshot(new)
    assert cache.snapshot("/a") == new


def test_restamp_updates_times_not_values(cache):
    cache.put_snapshot(_snap())
    cache.restamp("/a", 9, 99.0)
    got = cache.snapshot("/a")
    assert (got.pass_id, got.at) == (9, 99.0)
    folder = got.rows[1]
    assert (folder.size, folder.descendants) == (100, 7)
    assert (folder.at, folder.pass_id) == (99.0, 9)
    assert got.fingerprint == (123, 4)


def test_invalidate(cache):
    cache.put_snapshot(_snap())
    cache.invalidate("/a")
    assert cache.snapshot("/a").invalid is True


def test_path_for_uid(cache):
    cache.put_snapshot(_snap(rows=(_row("we/ird\\", "folder", "U-w"), _row("f", uid="U-f"))))
    assert cache.path_for_uid("U-w") == "/a/we\\/ird\\\\"
    assert cache.path_for_uid("U-a") == "/a"
    cache.put_snapshot(_snap())
    assert cache.path_for_uid("U-ab") == "/a/a\\/b"
    assert cache.path_for_uid("U-f") is None
    assert cache.path_for_uid("nope") is None


def test_schema_version_mismatch_rebuilds(tmp_path):
    p = tmp_path / "du.sqlite"
    c = UsageCache(p)
    c.put_figure("/a", Figure(1, 1, 1.0, 1))
    c.set_meta("k", "v")
    c.close()
    raw = sqlite3.connect(p)
    raw.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    raw.commit()
    raw.close()
    c = UsageCache(p)
    assert c.figure("/a") is None
    assert c.get_meta("k") is None
    c.put_figure("/a", Figure(2, 2, 2.0, 2))
    c.close()


def test_meta_trash_passes_clear(cache):
    assert cache.get_meta("x") is None
    cache.set_meta("x", "1")
    cache.set_meta("x", "2")
    assert cache.get_meta("x") == "2"
    assert cache.trash() is None
    cache.put_trash("{}", 1.0)
    cache.put_trash('{"a": 1}', 2.0)
    assert cache.trash() == ('{"a": 1}', 2.0)
    a, b = cache.new_pass(1.0), cache.new_pass(2.0)
    assert b > a
    cache.put_figure("/a", Figure(1, 1, 1.0, a))
    cache.clear()
    assert cache.figure("/a") is None and cache.trash() is None
    assert cache.new_pass(3.0) >= 1


def test_env_override_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTONFS_DU_CACHE", str(tmp_path / "x.sqlite"))
    assert default_cache_path() == tmp_path / "x.sqlite"
    monkeypatch.delenv("PROTONFS_DU_CACHE")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_path() == tmp_path / "xdg" / "protonfs" / "du.sqlite"
    assert not (tmp_path / "xdg").exists()
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert default_cache_path() == Path.home() / ".cache" / "protonfs" / "du.sqlite"


def test_threads_share_one_cache(cache):
    errors = []

    def work(i):
        try:
            for j in range(30):
                cache.put_figure(f"/t{i}/{j}", Figure(j, j, 1.0, i))
                assert cache.figure(f"/t{i}/{j}").size == j
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []


def test_locked_database_is_not_wiped(tmp_path):
    p = tmp_path / "du.sqlite"
    c = UsageCache(p)
    c.put_figure("/a", Figure(1, 1, 1.0, 1))
    c.close()
    raw = sqlite3.connect(p, isolation_level=None)
    raw.execute("BEGIN IMMEDIATE")
    try:
        try:
            c2 = UsageCache(p, timeout=0.2)
        except sqlite3.OperationalError:
            c2 = None
    finally:
        raw.execute("ROLLBACK")
        raw.close()
    if c2 is not None:
        c2.close()
    c3 = UsageCache(p)
    assert c3.figure("/a") == Figure(1, 1, 1.0, 1)
    c3.close()
