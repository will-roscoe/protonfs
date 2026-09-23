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


def test_compute_status_counts_local_only_and_locally_indexed(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "new_dump").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)

    counts = compute_status(ctx, None)

    assert counts["local-only"] == 1
    assert counts.get("locally-indexed", 0) == 0


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
    counts = Counter({SyncState.LOCALLY_INDEXED.value: 3, SyncState.METADATA_ONLY.value: 2})
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
        counts = Counter({SyncState.LOCALLY_INDEXED.value: 5, state.value: 1})
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


def test_compute_status_without_remote_reports_locally_indexed_from_the_index_alone(
    tmp_path: Path, make_fake_drive
) -> None:
    # #150: the default path never contacts Drive, so the state is named for what it
    # compared -- "locally-indexed", i.e. matches what protonfs last recorded. A remote
    # copy that has since changed is invisible here, which is why --remote exists and
    # why offload does its own live verification.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _synced_entry(tmp_path / "dump_0001"))
    ctx.drive = make_fake_drive(
        walk_entries=[RemoteEntry(rel_path="dump_0001", is_dir=False, size=999, claimed_size=999)]
    )

    counts = compute_status(ctx, None)

    assert counts[SyncState.LOCALLY_INDEXED.value] == 1
    assert ctx.drive.walk_roots == []  # Drive was never listed


def test_compute_status_with_remote_detects_a_changed_remote_copy(
    tmp_path: Path, make_fake_drive
) -> None:
    # #150: --remote walks Drive and classifies against it, so a remote copy that no
    # longer matches the index is reported instead of being counted locally-indexed.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _synced_entry(tmp_path / "dump_0001"))
    ctx.drive = make_fake_drive(
        walk_entries=[RemoteEntry(rel_path="dump_0001", is_dir=False, size=999, claimed_size=999)]
    )

    counts = compute_status(ctx, None, remote=True)

    assert counts[SyncState.LOCALLY_INDEXED.value] == 0
    assert counts[SyncState.REMOTE_MODIFIED.value] == 1
    assert ctx.drive.walk_roots == ["/my-files/test"]


def test_cli_status_json_names_the_state_for_what_was_compared(
    tmp_path: Path, monkeypatch
) -> None:
    # #150: the machine-facing key is renamed with the state, and the document says
    # whether Drive was consulted at all.
    import json

    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _synced_entry(tmp_path / "dump_0001"))
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["status", "--format", "json"])

    doc = json.loads(result.output)
    assert doc["counts"]["locally-indexed"] == 1
    assert "synced" not in doc["counts"]
    assert doc["remote"] is False
    assert doc["exit_code"] == 0 == result.exit_code


def test_cli_status_remote_reports_a_changed_remote_copy(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    import json

    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _synced_entry(tmp_path / "dump_0001"))
    ctx.drive = make_fake_drive(
        walk_entries=[RemoteEntry(rel_path="dump_0001", is_dir=False, size=999, claimed_size=999)]
    )
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["status", "--remote", "--format", "json"])

    doc = json.loads(result.output)
    assert doc["remote"] is True
    assert doc["counts"]["remote-modified"] == 1
    assert result.exit_code == 1


def test_status_counts_a_file_deleted_locally_as_local_deleted_not_remote_only(
    tmp_path: Path,
) -> None:
    # #150: nothing was checked on Drive, so the state names what was: it is gone here.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _synced_entry(tmp_path / "dump_0001"))
    (tmp_path / "dump_0001").unlink()

    counts = compute_status(ctx, None)

    assert counts[SyncState.LOCAL_DELETED.value] == 1
    assert counts[SyncState.REMOTE_ONLY.value] == 0
    assert status_exit_code(counts) == STATUS_DRIFT
