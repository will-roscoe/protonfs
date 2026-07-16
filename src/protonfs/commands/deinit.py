# src/protonfs/commands/deinit.py
"""`protonfs deinit`: the inverse of `setup` (#71).

Removes every file `setup` writes under `.protonfs/` -- the shared config, the
per-device local config, the index, the resumable-refresh state, ignore/include, and
the control `.gitattributes`/`.gitignore` -- after a summary + confirmation. Never
touches synced payload files, local or remote: deinit only ever looks inside
`.protonfs/`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import click

from protonfs.config import CONFIG_FILE_NAME, LOCAL_CONFIG_FILE_NAME, config_dir
from protonfs.ignore import IGNORE_FILE_NAME, INCLUDE_FILE_NAME
from protonfs.index import INDEX_FILE_NAME
from protonfs.refreshstate import REFRESH_STATE_FILE

# Committed (tracked) contract files -- exactly what `write_git_control_files` +
# `init_config`'s shared half + `init_ignore`/`init_include` write. Mirrors setup.py.
TRACKED_CONTROL_FILES = (
    CONFIG_FILE_NAME,
    IGNORE_FILE_NAME,
    INCLUDE_FILE_NAME,
    ".gitattributes",
    ".gitignore",
)
# Per-device/transient state -- gitignored by `write_git_control_files`'s own
# `.protonfs/.gitignore` template, never committed.
LOCAL_ONLY_FILES = (
    LOCAL_CONFIG_FILE_NAME,
    INDEX_FILE_NAME,
    REFRESH_STATE_FILE,
)
# Deliberately excludes `.protonfs/lock`: it is not something `setup` writes, and
# `deinit` itself holds it open for the duration of the teardown (see cli.py), so it
# cannot be part of the removal list without racing its own lock.
ALL_MANAGED_FILES = TRACKED_CONTROL_FILES + LOCAL_ONLY_FILES


@dataclass
class DeinitResult:
    removed: list[Path] = field(default_factory=list)
    dir_removed: bool = False
    in_git_repo: bool = False
    tracked_removed: list[str] = field(default_factory=list)


def is_deinit_target(root: Path) -> bool:
    """True when `root` looks like a protonfs root (has a shared config.json)."""
    return (config_dir(root) / CONFIG_FILE_NAME).exists()


def ensure_deinit_target(root: Path) -> None:
    if not is_deinit_target(root):
        raise click.ClickException(
            f"protonfs is not set up in {root} (no .protonfs/config.json found) -- "
            "nothing to deinit."
        )


def _is_inside_git_repo(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def plan_deinit(root: Path) -> list[Path]:
    """Every `.protonfs/` file `deinit` would remove, in the order it would remove them."""
    protonfs_dir = config_dir(root)
    return [protonfs_dir / name for name in ALL_MANAGED_FILES if (protonfs_dir / name).exists()]


def run_deinit(root: Path, dry_run: bool = False, yes: bool = False) -> DeinitResult:
    ensure_deinit_target(root)
    protonfs_dir = config_dir(root)
    to_remove = plan_deinit(root)
    result = DeinitResult(in_git_repo=_is_inside_git_repo(root))

    click.echo(f"This will remove {len(to_remove)} file(s) under {protonfs_dir}:")
    for path in to_remove:
        click.echo(f"  {'[dry-run] would remove' if dry_run else 'remove'} {path}")
    click.echo(
        "Synced payload files (local and remote) are never touched by deinit -- only "
        "protonfs's own bookkeeping under .protonfs/ is removed."
    )

    if dry_run:
        return result

    if not yes:
        click.confirm(f"Remove {len(to_remove)} file(s) under {protonfs_dir}?", abort=True)

    for path in to_remove:
        path.unlink()
        result.removed.append(path)
        if path.name in TRACKED_CONTROL_FILES:
            result.tracked_removed.append(path.name)

    try:
        protonfs_dir.rmdir()
        result.dir_removed = True
    except OSError:
        pass  # leftovers remain (e.g. our own still-open lock file) -- nothing to do

    if result.in_git_repo and result.tracked_removed:
        tracked = ", ".join(sorted(result.tracked_removed))
        click.echo(
            "\nThis directory is inside a git repo. The following protonfs control "
            f"file(s) were tracked and are now deleted: {tracked}."
        )
        click.echo(
            "deinit never runs git commands on your behalf -- stage the deletion yourself, "
            "e.g.:\n  git add -A .protonfs\n  git commit -m 'chore: remove protonfs'"
        )

    click.echo("protonfs deinit complete.")
    return result
