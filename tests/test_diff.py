# tests/test_diff.py
from __future__ import annotations

from protonfs.diff import SyncState, classify
from protonfs.index import IndexEntry, IndexStore
from protonfs.localscan import ScanEntry


def _index_entry(sha256: str, local_state: str = "present") -> IndexEntry:
    return IndexEntry(
        size=1,
        mtime=1.0,
        sha256=sha256,
        remote_path="/x/a",
        origin_device="d1",
        local_state=local_state,
        last_synced="2026-07-08T00:00:00+00:00",
    )


def _scan_entry(sha256: str) -> ScanEntry:
    return ScanEntry(rel_path="a", size=1, mtime=1.0, sha256=sha256)


def test_local_only_when_not_in_index(tmp_path) -> None:
    index = IndexStore(tmp_path)
    local = {"a": _scan_entry("h1")}
    result = classify(local, index)
    diff_entry = __import__("protonfs.diff", fromlist=["DiffEntry"]).DiffEntry
    assert result == [diff_entry("a", SyncState.LOCAL_ONLY)]


def test_synced_when_hashes_match(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1"))
    local = {"a": _scan_entry("h1")}
    result = classify(local, index)
    assert result[0].state == SyncState.SYNCED


def test_conflict_when_hashes_differ(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1"))
    local = {"a": _scan_entry("h2")}
    result = classify(local, index)
    assert result[0].state == SyncState.CONFLICT


def test_metadata_only_when_index_says_metadata_only_and_no_local_file(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", local_state="metadata-only"))
    result = classify({}, index)
    assert result[0].state == SyncState.METADATA_ONLY


def test_remote_only_when_index_says_present_but_local_file_missing(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", local_state="present"))
    result = classify({}, index)
    assert result[0].state == SyncState.REMOTE_ONLY


def test_remote_only_from_live_listing_not_yet_in_index(tmp_path) -> None:
    index = IndexStore(tmp_path)
    result = classify({}, index, remote={"a": 10})
    assert result[0].state == SyncState.REMOTE_ONLY


def test_local_only_file_never_touched_when_absent_from_remote_listing(tmp_path) -> None:
    # Direct regression test for the spec's core safety property: a file
    # unique to the local side must classify as LOCAL_ONLY, never as
    # something that would cause it to be deleted or skipped as "not real".
    index = IndexStore(tmp_path)
    local = {"local_only.txt": _scan_entry("h1")}
    result = classify(local, index, remote={"other_file": 5})
    assert result[0].rel_path == "local_only.txt"
    assert result[0].state == SyncState.LOCAL_ONLY


def test_remote_deleted_when_index_entry_absent_from_remote(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", local_state="metadata-only"))
    result = classify({}, index, remote={})  # remote walk ran, 'a' is gone
    assert result[0].state == SyncState.REMOTE_DELETED


def test_remote_changed_when_size_differs(tmp_path) -> None:
    index = IndexStore(tmp_path)
    # _index_entry builds size=1 (see helper); remote reports a different size
    index.set("a", _index_entry("h1", local_state="metadata-only"))
    result = classify({}, index, remote={"a": 999})
    assert result[0].state == SyncState.REMOTE_CHANGED


def test_metadata_only_preserved_when_remote_size_matches(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", local_state="metadata-only"))
    result = classify({}, index, remote={"a": 1})  # matches _index_entry size=1
    assert result[0].state == SyncState.METADATA_ONLY


def test_no_remote_view_keeps_v01_behavior(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", local_state="present"))
    result = classify({}, index, remote=None)  # index says present, no local file
    assert result[0].state == SyncState.REMOTE_ONLY
