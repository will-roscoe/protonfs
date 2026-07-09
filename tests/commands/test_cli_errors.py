# tests/commands/test_cli_errors.py
from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from protonfs.cli import _drive_error_boundary, main
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
    assert "proton-drive auth login" in message


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
    assert "proton-drive auth login" in result.output
    assert not isinstance(result.exception, DriveError)
