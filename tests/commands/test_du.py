"""Tests for the ``protonfs du`` command: depth, formats, exit codes and path resolution."""

from __future__ import annotations

import io
import json

import pytest
from click.testing import CliRunner
from tests.usage.conftest import FakeClock
from tests.usage.treedrive import TreeDrive

from protonfs.cli import main
from protonfs.config import init_config
from protonfs.drive import DriveError
from protonfs.usage.cache import UsageCache
from protonfs.usage.engine import UsageEngine
from protonfs.usage.report import JSON_ROW_KEYS


def _tree() -> TreeDrive:
    d = TreeDrive()
    d.add_file("/my-files/A/f1", 100)
    d.add_file("/my-files/A/f2", 50)
    d.add_file("/my-files/A/sub/x", 1000)
    d.add_file("/my-files/A/sub/deep/z", 300)
    d.add_file("/my-files/B/big", 5000)
    return d


@pytest.fixture()
def drive() -> TreeDrive:
    return _tree()


@pytest.fixture()
def du(drive, tmp_path, monkeypatch):
    """Invoke ``protonfs du`` against ``drive`` with an isolated cache and config."""
    for var in ("PROTONFS_DU_REFRESH", "PROTONFS_PLAN_SIZE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PROTONFS_CONFIG", str(tmp_path / "global.json"))
    cache = UsageCache(tmp_path / "du.sqlite")
    engine = UsageEngine(drive, cache, clock=FakeClock())
    monkeypatch.setattr("protonfs.commands.du.default_engine", lambda: engine)
    monkeypatch.chdir(tmp_path)
    yield lambda *args: _runner().invoke(main, ["du", *args])
    cache.close()


@pytest.fixture()
def du_live(drive, tmp_path, monkeypatch):
    """Like ``du``, but the clock moves on every read, as real time does between calls."""
    from tests.usage.conftest import TickingClock

    for var in ("PROTONFS_DU_REFRESH", "PROTONFS_PLAN_SIZE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PROTONFS_CONFIG", str(tmp_path / "global.json"))
    cache = UsageCache(tmp_path / "du.sqlite")
    engine = UsageEngine(drive, cache, clock=TickingClock())
    monkeypatch.setattr("protonfs.commands.du.default_engine", lambda: engine)
    monkeypatch.chdir(tmp_path)
    run = lambda *args: _runner().invoke(main, ["du", *args])  # noqa: E731
    run.clock = engine.clock
    yield run
    cache.close()


def _runner() -> CliRunner:
    """A runner whose result keeps stderr apart from stdout on every supported Click.

    Click 8.1 (the newest for Python 3.9) mixes them unless ``mix_stderr=False``; Click 8.2
    always separates them and removed the argument.
    """
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()


def _rows(result) -> list[dict]:
    return json.loads(result.stdout)["rows"]


def test_du_default_depth_one_sorted(du):
    res = du("/my-files", "--json")
    assert res.exit_code == 0, res.output
    rows = _rows(res)
    assert [r["name"] for r in rows if r["depth"] == 1] == ["B", "A"]
    assert rows[0]["depth"] == 0 and all(r["depth"] <= 1 for r in rows)


@pytest.mark.parametrize(
    ("flag", "lo", "hi"),
    [(["-d", "2"], 0, 2), (["-d1-3"], 1, 3), (["-d", "2-"], 2, 99), (["-d", "-2"], 0, 2)],
)
def test_du_depth_forms(du, flag, lo, hi):
    res = du("/my-files/A", "--json", *flag)
    assert res.exit_code == 0, res.output
    depths = {r["depth"] for r in _rows(res)}
    assert min(depths) == lo
    assert max(depths) <= hi


def test_du_depth_conflicts_with_maxdepth(du):
    assert du("/my-files", "-d", "1", "--maxdepth", "2").exit_code == 2


@pytest.mark.parametrize("target", ["/", "/my-files"])
def test_du_depth_zero_prints_only_the_target(du, target):
    res = du(target, "--json", "-d", "0")
    assert res.exit_code in (0, 3), res.output + res.stderr
    assert [(r["depth"], r["path"]) for r in _rows(res)] == [(0, target)]


def test_du_dirs_only(du):
    rows = _rows(du("/my-files/A", "--json", "--dirs-only", "--maxdepth", "2"))
    assert rows and all(r["kind"] != "file" for r in rows)


def test_du_json_keys_frozen(du):
    doc = json.loads(du("/my-files", "--json").stdout)
    assert set(doc) == {"target", "generated_at", "used_total", "plan_size", "trash", "rows"}
    assert all(set(r) == set(JSON_ROW_KEYS) for r in doc["rows"])
    assert {"trash_at", "trash_unresolved"} <= set(JSON_ROW_KEYS)
    root = json.loads(du("/", "--json").stdout)["trash"]
    assert set(root) == {"at", "known", "complete", "partial", "unresolved_folders",
                         "unattributed"}


def test_du_plain_tab_separated(du):
    res = du("/my-files", "--format", "plain")
    lines = res.stdout.splitlines()
    assert lines and all(len(ln.split("\t")) == len(JSON_ROW_KEYS) for ln in lines)
    assert lines[0].split("\t")[JSON_ROW_KEYS.index("path")] == "/my-files"


def test_du_gap_row_labels(du, drive):
    def gaps(*args):
        rows = _rows(du("/my-files/A", "--json", "--refresh", "0", *args))
        return {r["name"]: r["size"] for r in rows if r["kind"] == "gap"}

    drive.trash("/my-files/A/f1")
    # frozen child never measured: its bytes stay in the remainder
    assert gaps("-x", "sub") == {"(unattributed, incl. excluded)": 100 + 1300}
    assert gaps() == {"(in trash)": 100}
    # frozen child now has a figure from an older pass
    assert list(gaps("-x", "sub")) == ["(unreconciled)"]


def test_du_freeze_shows_row_with_empty_size(du):
    rows = _rows(du("/my-files/A", "--json", "-x", "sub"))
    sub = [r for r in rows if r["name"] == "sub"][0]
    assert sub["size"] is None and sub["exclusion"] == "freeze" and sub["state"] == "excluded"


def test_du_hide_omits_row(du):
    rows = _rows(du("/my-files/A", "--json", "-X", "sub"))
    assert "sub" not in [r["name"] for r in rows]


def test_du_root_trash_lines(du, drive):
    drive.trash("/my-files/A/sub")
    res = du("/")
    assert res.exit_code == 3, res.output
    assert "1 trashed folders, size unknown" in res.stdout
    assert res.stdout.index("my-files") < res.stdout.index("trashed folders")


def test_du_root_trash_complete_has_no_lines(du, drive):
    drive.trash("/my-files/A/sub")
    assert du("/my-files/A").exit_code == 0  # caches A so the trashed folder resolves
    res = du("/")
    assert "trashed folders" not in res.stdout and "partial" not in res.stdout
    assert json.loads(du("/", "--json").stdout)["trash"]["complete"] is True


def test_du_exit_3_on_error_row(du, drive):
    drive.fail_next("size", "/my-files/A", DriveError("boom"), times=5)
    res = du("/my-files", "--json")
    assert res.exit_code == 3
    rows = _rows(res)
    assert {r["name"] for r in rows if r["depth"] == 1 and r["kind"] != "gap"} == {"A", "B"}
    assert any(r["state"] == "error" for r in rows)
    assert "/my-files/A" in res.stderr


def test_du_exit_3_on_partial_trash(du, drive, monkeypatch):
    def slow(*, timeout):
        raise DriveError("timed out")

    monkeypatch.setattr(drive, "list_trash", slow)
    res = du("/", "--json")
    assert res.exit_code == 3
    assert json.loads(res.stdout)["trash"]["partial"]


def test_du_root_exit_3_and_names_a_trash_group_it_could_not_measure(du, drive):
    """I1: an incomplete trash total exits 3 even with no unresolved folder."""
    drive.add_file("/my-files/A/other/o", 5)
    assert du("/my-files/A").exit_code == 0  # caches A, so the trashed folder resolves
    drive.trash("/my-files/A/sub")
    drive.fail_next("size", "/my-files/A/other", DriveError("throttled"))
    res = du("/")
    assert res.exit_code == 3, res.output + res.stderr
    assert "/my-files/A" in res.stdout and "size unknown" in res.stdout
    doc = json.loads(du("/", "--json", "--refresh", "never").stdout)
    assert doc["trash"]["complete"] is False


@pytest.mark.parametrize("flag", ["-x", "-X"])
def test_du_root_trash_pass_honours_exclusions(du, drive, flag):
    """I3: no Drive calls under an excluded folder; a user's exclusion alone exits 0."""
    assert du("/my-files/A").exit_code == 0
    drive.trash("/my-files/A/sub")
    drive.calls.clear()
    res = du("/", flag, "my-files/A")
    assert res.exit_code == 0, res.output + res.stderr
    assert not [c for c in drive.calls if c[1].startswith("/my-files/A")]
    assert "/my-files/A" in res.stdout and "excluded" in res.stdout


def test_du_root_reruns_a_trash_pass_that_skipped_a_folder_no_longer_excluded(du, drive):
    assert du("/my-files/A").exit_code == 0
    drive.trash("/my-files/A/sub")
    assert du("/", "-x", "my-files/A").exit_code == 0
    # Cache-only, the stored summary still has the unmeasured group: partial now.
    assert du("/", "--refresh", "never").exit_code == 3
    res = du("/", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    trash = json.loads(res.stdout)["trash"]
    assert trash["complete"] and trash["known"] == 1300
    assert drive.calls.count(("list_trash", "/trash")) == 2


def test_du_root_retries_unresolved_trash_once_its_parent_is_cached(du, drive):
    """I2: listing the parent makes the next ``du /`` measure the trashed folder."""
    drive.trash("/my-files/A/sub")
    first = du("/")
    assert first.exit_code == 3 and "1 trashed folders, size unknown" in first.stdout
    assert du("/my-files/A").exit_code == 0
    res = du("/", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    trash = json.loads(res.stdout)["trash"]
    assert trash["complete"] and trash["unresolved_folders"] == 0 and trash["known"] == 1300
    assert drive.calls.count(("list_trash", "/trash")) == 2


def test_du_root_lists_before_the_trash_pass_on_a_cold_cache(du, drive):
    """I2: ``du / -d 2`` lists /my-files first, so a folder trashed from it resolves."""
    drive.trash("/my-files/B")
    res = du("/", "-d", "2", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    trash = json.loads(res.stdout)["trash"]
    assert trash["complete"] and trash["known"] == 5000
    assert drive.calls.count(("list_trash", "/trash")) == 1


def test_du_root_reruns_an_incomplete_summary_stored_before_unresolved_parents(du, drive):
    from protonfs.usage.trash import TrashSummary

    du("/my-files/A")
    drive.trash("/my-files/A/sub")
    old = '{"at": 1800000000.0, "groups": [], "unresolved_folders": 1, "partial": null}'
    assert not TrashSummary.from_json(old).complete
    from protonfs.commands import du as du_command

    du_command.default_engine().cache.put_trash(old, 1800000000.0)  # the fixture's engine
    res = du("/", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    assert json.loads(res.stdout)["trash"]["complete"]


def _clock():
    from protonfs.commands import du as du_command

    return du_command.default_engine().clock  # the fixture's FakeClock


def test_du_in_trash_shows_trash_found_by_a_newer_listing(du, drive):
    """C1: prune, then re-check: the In trash cell agrees with the fresh remainder row."""
    assert du("/my-files/A").exit_code == 0
    assert du("/").exit_code == 0  # an empty, complete trash pass
    _clock().advance(60)
    drive.trash("/my-files/A/sub")
    res = du("/my-files/A", "--refresh", "0", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    doc = json.loads(res.stdout)
    top = doc["rows"][0]
    assert top["in_trash"] == 1300 and top["in_trash_complete"] is True
    assert top["trash_at"] == top["checked_at"] and top["trash_unresolved"] == 0
    gap = next(r for r in doc["rows"] if r["kind"] == "gap")
    assert gap["name"] == "(in trash)" and gap["size"] == 1300
    assert doc["trash"] is not None and doc["trash"]["known"] == 0  # the stored pass
    table = du("/my-files/A", "--refresh", "never")
    assert "1.3 KiB" in table.stdout.splitlines()[1]


def test_du_in_trash_shows_its_own_age_when_it_rests_on_an_older_pass(du):
    assert du("/").exit_code == 0
    _clock().advance(2 * 86400)
    res = du("/my-files", "--refresh", "0", "--json")
    top = _rows(res)[0]
    assert top["trash_at"] == top["checked_at"] - 2 * 86400
    table = du("/my-files", "--refresh", "never")
    assert "0 B (2d old)" in table.stdout.splitlines()[1]


def test_du_root_row_counts_unresolved_trashed_folders(du, drive):
    drive.trash("/my-files/A/sub")
    res = du("/", "--json")
    assert res.exit_code == 3
    rows = _rows(res)
    assert rows[0]["path"] == "/" and rows[0]["trash_unresolved"] == 1
    assert rows[0]["in_trash_complete"] is False and rows[0]["unchecked_bytes"] is None
    assert all(r["trash_unresolved"] == 0 for r in rows[1:])
    assert "unchecked: unknown" in du("/", "--refresh", "never").stdout


def test_du_root_counts_a_trashed_file_once_after_its_folder_is_listed(du_live, drive):
    """N1/R36, the reviewer's repro: a file trashed before anything was cached."""
    drive.add_file("/my-files/P/keep", 1000)
    drive.add_file("/my-files/P/old", 5000)
    drive.trash("/my-files/P/old")
    assert du_live("/").exit_code == 0
    du_live.clock.advance(60)
    assert du_live("/my-files/P").exit_code == 0
    du_live.clock.advance(60)
    res = du_live("/", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    top = _rows(res)[0]
    assert top["in_trash"] == 5000 and top["in_trash_complete"] is True


def test_du_root_trashed_empty_folder_is_complete(du, drive):
    drive.add_folder("/my-files/A/empty")
    assert du("/my-files/A").exit_code == 0
    drive.trash("/my-files/A/empty")
    res = du("/", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    trash = json.loads(res.stdout)["trash"]
    assert trash["complete"] is True and trash["known"] == 0


def test_du_exit_1_when_target_size_fails(du, drive):
    drive.fail_next("size", "/my-files/A", DriveError("boom"), times=5)
    res = du("/my-files/A")
    assert res.exit_code == 1


def test_du_exit_3_when_only_the_target_listing_fails(du, drive):
    """Its size was measured and printed, so the run is partial, not a failure."""
    drive.fail_next("list", "/my-files/A", DriveError("list boom"), times=5)
    res = du("/my-files/A", "--json")
    assert res.exit_code == 3, res.output + res.stderr
    top = _rows(res)[0]
    assert top["state"] == "error" and top["size"] == 1450 and top["error"] == "list boom"


def test_du_plan_hint_once(du):
    res = du("/my-files", "/my-files/A")
    assert res.stderr.count("du.plan_size") == 1
    assert "protonfs config set --global du.plan_size 3TB" in res.stderr


def test_du_no_plan_hint_when_set(du, monkeypatch):
    monkeypatch.setenv("PROTONFS_PLAN_SIZE", "1MB")
    res = du("/my-files/A", "--json")
    assert res.stderr == ""
    assert _rows(res)[0]["pct_plan"] is not None


def test_du_no_path_outside_store_errors(du, tmp_path):
    res = du()
    assert res.exit_code == 1
    assert str(tmp_path) in res.output + res.stderr


def test_du_local_path_in_other_store(du, tmp_path, monkeypatch):
    store = tmp_path / "sim"
    store.mkdir()
    init_config(store, "/my-files/A")
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    res = du(str(store / "sub"), "--json")
    assert res.exit_code == 0, res.output + res.stderr
    assert json.loads(res.stdout)["target"] == "/my-files/A/sub"


@pytest.mark.parametrize(("flag", "pattern"), [("-x", "!"), ("-X", "\\"), ("-x", "\\")])
@pytest.mark.parametrize("gui", [False, True])
def test_du_invalid_exclusion_pattern_is_usage_error(du, gui_calls, flag, pattern, gui):
    res = du("/my-files", flag, pattern, *(["--gui"] if gui else []))
    assert res.exit_code == 2, res.output + res.stderr
    assert repr(pattern) in res.stderr and "Traceback" not in res.output + res.stderr
    assert gui_calls == []


def test_du_remote_glob_expands_against_its_parent_listing(du, drive):
    """I8/R33: a quoted remote pattern lists its literal parent and measures each match."""
    res = du("/my-files/*", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    assert [d["target"] for d in json.loads(res.stdout)] == ["/my-files/A", "/my-files/B"]
    assert drive.calls[:2] == [("info", "/my-files"), ("list", "/my-files")]


def test_du_glob_matching_nothing_is_skipped_with_a_warning(du):
    res = du("/my-files/Z*", "/my-files/A", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    assert json.loads(res.stdout)["target"] == "/my-files/A"
    assert "matched nothing" in res.stderr and "'/my-files/Z*'" in res.stderr
    alone = du("/my-files/Z*")
    assert alone.exit_code == 0 and alone.stdout == "" and "matched nothing" in alone.stderr


def test_du_local_glob_matches_offloaded_folders_through_the_index(du, tmp_path, monkeypatch):
    """I8/R33: like pull, a local pattern matches the index, so offloaded folders count."""
    from protonfs.context import load_context
    from protonfs.index import IndexEntry

    store = tmp_path / "sim"
    store.mkdir()
    init_config(store, "/my-files/A")
    ctx = load_context(store)
    for rel in ("sub/x", "subway/y", "other/z"):
        ctx.index.set(rel, IndexEntry(1, 0.0, "", "", f"/my-files/A/{rel}", "d",
                                      "metadata-only", ""))
    ctx.index.save()
    monkeypatch.chdir(tmp_path)
    res = du("sim/sub*", "--json", "--refresh", "never")
    assert res.exit_code == 0, res.output + res.stderr
    assert [d["target"] for d in json.loads(res.stdout)] == [
        "/my-files/A/sub", "/my-files/A/subway"]


def test_du_gui_glob_needs_exactly_one_match(du, gui_calls):
    assert du("--gui", "/my-files/*").exit_code == 2
    assert gui_calls == []
    res = du("--gui", "/my-files/A*")
    assert res.exit_code == 0, res.output + res.stderr
    assert gui_calls[0][0] == "/my-files/A"


def test_du_listen_without_gui_is_usage_error(du):
    for flag in (["--listen", "0.0.0.0"], ["--port", "1"], ["--log", "x"]):
        assert du("/my-files", *flag).exit_code == 2


@pytest.fixture()
def gui_calls(monkeypatch):
    """Replace ``run_gui`` with a recorder so ``--gui`` never starts a server."""
    calls = []

    def fake(target, options, *, stdout, stderr):
        calls.append((target, options))
        return 0

    monkeypatch.setattr("protonfs.webui.server.run_gui", fake)
    return calls


@pytest.mark.parametrize(
    "extra",
    [["-d", "2"], ["-d", "-2"], ["--maxdepth", "2"], ["-D"], ["-x", "sub"], ["-X", "sub"],
     ["--refresh", "1h"], ["/my-files"]],
)
def test_du_gui_allowed_flags_reach_gui_path(du, gui_calls, extra):
    res = du("--gui", *extra) if "/my-files" in extra else du("--gui", "/my-files", *extra)
    assert res.exit_code == 0, res.output
    assert len(gui_calls) == 1 and gui_calls[0][1].gui


@pytest.mark.parametrize(
    "extra",
    [["--json"], ["--format", "plain"], ["-p"], ["--mindepth", "1"], ["-d", "1-3"],
     ["/my-files", "/devices"]],
)
def test_du_gui_forbidden_flags(du, extra):
    res = du("--gui", *extra)
    assert res.exit_code == 2


def test_du_gui_outside_a_store_is_usage_error(du, gui_calls, tmp_path):
    res = du("--gui")
    assert res.exit_code == 2, res.output + res.stderr
    assert str(tmp_path) in res.stderr and gui_calls == []


def test_du_gui_exit_code_is_run_guis(du, monkeypatch):
    monkeypatch.setattr("protonfs.webui.server.run_gui", lambda *a, **k: 4)
    assert du("--gui", "/my-files").exit_code == 4


def test_du_mindepth_alone_stays_bounded(du):
    rows = _rows(du("/my-files", "--json", "--mindepth", "1"))
    assert {r["depth"] for r in rows} == {1}
    from protonfs.commands.du import _depth_range

    assert _depth_range(None, 3, None) == (3, 3)
    assert _depth_range(None, 0, None) == (0, 1)


def test_du_error_row_carries_text(du, drive):
    drive.fail_next("size", "/my-files/A", DriveError("boom"), times=5)
    res = du("/my-files", "--json")
    bad = [r for r in _rows(res) if r["state"] == "error"][0]
    assert "boom" in bad["error"]
    assert f"! /my-files/A: {bad['error']}" in res.stderr


def test_du_exit_3_on_unresolved_trash(du, drive):
    drive.trash("/my-files/A/sub")
    res = du("/")
    assert res.exit_code == 3
    assert "1 trashed folders, size unknown" in res.stdout


def test_du_cells_show_age_and_lower_bound(du, drive):
    drive.trash("/my-files/A/sub")
    res = du("/")
    assert "0s" in res.stdout
    assert "≥" in res.stdout and "unchecked" in res.stdout


def test_du_used_hint_table_only(du):
    assert "%Used needs `protonfs du /` once" in du("/my-files/A").stderr
    assert "%Used" not in du("/my-files/A", "--json").stderr
    assert "%Used" not in du("/my-files/A", "--format", "plain").stderr


def test_du_json_with_other_format_is_usage_error(du):
    assert du("/my-files", "--json", "--format", "plain").exit_code == 2


def test_du_progress_only_with_p(du, drive):
    quiet = du("/my-files", "--refresh", "0")
    assert "calls=" not in quiet.stderr
    loud = du("/my-files", "-p", "--refresh", "0")
    assert "calls=" in loud.stderr


def test_du_age_column_shown(du):
    res = du("/my-files/A")
    assert res.exit_code == 0
    assert "State" in res.stdout and "Age" in res.stdout


def test_du_checks_account_once_per_run(du, monkeypatch):
    calls = []
    monkeypatch.setattr(UsageEngine, "check_account", lambda self, policy: calls.append(1))
    du("/my-files", "/my-files/A")
    assert calls == [1]


def test_du_root_devices_unmeasured_is_exit_3(du, drive):
    drive.fail_next("list", "/devices", DriveError("nope"), times=9)
    res = du("/", "--json")
    assert res.exit_code == 3, res.output + res.stderr
    assert any(r["path"] == "/devices" and r["state"] == "error" for r in _rows(res))


def test_du_root_user_exclusion_alone_is_exit_0(du, drive):
    for flag in ("-x", "-X"):
        res = du("/", "--json", flag, "devices")
        assert res.exit_code == 0, (flag, res.output + res.stderr)
    res = du("/", "--json", "-X", "my-files")
    assert res.exit_code == 0, res.output + res.stderr
    assert _rows(res)[0]["state"] == "partial"


def test_du_root_unmeasured_my_files_is_exit_3(du, drive):
    drive.fail_next("size", "/my-files", DriveError("boom"), times=9)
    assert du("/").exit_code == 3


def test_du_prints_throttle_notice_once(du, drive):
    drive.on_slow_size = None  # run_du attaches its callback when the source supports it

    def throttled():
        drive.on_slow_size("/my-files/A")
        drive.on_slow_size("/my-files/A")

    drive.before("size", "/my-files/A", throttled)
    res = du("/my-files", "--refresh", "0")
    assert res.exit_code == 0, res.output
    assert res.stderr.count("rate-limiting folder sizes") == 1


def _interrupt():
    raise KeyboardInterrupt


class _TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("style", ["inline", "lines"])
def test_du_progress_follows_the_global_progress_style(drive, tmp_path, monkeypatch, style):
    """M12: -p rewrites one stderr line under --progress-inline, one line each otherwise."""
    from protonfs.commands.du import build_options, run_du
    from protonfs.reporting import Reporter
    from protonfs.usage.paths import Target

    monkeypatch.setenv("PROTONFS_CONFIG", str(tmp_path / "global.json"))
    monkeypatch.setattr("protonfs.reporting._current",
                        Reporter(0, progress_style=style, stream=_TTY()))
    engine = UsageEngine(drive, UsageCache(tmp_path / "du.sqlite"), clock=FakeClock())
    err = _TTY()
    code = run_du([Target("/my-files", None)], build_options(progress=True),
                  engine_factory=lambda: engine, out=_TTY(), err=err)
    text = err.getvalue()
    assert code == 0, text
    assert "du calls=1 last=" in text
    if style == "inline":
        assert "\rdu calls=1 last=" in text
        # the open progress line is ended before the plan hint is written
        assert "\n% of plan" in text and "\r% of plan" not in text
    else:
        assert "\r" not in text and "du calls=1 last=" in text.splitlines()[0]
    engine.cache.close()


def test_du_ctrl_c_keeps_sizes_and_exits_130(du, drive):
    drive.before("size", "/my-files/B", _interrupt)
    res = du("/my-files", "--refresh", "1h")
    assert res.exit_code == 130
    assert "sizes measured so far are kept; run again to continue" in res.stderr


def test_du_ctrl_c_under_refresh_0_does_not_promise_to_continue(du, drive):
    """I4: --refresh 0 measures everything again, so a re-run with it does not continue."""
    drive.before("size", "/my-files/B", _interrupt)
    res = du("/my-files", "--refresh", "0")
    assert res.exit_code == 130
    assert "sizes measured so far are kept" in res.stderr
    assert "run again to continue" not in res.stderr and "--refresh 0" in res.stderr


def test_du_ctrl_c_during_the_trash_pass_says_it_starts_over(du, drive):
    assert du("/my-files/A").exit_code == 0
    drive.trash("/my-files/A/sub")
    drive.before("size", "/my-files/A", _interrupt)  # the trash pass re-sizes A
    res = du("/")
    assert res.exit_code == 130
    assert "trash pass" in res.stderr and "starts over" in res.stderr
    assert "continue" not in res.stderr


@pytest.mark.parametrize(("args", "expect", "never"), [
    (["/my-files", "--refresh", "1h"], "continue from them", "--refresh 0"),
    (["/my-files", "--refresh", "0"], "without --refresh 0", "Ctrl-C and run again"),
])
def test_du_throttle_notice_says_what_a_rerun_would_reuse(du, drive, args, expect, never):
    drive.on_slow_size = None

    drive.before("size", "/my-files/A", lambda: drive.on_slow_size("/my-files/A"))
    res = du(*args)
    assert "rate-limiting folder sizes" in res.stderr
    assert expect in res.stderr and never not in res.stderr


def test_du_throttle_notice_in_the_trash_pass_says_it_starts_over(du, drive):
    assert du("/my-files/A").exit_code == 0
    drive.trash("/my-files/A/sub")
    drive.on_slow_size = None
    drive.before("size", "/my-files/A", lambda: drive.on_slow_size("/my-files/A"))
    res = du("/")
    assert res.exit_code == 0, res.output + res.stderr
    assert "rate-limiting folder sizes" in res.stderr
    assert "trash pass starts over" in res.stderr and "continue" not in res.stderr


def test_du_trash_cells_blank_until_trash_measured(du):
    res = du("/my-files", "--json")
    assert res.exit_code == 0, res.output
    assert all(r["in_trash"] is None for r in _rows(res) if r["kind"] != "gap")
    table = du("/my-files")
    assert "unchecked" not in table.stdout
    assert "(In trash needs `protonfs du /` once)" in table.stderr


def test_du_trash_cells_filled_after_root_pass(du):
    assert du("/").exit_code == 0
    res = du("/my-files", "--json")
    folders = [r for r in _rows(res) if r["kind"] == "folder"]
    assert folders and all(r["in_trash"] is not None for r in folders)
    assert "In trash needs" not in du("/my-files").stderr


def test_du_root_cache_only_with_nothing_cached_is_partial(du, drive):
    """/devices unmeasured without a user exclusion is partial: exit 3, no Drive calls."""
    res = du("/", "--refresh", "never", "--json")
    assert res.exit_code == 3, res.output
    assert drive.calls == []
    states = {r["path"]: r["state"] for r in _rows(res)}
    assert states["/devices"] == "unmeasured" and states["/"] == "partial"


def test_build_options_records_an_explicit_depth(monkeypatch, tmp_path):
    from protonfs.commands.du import build_options

    monkeypatch.setenv("PROTONFS_CONFIG", str(tmp_path / "global.json"))
    assert build_options(gui=True, n_paths=1).maxdepth_given is False
    assert build_options(gui=True, depth="1", n_paths=1).maxdepth_given is True
    assert build_options(gui=True, maxdepth=2, n_paths=1).maxdepth_given is True


def test_du_retries_a_child_whose_size_failed_once(du, drive):
    drive.fail_next("size", "/my-files/A", DriveError("throttled"), times=1)
    res = du("/my-files", "-d", "2", "--json")
    assert res.exit_code == 0, res.output + res.stderr
    row = next(r for r in _rows(res) if r["path"] == "/my-files/A")
    assert row["size"] is not None and row["error"] is None


@pytest.mark.parametrize("depth", ["0", "1"])
def test_du_devices_is_the_sum_of_its_devices(du, drive, depth):
    """R35: the live CLI cannot size /devices, so du never asks it to."""
    drive.add_device("laptop")
    drive.add_file("/devices/laptop/a", 7)
    drive.add_device("desk")
    drive.add_file("/devices/desk/b", 5)
    res = du("/devices", "--json", "-d", depth)
    assert res.exit_code == 0, res.output + res.stderr
    rows = _rows(res)
    assert rows[0]["path"] == "/devices" and rows[0]["size"] == 12
    if depth == "1":
        assert {r["path"]: r["size"] for r in rows[1:]} == {
            "/devices/laptop": 7, "/devices/desk": 5}
    assert ("size", "/devices") not in drive.calls


def test_du_root_depth_two_never_sizes_the_devices_placeholder(du, drive):
    drive.fail_next("list", "/devices", DriveError("nope"), times=9)
    du("/", "-d", "2", "--json")
    assert ("size", "/devices") not in drive.calls
