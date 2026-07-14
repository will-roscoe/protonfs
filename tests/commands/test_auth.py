# tests/commands/test_auth.py
from __future__ import annotations

import pytest
from click.testing import CliRunner

from protonfs.commands.auth import auth_passthrough


class _Result:
    def __init__(self, code: int) -> None:
        self.returncode = code


def test_auth_passthrough_invokes_proton_drive_auth(tmp_path) -> None:
    fake_bin = tmp_path / "proton-drive"
    fake_bin.write_text("#!/bin/sh\n")
    calls: list[list[str]] = []

    def runner(cmd, env=None):
        calls.append(cmd)
        return _Result(0)

    code = auth_passthrough("login", binary=str(fake_bin), runner=runner)

    assert code == 0
    assert calls == [[str(fake_bin), "auth", "login"]]


def test_auth_passthrough_propagates_exit_code(tmp_path) -> None:
    fake_bin = tmp_path / "proton-drive"
    fake_bin.write_text("#!/bin/sh\n")

    def runner(cmd, env=None):
        return _Result(7)

    assert auth_passthrough("status", binary=str(fake_bin), runner=runner) == 7


def test_auth_passthrough_missing_binary_raises_instructive() -> None:
    from protonfs.drive import DriveError

    def runner(cmd, env=None):  # should never be called
        raise AssertionError("runner invoked despite missing binary")

    with pytest.raises(DriveError, match="install-drive"):
        auth_passthrough("login", binary="/no/such/proton-drive", runner=runner)


def test_auth_passthrough_rejects_unknown_subcommand() -> None:
    with pytest.raises(ValueError, match="unknown auth subcommand"):
        auth_passthrough("frobnicate", binary="pd", runner=lambda cmd, env=None: _Result(0))


def test_cli_auth_login_calls_passthrough(monkeypatch) -> None:
    from protonfs import cli

    seen: list[str] = []

    def fake_passthrough(action, binary=None, runner=None):
        seen.append(action)
        return 0

    monkeypatch.setattr("protonfs.commands.auth.auth_passthrough", fake_passthrough)

    result = CliRunner().invoke(cli.main, ["auth", "login"])

    assert result.exit_code == 0
    assert seen == ["login"]


def test_cli_auth_rejects_unknown_action() -> None:
    result = CliRunner().invoke(cli_main(), ["auth", "frobnicate"])
    assert result.exit_code != 0


def test_cli_install_drive_success(monkeypatch) -> None:
    from pathlib import Path

    from protonfs import cli
    from protonfs.install import InstallResult

    def fake_install(version=None):
        return InstallResult(
            path=Path("/home/u/.local/bin/proton-drive"),
            on_path=True,
            sha512="deadbeef",
            warnings=[],
        )

    monkeypatch.setattr("protonfs.install.install_drive", fake_install)

    result = CliRunner().invoke(cli.main, ["install-drive"])

    assert result.exit_code == 0
    assert "Installed proton-drive" in result.output
    assert "auth login" in result.output


def test_cli_install_drive_error_is_clean(monkeypatch) -> None:
    from protonfs import cli
    from protonfs.install import InstallError

    def fake_install(version=None):
        raise InstallError("this CPU lacks AVX2")

    monkeypatch.setattr("protonfs.install.install_drive", fake_install)

    result = CliRunner().invoke(cli.main, ["install-drive"])

    assert result.exit_code != 0
    assert "AVX2" in result.output


def cli_main():
    from protonfs import cli

    return cli.main
