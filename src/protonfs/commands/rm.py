from __future__ import annotations

from pathlib import PurePosixPath

import click

from protonfs.context import RepoContext


def rm(ctx: RepoContext, rel_path: str, recursive: bool, force: bool, confirmed: bool) -> None:
    local_target = ctx.root / rel_path
    if local_target.is_dir() and not recursive:
        raise click.ClickException(
            f"'{rel_path}' is a directory; pass -r/--recursive to remove it."
        )

    if not confirmed:
        kind = "permanently delete" if force else "trash"
        click.confirm(f"{kind.capitalize()} '{rel_path}' on Drive?", abort=True)

    entry = ctx.index.get(rel_path)
    remote_path = entry.remote_path if entry else f"{ctx.config.remote_root}/{rel_path}"

    ctx.drive.trash([remote_path])
    if force:
        name = PurePosixPath(remote_path).name
        ctx.drive.delete([f"/trash/{name}"])

    for indexed_rel in list(ctx.index.all()):
        if indexed_rel == rel_path or indexed_rel.startswith(rel_path + "/"):
            ctx.index.remove(indexed_rel)
    ctx.index.save()
