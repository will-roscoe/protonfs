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

Explicit override directives (from 1.0.0): a ``+:<spec>`` token anywhere in a commit
message overrides the Conventional Commit classification entirely, where ``<spec>``
is ``major`` | ``minor`` | ``patch`` | ``pre`` | ``prepre`` | ``rc``. The pre-release
ladder follows SemVer 2.0.0 precedence with channels ``alpha < beta < rc``:

* from a final release ``X.Y.Z``: ``pre`` -> ``X.(Y+1).0-alpha``, ``prepre`` ->
  ``X.(Y+1).0-alpha.0``, ``rc`` -> ``X.(Y+1).0-rc`` (the pre-release always belongs
  to the NEXT minor, so it sorts after the released base; pair with ``+:major``'s
  semantics by hand-tagging when a pre-release of a major is wanted).
* within a pre-release ``B-chan[.n]``: ``prepre`` increments the number
  (``-alpha`` -> ``-alpha.1``, ``-alpha.1`` -> ``-alpha.2``); ``pre`` advances the
  channel (``alpha`` -> ``beta`` -> ``rc`` -> final ``B``); ``rc`` jumps any earlier
  channel straight to ``B-rc`` and increments when already there (``-rc`` ->
  ``-rc.1``); ``patch``/``minor`` finalize to ``B``; ``major`` finalizes to ``B``
  when ``B`` is already an ``X.0.0``, else bumps to ``(X+1).0.0``.
* plain Conventional Commits landing during a pre-release increment the pre-release
  number and never escape the channel -- leaving requires a directive.

When several directives appear across the batch, the highest-impact one wins
(``major > minor > patch > rc > pre > prepre``). Directives are imperative and are
not subject to the pre-1.0 breaking-change demotion.

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

# `type` or `type(scope)` optionally followed by `!`, then `:`. Deliberately NOT
# multiline: per Conventional Commits the type header is the commit's FIRST line
# only. Squash-merge messages carry the whole PR body, where a quoted `feat:` or
# `refactor!:` at the start of a body line must not (mis)classify the commit --
# post-1.0 a stray `!` would trigger a spurious MAJOR bump.
_HEADER_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\([^)]*\))?(?P<bang>!)?:")
# The breaking footer, by contrast, legitimately lives mid-message (a footer line),
# so this one stays multiline.
_BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)

_MINOR_TYPES = {"feat"}
_PATCH_TYPES = {"fix", "perf", "revert"}

# `+:<spec>` override directives -- anywhere in the message, word-bounded.
_DIRECTIVE_RE = re.compile(r"\+:(major|minor|patch|rc|prepre|pre)\b")
# Highest-impact directive wins across a batch (imperative, so ordered by the
# magnitude of the resulting version, not by commit order).
_DIRECTIVE_RANK = ["prepre", "pre", "rc", "patch", "minor", "major"]

_CHANNELS = ["alpha", "beta", "rc"]  # SemVer 2.0.0 precedence order.


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


def classify_directive(messages: list[str]) -> str | None:
    """Return the highest-impact `+:<spec>` directive across `messages`, or None."""
    found = {m for message in messages for m in _DIRECTIVE_RE.findall(message)}
    for spec in reversed(_DIRECTIVE_RANK):
        if spec in found:
            return spec
    return None


def _parse_version(current: str | None) -> tuple[int, int, int, str | None, int | None]:
    """Parse ``X.Y.Z`` or ``X.Y.Z-<chan>[.n]`` -> (major, minor, patch, chan, n).

    ``chan`` is None for a final version; ``n`` is None for a bare channel
    (``1.1.0-alpha``) and an int for a numbered one (``1.1.0-alpha.1``).
    """
    if not current:
        return (0, 0, 0, None, None)
    text = current.strip().lstrip("v")
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)(?:\.(\d+))?)?", text
    )
    if not match:
        raise ValueError(f"not a SemVer X.Y.Z[-chan[.n]] version: {current!r}")
    chan = match.group(4)
    num = int(match.group(5)) if match.group(5) is not None else None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), chan, num)


def _fmt(
    major: int, minor: int, patch: int, chan: str | None = None, num: int | None = None
) -> str:
    base = f"{major}.{minor}.{patch}"
    if chan is None:
        return base
    return f"{base}-{chan}" if num is None else f"{base}-{chan}.{num}"


def _apply_directive(spec: str, version: tuple[int, int, int, str | None, int | None]) -> str:
    """The `+:<spec>` state machine (see module docstring for the ladder)."""
    major, minor, patch, chan, num = version

    if chan is None:  # from a final release
        if spec == "major":
            return _fmt(major + 1, 0, 0)
        if spec == "minor":
            return _fmt(major, minor + 1, 0)
        if spec == "patch":
            return _fmt(major, minor, patch + 1)
        # Pre-release directives rebase onto the NEXT minor so the pre-release
        # sorts after the already-released base (SemVer precedence).
        if spec == "pre":
            return _fmt(major, minor + 1, 0, "alpha")
        if spec == "prepre":
            return _fmt(major, minor + 1, 0, "alpha", 0)
        return _fmt(major, minor + 1, 0, "rc")  # spec == "rc"

    # From a pre-release: a bare channel counts as an implicit 0 when incrementing.
    if spec == "prepre":
        return _fmt(major, minor, patch, chan, 1 if num is None else num + 1)
    if spec == "pre":
        idx = _CHANNELS.index(chan)
        if idx + 1 < len(_CHANNELS):
            return _fmt(major, minor, patch, _CHANNELS[idx + 1])
        return _fmt(major, minor, patch)  # past rc -> the final release
    if spec == "rc":
        if chan != "rc":
            return _fmt(major, minor, patch, "rc")
        return _fmt(major, minor, patch, "rc", 1 if num is None else num + 1)
    if spec in ("patch", "minor"):
        return _fmt(major, minor, patch)  # finalize: the base already carries the bump
    # spec == "major": finalize when the base is already an X.0.0, else bump to it.
    if minor == 0 and patch == 0:
        return _fmt(major, minor, patch)
    return _fmt(major + 1, 0, 0)


def compute_next_version(current: str | None, messages: list[str]) -> str | None:
    """Compute the next version string, or None if no release is warranted.

    `current` may be ``None``/empty (no prior tag -> treated as 0.0.0) and may carry
    a leading ``v``. Raises ValueError if `current` is present but malformed.

    A `+:<spec>` directive in any message overrides the Conventional Commit
    classification entirely (see module docstring).
    """
    version = _parse_version(current)
    directive = classify_directive(messages)
    if directive is not None:
        return _apply_directive(directive, version)

    bump = classify_bump(messages)
    if bump is None:
        return None
    major, minor, patch, chan, num = version
    if chan is not None:
        # Mid-pre-release, plain commits only advance the pre-release number; the
        # channel/base never moves without an explicit directive.
        return _fmt(major, minor, patch, chan, 1 if num is None else num + 1)
    # Pre-1.0: demote breaking (major) to minor so 0.x doesn't jump to 1.0.0.
    if bump == "major" and major == 0:
        bump = "minor"
    if bump == "major":
        return _fmt(major + 1, 0, 0)
    if bump == "minor":
        return _fmt(major, minor + 1, 0)
    return _fmt(major, minor, patch + 1)


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
