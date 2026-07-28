from __future__ import annotations

import pytest

from protonfs import credstore as cs
from protonfs import secretservice as ss


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ss.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def test_managed_paths_live_under_state_dir(home):
    assert cs.gnupg_home() == ss.state_dir() / "gnupg"
    assert cs.password_store_dir() == ss.state_dir() / "password-store"
    assert cs.store_choice_file() == ss.state_dir() / "credentials-store"


def test_store_choice_round_trips(home):
    assert cs.read_store_choice() is None
    cs.write_store_choice(cs.PASS)
    assert cs.read_store_choice() == "pass"


def test_store_choice_rejects_garbage(home):
    cs.store_choice_file().parent.mkdir(parents=True, exist_ok=True)
    cs.store_choice_file().write_text("nonsense\n")
    assert cs.read_store_choice() is None  # unknown value is treated as unset
