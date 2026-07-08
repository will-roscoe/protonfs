from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import click

from protonfs.config import Config, load_config
from protonfs.drive import DriveClient
from protonfs.index import IndexStore


@dataclass
class RepoContext:
    root: Path
    config: Config
    index: IndexStore
    drive: DriveClient


def load_context(start: Path | None = None) -> RepoContext:
    root = (start or Path.cwd()).resolve()
    config = load_config(root)
    if config is None:
        raise click.ClickException(
            "protonfs is not set up in this directory. Run `protonfs setup` first."
        )
    return RepoContext(root=root, config=config, index=IndexStore(root), drive=DriveClient())
