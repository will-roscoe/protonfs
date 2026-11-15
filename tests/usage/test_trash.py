"""Tests for the trash pass and per-subtree trash figures."""

from __future__ import annotations

import subprocess

import pytest
from tests.usage.treedrive import TreeDrive

from protonfs.drive import DriveError
from protonfs.usage.engine import UsageEngine
from protonfs.usage.excludes import ExcludeRules
from protonfs.usage.policy import RefreshPolicy
from protonfs.usage.trash import (
    EXCLUDED_REASON,
    FROZEN_OLDER_REASON,
    TrashGroup,
    TrashSummary,
    figures_for,
    run_trash_pass,
)

P = "/my-files/p"
REVAL = RefreshPolicy.revalidate()
NO_RULES = ExcludeRules()


def _probe() -> TreeDrive:
    d = TreeDrive()
    d.add_folder(P)
    d.add_file(P + "/a", 1048723)
    d.add_file(P + "/c", 524403)
    d.add_folder(P + "/sub")
    d.add_file(P + "/sub/b", 2097364)
    return d


def _trashed(usage_cache, clock):
    d = _probe()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(P, REVAL, NO_RULES, target=P)
    d.trash(P + "/a")
    d.trash(P + "/sub")
    return d, eng


def test_trash_pass_measures_trashed_folder_via_parent_gap(usage_cache, clock):
    d, eng = _trashed(usage_cache, clock)

    summary = run_trash_pass(eng, REVAL)

    assert summary.total_known == 1048723 + 2097364
    assert summary.complete and summary.partial is None
    assert TrashGroup(P, 1, 0, 1048723) in summary.groups
    assert TrashGroup(P, 0, 1, 2097364) in summary.groups
    assert summary.at == clock.now


def test_trash_pass_groups_several_folders_of_one_parent(usage_cache, clock):
    d = _probe()
    d.add_file(P + "/sub2/b2", 1000)
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(P, REVAL, NO_RULES, target=P)
    d.trash(P + "/sub")
    d.trash(P + "/sub2")

    summary = run_trash_pass(eng, REVAL)

    assert summary.groups == (TrashGroup(P, 0, 2, 2097364 + 1000),)


def test_trashed_empty_folder_is_zero_bytes_not_unknown(usage_cache, clock):
    """I1: a zero remainder from one consistent pass means the trashed folders hold 0 B."""
    d = _probe()
    d.add_folder(P + "/empty")
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(P, REVAL, NO_RULES, target=P)
    d.trash(P + "/empty")

    summary = run_trash_pass(eng, REVAL)

    assert summary.groups == (TrashGroup(P, 0, 1, 0),)
    assert summary.complete and summary.total_known == 0


def test_zero_remainder_over_mixed_passes_stays_unknown(usage_cache, clock):
    """A zero remainder only proves 0 B when every input comes from the one pass."""
    d = _probe()
    d.add_folder(P + "/empty")
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(P, REVAL, NO_RULES, target=P)
    d.trash(P + "/empty")
    # sub is frozen, so the pass reuses its figure from the earlier pass
    summary = run_trash_pass(eng, REVAL, rules=ExcludeRules(freeze=["sub"]))

    (group,) = summary.groups
    # R41: the freeze alone makes it unknown, and the reason says so
    assert group.bytes is None and group.reason == FROZEN_OLDER_REASON
    assert not summary.complete


@pytest.mark.parametrize("rules", [ExcludeRules(freeze=["my-files/p"]),
                                   ExcludeRules(hide=["p"]),
                                   ExcludeRules(freeze=["/my-files"])])
def test_trash_pass_makes_no_calls_under_an_excluded_parent(usage_cache, clock, rules):
    """I3: -x/-X mean no Drive calls for a match or below, for the trash pass too."""
    d, eng = _trashed(usage_cache, clock)
    d.calls.clear()

    summary = run_trash_pass(eng, REVAL, rules=rules)

    assert d.calls == [("list_trash", "/trash")]
    (group,) = [g for g in summary.groups if g.folders]
    assert group.bytes is None and group.reason == EXCLUDED_REASON
    assert TrashGroup(P, 1, 0, 1048723) in summary.groups  # trashed files need no call
    assert not summary.complete


def test_trash_pass_unresolved_parent_counts_folder(usage_cache, clock):
    d = _probe()
    d.trash(P + "/sub")
    eng = UsageEngine(d, usage_cache, clock=clock)

    summary = run_trash_pass(eng, REVAL)

    assert summary.unresolved_folders == 1
    assert not summary.complete
    assert summary.total_known == 0


def test_trash_listing_timeout_is_partial(usage_cache, clock):
    d, eng = _trashed(usage_cache, clock)
    d.fail_next("list_trash", "/trash", subprocess.TimeoutExpired("x", 300))

    summary = run_trash_pass(eng, REVAL)

    assert summary.partial == "trash listing timed out after 300 s"
    assert not summary.complete


def test_trash_pass_lists_with_the_configured_timeout(usage_cache, clock, monkeypatch):
    """The timeout in effect is PROTONFS_TRASH_LIST_TIMEOUT (#235), recorded on a timeout."""
    from protonfs.drive import DriveTrashListTimeoutError

    monkeypatch.setattr("protonfs.drive.TRASH_LIST_TIMEOUT_SECONDS", 123.0)
    d, eng = _trashed(usage_cache, clock)
    seen = []
    real = d.list_trash

    def recording(*, timeout=None):
        seen.append(timeout)
        return real(timeout=timeout)

    d.list_trash = recording
    assert run_trash_pass(eng, REVAL).timed_out_after is None
    assert seen == [123.0]
    d.fail_next("list_trash", "/trash", DriveTrashListTimeoutError("slow"))
    assert run_trash_pass(eng, REVAL).timed_out_after == 123.0
    d.fail_next("list_trash", "/trash", DriveError("down"))
    assert run_trash_pass(eng, REVAL).timed_out_after is None


def test_trash_list_timeout_error_is_partial_with_its_advice(usage_cache, clock):
    from protonfs.drive import DriveTrashListTimeoutError

    d, eng = _trashed(usage_cache, clock)
    d.fail_next("list_trash", "/trash", DriveTrashListTimeoutError(
        "listing /trash timed out after 900s; ... Raise PROTONFS_TRASH_LIST_TIMEOUT"))

    summary = run_trash_pass(eng, REVAL)

    assert summary.partial is not None and not summary.complete
    assert "timed out after 900" in summary.partial
    assert "PROTONFS_TRASH_LIST_TIMEOUT" in summary.partial


def test_trash_listing_error_is_partial(usage_cache, clock):
    d, eng = _trashed(usage_cache, clock)
    d.fail_next("list_trash", "/trash", DriveError("boom"))

    summary = run_trash_pass(eng, REVAL)

    assert summary.partial == "boom" and not summary.complete


def test_gap_not_in_trash_leaves_folder_group_unmeasured(usage_cache, clock):
    d = _probe()
    d.add_file(P + "/d/x", 10)
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(P, REVAL, NO_RULES, target=P)
    d.trash(P + "/a")
    d.trash(P + "/sub")
    # A failed child size leaves a child without a figure, so the remainder is unattributed.
    d.fail_next("size", P + "/d", DriveError("nope"))

    summary = run_trash_pass(eng, REVAL)

    (group,) = [g for g in summary.groups if g.folders]
    assert group.bytes is None and "no size" in (group.reason or "")
    assert not summary.complete
    assert summary.total_known == 1048723


def test_summary_json_round_trip():
    s = TrashSummary(5.0, (TrashGroup(P, 1, 0, 7), TrashGroup(None, 0, 2, None, "why", "u3")),
                     3, "x", ("uid-9",), 6.0)
    back = TrashSummary.from_json(s.to_json())
    assert back == s and [g.parent_uid for g in back.groups] == [None, "u3"]


def test_trash_pass_records_the_unresolved_parent_uids(usage_cache, clock):
    d = _probe()
    d.trash(P + "/sub")
    eng = UsageEngine(d, usage_cache, clock=clock)
    summary = run_trash_pass(eng, REVAL)
    assert summary.unresolved_folders == 1
    assert summary.unresolved_parents == (d.list_trash()[0].parent_uid,)


def test_summary_stored_before_unresolved_parents_has_them_unknown():
    old = '{"at": 5.0, "groups": [], "unresolved_folders": 1, "partial": null}'
    assert TrashSummary.from_json(old).unresolved_parents is None


def test_summary_json_without_reasons_still_loads():
    old = ('{"at": 5.0, "groups": [["/my-files/p", 0, 1, null]], '
           '"unresolved_folders": 0, "partial": null}')
    (group,) = TrashSummary.from_json(old).groups
    assert group == TrashGroup(P, 0, 1, None) and group.reason is None


def test_trash_for_with_unresolved_folders_has_unknown_unchecked(usage_cache, clock):
    """An unresolved trashed folder could be anywhere, so no unchecked bound is claimed."""
    d = TreeDrive()
    d.add_file("/my-files/a/f", 100)
    d.add_file("/my-files/a/g", 200)
    d.add_file("/my-files/b/big", 190_000)
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown("/my-files/a", REVAL, NO_RULES, target="/my-files/a")
    eng.breakdown("/my-files", REVAL, NO_RULES, target="/my-files")
    # re-measure a after the root pass so b is sized-only; then trash g
    d.trash("/my-files/a/g")
    usage_cache.put_trash(
        TrashSummary(clock.now, (TrashGroup("/my-files/a", 1, 0, 200),), 1, None).to_json(),
        clock.now,
    )
    snap = usage_cache.snapshot("/my-files")
    assert snap is not None
    # drop b's own listing: it was never broken down
    assert usage_cache.snapshot("/my-files/b") is None

    fig = eng.trash_for("/my-files")

    assert fig.known == 200
    assert not fig.complete
    assert fig.unchecked_bytes is None and fig.unchecked_share is None
    assert fig.at == clock.now
    assert eng.trash_for("/my-files/a").known == 200
    assert eng.trash_for("/my-files/b").known == 0


ZERO = RefreshPolicy.parse("0")


def _passed(usage_cache, clock):
    """P listed and an empty trash pass stored at the current time; returns (drive, engine)."""
    d = _probe()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(P, REVAL, NO_RULES, target=P)
    eng.refresh_trash(REVAL, NO_RULES)
    assert eng.trash_summary().complete and eng.trash_summary().total_known == 0
    return d, eng


def test_listing_newer_than_the_pass_replaces_its_figure_for_that_folder(usage_cache, clock):
    """C1/R29: trash found by a later listing shows; the pass's figure for that folder is
    replaced, never added to."""
    d = _probe()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(P, REVAL, NO_RULES, target=P)
    d.trash(P + "/a")
    eng.refresh_trash(REVAL, NO_RULES)
    pass_at = clock.now
    assert eng.trash_for(P).known == 1048723
    clock.advance(60)
    d.trash(P + "/sub")
    eng.breakdown(P, ZERO, NO_RULES, target=P)  # a newer listing: remainder a + sub

    fig = eng.trash_for(P)

    assert fig.known == 1048723 + 2097364 and fig.complete
    assert fig.at == clock.now  # every folder under P was listed after the pass
    top = eng.trash_for("/my-files")
    assert top.known == 1048723 + 2097364 and top.complete
    assert top.at == pass_at  # /my-files itself still rests on the pass
    assert eng.trash_for("/").known == 1048723 + 2097364


def test_unattributable_newer_listing_makes_the_figure_incomplete(usage_cache, clock):
    d, eng = _passed(usage_cache, clock)
    d.add_file(P + "/d/x", 10)
    clock.advance(60)
    d.trash(P + "/a")
    d.fail_next("size", P + "/d", DriveError("throttled"))
    eng.breakdown(P, ZERO, NO_RULES, target=P)

    fig = eng.trash_for(P)

    assert not fig.complete and fig.known == 0
    # the bound is the size of the folder whose trash is unknown
    assert fig.unchecked_bytes == usage_cache.figure(P).size
    assert fig.unchecked_share == 1.0


def test_a_relisted_parent_covers_the_trash_of_a_child_trashed_since(usage_cache, clock):
    """No double count: once P is re-listed without sub, sub's own listing is not added."""
    d, eng = _passed(usage_cache, clock)
    clock.advance(60)
    d.trash(P + "/sub/b")
    eng.breakdown(P + "/sub", ZERO, NO_RULES, target=P + "/sub")
    assert eng.trash_for(P).known == 2097364
    clock.advance(60)
    d.trash(P + "/sub")
    eng.breakdown(P, ZERO, NO_RULES, target=P)  # sub's whole size is now P's remainder

    fig = eng.trash_for(P)

    assert fig.known == 2097364 and fig.complete
    paths = ["/", "/my-files", P, P + "/sub"]
    assert figures_for(eng, paths) == {p: eng.trash_for(p) for p in paths}


def test_the_pass_own_listings_never_override_its_verdict(usage_cache, ticking_clock):
    """N3/R39: with real time, the pass's breakdowns are later than its start; they must not
    count as listings made since the pass (a zero remainder over a frozen child's older
    figure stays unknown, as the pass said)."""
    from protonfs.usage.report import collect_rows

    d = TreeDrive()
    d.add_file(P + "/F/x", 1000)
    d.add_file(P + "/k", 10)
    d.add_folder(P + "/T")
    eng = UsageEngine(d, usage_cache, clock=ticking_clock)
    eng.breakdown(P, REVAL, NO_RULES, target=P)
    d.trash(P + "/T")
    ticking_clock.advance(60)
    rules = ExcludeRules(freeze=["my-files/p/F"])

    rows = collect_rows(eng, "/", RefreshPolicy.parse("5d"), rules, now=ticking_clock.now)

    (group,) = [g for g in eng.trash_summary().groups if g.folders]
    assert group.bytes is None
    assert rows[0].path == "/" and rows[0].in_trash_complete is False
    assert eng.trash_summary().ended_at > eng.trash_summary().at


def _loose(usage_cache, clock):
    """P/old (5000 B) trashed while nothing is cached, then a cold ``du /``."""
    from protonfs.usage.report import collect_rows

    d = TreeDrive()
    d.add_file(P + "/keep", 1000)
    d.add_file(P + "/old", 5000)
    d.trash(P + "/old")
    eng = UsageEngine(d, usage_cache, clock=clock)
    five = RefreshPolicy.parse("5d")
    eng.check_account(five)
    collect_rows(eng, "/", five, NO_RULES, now=clock.now)
    (group,) = eng.trash_summary().groups
    assert group.parent_path is None and group.parent_uid is not None
    assert eng.trash_for("/").known == 5000 and eng.trash_for("/").complete
    return d, eng, five


def test_a_loose_trashed_file_is_not_counted_twice_once_its_folder_is_listed(
        usage_cache, ticking_clock):
    """N1/R36: listing P after the pass puts the loose file in P's remainder; the pass's
    group then belongs to P, so R29 replaces it instead of adding it to /."""
    d, eng, five = _loose(usage_cache, ticking_clock)
    ticking_clock.advance(60)
    eng.breakdown(P, five, NO_RULES, target=P)

    fig = eng.trash_for("/")

    assert fig.known == 5000 and fig.complete
    assert eng.trash_for(P).known == 5000
    assert eng.trash_parent_now_cached()  # the GUI re-runs the pass, as du / does
    calls = d.calls.count(("list_trash", "/trash"))
    eng.refresh_trash(five, NO_RULES)
    assert d.calls.count(("list_trash", "/trash")) == calls + 1
    (group,) = eng.trash_summary().groups
    assert group.parent_path == P
    assert not eng.trash_parent_now_cached()


def test_an_unresolvable_loose_file_that_may_overlap_a_newer_remainder_is_a_lower_bound(
        usage_cache, ticking_clock):
    """N1/R36: the loose file's folder is trashed later and an ancestor re-listed; the
    ancestor's remainder may hold it, so / shows max(direct, loose), incomplete."""
    from protonfs.usage.report import collect_rows

    d = TreeDrive()
    d.add_file("/my-files/A/keep", 10)
    d.add_file("/my-files/A/G/F/old", 5000)
    d.trash("/my-files/A/G/F/old")
    eng = UsageEngine(d, usage_cache, clock=ticking_clock)
    five = RefreshPolicy.parse("5d")
    eng.check_account(five)
    collect_rows(eng, "/", five, NO_RULES, now=ticking_clock.now)
    ticking_clock.advance(60)
    d.trash("/my-files/A/G")
    eng.breakdown("/my-files/A", five, NO_RULES, target="/my-files/A")  # gap holds G

    fig = eng.trash_for("/")

    assert fig.known == 5000 and not fig.complete
    assert fig.unchecked_bytes is None
    assert any(path is None for path, _ in fig.unknown)


def test_complete_figure_reads_no_snapshots(usage_cache, clock):
    """M7: a complete figure needs no walk of the cached subtree."""
    d = TreeDrive()
    for i in range(30):
        d.add_file(f"/my-files/f{i}/x", 10)
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.walk("/my-files", REVAL, NO_RULES)
    eng.refresh_trash(REVAL, NO_RULES)
    reads = []
    real = usage_cache.snapshot
    usage_cache.snapshot = lambda p: reads.append(p) or real(p)

    fig = eng.trash_for("/my-files")

    assert fig.complete and fig.known == 0
    assert reads == []


def test_trash_for_without_summary_uses_cached_gaps(usage_cache, clock):
    d, eng = _trashed(usage_cache, clock)
    zero = RefreshPolicy.parse("0")
    eng.breakdown("/my-files", zero, NO_RULES, target="/my-files")
    eng.breakdown(P, zero, NO_RULES, target=P)

    fig = eng.trash_for("/my-files")

    assert fig.known == 1048723 + 2097364
    assert not fig.complete and fig.at is None
    assert eng.trash_summary() is None


def test_trash_for_complete_has_no_unchecked(usage_cache, clock):
    d, eng = _trashed(usage_cache, clock)
    eng.breakdown(P, REVAL, NO_RULES, target=P)
    usage_cache.put_trash(run_trash_pass(eng, REVAL).to_json(), clock.now)

    fig = eng.trash_for(P)

    assert fig.complete and fig.known == 1048723 + 2097364
    assert fig.unchecked_bytes is None and fig.unchecked_share is None


def test_root_runs_trash_pass_only_when_stale(usage_cache, clock):
    d = _probe()
    eng = UsageEngine(d, usage_cache, clock=clock)
    five = RefreshPolicy.parse("5d")

    eng.root(RefreshPolicy.parse("never"), NO_RULES)
    assert ("list_trash", "/trash") not in d.calls and eng.trash_summary() is None

    eng.root(five, NO_RULES)
    assert d.calls.count(("list_trash", "/trash")) == 1
    stored = eng.trash_summary()
    assert stored is not None and stored.at == clock.now

    clock.advance(86400)
    eng.root(five, NO_RULES)
    assert d.calls.count(("list_trash", "/trash")) == 1

    eng.root(RefreshPolicy.parse("0"), NO_RULES)
    assert d.calls.count(("list_trash", "/trash")) == 2

    clock.advance(6 * 86400)
    eng.root(five, NO_RULES)
    assert d.calls.count(("list_trash", "/trash")) == 3


def test_trash_for_unknown_sized_only_folder_gives_unknown_coverage(usage_cache, clock):
    from dataclasses import replace

    d = TreeDrive()
    d.add_file("/my-files/a/f", 100)
    d.add_file("/my-files/b/big", 190_000)
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown("/my-files", REVAL, NO_RULES, target="/my-files")
    snap = usage_cache.snapshot("/my-files")
    assert snap is not None
    rows = tuple(replace(r, size=None) if r.name == "b" else r for r in snap.rows)
    usage_cache.put_snapshot(replace(snap, rows=rows))

    fig = eng.trash_for("/my-files")

    assert not fig.complete
    assert fig.unchecked_bytes is None and fig.unchecked_share is None


def test_unresolved_parent_file_group_counts_in_total_but_no_subtree(usage_cache, clock):
    d = _probe()
    d.trash(P + "/a")  # parent p never listed: its uid is unknown to the cache
    eng = UsageEngine(d, usage_cache, clock=clock)
    summary = run_trash_pass(eng, REVAL)
    usage_cache.put_trash(summary.to_json(), clock.now)

    assert summary.groups == (TrashGroup(None, 1, 0, 1048723),)
    assert summary.total_known == 1048723
    assert summary.complete  # only an unresolved FOLDER makes the summary incomplete
    assert eng.trash_for("/").known == 1048723
    sub = eng.trash_for("/my-files")
    assert sub.known == 0 and not sub.complete  # it might belong anywhere below


def test_negative_remainder_makes_folder_group_unmeasured(usage_cache, clock):
    from dataclasses import replace

    d, eng = _trashed(usage_cache, clock)
    uid = usage_cache.snapshot(P).uid
    real = d.list_trash

    def inflated(*, timeout):
        extra = replace(real(timeout=timeout)[0], name="ghost", kind="file",
                        size=10**9, parent_uid=uid)
        return real(timeout=timeout) + [extra]

    d.list_trash = inflated

    summary = run_trash_pass(eng, REVAL)

    (group,) = [g for g in summary.groups if g.folders]
    assert group.bytes is None and "smaller than" in (group.reason or "")
    assert not summary.complete
