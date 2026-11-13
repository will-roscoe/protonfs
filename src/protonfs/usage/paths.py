"""Drive path helpers and ``du`` target resolution.

Drive paths are ``/``-separated; a literal ``/`` inside a node name is written ``\\/`` and a
literal backslash ``\\\\``. :func:`resolve_target` maps a ``du`` argument (a Drive path or a
local path inside a protonfs store) to a Drive path.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from protonfs.config import load_config
from protonfs.context import find_store_root

#: Top-level Drive sections the CLI knows.
SECTIONS = frozenset(
    {
        "my-files",
        "devices",
        "shared-by-me",
        "shared-with-me",
        "trash",
        "albums",
        "photos",
        "photos-shared-by-me",
        "photos-shared-with-me",
        "photos-trash",
    }
)

#: Sections ``du`` can analyse.
DU_SECTIONS = frozenset({"my-files", "devices"})


class TargetError(ValueError):
    """A ``du`` target could not be resolved to a supported Drive path.

    .. versionadded:: 3.4.0
    """


@dataclass(frozen=True)
class Target:
    """A resolved ``du`` target.

    :ivar remote: Normalised Drive path.
    :ivar store_root: The local store root it was derived from, or ``None`` for a Drive
        path given directly.

    .. versionadded:: 3.4.0
    """

    remote: str
    store_root: Path | None


def is_remote(arg: str) -> bool:
    """Return whether ``arg`` is a Drive path, i.e. starts with ``/`` and, if not ``/``
    itself, its first segment is a known Drive section.

    .. versionadded:: 3.4.0
    """
    if arg == "/":
        return True
    if not arg.startswith("/"):
        return False
    return split(arg)[0] in SECTIONS


def escape_name(name: str) -> str:
    """Return ``name`` escaped for use as one Drive path segment.

    .. versionadded:: 3.4.0
    """
    return name.replace("\\", "\\\\").replace("/", "\\/")


def join(parent: str, segment: str) -> str:
    """Return the Drive path of the child ``segment`` (an unescaped name) under ``parent``.

    .. versionadded:: 3.4.0
    """
    esc = escape_name(segment)
    return "/" + esc if parent == "/" else parent + "/" + esc


def node_segment(name: str, uid: str | None, decrypted: bool) -> str:
    """Return the plain (unescaped) path segment that addresses a listed node.

    That is ``name``, or the node ``uid`` when the name could not be decrypted (``name``
    itself if the uid is unknown).

    :param name: the listed name.
    :param uid: the node uid, if known.
    :param decrypted: whether ``name`` is the decrypted name.
    :returns: the segment, ready for :func:`join`.

    .. versionadded:: 3.4.0
    """
    return name if decrypted else (uid or name)


def child_path(parent: str, name: str, uid: str | None, decrypted: bool) -> str:
    """Return the escaped Drive path of a listed child of ``parent``.

    Same as ``join(parent, node_segment(name, uid, decrypted))``.

    :param parent: escaped Drive path of the folder that listed the child.
    :param name: the child's listed name (plain).
    :param uid: the child's uid, if known.
    :param decrypted: whether ``name`` is the decrypted name.
    :returns: the child's escaped Drive path.

    .. versionadded:: 3.4.0
    """
    return join(parent, node_segment(name, uid, decrypted))


def split(path: str) -> list[str]:
    """Return the unescaped, non-empty segments of a Drive path (``[]`` for ``/``).

    .. versionadded:: 3.4.0
    """
    segments: list[str] = []
    cur: list[str] = []
    i, n = 0, len(path)
    while i < n:
        ch = path[i]
        if ch == "\\" and i + 1 < n:
            cur.append(path[i + 1])
            i += 2
            continue
        if ch == "/":
            if cur:
                segments.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        segments.append("".join(cur))
    return segments


def parent_of(path: str) -> str | None:
    """Return the Drive path of the parent of ``path``, or ``None`` for ``/``.

    .. versionadded:: 3.4.0
    """
    segs = split(path)
    if not segs:
        return None
    out = "/"
    for seg in segs[:-1]:
        out = join(out, seg)
    return out


def relative(path: str, base: str) -> str:
    """Return ``path`` relative to ``base`` with unescaped segments joined by ``/``.

    :param path: A Drive path at or below ``base``.
    :param base: A Drive path.
    :returns: ``""`` when ``path == base``.
    :raises ValueError: If ``path`` is not within ``base``.

    .. versionadded:: 3.4.0
    """
    if not is_within(path, base):
        raise ValueError(f"{path!r} is not within {base!r}")
    return "/".join(split(path)[len(split(base)) :])


def is_within(path: str, base: str) -> bool:
    """Return whether ``path`` equals ``base`` or lies below it (whole segments only).

    .. versionadded:: 3.4.0
    """
    p, b = split(path), split(base)
    return p[: len(b)] == b


def _normalise_remote(arg: str) -> str:
    out = "/"
    for seg in split(arg):
        out = join(out, seg)
    return out


def resolve_target(arg: str | None, cwd: Path) -> Target:
    """Resolve a ``du`` argument to a :class:`Target`.

    :param arg: A Drive path, a local path (absolute or relative to ``cwd``), or ``None``
        for ``cwd`` itself.
    :param cwd: The working directory.
    :returns: The target; ``store_root`` is set only for local arguments. The local path
        need not exist.
    :raises TargetError: If a Drive path is outside ``/``, ``/my-files`` and ``/devices``,
        or a local path is not inside a protonfs store.

    .. versionadded:: 3.4.0
    """
    if arg is not None and is_remote(arg):
        remote = _normalise_remote(arg)
        segs = split(remote)
        if segs and segs[0] not in DU_SECTIONS:
            raise TargetError(
                f"cannot analyse {arg!r}: du supports /, /my-files, /devices"
            )
        return Target(remote, None)
    local = Path(os.path.abspath(Path(cwd) / arg)) if arg is not None else Path(cwd)
    root = find_store_root(local)
    config = load_config(root) if root is not None else None
    if root is None or config is None:
        what = repr(str(local)) if arg is not None else f"current directory {str(local)!r}"
        raise TargetError(f"{what} is not inside a protonfs store; pass a Drive path instead")
    try:
        parts = local.resolve().relative_to(root).parts
    except ValueError:
        raise TargetError(
            f"{str(local)!r} resolves outside its protonfs store {str(root)!r}"
        ) from None
    remote = _normalise_remote(config.remote_root)
    for part in parts:
        remote = join(remote, part)
    return Target(remote, root)
