from __future__ import annotations

import json
from pathlib import Path

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
