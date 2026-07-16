# src/protonfs/commands/ls.py
"""``protonfs ls``: list tracked files with their sync state, or list Drive's trash."""
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from protonfs.context import RepoContext
from protonfs.diff import classify, within_subpath
from protonfs.drive import RemoteEntry, decrypted_name
from protonfs.ignore import IgnoreMatcher
from protonfs.localscan import scan


def remote_rel_paths(ctx: RepoContext, subpath: str | None = None) -> dict[str, RemoteEntry]:
    """Recursive files-only remote listing, scoped to `subpath` when given. rel_paths
    are re-prefixed with the subpath so they match the index's repo-root-relative keys
    (same convention as `refresh`)."""
    remote_root = ctx.config.remote_root
    if subpath:
        remote_root = f"{remote_root}/{subpath}"
    result = {e.rel_path: e for e in ctx.drive.walk(remote_root) if not e.is_dir}
    if subpath:
        result = {f"{subpath}/{rel}": entry for rel, entry in result.items()}
    return result


def render_ls(
    ctx: RepoContext,
    subpath: str | None,
    remote: bool,
    trash: bool,
    console: Console,
) -> None:
    """Print a table of tracked files with their sync state (or a trash listing).

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree to list, or ``None`` for everything.
    :param remote: when true, cross-reference a recursive remote walk so remote-only
        and remote-changed states are shown (otherwise state is local-vs-index only).
    :param trash: when true, list ``/trash`` entries instead of tracked files;
        ``subpath``/``remote`` are ignored in this mode.
    :param console: the :class:`rich.console.Console` to render the table to.

    .. seealso:: :func:`remote_rel_paths` for the remote-listing helper.
    """
    if trash:
        entries = ctx.drive.list("/trash")
        table = Table("name", "type")
        for entry in entries:
            name_val = decrypted_name(entry)
            if name_val is not None:
                table.add_row(name_val, entry.get("type", ""))
        console.print(table)
        return

    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)
    remote_map = remote_rel_paths(ctx, subpath) if remote else None
    # classify reasons over the whole repo-wide index; when a subpath was given, the
    # local scan and remote walk are scoped to it, so restrict the rows to that
    # subpath too -- otherwise out-of-scope index entries (never scanned/walked) show
    # up, and with a remote view are misread as remote-deleted.
    diff_entries = [
        e for e in classify(local, ctx.index, remote_map) if within_subpath(e.rel_path, subpath)
    ]

    table = Table("path", "state")
    for entry in diff_entries:
        table.add_row(entry.rel_path, entry.state.value)
    console.print(table)
