from __future__ import annotations

import json
from pathlib import Path

import pytest

from protonfs.config import (
    Config,
    Defaults,
    init_config,
    load_config,
    load_layered_config,
    load_local_config,
    migrate_device_id_to_local,
    save_config,
    save_local_config,
)


def test_load_config_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_config(tmp_path) is None


def test_init_config_creates_shared_file_without_device_id(tmp_path: Path) -> None:
    # #21: device_id goes to config.local.json, not the shared/committed config.json.
    config = init_config(tmp_path, "/my-files/test")
    assert config.remote_root == "/my-files/test"
    assert len(config.device_id) == 36  # uuid4 string form

    on_disk = json.loads((tmp_path / ".protonfs" / "config.json").read_text())
    assert on_disk["remote_root"] == "/my-files/test"
    assert "device_id" not in on_disk


def test_init_config_writes_device_id_to_local_file(tmp_path: Path) -> None:
    config = init_config(tmp_path, "/my-files/test")
    local_on_disk = json.loads((tmp_path / ".protonfs" / "config.local.json").read_text())
    assert local_on_disk["device_id"] == config.device_id


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    original = Config(
        remote_root="/my-files/x",
        device_id="abc-123",
        defaults=Defaults(on_conflict="replace", low_io=True),
    )
    save_config(tmp_path, original)
    loaded = load_config(tmp_path)
    assert loaded == original


def test_load_config_defaults_missing_defaults_block(tmp_path: Path) -> None:
    config_dir = tmp_path / ".protonfs"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"remote_root": "/x", "device_id": "d1"}))
    loaded = load_config(tmp_path)
    assert loaded.defaults == Defaults()


class TestLayeredConfig:
    def test_returns_none_when_shared_file_missing(self, tmp_path: Path) -> None:
        assert load_layered_config(tmp_path) is None

    def test_resolves_device_id_from_local_over_shared(self, tmp_path: Path) -> None:
        # Backward compat: an OLD-layout repo with device_id embedded in config.json
        # still resolves -- but a device_id in config.local.json wins if both are present.
        save_config(
            tmp_path,
            Config(remote_root="/my-files/x", device_id="shared-device"),
        )
        save_local_config(tmp_path, {"device_id": "local-device"})
        resolved = load_layered_config(tmp_path)
        assert resolved.device_id == "local-device"

    def test_backward_compat_device_id_in_shared_file_alone(self, tmp_path: Path) -> None:
        save_config(
            tmp_path,
            Config(remote_root="/my-files/x", device_id="shared-device"),
        )
        resolved = load_layered_config(tmp_path)
        assert resolved.device_id == "shared-device"
        assert resolved.remote_root == "/my-files/x"

    def test_missing_device_id_in_every_layer_raises(self, tmp_path: Path) -> None:
        (tmp_path / ".protonfs").mkdir()
        (tmp_path / ".protonfs" / "config.json").write_text(
            json.dumps({"remote_root": "/my-files/x"})
        )
        with pytest.raises(ValueError):
            load_layered_config(tmp_path)

    def test_global_layer_beaten_by_shared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        global_path = tmp_path / "global-config.json"
        global_path.write_text(json.dumps({"defaults": {"on_conflict": "replace"}}))
        monkeypatch.setenv("PROTONFS_CONFIG", str(global_path))

        repo = tmp_path / "repo"
        repo.mkdir()
        init_config(repo, "/my-files/repo")

        resolved = load_layered_config(repo)
        # shared config.json didn't set on_conflict -> global's value wins over built-in default
        assert resolved.defaults.on_conflict == "replace"

    def test_shared_beats_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        global_path = tmp_path / "global-config.json"
        global_path.write_text(json.dumps({"defaults": {"on_conflict": "replace"}}))
        monkeypatch.setenv("PROTONFS_CONFIG", str(global_path))

        repo = tmp_path / "repo"
        repo.mkdir()
        config = init_config(repo, "/my-files/repo")
        config.defaults.on_conflict = "keep-both"
        save_config(repo, config)

        resolved = load_layered_config(repo)
        assert resolved.defaults.on_conflict == "keep-both"

    def test_local_beats_shared(self, tmp_path: Path) -> None:
        config = init_config(tmp_path, "/my-files/repo")
        config.defaults.low_io = False
        save_config(tmp_path, config)
        local_data = load_local_config(tmp_path)
        local_data["defaults"] = {"low_io": True}
        save_local_config(tmp_path, local_data)

        resolved = load_layered_config(tmp_path)
        assert resolved.defaults.low_io is True

    def test_env_beats_everything(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        init_config(tmp_path, "/my-files/repo")
        local_data = load_local_config(tmp_path)
        local_data["defaults"] = {"low_io": False}
        save_local_config(tmp_path, local_data)

        monkeypatch.setenv("PROTONFS_LOW_IO", "true")
        resolved = load_layered_config(tmp_path)
        assert resolved.defaults.low_io is True

    def test_env_device_id_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        init_config(tmp_path, "/my-files/repo")
        monkeypatch.setenv("PROTONFS_DEVICE_ID", "env-device")
        resolved = load_layered_config(tmp_path)
        assert resolved.device_id == "env-device"

    def test_missing_global_layer_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROTONFS_CONFIG", str(tmp_path / "does-not-exist.json"))
        config = init_config(tmp_path, "/my-files/repo")
        resolved = load_layered_config(tmp_path)
        assert resolved.remote_root == "/my-files/repo"
        assert resolved.device_id == config.device_id

    def test_missing_local_layer_after_shared_only_setup_raises(self, tmp_path: Path) -> None:
        # New-layout repo has device_id ONLY in config.local.json; delete it and there's
        # no layer left to resolve device_id from.
        init_config(tmp_path, "/my-files/repo")
        (tmp_path / ".protonfs" / "config.local.json").unlink()
        with pytest.raises(ValueError):
            load_layered_config(tmp_path)


class TestMigrateDeviceIdToLocal:
    def test_moves_device_id_from_shared_to_local(self, tmp_path: Path) -> None:
        save_config(tmp_path, Config(remote_root="/my-files/x", device_id="old-device"))
        assert not (tmp_path / ".protonfs" / "config.local.json").exists()

        moved = migrate_device_id_to_local(tmp_path)

        assert moved is True
        shared_on_disk = json.loads((tmp_path / ".protonfs" / "config.json").read_text())
        assert "device_id" not in shared_on_disk
        local_data = load_local_config(tmp_path)
        assert local_data["device_id"] == "old-device"

    def test_noop_when_shared_has_no_device_id(self, tmp_path: Path) -> None:
        init_config(tmp_path, "/my-files/x")  # already new-layout: local has it
        shared_before = (tmp_path / ".protonfs" / "config.json").read_text()
        moved = migrate_device_id_to_local(tmp_path)
        assert moved is False
        assert (tmp_path / ".protonfs" / "config.json").read_text() == shared_before

    def test_does_not_overwrite_existing_local_device_id(self, tmp_path: Path) -> None:
        save_config(tmp_path, Config(remote_root="/my-files/x", device_id="shared-device"))
        save_local_config(tmp_path, {"device_id": "local-device"})

        migrate_device_id_to_local(tmp_path)

        local_data = load_local_config(tmp_path)
        assert local_data["device_id"] == "local-device"


def test_defaults_have_event_log_and_progress_style() -> None:
    from protonfs.config import Defaults
    d = Defaults()
    assert d.event_log is False
    assert d.progress_style == "inline"


def test_from_dict_reads_new_defaults(tmp_path) -> None:
    from protonfs.config import Config
    cfg = Config.from_dict(
        {"remote_root": "/x", "defaults": {"event_log": True, "progress_style": "lines"}}
    )
    assert cfg.defaults.event_log is True
    assert cfg.defaults.progress_style == "lines"


def test_env_overrides_event_log_and_progress_style(monkeypatch, tmp_path) -> None:
    from protonfs.config import init_config, load_layered_config
    init_config(tmp_path, "/my-files/test")
    monkeypatch.setenv("PROTONFS_EVENT_LOG", "true")
    monkeypatch.setenv("PROTONFS_PROGRESS_STYLE", "lines")
    cfg = load_layered_config(tmp_path)
    assert cfg.defaults.event_log is True
    assert cfg.defaults.progress_style == "lines"


def test_defaults_batch_size_matches_batching_default() -> None:
    from protonfs.batching import DEFAULT_BATCH_SIZE
    from protonfs.config import Defaults
    assert Defaults().batch_size == DEFAULT_BATCH_SIZE


def test_from_dict_reads_batch_size(tmp_path) -> None:
    from protonfs.config import Config
    cfg = Config.from_dict({"remote_root": "/x", "defaults": {"batch_size": 50}})
    assert cfg.defaults.batch_size == 50


def test_to_dict_omits_default_batch_size_but_writes_custom(tmp_path) -> None:
    from protonfs.batching import DEFAULT_BATCH_SIZE
    from protonfs.config import Config
    default = Config(remote_root="/x", device_id="d")
    assert "batch_size" not in default.to_dict().get("defaults", {})
    default.defaults.batch_size = DEFAULT_BATCH_SIZE + 25
    assert default.to_dict()["defaults"]["batch_size"] == DEFAULT_BATCH_SIZE + 25


def test_env_overrides_batch_size_as_int(monkeypatch, tmp_path) -> None:
    from protonfs.config import init_config, load_layered_config
    init_config(tmp_path, "/my-files/test")
    monkeypatch.setenv("PROTONFS_BATCH_SIZE", "40")
    cfg = load_layered_config(tmp_path)
    assert cfg.defaults.batch_size == 40


def test_env_batch_size_below_one_clamps_to_one(monkeypatch, tmp_path) -> None:
    # A non-positive batch size would make batches() produce no chunks; clamp defensively.
    from protonfs.config import init_config, load_layered_config
    init_config(tmp_path, "/my-files/test")
    monkeypatch.setenv("PROTONFS_BATCH_SIZE", "0")
    cfg = load_layered_config(tmp_path)
    assert cfg.defaults.batch_size == 1
