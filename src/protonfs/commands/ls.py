# src/protonfs/commands/ls.py
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from protonfs.context import RepoContext
from protonfs.diff import classify
from protonfs.ignore import IgnoreMatcher
from protonfs.localscan import scan


def remote_rel_paths(ctx: RepoContext) -> set[str]:
    entries = ctx.drive.list(ctx.config.remote_root)
    names = set()
    for entry in entries:
        name = entry.get("name", {})
        if name.get("ok"):
            names.add(name["value"])
    return names


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
            name = entry.get("name", {})
            if name.get("ok"):
                table.add_row(name["value"], entry.get("type", ""))
        console.print(table)
        return

    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)
    remote_paths = remote_rel_paths(ctx) if remote else None
    diff_entries = classify(local, ctx.index, remote_paths)

    table = Table("path", "state")
    for entry in diff_entries:
        table.add_row(entry.rel_path, entry.state.value)
    console.print(table)
