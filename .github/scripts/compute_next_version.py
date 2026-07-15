#!/usr/bin/env python3
"""Compute the next SemVer release from Conventional Commit messages.

Used by ``.github/workflows/auto-release.yml`` to auto-bump + auto-tag on merge to
``main`` (issue #31). Given the current version tag and the commit messages since
that tag, it decides whether a release is warranted and what the next version is.

Bump policy (Conventional Commits, semantic-release-style defaults):

* ``feat``                          -> minor
* ``fix`` / ``perf`` / ``revert``   -> patch
* a breaking change (``type!:`` or a ``BREAKING CHANGE:`` footer) -> major
* everything else (``chore`` / ``docs`` / ``style`` / ``refactor`` / ``build`` /
  ``ci`` / ``test`` / non-conventional subjects) -> no release

Pre-1.0 policy: while the major version is ``0`` a breaking change bumps the
**minor** component, not the major one, so active 0.x development does not jump to
1.0.0 by accident. SemVer starts being fully enforced from 1.0 onward (milestone M4).

CLI: prints the next version (no ``v`` prefix) to stdout, or nothing (exit 0) when
no release is warranted. Reads the current version from ``$CURRENT_VERSION`` and the
commit messages as a NUL-separated stream on stdin (``git log -z --format=%B``).
"""

from __future__ import annotations

import os
import re
import sys

# Highest-precedence bump wins across a set of commits.
_RANK = {None: 0, "patch": 1, "minor": 2, "major": 3}
_RANK_INV = {v: k for k, v in _RANK.items()}

# `type` or `type(scope)` optionally followed by `!`, then `:`.
_HEADER_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\([^)]*\))?(?P<bang>!)?:", re.MULTILINE)
_BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)

_MINOR_TYPES = {"feat"}
_PATCH_TYPES = {"fix", "perf", "revert"}


def _bump_for_message(message: str) -> str | None:
    """Return the bump ('major'|'minor'|'patch') implied by a single commit, or None."""
    header = _HEADER_RE.search(message)
    breaking = bool(_BREAKING_FOOTER_RE.search(message)) or bool(header and header.group("bang"))
    if breaking:
        return "major"
    if not header:
        return None
    ctype = header.group("type").lower()
    if ctype in _MINOR_TYPES:
        return "minor"
    if ctype in _PATCH_TYPES:
        return "patch"
    return None


def classify_bump(messages: list[str]) -> str | None:
    """Return the highest-precedence bump across all `messages`, or None for no release."""
    best = 0
    for message in messages:
        best = max(best, _RANK[_bump_for_message(message)])
    return _RANK_INV[best]


def _parse_version(current: str | None) -> tuple[int, int, int]:
    if not current:
        return (0, 0, 0)
    text = current.strip().lstrip("v")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        raise ValueError(f"not a plain X.Y.Z version: {current!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def compute_next_version(current: str | None, messages: list[str]) -> str | None:
    """Compute the next version string, or None if no release is warranted.

    `current` may be ``None``/empty (no prior tag -> treated as 0.0.0) and may carry
    a leading ``v``. Raises ValueError if `current` is present but malformed.
    """
    bump = classify_bump(messages)
    if bump is None:
        return None
    major, minor, patch = _parse_version(current)
    # Pre-1.0: demote breaking (major) to minor so 0.x doesn't jump to 1.0.0.
    if bump == "major" and major == 0:
        bump = "minor"
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _main() -> int:
    current = os.environ.get("CURRENT_VERSION", "").strip()
    raw = sys.stdin.read()
    # git log -z separates commit bodies with NUL; drop the trailing empty field.
    messages = [m for m in raw.split("\0") if m.strip()]
    nxt = compute_next_version(current or None, messages)
    if nxt is None:
        return 0
    sys.stdout.write(nxt + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
