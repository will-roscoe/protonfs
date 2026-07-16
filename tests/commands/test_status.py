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
from protonfs.lfs import POINTER_SIGNATURE


def test_compute_status_counts_local_only_and_synced(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "new_dump").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)

    counts = compute_status(ctx, None)

    assert counts["local-only"] == 1
    assert counts.get("synced", 0) == 0


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
