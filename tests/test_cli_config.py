from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from protonfs.cli import main
from protonfs.config import init_config


def test_cli_config_get_and_set_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    init_config(tmp_path, "/my-files/test")

    result = CliRunner().invoke(main, ["config", "get", "remote_root"])
    assert result.exit_code == 0
    assert result.output.strip() == "/my-files/test"

    result = CliRunner().invoke(main, ["config", "set", "defaults.on_conflict", "replace"])
    assert result.exit_code == 0
    assert "config.json" in result.output

    result = CliRunner().invoke(main, ["config", "get", "defaults.on_conflict"])
    assert result.exit_code == 0
    assert result.output.strip() == "replace"


def test_cli_config_set_local_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    init_config(tmp_path, "/my-files/test")

    result = CliRunner().invoke(main, ["config", "set", "defaults.low_io", "true", "--local"])
    assert result.exit_code == 0
    assert "config.local.json" in result.output


def test_cli_config_set_global_and_local_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    init_config(tmp_path, "/my-files/test")

    result = CliRunner().invoke(
        main, ["config", "set", "defaults.low_io", "true", "--global", "--local"]
    )
    assert result.exit_code != 0


def test_cli_config_get_missing_repo_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["config", "get", "remote_root"])
    assert result.exit_code != 0
