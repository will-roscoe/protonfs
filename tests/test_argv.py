# tests/test_argv.py
"""Position-independent flag reordering (protonfs.argv)."""
from __future__ import annotations

from protonfs.argv import is_global_flag, reorder_argv

_COMMANDS = frozenset({"push", "pull", "status", "config", "trash"})


def _reorder(args):
    return reorder_argv(args, _COMMANDS)


def test_is_global_flag_recognizes_stacked_verbose_and_long_flags() -> None:
    assert is_global_flag("-v") and is_global_flag("-vvvv")
    assert is_global_flag("--event-log") and is_global_flag("--progress-lines")
    assert not is_global_flag("-vx")  # not all v's
    assert not is_global_flag("--dry-run")
    assert not is_global_flag("push")


def test_canonical_order_passes_through_unchanged() -> None:
    assert _reorder(["-v", "push", "mload001"]) == ["-v", "push", "mload001"]


def test_global_flag_after_subcommand_is_hoisted() -> None:
    assert _reorder(["push", "-v", "mload001"]) == ["-v", "push", "mload001"]


def test_subcommand_flag_before_subcommand_moves_after_it() -> None:
    assert _reorder(["--dry-run", "push", "a", "b"]) == ["push", "--dry-run", "a", "b"]


def test_global_flag_interspersed_with_args() -> None:
    assert _reorder(["push", "a", "--event-log", "b"]) == ["--event-log", "push", "a", "b"]


def test_global_before_a_subgroup_command_chain() -> None:
    assert _reorder(["config", "set", "k", "v", "-v"]) == ["-v", "config", "set", "k", "v"]


def test_value_option_before_subcommand_keeps_its_value_adjacent() -> None:
    # --resolve's value ("replace") is not a command name, so the first command name
    # (push) still anchors correctly and the value stays next to its option.
    assert _reorder(["--resolve", "replace", "push", "a"]) == [
        "push",
        "--resolve",
        "replace",
        "a",
    ]


def test_no_subcommand_is_left_unchanged() -> None:
    assert _reorder(["--help"]) == ["--help"]
    assert _reorder(["-v"]) == ["-v"]
    assert _reorder([]) == []


def test_multiple_globals_all_hoist_preserving_order() -> None:
    assert _reorder(["push", "-vv", "a", "--event-log"]) == ["-vv", "--event-log", "push", "a"]
