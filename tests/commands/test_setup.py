# tests/commands/test_setup.py
from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from protonfs.commands.setup import (
    _append_gitignore,
    clean_pointer_stubs,
    ensure_authenticated,
    ensure_cli_present,
    ensure_config,
    is_git_toplevel,
    migrate_lfs,
    write_git_control_files,
)
from protonfs.config import load_config
from protonfs.context import RepoContext
from protonfs.drive import TransferResult
from protonfs.index import IndexStore


def _git_init(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)


def test_is_git_toplevel_true_at_repo_root(tmp_path: Path) -> None:
    _git_init(tmp_path)
    assert is_git_toplevel(tmp_path) is True


def test_is_git_toplevel_false_in_subdirectory(tmp_path: Path) -> None:
    _git_init(tmp_path)
    sub = tmp_path / "sim"
    sub.mkdir()
    # A subdir of a git repo is NOT the toplevel -> migration must not run there (#19).
    assert is_git_toplevel(sub) is False


def test_is_git_toplevel_false_when_not_a_git_repo(tmp_path: Path) -> None:
    assert is_git_toplevel(tmp_path) is False


def test_write_git_control_files_creates_exempting_attributes_and_gitignore(
    tmp_path: Path,
) -> None:
    write_git_control_files(tmp_path)

    attrs = (tmp_path / ".protonfs" / ".gitattributes").read_text()
    assert "!filter" in attrs and "!diff" in attrs and "!merge" in attrs  # exempt from LFS (#20)
    ignore = (tmp_path / ".protonfs" / ".gitignore").read_text()
    pattern_lines = [
        ln.strip()
        for ln in ignore.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert "index.json" in pattern_lines
    assert "refresh-state.json" in pattern_lines
    # The shared contract stays tracked -- config.json / ignore must NOT be gitignored.
    assert "config.json" not in pattern_lines
    assert "ignore" not in pattern_lines


def test_write_git_control_files_is_idempotent_and_preserves_user_lines(tmp_path: Path) -> None:
    protonfs_dir = tmp_path / ".protonfs"
    protonfs_dir.mkdir()
    (protonfs_dir / ".gitignore").write_text("index.json\nmy-own-scratch/\n")

    write_git_control_files(tmp_path)

    ignore = (protonfs_dir / ".gitignore").read_text()
    assert ignore.count("index.json") == 1  # not duplicated
    assert "my-own-scratch/" in ignore  # user's line preserved
    assert "refresh-state.json" in ignore  # missing managed line appended


def test_ensure_cli_present_raises_when_missing(make_fake_drive) -> None:
    with pytest.raises(click.ClickException):
        ensure_cli_present(make_fake_drive(version=None))


def test_ensure_cli_present_returns_version_string(make_fake_drive) -> None:
    assert ensure_cli_present(make_fake_drive(version="v0.4.6")) == "v0.4.6"


def test_ensure_authenticated_raises_when_not_authed(make_fake_drive) -> None:
    with pytest.raises(click.ClickException):
        ensure_authenticated(make_fake_drive(authed=False))


def test_ensure_config_reuses_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from protonfs.config import init_config

    existing = init_config(tmp_path, "/my-files/existing")

    def _fail_if_prompted(*args, **kwargs):
        raise AssertionError("should not prompt when config already exists")

    monkeypatch.setattr(click, "prompt", _fail_if_prompted)
    result = ensure_config(tmp_path)
    assert result.remote_root == "/my-files/existing"
    assert result.device_id == existing.device_id


def test_ensure_config_prompts_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(click, "prompt", lambda *a, **k: "/my-files/new")
    result = ensure_config(tmp_path)
    assert result.remote_root == "/my-files/new"
    assert load_config(tmp_path) is not None


def test_migrate_lfs_is_noop_when_not_lfs_tracked(tmp_path: Path, make_fake_drive) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    performed = migrate_lfs(ctx, dry_run=False)

    assert performed is False


def test_migrate_lfs_dry_run_reports_without_acting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0),
    )

    performed = migrate_lfs(ctx, dry_run=True)

    assert performed is True
    assert calls == []  # dry-run must not invoke git at all
    assert (
        tmp_path / ".gitattributes"
    ).read_text() == "sim/*/* filter=lfs diff=lfs merge=lfs -text\n"


def test_migrate_lfs_full_success_mutates_git_only_after_upload_and_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    # Single shared event log so we can prove push happened strictly before
    # any git mutation (add / rm --cached / commit).
    events: list[tuple] = []

    def fake_push_files(*args, **kwargs):
        events.append(("push",))
        return TransferResult(3, 0, 0, [])

    monkeypatch.setattr("protonfs.commands.setup.push_files", fake_push_files)

    confirm_calls = []

    def fake_confirm(*args, **kwargs):
        confirm_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(click, "confirm", fake_confirm)

    def fake_run(cmd, *args, **kwargs):
        events.append(("run", cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    performed = migrate_lfs(ctx, dry_run=False)

    assert performed is True
    assert confirm_calls  # click.confirm(..., abort=True) was invoked
    assert any(e[0] == "push" for e in events)  # push_files was called

    push_index = next(i for i, e in enumerate(events) if e[0] == "push")
    mutation_indices = [
        i
        for i, e in enumerate(events)
        if e[0] == "run" and e[1][3] in ("add", "rm", "commit")
    ]
    assert mutation_indices  # git add/rm --cached/commit all ran
    assert all(push_index < i for i in mutation_indices)

    assert "filter=lfs" not in (tmp_path / ".gitattributes").read_text()


def test_migrate_lfs_confirm_declined_leaves_git_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    monkeypatch.setattr(
        "protonfs.commands.setup.push_files",
        lambda *a, **k: TransferResult(3, 0, 0, []),
    )

    def fake_confirm(*args, **kwargs):
        raise click.exceptions.Abort()

    monkeypatch.setattr(click, "confirm", fake_confirm)

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(click.exceptions.Abort):
        migrate_lfs(ctx, dry_run=False)

    assert (
        tmp_path / ".gitattributes"
    ).read_text() == "sim/*/* filter=lfs diff=lfs merge=lfs -text\n"
    mutation_calls = [cmd for cmd in calls if cmd[3] in ("add", "rm", "commit")]
    assert mutation_calls == []


def test_append_gitignore_adds_pattern_that_is_substring_of_existing_line(
    tmp_path: Path,
) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("sim/build_output/\n")

    _append_gitignore(tmp_path, ["sim/"])

    lines = gitignore.read_text().splitlines()
    assert "sim/build_output/" in lines
    assert "sim/" in lines  # must be added on its own line, not skipped as a substring


def test_migrate_lfs_wraps_git_mutation_failure_in_click_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    monkeypatch.setattr(
        "protonfs.commands.setup.push_files",
        lambda *a, **k: TransferResult(3, 0, 0, []),
    )
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)

    def fake_run(cmd, *args, **kwargs):
        # First git-mutation call (`git add ...`) fails; everything before it
        # (git lfs pull, push_files) is unaffected since it's monkeypatched away.
        if cmd[3] == "add":
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(click.ClickException) as excinfo:
        migrate_lfs(ctx, dry_run=False)

    assert not isinstance(excinfo.value, subprocess.CalledProcessError)
    message = str(excinfo.value)
    assert "git" in message.lower()
    assert "Drive" in message


def test_clean_pointer_stubs_removes_stub_files(tmp_path: Path) -> None:
    stub = tmp_path / "dump_0001"
    stub.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 1\n")
    real = tmp_path / "dump_0002"
    real.write_bytes(b"real data")

    removed = clean_pointer_stubs(tmp_path)

    assert removed == 1
    assert not stub.exists()
    assert real.exists()
