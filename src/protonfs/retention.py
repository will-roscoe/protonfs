"""Settle rule and retention policy: which local copies are old enough to remove.

A file can still be in use when no process holds it open: a simulation writes a dump,
closes it, and may keep appending to its time series for days. So nothing that deletes
local bytes (``offload``, ``prune``) acts on a file modified more recently than a
minimum age -- the **settle window** -- and ``prune`` additionally keeps the newest
files of every directory (#158).

Both rules use the file's local modification time (``mtime``). "Per directory" means
the file's immediate parent directory, relative to the repo root.

.. versionadded:: 2.2.0
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath

# Defaults shared by offload, prune and their scheduled jobs: one day unmodified, and the
# ten newest files of each directory kept.
DEFAULT_MIN_AGE = "1d"
DEFAULT_KEEP = 10

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$")


class RetentionError(ValueError):
    """A settle window or retention setting that cannot be used."""


def parse_duration(text: str) -> float:
    """Seconds in a duration such as ``90s``, ``30m``, ``12h``, ``1d`` or ``1.5d``.

    ``0`` (no settle window) is the only unitless value accepted: a bare number is
    ambiguous, and guessing the wrong unit here decides what gets deleted.

    :raises RetentionError: on anything else.
    """
    if text.strip() == "0":
        return 0.0
    match = _DURATION_RE.match(text)
    if not match:
        raise RetentionError(
            f"invalid duration {text!r}: use a number with a unit s|m|h|d (e.g. 12h, 1d), "
            "or 0 for no settle window"
        )
    return float(match.group(1)) * _UNITS[match.group(2)]


def format_age(seconds: float) -> str:
    """A short human form of a duration, e.g. ``3h`` or ``2d``."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            value = seconds / size
            if value >= 10 or value.is_integer():
                return f"{value:.0f}{unit}"
            return f"{value:.1f}{unit}"
    return f"{seconds:.0f}s"


def is_settled(mtime: float, min_age: float, now: float) -> bool:
    """Whether a file last modified at ``mtime`` has been left alone for ``min_age``."""
    return now - mtime >= min_age


def select_prune_candidates(
    files: dict[str, float], keep: int, min_age: float, now: float
) -> list[str]:
    """The files a retention policy may remove (pure; no filesystem access).

    A file is a candidate only when BOTH hold: it is not among the ``keep`` most
    recently modified files of its directory, AND it has not been modified for
    ``min_age`` seconds. Either rule alone protects a file.

    :param files: ``{repo-relative path: local mtime}`` of the tracked files in scope.
    :param keep: newest files to keep per directory (``0`` keeps none by count).
    :param min_age: settle window in seconds.
    :param now: the current time (seconds since the epoch).
    :returns: candidate paths, sorted.
    :raises RetentionError: when ``keep`` or ``min_age`` is negative.
    """
    if keep < 0:
        raise RetentionError("--keep must be 0 or more")
    if min_age < 0:
        raise RetentionError("--min-age must be 0 or more")
    by_dir: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for rel, mtime in files.items():
        by_dir[str(PurePosixPath(rel).parent)].append((mtime, rel))
    candidates: list[str] = []
    for entries in by_dir.values():
        # newest first; ties broken by path so the choice is deterministic
        entries.sort(key=lambda item: (-item[0], item[1]))
        for mtime, rel in entries[keep:]:
            if is_settled(mtime, min_age, now):
                candidates.append(rel)
    return sorted(candidates)
