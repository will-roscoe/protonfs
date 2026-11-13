from __future__ import annotations

import pytest

from protonfs.usage.policy import RefreshPolicy, parse_size


def test_refresh_policy_parse() -> None:
    assert RefreshPolicy.parse("5d").threshold == 432000
    assert RefreshPolicy.parse("0") == RefreshPolicy(0.0, allow_shortcut=False)
    assert RefreshPolicy.parse("never").never
    assert RefreshPolicy.revalidate() == RefreshPolicy(0.0, allow_shortcut=True)
    assert RefreshPolicy.parse("0").fresh_only


def test_is_stale() -> None:
    p = RefreshPolicy(60.0)
    assert p.is_stale(None, 1000.0)
    assert not p.is_stale(950.0, 1000.0)
    assert p.is_stale(939.0, 1000.0)
    assert not RefreshPolicy(None).is_stale(None, 1000.0)
    assert RefreshPolicy(0.0).is_stale(1000.0, 1000.0)


@pytest.mark.parametrize("text,n", [("3TB", 3 * 10**12), ("500GB", 500 * 10**9),
                                    ("2TiB", 2 * 2**40), ("512GiB", 512 * 2**30),
                                    ("1024", 1024)])
def test_parse_size(text: str, n: int) -> None:
    assert parse_size(text) == n


@pytest.mark.parametrize("bad", ["", "GB", "5XB", "-3GB"])
def test_parse_size_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_size(bad)
