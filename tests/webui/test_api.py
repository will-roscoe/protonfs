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


def _settle(api, q, name="breakdown"):
    """Wait for one ``name`` event; afterwards the job has left ``pending()``."""
    _next(q, name)
    assert api.jobs.pending() == []


def test_revisit_after_threshold_queues_again(make, clock):
    api = make(refresh="5m")
    q = api.jobs.subscribe()
    api.tree(TARGET, 1)
    _settle(api, q)
    assert api.tree(TARGET, 1)["pending"] == []  # fresh: nothing to do
    clock.advance(301)
    assert api.tree(TARGET, 1)["pending"] == [TARGET]  # stale again, queued again
    _settle(api, q)


def test_queue_false_never_queues(make):
    api = make(refresh="0")
    out = api.tree(TARGET, 1, queue=False)
    assert out["pending"] == [] and api.root(queue=False)["pending"] == []
    assert api.tree(TARGET, 1)["pending"] == [TARGET]


def test_refresh_zero_queues_on_every_navigation(make):
    api = make(refresh="0")
    q = api.jobs.subscribe()
    for _ in range(3):
        assert api.tree(TARGET, 1)["pending"] == [TARGET]
        _settle(api, q)


def test_drilling_down_keeps_target_relative_rules(make):
    api = make()
    api.set_settings({"hide": ["/A/sub"]})  # anchored: only target/A/sub
    q = api.jobs.subscribe()
    api.tree(f"{TARGET}/A", 2)
    _settle(api, q)
    out = api.tree(f"{TARGET}/A", 2, queue=False)
    names = {r["name"] for r in out["rows"]}
    assert "f1" in names and "sub" not in names
    # relative to the drilled-into folder the same pattern would match nothing
    api.set_settings({"hide": ["/sub"]})
    names = {r["name"] for r in api.tree(f"{TARGET}/A", 2, queue=False)["rows"]}
    assert "sub" in names


def test_engine_base_must_contain_target(engine):
    from protonfs.usage.excludes import ExcludeRules
    from protonfs.usage.report import collect_rows

    never = RefreshPolicy.parse("never")
    with pytest.raises(ValueError):
        engine.tree(f"{TARGET}/A", never, ExcludeRules(), maxdepth=1, base=f"{TARGET}/B")
    with pytest.raises(ValueError):
        collect_rows(engine, f"{TARGET}/A", never, ExcludeRules(), now=0.0, base="/devices")


def test_refresh_forces_fresh(make, drive, engine):
    api = make()
    q = api.jobs.subscribe()
    api.tree(TARGET, 1)
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
    with pytest.raises(ValueError):
        api.set_settings("nope")
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


@pytest.mark.parametrize(
    "patch",
    [{"refresh": 5}, {"refresh": None}, {"hide": "A"}, {"hide": None}, {"freeze": [1]},
     {"maxdepth": 0}, {"maxdepth": 7}, {"maxdepth": None}, {"dirs_only": 1},
     {"hide": ["a/**/[z-a]"]}],
)
def test_bad_settings_are_rejected_and_never_stored(make, patch):
    api = make()
    before = api.get_settings()
    with pytest.raises(ValueError):
        api.set_settings(patch)
    assert api.get_settings() == before
    api.tree(TARGET, 1, queue=False)  # still usable


def test_refused_walk_does_not_break_cancel(make, drive):
    api = make()
    gate = threading.Event()
    drive.before("list", f"{TARGET}/A", lambda: gate.wait(5))
    q = api.jobs.subscribe()
    assert api.walk([TARGET])["started"] is True
    assert api.walk([TARGET])["started"] is False
    api.cancel_walk()
    gate.set()
    while True:
        ev = _next(q, "progress")
        if ev["state"] != "running":
            break
    assert ev["state"] == "cancelled"


def test_walk_that_raises_ends_in_error_with_last_known_counts(make, engine, monkeypatch):
    api = make()

    def boom(path, policy, rules, *, target, on_progress, cancel):
        on_progress(2, 5)
        raise RuntimeError("kaput")

    monkeypatch.setattr(engine, "walk", boom)
    q = api.jobs.subscribe()
    api.walk([TARGET])
    err = _next(q, "error")
    assert err["message"] == "kaput"
    while True:
        ev = _next(q, "progress")
        if ev["state"] != "running":
            break
    assert (ev["state"], ev["done"], ev["total"], ev["message"]) == ("error", 2, 5, "kaput")


def test_cancelled_walk_keeps_last_known_total(make, engine, monkeypatch):
    api = make()

    def slow(path, policy, rules, *, target, on_progress, cancel):
        on_progress(1, 4)
        cancel.set()
        return 1

    monkeypatch.setattr(engine, "walk", slow)
    q = api.jobs.subscribe()
    api.walk([TARGET])
    while True:
        ev = _next(q, "progress")
        if ev["state"] != "running":
            break
    assert (ev["state"], ev["done"], ev["total"]) == ("cancelled", 1, 4)


def test_events_subscribe_eagerly(make):
    api = make()
    events = api.events(timeout=5)  # no next() yet
    api.jobs.submit("breakdown", TARGET, RefreshPolicy.parse("0"))
    ev = next(events)
    assert ev["event"] == "breakdown"
    events.close()
    assert api.jobs._subs == []


def test_slow_subscriber_is_dropped(make):
    from protonfs.webui import jobs as jobs_mod

    api = make()
    q = api.jobs.subscribe()
    for i in range(jobs_mod.MAX_BACKLOG + 5):
        api.jobs._publish({"event": "error", "path": str(i), "message": "x"})
    assert q not in api.jobs._subs
    assert q.get_nowait() is None  # backlog discarded, stream ends
