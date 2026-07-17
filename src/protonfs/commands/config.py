# src/protonfs/commands/config.py
"""`protonfs config get/set` -- inspect and edit the layered config (#21).

Layers, highest wins::

    env vars
      > .protonfs/config.local.json        (per-device, gitignored)
        > .protonfs/config.json            (per-repo shared, committed)
          > ~/.config/protonfs/config.json (global user defaults; $PROTONFS_CONFIG overrides)
            > built-in defaults

`get` always reports the fully RESOLVED value. `set` writes to exactly one layer:
the shared repo file by default, or the global/local file with `--global`/`--local`.
"""
from __future__ import annotations

from pathlib import Path

import click

from protonfs.config import (
    Config,
    config_path,
    global_config_path,
    load_config,
    load_global_config,
    load_layered_config,
    load_local_config,
    local_config_path,
    save_config,
    save_global_config,
    save_local_config,
)

# Keys settable/gettable through this command -- kept in lockstep with the `Config`
# dataclass shape (#21 asks that it stay stable; this is the only place that needs to
# know its field layout for dotted-path access).
KNOWN_KEYS = (
    "remote_root",
    "device_id",
    "defaults.on_conflict",
    "defaults.low_io",
    "defaults.event_log",
    "defaults.progress_style",
)
_BOOL_KEYS = {"defaults.low_io", "defaults.event_log"}
_CHOICE_KEYS = {"defaults.progress_style": ("inline", "lines")}


def _get_nested(data: dict, dotted_key: str):
    """Read a value from a nested dict by dotted key, or ``None`` if any segment is absent.

    :param data: the (possibly nested) config dict.
    :param dotted_key: a ``a.b.c`` path into it.
    :returns: the value at that path, or ``None`` when the path does not resolve.
    """
    node = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_nested(data: dict, dotted_key: str, value) -> None:
    """Set a value in a nested dict by dotted key, creating intermediate dicts as needed.

    :param data: the dict to mutate in place.
    :param dotted_key: a ``a.b.c`` path; intermediate levels are auto-created.
    :param value: the value to store at the leaf.
    """
    parts = dotted_key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _coerce_value(key: str, raw: str):
    """Coerce a raw string CLI value to the type the config key expects.

    Boolean keys accept ``1/true/yes/on`` (case-insensitive) as true; choice keys
    validate against allowed values; every other key is stored as the raw string.

    :param key: the config key being set.
    :param raw: the raw string value from the command line.
    :returns: a ``bool`` for boolean keys, else the unchanged string.
    :raises click.ClickException: when a choice key receives an invalid value.
    """
    if key in _BOOL_KEYS:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if key in _CHOICE_KEYS:
        choices = _CHOICE_KEYS[key]
        if raw not in choices:
            raise click.ClickException(
                f"invalid value {raw!r} for {key}; choose one of {', '.join(choices)}."
            )
    return raw


def _require_known_key(key: str) -> None:
    """Reject an unknown config key with a helpful list of the known ones.

    :param key: the key to validate.
    :raises click.ClickException: when ``key`` is not a recognised config key.
    """
    if key not in KNOWN_KEYS:
        raise click.ClickException(
            f"unknown config key {key!r}. Known keys: {', '.join(KNOWN_KEYS)}"
        )


def config_get(root: Path, key: str) -> str:
    """Return the RESOLVED value of `key` across all layers, as a string."""
    _require_known_key(key)
    config = load_layered_config(root)
    if config is None:
        raise click.ClickException(
            "protonfs is not set up in this directory. Run `protonfs setup` first."
        )
    value = _get_nested(config.to_dict(), key)
    if value is None or value == "":
        raise click.ClickException(f"{key!r} is not set in any config layer.")
    return str(value)


def config_set(root: Path, key: str, value: str, scope: str = "shared") -> Path:
    """Write `key` = `value` (dotted path, e.g. `defaults.low_io`) into one layer.

    `scope`: "shared" (default, `.protonfs/config.json`, committed), "local"
    (`.protonfs/config.local.json`, gitignored, per-device), or "global"
    (`~/.config/protonfs/config.json`, or `$PROTONFS_CONFIG`). Returns the path written.
    """
    _require_known_key(key)
    coerced = _coerce_value(key, value)

    if scope == "global":
        data = load_global_config()
        _set_nested(data, key, coerced)
        save_global_config(data)
        return global_config_path()

    if scope == "local":
        data = load_local_config(root)
        _set_nested(data, key, coerced)
        save_local_config(root, data)
        return local_config_path(root)

    if scope != "shared":
        raise ValueError(f"unknown scope: {scope!r}")

    existing = load_config(root)
    data = existing.to_dict() if existing is not None else {}
    _set_nested(data, key, coerced)
    if "remote_root" not in data:
        raise click.ClickException(
            "no shared config for this repo yet -- run `protonfs setup` first, or set "
            "remote_root explicitly: `protonfs config set remote_root <path>`."
        )
    save_config(root, Config.from_dict(data))
    return config_path(root)
