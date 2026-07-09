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
    migrate_lfs,
)
from protonfs.config import load_config
from protonfs.context import RepoContext
from protonfs.drive import TransferResult
from protonfs.index import IndexStore


class _FakeDrive:
    def __init__(self, version: str | None = "v0.4.6", authed: bool = True) -> None:
        self._version = version
        self._authed = authed

    def version(self) -> str | None:
        return self._version

    def is_authenticated(self) -> bool:
        return self._authed


def test_ensure_cli_present_raises_when_missing() -> None:
    with pytest.raises(click.ClickException):
        ensure_cli_present(_FakeDrive(version=None))


def test_ensure_cli_present_returns_version_string() -> None:
    assert ensure_cli_present(_FakeDrive(version="v0.4.6")) == "v0.4.6"


def test_ensure_authenticated_raises_when_not_authed() -> None:
    with pytest.raises(click.ClickException):
        ensure_authenticated(_FakeDrive(authed=False))


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


def test_migrate_lfs_is_noop_when_not_lfs_tracked(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(root=tmp_path, config=config, index=IndexStore(tmp_path), drive=_FakeDrive())

    performed = migrate_lfs(ctx, dry_run=False)

    assert performed is False


def test_migrate_lfs_dry_run_reports_without_acting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(root=tmp_path, config=config, index=IndexStore(tmp_path), drive=_FakeDrive())

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(root=tmp_path, config=config, index=IndexStore(tmp_path), drive=_FakeDrive())

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(root=tmp_path, config=config, index=IndexStore(tmp_path), drive=_FakeDrive())

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(root=tmp_path, config=config, index=IndexStore(tmp_path), drive=_FakeDrive())

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
