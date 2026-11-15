"""The browser fixture must not leak the Playwright driver when it skips."""

from __future__ import annotations

import types

import pytest

from .conftest import start_chromium


class _FakePw:
    def __init__(self, exe):
        self.stopped = False
        self.chromium = types.SimpleNamespace(executable_path=exe)

    def start(self):
        return self

    def stop(self):
        self.stopped = True


def test_missing_chromium_stops_the_driver_before_skipping(tmp_path):
    fake = _FakePw(str(tmp_path / "no-such-chrome"))
    api = types.SimpleNamespace(sync_playwright=lambda: fake)
    with pytest.raises(pytest.skip.Exception):
        start_chromium(api)
    assert fake.stopped
