"""Remove a protonfs-tracked path from Proton Drive.

``rm`` trashes the remote copy (reversible). ``rm -f`` additionally attempts a
*permanent* delete of the trashed node.

Permanent limitation — duplicate basenames
------------------------------------------
proton-drive's ``filesystem delete`` addresses a trashed node by its **path
under /trash**, i.e. ``/trash/<basename>``. There is no working way to address a
specific trashed node by its stable UID: as of 2026-07-16, both ``/trash/<uid>``
and a bare ``<uid>`` are rejected (``Trashed node not found`` / path-not-found),
even though ``filesystem delete --help`` advertises UID addressing for
name-conflicting nodes elsewhere. ``test_live_uid_addressed_permanent_delete_
still_unsupported`` probes this and fails loudly if a future proton-drive starts
accepting it — at which point this guard can be lifted.

Consequence: when two or more trashed items share a basename, protonfs cannot
tell which one is the user's, so ``rm -f`` refuses to permanently delete and
leaves the item **trashed (still reversible)**. The instructive fallback is to
empty that item from trash via the Proton Drive app/web, or simply leave it
trashed — trash is reversible, so nothing is lost.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import click

from protonfs.context import RepoContext
from protonfs.drive import decrypted_name


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
        matches = [entry for entry in ctx.drive.list("/trash") if decrypted_name(entry) == name]
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
