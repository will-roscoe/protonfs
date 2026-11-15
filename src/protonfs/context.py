"""Assembles the per-invocation context CLI commands operate against.

Bundles together the resolved repo root, layered :class:`~protonfs.config.Config`, the
local :class:`~protonfs.index.IndexStore`, and a :class:`~protonfs.drive.DriveClient`, so
each CLI command can call :func:`load_context` once and get everything it needs.

.. versionadded:: 1.0.0
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

from protonfs.config import Config, MissingDeviceIdError, load_layered_config
from protonfs.drive import DriveClient
from protonfs.index import IndexStore
from protonfs.reporting import get_reporter
from protonfs.storeroot import find_store_root

if TYPE_CHECKING:
    from protonfs.hashcache import HashCache


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

    @functools.cached_property
    def hash_cache(self) -> HashCache:
        """This repo's :class:`~protonfs.hashcache.HashCache`, read from disk on first use
        and shared by every pass of the command, so a command run once per PATH does not
        re-read a cache holding every file in the store.

        .. versionadded:: 3.1.0
        """
        from protonfs.hashcache import HashCache

        return HashCache(self.root)

    def invocation_dir(self) -> Path:
        """The directory PATH arguments are relative to: the current directory when it is
        inside the store, else the store root (only a context built for another
        directory, as in tests, has a current directory outside its store).

        .. versionadded:: 3.4.0
        """
        cwd = Path.cwd().resolve()
        return cwd if cwd == self.root or self.root in cwd.parents else self.root

    def store_path(self, arg: str, *, glob: bool = True) -> str:
        """Map a PATH argument, typed relative to the current directory (or absolute),
        to a store-relative POSIX path (``"."`` for the store root itself).

        A glob pattern is mapped the same way: when it is relative and has no ``..``,
        the directory prefix has its own glob characters escaped, so only the part the
        user typed acts as a pattern.

        :param arg: the PATH as typed.
        :param glob: read ``*``, ``?`` and ``[`` in ``arg`` as a pattern; false for commands
            that take literal paths.
        :returns: the path relative to :attr:`root`.
        :raises ValueError: if ``arg`` resolves outside the store.

        .. versionadded:: 3.4.0
        """
        base = self.invocation_dir()
        if (
            glob
            and not os.path.isabs(arg)
            and any(c in arg for c in "*?[")
            and ".." not in arg.split("/")
        ):
            prefix = base.relative_to(self.root).as_posix()
            pattern = os.path.normpath(arg).replace(os.sep, "/")
            if prefix == ".":
                return pattern
            return "/".join(_escape_glob(part) for part in prefix.split("/")) + "/" + pattern
        full = Path(os.path.abspath(base / arg))
        for candidate in (full, full.parent.resolve() / full.name):
            try:
                return candidate.relative_to(self.root).as_posix()
            except ValueError:
                continue
        raise ValueError(arg)

    def shown(self, rel: str | None) -> str:
        """A store-relative path as the user would type it from the current directory
        (``"."`` for the store root); identical to ``rel`` when run from the store root.

        .. versionadded:: 3.4.0
        """
        target = self.root if rel in (None, ".", "") else self.root / rel
        return Path(os.path.relpath(target, self.invocation_dir())).as_posix()

    def typed_pattern(self, mapped: str) -> str:
        """Undo :meth:`store_path` for a glob pattern, for messages naming it as typed."""
        prefix = self.invocation_dir().relative_to(self.root).as_posix()
        if prefix == ".":
            return mapped
        head = "/".join(_escape_glob(part) for part in prefix.split("/")) + "/"
        return mapped[len(head):] if mapped.startswith(head) else mapped


def _escape_glob(text: str) -> str:
    """Make ``*``, ``?`` and ``[`` in ``text`` match themselves in a glob pattern."""
    return "".join(f"[{c}]" if c in "*?[" else c for c in text)


def load_context(start: Path | None = None) -> RepoContext:
    """Find the store containing ``start`` and build a :class:`RepoContext` for it.

    The store is the nearest ancestor of ``start`` (inclusive) holding
    ``.protonfs/config.json``, as git finds a repository; nested stores resolve to the
    innermost (:func:`find_store_root`).

    :param start: Directory to search upward from; defaults to the current working
        directory.
    :returns: The assembled :class:`RepoContext`.
    :raises click.ClickException: If no layered config resolves for this directory
        (i.e. ``protonfs setup`` has not been run there, or has not been run on this
        machine yet) -- see :func:`~protonfs.config.load_layered_config`.

    .. versionchanged:: 3.4.0
       Searches ``start``'s ancestors for the store instead of requiring ``start`` to be
       its root (#232).

    .. versionchanged:: 1.12.1
       A clone whose ``device_id`` has not been minted on this machine reports that,
       instead of raising :exc:`ValueError` as an uncaught traceback (#151).
    """
    here = (start or Path.cwd()).resolve()
    root = find_store_root(here) or here
    try:
        config = load_layered_config(root)
    except MissingDeviceIdError as exc:
        # #151: a clone of a repo that IS set up, just not here. Reported apart from the
        # "no protonfs in this directory" case because the remedy differs -- `setup` mints
        # a device id without prompting -- and because this used to be a raw traceback.
        raise click.ClickException(
            "This repo is set up, but not on this machine yet: no device_id resolved. "
            "It lives in .protonfs/config.local.json, which is gitignored and so never "
            "arrives with a clone. Run `protonfs setup` here to generate one -- it will "
            "not prompt, and will not change the shared config."
        ) from exc
    if config is None:
        raise click.ClickException(
            "protonfs is not set up in this directory or any parent directory. "
            "Run `protonfs setup` first."
        )
    try:
        drive = DriveClient(clock_skew=config.defaults.clock_skew)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx = RepoContext(root=root, config=config, index=IndexStore(root), drive=drive)
    get_reporter().path_view = ctx.shown  # narration names paths as typed from here
    return ctx


__all__ = ["RepoContext", "find_store_root", "load_context"]
