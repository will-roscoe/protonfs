"""Settle window and retention policy (#158)."""
from __future__ import annotations

import pytest

from protonfs.retention import (
    RetentionError,
    format_age,
    is_settled,
    parse_duration,
    select_prune_candidates,
)

DAY = 86400.0
NOW = 1_000 * DAY


@pytest.mark.parametrize(
    "text,seconds",
    [("0", 0), ("90s", 90), ("30m", 1800), ("12h", 43200), ("1d", DAY), ("1.5d", 1.5 * DAY),
     (" 2h ", 7200)],
)
def test_parse_duration(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "12", "1 week", "-1d", "1w", "d"])
def test_parse_duration_refuses_what_it_would_have_to_guess(text: str) -> None:
    with pytest.raises(RetentionError):
        parse_duration(text)


def test_format_age() -> None:
    assert format_age(30) == "30s"
    assert format_age(90) == "1.5m"
    assert format_age(7200) == "2h"
    assert format_age(12 * DAY) == "12d"


def test_is_settled_is_inclusive_at_the_boundary() -> None:
    assert is_settled(NOW - DAY, DAY, NOW)
    assert not is_settled(NOW - DAY + 1, DAY, NOW)


def _ages(**days_old: float) -> dict[str, float]:
    return {rel.replace("__", "/"): NOW - d * DAY for rel, d in days_old.items()}


def test_prune_keeps_the_newest_per_directory_and_needs_both_rules() -> None:
    files = _ages(
        run1__d1=10, run1__d2=9, run1__d3=8, run1__d4=0.1,  # d4 newest but fresh
        run2__d1=10, run2__d2=0.5,
    )

    candidates = select_prune_candidates(files, keep=2, min_age=DAY, now=NOW)

    # run1: newest two by mtime are d4 and d3 -> kept; d1, d2 old -> released.
    # run2: both within keep=2 -> kept, however old d1 is.
    assert candidates == ["run1/d1", "run1/d2"]


def test_a_fresh_file_outside_the_newest_n_is_still_kept() -> None:
    files = _ages(run__a=0.2, run__b=0.1, run__c=0.05)

    assert select_prune_candidates(files, keep=1, min_age=DAY, now=NOW) == []


def test_keep_zero_and_no_settle_window_release_everything_old_enough() -> None:
    files = _ages(a=2, b=0)

    assert select_prune_candidates(files, keep=0, min_age=0, now=NOW) == ["a", "b"]
    assert select_prune_candidates(files, keep=0, min_age=DAY, now=NOW) == ["a"]


def test_ties_are_broken_by_path_so_the_choice_is_stable() -> None:
    files = {"run/b": NOW - 5 * DAY, "run/a": NOW - 5 * DAY}

    assert select_prune_candidates(files, keep=1, min_age=DAY, now=NOW) == ["run/b"]


def test_negative_settings_are_refused() -> None:
    with pytest.raises(RetentionError):
        select_prune_candidates({}, keep=-1, min_age=0, now=NOW)
    with pytest.raises(RetentionError):
        select_prune_candidates({}, keep=0, min_age=-1, now=NOW)
