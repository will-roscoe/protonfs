# tests/commands/test_cli_errors.py
from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from protonfs.cli import _drive_error_boundary, main
from protonfs.config import Config
from protonfs.drive import DriveAuthError, DriveError


def test_drive_error_boundary_converts_drive_error_to_click_exception() -> None:
    @_drive_error_boundary
    def boom():
        raise DriveError("network unreachable")

    with pytest.raises(click.ClickException) as excinfo:
        boom()
    assert "network unreachable" in str(excinfo.value)


def test_drive_error_boundary_converts_drive_auth_error_with_tailored_message() -> None:
    @_drive_error_boundary
    def boom():
        raise DriveAuthError("session expired")

    with pytest.raises(click.ClickException) as excinfo:
        boom()
    message = str(excinfo.value)
    assert "session expired" in message
    assert "protonfs auth login" in message


def test_ls_command_reports_clean_error_on_drive_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args, **kwargs):
        raise DriveError("proton-drive request failed")

    monkeypatch.setattr("protonfs.context.load_context", _raise)

    result = CliRunner().invoke(main, ["ls"])

    assert result.exit_code != 0
    assert "proton-drive request failed" in result.output
    assert not isinstance(result.exception, DriveError)


def test_ls_command_reports_auth_hint_on_drive_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*args, **kwargs):
        raise DriveAuthError("not authenticated")

    monkeypatch.setattr("protonfs.context.load_context", _raise)

    result = CliRunner().invoke(main, ["ls"])

    assert result.exit_code != 0
    assert "not authenticated" in result.output
    assert "protonfs auth login" in result.output
    assert not isinstance(result.exception, DriveError)


def test_setup_command_reports_clean_error_on_drive_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DriveError raised mid-`setup` (e.g. from `ensure_remote_root`) must surface as
    a clean ClickException, not an uncaught traceback -- `setup` fronts the same Drive
    calls as the mutating commands but historically lacked the error boundary."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("protonfs.commands.setup.ensure_cli_present", lambda drive: "1.0")
    monkeypatch.setattr("protonfs.commands.setup.ensure_secrets", lambda drive: None)
    monkeypatch.setattr("protonfs.commands.setup.ensure_authenticated", lambda drive: None)
    monkeypatch.setattr(
        "protonfs.commands.setup.ensure_config",
        lambda root: Config(remote_root="/my-files/test", device_id="d1"),
    )
    monkeypatch.setattr("protonfs.commands.setup.init_ignore", lambda root: None)
    monkeypatch.setattr("protonfs.commands.setup.init_include", lambda root: None)
    monkeypatch.setattr("protonfs.commands.setup.write_git_control_files", lambda root: None)

    def _raise(*args, **kwargs):
        raise DriveError("remote_root is not under a known Drive area")

    monkeypatch.setattr("protonfs.commands.setup.ensure_remote_root", _raise)

    result = CliRunner().invoke(main, ["setup"])

    assert result.exit_code != 0
    assert "remote_root is not under a known Drive area" in result.output
    assert not isinstance(result.exception, DriveError)
