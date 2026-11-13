"""Tests for the ``protonfs du`` command: depth, formats, exit codes and path resolution."""

from __future__ import annotations

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
    yield lambda *args: CliRunner().invoke(main, ["du", *args])
    cache.close()


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


def test_du_dirs_only(du):
    rows = _rows(du("/my-files/A", "--json", "--dirs-only", "--maxdepth", "2"))
    assert rows and all(r["kind"] != "file" for r in rows)


def test_du_json_keys_frozen(du):
    doc = json.loads(du("/my-files", "--json").stdout)
    assert set(doc) == {"target", "generated_at", "used_total", "plan_size", "trash", "rows"}
    assert all(set(r) == set(JSON_ROW_KEYS) for r in doc["rows"])


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


def test_du_exit_1_when_target_size_fails(du, drive):
    drive.fail_next("size", "/my-files/A", DriveError("boom"), times=5)
    res = du("/my-files/A")
    assert res.exit_code == 1


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


def test_du_ctrl_c_keeps_sizes_and_exits_130(du, drive):
    def interrupt():
        raise KeyboardInterrupt

    drive.before("size", "/my-files/B", interrupt)
    res = du("/my-files", "--refresh", "0")
    assert res.exit_code == 130
    assert "sizes measured so far are kept" in res.stderr


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
