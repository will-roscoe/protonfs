# tests/test_live_workflows.py
"""Opt-in live end-to-end COMMAND workflows against a real throwaway Proton Drive dir.

Where tests/test_live_integration.py exercises DriveClient primitives (upload, walk,
trash, restore), this file drives whole protonfs commands -- push / refresh / pull /
status / offload -- through a real repo context and the real proton-drive binary.

This is the layer where #96 lived undetected: the unit suite validates every command
against configured fakes, so a composition bug between scan, classify, the index, and
the real backend (pull ignoring its subpath and downloading unrelated directories)
passes the whole fake-drive test bank and only surfaces here.

Gated on PROTONFS_TEST_REMOTE exactly like the primitives suite: skipped entirely
when unset, so these NEVER run in CI. Each test works under a unique remote subdir
and cleans up with `trash` (reversible) -- never `empty-trash` (account-global).

Run locally with:
    PROTONFS_TEST_REMOTE=/my-files/test pytest tests/test_live_workflows.py -v
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from protonfs.commands.offload import offload
from protonfs.commands.pull import pull
from protonfs.commands.push import push
from protonfs.commands.refresh import refresh
from protonfs.commands.status import STATUS_CLEAN, compute_status, status_exit_code
from protonfs.config import init_config
from protonfs.context import RepoContext, load_context
from protonfs.drive import DriveClient

REMOTE = os.environ.get("PROTONFS_TEST_REMOTE")

pytestmark = pytest.mark.skipif(
    not REMOTE,
    reason="set PROTONFS_TEST_REMOTE to a throwaway Drive dir to run live tests",
)


@pytest.fixture
def live_remote():
    """A unique remote subdir path under the throwaway root, trashed on teardown.

    The directory itself is created lazily by the first `push` (ensure_remote_root),
    so a test that never pushes leaves nothing behind and the teardown trash is a
    best-effort no-op."""
    client = DriveClient()
    root = REMOTE.rstrip("/")
    remote_dir = f"{root}/pfs-flow-{uuid.uuid4().hex[:12]}"
    try:
        yield remote_dir
    finally:
        try:
            client.trash([remote_dir])  # reversible cleanup; never empty-trash
        except Exception:
            pass  # nothing was pushed -> nothing to trash


def _repo(tmp_path: Path, name: str, remote_dir: str) -> RepoContext:
    """A fresh protonfs root under tmp_path syncing to `remote_dir`, with a REAL drive."""
    root = tmp_path / name
    root.mkdir()
    init_config(root, remote_dir)
    return load_context(root)


def _seed(ctx: RepoContext, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = ctx.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _eventually(op, deadline_s: float = 90.0):
    """Retry `op` on 'Node not found' DriveErrors within a bounded deadline.

    Path resolution of freshly-created remote directories is eventually consistent
    (same family as the restore-visibility lag measured at ~4-15s in
    test_live_integration.py): a pull/refresh moments after push created the folder
    chain can transiently fail to resolve it. Only that error class is retried --
    anything else propagates immediately so real failures stay loud."""
    from protonfs.drive import DriveError

    deadline = time.monotonic() + deadline_s
    while True:
        try:
            return op()
        except DriveError as exc:
            if "node not found" not in str(exc).lower() or time.monotonic() > deadline:
                raise
            time.sleep(5)


def test_workflow_push_then_status_is_clean(tmp_path: Path, live_remote: str) -> None:
    """The most basic full loop: local files -> push -> index agrees -> status clean."""
    ctx = _repo(tmp_path, "repo", live_remote)
    _seed(ctx, {"a/f1": b"one", "a/f2": b"two"})

    result = push(ctx, None, None, dry_run=False)

    assert result.failed_items == 0, result.failures
    assert result.transferred_items == 2
    counts = compute_status(ctx, None)
    assert status_exit_code(counts) == STATUS_CLEAN, dict(counts)


def test_workflow_scoped_pull_downloads_only_the_requested_subdir(
    tmp_path: Path, live_remote: str
) -> None:
    """#96 end-to-end: after refresh sees the whole remote, `pull a` must download
    a/* only -- b/* stays metadata-only and no bytes for it land on disk."""
    src = _repo(tmp_path, "src", live_remote)
    _seed(src, {"a/f1": b"one", "b/f2": b"two"})
    assert push(src, None, None, dry_run=False).failed_items == 0

    clone = _repo(tmp_path, "clone", live_remote)
    _eventually(lambda: refresh(clone, None, prune=False))
    result = _eventually(lambda: pull(clone, "a", resolve=None, dry_run=False))

    assert result.failed_items == 0, result.failures
    assert (clone.root / "a" / "f1").read_bytes() == b"one"
    assert not (clone.root / "b").exists()  # the #96 leak: unrelated dir pulled
    assert clone.index.get("b/f2").local_state == "metadata-only"


def test_workflow_cli_pull_accepts_multiple_pathspecs(
    tmp_path: Path, live_remote: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#92 end-to-end through the real CLI: a glob-expanded `pull a b` pulls both
    subtrees (previously a Click usage error) and still leaves others untouched."""
    from click.testing import CliRunner

    from protonfs.cli import main

    src = _repo(tmp_path, "src", live_remote)
    _seed(src, {"a/f1": b"one", "b/f2": b"two", "c/f3": b"three"})
    assert push(src, None, None, dry_run=False).failed_items == 0

    clone = _repo(tmp_path, "clone", live_remote)
    _eventually(lambda: refresh(clone, None, prune=False))
    monkeypatch.chdir(clone.root)

    def _cli_pull():
        result = CliRunner().invoke(main, ["pull", "a", "b"])
        # The CLI's error boundary renders DriveError as a message + exit 1; re-raise
        # the consistency-lag case as DriveError so _eventually can retry it.
        if result.exit_code != 0 and "node not found" in result.output.lower():
            from protonfs.drive import DriveError

            raise DriveError(result.output)
        return result

    result = _eventually(_cli_pull)

    assert result.exit_code == 0, result.output
    assert (clone.root / "a" / "f1").read_bytes() == b"one"
    assert (clone.root / "b" / "f2").read_bytes() == b"two"
    assert not (clone.root / "c").exists()


def test_workflow_offload_then_pull_restores_content(
    tmp_path: Path, live_remote: str
) -> None:
    """offload deletes local bytes only after verifying the remote copy; a later pull
    brings back byte-identical content."""
    ctx = _repo(tmp_path, "repo", live_remote)
    payload = b"offload-me-" + uuid.uuid4().hex.encode()
    _seed(ctx, {"big/dump": payload})
    assert push(ctx, None, None, dry_run=False).failed_items == 0

    result = _eventually(lambda: offload(ctx, None, verify=True, dry_run=False))

    assert result.offloaded == 1, (result.skipped_paths, result.modified_paths)
    assert not (ctx.root / "big" / "dump").exists()
    assert ctx.index.get("big/dump").local_state == "metadata-only"

    pulled = _eventually(lambda: pull(ctx, None, resolve=None, dry_run=False))

    assert pulled.failed_items == 0, pulled.failures
    assert (ctx.root / "big" / "dump").read_bytes() == payload
