from __future__ import annotations

from pathlib import Path

import click
import pytest

from protonfs.commands.rm import rm
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.index import IndexEntry


class _FakeDrive:
    def __init__(self) -> None:
        self.trashed: list[str] = []
        self.deleted: list[str] = []

    def trash(self, remote_paths: list[str]) -> list[dict]:
        self.trashed.extend(remote_paths)
        return [{"ok": True} for _ in remote_paths]

    def delete(self, remote_paths: list[str]) -> list[dict]:
        self.deleted.extend(remote_paths)
        return [{"ok": True} for _ in remote_paths]


def test_rm_trashes_and_removes_from_index(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            remote_path="/my-files/test/dump_0001",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    fake = _FakeDrive()
    ctx.drive = fake

    rm(ctx, "dump_0001", recursive=False, force=False, confirmed=True)

    assert fake.trashed == ["/my-files/test/dump_0001"]
    assert fake.deleted == []
    assert ctx.index.get("dump_0001") is None


def test_rm_force_also_calls_delete(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = _FakeDrive()
    ctx.drive = fake

    rm(ctx, "dump_0001", recursive=False, force=True, confirmed=True)

    assert fake.trashed == ["/my-files/test/dump_0001"]
    assert fake.deleted == ["/trash/dump_0001"]


def test_rm_directory_without_recursive_raises(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = _FakeDrive()

    with pytest.raises(click.ClickException):
        rm(ctx, "run1", recursive=False, force=False, confirmed=True)


def test_rm_directory_with_recursive_removes_all_index_entries_under_it(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "run1/dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            remote_path="/my-files/test/run1/dump_0001",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.drive = _FakeDrive()

    rm(ctx, "run1", recursive=True, force=False, confirmed=True)

    assert ctx.index.get("run1/dump_0001") is None
