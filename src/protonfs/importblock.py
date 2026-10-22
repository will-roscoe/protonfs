"""The ``protonfs import`` block in a store's ``.gitignore`` (#205).

``protonfs import`` stops git tracking a file and adds one literal, anchored line for it
to a marked block in ``<store>/.gitignore``, so git ignores exactly that file and nothing
else. ``push`` reads the block too: a file listed there that is gone locally was removed
by another clone's ``git pull``, not deleted by the user, so push never trashes its Drive
copy.

.. code-block:: text

    # protonfs import (do not edit inside this block; `protonfs export` removes lines from it)
    /run01/frame0001.reduced.fits
    # end protonfs import

The block's location and format are current behaviour, not part of the stability
contract.

.. versionadded:: 3.1.0
"""

from __future__ import annotations

from pathlib import Path

BLOCK_START = (
    "# protonfs import (do not edit inside this block; "
    "`protonfs export` removes lines from it)"
)
BLOCK_END = "# end protonfs import"

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


def _read(store_root: Path) -> list[str] | None:
    # surrogateescape: a .gitignore that is not UTF-8 is read and written back byte for
    # byte instead of raising (push reads the block on every --deletions trash run).
    path = gitignore_path(store_root)
    if not path.exists():
        return None
    return path.read_bytes().decode("utf-8", "surrogateescape").splitlines()


def _newline(store_root: Path) -> str:
    path = gitignore_path(store_root)
    return "\r\n" if path.exists() and b"\r\n" in path.read_bytes() else "\n"


def _bounds(lines: list[str]) -> tuple[int, int] | None:
    try:
        start = lines.index(BLOCK_START)
        end = lines.index(BLOCK_END, start + 1)
    except ValueError:
        return None
    return start, end


def _write(store_root: Path, lines: list[str]) -> None:
    newline = _newline(store_root)  # keep a CRLF file CRLF, so user lines stay unchanged
    text = newline.join(lines) + (newline if lines else "")
    gitignore_path(store_root).write_bytes(text.encode("utf-8", "surrogateescape"))


def block_paths(store_root: Path) -> set[str]:
    """Return the store-relative paths listed in the import block.

    :param store_root: the store root.
    :returns: the unescaped paths; empty when there is no ``.gitignore`` or no block.

    .. versionadded:: 3.1.0
    """
    lines = _read(store_root)
    bounds = _bounds(lines) if lines is not None else None
    if bounds is None:
        return set()
    start, end = bounds
    return {_unescape(line) for line in lines[start + 1:end] if line.strip()}


def add_paths(store_root: Path, rels: list[str]) -> list[str]:
    """Add a line for each of ``rels`` to the import block, creating the block (at the end
    of the file) and the file when absent. Lines already in the block are not repeated;
    lines outside it are left exactly as they are.

    :param store_root: the store root.
    :param rels: store-relative file paths.
    :returns: the lines actually added, in order.

    .. versionadded:: 3.1.0
    """
    lines = _read(store_root) or []
    bounds = _bounds(lines)
    if bounds is None:
        lines += [BLOCK_START, BLOCK_END]
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
    _write(store_root, lines)
    return added


def remove_paths(store_root: Path, rels: list[str]) -> list[str]:
    """Remove the lines for ``rels`` from the import block; a block left empty is removed
    whole. Nothing outside the block is touched.

    :param store_root: the store root.
    :param rels: store-relative file paths.
    :returns: the lines actually removed.

    .. versionadded:: 3.1.0
    """
    lines = _read(store_root)
    bounds = _bounds(lines) if lines is not None else None
    if bounds is None:
        return []
    start, end = bounds
    drop = {escape_line(rel) for rel in rels}
    inside = lines[start + 1:end]
    removed = [line for line in inside if line in drop]
    kept = [line for line in inside if line not in drop]
    lines[start:end + 1] = [BLOCK_START, *kept, BLOCK_END] if kept else []
    _write(store_root, lines)
    return removed
