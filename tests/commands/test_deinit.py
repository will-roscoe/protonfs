# tests/commands/test_deinit.py
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click
import pytest

from protonfs.commands.deinit import ALL_MANAGED_FILES, run_deinit
from protonfs.commands.setup import write_git_control_files
from protonfs.config import config_dir, init_config, save_local_config
from protonfs.ignore import init_ignore, init_include
from protonfs.index import IndexStore
from protonfs.refreshstate import save_frontier

fcntl = pytest.importorskip("fcntl")  # POSIX-only lock; the lock-refusal test needs it


def _full_setup(root: Path) -> None:
    """Recreate everything `protonfs setup` writes under `.protonfs/`, so a deinit test
    starts from a realistic fully-populated root, not just the shared config."""
    init_config(root, "/my-files/test")
    save_local_config(root, {"device_id": "d1"})
    init_ignore(root)
    init_include(root)
    write_git_control_files(root)
    IndexStore(root).save()
    save_frontier(root, "/my-files/test", [])


def test_full_teardown_removes_every_managed_file_and_the_directory(tmp_path: Path) -> None:
    _full_setup(tmp_path)
    protonfs_dir = config_dir(tmp_path)
    for name in ALL_MANAGED_FILES:
        assert (protonfs_dir / name).exists()  # sanity: setup really wrote all of them

    result = run_deinit(tmp_path, dry_run=False, yes=True)

    assert not protonfs_dir.exists()
    assert result.dir_removed is True
    assert {p.name for p in result.removed} == set(ALL_MANAGED_FILES)


def test_dry_run_leaves_the_tree_completely_untouched(tmp_path: Path) -> None:
    _full_setup(tmp_path)
    protonfs_dir = config_dir(tmp_path)
    before = {p: p.read_bytes() for p in protonfs_dir.iterdir() if p.is_file()}

    result = run_deinit(tmp_path, dry_run=True)

    assert result.removed == []
    after = {p: p.read_bytes() for p in protonfs_dir.iterdir() if p.is_file()}
    assert before == after
    for name in ALL_MANAGED_FILES:
        assert (protonfs_dir / name).exists()


def test_refuses_when_lock_is_held_by_another_process(tmp_path: Path) -> None:
    from protonfs.locking import LOCK_FILE_NAME, RepoLockError, repo_lock

    _full_setup(tmp_path)
    protonfs_dir = config_dir(tmp_path)

    # Simulate another process holding the lock: an independent fd flock'd on the same
    # file contends, since flock is per open-file-description (same pattern as
    # tests/test_locking.py's test_second_holder_raises_repo_lock_error).
    held = os.open(protonfs_dir / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(RepoLockError):
            with repo_lock(tmp_path):
                run_deinit(tmp_path, dry_run=False, yes=True)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)

    # Every managed file must still be present -- refusal happens before any deletion.
    for name in ALL_MANAGED_FILES:
        assert (protonfs_dir / name).exists()


def test_cli_deinit_reports_lock_contention_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main
    from protonfs.locking import LOCK_FILE_NAME

    _full_setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    protonfs_dir = config_dir(tmp_path)

    held = os.open(protonfs_dir / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = CliRunner().invoke(main, ["deinit", "--yes"])
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)

    assert result.exit_code != 0
    assert "another protonfs process" in result.output
    for name in ALL_MANAGED_FILES:
        assert (protonfs_dir / name).exists()


def test_payload_files_outside_protonfs_dir_are_never_touched(tmp_path: Path) -> None:
    _full_setup(tmp_path)
    payload = tmp_path / "dump_0001"
    payload.write_bytes(b"synced data")
    subdir_payload = tmp_path / "nested" / "dump_0002"
    subdir_payload.parent.mkdir()
    subdir_payload.write_bytes(b"more synced data")

    run_deinit(tmp_path, dry_run=False, yes=True)

    assert payload.read_bytes() == b"synced data"
    assert subdir_payload.read_bytes() == b"more synced data"


def test_reports_git_followup_steps_when_inside_a_git_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    _full_setup(tmp_path)

    result = run_deinit(tmp_path, dry_run=False, yes=True)

    assert result.in_git_repo is True
    assert "config.json" in result.tracked_removed
    assert ".gitattributes" in result.tracked_removed
    assert "config.local.json" not in result.tracked_removed  # local-only, not tracked
    captured = capsys.readouterr()
    assert "git add -A .protonfs" in captured.out


def test_declined_confirmation_leaves_tree_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _full_setup(tmp_path)
    protonfs_dir = config_dir(tmp_path)

    def fake_confirm(*args, **kwargs):
        raise click.exceptions.Abort()

    monkeypatch.setattr(click, "confirm", fake_confirm)

    with pytest.raises(click.exceptions.Abort):
        run_deinit(tmp_path, dry_run=False, yes=False)

    for name in ALL_MANAGED_FILES:
        assert (protonfs_dir / name).exists()


def test_not_a_protonfs_root_raises_click_exception(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException):
        run_deinit(tmp_path)
