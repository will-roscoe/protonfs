"""Assembles the per-invocation context CLI commands operate against.

Bundles together the resolved repo root, layered :class:`~protonfs.config.Config`, the
local :class:`~protonfs.index.IndexStore`, and a :class:`~protonfs.drive.DriveClient`, so
each CLI command can call :func:`load_context` once and get everything it needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import click

from protonfs.config import Config, load_layered_config
from protonfs.drive import DriveClient
from protonfs.index import IndexStore


@dataclass
class RepoContext:
    """Everything a CLI command needs to operate on a synced repo.

    :ivar root: Resolved (absolute, symlink-free) repo root.
    :ivar config: Fully layered :class:`~protonfs.config.Config` for this repo.
    :ivar index: Local :class:`~protonfs.index.IndexStore` for this repo.
    :ivar drive: Client for talking to the Proton Drive remote.
    """

    root: Path
    config: Config
    index: IndexStore
    drive: DriveClient


def load_context(start: Path | None = None) -> RepoContext:
    """Resolve the repo root from ``start`` and build a :class:`RepoContext` for it.

    :param start: Directory to resolve the repo from; defaults to the current working
        directory.
    :returns: The assembled :class:`RepoContext`.
    :raises click.ClickException: If no layered config resolves for this directory
        (i.e. ``protonfs setup`` has not been run there) -- see
        :func:`~protonfs.config.load_layered_config`.
    """
    root = (start or Path.cwd()).resolve()
    config = load_layered_config(root)
    if config is None:
        raise click.ClickException(
            "protonfs is not set up in this directory. Run `protonfs setup` first."
        )
    return RepoContext(root=root, config=config, index=IndexStore(root), drive=DriveClient())
