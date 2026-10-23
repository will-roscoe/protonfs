"""The marked blocks ``protonfs import`` and ``protonfs export`` keep (#205).

``protonfs import`` stops git tracking a file and adds one literal, anchored line for it
to a marked block in ``<store>/.gitignore``, so git ignores exactly that file and nothing
else. ``push`` reads the block too: a file listed there that is gone locally was removed
by another clone's ``git pull``, not deleted by the user, so push never trashes its Drive
copy. ``protonfs export`` removes a file's line from that block, and adds one to a second
block in ``.protonfs/ignore`` so every machine stops syncing it.

.. code-block:: text

    # protonfs import (do not edit inside this block; `protonfs export` removes lines from it)
    /run01/frame0001.reduced.fits
    # end protonfs import

Both blocks use :func:`escape_line`, which git and :class:`~protonfs.ignore.IgnoreMatcher`
read alike. Their locations and format are current behaviour, not part of the stability
contract.

.. versionadded:: 3.1.0
"""

from __future__ import annotations

from pathlib import Path

from protonfs.ignore import ignore_path

BLOCK_START = (
    "# protonfs import (do not edit inside this block; "
    "`protonfs export` removes lines from it)"
)
BLOCK_END = "# end protonfs import"
EXPORT_BLOCK_START = (
    "# protonfs export (do not edit inside this block; "
    "`protonfs import` refuses these paths)"
)
EXPORT_BLOCK_END = "# end protonfs export"

_SPECIAL = set("*?[\\")


def gitignore_path(store_root: Path) -> Path:
    """Return the store root's own ``.gitignore``, where the block lives.

    .. versionadded:: 3.1.0
    """
    return store_root / ".gitignore"


def escape_line(rel: str) -> str:
    """Return the gitignore line that matches exactly the file ``rel`` and nothing else.

    The line is anchored to the store root (leading ``/``); ``*``, ``?``, ``[`` and ``\\``
    are backslash-escaped, as are a leading ``#`` or ``!`` and trailing spaces.

    :param rel: a store-relative POSIX file path.
    :returns: the escaped, anchored line.

    .. versionadded:: 3.1.0
    """
    out = "".join("\\" + c if c in _SPECIAL else c for c in rel)
    stripped = out.rstrip(" ")
    out = stripped + "\\ " * (len(out) - len(stripped))
    if out[:1] in ("#", "!"):
        out = "\\" + out
    return "/" + out


def _unescape(line: str) -> str:
    body = line[1:] if line.startswith("/") else line
    out, i = [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def _read(path: Path) -> list[str] | None:
    # surrogateescape: a file that is not UTF-8 is read and written back byte for byte
    # instead of raising (push reads the import block on every --deletions trash run).
    if not path.exists():
        return None
    return path.read_bytes().decode("utf-8", "surrogateescape").splitlines()


def _newline(path: Path) -> str:
    return "\r\n" if path.exists() and b"\r\n" in path.read_bytes() else "\n"


def _bounds(lines: list[str], start_marker: str, end_marker: str) -> tuple[int, int] | None:
    try:
        start = lines.index(start_marker)
        end = lines.index(end_marker, start + 1)
    except ValueError:
        return None
    return start, end


def _write(path: Path, lines: list[str]) -> None:
    newline = _newline(path)  # keep a CRLF file CRLF, so user lines stay unchanged
    text = newline.join(lines) + (newline if lines else "")
    path.write_bytes(text.encode("utf-8", "surrogateescape"))


def _paths(path: Path, markers: tuple[str, str]) -> set[str]:
    lines = _read(path)
    bounds = _bounds(lines, *markers) if lines is not None else None
    if bounds is None:
        return set()
    start, end = bounds
    return {_unescape(line) for line in lines[start + 1:end] if line.strip()}


def _add(path: Path, markers: tuple[str, str], rels: list[str]) -> list[str]:
    lines = _read(path) or []
    bounds = _bounds(lines, *markers)
    if bounds is None:
        lines += list(markers)
        bounds = (len(lines) - 2, len(lines) - 1)
    start, end = bounds
    present = set(lines[start + 1:end])
    added = []
    for rel in rels:
        line = escape_line(rel)
        if line not in present:
            present.add(line)
            added.append(line)
    lines[end:end] = added
    _write(path, lines)
    return added


def _remove(path: Path, markers: tuple[str, str], rels: list[str]) -> list[str]:
    lines = _read(path)
    bounds = _bounds(lines, *markers) if lines is not None else None
    if bounds is None:
        return []
    start, end = bounds
    drop = {escape_line(rel) for rel in rels}
    inside = lines[start + 1:end]
    removed = [line for line in inside if line in drop]
    kept = [line for line in inside if line not in drop]
    lines[start:end + 1] = [markers[0], *kept, markers[1]] if kept else []
    _write(path, lines)
    return removed


_IMPORT = (BLOCK_START, BLOCK_END)
_EXPORT = (EXPORT_BLOCK_START, EXPORT_BLOCK_END)


def block_paths(store_root: Path) -> set[str]:
    """Return the store-relative paths listed in the import block.

    :param store_root: the store root.
    :returns: the unescaped paths; empty when there is no ``.gitignore`` or no block.

    .. versionadded:: 3.1.0
    """
    return _paths(gitignore_path(store_root), _IMPORT)


def add_paths(store_root: Path, rels: list[str]) -> list[str]:
    """Add a line for each of ``rels`` to the import block, creating the block (at the end
    of the file) and the file when absent. Lines already in the block are not repeated;
    lines outside it are left exactly as they are.

    :param store_root: the store root.
    :param rels: store-relative file paths.
    :returns: the lines actually added, in order.

    .. versionadded:: 3.1.0
    """
    return _add(gitignore_path(store_root), _IMPORT, rels)


def remove_paths(store_root: Path, rels: list[str]) -> list[str]:
    """Remove the lines for ``rels`` from the import block; a block left empty is removed
    whole. Nothing outside the block is touched.

    :param store_root: the store root.
    :param rels: store-relative file paths.
    :returns: the lines actually removed.

    .. versionadded:: 3.1.0
    """
    return _remove(gitignore_path(store_root), _IMPORT, rels)


def export_block_paths(store_root: Path) -> set[str]:
    """Return the store-relative paths listed in the export block of ``.protonfs/ignore``.

    :param store_root: the store root.
    :returns: the unescaped paths; empty when there is no ignore file or no block.

    .. versionadded:: 3.1.0
    """
    return _paths(ignore_path(store_root), _EXPORT)


def add_export_paths(store_root: Path, rels: list[str]) -> list[str]:
    """Add a line for each of ``rels`` to the export block of ``.protonfs/ignore``, so
    every machine stops syncing exactly those files; as :func:`add_paths` otherwise.

    :param store_root: the store root.
    :param rels: store-relative file paths.
    :returns: the lines actually added, in order.

    .. versionadded:: 3.1.0
    """
    return _add(ignore_path(store_root), _EXPORT, rels)


def remove_export_paths(store_root: Path, rels: list[str]) -> list[str]:
    """Remove the lines for ``rels`` from the export block; as :func:`remove_paths`.

    :param store_root: the store root.
    :param rels: store-relative file paths.
    :returns: the lines actually removed.

    .. versionadded:: 3.1.0
    """
    return _remove(ignore_path(store_root), _EXPORT, rels)
