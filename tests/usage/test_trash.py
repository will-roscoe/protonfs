"""Tests for the trash pass and per-subtree trash figures."""

from __future__ import annotations

import subprocess

from tests.usage.treedrive import TreeDrive

from protonfs.drive import DriveError
from protonfs.usage.engine import UsageEngine
from protonfs.usage.excludes import ExcludeRules
from protonfs.usage.policy import RefreshPolicy
from protonfs.usage.trash import TrashGroup, TrashSummary, run_trash_pass

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


def test_trash_pass_leaves_the_listing_timeout_to_the_source(usage_cache, clock):
    d, eng = _trashed(usage_cache, clock)
    seen = []
    real = d.list_trash

    def recording(*, timeout=None):
        seen.append(timeout)
        return real(timeout=timeout)

    d.list_trash = recording
    run_trash_pass(eng, REVAL)
    assert seen == [None]


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
    s = TrashSummary(5.0, (TrashGroup(P, 1, 0, 7), TrashGroup(None, 0, 2, None, "why")), 3,
                     "x")
    assert TrashSummary.from_json(s.to_json()) == s


def test_summary_json_without_reasons_still_loads():
    old = ('{"at": 5.0, "groups": [["/my-files/p", 0, 1, null]], '
           '"unresolved_folders": 0, "partial": null}')
    (group,) = TrashSummary.from_json(old).groups
    assert group == TrashGroup(P, 0, 1, None) and group.reason is None


def test_trash_for_subtree_lower_bound_with_unchecked(usage_cache, clock):
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
    assert fig.unchecked_bytes == 190_000
    total = usage_cache.figure("/my-files").size
    assert fig.unchecked_share == 190_000 / total
    assert fig.at == clock.now
    assert eng.trash_for("/my-files/a").known == 200
    assert eng.trash_for("/my-files/b").known == 0


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
