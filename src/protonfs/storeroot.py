"""Finds the protonfs store a directory belongs to, without importing anything heavy.

:func:`find_store_root` is re-exported from :mod:`protonfs.context`, which is where
callers should import it from; it lives here so the CLI group callback can use it on
every invocation without loading the Drive client.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

from pathlib import Path


def find_store_root(start: Path) -> Path | None:
    """Return the nearest ancestor of ``start`` (inclusive) holding ``.protonfs/config.json``.

    :param start: A path, which need not exist; the search begins at its nearest existing
        ancestor.
    :returns: The resolved store root, or ``None`` if no ancestor is a protonfs store.

    .. versionadded:: 3.4.0
    """
    current = Path(start).resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".protonfs" / "config.json").is_file():
            return candidate
    return None
