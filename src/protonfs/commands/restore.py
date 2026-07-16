"""``protonfs restore``: bring a trashed Drive file back to its original location."""
from __future__ import annotations

from protonfs.context import RepoContext


def restore(ctx: RepoContext, rel_path: str) -> None:
    """Restore a trashed file or directory on Drive back to its original location.

    Resolves the remote path from the index entry when present, else derives it from
    the configured ``remote_root``, and asks proton-drive to restore it from trash.

    :param ctx: the loaded repo context.
    :param rel_path: repo-root-relative path of the item to restore.
    :raises protonfs.drive.DriveError: on a Drive failure, including the same-named
        trash-entry ambiguity that proton-drive >= 0.5.0 cannot disambiguate (#56).

    .. seealso:: :meth:`protonfs.drive.DriveClient.restore` for the trash-resolution
        semantics and the ambiguity guard.
    """
    entry = ctx.index.get(rel_path)
    remote_path = entry.remote_path if entry else f"{ctx.config.remote_root}/{rel_path}"
    ctx.drive.restore([remote_path])
