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


def test_config_set_progress_style_rejects_bad_value(tmp_path, monkeypatch) -> None:
    import click
    import pytest

    from protonfs.commands.config import config_set
    from protonfs.config import init_config
    init_config(tmp_path, "/my-files/test")
    with pytest.raises(click.ClickException):
        config_set(tmp_path, "defaults.progress_style", "sideways")


def test_config_set_event_log_bool(tmp_path) -> None:
    from protonfs.commands.config import config_get, config_set
    from protonfs.config import init_config
    init_config(tmp_path, "/my-files/test")
    config_set(tmp_path, "defaults.event_log", "on")
    assert config_get(tmp_path, "defaults.event_log") == "True"
