"""Helpers for grouping and chunking file lists ahead of bulk remote operations.

Used to shape work before sending it to the Proton Drive API: :func:`group_by_parent`
clusters relative paths by their containing directory (so per-directory remote listings
can be reused across siblings), and :func:`batches` chunks any list into fixed-size
pages to keep individual API calls bounded.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_BATCH_SIZE = 200


def group_by_parent(rel_paths: list[str]) -> dict[str, list[str]]:
    """Group relative paths by their parent directory.

    :param rel_paths: Repo-relative file paths.
    :returns: Dict mapping each parent directory (as ``str(Path(rel).parent)``, ``"."``
        for repo-root files) to the list of paths under it, in input order.
    """
    groups: dict[str, list[str]] = {}
    for rel in rel_paths:
        parent = str(Path(rel).parent)
        groups.setdefault(parent, []).append(rel)
    return groups


def batches(items: list, size: int = DEFAULT_BATCH_SIZE) -> list[list]:
    """Split ``items`` into consecutive chunks of at most ``size`` elements.

    :param items: List to chunk; order is preserved.
    :param size: Maximum chunk size, defaulting to :data:`DEFAULT_BATCH_SIZE`.
    :returns: A list of chunks; the last chunk may be smaller than ``size``. Empty for
        an empty ``items``.
    """
    return [items[i : i + size] for i in range(0, len(items), size)]
