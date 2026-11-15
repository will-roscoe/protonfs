"""``-d`` depth parsing for ``protonfs du`` (3.4.0)."""

from __future__ import annotations

import click
import pytest

from protonfs.commands.du import parse_depth


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2", (0, 2)),
        ("0", (0, 0)),
        ("1-3", (1, 3)),
        ("2-", (2, None)),
        ("-2", (0, 2)),
        (" 1-3 ", (1, 3)),
    ],
)
def test_parse_depth_forms(text: str, expected: tuple[int, int | None]) -> None:
    assert parse_depth(text) == expected


@pytest.mark.parametrize("bad", ["", "-", "a", "3-1", "1-2-3", "-1-2", "1.5", "+2"])
def test_parse_depth_rejects(bad: str) -> None:
    with pytest.raises(click.BadParameter):
        parse_depth(bad)
