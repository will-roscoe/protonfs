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


class TickingClock(FakeClock):
    """A :class:`FakeClock` that also moves ``tick`` seconds on every read.

    Real time passes between Drive calls, so measurements made later in a run carry later
    times; a clock that never moves on its own hides ordering bugs (a trash pass's own
    listings look exactly as old as the pass).
    """

    def __init__(self, start: float = 1_800_000_000.0, tick: float = 0.01) -> None:
        super().__init__(start)
        self.tick = tick

    def __call__(self) -> float:
        self.now += self.tick
        return self.now


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def ticking_clock() -> TickingClock:
    return TickingClock()


@pytest.fixture()
def usage_cache(tmp_path):
    c = UsageCache(tmp_path / "du.sqlite")
    yield c
    c.close()
