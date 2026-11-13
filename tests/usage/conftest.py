"""Shared fixtures for the ``protonfs.usage`` tests."""

from __future__ import annotations

import pytest

from protonfs.usage.cache import UsageCache


class FakeClock:
    """A callable returning a fake Unix time that only moves when :meth:`advance` is called."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def usage_cache(tmp_path):
    c = UsageCache(tmp_path / "du.sqlite")
    yield c
    c.close()
