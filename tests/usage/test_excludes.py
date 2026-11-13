from __future__ import annotations

import pytest

from protonfs.usage.excludes import ExcludeRules, Exclusion


@pytest.mark.parametrize("pattern", ["some-dir", "some-dir/"])
def test_freeze_matches_dir_at_any_depth(pattern):
    r = ExcludeRules(freeze=[pattern])
    assert r.match("some-dir", True) is Exclusion.FREEZE
    assert r.match("a/b/some-dir", True) is Exclusion.FREEZE
    assert r.match("a/some-dir/deeper", True) is Exclusion.FREEZE  # inherited
    assert r.match("a/some-dir/file.txt", False) is Exclusion.FREEZE  # inherited
    assert r.match("some-dir-2", True) is Exclusion.NONE


def test_no_slash_pattern_matches_file_too():
    assert ExcludeRules(freeze=["some-dir"]).match("a/some-dir", False) is Exclusion.FREEZE


def test_trailing_slash_is_dirs_only():
    r = ExcludeRules(hide=["x/"])
    assert r.match("x", False) is Exclusion.NONE
    assert r.match("x", True) is Exclusion.HIDE


def test_anchored_pattern():
    r = ExcludeRules(freeze=["/top"])
    assert r.match("top", True) is Exclusion.FREEZE
    assert r.match("a/top", True) is Exclusion.NONE


def test_hide_beats_freeze():
    assert ExcludeRules(freeze=["a"], hide=["a"]).match("a", True) is Exclusion.HIDE


def test_hide_inherited_beats_freeze_on_child():
    r = ExcludeRules(freeze=["a/b"], hide=["a"])
    assert r.match("a/b", True) is Exclusion.HIDE


def test_target_itself_never_excluded():
    assert ExcludeRules(hide=["*"]).match("", True) is Exclusion.NONE


def test_glob_and_negation():
    r = ExcludeRules(freeze=["*.log", "!keep.log"])
    assert r.match("x/a.log", False) is Exclusion.FREEZE
    assert r.match("x/keep.log", False) is Exclusion.NONE


def test_empty_rules_match_nothing():
    assert ExcludeRules().match("a/b", True) is Exclusion.NONE


def test_dict_round_trip():
    r = ExcludeRules(freeze=["a/"], hide=["b"])
    d = r.to_dict()
    assert d == {"freeze": ["a/"], "hide": ["b"]}
    r2 = ExcludeRules.from_dict(d)
    assert r2.to_dict() == d
    assert r2.match("b", False) is Exclusion.HIDE
    assert ExcludeRules.from_dict({}).to_dict() == {"freeze": [], "hide": []}
