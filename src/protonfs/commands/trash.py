# src/protonfs/commands/trash.py
"""Inspect and empty Proton Drive's trash (#70, M6.2).

`rm` puts things INTO trash and `restore` pulls them back out, but neither lets a
user see what is *in* trash or empty it -- and proton-drive >= 0.5.0 resolves
`/trash` paths by name with first-match-wins (#56), so stale same-named entries can
silently make `restore` refuse to act. `trash list` surfaces that ambiguity
directly (same-name duplicate counts); `trash empty` wraps the account-global,
irreversible `filesystem empty-trash` behind an explicit typed confirmation.

Deliberately out of scope: selective/per-item permanent deletion from trash by UID
-- proton-drive does not accept UIDs for `/trash` paths (see #56's analysis and the
duplicate-basename guard in `commands/rm.py`), so there is no safe way to target a
single item there yet.
"""

from __future__ import annotations

from collections import Counter

import click
from rich.console import Console
from rich.table import Table

from protonfs.context import RepoContext
from protonfs.drive import decrypted_name

_CONFIRMATION_PHRASE = "empty trash"


def list_trash(ctx: RepoContext, console: Console) -> None:
    """Render every item in /trash: name, original parent (best-effort -- "?" when
    proton-drive can't resolve it), and how many OTHER trashed items share the same
    name. A nonzero duplicate count is exactly the ambiguity `restore` warns about
    (#56): proton-drive would act on the first name-match, not necessarily this one.
    """
    entries = ctx.drive.list("/trash")
    names = [decrypted_name(entry) for entry in entries]
    counts = Counter(name for name in names if name is not None)

    parent_cache: dict[str, str | None] = {}

    def parent_for(entry: dict) -> str:
        parent_uid = entry.get("parentUid")
        if not parent_uid:
            return "?"
        if parent_uid not in parent_cache:
            parent_cache[parent_uid] = ctx.drive.parent_name(parent_uid)
        return parent_cache[parent_uid] or "?"

    table = Table("name", "type", "original parent", "duplicates")
    for entry, name in zip(entries, names):
        if name is None:
            continue
        dup_others = counts[name] - 1
        table.add_row(
            name,
            entry.get("type", ""),
            parent_for(entry),
            str(dup_others) if dup_others > 0 else "",
        )
    console.print(table)


def empty_trash(ctx: RepoContext, confirmed: bool) -> None:
    """Permanently empty /trash for the whole account. Irreversible, and NOT scoped
    to this repo's `remote_root` -- it empties every trashed item on the account,
    not just ones protonfs put there. Requires the user to type an exact
    confirmation phrase unless `confirmed` (--yes) is set.
    """
    if not confirmed:
        click.echo(
            "This will PERMANENTLY delete every item currently in this Proton Drive "
            "account's trash -- not just items under this repo's remote root, and not "
            "just items protonfs trashed. This cannot be undone."
        )
        typed = click.prompt(
            f"Type '{_CONFIRMATION_PHRASE}' to confirm", default="", show_default=False
        )
        if typed.strip().lower() != _CONFIRMATION_PHRASE:
            raise click.ClickException("confirmation text did not match; trash was not emptied.")

    ctx.drive.empty_trash()
