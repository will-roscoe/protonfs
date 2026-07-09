from __future__ import annotations

from pathlib import Path

from protonfs.commands.push import push
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import TransferResult


class _FakeDrive:
    def __init__(self) -> None:
        self.upload_calls: list[tuple] = []
        self.created_folders: list[tuple[str, str]] = []

    def create_folder(self, parent_path: str, name: str) -> dict:
        self.created_folders.append((parent_path, name))
        return {}

    def upload(self, local_paths, remote_parent, file_strategy=None, folder_strategy=None):
        self.upload_calls.append(
            (tuple(str(p) for p in local_paths), remote_parent, file_strategy)
        )
        return TransferResult(
            transferred_items=len(local_paths), skipped_items=0, failed_items=0, failures=[]
        )


def test_push_uploads_local_only_files_and_updates_index(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = _FakeDrive()
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 1
    assert fake.upload_calls[0][1] == "/my-files/test/run1"
    assert ctx.index.get("run1/dump_0001") is not None
    assert ctx.index.get("run1/dump_0001").remote_path == "/my-files/test/run1/dump_0001"


def test_push_dry_run_does_not_call_upload(tmp_path: Path) -> None:
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = _FakeDrive()
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=True)

    assert result.transferred_items == 1  # reported as "would transfer"
    assert fake.upload_calls == []


def test_push_no_files_to_push_returns_zero_result(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = _FakeDrive()

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 0


def test_push_does_not_index_failed_files(tmp_path: Path) -> None:
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)

    class _FailingDrive(_FakeDrive):
        def upload(self, local_paths, remote_parent, file_strategy=None, folder_strategy=None):
            return TransferResult(
                transferred_items=0,
                skipped_items=0,
                failed_items=1,
                failures=[{"name": "dump_0001", "error": "conflict"}],
            )

    ctx.drive = _FailingDrive()
    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.failed_items == 1
    assert ctx.index.get("dump_0001") is None
