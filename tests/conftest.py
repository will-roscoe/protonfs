# tests/conftest.py
"""Shared test fixtures for protonfs command tests.

`FakeDrive` is a single configurable stand-in for `DriveClient` (D4.2), replacing
the per-file `_FakeDrive` copies. It is *configured*, never subclassed: pass an
`upload_result` to simulate an upload failure/skip, `walk_entries` (or
`walk_by_root`) for remote listings, `trash_listing` for `list("/trash")`, and
`version` / `authed` for the setup checks. Use the `make_fake_drive` fixture (a
factory) so tests need no imports.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from protonfs.drive import RemoteEntry, TransferResult


class FakeDrive:
    def __init__(
        self,
        *,
        walk_entries: list[RemoteEntry] | None = None,
        walk_by_root: dict[str, list[RemoteEntry]] | None = None,
        trash_listing: list[dict] | None = None,
        upload_result: TransferResult | None = None,
        version: str | None = "v0.4.6",
        authed: bool = True,
    ) -> None:
        # recorded calls
        self.upload_calls: list[tuple] = []
        self.download_calls: list[tuple] = []
        self.created_folders: list[tuple[str, str]] = []
        self.trashed: list[str] = []
        self.deleted: list[str] = []
        self.restored: list[str] = []
        self.walk_roots: list[str] = []
        # configured responses
        self._walk_entries = walk_entries or []
        self._walk_by_root = walk_by_root
        self._trash_listing = trash_listing
        self._upload_result = upload_result
        self._version = version
        self._authed = authed

    def version(self):
        return self._version

    def is_authenticated(self):
        return self._authed

    def upload(self, local_paths, remote_parent, file_strategy=None, folder_strategy=None):
        self.upload_calls.append(
            (tuple(str(p) for p in local_paths), remote_parent, file_strategy)
        )
        if self._upload_result is not None:
            return self._upload_result
        return TransferResult(len(local_paths), 0, 0, [])

    def download(self, remote_paths, local_folder, file_strategy=None, folder_strategy=None):
        self.download_calls.append((tuple(remote_paths), str(local_folder), file_strategy))
        for remote_path in remote_paths:
            name = remote_path.rsplit("/", 1)[-1]
            (Path(local_folder) / name).write_bytes(b"downloaded")
        return TransferResult(len(remote_paths), 0, 0, [])

    def create_folder(self, parent_path, name):
        self.created_folders.append((parent_path, name))
        return {}

    def walk(self, remote_root):
        self.walk_roots.append(remote_root)
        if self._walk_by_root is not None:
            return list(self._walk_by_root.get(remote_root, []))
        return list(self._walk_entries)

    def trash(self, remote_paths):
        self.trashed.extend(remote_paths)
        return [{"ok": True} for _ in remote_paths]

    def delete(self, remote_paths):
        self.deleted.extend(remote_paths)
        return [{"ok": True} for _ in remote_paths]

    def restore(self, remote_paths):
        self.restored.extend(remote_paths)
        return [{"ok": True} for _ in remote_paths]

    def list(self, remote_path):
        if remote_path == "/trash":
            if self._trash_listing is not None:
                return self._trash_listing
            return [
                {"name": {"ok": True, "value": PurePosixPath(p).name}, "type": "file"}
                for p in self.trashed
            ]
        return []


@pytest.fixture
def make_fake_drive():
    """Factory fixture: `make_fake_drive(walk_entries=..., upload_result=...)`."""

    def _make(**kwargs) -> FakeDrive:
        return FakeDrive(**kwargs)

    return _make


@pytest.fixture(autouse=True)
def no_keyring_bootstrap(monkeypatch):
    """Keep the keyring bootstrap out of every test that does not target it.

    Without this, any test that exercises DriveClient would reach
    protonfs.secretservice, and on a developer's Linux box that can start a real
    dbus-daemon and gnome-keyring as a side effect of running the suite.
    tests/test_secretservice.py clears this to test the bootstrap itself.
    """
    monkeypatch.setenv("PROTONFS_NO_KEYRING_BOOTSTRAP", "1")
