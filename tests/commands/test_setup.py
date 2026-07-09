# tests/commands/test_setup.py
from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from protonfs.commands.setup import (
    clean_pointer_stubs,
    ensure_authenticated,
    ensure_cli_present,
    ensure_config,
    migrate_lfs,
)
from protonfs.config import load_config
from protonfs.context import RepoContext
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


def test_clean_pointer_stubs_removes_stub_files(tmp_path: Path) -> None:
    stub = tmp_path / "dump_0001"
    stub.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 1\n")
    real = tmp_path / "dump_0002"
    real.write_bytes(b"real data")

    removed = clean_pointer_stubs(tmp_path)

    assert removed == 1
    assert not stub.exists()
    assert real.exists()
