from __future__ import annotations

import json
from pathlib import Path

from protonfs.config import Config, Defaults, init_config, load_config, save_config


def test_load_config_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_config(tmp_path) is None


def test_init_config_creates_file_with_generated_device_id(tmp_path: Path) -> None:
    config = init_config(tmp_path, "/my-files/test")
    assert config.remote_root == "/my-files/test"
    assert len(config.device_id) == 36  # uuid4 string form
    on_disk = json.loads((tmp_path / ".protonfs" / "config.json").read_text())
    assert on_disk["remote_root"] == "/my-files/test"
    assert on_disk["device_id"] == config.device_id


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
