from __future__ import annotations

import json
from pathlib import Path

import pytest

from protonfs import index as index_mod
from protonfs.index import IndexEntry, IndexStore


def _entry(**overrides) -> IndexEntry:
    defaults = dict(
        size=100,
        mtime=123.0,
        sha256="abc",
        remote_path="/my-files/x/f",
        origin_device="dev-1",
        local_state="present",
        last_synced="2026-07-08T00:00:00+00:00",
    )
    defaults.update(overrides)
    return IndexEntry(**defaults)


def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = IndexStore(tmp_path)
    assert store.get("nope") is None


def test_set_then_get_round_trips_in_memory(tmp_path: Path) -> None:
    store = IndexStore(tmp_path)
    entry = _entry()
    store.set("a/b", entry)
    assert store.get("a/b") == entry


def test_save_then_reload_from_disk(tmp_path: Path) -> None:
    store = IndexStore(tmp_path)
    store.set("a/b", _entry())
    store.save()

    on_disk = json.loads((tmp_path / ".protonfs" / "index.json").read_text())
    assert "a/b" in on_disk

    reloaded = IndexStore(tmp_path)
    assert reloaded.get("a/b") == _entry()


def test_remove_deletes_entry(tmp_path: Path) -> None:
    store = IndexStore(tmp_path)
    store.set("a/b", _entry())
    store.remove("a/b")
    assert store.get("a/b") is None


def test_all_returns_copy_not_internal_reference(tmp_path: Path) -> None:
    store = IndexStore(tmp_path)
    store.set("a/b", _entry())
    snapshot = store.all()
    snapshot["a/b"] = None
    assert store.get("a/b") is not None


def test_save_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    store = IndexStore(tmp_path)
    store.set("a/b", _entry())
    store.save()
    contents = {p.name for p in (tmp_path / ".protonfs").iterdir()}
    assert contents == {"index.json"}


def test_save_is_atomic_original_survives_failed_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Write a good v1 index.
    store = IndexStore(tmp_path)
    store.set("a/b", _entry(size=1))
    store.save()

    # Now mutate and make the atomic swap fail partway through the next save.
    store.set("a/b", _entry(size=999))

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(index_mod.os, "replace", boom)
    with pytest.raises(OSError):
        store.save()

    # The on-disk index must still be the intact v1 — never torn or truncated.
    on_disk = json.loads((tmp_path / ".protonfs" / "index.json").read_text())
    assert on_disk["a/b"]["size"] == 1

    # And the failed write must not leave a temp file lying around.
    contents = {p.name for p in (tmp_path / ".protonfs").iterdir()}
    assert contents == {"index.json"}


def test_save_swaps_via_os_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard that saving goes through an atomic os.replace, not a plain in-place write.
    calls: list[tuple] = []
    real_replace = index_mod.os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(index_mod.os, "replace", spy)
    store = IndexStore(tmp_path)
    store.set("a/b", _entry())
    store.save()

    assert len(calls) == 1
    src, dst = calls[0]
    assert dst.endswith("index.json")
    assert src != dst  # replaced from a distinct temp file
