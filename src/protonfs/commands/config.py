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

The ``du.*`` keys (:data:`GLOBAL_ONLY_KEYS`) configure ``protonfs du``, which is not tied
to a store: they live only in the global file, resolve as env var > global > built-in
default, and can be read outside a store.

.. versionadded:: 1.0.0

.. versionchanged:: 3.4.0
   Added the global-only ``du.refresh`` and ``du.plan_size`` keys.
"""
from __future__ import annotations

import os
from pathlib import Path

import click

from protonfs.config import (
    DELETION_POLICIES,
    Config,
    MissingDeviceIdError,
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
from protonfs.drive import parse_clock_skew
from protonfs.usage.policy import RefreshPolicy, parse_size

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
    "defaults.manifest",
    "defaults.skip_thumbnails",
    "defaults.deletions",
    "defaults.clock_skew",
    "du.refresh",
    "du.plan_size",
)

#: Keys that exist only in the global config (``protonfs du`` is not tied to a store),
#: mapped to ``(environment override, built-in default)``. ``None`` means no default.
#:
#: .. versionadded:: 3.4.0
GLOBAL_ONLY_KEYS: dict[str, tuple[str, str | None]] = {
    "du.refresh": ("PROTONFS_DU_REFRESH", "5d"),
    "du.plan_size": ("PROTONFS_PLAN_SIZE", None),
}
_BOOL_KEYS = {
    "defaults.low_io", "defaults.event_log", "defaults.manifest", "defaults.skip_thumbnails"
}
_CHOICE_KEYS = {
    "defaults.progress_style": ("inline", "lines"),
    "defaults.deletions": DELETION_POLICIES,
}


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
    validate against allowed values; ``defaults.clock_skew`` must parse with
    :func:`~protonfs.drive.parse_clock_skew`; every other key is stored as the raw string.

    :param key: the config key being set.
    :param raw: the raw string value from the command line.
    :returns: a ``bool`` for boolean keys, else the unchanged string.
    :raises click.ClickException: when a choice key or ``defaults.clock_skew`` receives an
        invalid value.
    """
    if key in _BOOL_KEYS:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if key in _CHOICE_KEYS:
        choices = _CHOICE_KEYS[key]
        if raw not in choices:
            raise click.ClickException(
                f"invalid value {raw!r} for {key}; choose one of {', '.join(choices)}."
            )
    if key == "defaults.clock_skew":
        try:
            parse_clock_skew(raw)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    validators = {"du.refresh": RefreshPolicy.parse, "du.plan_size": parse_size}
    if key in validators:
        try:
            validators[key](raw)
        except ValueError as exc:
            raise click.ClickException(f"invalid value {raw!r} for {key}: {exc}") from exc
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


def resolve_du_setting(key: str) -> str | None:
    """Resolved value of a global-only ``du.*`` key: env var, else global config, else default.

    :param key: one of :data:`GLOBAL_ONLY_KEYS` (``du.refresh``, ``du.plan_size``).
    :returns: the raw string value, or ``None`` when unset and the key has no default
        (``du.plan_size``).
    :raises KeyError: when ``key`` is not a global-only key.

    .. versionadded:: 3.4.0
    """
    env_var, default = GLOBAL_ONLY_KEYS[key]
    env_value = os.environ.get(env_var)
    if env_value:
        return env_value
    value = _get_nested(load_global_config(), key)
    if value is None or value == "":
        return default
    return str(value)


def config_get(root: Path, key: str) -> str:
    """Return the RESOLVED value of `key` across all layers, as a string.

    Global-only ``du.*`` keys resolve without a store (see :func:`resolve_du_setting`).

    .. versionchanged:: 3.4.0
       Reads ``du.*`` keys outside a store.
    """
    _require_known_key(key)
    if key in GLOBAL_ONLY_KEYS:
        value = resolve_du_setting(key)
        if value is None:
            raise click.ClickException(
                f"{key!r} is not set: protonfs config set --global {key} <value>"
            )
        return value
    try:
        config = load_layered_config(root)
    except MissingDeviceIdError as exc:
        # #151: a clone not yet set up on this machine.
        raise click.ClickException(
            "This repo is set up, but not on this machine yet: no device_id resolved. "
            "It lives in .protonfs/config.local.json, which is gitignored and so never "
            "arrives with a clone. Run `protonfs setup` here to generate one -- it will "
            "not prompt, and will not change the shared config."
        ) from exc
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
    if key in GLOBAL_ONLY_KEYS and scope != "global":
        raise click.ClickException(f"du.* keys are global: use --global ({key})")
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
