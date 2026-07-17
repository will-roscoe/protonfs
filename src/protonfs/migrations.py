# src/protonfs/migrations.py
"""Versioned repo-state migrations (#67).

Migrations of on-disk `.protonfs/` state existed already, but scattered and implicit:
index schema upgrades happen silently on `IndexStore` load, `device_id` relocation to
`config.local.json` runs opportunistically inside `protonfs setup`, and control-file
backfill (`.protonfs/ignore`, `include`, `.gitattributes`, `.gitignore`) is also folded
into `setup`. This module makes that set explicit, orderable, and previewable without
replacing any of the existing implicit paths -- a plain `pull` on an old repo must keep
working exactly as before; this registry is an additional, explicit way to bring a repo
fully up to date in one step.

Each `Migration` is idempotent and self-contained: `is_applied` probes the actual
on-disk state (never trusts the layout-version marker alone), so migrations stay correct
on an untouched 0.2.0-era repo, a partially-migrated repo, or an already-current one.

The `layout_version` marker lives in `.protonfs/config.local.json` (per-device, already
the home for local/gitignored state -- see `config.py`) and records the newest layout
this specific on-disk checkout has been fully migrated to. It is written only after every
registered migration reports itself applied; it is never consulted to SKIP a migration's
own `is_applied` check, only used as a fast, informational summary (e.g. for `doctor`/
`status`-style reporting).

Public API for `protonfs upgrade` (#66, a separate task) to call:
    pending_migrations(root) -> list[Migration]
    run_migrations(root, dry_run=False) -> MigrationResult
    layout_version(root) -> int
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from protonfs.commands.setup import (
    _PROTONFS_GITATTRIBUTES,
    _PROTONFS_GITIGNORE,
    write_git_control_files,
)
from protonfs.config import (
    config_path,
    load_local_config,
    migrate_device_id_to_local,
    save_local_config,
)
from protonfs.ignore import ignore_path, include_path, init_ignore, init_include
from protonfs.index import INDEX_FILE_NAME, INDEX_SCHEMA_VERSION, IndexStore

# Key under which the layout-version marker is stored in `.protonfs/config.local.json`.
LAYOUT_VERSION_KEY = "layout_version"


@dataclass(frozen=True)
class Migration:
    """One registered, idempotent repo-state migration.

    `version` is the layout version this migration brings the repo to (migrations are
    registered in ascending order). `is_applied` must be a pure, side-effect-free probe
    of on-disk state; `apply` must be safe to call even when `is_applied` is already
    True (every migration here delegates to code that already guarantees that).
    """

    id: str
    version: int
    description: str
    is_applied: Callable[[Path], bool]
    apply: Callable[[Path], None]


@dataclass
class MigrationResult:
    """Outcome of `run_migrations`."""

    applied: list[str] = field(default_factory=list)
    dry_run: bool = False
    layout_version_before: int = 0
    layout_version_after: int = 0


# --- index schema: persist-at-current-version ------------------------------------------
#
# `IndexStore._load` already migrates an old on-disk index forward IN MEMORY on every
# load; the migration is only ever written back if something else calls `.save()`. A
# repo that's read-only since 0.2.0 (nothing but `pull`s that never dirty the index)
# would therefore never actually rewrite `index.json` on disk. This migration forces
# that resave explicitly.


def _index_is_current(root: Path) -> bool:
    """``is_applied`` probe: whether ``index.json`` is already at the current schema.

    :param root: the protonfs root.
    :returns: ``True`` when there is no index or it is already current.
    """
    index_file = root / ".protonfs" / INDEX_FILE_NAME
    if not index_file.exists():
        return True  # nothing to migrate
    raw = json.loads(index_file.read_text())
    if isinstance(raw.get("schema_version"), int) and isinstance(raw.get("entries"), dict):
        version = raw["schema_version"]
    else:
        version = 0  # legacy v0: bare {rel_path: entry} document
    return version == INDEX_SCHEMA_VERSION


def _index_apply(root: Path) -> None:
    """``apply``: re-save the index so its in-memory forward-migration lands on disk.

    :param root: the protonfs root.
    """
    # Loading migrates in memory (see index.py `_migrate`); saving persists it.
    IndexStore(root).save()


# --- device_id relocation (#21) ----------------------------------------------------------


def _device_id_is_local(root: Path) -> bool:
    """``is_applied`` probe: whether the shared config no longer carries ``device_id``.

    :param root: the protonfs root.
    :returns: ``True`` when there is no shared config or it has no ``device_id``.
    """
    shared_path = config_path(root)
    if not shared_path.exists():
        return True  # not a set-up repo at all -- nothing to relocate
    data = json.loads(shared_path.read_text())
    return "device_id" not in data


def _device_id_apply(root: Path) -> None:
    """``apply``: relocate ``device_id`` from the shared config to ``config.local.json``.

    :param root: the protonfs root.
    """
    migrate_device_id_to_local(root)


# --- control-file backfill (#18/#20) ------------------------------------------------------


def _missing_lines(path: Path, content: str) -> bool:
    """True if any non-blank line of `content` is absent from `path` -- mirrors the
    missing-line check `write_git_control_files`'s `_ensure_lines` uses to decide
    whether it needs to append anything."""
    existing_lines = {line.strip() for line in path.read_text().splitlines()}
    return any(ln.strip() and ln.strip() not in existing_lines for ln in content.splitlines())


def _control_files_current(root: Path) -> bool:
    """``is_applied`` probe: whether all ``.protonfs/`` control files are present + complete.

    Checks ``ignore``/``include`` exist and the control ``.gitattributes``/``.gitignore``
    contain every managed line.

    :param root: the protonfs root.
    :returns: ``True`` when nothing needs backfilling.
    """
    if not ignore_path(root).exists() or not include_path(root).exists():
        return False
    protonfs_dir = root / ".protonfs"
    gitattributes = protonfs_dir / ".gitattributes"
    gitignore = protonfs_dir / ".gitignore"
    if not gitattributes.exists() or _missing_lines(gitattributes, _PROTONFS_GITATTRIBUTES):
        return False
    if not gitignore.exists() or _missing_lines(gitignore, _PROTONFS_GITIGNORE):
        return False
    return True


def _control_files_apply(root: Path) -> None:
    """``apply``: backfill the ``.protonfs/`` ignore/include and control git files.

    :param root: the protonfs root.
    """
    init_ignore(root)
    init_include(root)
    write_git_control_files(root)


# --- refresh-state format ------------------------------------------------------------------
#
# `refresh-state.json` (refreshstate.py) has had exactly one on-disk shape since it was
# introduced (#33 item 2): `{"root": str, "frontier": [[path, prefix], ...]}`. There is no
# legacy format to migrate away from. Registered anyway (always a no-op) so it has a
# documented home here rather than being silently absent from the registry, and so a
# future format change has a slot to land in without restructuring the registry.


def _refresh_state_is_current(root: Path) -> bool:
    """``is_applied`` probe for the refresh-state format: always current (no legacy format).

    :param root: the protonfs root (unused; the format has never changed).
    :returns: always ``True``.
    """
    return True


def _refresh_state_apply(root: Path) -> None:
    """``apply`` for the refresh-state format: a no-op placeholder for future changes.

    :param root: the protonfs root (unused).
    """
    pass  # no legacy format exists; nothing to do


# --- event-log gitignore (#XXX) --------------------------------------------------------


def _event_log_gitignored(root: Path) -> bool:
    """is_applied: whether .protonfs/.gitignore already excludes the event log.

    :param root: the protonfs root.
    :returns: ``False`` if the gitignore file is missing or lacks the event-log lines.
    """
    gitignore = root / ".protonfs" / ".gitignore"
    if not gitignore.exists():
        return False
    return "events.log" in gitignore.read_text()


def _event_log_gitignore_apply(root: Path) -> None:
    """apply: append the event-log gitignore lines (idempotent via write_git_control_files).

    :param root: the protonfs root.
    """
    write_git_control_files(root)


MIGRATIONS: list[Migration] = [
    Migration(
        id="index-schema-current",
        version=1,
        description=f"resave index.json at current schema (v{INDEX_SCHEMA_VERSION}) if stale",
        is_applied=_index_is_current,
        apply=_index_apply,
    ),
    Migration(
        id="device-id-to-local-config",
        version=2,
        description="move device_id from committed config.json to config.local.json (#21)",
        is_applied=_device_id_is_local,
        apply=_device_id_apply,
    ),
    Migration(
        id="control-file-backfill",
        version=3,
        description="backfill .protonfs/ignore, include, .gitattributes, .gitignore (#18/#20)",
        is_applied=_control_files_current,
        apply=_control_files_apply,
    ),
    Migration(
        id="refresh-state-format",
        version=4,
        description="refresh-state.json format (no legacy format; placeholder for future changes)",
        is_applied=_refresh_state_is_current,
        apply=_refresh_state_apply,
    ),
    Migration(
        id="event-log-gitignore",
        version=5,
        description="gitignore .protonfs/events.log(.1) (verbosity/event-log feature)",
        is_applied=_event_log_gitignored,
        apply=_event_log_gitignore_apply,
    ),
]

CURRENT_LAYOUT_VERSION = max(m.version for m in MIGRATIONS)


def layout_version(root: Path) -> int:
    """The layout version this repo's local checkout was last fully migrated to (0 if
    never recorded -- e.g. a repo untouched since before this registry existed)."""
    return int(load_local_config(root).get(LAYOUT_VERSION_KEY, 0))


def pending_migrations(root: Path) -> list[Migration]:
    """Migrations not yet applied to `root`, in registration order.

    Each migration's `is_applied` probes actual on-disk state (not the layout-version
    marker), so this is correct for an untouched repo, a partially-migrated one, or an
    already-current one. Returns `[]` for a directory that was never `protonfs setup` at
    all (no `.protonfs/config.json`) -- there is nothing to migrate.
    """
    if not config_path(root).exists():
        return []
    return [m for m in MIGRATIONS if not m.is_applied(root)]


def run_migrations(root: Path, dry_run: bool = False) -> MigrationResult:
    """Run all pending migrations against `root`, in order.

    `dry_run=True` computes and returns the plan without applying anything or writing
    the layout-version marker. Safe to call repeatedly: migrations already applied are
    skipped, and running twice in a row is a no-op the second time (`applied == []`).
    """
    before = layout_version(root)
    pending = pending_migrations(root)

    if dry_run:
        return MigrationResult(
            applied=[m.id for m in pending],
            dry_run=True,
            layout_version_before=before,
            layout_version_after=before,
        )

    applied: list[str] = []
    for migration in pending:
        migration.apply(root)
        applied.append(migration.id)

    after = before
    if not pending_migrations(root):
        # Every registered migration now checks out as applied -- record the marker.
        # (No-op if `root` was never set up: config_path absent -> pending_migrations
        # returned [] above, so `pending` is empty and we still only touch the marker
        # when root looks like a real, set-up repo.)
        if config_path(root).exists():
            local_data = load_local_config(root)
            local_data[LAYOUT_VERSION_KEY] = CURRENT_LAYOUT_VERSION
            save_local_config(root, local_data)
            after = CURRENT_LAYOUT_VERSION

    return MigrationResult(
        applied=applied,
        dry_run=False,
        layout_version_before=before,
        layout_version_after=after,
    )
