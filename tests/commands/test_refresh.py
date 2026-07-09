# tests/commands/test_refresh.py
from __future__ import annotations

from pathlib import Path

from protonfs.commands.refresh import refresh
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import RemoteEntry
from protonfs.index import IndexEntry


class _FakeDrive:
    def __init__(self, entries: list[RemoteEntry]) -> None:
        self._entries = entries

    def walk(self, remote_root: str) -> list[RemoteEntry]:
        return self._entries


def test_refresh_seeds_metadata_only_for_new_remote_files(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = _FakeDrive([
        RemoteEntry("run1", is_dir=True, size=0),
        RemoteEntry("run1/dump_0001", is_dir=False, size=100),
    ])

    result = refresh(ctx, None, prune=False)

    assert result.seeded == 1
    entry = ctx.index.get("run1/dump_0001")
    assert entry is not None
    assert entry.local_state == "metadata-only"
    assert entry.size == 100
    assert entry.sha256 == ""
    assert entry.remote_path == "/my-files/test/run1/dump_0001"


def test_refresh_is_idempotent(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = _FakeDrive([RemoteEntry("a", is_dir=False, size=10)])
    refresh(ctx, None, prune=False)
    result2 = refresh(ctx, None, prune=False)
    assert result2.seeded == 0


def test_refresh_flags_remote_deleted_without_prune(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "gone",
        IndexEntry(
            1, 1.0, "", "/my-files/test/gone", "d1", "metadata-only", "2026-07-09T00:00:00+00:00"
        ),
    )
    ctx.drive = _FakeDrive([])  # remote is empty -> 'gone' is remote-deleted

    result = refresh(ctx, None, prune=False)

    assert result.remote_deleted == 1
    assert "gone" in result.deleted_paths
    assert ctx.index.get("gone") is not None  # NOT pruned without --prune


def test_refresh_prune_removes_remote_deleted(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "gone",
        IndexEntry(
            1, 1.0, "", "/my-files/test/gone", "d1", "metadata-only", "2026-07-09T00:00:00+00:00"
        ),
    )
    ctx.drive = _FakeDrive([])

    result = refresh(ctx, None, prune=True)

    assert result.pruned == 1
    assert ctx.index.get("gone") is None


def test_refresh_flags_remote_changed(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "f",
        IndexEntry(
            10, 1.0, "", "/my-files/test/f", "d1", "metadata-only", "2026-07-09T00:00:00+00:00"
        ),
    )
    ctx.drive = _FakeDrive([RemoteEntry("f", is_dir=False, size=999)])

    result = refresh(ctx, None, prune=False)

    assert result.remote_changed == 1
    assert "f" in result.changed_paths
