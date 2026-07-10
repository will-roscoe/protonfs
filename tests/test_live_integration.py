# tests/test_live_integration.py
"""Opt-in live integration tests against a real throwaway Proton Drive dir (D4.1).

Gated on PROTONFS_TEST_REMOTE (a disposable remote directory, e.g. /my-files/test).
Skipped entirely when unset, so these NEVER run in CI. Each test works in a unique
subdir and cleans up with `trash` (reversible) -- never `empty-trash` (global).

Run locally with:
    PROTONFS_DRIVE_BIN=/path/to/proton-drive \
    PROTONFS_TEST_REMOTE=/my-files/test \
    pytest tests/test_live_integration.py -v
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from protonfs.drive import DriveClient

REMOTE = os.environ.get("PROTONFS_TEST_REMOTE")

pytestmark = pytest.mark.skipif(
    not REMOTE,
    reason="set PROTONFS_TEST_REMOTE to a throwaway Drive dir to run live tests",
)


@pytest.fixture
def live_dir():
    """A unique subdir under the throwaway remote, trashed (not emptied) on teardown."""
    client = DriveClient()
    root = REMOTE.rstrip("/")
    name = f"pfs-live-{uuid.uuid4().hex[:12]}"
    client.create_folder(root, name)
    remote_dir = f"{root}/{name}"
    try:
        yield client, remote_dir
    finally:
        client.trash([remote_dir])  # reversible cleanup; never empty-trash


def test_live_auth_is_active() -> None:
    assert DriveClient().is_authenticated(), "proton-drive is not authenticated"


def test_live_upload_walk_download_roundtrip(tmp_path: Path, live_dir) -> None:
    client, remote_dir = live_dir
    src = tmp_path / "sample.bin"
    payload = b"hello-live-" + uuid.uuid4().hex.encode()
    src.write_bytes(payload)

    client.upload([src], remote_dir)

    files = [e.rel_path for e in client.walk(remote_dir) if not e.is_dir]
    assert "sample.bin" in files

    dest = tmp_path / "download"
    dest.mkdir()
    client.download([f"{remote_dir}/sample.bin"], dest)
    assert (dest / "sample.bin").read_bytes() == payload


def test_live_trash_then_restore_roundtrip(tmp_path: Path, live_dir) -> None:
    client, remote_dir = live_dir
    src = tmp_path / "to_trash.bin"
    src.write_bytes(b"trash-me")
    client.upload([src], remote_dir)
    remote_file = f"{remote_dir}/to_trash.bin"

    client.trash([remote_file])
    assert "to_trash.bin" not in [e.rel_path for e in client.walk(remote_dir) if not e.is_dir]

    client.restore([remote_file])
    assert "to_trash.bin" in [e.rel_path for e in client.walk(remote_dir) if not e.is_dir]
