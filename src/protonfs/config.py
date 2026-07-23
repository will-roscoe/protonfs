"""Layered configuration for a protonfs repo.

Four layers combine into the effective :class:`Config` for a repo, highest precedence
first: environment variables, the per-device local file (``.protonfs/config.local.json``,
gitignored), the per-repo shared file (``.protonfs/config.json``, committed), and the
global user file (``~/.config/protonfs/config.json``, or ``$PROTONFS_CONFIG``). See
:func:`load_layered_config` for the full precedence chain and :func:`init_config` for how
a new repo's layers are populated (#21).

All writes go through :func:`_atomic_write_json`, which writes a temp file in the target
directory and ``os.replace`` moves it into place, so a reader (or a crash mid-write) never
observes a torn or truncated config file.

.. versionadded:: 1.0.0
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from protonfs.batching import DEFAULT_BATCH_SIZE

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
    "event_log": "PROTONFS_EVENT_LOG",
    "progress_style": "PROTONFS_PROGRESS_STYLE",
    "batch_size": "PROTONFS_BATCH_SIZE",
}


@dataclass
class Defaults:
    """Built-in fallback values for repo-configurable sync behaviour.

    :ivar on_conflict: Default action when a file is in :class:`~protonfs.diff.SyncState`
        conflict (e.g. ``"skip"``); overridable per-repo or via ``$PROTONFS_ON_CONFLICT``.
    :ivar low_io: Default for skip-hashing-unchanged-files mode; overridable per-repo or
        via ``$PROTONFS_LOW_IO``.
    :ivar event_log: Default for enabling structured event logging; overridable per-repo or
        via ``$PROTONFS_EVENT_LOG``.
    :ivar progress_style: Default display style for progress updates (e.g. ``"inline"``,
        ``"lines"``); overridable per-repo or via ``$PROTONFS_PROGRESS_STYLE``.
    :ivar batch_size: Files per ``filesystem upload``/``download`` call; overridable
        per-repo or via ``$PROTONFS_BATCH_SIZE``. Lower it on a slow/throttled link so each
        transfer call stays under ``$PROTONFS_TRANSFER_TIMEOUT`` (a large batch that times
        out is retried whole, so smaller batches also lose less work per throttle-retry).

    .. versionchanged:: 1.3.0
       Added the ``event_log`` and ``progress_style`` defaults.

    .. versionchanged:: 1.6.0
       Added the ``batch_size`` default.
    """

    on_conflict: str = "skip"
    low_io: bool = False
    event_log: bool = False
    progress_style: str = "inline"
    batch_size: int = DEFAULT_BATCH_SIZE


@dataclass
class Config:
    """A fully resolved protonfs configuration for one repo.

    :ivar remote_root: Path (on the Proton Drive remote) this repo syncs against.
    :ivar device_id: Unique identifier for this device/checkout, used to distinguish
        concurrent syncers. Empty in a :class:`Config` written to the SHARED config file
        (``config.json``); populated in the per-device local file or a layered-merge
        result.
    :ivar defaults: The (possibly repo-overridden) :class:`Defaults`.
    """

    remote_root: str
    device_id: str
    defaults: Defaults = field(default_factory=Defaults)

    def to_dict(self) -> dict:
        """Serialize to the shared ``config.json`` layout.

        :returns: A dict with ``remote_root``, and only the ``defaults`` fields that
            differ from :class:`Defaults`' built-ins (so an unset field still lets the
            global config layer's default take effect -- see :func:`load_layered_config`).
            ``device_id`` is included only if non-empty; new setups leave it empty here
            and write the real value to ``config.local.json`` instead (see
            :func:`init_config`).
        """
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
        """Build a :class:`Config` from a raw (possibly layer-merged) dict.

        :param data: Parsed JSON dict; ``device_id`` and ``defaults`` are optional and
            fall back to ``""`` and built-in :class:`Defaults` values respectively.
        :returns: The resulting :class:`Config`.
        :raises KeyError: If ``remote_root`` is missing.
        """
        defaults_data = data.get("defaults", {})
        return cls(
            remote_root=data["remote_root"],
            device_id=data.get("device_id", ""),
            defaults=Defaults(
                on_conflict=defaults_data.get("on_conflict", "skip"),
                low_io=defaults_data.get("low_io", False),
                event_log=defaults_data.get("event_log", False),
                progress_style=defaults_data.get("progress_style", "inline"),
                batch_size=defaults_data.get("batch_size", DEFAULT_BATCH_SIZE),
            ),
        )


def config_dir(repo_root: Path) -> Path:
    """Return the ``.protonfs`` control directory under ``repo_root``.

    :param repo_root: Root of the repo being synced.
    :returns: ``repo_root / ".protonfs"``.
    """
    return repo_root / CONFIG_DIR_NAME


def config_path(repo_root: Path) -> Path:
    """Return the path to the per-repo SHARED config file.

    :param repo_root: Root of the repo being synced.
    :returns: ``.protonfs/config.json`` under ``repo_root``; this file is committed and
        holds ``remote_root`` + ``defaults``. See :func:`local_config_path` for the
        gitignored per-device counterpart.
    """
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
    """Write ``data`` as pretty-printed, sorted-key JSON to ``path`` atomically.

    :param path: Destination file; parent directories are created as needed.
    :param data: JSON-serializable dict to write.

    .. note::
       Same atomic-write pattern as ``IndexStore.save``: write to a temp file in the same
       directory (same filesystem, so ``os.replace`` is a true atomic rename), so a
       reader -- or a crash mid-write -- never sees a torn or truncated config file. The
       temp file is removed on any exception.
    """
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
    """Read and parse ``path`` as a JSON dict, tolerating an absent or empty file.

    :param path: File to read.
    :returns: The parsed dict, or ``{}`` if the file does not exist or is blank.
    """
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
    """Atomically write ``config`` to the per-repo SHARED config file.

    :param repo_root: Root of the repo being synced.
    :param config: Config to persist via :meth:`Config.to_dict`.

    .. seealso:: :func:`config_path`, :func:`_atomic_write_json`
    """
    _atomic_write_json(config_path(repo_root), config.to_dict())


def load_local_config(repo_root: Path) -> dict:
    """Read the per-device local config (`.protonfs/config.local.json`, #21), if present."""
    return _read_json_dict(local_config_path(repo_root))


def save_local_config(repo_root: Path, data: dict) -> None:
    """Atomically write ``data`` to the per-device local config file.

    :param repo_root: Root of the repo being synced.
    :param data: Raw dict to persist (e.g. ``{"device_id": ...}``); not a :class:`Config`.

    .. seealso:: :func:`local_config_path`
    """
    _atomic_write_json(local_config_path(repo_root), data)


def load_global_config() -> dict:
    """Read the global user/machine config (#21), if present."""
    return _read_json_dict(global_config_path())


def save_global_config(data: dict) -> None:
    """Atomically write ``data`` to the global user/machine config file.

    :param data: Raw dict to persist.

    .. seealso:: :func:`global_config_path`
    """
    _atomic_write_json(global_config_path(), data)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto ``base``, ``override`` winning on conflicts.

    :param base: Lower-precedence dict (not mutated).
    :param override: Higher-precedence dict; nested dicts are merged key-by-key rather
        than replacing the whole sub-dict, so e.g. an env-only ``defaults.low_io`` override
        does not erase a repo-configured ``defaults.on_conflict``.
    :returns: A new merged dict.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_bool_env(value: str) -> bool:
    """Parse an environment variable string as a boolean.

    :param value: Raw env var value.
    :returns: ``True`` for (case-insensitive, whitespace-trimmed) ``"1"``, ``"true"``,
        ``"yes"``, or ``"on"``; ``False`` for anything else.
    """
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
        if key in ("low_io", "event_log"):
            defaults_layer[key] = _parse_bool_env(value)
        elif key == "batch_size":
            # A non-positive size makes batches() produce no chunks; clamp to >= 1. A
            # non-integer value is a user error surfaced at config load.
            defaults_layer[key] = max(1, int(value))
        else:
            defaults_layer[key] = value
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
