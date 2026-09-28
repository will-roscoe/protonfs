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
    r"^\[Unreleased\]: (?P<base>https://\S+?)/compare/"
    r"(?P<prev_tag>v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)(?:\.\d+)?)?)\.\.\.HEAD$",
    re.MULTILINE,
)


# Release-notes generation from Conventional Commit subjects: section order in the
# finalized entry. `chore` is deliberately absent (housekeeping stays out of release
# notes), as is anything carrying `[skip ci]` (badge/changelog bot commits).
_SECTION_ORDER: list[tuple[str, str]] = [
    ("feat", "Features"),
    ("fix", "Bug fixes"),
    ("perf", "Performance"),
    ("revert", "Reverts"),
    ("refactor", "Refactors"),
    ("docs", "Documentation"),
    ("test", "Tests"),
    ("build", "Build"),
    ("ci", "CI"),
    ("style", "Style"),
]
_SUBJECT_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?!?:\s*(?P<desc>.+)$")


def render_commit_sections(subjects: list[str]) -> str:
    """Group Conventional Commit subjects (oldest first) into `### <Section>` blocks.

    Within each section commits stay chronological, earliest at the top. Subjects
    that are non-conventional, `chore`-typed, of an unknown type, or tagged
    `[skip ci]` are dropped. Returns "" when nothing survives.
    """
    titles = dict(_SECTION_ORDER)
    groups: dict[str, list[str]] = {}
    for subject in subjects:
        subject = subject.strip()
        if not subject or "[skip ci]" in subject:
            continue
        match = _SUBJECT_RE.match(subject)
        if not match:
            continue
        ctype = match.group("type").lower()
        if ctype not in titles:
            continue
        scope, desc = match.group("scope"), match.group("desc")
        line = f"- **{scope}**: {desc}" if scope else f"- {desc}"
        groups.setdefault(ctype, []).append(line)
    blocks = [
        f"### {title}\n\n" + "\n".join(groups[ctype])
        for ctype, title in _SECTION_ORDER
        if ctype in groups
    ]
    return "\n\n".join(blocks)


def finalize_changelog(
    text: str,
    version: str,
    release_date: date,
    commit_subjects: list[str] | None = None,
) -> tuple[str, bool]:
    """Return ``(new_text, changed)``.

    Renames the ``## [Unreleased]`` section to ``## [<version>] - <release_date>``
    and inserts a fresh empty ``## [Unreleased]`` above it. The finalized entry is
    any hand-written Unreleased content followed by release notes generated from
    `commit_subjects` (oldest first; see `render_commit_sections`). Also rewires
    the reference-style links at the bottom of the file when present. Leaves
    `text` untouched (``changed=False``) when there is neither hand-written nor
    generated content.
    """
    start = text.find(_UNRELEASED_HEADER)
    if start == -1:
        return text, False

    body_start = start + len(_UNRELEASED_HEADER)
    next_header = _HEADER_RE.search(text, body_start)
    body_end = next_header.start() if next_header else len(text)
    body = text[body_start:body_end]

    generated = render_commit_sections(commit_subjects or [])
    if generated:
        body = f"{body.rstrip()}\n\n{generated}\n\n" if body.strip() else f"\n\n{generated}\n\n"

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

    # Commit subjects arrive as a NUL-separated stream on stdin (mirroring
    # compute_next_version.py's input contract): `git log -z --reverse --format=%s`.
    # A terminal (manual invocation without a pipe) means "no generated notes".
    subjects: list[str] = []
    if not sys.stdin.isatty():
        subjects = [s for s in sys.stdin.read().split("\0") if s.strip()]

    text = changelog_path.read_text(encoding="utf-8")
    new_text, changed = finalize_changelog(
        text, version, datetime.now(timezone.utc).date(), commit_subjects=subjects
    )

    if not changed:
        print("Unreleased section is empty -- nothing to finalize.")
        return 0

    changelog_path.write_text(new_text, encoding="utf-8")
    print(f"Finalized CHANGELOG.md Unreleased section as {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
