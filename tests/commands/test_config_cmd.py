# tests/commands/test_config_cmd.py
from __future__ import annotations

import json
from pathlib import Path

import click
import pytest

from protonfs.commands.config import config_get, config_set, resolve_du_setting
from protonfs.config import init_config, load_config, load_local_config


def test_config_get_resolves_shared_value(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    assert config_get(tmp_path, "remote_root") == "/my-files/test"


def test_config_get_resolves_device_id_from_local(tmp_path: Path) -> None:
    config = init_config(tmp_path, "/my-files/test")
    assert config_get(tmp_path, "device_id") == config.device_id


def test_config_get_raises_when_not_set_up(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException):
        config_get(tmp_path, "remote_root")


def test_config_get_raises_for_unknown_key(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    with pytest.raises(click.ClickException):
        config_get(tmp_path, "nonsense")


def test_config_get_env_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_config(tmp_path, "/my-files/test")
    monkeypatch.setenv("PROTONFS_REMOTE_ROOT", "/my-files/env-wins")
    assert config_get(tmp_path, "remote_root") == "/my-files/env-wins"


def test_config_set_shared_writes_committed_file(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    path = config_set(tmp_path, "defaults.on_conflict", "replace", scope="shared")
    assert path == tmp_path / ".protonfs" / "config.json"
    assert load_config(tmp_path).defaults.on_conflict == "replace"


def test_config_set_local_writes_gitignored_file(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    path = config_set(tmp_path, "defaults.low_io", "true", scope="local")
    assert path == tmp_path / ".protonfs" / "config.local.json"
    assert load_local_config(tmp_path)["defaults"]["low_io"] is True
    # shared file untouched
    assert "low_io" not in json.loads(
        (tmp_path / ".protonfs" / "config.json").read_text()
    ).get("defaults", {})


def test_config_set_global_writes_xdg_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_path = tmp_path / "global.json"
    monkeypatch.setenv("PROTONFS_CONFIG", str(global_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    init_config(repo, "/my-files/test")

    written_path = config_set(repo, "defaults.on_conflict", "keep-both", scope="global")

    assert written_path == global_path
    assert json.loads(global_path.read_text())["defaults"]["on_conflict"] == "keep-both"
    # resolved value now comes from the global layer
    assert config_get(repo, "defaults.on_conflict") == "keep-both"


def test_config_set_low_io_bool_roundtrips_as_bool(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    config_set(tmp_path, "defaults.low_io", "yes", scope="shared")
    on_disk = json.loads((tmp_path / ".protonfs" / "config.json").read_text())
    assert on_disk["defaults"]["low_io"] is True


def test_config_set_unknown_key_raises(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    with pytest.raises(click.ClickException):
        config_set(tmp_path, "nonsense", "x", scope="shared")


def test_config_set_shared_without_existing_config_requires_remote_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(click.ClickException):
        config_set(tmp_path, "defaults.low_io", "true", scope="shared")


def test_config_set_shared_remote_root_bootstraps_config(tmp_path: Path) -> None:
    path = config_set(tmp_path, "remote_root", "/my-files/fresh", scope="shared")
    assert path == tmp_path / ".protonfs" / "config.json"
    assert load_config(tmp_path).remote_root == "/my-files/fresh"


# --- du.* keys (protonfs du, 3.4.0): global-only, readable outside a store ------------


def test_du_keys_are_global_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONFS_CONFIG", str(tmp_path / "global.json"))
    init_config(tmp_path, "/my-files/test")
    for scope in ("shared", "local"):
        with pytest.raises(click.ClickException, match="du.\\* keys are global: use --global"):
            config_set(tmp_path, "du.refresh", "2d", scope=scope)


def test_du_keys_set_and_get_outside_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_path = tmp_path / "global.json"
    monkeypatch.setenv("PROTONFS_CONFIG", str(global_path))
    nowhere = tmp_path / "not-a-store"
    nowhere.mkdir()

    assert config_set(nowhere, "du.refresh", "2h45m", scope="global") == global_path
    config_set(nowhere, "du.plan_size", "3TB", scope="global")

    assert json.loads(global_path.read_text())["du"] == {"refresh": "2h45m", "plan_size": "3TB"}
    assert config_get(nowhere, "du.refresh") == "2h45m"
    assert config_get(nowhere, "du.plan_size") == "3TB"


def test_du_refresh_defaults_to_5d_and_plan_size_has_no_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROTONFS_CONFIG", str(tmp_path / "global.json"))
    assert resolve_du_setting("du.refresh") == "5d"
    assert resolve_du_setting("du.plan_size") is None
    assert config_get(tmp_path, "du.refresh") == "5d"
    with pytest.raises(click.ClickException, match="not set"):
        config_get(tmp_path, "du.plan_size")


def test_du_env_overrides_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONFS_CONFIG", str(tmp_path / "global.json"))
    config_set(tmp_path, "du.refresh", "2d", scope="global")
    monkeypatch.setenv("PROTONFS_DU_REFRESH", "never")
    monkeypatch.setenv("PROTONFS_PLAN_SIZE", "500GB")
    assert resolve_du_setting("du.refresh") == "never"
    assert resolve_du_setting("du.plan_size") == "500GB"


@pytest.mark.parametrize(("key", "bad"), [("du.refresh", "5"), ("du.plan_size", "lots")])
def test_du_values_are_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, bad: str
) -> None:
    monkeypatch.setenv("PROTONFS_CONFIG", str(tmp_path / "global.json"))
    with pytest.raises(click.ClickException, match=key):
        config_set(tmp_path, key, bad, scope="global")
