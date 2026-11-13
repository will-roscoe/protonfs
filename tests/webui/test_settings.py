"""GUI settings seeded from ``du --gui`` flags and edited in the page (3.4.0)."""

from __future__ import annotations

import pytest

from protonfs.usage.excludes import ExcludeRules
from protonfs.usage.policy import RefreshPolicy
from protonfs.webui.settings import GuiSettings


def test_defaults() -> None:
    s = GuiSettings(target="/my-files")
    assert (s.refresh, s.maxdepth, s.dirs_only, s.freeze, s.hide) == ("5d", 3, False, [], [])


def test_roundtrip_and_derived_objects() -> None:
    s = GuiSettings("/my-files/x", refresh="2h", maxdepth=5, dirs_only=True,
                    freeze=["sim/"], hide=["tmp"])
    again = GuiSettings.from_dict(s.to_dict())
    assert again == s
    assert s.policy() == RefreshPolicy.parse("2h")
    assert s.rules().to_dict() == ExcludeRules(freeze=["sim/"], hide=["tmp"]).to_dict()


def test_update_merges_and_validates() -> None:
    s = GuiSettings("/my-files")
    s2 = s.updated({"maxdepth": 6, "refresh": "never", "freeze": ["a", "b/"]})
    assert (s2.maxdepth, s2.refresh, s2.freeze, s2.target) == (6, "never", ["a", "b/"], "/my-files")
    assert s.maxdepth == 3  # original untouched


@pytest.mark.parametrize(
    "patch",
    [{"maxdepth": 0}, {"maxdepth": 7}, {"maxdepth": "3"}, {"refresh": "5"},
     {"freeze": "a"}, {"hide": [1]}, {"dirs_only": "yes"}, {"target": "/x"}, {"bogus": 1}],
)
def test_update_rejects_bad_values(patch: dict) -> None:
    with pytest.raises(ValueError):
        GuiSettings("/my-files").updated(patch)
