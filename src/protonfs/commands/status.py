from __future__ import annotations

from collections import Counter
from pathlib import Path

from protonfs.context import RepoContext
from protonfs.diff import classify
from protonfs.ignore import IgnoreMatcher
from protonfs.localscan import scan


def compute_status(ctx: RepoContext, subpath: str | None) -> Counter:
    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)
    entries = classify(local, ctx.index)
    return Counter(entry.state.value for entry in entries)
