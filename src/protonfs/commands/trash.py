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

.. versionadded:: 1.0.0
"""

from __future__ import annotations

from collections import Counter

import click
from rich.console import Console
from rich.table import Table

from protonfs.context import RepoContext
from protonfs.drive import DriveError, decrypted_name

_CONFIRMATION_PHRASE = "empty trash"


PARENT_UNKNOWN = "(unknown)"


def resolve_parents(ctx: RepoContext, entries: list[dict]) -> dict[str, str]:
    """Map each ``parentUid`` in a `/trash` listing to a human-readable original parent.

    proton-drive cannot resolve a bare UID, so this works from what can be known: the
    account root and the repo's ``remote_root`` (one ``filesystem info`` each), parents
    that are themselves trashed (named from the same listing), and a bounded search of
    the ``remote_root`` subtree (:meth:`DriveClient.find_folder_paths`). Whatever stays
    unresolved gets an honest marker rather than a bare ``?``: ``(outside <remote_root>)``
    when the whole subtree was searched, ``(not found within search limit)`` when the
    search was cut short, ``(unknown)`` for an entry with no ``parentUid``.

    :param ctx: the repo context (its ``remote_root`` bounds the search).
    :param entries: raw `/trash` listing entries.
    :returns: ``{parentUid: display text}`` for every distinct ``parentUid``.

    .. versionadded:: 3.4.0
    """
    wanted = {e["parentUid"] for e in entries if e.get("parentUid")}
    resolved: dict[str, str] = {}
    root = ctx.config.remote_root.rstrip("/")
    for path in ("/my-files", root):
        uid = _node_uid_or_none(ctx, path)
        if uid in wanted:
            resolved[uid] = path
    trashed = {e.get("uid"): decrypted_name(e) for e in entries if e.get("type") == "folder"}
    for uid in wanted - set(resolved):
        if uid in trashed:
            shown = trashed[uid]
            resolved[uid] = f"(trashed folder {shown})" if shown else "(trashed folder)"
    todo = wanted - set(resolved)
    complete = True
    if todo:
        found, complete = ctx.drive.find_folder_paths(root, todo)
        resolved.update(found)
    for uid in wanted - set(resolved):
        resolved[uid] = f"(outside {root})" if complete else "(not found within search limit)"
    return resolved


def _node_uid_or_none(ctx: RepoContext, path: str) -> str | None:
    """UID of the node at ``path``, or ``None`` if it can't be read (never raises)."""
    try:
        return ctx.drive.node_uid(path)
    except DriveError:
        return None


def list_trash(ctx: RepoContext, console: Console) -> None:
    """Render every item in /trash: name, type, original parent path and duplicates.

    The parent comes from :func:`resolve_parents` (an honest marker when it can't be
    resolved). The duplicate column counts how many OTHER trashed items share the name,
    which is exactly the ambiguity `restore` warns about (#56): proton-drive would act on
    the first name-match, not necessarily this one.

    .. versionchanged:: 3.4.0
       The original parent is a resolved path (was always ``?``, #231).
    """
    entries = ctx.drive.list_trash()
    names = [decrypted_name(entry) for entry in entries]
    counts = Counter(name for name in names if name is not None)
    parents = resolve_parents(ctx, entries)

    def parent_for(entry: dict) -> str:
        return parents.get(entry.get("parentUid") or "", PARENT_UNKNOWN)

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


def empty_trash(ctx: RepoContext, confirmed: bool, reporter=None) -> None:
    """Permanently empty /trash for the whole account. Irreversible, and NOT scoped
    to this repo's `remote_root` -- it empties every trashed item on the account,
    not just ones protonfs put there. Requires the user to type an exact
    confirmation phrase unless `confirmed` (--yes) is set.

    :param ctx: the loaded repo context.
    :param confirmed: skip the interactive typed confirmation (the ``--yes`` flag).
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    """
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
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

    reporter.phase("emptying trash")
    ctx.drive.empty_trash()
