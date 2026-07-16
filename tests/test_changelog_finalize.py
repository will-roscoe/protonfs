"""Tests for the release-time changelog finalizer (`.github/scripts/finalize_changelog.py`).

The script is not part of the shipped `protonfs` package (it lives under
`.github/scripts/`), so it is loaded by path via importlib rather than imported.
See `tests/test_versioning.py` for the same pattern applied to
`compute_next_version.py`.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "finalize_changelog.py"


def _load():
    spec = importlib.util.spec_from_file_location("finalize_changelog", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()
finalize_changelog = mod.finalize_changelog

_DATE = date(2026, 8, 1)

_WITH_ENTRIES = """\
# Changelog

## [Unreleased]

### Added

- feat: something new (#99)

## [0.17.0] - 2026-07-16

### Added

- stuff (#61)

[Unreleased]: https://github.com/will-roscoe/protonfs/compare/v0.17.0...HEAD
[0.17.0]: https://github.com/will-roscoe/protonfs/compare/v0.16.0...v0.17.0
"""

_EMPTY_UNRELEASED = """\
# Changelog

## [Unreleased]

## [0.17.0] - 2026-07-16

### Added

- stuff (#61)

[Unreleased]: https://github.com/will-roscoe/protonfs/compare/v0.17.0...HEAD
[0.17.0]: https://github.com/will-roscoe/protonfs/compare/v0.16.0...v0.17.0
"""

_WHITESPACE_ONLY_UNRELEASED = """\
# Changelog

## [Unreleased]


## [0.17.0] - 2026-07-16

Stuff.
"""

_NO_UNRELEASED_SECTION = """\
# Changelog

## [0.17.0] - 2026-07-16

Stuff.
"""


class TestFinalizeChangelog:
    def test_noop_when_unreleased_empty(self):
        new_text, changed = finalize_changelog(_EMPTY_UNRELEASED, "0.18.0", _DATE)
        assert changed is False
        assert new_text == _EMPTY_UNRELEASED

    def test_noop_when_unreleased_whitespace_only(self):
        new_text, changed = finalize_changelog(_WHITESPACE_ONLY_UNRELEASED, "0.18.0", _DATE)
        assert changed is False
        assert new_text == _WHITESPACE_ONLY_UNRELEASED

    def test_noop_when_no_unreleased_header(self):
        new_text, changed = finalize_changelog(_NO_UNRELEASED_SECTION, "0.18.0", _DATE)
        assert changed is False
        assert new_text == _NO_UNRELEASED_SECTION

    def test_renames_section_and_leaves_fresh_unreleased(self):
        new_text, changed = finalize_changelog(_WITH_ENTRIES, "0.18.0", _DATE)
        assert changed is True
        assert "## [Unreleased]\n\n## [0.18.0] - 2026-08-01" in new_text
        # Fresh Unreleased is empty: immediately followed by the new version header.
        assert new_text.index("## [Unreleased]") < new_text.index("## [0.18.0]")

    def test_preserves_finalized_entry_content(self):
        new_text, _ = finalize_changelog(_WITH_ENTRIES, "0.18.0", _DATE)
        assert "- feat: something new (#99)" in new_text
        # Old version section untouched.
        assert "## [0.17.0] - 2026-07-16" in new_text
        assert "- stuff (#61)" in new_text

    def test_updates_reference_links(self):
        new_text, _ = finalize_changelog(_WITH_ENTRIES, "0.18.0", _DATE)
        assert (
            "[Unreleased]: https://github.com/will-roscoe/protonfs/compare/v0.18.0...HEAD"
            in new_text
        )
        assert (
            "[0.18.0]: https://github.com/will-roscoe/protonfs/compare/v0.17.0...v0.18.0"
            in new_text
        )
        # Old Unreleased link line (pointing at v0.17.0...HEAD) is gone.
        assert "compare/v0.17.0...HEAD" not in new_text

    def test_accepts_v_prefixed_version(self):
        new_text, changed = finalize_changelog(_WITH_ENTRIES, "v0.18.0", _DATE)
        # The function does not strip a leading v itself -- callers (the CLI) do.
        assert changed is True
        assert "## [v0.18.0] - 2026-08-01" in new_text

    def test_missing_link_block_still_finalizes_section(self):
        text_no_links = _WITH_ENTRIES.split("[Unreleased]:")[0]
        new_text, changed = finalize_changelog(text_no_links, "0.18.0", _DATE)
        assert changed is True
        assert "## [0.18.0] - 2026-08-01" in new_text
