from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR_NAME = ".protonfs"
CONFIG_FILE_NAME = "config.json"
LOCAL_CONFIG_FILE_NAME = "config.local.json"

# $PROTONFS_CONFIG overrides the global config file path outright (#21).
GLOBAL_CONFIG_ENV = "PROTONFS_CONFIG"

# Per-key env var overrides, highest-precedence layer in `load_layered_config` (#21).
_ENV_KEY_OVERRIDES = {
    "remote_root": "PROTONFS_REMOTE_ROOT",
    "device_id": "PROTONFS_DEVICE_ID",
}
_ENV_DEFAULTS_OVERRIDES = {
    "on_conflict": "PROTONFS_ON_CONFLICT",
    "low_io": "PROTONFS_LOW_IO",
}


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
        data: dict = {"remote_root": self.remote_root}
        # Omit an empty device_id rather than persisting a placeholder (#21): new setups
        # write the real device_id to config.local.json instead -- see `init_config`.
        if self.device_id:
            data["device_id"] = self.device_id
        # Only persist `defaults` fields that differ from the built-in default (#21): a
        # fresh repo's config.json should stay silent on fields it never customized, so a
        # global config layer's own default for that field can still take effect. A field
        # this repo DOES set explicitly is written and always wins over the global layer.
        builtin = Defaults()
        overrides = {
            key: value
            for key, value in asdict(self.defaults).items()
            if value != getattr(builtin, key)
        }
        if overrides:
            data["defaults"] = overrides
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        defaults_data = data.get("defaults", {})
        return cls(
            remote_root=data["remote_root"],
            device_id=data.get("device_id", ""),
            defaults=Defaults(
                on_conflict=defaults_data.get("on_conflict", "skip"),
                low_io=defaults_data.get("low_io", False),
            ),
        )


def config_dir(repo_root: Path) -> Path:
    return repo_root / CONFIG_DIR_NAME


def config_path(repo_root: Path) -> Path:
    return config_dir(repo_root) / CONFIG_FILE_NAME


def local_config_path(repo_root: Path) -> Path:
    """Per-device, gitignored config (`.protonfs/config.local.json`, #21).

    Holds `device_id` and any per-device overrides (e.g. `low_io`) that must never be
    committed -- see the `.protonfs/.gitignore` line `write_git_control_files` adds.
    """
    return config_dir(repo_root) / LOCAL_CONFIG_FILE_NAME


def global_config_path() -> Path:
    """Path to the global user/machine config (#21).

    XDG-style: `$XDG_CONFIG_HOME/protonfs/config.json`, falling back to
    `~/.config/protonfs/config.json`. `$PROTONFS_CONFIG`, if set, overrides the path
    outright (points directly at the file, not a directory).
    """
    override = os.environ.get(GLOBAL_CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home).expanduser() if xdg_config_home else Path.home() / ".config"
    return base / "protonfs" / "config.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    # Same atomic-write pattern as IndexStore.save: write to a temp file in the same
    # directory (same filesystem, so os.replace is a true atomic rename), so a reader --
    # or a crash -- never sees a torn or truncated config file.
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_json_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text().strip()
    if not text:
        return {}
    return json.loads(text)


def load_config(repo_root: Path) -> Config | None:
    """Read the per-repo SHARED config file (`.protonfs/config.json`) only.

    This is the committed contract: `remote_root` + `defaults`. Repos set up before
    per-device layering existed (#21) may also have `device_id` in this file -- tolerated
    here for backward compatibility, but new setups write `device_id` to
    `config.local.json` instead. For the fully resolved config across all layers
    (global/shared/local/env), use `load_layered_config`.
    """
    path = config_path(repo_root)
    if not path.exists():
        return None
    return Config.from_dict(json.loads(path.read_text()))


def save_config(repo_root: Path, config: Config) -> None:
    _atomic_write_json(config_path(repo_root), config.to_dict())


def load_local_config(repo_root: Path) -> dict:
    """Read the per-device local config (`.protonfs/config.local.json`, #21), if present."""
    return _read_json_dict(local_config_path(repo_root))


def save_local_config(repo_root: Path, data: dict) -> None:
    _atomic_write_json(local_config_path(repo_root), data)


def load_global_config() -> dict:
    """Read the global user/machine config (#21), if present."""
    return _read_json_dict(global_config_path())


def save_global_config(data: dict) -> None:
    _atomic_write_json(global_config_path(), data)


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_bool_env(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_layer() -> dict:
    """The env-var layer: highest precedence, only known keys are recognised (#21)."""
    layer: dict = {}
    for key, env_name in _ENV_KEY_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is not None:
            layer[key] = value
    defaults_layer: dict = {}
    for key, env_name in _ENV_DEFAULTS_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None:
            continue
        defaults_layer[key] = _parse_bool_env(value) if key == "low_io" else value
    if defaults_layer:
        layer["defaults"] = defaults_layer
    return layer


def load_layered_config(repo_root: Path) -> Config | None:
    """Resolve the effective config for `repo_root` across all layers (#21), highest wins::

        env vars
          > .protonfs/config.local.json        (per-device, gitignored)
            > .protonfs/config.json            (per-repo shared, committed)
              > ~/.config/protonfs/config.json (global user defaults; $PROTONFS_CONFIG overrides)
                > built-in defaults

    Returns None when the per-repo shared file is absent, matching `load_config`'s
    contract: a global or local file alone does not mean this repo is "set up". Raises
    `ValueError` if no layer resolves a `device_id` (shouldn't happen for a repo that went
    through `protonfs setup`, since `init_config` always writes one).
    """
    shared_path = config_path(repo_root)
    if not shared_path.exists():
        return None

    merged: dict = {}
    merged = _deep_merge(merged, load_global_config())
    merged = _deep_merge(merged, _read_json_dict(shared_path))
    merged = _deep_merge(merged, load_local_config(repo_root))
    merged = _deep_merge(merged, _env_layer())

    if not merged.get("device_id"):
        raise ValueError(
            "no device_id resolved for this repo (checked config.local.json, config.json, "
            "the global config, and env vars). Run `protonfs setup` to generate one."
        )
    return Config.from_dict(merged)


def migrate_device_id_to_local(repo_root: Path) -> bool:
    """Move a `device_id` living in the (old-layout) shared `config.json` into the
    per-device `config.local.json` (#21). Backward-compat: existing repos that predate
    per-device layering keep working via `load_layered_config` without this, since it
    reads `device_id` from either layer -- this just cleans up the layout, straightforward
    and idempotent, so `protonfs setup` can call it on every run. No-ops (returns False)
    when there's nothing to migrate: no shared file, no device_id in it, or a device_id
    already present locally (local wins either way, so we never overwrite it).
    """
    shared_path = config_path(repo_root)
    if not shared_path.exists():
        return False
    shared_data = _read_json_dict(shared_path)
    device_id = shared_data.get("device_id")
    if not device_id:
        return False
    local_data = load_local_config(repo_root)
    if not local_data.get("device_id"):
        local_data["device_id"] = device_id
        save_local_config(repo_root, local_data)
    shared_data.pop("device_id", None)
    _atomic_write_json(shared_path, shared_data)
    return True


def init_config(repo_root: Path, remote_root: str) -> Config:
    """Initialize a new repo (#21): `remote_root` + `defaults` go to the SHARED
    `config.json` (committed); a freshly generated `device_id` goes to the per-device
    `config.local.json` (gitignored). Returns the fully resolved `Config`.
    """
    device_id = str(uuid.uuid4())
    save_config(repo_root, Config(remote_root=remote_root, device_id=""))
    save_local_config(repo_root, {"device_id": device_id})
    return Config(remote_root=remote_root, device_id=device_id)
