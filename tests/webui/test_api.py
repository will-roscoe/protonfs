"""Tests for the GUI's Api and JobQueue (no sockets)."""

from __future__ import annotations

import threading

import pytest
from tests.usage.conftest import FakeClock
from tests.usage.treedrive import TreeDrive

from protonfs.usage.cache import UsageCache
from protonfs.usage.engine import UsageEngine
from protonfs.usage.policy import RefreshPolicy
from protonfs.webui.api import Api
from protonfs.webui.jobs import JobQueue
from protonfs.webui.settings import GuiSettings

TARGET = "/my-files"


@pytest.fixture()
def drive() -> TreeDrive:
    d = TreeDrive()
    d.add_file("/my-files/A/f1", 100)
    d.add_file("/my-files/A/sub/x", 1000)
    d.add_file("/my-files/B/big", 5000)
    return d


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def engine(drive, clock, tmp_path):
    cache = UsageCache(tmp_path / "du.sqlite")
    yield UsageEngine(drive, cache, clock=clock)
    cache.close()


@pytest.fixture()
def make(engine):
    made = []

    def build(**kw):
        holder = {}
        jobs = JobQueue(engine, lambda: holder["api"].settings)
        holder["api"] = api = Api(engine, jobs, GuiSettings(TARGET, **kw))
        made.append(jobs)
        return api

    yield build
    for jobs in made:
        jobs.close()


def _next(q, name, timeout=5):
    import queue

    while True:
        try:
            ev = q.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no {name} event") from None
        if ev and ev["event"] == name:
            return ev


def test_tree_makes_no_drive_calls_and_queues_stale(make, drive):
    api = make()
    q = api.jobs.subscribe()
    out = api.tree(TARGET, 2)
    assert drive.calls == []
    assert out["rows"][0]["path"] == TARGET
    assert out["pending"] == [TARGET]  # nothing cached: the target is queued
    ev = _next(q, "breakdown")
    assert ev["path"] == TARGET and ev["breakdown"].size == 6100
    again = api.tree(TARGET, 2)
    assert {"A", "B"} <= {r["name"] for r in again["rows"]}
    bd = again["breakdowns"][TARGET]
    assert bd["state"] == "listed" and bd["entries"][0]["state"] in ("sized", "listed")
    assert isinstance(bd["entries"][0]["exclusion"], str)


def test_tree_under_never_queues_nothing(make, drive):
    api = make(refresh="never")
    out = api.tree(TARGET, 1)
    assert out["pending"] == [] and drive.calls == []


def test_tree_requests_do_not_requeue_every_time(make):
    api = make(refresh="0")  # always stale: must still not loop
    api.tree(TARGET, 1)
    q = api.jobs.subscribe()
    _next(q, "breakdown")
    api.tree(TARGET, 1)
    assert api.jobs.pending() == []


def test_refresh_forces_fresh(make, drive, engine):
    api = make()
    api.tree(TARGET, 1)
    q = api.jobs.subscribe()
    _next(q, "breakdown")
    n = len(drive.calls)
    assert api.refresh(f"{TARGET}/A")["queued"] is True
    _next(q, "breakdown")
    assert len(drive.calls) > n
    # a repeat while nothing runs is accepted again; a duplicate in flight is not
    gate = threading.Event()
    drive.before("list", f"{TARGET}/B", lambda: gate.wait(5))
    assert api.refresh(f"{TARGET}/B")["queued"] is True
    assert api.refresh(f"{TARGET}/B")["queued"] is False
    gate.set()


def test_path_outside_target_is_value_error(make):
    api = make()
    for call in (lambda: api.tree("/devices", 1), lambda: api.refresh("/elsewhere"),
                 lambda: api.walk(["/devices"])):
        with pytest.raises(ValueError):
            call()
    with pytest.raises(ValueError):
        api.tree(TARGET, 0)


def test_settings_roundtrip_and_validation(make):
    api = make()
    out = api.set_settings({"maxdepth": 5, "freeze": ["A/"], "target": TARGET})
    assert out["maxdepth"] == 5 and api.get_settings() == out
    assert api.settings.rules().to_dict()["freeze"] == ["A/"]
    with pytest.raises(ValueError):
        api.set_settings({"maxdepth": 99})
    with pytest.raises(ValueError):
        api.set_settings({"target": "/devices"})
    assert api.get_settings()["maxdepth"] == 5  # unchanged by the failures


def test_job_reads_live_settings(make):
    api = make()
    api.set_settings({"hide": ["B/"]})
    q = api.jobs.subscribe()
    api.jobs.submit("breakdown", TARGET, RefreshPolicy.parse("0"))
    ev = _next(q, "breakdown")
    assert [e.name for e in ev["breakdown"].entries] == ["A"]


def test_job_error_is_published_and_worker_survives(make):
    api = make()
    q = api.jobs.subscribe()
    # a path outside the target makes the engine raise ValueError inside the job
    api.jobs.submit("breakdown", "/devices", RefreshPolicy.parse("0"))
    ev = _next(q, "error")
    assert ev["path"] == "/devices" and ev["message"]
    api.jobs.submit("breakdown", TARGET, RefreshPolicy.parse("0"))
    _next(q, "breakdown")


def test_walk_progress_and_second_walk_refused(make, drive):
    api = make()
    gate = threading.Event()
    drive.before("list", f"{TARGET}/A", lambda: gate.wait(5))
    q = api.jobs.subscribe()
    assert api.walk([TARGET])["started"] is True
    assert api.walk([TARGET])["started"] is False
    gate.set()
    last = None
    while True:
        ev = _next(q, "progress")
        last = ev
        if ev["state"] != "running":
            break
    assert last["state"] == "done" and last["done"] >= 3 and last["total"] == last["done"]


def test_walk_cancel(make, drive):
    api = make()
    gate = threading.Event()
    drive.before("list", TARGET, lambda: gate.wait(5))
    q = api.jobs.subscribe()
    assert api.walk([TARGET])["started"] is True
    api.cancel_walk()
    gate.set()
    while True:
        ev = _next(q, "progress")
        if ev["state"] != "running":
            break
    assert ev["state"] == "cancelled"


def test_events_are_json_able_and_end_on_close(make):
    import json

    api = make()
    gen = api.events(timeout=0.05)
    assert next(gen) == {"event": "keepalive", "data": {}}
    api.jobs.submit("breakdown", TARGET, RefreshPolicy.parse("0"))
    while True:
        ev = next(gen)
        if ev["event"] == "breakdown":
            break
    json.dumps(ev)
    assert ev["data"]["breakdown"]["state"] == "listed"
    api.jobs.close()
    assert list(gen) == []
