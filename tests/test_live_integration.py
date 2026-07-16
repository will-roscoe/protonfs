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
import time
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


def test_live_uid_addressed_permanent_delete_still_unsupported(tmp_path: Path, live_dir) -> None:
    """Probe for issue #6: uid-addressed permanent delete of a trashed node.

    protonfs's ``rm -f`` refuses to permanently delete when two trashed items share
    a basename, because proton-drive can only address a trashed node for deletion by
    ``/trash/<basename>`` -- not by its stable UID. If that ever changes, ``rm -f``
    could delete the correct node unambiguously and the duplicate-basename guard in
    ``commands/rm.py`` could be lifted.

    This test asserts the CURRENT (blocked) behavior. When a future proton-drive
    starts accepting UID addressing for permanent delete, the delete below will
    succeed instead of raising, this assertion will flip, and the test will FAIL
    LOUDLY -- signalling that the D2.2 stance in ``commands/rm.py`` should be revisited.
    """
    from protonfs.drive import DriveError, decrypted_name

    client, remote_dir = live_dir
    src = tmp_path / "uid_probe.bin"
    src.write_bytes(b"uid-probe-" + uuid.uuid4().hex.encode())
    client.upload([src], remote_dir)

    entry = next(e for e in client.list(remote_dir) if e.get("type") != "folder")
    uid = entry["uid"]

    remote_file = f"{remote_dir}/uid_probe.bin"
    client.trash([remote_file])

    # Both UID addressing forms must still be rejected. If either permanently deletes
    # the trashed node, UID addressing now works -> revisit commands/rm.py (#6).
    for addr in (f"/trash/{uid}", uid):
        with pytest.raises(DriveError):
            client.delete([addr])

    # And it must still be sitting in trash (nothing was permanently removed).
    trashed_names = [decrypted_name(e) for e in client.list("/trash")]
    assert "uid_probe.bin" in trashed_names, (
        "trashed node vanished without a basename-path delete -- UID addressing may "
        "now be supported; revisit the rm -f duplicate-basename guard (#6)"
    )

    # Clean up the trashed probe file by its supported basename path (reversible anyway).
    try:
        client.delete(["/trash/uid_probe.bin"])
    except DriveError:
        pass


def test_live_trash_then_restore_roundtrip(tmp_path: Path, live_dir) -> None:
    client, remote_dir = live_dir
    # Unique per-run name: proton-drive >= 0.5.0 resolves /trash paths by name,
    # first match wins (#56), so a stale same-named entry from an earlier failed
    # run would shadow this one and make restore impossible.
    name = f"to_trash-{uuid.uuid4().hex[:12]}.bin"
    src = tmp_path / name
    src.write_bytes(b"trash-me")
    client.upload([src], remote_dir)
    remote_file = f"{remote_dir}/{name}"

    client.trash([remote_file])
    assert name not in [e.rel_path for e in client.walk(remote_dir) if not e.is_dir]

    client.restore([remote_file])
    # Restore is eventually consistent: the API reports success seconds before the
    # node is re-linked into its parent's listing (measured ~4-15s, longer under
    # load; a v1.0.0 release-gate run failed asserting at t=0 while the file had
    # verifiably left /trash). Poll with a bounded deadline instead of asserting
    # immediately -- the trash assert above stays immediate because unlinking has
    # never been observed to lag.
    deadline = time.monotonic() + 90
    while True:
        names = [e.rel_path for e in client.walk(remote_dir) if not e.is_dir]
        if name in names:
            break
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{name} not visible in {remote_dir} within 90s of restore "
                f"returning (listing: {names})"
            )
        time.sleep(5)


def test_live_trash_list_shows_trashed_item(tmp_path: Path, live_dir) -> None:
    # #70: `trash list` (via DriveClient.list("/trash") + parent_name) must see a
    # freshly trashed item and not blow up resolving its original parent. Never
    # empties trash here -- that's out of the live suite's scope by policy.
    from protonfs.drive import decrypted_name

    client, remote_dir = live_dir
    name = f"trash-list-{uuid.uuid4().hex[:12]}.bin"
    src = tmp_path / name
    src.write_bytes(b"trash-list-me")
    client.upload([src], remote_dir)
    remote_file = f"{remote_dir}/{name}"

    client.trash([remote_file])

    trash_entries = client.list("/trash")
    matches = [e for e in trash_entries if decrypted_name(e) == name]
    assert matches, f"{name!r} not found in /trash listing"

    parent_uid = matches[0].get("parentUid")
    if parent_uid:
        # Best-effort: must not raise, may legitimately return None.
        client.parent_name(parent_uid)

    # Restore it so live_dir's trash-on-teardown cleanup still applies to a live node.
    client.restore([remote_file])
