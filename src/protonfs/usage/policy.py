"""Refresh policy and size parsing for ``protonfs du``.

.. versionadded:: 3.4.0
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from protonfs.retention import parse_duration

_SIZE_UNITS = {
    "": 1, "b": 1,
    "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12, "pb": 10**15,
    "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40, "pib": 2**50,
}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


@dataclass(frozen=True)
class RefreshPolicy:
    """How old a cached figure may be before it is fetched again.

    .. versionadded:: 3.4.0

    :param threshold: maximum age in seconds, or ``None`` to never refresh.
    :param allow_shortcut: whether a cheaper revalidation may stand in for a full
        fetch when the figure is stale.
    """

    threshold: float | None
    allow_shortcut: bool = True

    @property
    def never(self) -> bool:
        """Whether nothing is ever refreshed."""
        return self.threshold is None

    @property
    def fresh_only(self) -> bool:
        """Whether every figure is refreshed (threshold ``0``)."""
        return self.threshold == 0.0

    def is_stale(self, at: float | None, now: float) -> bool:
        """Return whether a figure fetched at ``at`` (``None`` if never) is stale at ``now``.

        :param at: fetch time, or ``None`` if never fetched.
        :param now: current time, same clock as ``at``.
        :returns: ``False`` always under ``never``; otherwise ``True`` when never
            fetched or at least ``threshold`` seconds old.
        """
        if self.threshold is None:
            return False
        if at is None:
            return True
        return now - at >= self.threshold

    @classmethod
    def parse(cls, text: str) -> RefreshPolicy:
        """Parse ``never``, ``0`` or a duration such as ``5d`` into a policy.

        ``0`` forbids the shortcut, so everything is fetched in full.

        :param text: the setting text.
        :returns: the policy.
        :raises protonfs.retention.RetentionError: if ``text`` is not a valid duration.
        """
        if text.strip().lower() == "never":
            return cls(None)
        seconds = parse_duration(text)
        return cls(seconds, allow_shortcut=seconds != 0.0)

    @classmethod
    def revalidate(cls) -> RefreshPolicy:
        """Return the policy that revalidates everything, allowing the shortcut."""
        return cls(0.0, allow_shortcut=True)


def parse_size(text: str) -> int:
    """Return a size in bytes from ``500GB``, ``3TB`` (decimal), ``2TiB`` (binary) or bytes.

    .. versionadded:: 3.4.0

    :param text: the size text.
    :returns: the number of bytes.
    :raises ValueError: if the text is not a number with an optional known unit.
    """
    match = _SIZE_RE.match(text)
    unit = match.group(2).lower() if match else None
    if not match or unit not in _SIZE_UNITS:
        raise ValueError(
            f"invalid size {text!r}: use bytes or a number with a unit "
            "KB|MB|GB|TB|PB (decimal) or KiB|MiB|GiB|TiB|PiB (binary), e.g. 500GB, 2TiB"
        )
    return int(float(match.group(1)) * _SIZE_UNITS[unit])
