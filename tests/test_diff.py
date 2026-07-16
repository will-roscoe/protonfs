# tests/test_diff.py
from __future__ import annotations

from protonfs.diff import DiffEntry, SyncState, classify
from protonfs.drive import RemoteEntry
from protonfs.index import IndexEntry, IndexStore
from protonfs.localscan import ScanEntry


def _index_entry(
    sha256: str,
    local_state: str = "present",
    *,
    size: int = 1,
    sha1: str = "",
) -> IndexEntry:
    return IndexEntry(
        size=size,
        mtime=1.0,
        sha256=sha256,
        sha1=sha1,
        remote_path="/x/a",
        origin_device="d1",
        local_state=local_state,
        last_synced="2026-07-08T00:00:00+00:00",
    )


def _scan_entry(sha256: str, *, size: int = 1, sha1: str = "") -> ScanEntry:
    return ScanEntry(rel_path="a", size=size, mtime=1.0, sha256=sha256, sha1=sha1)


def _remote(
    *,
    size: int = 1,
    claimed_size: int | None = None,
    sha1: str | None = None,
    rel_path: str = "a",
) -> RemoteEntry:
    return RemoteEntry(
        rel_path=rel_path,
        is_dir=False,
        size=size,
        claimed_size=claimed_size,
        sha1=sha1,
    )


def test_local_only_when_not_in_index(tmp_path) -> None:
    index = IndexStore(tmp_path)
    local = {"a": _scan_entry("h1")}
    result = classify(local, index)
    assert result == [DiffEntry("a", SyncState.LOCAL_ONLY)]


def test_synced_when_hashes_match(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1"))
    local = {"a": _scan_entry("h1")}
    result = classify(local, index)
    assert result[0].state == SyncState.SYNCED


def test_conflict_when_hashes_differ_and_no_remote_view(tmp_path) -> None:
    # No remote view: a local!=index diff cannot be attributed to a direction, so it
    # stays a conservative CONFLICT (v0.1 behaviour preserved for push/status).
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1"))
    local = {"a": _scan_entry("h2")}
    result = classify(local, index)
    assert result[0].state == SyncState.CONFLICT


def test_local_modified_when_only_local_diverged(tmp_path) -> None:
    # local != index, but the remote still matches the index -> safe to push.
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", sha1="s1", size=10))
    local = {"a": _scan_entry("h2", size=20)}
    remote = {"a": _remote(sha1="s1", claimed_size=10)}
    result = classify(local, index, remote)
    assert result[0].state == SyncState.LOCAL_MODIFIED


def test_remote_modified_when_only_remote_diverged(tmp_path) -> None:
    # local == index, but the remote diverged -> safe to pull.
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", sha1="s1", size=10))
    local = {"a": _scan_entry("h1", size=10)}
    remote = {"a": _remote(sha1="s2", claimed_size=99)}
    result = classify(local, index, remote)
    assert result[0].state == SyncState.REMOTE_MODIFIED


def test_both_modified_is_a_genuine_conflict(tmp_path) -> None:
    # Both sides diverged from the index -> genuine conflict.
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", sha1="s1", size=10))
    local = {"a": _scan_entry("h2", size=20)}
    remote = {"a": _remote(sha1="s2", claimed_size=99)}
    result = classify(local, index, remote)
    assert result[0].state == SyncState.BOTH_MODIFIED


def test_local_diverged_with_remote_gone_is_conservative_conflict(tmp_path) -> None:
    # local != index and the remote no longer has the path: we cannot prove the remote
    # side, so fall back to a conservative conflict rather than inventing a direction.
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", sha1="s1"))
    local = {"a": _scan_entry("h2")}
    result = classify(local, index, remote={})
    assert result[0].state == SyncState.CONFLICT


def test_empty_remote_sha1_falls_back_to_size_trust_on_first_use(tmp_path) -> None:
    # remote sha1 unknown (trust-on-first-use): must not force a false conflict. With
    # matching sizes and a matching local hash the file stays SYNCED.
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", sha1="", size=10))
    local = {"a": _scan_entry("h1", size=10)}
    remote = {"a": _remote(sha1=None, claimed_size=10)}
    result = classify(local, index, remote)
    assert result[0].state == SyncState.SYNCED


def test_local_modified_prefers_claimed_size_over_encrypted_size(tmp_path) -> None:
    # The remote's encrypted `size` runs larger than the plaintext `claimed_size`;
    # classify must compare against claimed_size so a matching remote is not a false
    # REMOTE_MODIFIED. Here only the local side diverged.
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", sha1="", size=10))
    local = {"a": _scan_entry("h2", size=20)}
    # encrypted size 13 != index size 10, but claimed_size 10 == index size 10.
    remote = {"a": _remote(sha1=None, size=13, claimed_size=10)}
    result = classify(local, index, remote)
    assert result[0].state == SyncState.LOCAL_MODIFIED


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


def test_local_deleted_when_present_entry_absent_locally_but_on_remote(tmp_path) -> None:
    # index recorded local_state="present"; the file is gone locally but the remote
    # still has it -> this is a LOCAL deletion, distinct from remote-only.
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", local_state="present", size=1))
    result = classify({}, index, remote={"a": _remote(claimed_size=1)})
    assert result[0].state == SyncState.LOCAL_DELETED


def test_metadata_only_absent_locally_is_not_a_local_deletion(tmp_path) -> None:
    # A metadata-only entry never had a local file, so its local absence is expected,
    # not a deletion.
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", local_state="metadata-only", size=1))
    result = classify({}, index, remote={"a": _remote(claimed_size=1)})
    assert result[0].state == SyncState.METADATA_ONLY


def test_remote_only_from_live_listing_not_yet_in_index(tmp_path) -> None:
    index = IndexStore(tmp_path)
    result = classify({}, index, remote={"a": _remote(claimed_size=10)})
    assert result[0].state == SyncState.REMOTE_ONLY


def test_local_only_file_never_touched_when_absent_from_remote_listing(tmp_path) -> None:
    # Direct regression test for the spec's core safety property: a file
    # unique to the local side must classify as LOCAL_ONLY, never as
    # something that would cause it to be deleted or skipped as "not real".
    index = IndexStore(tmp_path)
    local = {"local_only.txt": _scan_entry("h1")}
    result = classify(local, index, remote={"other_file": _remote(rel_path="other_file")})
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
    result = classify({}, index, remote={"a": _remote(size=999)})
    assert result[0].state == SyncState.REMOTE_CHANGED


def test_metadata_only_preserved_when_remote_size_matches(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", local_state="metadata-only"))
    result = classify({}, index, remote={"a": _remote(size=1)})  # matches _index_entry size=1
    assert result[0].state == SyncState.METADATA_ONLY


def test_no_remote_view_keeps_v01_behavior(tmp_path) -> None:
    index = IndexStore(tmp_path)
    index.set("a", _index_entry("h1", local_state="present"))
    result = classify({}, index, remote=None)  # index says present, no local file
    assert result[0].state == SyncState.REMOTE_ONLY
