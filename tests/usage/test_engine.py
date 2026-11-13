"""Tests for the ``du`` engine: consistent-set breakdowns, refresh rules, gaps and views."""

from __future__ import annotations

import subprocess
import threading
from collections import Counter
from dataclasses import replace

from tests.usage.treedrive import TreeDrive

from protonfs.drive import DriveError, DriveLockError
from protonfs.usage import paths
from protonfs.usage.cache import Figure, UsageCache
from protonfs.usage.engine import UsageEngine
from protonfs.usage.excludes import ExcludeRules, Exclusion
from protonfs.usage.model import GapKind, NodeState
from protonfs.usage.policy import RefreshPolicy

DAY = 86400.0
F = "/my-files/F"
FIVE_DAYS = RefreshPolicy.parse("5d")
ZERO = RefreshPolicy.parse("0")
NEVER = RefreshPolicy.parse("never")
NO_RULES = ExcludeRules()


def _tree() -> TreeDrive:
    d = TreeDrive()
    d.add_file(F + "/f1", 100)
    d.add_file(F + "/f2", 50)
    d.add_file(F + "/c1/x", 1000)
    d.add_file(F + "/c2/y", 2000)
    d.add_file(F + "/c2/deep/z", 300)
    return d


def _full_pass(path: str, children: list[str]) -> Counter:
    calls = Counter({("size", path): 1, ("list", path): 1})
    calls.update(("size", c) for c in children)
    return calls


def _by_name(bd):
    return {e.name: e for e in bd.entries}


def _touches(calls, folder: str) -> bool:
    return any(paths.is_within(p, folder) for _, p in calls)


def test_breakdown_lists_and_sizes_children_in_one_pass(usage_cache, clock):
    d = _tree()
    seen: list[tuple[str, str]] = []
    eng = UsageEngine(d, usage_cache, clock=clock, on_call=lambda op, p: seen.append((op, p)))
    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert Counter(d.calls) == _full_pass(F, [F + "/c1", F + "/c2"])
    assert d.calls[:2] == [("size", F), ("list", F)]
    assert Counter(seen) == Counter(d.calls)
    snap = usage_cache.snapshot(F)
    assert snap is not None and not snap.invalid
    assert all(r.pass_id == snap.pass_id for r in snap.rows)
    assert usage_cache.figure(F).pass_id == snap.pass_id
    assert usage_cache.figure(F + "/c2").pass_id == snap.pass_id
    assert usage_cache.get_meta("scope") == "vol-1"

    assert bd.state is NodeState.LISTED
    assert (bd.size, bd.descendants) == (3450, 8)
    assert bd.gap == 0 and bd.gap_kind is GapKind.NONE
    assert bd.at == bd.oldest_at == bd.newest_at == clock.now
    by = _by_name(bd)
    assert set(by) == {"f1", "f2", "c1", "c2"}
    assert by["c2"].size == 2300 and by["c2"].descendants == 3
    assert by["c2"].state is NodeState.SIZED and by["c2"].path == F + "/c2"
    assert by["f1"].size == 100 and by["f1"].kind == "file"


def test_gap_in_trash_exact(usage_cache, clock):
    d = TreeDrive()
    d.add_folder("/my-files/p")
    d.add_file("/my-files/p/a", 1048723)
    d.add_file("/my-files/p/c", 524403)
    d.add_folder("/my-files/p/sub")
    d.add_file("/my-files/p/sub/b", 2097364)
    d.trash("/my-files/p/a")
    d.trash("/my-files/p/sub")

    bd = UsageEngine(d, usage_cache, clock=clock).breakdown(
        "/my-files/p", FIVE_DAYS, NO_RULES, target="/my-files/p"
    )

    assert bd.size == 3670490
    assert [e.name for e in bd.entries] == ["c"]
    assert bd.gap == 3146087
    assert bd.gap_kind is GapKind.TRASH


def test_cache_hit_makes_no_calls(usage_cache, clock):
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    first = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    d.calls.clear()
    clock.advance(4 * DAY)

    second = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert d.calls == []
    assert second == first


def test_stale_unchanged_tree_costs_one_size_one_list(usage_cache, clock):
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    old_pass = usage_cache.snapshot(F).pass_id
    d.calls.clear()
    clock.advance(6 * DAY)

    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert d.calls == [("size", F), ("list", F)]
    snap = usage_cache.snapshot(F)
    assert snap.at == clock.now and snap.pass_id != old_pass
    assert all(r.pass_id == snap.pass_id and r.at == clock.now for r in snap.rows)
    assert usage_cache.figure(F).pass_id == snap.pass_id
    assert _by_name(bd)["c2"].size == 2300
    assert bd.at == bd.oldest_at == clock.now
    assert bd.gap_kind is GapKind.NONE


def test_trashed_child_folder_is_seen_after_threshold(usage_cache, clock):
    """Trashing leaves size(F) unchanged; only the re-listing reveals it (R10)."""
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    d.trash(F + "/c1")
    d.calls.clear()
    clock.advance(6 * DAY)

    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert Counter(d.calls) == _full_pass(F, [F + "/c2"])  # set changed: re-size all
    assert "c1" not in _by_name(bd)
    assert bd.gap == 1000
    assert bd.gap_kind is GapKind.TRASH


def test_trashed_direct_file_is_seen_after_threshold(usage_cache, clock):
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    d.trash(F + "/f1")
    d.calls.clear()
    clock.advance(6 * DAY)

    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert Counter(d.calls) == _full_pass(F, [F + "/c1", F + "/c2"])  # file bytes changed
    assert "f1" not in _by_name(bd)
    assert bd.gap == 100 and bd.gap_kind is GapKind.TRASH


def test_deep_change_resizes_children(usage_cache, clock):
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    d.add_file(F + "/c2/deep/new", 9)
    d.calls.clear()
    clock.advance(6 * DAY)

    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert Counter(d.calls) == _full_pass(F, [F + "/c1", F + "/c2"])  # fingerprint changed
    assert _by_name(bd)["c2"].size == 2309
    assert bd.gap_kind is GapKind.NONE


def test_deep_trash_reuses_child_sizes(usage_cache, clock):
    """Trash below a child stays inside that child's size, so F's sizes are reused."""
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    d.trash(F + "/c2/deep")
    d.calls.clear()
    clock.advance(6 * DAY)

    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert d.calls == [("size", F), ("list", F)]
    assert _by_name(bd)["c2"].size == 2300
    assert bd.gap == 0 and bd.gap_kind is GapKind.NONE
    sub = eng.breakdown(F + "/c2", FIVE_DAYS, NO_RULES, target=F)
    assert sub.gap == 300 and sub.gap_kind is GapKind.TRASH


def test_refresh_zero_disables_shortcut(usage_cache, clock):
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, ZERO, NO_RULES, target=F)
    d.calls.clear()
    clock.advance(1)

    eng.breakdown(F, ZERO, NO_RULES, target=F)

    assert Counter(d.calls) == _full_pass(F, [F + "/c1", F + "/c2"])


def test_refresh_never_makes_no_calls_and_returns_cached(usage_cache, clock, tmp_path):
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    first = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    d.calls.clear()
    clock.advance(100 * DAY)

    assert eng.breakdown(F, NEVER, NO_RULES, target=F) == first
    sized_only = eng.breakdown(F + "/c2", NEVER, NO_RULES, target=F)
    assert d.calls == []
    assert sized_only.state is NodeState.SIZED and sized_only.size == 2300
    assert sized_only.entries == () and sized_only.gap is None

    from protonfs.usage.cache import UsageCache

    empty = UsageCache(tmp_path / "empty.sqlite")
    try:
        bd = UsageEngine(d, empty, clock=clock).breakdown(F, NEVER, NO_RULES, target=F)
    finally:
        empty.close()
    assert d.calls == []
    assert bd.state is NodeState.UNMEASURED and bd.size is None
    assert bd.gap is None and bd.gap_kind is GapKind.NONE


def _sim_tree() -> TreeDrive:
    d = TreeDrive()
    d.add_file(F + "/sim/big", 5000)
    d.add_file(F + "/sim/sub/x", 7)
    d.add_file(F + "/other/y", 10)
    d.add_file(F + "/f", 1)
    return d


def test_freeze_child_no_calls_empty_cells(usage_cache, clock):
    d = _sim_tree()
    bd = UsageEngine(d, usage_cache, clock=clock).breakdown(
        F, FIVE_DAYS, ExcludeRules(freeze=["sim"]), target=F
    )

    assert not _touches(d.calls, F + "/sim")
    sim = _by_name(bd)["sim"]
    assert sim.size is None and sim.descendants is None and sim.at is None
    assert sim.exclusion is Exclusion.FREEZE and sim.state is NodeState.EXCLUDED
    assert bd.gap == 5007
    assert bd.gap_kind is GapKind.UNATTRIBUTED


def test_freeze_child_with_cached_figure_is_unreconciled(usage_cache, clock):
    d = _sim_tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    t1 = clock.now
    d.trash(F + "/f")
    clock.advance(60)
    d.calls.clear()

    bd = eng.breakdown(F, ZERO, ExcludeRules(freeze=["sim"]), target=F)

    assert not _touches(d.calls, F + "/sim")
    sim = _by_name(bd)["sim"]
    assert (sim.size, sim.at) == (5007, t1)
    assert sim.exclusion is Exclusion.FREEZE and sim.state is NodeState.EXCLUDED
    assert bd.gap == 1
    assert bd.gap_kind is GapKind.UNRECONCILED
    assert (bd.oldest_at, bd.newest_at) == (t1, clock.now)
    # Control: the same remainder from one pass is trash.
    assert eng.breakdown(F, ZERO, NO_RULES, target=F).gap_kind is GapKind.TRASH


def test_freeze_cached_figure_keeps_its_age_when_sizes_are_reused(usage_cache, clock):
    """Reusing child sizes must not promote a frozen row from an older pass."""
    d = _sim_tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    t1 = clock.now
    d.trash(F + "/f")
    clock.advance(60)
    rules = ExcludeRules(freeze=["sim"])
    assert eng.breakdown(F, ZERO, rules, target=F).gap_kind is GapKind.UNRECONCILED
    clock.advance(6 * DAY)
    d.calls.clear()

    bd = eng.breakdown(F, FIVE_DAYS, rules, target=F)

    assert d.calls == [("size", F), ("list", F)]  # "other" reused, "sim" frozen
    assert _by_name(bd)["sim"].at == t1
    assert bd.gap_kind is GapKind.UNRECONCILED


def test_hide_child_omitted_but_labels_gap_unattributed(usage_cache, clock):
    d = _sim_tree()
    bd = UsageEngine(d, usage_cache, clock=clock).breakdown(
        F, FIVE_DAYS, ExcludeRules(hide=["sim"]), target=F
    )

    assert not _touches(d.calls, F + "/sim")
    assert set(_by_name(bd)) == {"other", "f"}
    assert bd.size == 5018
    assert bd.gap == 5007
    assert bd.gap_kind is GapKind.UNATTRIBUTED


def test_breakdown_of_excluded_path_makes_no_calls(usage_cache, clock):
    d = _sim_tree()
    bd = UsageEngine(d, usage_cache, clock=clock).breakdown(
        F + "/sim", FIVE_DAYS, ExcludeRules(freeze=["sim"]), target=F
    )

    assert d.calls == []
    assert bd.state is NodeState.EXCLUDED and bd.size is None


def test_negative_gap_is_unreconciled(usage_cache, clock):
    d = TreeDrive()
    d.add_file(F + "/f", 5)
    d.add_file(F + "/c/x", 100)
    child = F + "/c"
    d.before("size", child, lambda: d.add_file(child + "/late", 10))
    eng = UsageEngine(d, usage_cache, clock=clock)

    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert bd.gap == -10
    assert bd.gap_kind is GapKind.UNRECONCILED
    # The next full pass reconciles.
    again = eng.breakdown(F, ZERO, NO_RULES, target=F)
    assert again.gap == 0 and again.gap_kind is GapKind.NONE


def test_child_change_invalidates_child_and_parent_snapshots(usage_cache, clock):
    d = TreeDrive()
    d.add_file(F + "/f", 1)
    d.add_file(F + "/c/x", 10)
    c = F + "/c"
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    assert _by_name(eng.breakdown(F, NEVER, NO_RULES, target=F))["c"].state is NodeState.SIZED
    eng.breakdown(c, FIVE_DAYS, NO_RULES, target=F)
    assert _by_name(eng.breakdown(F, NEVER, NO_RULES, target=F))["c"].state is NodeState.LISTED
    assert not usage_cache.snapshot(F).invalid and not usage_cache.snapshot(c).invalid

    # The child changes and is re-measured on its own: the parent's row is now outdated.
    d.add_file(c + "/new", 5)
    clock.advance(DAY)
    eng.breakdown(c, ZERO, NO_RULES, target=F)
    assert usage_cache.snapshot(F).invalid

    # An invalid parent is re-measured even within the threshold; re-sizing the
    # changed child marks the child's own snapshot invalid.
    d.add_file(c + "/newer", 7)
    clock.advance(DAY)
    d.calls.clear()
    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    assert ("list", F) in d.calls
    assert not usage_cache.snapshot(F).invalid
    assert usage_cache.snapshot(c).invalid
    assert usage_cache.figure(c).size == 22
    assert _by_name(bd)["c"].size == 22 and bd.gap_kind is GapKind.NONE


def test_child_size_error_marks_row_error_not_crash(usage_cache, clock):
    d = TreeDrive()
    d.add_file(F + "/c1/x", 10)
    d.add_file(F + "/c2/y", 20)
    d.fail_next(
        "size", F + "/c1",
        DriveLockError("database is locked by another proton-drive process"), times=7,
    )

    bd = UsageEngine(d, usage_cache, clock=clock).breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    by = _by_name(bd)
    assert by["c1"].state is NodeState.ERROR and by["c1"].size is None
    assert "database is locked" in by["c1"].error
    assert by["c2"].state is NodeState.SIZED and by["c2"].size == 20
    assert bd.state is NodeState.LISTED and bd.error is None
    assert bd.gap == 10
    assert bd.gap_kind is GapKind.UNATTRIBUTED


def test_error_hole_is_retried_within_threshold(usage_cache, clock):
    d = TreeDrive()
    d.add_file(F + "/c1/x", 10)
    d.add_file(F + "/c2/y", 20)
    d.fail_next("size", F + "/c1", DriveError("transient"))
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    d.calls.clear()
    clock.advance(60)

    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert Counter(d.calls) == _full_pass(F, [F + "/c1"])  # c2's size is reused
    assert _by_name(bd)["c1"].state is NodeState.SIZED
    assert bd.gap_kind is GapKind.NONE


def test_target_size_error_is_error_breakdown(usage_cache, clock):
    d = _tree()
    d.fail_next("size", F, DriveError("Node not found"))

    bd = UsageEngine(d, usage_cache, clock=clock).breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert bd.state is NodeState.ERROR and bd.error == "Node not found"
    assert bd.entries == () and bd.gap is None
    assert d.calls == [("size", F)]


def test_scope_change_wipes_cache(usage_cache, clock):
    d1 = TreeDrive(scope="vol-1")
    d1.add_file(F + "/old/x", 10)
    d1.add_file(F + "/f", 1)
    UsageEngine(d1, usage_cache, clock=clock).breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    assert usage_cache.get_meta("scope") == "vol-1"
    assert d1.calls.count(("size", F)) == 1  # a fresh cache records the scope, no restart
    assert usage_cache.figure(F + "/old") is not None

    d2 = TreeDrive(scope="vol-2")
    d2.add_file(F + "/new/y", 30)
    clock.advance(6 * DAY)
    bd = UsageEngine(d2, usage_cache, clock=clock).breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert usage_cache.get_meta("scope") == "vol-2"
    assert usage_cache.figure(F + "/old") is None
    assert [e.name for e in bd.entries] == ["new"]
    assert bd.size == 30 and bd.gap_kind is GapKind.NONE
    assert d2.calls.count(("size", F)) == 2 and d2.calls.count(("list", F)) == 2


def _deep_tree(t: str) -> TreeDrive:
    d = TreeDrive()
    d.add_file(t + "/a/b/c/x", 1)
    d.add_file(t + "/a/y", 2)
    d.add_file(t + "/z/w", 3)
    d.add_file(t + "/top", 4)
    return d


def test_tree_respects_maxdepth(usage_cache, clock):
    t = "/my-files/T"
    d = _deep_tree(t)
    eng = UsageEngine(d, usage_cache, clock=clock)

    assert set(eng.tree(t, FIVE_DAYS, NO_RULES, maxdepth=1)) == {t}
    out = eng.tree(t, FIVE_DAYS, NO_RULES, maxdepth=2)
    assert set(out) == {t, t + "/a", t + "/z"}
    assert ("list", t + "/a/b") not in d.calls
    assert out[t + "/a"].state is NodeState.LISTED

    frozen = eng.tree(t, FIVE_DAYS, ExcludeRules(freeze=["z"]), maxdepth=3)
    assert set(frozen) == {t, t + "/a", t + "/a/b"}


def test_walk_cancel(usage_cache, clock):
    t = "/my-files/T"
    d = _deep_tree(t)
    cancel = threading.Event()
    progress: list[tuple[int, int]] = []

    def on_progress(done: int, total: int) -> None:
        progress.append((done, total))
        cancel.set()

    eng = UsageEngine(d, usage_cache, clock=clock)
    n = eng.walk(t, FIVE_DAYS, NO_RULES, on_progress=on_progress, cancel=cancel)

    assert n == 1
    assert progress == [(1, 3)]
    assert {p for op, p in d.calls if op == "list"} == {t}


def test_walk_breaks_down_every_non_excluded_folder(usage_cache, clock):
    t = "/my-files/T"
    d = _deep_tree(t)
    eng = UsageEngine(d, usage_cache, clock=clock)

    assert eng.walk(t, FIVE_DAYS, ExcludeRules(freeze=["a/b"])) == 3
    assert not _touches(d.calls, t + "/a/b")
    assert eng.walk(t, FIVE_DAYS, NO_RULES) == 5


def test_figure_sizes_only_when_stale(usage_cache, clock):
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)

    e = eng.figure(F, FIVE_DAYS)
    assert d.calls == [("size", F)]
    assert (e.size, e.state, e.at, e.name) == (3450, NodeState.SIZED, clock.now, "F")
    clock.advance(DAY)
    assert eng.figure(F, FIVE_DAYS) == e and d.calls == [("size", F)]
    eng.breakdown(F, ZERO, NO_RULES, target=F)
    d.add_file(F + "/more", 1)
    clock.advance(6 * DAY)
    d.calls.clear()

    e2 = eng.figure(F, FIVE_DAYS)

    assert d.calls == [("size", F)]
    assert e2.size == 3451 and e2.state is NodeState.LISTED
    assert usage_cache.snapshot(F).invalid


def test_root_view_sections(usage_cache, clock):
    d = TreeDrive()
    d.add_file("/my-files/f", 100)
    d.add_file("/my-files/d/x", 50)
    d.add_device("laptop")
    d.add_file("/devices/laptop/a", 7)
    d.add_device("desk")
    eng = UsageEngine(d, usage_cache, clock=clock)
    assert eng.used_total() is None and eng.device_paths() is None

    bd = eng.root(FIVE_DAYS, NO_RULES)

    by = {e.path: e for e in bd.entries}
    assert set(by) == {"/my-files", "/devices/laptop", "/devices/desk", "/photos"}
    assert (by["/my-files"].size, by["/my-files"].state) == (150, NodeState.SIZED)
    assert (by["/devices/laptop"].size, by["/devices/laptop"].state) == (7, NodeState.SIZED)
    assert (by["/devices/desk"].size, by["/devices/desk"].state) == (0, NodeState.SIZED)
    assert by["/photos"].state is NodeState.UNMEASURED and by["/photos"].size is None
    assert all(e.kind == "section" for e in bd.entries)
    assert bd.path == "/" and bd.size == 157 and bd.state is NodeState.LISTED
    assert set(eng.device_paths()) == {"/devices/laptop", "/devices/desk"}
    assert bd.gap is None and bd.gap_kind is GapKind.NONE
    assert eng.used_total() == 157
    assert Counter(d.calls) == Counter({
        ("size", "/my-files"): 1, ("list", "/devices"): 1,
        ("size", "/devices/laptop"): 1, ("size", "/devices/desk"): 1,
        ("list_trash", "/trash"): 1,  # root() runs the automatic trash pass
    })

    d.calls.clear()
    assert eng.root(FIVE_DAYS, NO_RULES) == bd and d.calls == []
    hidden = eng.root(FIVE_DAYS, ExcludeRules(hide=["devices"]))
    assert {e.path for e in hidden.entries} == {"/my-files", "/photos"}
    assert hidden.state is NodeState.PARTIAL and hidden.size == 150
    assert eng.breakdown("/", FIVE_DAYS, NO_RULES, target="/") == bd
    assert d.calls == []


def test_undecryptable_child_addressed_by_uid(usage_cache, clock):
    class _OpaqueDrive(TreeDrive):
        """Lists the folders whose uid is in ``opaque`` with an undecryptable name."""

        def __init__(self) -> None:
            super().__init__()
            self.opaque: set[str] = set()

        def list_dir(self, path, *, expected_entries=None):
            out = super().list_dir(path, expected_entries=expected_entries)
            return [
                replace(e, name="(undecryptable)", decrypted=False) if e.uid in self.opaque
                else e
                for e in out
            ]

    d = _OpaqueDrive()
    n = int(d.add_folder(F).rsplit("-", 1)[1]) + 1
    uid = f"uid-{n}"
    opaque_path = f"{F}/{uid}"
    assert d.add_folder(opaque_path) == uid  # its last path segment is its uid
    d.add_file(opaque_path + "/x", 40)
    d.opaque.add(uid)

    bd = UsageEngine(d, usage_cache, clock=clock).breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert ("size", opaque_path) in d.calls
    (e,) = bd.entries
    assert (e.path, e.uid, e.name, e.decrypted) == (opaque_path, uid, uid, False)
    assert e.size == 40 and e.state is NodeState.SIZED
    assert bd.gap_kind is GapKind.NONE
    assert usage_cache.path_for_uid(uid) == opaque_path


def test_names_with_slash_and_glob_chars(usage_cache, clock):
    d = TreeDrive()
    uids = {seg: d.add_folder(f"{F}/{seg}") for seg in ("a\\/b", "x[1]", "*")}
    for seg in uids:
        d.add_file(f"{F}/{seg}/f", 1)
    eng = UsageEngine(d, usage_cache, clock=clock)

    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert {p for op, p in d.calls if op == "size"} == {
        F, F + "/a\\/b", F + "/x[1]", F + "/*",
    }
    assert {(e.name, e.path) for e in bd.entries} == {
        ("a/b", F + "/a\\/b"), ("x[1]", F + "/x[1]"), ("*", F + "/*"),
    }
    assert {r.name for r in usage_cache.snapshot(F).rows} == {"a/b", "x[1]", "*"}
    assert usage_cache.path_for_uid(uids["a\\/b"]) == F + "/a\\/b"
    d.calls.clear()
    child = eng.breakdown(F + "/a\\/b", FIVE_DAYS, NO_RULES, target=F)
    assert ("list", F + "/a\\/b") in d.calls and child.state is NodeState.LISTED
    frozen = eng.breakdown(F, ZERO, ExcludeRules(freeze=["a/b"]), target=F)
    assert _by_name(frozen)["a/b"].exclusion is Exclusion.FREEZE


class _RaceSource:
    """Wraps a source; runs ``hook`` (once, when set) just before ``size(path)``."""

    def __init__(self, inner: TreeDrive, path: str) -> None:
        self.inner = inner
        self.path = path
        self.hook = None
        self.concurrency = inner.concurrency

    def size(self, path):
        if path == self.path and self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        return self.inner.size(path)

    def list_dir(self, path, *, expected_entries=None):
        return self.inner.list_dir(path, expected_entries=expected_entries)

    def list_trash(self, *, timeout):
        return self.inner.list_trash(timeout=timeout)


def test_concurrent_writer_cannot_promote_old_rows(usage_cache, clock):
    """Two engines on one cache file: B stores a mixed-pass snapshot during A's size(F).

    A must build its snapshot from its own listing and sizes, never adopt B's rows.
    """
    d = _sim_tree()
    race = _RaceSource(d, F)
    eng_a = UsageEngine(race, usage_cache, clock=clock)
    eng_a.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    d.trash(F + "/f")
    clock.advance(6 * DAY)
    other = UsageCache(usage_cache.path)
    seen_by_b = []
    try:
        eng_b = UsageEngine(d, other, clock=clock)
        race.hook = lambda: seen_by_b.append(
            eng_b.breakdown(F, ZERO, ExcludeRules(freeze=["sim"]), target=F)
        )
        d.calls.clear()

        bd = eng_a.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    finally:
        other.close()

    assert seen_by_b[0].gap_kind is GapKind.UNRECONCILED  # B's frozen set is mixed
    assert ("size", F + "/sim") in d.calls  # only A sizes sim: B froze it
    assert bd.gap == 1 and bd.gap_kind is GapKind.TRASH
    assert _by_name(bd)["sim"].at == clock.now
    assert usage_cache.figure(F + "/sim").at == clock.now
    stored = usage_cache.snapshot(F)
    assert all(r.pass_id == stored.pass_id for r in stored.rows)


def _root_tree() -> TreeDrive:
    d = TreeDrive()
    d.add_file("/my-files/f", 100)
    d.add_file("/my-files/d/x", 50)
    d.add_device("laptop")
    d.add_file("/devices/laptop/a", 7)
    d.add_device("desk")
    return d


def test_root_unknown_device_list_is_partial(usage_cache, clock, tmp_path):
    d = _root_tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.figure("/my-files", FIVE_DAYS)
    d.calls.clear()

    bd = eng.root(NEVER, NO_RULES)

    assert d.calls == []
    by = {e.path: e for e in bd.entries}
    assert set(by) == {"/my-files", "/devices", "/photos"}
    assert by["/devices"].state is NodeState.UNMEASURED and by["/devices"].size is None
    assert bd.state is NodeState.PARTIAL and bd.size == 150

    empty = UsageCache(tmp_path / "empty.sqlite")
    try:
        frozen = UsageEngine(d, empty, clock=clock).root(
            FIVE_DAYS, ExcludeRules(freeze=["devices"])
        )
    finally:
        empty.close()
    assert not _touches(d.calls, "/devices")
    dev = {e.path: e for e in frozen.entries}["/devices"]
    assert dev.state is NodeState.EXCLUDED and dev.exclusion is Exclusion.FREEZE
    assert dev.size is None
    assert frozen.state is NodeState.PARTIAL and frozen.size == 150


def test_tree_and_walk_at_root_skip_unmeasurable_entries(usage_cache, clock):
    d = _root_tree()
    d.fail_next("list", "/devices", DriveError("devices unavailable"), times=2)
    eng = UsageEngine(d, usage_cache, clock=clock)

    out = eng.tree("/", FIVE_DAYS, NO_RULES, maxdepth=2)

    assert set(out) == {"/", "/my-files"}
    devices = {e.path: e for e in out["/"].entries}["/devices"]
    assert devices.state is NodeState.ERROR and out["/"].state is NodeState.PARTIAL
    assert eng.walk("/", FIVE_DAYS, NO_RULES) == 3  # /, /my-files, /my-files/d
    assert ("size", "/devices") not in d.calls
    assert d.calls.count(("list", "/devices")) == 2  # only root()'s own attempts
    assert not _touches(d.calls, "/photos")


def test_partial_trash_summary_is_retried(usage_cache, clock):
    d = _root_tree()
    d.fail_next("list_trash", "/trash", subprocess.TimeoutExpired("proton-drive", 300))
    eng = UsageEngine(d, usage_cache, clock=clock)
    eng.root(FIVE_DAYS, NO_RULES)
    assert eng.trash_summary().partial == "trash listing timed out after 300 s"

    eng.root(NEVER, NO_RULES)
    assert d.calls.count(("list_trash", "/trash")) == 1
    eng.root(FIVE_DAYS, NO_RULES)  # same moment: partial is stale anyway
    assert d.calls.count(("list_trash", "/trash")) == 2
    assert eng.trash_summary().partial is None
    eng.root(FIVE_DAYS, NO_RULES)  # complete and fresh: not re-run
    assert d.calls.count(("list_trash", "/trash")) == 2


def test_check_account_clears_old_account_with_fresh_figures(usage_cache, clock):
    d1 = TreeDrive(scope="vol-1")
    d1.add_file(F + "/old/x", 10)
    UsageEngine(d1, usage_cache, clock=clock).breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    d2 = TreeDrive(scope="vol-2")
    d2.add_file(F + "/new/y", 30)
    eng = UsageEngine(d2, usage_cache, clock=clock)  # every figure is still fresh

    assert eng.check_account(FIVE_DAYS) is True

    assert d2.calls == [("list", "/my-files")]
    assert usage_cache.figure(F + "/old") is None and usage_cache.snapshot(F) is None
    assert usage_cache.get_meta("scope") == "vol-2"
    bd = eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)
    assert [e.name for e in bd.entries] == ["new"]
    d2.calls.clear()
    assert eng.check_account(FIVE_DAYS) is False and d2.calls == []


def test_check_account_same_scope_or_never_keeps_cache(usage_cache, clock):
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)
    assert eng.check_account(FIVE_DAYS) is False  # empty cache: scope just recorded
    assert usage_cache.get_meta("scope") == "vol-1"
    eng.breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    later = UsageEngine(d, usage_cache, clock=clock)
    d.calls.clear()
    assert later.check_account(NEVER) is False and d.calls == []
    assert later.check_account(FIVE_DAYS) is False
    assert d.calls == [("list", "/my-files")]
    assert usage_cache.snapshot(F) is not None


def test_unscoped_data_counts_as_another_account(usage_cache, clock):
    usage_cache.put_figure(F + "/stray", Figure(1, 0, clock.now, None))
    d = _tree()
    eng = UsageEngine(d, usage_cache, clock=clock)

    assert eng.check_account(FIVE_DAYS) is True

    assert usage_cache.figure(F + "/stray") is None
    assert usage_cache.get_meta("scope") == "vol-1"


def test_unscoped_data_found_by_a_breakdown_restarts_it(usage_cache, clock):
    usage_cache.put_figure(F + "/stray", Figure(1, 0, clock.now, None))
    d = _tree()

    bd = UsageEngine(d, usage_cache, clock=clock).breakdown(F, FIVE_DAYS, NO_RULES, target=F)

    assert usage_cache.figure(F + "/stray") is None
    assert d.calls.count(("size", F)) == 2 and d.calls.count(("list", F)) == 2
    assert bd.gap_kind is GapKind.NONE and bd.size == 3450
