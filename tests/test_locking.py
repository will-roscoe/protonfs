from __future__ import annotations

import os
from pathlib import Path

import pytest

from protonfs.locking import LOCK_FILE_NAME, RepoLockError, repo_lock

fcntl = pytest.importorskip("fcntl")  # POSIX-only lock; skip whole module elsewhere


def test_single_holder_succeeds_and_creates_lock_file(tmp_path: Path) -> None:
    with repo_lock(tmp_path):
        assert (tmp_path / ".protonfs" / LOCK_FILE_NAME).exists()


def test_lock_is_released_after_the_context(tmp_path: Path) -> None:
    with repo_lock(tmp_path):
        pass
    # A second acquisition after the first releases must succeed.
    with repo_lock(tmp_path):
        pass


def test_second_holder_raises_repo_lock_error(tmp_path: Path) -> None:
    # Simulate another process holding the lock: take an flock on the same file via an
    # independent fd (flock is per open-file-description, so this contends).
    lock_dir = tmp_path / ".protonfs"
    lock_dir.mkdir()
    held = os.open(lock_dir / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(RepoLockError):
            with repo_lock(tmp_path):
                pass
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


def test_lock_freed_when_contender_releases(tmp_path: Path) -> None:
    lock_dir = tmp_path / ".protonfs"
    lock_dir.mkdir()
    held = os.open(lock_dir / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with pytest.raises(RepoLockError):
        with repo_lock(tmp_path):
            pass
    # Release the simulated other process; the lock becomes acquirable again.
    fcntl.flock(held, fcntl.LOCK_UN)
    os.close(held)
    with repo_lock(tmp_path):
        pass


def test_degrades_to_no_op_without_fcntl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On a platform without fcntl (e.g. Windows) the lock is a no-op: it must not raise,
    # even when a "held" lock file already exists. Real Windows locking is issue #9.
    import protonfs.locking as locking

    monkeypatch.setattr(locking, "fcntl", None)
    with locking.repo_lock(tmp_path):
        pass
    # A second acquisition also succeeds (no contention is enforced without fcntl).
    with locking.repo_lock(tmp_path):
        pass


def test_cli_push_reports_lock_contention_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    # A mutating command must fail fast with the instructive message (exit != 0) when
    # another process holds the lock -- never a traceback, never a silent wait.
    from click.testing import CliRunner

    from protonfs.cli import main
    from protonfs.config import init_config
    from protonfs.context import load_context

    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    held = os.open(tmp_path / ".protonfs" / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = CliRunner().invoke(main, ["push"])
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)

    assert result.exit_code != 0
    assert "another protonfs process" in result.output
    assert ctx.drive.upload_calls == []  # never touched the remote while locked out
