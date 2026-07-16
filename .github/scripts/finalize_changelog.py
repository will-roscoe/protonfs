#!/usr/bin/env python3
"""Finalize the ``[Unreleased]`` section of CHANGELOG.md for a release (issue #13).

Used by ``.github/workflows/auto-release.yml``'s ``compute`` job: after the next
version has been decided but *before* the release tag is created, this script
renames ``## [Unreleased]`` to ``## [<version>] - <date>`` (leaving a fresh, empty
``## [Unreleased]`` above it) so the finalized changelog entry is committed to
``main`` and included in the commit the tag points at. It fails soft: if the
``Unreleased`` section has no content, it makes no changes and exits 0.

CLI: ``finalize_changelog.py <version> [changelog_path]``

* ``<version>`` -- the new version, no leading ``v`` (e.g. ``0.18.0``).
* ``changelog_path`` -- defaults to ``CHANGELOG.md`` in the repo root (this
  script's grandparent directory).

Exit status is always 0; check stdout / the file's mtime to know whether it
changed anything (the workflow step diffs ``git status`` for that).
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_UNRELEASED_HEADER = "## [Unreleased]"
_HEADER_RE = re.compile(r"^## \[", re.MULTILINE)
_UNRELEASED_LINK_RE = re.compile(
    r"^\[Unreleased\]: (?P<base>https://\S+?)/compare/(?P<prev_tag>v\d+\.\d+\.\d+)\.\.\.HEAD$",
    re.MULTILINE,
)


def finalize_changelog(text: str, version: str, release_date: date) -> tuple[str, bool]:
    """Return ``(new_text, changed)``.

    Renames the ``## [Unreleased]`` section to ``## [<version>] - <release_date>``
    and inserts a fresh empty ``## [Unreleased]`` above it, only when the
    existing Unreleased section has non-whitespace content. Also rewires the
    reference-style links at the bottom of the file when present. Leaves
    `text` untouched (``changed=False``) when there's nothing to finalize.
    """
    start = text.find(_UNRELEASED_HEADER)
    if start == -1:
        return text, False

    body_start = start + len(_UNRELEASED_HEADER)
    next_header = _HEADER_RE.search(text, body_start)
    body_end = next_header.start() if next_header else len(text)
    body = text[body_start:body_end]

    if not body.strip():
        return text, False

    date_str = release_date.isoformat()
    new_section = (
        f"{_UNRELEASED_HEADER}\n\n## [{version}] - {date_str}{body}"
    )
    new_text = text[:start] + new_section + text[body_end:]

    link_match = _UNRELEASED_LINK_RE.search(new_text)
    if link_match:
        base = link_match.group("base")
        prev_tag = link_match.group("prev_tag")
        old_line = link_match.group(0)
        new_lines = (
            f"[Unreleased]: {base}/compare/v{version}...HEAD\n"
            f"[{version}]: {base}/compare/{prev_tag}...v{version}"
        )
        new_text = new_text.replace(old_line, new_lines, 1)

    return new_text, True


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: finalize_changelog.py <version> [changelog_path]", file=sys.stderr)
        return 2

    version = argv[0].strip().lstrip("v")
    changelog_path = Path(argv[1]) if len(argv) > 1 else _REPO_ROOT / "CHANGELOG.md"

    text = changelog_path.read_text(encoding="utf-8")
    new_text, changed = finalize_changelog(text, version, datetime.now(timezone.utc).date())

    if not changed:
        print("Unreleased section is empty -- nothing to finalize.")
        return 0

    changelog_path.write_text(new_text, encoding="utf-8")
    print(f"Finalized CHANGELOG.md Unreleased section as {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
