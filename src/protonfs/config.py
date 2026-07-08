from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR_NAME = ".protonfs"
CONFIG_FILE_NAME = "config.json"


@dataclass
class Defaults:
    on_conflict: str = "skip"
    low_io: bool = False


@dataclass
class Config:
    remote_root: str
    device_id: str
    defaults: Defaults = field(default_factory=Defaults)

    def to_dict(self) -> dict:
        return {
            "remote_root": self.remote_root,
            "device_id": self.device_id,
            "defaults": asdict(self.defaults),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        defaults_data = data.get("defaults", {})
        return cls(
            remote_root=data["remote_root"],
            device_id=data["device_id"],
            defaults=Defaults(
                on_conflict=defaults_data.get("on_conflict", "skip"),
                low_io=defaults_data.get("low_io", False),
            ),
        )


def config_dir(repo_root: Path) -> Path:
    return repo_root / CONFIG_DIR_NAME


def config_path(repo_root: Path) -> Path:
    return config_dir(repo_root) / CONFIG_FILE_NAME


def load_config(repo_root: Path) -> Config | None:
    path = config_path(repo_root)
    if not path.exists():
        return None
    return Config.from_dict(json.loads(path.read_text()))


def save_config(repo_root: Path, config: Config) -> None:
    config_dir(repo_root).mkdir(parents=True, exist_ok=True)
    config_path(repo_root).write_text(json.dumps(config.to_dict(), indent=2) + "\n")


def init_config(repo_root: Path, remote_root: str) -> Config:
    config = Config(remote_root=remote_root, device_id=str(uuid.uuid4()))
    save_config(repo_root, config)
    return config
