from __future__ import annotations

from pathlib import Path

import pathspec

IGNORE_FILE_NAME = "ignore"

DEFAULT_IGNORE_TEMPLATE = """\
# protonfs ignore patterns (gitignore syntax)
# Scopes what protonfs will sync from a given directory -- independent of
# the repo's own .gitignore.
*.tmp
*.swp
core.*
"""


def ignore_path(repo_root: Path) -> Path:
    return repo_root / ".protonfs" / IGNORE_FILE_NAME


def init_ignore(repo_root: Path) -> None:
    path = ignore_path(repo_root)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_IGNORE_TEMPLATE)


class IgnoreMatcher:
    def __init__(self, patterns: list[str]) -> None:
        self._spec = pathspec.GitIgnoreSpec.from_lines(patterns)

    @classmethod
    def from_file(cls, repo_root: Path) -> IgnoreMatcher:
        path = ignore_path(repo_root)
        if not path.exists():
            return cls([])
        return cls(path.read_text().splitlines())

    def matches(self, rel_path: str) -> bool:
        return self._spec.match_file(rel_path)
