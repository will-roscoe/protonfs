"""The settings a ``du --gui`` page can change live, seeded from the command line.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from protonfs.usage.excludes import ExcludeRules
from protonfs.usage.policy import RefreshPolicy

#: Range of the page's depth slider (inclusive).
DEPTH_RANGE = (1, 6)

_EDITABLE = {"refresh", "maxdepth", "dirs_only", "freeze", "hide"}


@dataclass(frozen=True)
class GuiSettings:
    """What the page shows: target, refresh threshold, nesting depth and exclusions.

    :ivar target: the escaped Drive path the GUI was opened on (fixed for the session).
    :ivar refresh: refresh threshold as typed (``"5d"``, ``"0"``, ``"never"``, ...).
    :ivar maxdepth: how many levels the charts nest, within :data:`DEPTH_RANGE`.
    :ivar dirs_only: hide files.
    :ivar freeze: ``-x`` patterns (no Drive calls, cached figures shown).
    :ivar hide: ``-X`` patterns (not walked, not shown).

    .. versionadded:: 3.4.0
    """

    target: str
    refresh: str = "5d"
    maxdepth: int = 3
    dirs_only: bool = False
    freeze: list = field(default_factory=list)
    hide: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return the settings as a JSON-able dict (the ``/api/settings`` body)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> GuiSettings:
        """Rebuild settings from :meth:`to_dict` output, validating every field.

        :param data: a dict with ``target`` and any editable fields.
        :returns: the validated settings.
        :raises ValueError: on a missing target or an invalid field.
        """
        if not isinstance(data.get("target"), str):
            raise ValueError("settings need a string 'target'")
        rest = {k: v for k, v in data.items() if k != "target"}
        return cls(target=data["target"]).updated(rest)

    def updated(self, patch: dict) -> GuiSettings:
        """Return a copy with ``patch`` applied; the target cannot change.

        :param patch: any of ``refresh``, ``maxdepth``, ``dirs_only``, ``freeze``, ``hide``.
        :returns: the new settings (this object is unchanged).
        :raises ValueError: on an unknown key, an invalid value or a pattern that does not compile.
        """
        unknown = set(patch) - _EDITABLE
        if unknown:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))}")
        if "refresh" in patch:
            if not isinstance(patch["refresh"], str):
                raise ValueError("refresh must be a string such as '5d', '0' or 'never'")
            RefreshPolicy.parse(patch["refresh"])  # raises ValueError
        depth = patch.get("maxdepth", self.maxdepth)
        if type(depth) is not int or not DEPTH_RANGE[0] <= depth <= DEPTH_RANGE[1]:
            raise ValueError(f"maxdepth must be an int from {DEPTH_RANGE[0]} to {DEPTH_RANGE[1]}")
        if "dirs_only" in patch and not isinstance(patch["dirs_only"], bool):
            raise ValueError("dirs_only must be true or false")
        for key in ("freeze", "hide"):
            value = patch.get(key, [])
            if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
                raise ValueError(f"{key} must be a list of pattern strings")
        copied = {k: (list(v) if isinstance(v, list) else v) for k, v in patch.items()}
        new = replace(self, **copied)
        try:
            new.rules()
        except Exception as exc:  # noqa: BLE001 - any pattern-compiler failure
            raise ValueError(f"invalid freeze/hide pattern: {exc}") from exc
        return new

    def policy(self) -> RefreshPolicy:
        """Return the :class:`~protonfs.usage.policy.RefreshPolicy` for :attr:`refresh`."""
        return RefreshPolicy.parse(self.refresh)

    def rules(self) -> ExcludeRules:
        """Return the :class:`~protonfs.usage.excludes.ExcludeRules` for the patterns."""
        return ExcludeRules(freeze=self.freeze, hide=self.hide)
