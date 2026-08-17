from __future__ import annotations

from collections import Counter
from pathlib import Path

from protonfs.commands.status import (
    STATUS_CLEAN,
    STATUS_CONFLICT,
    STATUS_DRIFT,
    compute_status,
    status_exit_code,
)
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.diff import SyncState
from protonfs.drive import RemoteEntry
from protonfs.index import IndexEntry
from protonfs.lfs import POINTER_SIGNATURE


def _synced_entry(path: Path) -> IndexEntry:
    """An index entry recording `path` exactly as it is on disk right now."""
    from protonfs.localscan import hash_file_digests

    sha256, sha1 = hash_file_digests(path)
    stat = path.stat()
    return IndexEntry(
        size=stat.st_size, mtime=stat.st_mtime, sha256=sha256, sha1=sha1,
        remote_path=f"/my-files/test/{path.name}", origin_device="d",
        local_state="present", last_synced="2026-01-01T00:00:00Z",
    )


def test_compute_status_narrates_scan(tmp_path: Path, recording_reporter_cls) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "new_dump").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    rep = recording_reporter_cls()

    compute_status(ctx, None, reporter=rep)

    kinds = [c[0] for c in rep.calls]
    assert kinds == ["phase"]


def test_compute_status_counts_local_only_and_synced(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "new_dump").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)

    counts = compute_status(ctx, None)

    assert counts["local-only"] == 1
    assert counts.get("synced", 0) == 0


def test_compute_status_subpath_excludes_index_entries_outside_it(tmp_path: Path) -> None:
    """#96 companion: `status SUBPATH` must not count (or exit non-zero for) index
    entries outside SUBPATH -- classify() sees the whole index, so the counts need
    the same within_subpath filter as ls/refresh/offload."""
    from protonfs.index import IndexEntry

    (tmp_path / "sub").mkdir()
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "other/dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="placeholder",
            sha1="",
            remote_path="/my-files/test/other/dump_0001",
            origin_device="d1",
            local_state="metadata-only",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )

    counts = compute_status(ctx, "sub")

    assert counts.get("metadata-only", 0) == 0  # out-of-scope entry not counted
    assert status_exit_code(counts) == STATUS_CLEAN


def test_exit_code_clean_when_empty() -> None:
    assert status_exit_code(Counter()) == STATUS_CLEAN


def test_exit_code_clean_for_synced_and_metadata_only() -> None:
    counts = Counter({SyncState.SYNCED.value: 3, SyncState.METADATA_ONLY.value: 2})
    assert status_exit_code(counts) == STATUS_CLEAN


def test_exit_code_drift_for_non_conflict_divergence() -> None:
    for state in (
        SyncState.LOCAL_ONLY,
        SyncState.REMOTE_ONLY,
        SyncState.LOCAL_MODIFIED,
        SyncState.REMOTE_MODIFIED,
        SyncState.LOCAL_DELETED,
        SyncState.REMOTE_DELETED,
        SyncState.REMOTE_CHANGED,
    ):
        counts = Counter({SyncState.SYNCED.value: 5, state.value: 1})
        assert status_exit_code(counts) == STATUS_DRIFT, state


def test_exit_code_conflict_for_conflict_states() -> None:
    for state in (SyncState.CONFLICT, SyncState.BOTH_MODIFIED):
        counts = Counter({state.value: 1})
        assert status_exit_code(counts) == STATUS_CONFLICT, state


def test_exit_code_conflict_outranks_drift() -> None:
    counts = Counter({SyncState.LOCAL_ONLY.value: 4, SyncState.CONFLICT.value: 1})
    assert status_exit_code(counts) == STATUS_CONFLICT


def test_exit_code_clean_for_lfs_pointer_state() -> None:
    counts = Counter({SyncState.LFS_POINTER.value: 3})
    assert status_exit_code(counts) == STATUS_CLEAN


def test_pointer_only_tree_is_clean_end_to_end(tmp_path: Path) -> None:
    # #32: an unmaterialised git-LFS pointer tree is a deliberate no-op state, not
    # drift -- `status` must exit clean, not flag the pointer as something to reconcile.
    (tmp_path / "big.bin").write_text(
        f"{POINTER_SIGNATURE}\noid sha256:{'0' * 64}\nsize 171008\n"
    )
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)

    counts = compute_status(ctx, None)

    assert counts[SyncState.LFS_POINTER.value] == 1
    assert status_exit_code(counts) == STATUS_CLEAN


def test_compute_status_without_remote_reports_synced_from_the_index_alone(
    tmp_path: Path, make_fake_drive
) -> None:
    # #144: the default path never contacts Drive, so "synced" means "matches what
    # protonfs last recorded". A remote copy that has since changed is invisible here --
    # which is why --remote exists and why offload does its own live verification.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _synced_entry(tmp_path / "dump_0001"))
    ctx.drive = make_fake_drive(
        walk_entries=[RemoteEntry(rel_path="dump_0001", is_dir=False, size=999, claimed_size=999)]
    )

    counts = compute_status(ctx, None)

    assert counts[SyncState.SYNCED.value] == 1
    assert ctx.drive.walk_roots == []  # Drive was never listed


def test_compute_status_with_remote_detects_a_changed_remote_copy(
    tmp_path: Path, make_fake_drive
) -> None:
    # #144: --remote walks Drive and classifies against it, so a remote copy that no
    # longer matches the index is reported instead of being counted as synced.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _synced_entry(tmp_path / "dump_0001"))
    ctx.drive = make_fake_drive(
        walk_entries=[RemoteEntry(rel_path="dump_0001", is_dir=False, size=999, claimed_size=999)]
    )

    counts = compute_status(ctx, None, remote=True)

    assert counts[SyncState.SYNCED.value] == 0
    assert counts[SyncState.REMOTE_MODIFIED.value] == 1
    assert ctx.drive.walk_roots == ["/my-files/test"]
