"""gitignore-syntax filtering of which repo files protonfs syncs.

Two independent pattern files under ``.protonfs/`` control this, both using gitignore
syntax via the :mod:`pathspec` library: ``ignore`` (a denylist, matches
:data:`DEFAULT_IGNORE_TEMPLATE`'s defaults unless customized) and ``include`` (an
optional allowlist, off by default). :class:`IgnoreMatcher` combines the two -- ignore
always wins over include (#18).

.. versionadded:: 1.0.0
"""

from __future__ import annotations

from pathlib import Path

import pathspec

IGNORE_FILE_NAME = "ignore"
INCLUDE_FILE_NAME = "include"

DEFAULT_IGNORE_TEMPLATE = """\
# protonfs ignore patterns (gitignore syntax)
# Scopes what protonfs will sync from a given directory -- independent of
# the repo's own .gitignore.
*.tmp
*.swp
core.*
"""

# Fully commented-out by default -- an include file with no active patterns is a no-op
# (see IgnoreMatcher), so this template changes nothing until a user uncomments lines.
DEFAULT_INCLUDE_TEMPLATE = """\
# protonfs include allowlist (gitignore syntax, matched against FILE paths only)
#
# If this file is absent or has no active (non-blank, non-comment) lines, protonfs
# syncs everything not excluded by `.protonfs/ignore`, exactly as before -- this file
# is entirely optional.
#
# Uncomment lines below to sync ONLY files matching at least one pattern here (and
# still not matching `.protonfs/ignore` -- ignore always wins over include). Patterns
# are plain gitignore file patterns; no `!*/` or `dir/**` tricks are needed since
# directories are always descended into regardless of include/ignore.
#
# *.ev
# *.sink
# *_[0-9][0-9][0-9][0-9][0-9]
"""


def ignore_path(repo_root: Path) -> Path:
    """Return the path to the repo's ignore-pattern denylist file.

    :param repo_root: Root of the repo being synced.
    :returns: ``.protonfs/ignore`` under ``repo_root``.
    """
    return repo_root / ".protonfs" / IGNORE_FILE_NAME


def include_path(repo_root: Path) -> Path:
    """Return the path to the repo's include-pattern allowlist file.

    :param repo_root: Root of the repo being synced.
    :returns: ``.protonfs/include`` under ``repo_root``.
    """
    return repo_root / ".protonfs" / INCLUDE_FILE_NAME


def init_ignore(repo_root: Path) -> None:
    """Create ``.protonfs/ignore`` with :data:`DEFAULT_IGNORE_TEMPLATE` if absent.

    :param repo_root: Root of the repo being synced.

    .. note:: No-op if the file already exists -- never overwrites user customizations.
    """
    path = ignore_path(repo_root)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_IGNORE_TEMPLATE)


def init_include(repo_root: Path) -> None:
    """Create ``.protonfs/include`` with :data:`DEFAULT_INCLUDE_TEMPLATE` if absent.

    :param repo_root: Root of the repo being synced.

    .. note:: No-op if the file already exists. The written template is fully commented
       out, so this changes sync behaviour for no repo until the user uncomments a line.
    """
    path = include_path(repo_root)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_INCLUDE_TEMPLATE)


def _active_patterns(patterns: list[str]) -> list[str]:
    """Lines that actually mean something to pathspec -- drop blank lines and full-line
    comments so an include file containing only commentary is treated as absent (#18)."""
    return [line for line in patterns if line.strip() and not line.strip().startswith("#")]


class IgnoreMatcher:
    """Combines an ignore denylist with an optional include allowlist.

    Ignore always wins over include (#18): a path matching both an ignore pattern and
    an include pattern is still excluded. See :meth:`matches` for the exact precedence.

    :param patterns: Gitignore-syntax ignore patterns (denylist).
    :param include_patterns: Gitignore-syntax include patterns (allowlist), or ``None``.
        Blank lines and full-line comments are dropped before use (see
        :func:`_active_patterns`); if nothing remains active, the allowlist is treated
        as absent (matches everything), not as an empty allowlist (which would match
        nothing).
    """

    def __init__(self, patterns: list[str], include_patterns: list[str] | None = None) -> None:
        self._spec = pathspec.GitIgnoreSpec.from_lines(patterns)
        active_include = _active_patterns(include_patterns or [])
        # None (not an empty spec) is the "no allowlist configured" sentinel -- an empty
        # GitIgnoreSpec matches nothing, which would exclude every file, the opposite of
        # today's default (no include file = sync everything not ignored) (#18).
        self._include_spec = (
            pathspec.GitIgnoreSpec.from_lines(active_include) if active_include else None
        )

    @classmethod
    def from_file(cls, repo_root: Path) -> IgnoreMatcher:
        """Build an :class:`IgnoreMatcher` from a repo's ``.protonfs/ignore`` and
        ``.protonfs/include`` files.

        :param repo_root: Root of the repo being synced.
        :returns: A matcher reflecting the repo's current pattern files; an absent file
            is treated as containing no patterns.

        .. seealso:: :func:`init_ignore`, :func:`init_include` to create default files.
        """
        path = ignore_path(repo_root)
        patterns = path.read_text().splitlines() if path.exists() else []
        include_file = include_path(repo_root)
        include_patterns = include_file.read_text().splitlines() if include_file.exists() else []
        return cls(patterns, include_patterns)

    def matches(self, rel_path: str) -> bool:
        """True if `rel_path` should be EXCLUDED from sync: it matches the ignore
        denylist, or an active include allowlist is configured and `rel_path` matches
        none of its patterns. Ignore always wins over include (#18)."""
        if self._spec.match_file(rel_path):
            return True
        if self._include_spec is not None and not self._include_spec.match_file(rel_path):
            return True
        return False
