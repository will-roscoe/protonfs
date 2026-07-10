# src/protonfs/commands/ls.py
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from protonfs.context import RepoContext
from protonfs.diff import classify
from protonfs.drive import decrypted_name
from protonfs.ignore import IgnoreMatcher
from protonfs.localscan import scan


def remote_rel_paths(ctx: RepoContext, subpath: str | None = None) -> dict[str, int]:
    """Recursive files-only remote listing, scoped to `subpath` when given. rel_paths
    are re-prefixed with the subpath so they match the index's repo-root-relative keys
    (same convention as `refresh`)."""
    remote_root = ctx.config.remote_root
    if subpath:
        remote_root = f"{remote_root}/{subpath}"
    result = {e.rel_path: e.size for e in ctx.drive.walk(remote_root) if not e.is_dir}
    if subpath:
        result = {f"{subpath}/{rel}": size for rel, size in result.items()}
    return result


def render_ls(
    ctx: RepoContext,
    subpath: str | None,
    remote: bool,
    trash: bool,
    console: Console,
) -> None:
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
    diff_entries = classify(local, ctx.index, remote_map)

    table = Table("path", "state")
    for entry in diff_entries:
        table.add_row(entry.rel_path, entry.state.value)
    console.print(table)
