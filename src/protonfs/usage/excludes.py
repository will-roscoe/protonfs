"""Gitignore-style freeze/hide exclusion rules for ``protonfs du``.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from typing import Any

from pathspec import GitIgnoreSpec


class Exclusion(enum.Enum):
    """How ``du`` treats a path: ``NONE`` (walk normally), ``FREEZE`` or ``HIDE``.

    .. versionadded:: 3.4.0
    """

    NONE = "none"
    FREEZE = "freeze"
    HIDE = "hide"


class ExcludeRules:
    """Freeze and hide rules written as gitignore patterns.

    A path under an excluded directory inherits that exclusion, and ``HIDE`` wins
    over ``FREEZE``. The du target itself (``""``) is never excluded.

    .. versionadded:: 3.4.0
    """

    def __init__(self, freeze: Sequence[str] = (), hide: Sequence[str] = ()) -> None:
        """Compile the rules.

        :param freeze: Gitignore patterns for paths to list but not walk into.
        :param hide: Gitignore patterns for paths to leave out entirely.
        """
        self._freeze = list(freeze)
        self._hide = list(hide)
        self._freeze_spec: GitIgnoreSpec | None = (
            GitIgnoreSpec.from_lines(self._freeze) if self._freeze else None
        )
        self._hide_spec: GitIgnoreSpec | None = (
            GitIgnoreSpec.from_lines(self._hide) if self._hide else None
        )

    @staticmethod
    def _hit(spec: GitIgnoreSpec | None, parts: list[str], is_dir: bool) -> bool:
        if spec is None:
            return False
        for i in range(1, len(parts) + 1):
            last = i == len(parts)
            path = "/".join(parts[:i])
            if (is_dir or not last) and not path.endswith("/"):
                path += "/"
            if spec.match_file(path):
                return True
        return False

    def match(self, rel_path: str, is_dir: bool) -> Exclusion:
        """Return the exclusion that applies to ``rel_path``.

        :param rel_path: Path relative to the du target, ``"/"``-joined with plain
            (unescaped) names; ``""`` is the target itself.
        :param is_dir: Whether ``rel_path`` is a directory.
        :returns: ``HIDE`` if the path or an ancestor directory matches a hide rule,
            else ``FREEZE`` likewise, else ``NONE``.
        """
        if not rel_path:
            return Exclusion.NONE
        parts = rel_path.split("/")
        if self._hit(self._hide_spec, parts, is_dir):
            return Exclusion.HIDE
        if self._hit(self._freeze_spec, parts, is_dir):
            return Exclusion.FREEZE
        return Exclusion.NONE

    def to_dict(self) -> dict[str, list[str]]:
        """Return ``{"freeze": [...], "hide": [...]}`` with the original patterns."""
        return {"freeze": list(self._freeze), "hide": list(self._hide)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExcludeRules:
        """Build rules from :meth:`to_dict` output; missing keys mean no rules.

        :param data: Mapping with optional ``freeze`` and ``hide`` pattern lists.
        :returns: The compiled rules.
        """
        return cls(freeze=data.get("freeze", ()), hide=data.get("hide", ()))
