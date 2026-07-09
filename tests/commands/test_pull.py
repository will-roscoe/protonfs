# tests/commands/test_pull.py
from __future__ import annotations

from pathlib import Path

from protonfs.commands.pull import pull
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import RemoteEntry, TransferResult
from protonfs.index import IndexEntry


class _FakeDrive:
    def __init__(self, write_to: Path) -> None:
        self._write_to = write_to
        self.download_calls: list[tuple] = []

    def download(self, remote_paths, local_folder, file_strategy=None, folder_strategy=None):
        self.download_calls.append((tuple(remote_paths), str(local_folder), file_strategy))
        for remote_path in remote_paths:
            name = remote_path.rsplit("/", 1)[-1]
            (Path(local_folder) / name).write_bytes(b"downloaded")
        return TransferResult(len(remote_paths), 0, 0, [])

    def walk(self, remote_root):
        return []


def test_pull_downloads_remote_only_files_and_updates_index(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "run1/dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="placeholder",
            remote_path="/my-files/test/run1/dump_0001",
            origin_device="other-device",
            local_state="metadata-only",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.drive = _FakeDrive(tmp_path)

    result = pull(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 1
    assert (tmp_path / "run1" / "dump_0001").exists()
    updated = ctx.index.get("run1/dump_0001")
    assert updated.local_state == "present"
    assert updated.origin_device == "other-device"  # origin is preserved, not overwritten


def test_pull_dry_run_does_not_call_download(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "dump_0001",
        IndexEntry(
            1,
            1.0,
            "h",
            "/my-files/test/dump_0001",
            "d1",
            "metadata-only",
            "2026-07-08T00:00:00+00:00",
        ),
    )
    fake = _FakeDrive(tmp_path)
    ctx.drive = fake

    result = pull(ctx, None, resolve=None, dry_run=True)

    assert result.transferred_items == 1
    assert fake.download_calls == []


def test_pull_no_remote_only_files_returns_zero_result(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = _FakeDrive(tmp_path)

    result = pull(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 0


def test_pull_refresh_seeds_then_downloads_on_empty_index(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)

    class _WalkDownloadDrive(_FakeDrive):
        def walk(self, remote_root):
            return [RemoteEntry("run1/dump_0001", is_dir=False, size=9)]

    fake = _WalkDownloadDrive(tmp_path)
    ctx.drive = fake

    # empty index: a bare pull would do nothing; with refresh=True it seeds then pulls
    result = pull(ctx, None, resolve=None, dry_run=False, refresh=True)

    assert result.transferred_items == 1
    assert (tmp_path / "run1" / "dump_0001").exists()


def test_pull_without_refresh_on_empty_index_is_noop(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = _FakeDrive(tmp_path)

    result = pull(ctx, None, resolve=None, dry_run=False, refresh=False)

    assert result.transferred_items == 0
