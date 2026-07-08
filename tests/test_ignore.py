from __future__ import annotations

from pathlib import Path

from protonfs.ignore import IgnoreMatcher, ignore_path, init_ignore


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
