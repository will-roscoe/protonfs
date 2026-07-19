"""Unit tests for the docs command-xref extension (pure functions, no Sphinx build)."""
from __future__ import annotations

import sys
from pathlib import Path

# The extension lives in docs/_ext (not importable as a package); add it to the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docs" / "_ext"))

import command_xref as cx  # noqa: E402


def test_target_map_has_leaf_and_group_phrases():
    m = cx.build_target_map()
    assert m["push"] == "cmd-push"
    assert m["trash list"] == "cmd-trash-list"
    assert m["config get"] == "cmd-config-get"
    # every mapped label follows the cmd-<dashed-phrase> convention
    for phrase, label in m.items():
        assert label == "cmd-" + phrase.replace(" ", "-")


def test_spans_match_flags_between_program_and_subcommand():
    m = cx.build_target_map()
    spans = cx.find_command_spans("run protonfs --dry-run push now", m)
    assert len(spans) == 1
    start, end, label = spans[0]
    assert label == "cmd-push"
    assert "protonfs --dry-run push" == "run protonfs --dry-run push now"[start:end]


def test_spans_prefer_longest_phrase():
    m = cx.build_target_map()
    spans = cx.find_command_spans("use protonfs trash list here", m)
    assert [s[2] for s in spans] == ["cmd-trash-list"]


def test_spans_ignore_bare_program():
    m = cx.build_target_map()
    assert cx.find_command_spans("just protonfs alone", m) == []
