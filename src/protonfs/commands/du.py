"""``protonfs du``: how much Proton Drive space remote paths take, trash included.

The terminal front end over :mod:`protonfs.usage`. It parses the flags, resolves the
targets, and renders the engine's rows as a table, plain lines or JSON.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import re

import click

_DEPTH_RE = re.compile(r"^(\d+)?(-)?(\d+)?$")


def parse_depth(text: str) -> tuple[int, int | None]:
    """Parse a ``-d`` value into ``(mindepth, maxdepth)``.

    ``N`` is a maximum depth (as in ``du -d N``), so the minimum is 0. ``MIN-MAX`` is a
    range, ``MIN-`` has no maximum, and ``-MAX`` is the same as ``MAX``.

    :param text: the option value, e.g. ``"2"``, ``"1-3"``, ``"2-"`` or ``"-2"``.
    :returns: ``(mindepth, maxdepth)``, with ``maxdepth`` ``None`` for an open range.
    :raises click.BadParameter: for anything else, or when MIN exceeds MAX.

    .. versionadded:: 3.4.0
    """
    match = _DEPTH_RE.match(text.strip())
    if not match or not (match.group(1) or match.group(3)):
        raise click.BadParameter(f"{text!r}: use N, MIN-MAX, MIN- or -MAX (e.g. 2 or 1-3)")
    low, dash, high = match.groups()
    if not dash:
        return 0, int(low)
    mindepth = int(low) if low else 0
    maxdepth = int(high) if high else None
    if maxdepth is not None and mindepth > maxdepth:
        raise click.BadParameter(f"{text!r}: MIN ({mindepth}) is greater than MAX ({maxdepth})")
    return mindepth, maxdepth
