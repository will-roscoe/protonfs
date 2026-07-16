from __future__ import annotations

from pathlib import Path

from protonfs.ignore import (
    IgnoreMatcher,
    ignore_path,
    include_path,
    init_ignore,
    init_include,
)


def test_default_template_ignores_tmp_files(tmp_path: Path) -> None:
    init_ignore(tmp_path)
    matcher = IgnoreMatcher.from_file(tmp_path)
    assert matcher.matches("sim/03pol012/scratch.tmp")
    assert not matcher.matches("sim/03pol012/03pol012_00001")


def test_init_ignore_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    init_ignore(tmp_path)
    custom = "custom-pattern.*\n"
    ignore_path(tmp_path).write_text(custom)
    init_ignore(tmp_path)
    assert ignore_path(tmp_path).read_text() == custom


def test_from_file_with_no_ignore_file_matches_nothing(tmp_path: Path) -> None:
    matcher = IgnoreMatcher.from_file(tmp_path)
    assert not matcher.matches("anything.tmp")


def test_matcher_from_explicit_patterns() -> None:
    matcher = IgnoreMatcher(["*.log", "build/"])
    assert matcher.matches("run.log")
    assert matcher.matches("build/output.txt")
    assert not matcher.matches("src/main.py")


# --- include allowlist (#18) ---


def test_include_only_matching_files_are_synced() -> None:
    matcher = IgnoreMatcher([], include_patterns=["*.ev"])
    assert not matcher.matches("data/run1/dump.ev")  # matches include -> synced
    assert matcher.matches("data/run1/dump.txt")  # doesn't match include -> excluded


def test_include_applies_to_nested_files_without_slash_tricks() -> None:
    # Plain patterns (no leading `!*/`, no trailing `/**`) must still work at any depth,
    # because include patterns are only ever tested against FILE paths -- directory
    # descent is unconditional in the scanner, so no re-include-the-parent-dir dance
    # is needed here (unlike the raw ignore-only double-negation recipe).
    matcher = IgnoreMatcher([], include_patterns=["*_[0-9][0-9][0-9][0-9][0-9]", "*.ev"])
    assert not matcher.matches("sim/03pol012/03pol012_00001")
    assert not matcher.matches("sim/03pol012/nested/deep/run.ev")
    assert matcher.matches("sim/03pol012/notes.txt")


def test_include_and_ignore_intersection_ignore_wins() -> None:
    # A file matching include but ALSO matching ignore must still be excluded.
    matcher = IgnoreMatcher(["secret.ev"], include_patterns=["*.ev"])
    assert matcher.matches("secret.ev")
    assert not matcher.matches("public.ev")


def test_include_empty_list_is_noop() -> None:
    matcher = IgnoreMatcher(["*.tmp"], include_patterns=[])
    assert matcher.matches("scratch.tmp")
    assert not matcher.matches("anything/else.txt")


def test_include_blank_and_comment_only_lines_are_noop() -> None:
    matcher = IgnoreMatcher([], include_patterns=["# only *.ev files", "", "   "])
    assert not matcher.matches("anything.txt")
    assert not matcher.matches("anything.ev")


def test_include_absent_defaults_to_no_filtering() -> None:
    matcher = IgnoreMatcher(["*.tmp"])
    assert not matcher.matches("anything/else.txt")


def test_from_file_picks_up_include_file(tmp_path: Path) -> None:
    (tmp_path / ".protonfs").mkdir()
    include_path(tmp_path).write_text("*.ev\n")
    matcher = IgnoreMatcher.from_file(tmp_path)
    assert not matcher.matches("run1/dump.ev")
    assert matcher.matches("run1/dump.txt")


def test_from_file_with_absent_include_file_is_noop(tmp_path: Path) -> None:
    init_ignore(tmp_path)
    matcher = IgnoreMatcher.from_file(tmp_path)
    assert not matcher.matches("run1/whatever.anything")


def test_init_include_writes_fully_commented_template(tmp_path: Path) -> None:
    init_include(tmp_path)
    assert include_path(tmp_path).exists()
    matcher = IgnoreMatcher.from_file(tmp_path)
    # Default template must be a no-op -- everything still syncs (subject to ignore).
    assert not matcher.matches("anything/at/all.dat")


def test_init_include_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    init_include(tmp_path)
    custom = "*.ev\n"
    include_path(tmp_path).write_text(custom)
    init_include(tmp_path)
    assert include_path(tmp_path).read_text() == custom
