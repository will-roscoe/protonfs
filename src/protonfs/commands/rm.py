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
        # D2.2: permanent delete works only against /trash/<basename>, and with
        # duplicate basenames the CLI deletes one arbitrarily. So only delete when
        # exactly one trashed item carries this basename; otherwise leave it trashed
        # (still reversible) and tell the user to resolve it manually.
        name = PurePosixPath(remote_path).name
        matches = [
            entry
            for entry in ctx.drive.list("/trash")
            if entry.get("name", {}).get("ok") and entry["name"]["value"] == name
        ]
        if len(matches) == 1:
            ctx.drive.delete([f"/trash/{name}"])
        elif len(matches) > 1:
            click.echo(
                f"{len(matches)} items named '{name}' are in trash; protonfs can't "
                f"safely pick yours for permanent deletion. Resolve it via the Proton "
                f"Drive app/web, or leave it trashed (it is already reversible)."
            )
        else:
            click.echo(
                f"'{name}' was trashed but could not be found in trash for permanent "
                f"deletion (it may still be processing); it remains trashed and reversible."
            )

    for indexed_rel in list(ctx.index.all()):
        if indexed_rel == rel_path or indexed_rel.startswith(rel_path + "/"):
            ctx.index.remove(indexed_rel)
    ctx.index.save()
