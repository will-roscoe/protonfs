from __future__ import annotations

from protonfs.context import RepoContext


def restore(ctx: RepoContext, rel_path: str) -> None:
    entry = ctx.index.get(rel_path)
    remote_path = entry.remote_path if entry else f"{ctx.config.remote_root}/{rel_path}"
    ctx.drive.restore([remote_path])
