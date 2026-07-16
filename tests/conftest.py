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

from protonfs.drive import RemoteEntry, RemoteIdentity, TransferResult


class FakeDrive:
    def __init__(
        self,
        *,
        walk_entries: list[RemoteEntry] | None = None,
        walk_by_root: dict[str, list[RemoteEntry]] | None = None,
        trash_listing: list[dict] | None = None,
        upload_result: TransferResult | None = None,
        dropped_files: set[str] | None = None,
        remote_size_overrides: dict[str, int] | None = None,
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
        self.identity_calls: list[str] = []
        # configured responses
        self._walk_entries = walk_entries or []
        self._walk_by_root = walk_by_root
        self._trash_listing = trash_listing
        self._upload_result = upload_result
        # #22 simulation: names proton-drive reports as transferred but that never land,
        # and per-name size overrides to simulate a truncated/partial upload on the remote.
        self._dropped_files = dropped_files or set()
        self._remote_size_overrides = remote_size_overrides or {}
        # remote_parent -> {name: claimed_size} for files that actually landed.
        self._remote_files: dict[str, dict[str, int]] = {}
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
        result = (
            self._upload_result
            if self._upload_result is not None
            else TransferResult(len(local_paths), 0, 0, [])
        )
        # Model what actually lands on the remote: every uploaded file EXCEPT the ones
        # proton-drive reported as failures and the ones configured as silently dropped.
        failed = {f["name"] for f in result.failures}
        bucket = self._remote_files.setdefault(remote_parent, {})
        for p in local_paths:
            name = Path(p).name
            if name in failed or name in self._dropped_files:
                continue
            if name in self._remote_size_overrides:
                bucket[name] = self._remote_size_overrides[name]
            else:
                try:
                    bucket[name] = Path(p).stat().st_size
                except OSError:
                    bucket[name] = 0
        return result

    def remote_identities(self, remote_parent):
        self.identity_calls.append(remote_parent)
        bucket = self._remote_files.get(remote_parent, {})
        return {
            name: RemoteIdentity(claimed_size=size, sha1=None) for name, size in bucket.items()
        }

    def download(self, remote_paths, local_folder, file_strategy=None, folder_strategy=None):
        self.download_calls.append((tuple(remote_paths), str(local_folder), file_strategy))
        for remote_path in remote_paths:
            name = remote_path.rsplit("/", 1)[-1]
            (Path(local_folder) / name).write_bytes(b"downloaded")
        return TransferResult(len(remote_paths), 0, 0, [])

    def create_folder(self, parent_path, name):
        self.created_folders.append((parent_path, name))
        return {}

    def walk(self, remote_root, on_directory=None, *, sleep=None, frontier=None, on_progress=None):
        self.walk_roots.append(remote_root)
        self.walk_frontier = frontier  # record what refresh passed (resume vs fresh)
        if self._walk_by_root is not None:
            entries = list(self._walk_by_root.get(remote_root, []))
        else:
            entries = list(self._walk_entries)
        # Simulate the per-directory seeding callback (#33): group file entries by parent
        # directory and invoke the callback once per group, so incremental-persistence
        # behaviour is exercised the same way the real walk drives it.
        if on_directory is not None:
            from collections import defaultdict

            groups: dict[str, list] = defaultdict(list)
            for entry in entries:
                if not entry.is_dir:
                    parent = entry.rel_path.rsplit("/", 1)[0] if "/" in entry.rel_path else ""
                    groups[parent].append(entry)
            for parent in groups or {"": []}:
                on_directory(groups[parent])
        # Simulate a walk that ran to completion: the frontier drains to empty, so a caller
        # persisting progress ends with an empty frontier (refresh then clears its state).
        if on_progress is not None:
            on_progress([])
        return entries

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
