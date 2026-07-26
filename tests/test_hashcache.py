from __future__ import annotations

from pathlib import Path

from protonfs.hashcache import HASHCACHE_FILE_NAME, HashCache


def _mk(tmp_path: Path) -> HashCache:
    (tmp_path / ".protonfs").mkdir(exist_ok=True)
    return HashCache(tmp_path)


def test_get_miss_on_empty_cache(tmp_path: Path) -> None:
    assert _mk(tmp_path).get("a", 10, 1.0) is None


def test_set_then_get_hit(tmp_path: Path) -> None:
    hc = _mk(tmp_path)
    hc.set("a", 10, 1.5, "sha256val", "sha1val")
    assert hc.get("a", 10, 1.5) == ("sha256val", "sha1val")


def test_get_miss_on_mtime_change(tmp_path: Path) -> None:
    hc = _mk(tmp_path)
    hc.set("a", 10, 1.5, "s256", "s1")
    assert hc.get("a", 10, 2.0) is None  # mtime differs -> stale -> miss


def test_get_miss_on_size_change(tmp_path: Path) -> None:
    hc = _mk(tmp_path)
    hc.set("a", 10, 1.5, "s256", "s1")
    assert hc.get("a", 11, 1.5) is None  # size differs -> miss


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    hc = _mk(tmp_path)
    hc.set("dir/f", 42, 3.25, "abc", "def")
    hc.save()
    assert (tmp_path / ".protonfs" / HASHCACHE_FILE_NAME).exists()
    reloaded = HashCache(tmp_path)
    assert reloaded.get("dir/f", 42, 3.25) == ("abc", "def")


def test_save_is_noop_when_not_dirty(tmp_path: Path) -> None:
    hc = _mk(tmp_path)
    hc.save()  # nothing set -> no file written
    assert not (tmp_path / ".protonfs" / HASHCACHE_FILE_NAME).exists()


def test_corrupt_cache_is_treated_as_empty(tmp_path: Path) -> None:
    (tmp_path / ".protonfs").mkdir()
    (tmp_path / ".protonfs" / HASHCACHE_FILE_NAME).write_text("{ not json")
    hc = HashCache(tmp_path)  # must not raise
    assert hc.get("a", 1, 1.0) is None


def test_wrong_schema_is_ignored(tmp_path: Path) -> None:
    (tmp_path / ".protonfs").mkdir()
    (tmp_path / ".protonfs" / HASHCACHE_FILE_NAME).write_text(
        '{"schema_version": 999, "entries": {"a": [1, 1.0, "x", "y"]}}'
    )
    hc = HashCache(tmp_path)
    assert hc.get("a", 1, 1.0) is None  # unknown schema -> rebuild from scratch
