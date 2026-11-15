"""Every store-scoped command finds its store from a subdirectory (#232), with PATH
arguments relative to the current directory, as git pathspecs are."""
from __future__ import annotations

import os
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from protonfs.cli import _implicit_scope, main
from protonfs.config import init_config
from protonfs.context import load_context


@pytest.fixture
def store(tmp_path, monkeypatch, make_fake_drive):
    """A store at ``tmp_path/root`` with ``sim/`` and ``other/`` subdirectories, a fake
    Drive wired in under the real discovery, and the cwd left at the store root."""
    root = tmp_path / "root"
    (root / "sim" / "mload005").mkdir(parents=True)
    (root / "sim" / "mload006").mkdir()
    (root / "other").mkdir()
    (root / "sim" / "mload005" / "d1").write_bytes(b"one")
    (root / "sim" / "mload006" / "d1").write_bytes(b"two")
    (root / "other" / "x").write_bytes(b"x")
    init_config(root, "/my-files/test")
    drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.DriveClient", lambda **kw: drive)
    monkeypatch.chdir(root)
    return root, drive


def _run(*args: str):
    return CliRunner().invoke(main, list(args))


def test_load_context_finds_the_store_from_a_subdirectory(store) -> None:
    root, _ = store
    assert load_context(root / "sim" / "mload005").root == root.resolve()


def test_load_context_prefers_the_innermost_nested_store(store, make_fake_drive) -> None:
    root, _ = store
    init_config(root / "sim", "/my-files/inner")
    assert load_context(root / "sim" / "mload005").root == (root / "sim").resolve()
    assert load_context(root / "other").root == root.resolve()


def test_load_context_follows_a_symlinked_directory(store, tmp_path) -> None:
    root, _ = store
    link = tmp_path / "link"
    link.symlink_to(root / "sim")
    assert load_context(link).root == root.resolve()


def test_load_context_outside_any_store_says_so(tmp_path) -> None:
    with pytest.raises(click.ClickException, match="any parent directory"):
        load_context(tmp_path)


def test_config_get_works_from_a_subdirectory(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim" / "mload005")
    result = _run("config", "get", "remote_root")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "/my-files/test"


def test_config_set_writes_the_discovered_store(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("config", "set", "remote_root", "/my-files/moved")
    assert result.exit_code == 0, result.output
    assert not (root / "sim" / ".protonfs").exists()
    assert "/my-files/moved" in (root / ".protonfs" / "config.json").read_text()


def test_push_path_is_relative_to_the_current_directory(store, monkeypatch) -> None:
    root, drive = store
    monkeypatch.chdir(root / "sim")
    result = _run("push", "mload005")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload005/d1") is not None
    assert ctx.index.get("sim/mload006/d1") is None
    assert ctx.index.get("other/x") is None


def test_push_same_as_from_the_root(store, monkeypatch) -> None:
    root, drive = store
    monkeypatch.chdir(root / "sim" / "mload005")
    result = _run("push", ".")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload005/d1") is not None
    assert ctx.index.get("sim/mload006/d1") is None


def test_push_dotdot_reaches_a_sibling(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim" / "mload005")
    result = _run("push", "../mload006")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload006/d1") is not None
    assert ctx.index.get("sim/mload005/d1") is None


def test_push_absolute_path(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "other")
    result = _run("push", str(root / "sim" / "mload006"))
    assert result.exit_code == 0, result.output
    assert load_context(root).index.get("sim/mload006/d1") is not None


def test_push_quoted_glob_expands_from_the_current_directory(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("push", "mload00*")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload005/d1") is not None
    assert ctx.index.get("sim/mload006/d1") is not None
    assert ctx.index.get("other/x") is None


def test_push_outside_the_store_is_a_usage_error_naming_the_path(
    store, monkeypatch, tmp_path
) -> None:
    root, _ = store
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "f").write_bytes(b"f")
    monkeypatch.chdir(root / "sim")
    for arg in (str(outside / "f"), "../../elsewhere/f"):
        result = _run("push", arg)
        assert result.exit_code == 2, result.output
        assert arg in result.output
        assert "outside" in result.output


def test_push_missing_path_is_named_as_typed(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("push", "nope")
    assert result.exit_code == 2
    assert "'nope'" in result.output
    assert "sim/nope" not in result.output


def test_push_unmatched_pattern_is_named_as_typed(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("push", "zzz*")
    assert result.exit_code == 0, result.output
    assert "'zzz*'" in result.output
    assert "sim/" not in result.output


def test_pull_pattern_matches_the_index_relative_to_the_current_directory(
    store, monkeypatch
) -> None:
    root, _ = store
    assert _run("push", ".").exit_code == 0
    assert _run("offload", "--yes", "--min-age=0", ".").exit_code == 0
    assert not (root / "sim" / "mload005" / "d1").exists()
    monkeypatch.chdir(root / "sim")
    result = _run("pull", "mload005*")
    assert result.exit_code == 0, result.output
    assert (root / "sim" / "mload005" / "d1").exists()
    assert not (root / "sim" / "mload006" / "d1").exists()


def test_ls_table_prints_paths_as_typed_from_the_current_directory(store, monkeypatch) -> None:
    root, _ = store
    assert _run("push", ".").exit_code == 0
    monkeypatch.chdir(root / "sim")
    result = _run("ls", "mload005")
    assert result.exit_code == 0, result.output
    assert " mload005/d1 " in result.output
    assert "sim/mload005/d1" not in result.output


def test_ls_machine_formats_stay_store_relative_from_a_subdirectory(store, monkeypatch) -> None:
    import json

    root, _ = store
    assert _run("push", ".").exit_code == 0
    monkeypatch.chdir(root / "sim" / "mload005")
    plain = _run("ls", "--format", "plain", ".")
    assert plain.output.split("\t")[0] == "sim/mload005/d1"
    rows = json.loads(_run("ls", "--format", "json", ".").output)
    assert [r["path"] for r in rows] == ["sim/mload005/d1"]


def test_ls_from_the_root_prints_store_relative_paths_unchanged(store) -> None:
    assert _run("push", ".").exit_code == 0
    result = _run("ls", "--format", "plain", "sim/mload005")
    assert result.output.split("\t")[0] == "sim/mload005/d1"


def test_deinit_from_a_subdirectory_names_the_root_to_run_it_from(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("deinit", "--yes")
    assert result.exit_code == 2, result.output
    assert str(root.resolve()) in result.output
    assert (root / ".protonfs" / "config.json").exists()


def test_setup_still_creates_a_store_where_you_are(store, monkeypatch) -> None:
    root, _ = store
    seen: list[Path] = []
    monkeypatch.setattr(
        "protonfs.commands.setup.run_setup", lambda cwd, **kw: seen.append(cwd)
    )
    monkeypatch.chdir(root / "other")
    _run("setup", "--dry-run")
    assert seen == [(root / "other").resolve()]


def test_rm_path_is_relative_to_the_current_directory(store, monkeypatch) -> None:
    root, drive = store
    assert _run("push", ".").exit_code == 0
    monkeypatch.chdir(root / "sim")
    result = _run("rm", "-r", "--yes", "mload005")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload005/d1") is None
    assert ctx.index.get("sim/mload006/d1") is not None


def test_implicit_scope_is_the_whole_store_for_now(store, monkeypatch) -> None:
    """The single place that decides what a command with no PATH covers from a
    subdirectory (#232); ``None`` is the whole store."""
    root, _ = store
    ctx = load_context(root)
    assert _implicit_scope(ctx, root / "sim") is None
    assert _implicit_scope(ctx, root) is None


def test_status_without_a_path_from_a_subdirectory(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("status")
    assert result.exit_code == 1, result.output  # local-only files present: drift
    assert "local-only: 3" in result.output  # whole store, not just sim/


def test_symlinked_cwd_resolves_to_the_store(store, monkeypatch, tmp_path) -> None:
    root, _ = store
    link = tmp_path / "link"
    link.symlink_to(root / "sim")
    monkeypatch.chdir(link)
    assert os.getcwd() == str((root / "sim").resolve())
    result = _run("push", "mload005")
    assert result.exit_code == 0, result.output
    assert load_context(root).index.get("sim/mload005/d1") is not None


def test_item_narration_names_paths_as_typed_from_the_current_directory(
    store, monkeypatch
) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("-vv", "push", "mload005")
    assert result.exit_code == 0, result.output
    assert " mload005/d1" in result.output
    assert "sim/mload005/d1" not in result.output


def test_status_phase_names_the_subpath_as_typed(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("-v", "status", "mload005")
    assert "subpath=mload005" in result.output


def test_path_into_an_outer_store_is_outside_the_inner_one(store, monkeypatch) -> None:
    root, _ = store
    init_config(root / "sim", "/my-files/inner")
    monkeypatch.chdir(root / "sim" / "mload005")
    result = _run("push", "../../other")
    assert result.exit_code == 2, result.output
    assert str((root / "sim").resolve()) in result.output  # names the store it is outside of


def test_schedule_path_is_stored_relative_to_the_store(store, monkeypatch) -> None:
    root, _ = store
    seen: dict = {}

    class Job:
        id, cron, command, wrapper_path, log_path = "j", "* * * * *", "push", "w", "l"
        drive_cache_home = None

    def fake_add(repo, **kw):
        seen.update(repo=repo, **kw)
        return Job()

    monkeypatch.setattr("protonfs.commands.schedule.add_job", fake_add)
    monkeypatch.chdir(root / "sim")
    result = _run("schedule", "--add", "--every", "daily", "--path", "mload00*")
    assert result.exit_code == 0, result.output
    assert seen["repo"] == root.resolve()
    assert seen["path"] == "sim/mload00*"


def test_upgrade_acts_on_the_discovered_store(store, monkeypatch) -> None:
    root, _ = store
    seen: dict = {}
    monkeypatch.setattr(
        "protonfs.commands.upgrade.run_upgrade", lambda r, **kw: seen.update(upgrade=r) or 0
    )
    monkeypatch.chdir(root / "sim")
    assert _run("upgrade", "--check").exit_code == 0
    assert seen["upgrade"] == root.resolve()


def test_doctor_checks_the_discovered_store(store, monkeypatch) -> None:
    from protonfs.commands import doctor as doctor_mod

    class Drive:
        binary = "proton-drive"

        def binary_available(self) -> bool:
            return False

    root, _ = store
    seen: list = []
    monkeypatch.setattr(doctor_mod, "DriveClient", Drive)
    monkeypatch.setattr(doctor_mod, "is_linux", lambda: False)
    monkeypatch.setattr(doctor_mod, "repo_currency_checks", lambda r: seen.append(r) or [])
    monkeypatch.setattr(
        doctor_mod, "upstream_currency_check", lambda: doctor_mod.Check("u", True, "ok")
    )
    monkeypatch.chdir(root / "sim")
    doctor_mod.run_doctor()
    assert seen == [root.resolve()]


def test_setup_event_log_never_goes_to_an_enclosing_store(store, monkeypatch) -> None:
    root, _ = store
    seen: list = []
    monkeypatch.setattr(
        "protonfs.commands.setup.run_setup", lambda cwd, **kw: seen.append(cwd)
    )
    (root / "other" / ".protonfs").mkdir()  # a store being set up again, config not yet written
    monkeypatch.chdir(root / "other")
    result = _run("--event-log", "-vv", "setup", "--dry-run")
    assert result.exit_code == 0, result.output
    assert seen == [(root / "other").resolve()]
    assert (root / "other" / ".protonfs" / "events.log").exists()
    assert not (root / ".protonfs" / "events.log").exists()


def test_event_log_goes_to_the_discovered_store_for_other_commands(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "other")
    assert _run("--event-log", "status").exit_code in (0, 1)
    assert (root / ".protonfs" / "events.log").exists()
    assert not (root / "other" / ".protonfs").exists()


def test_dry_run_git_commands_are_pasteable_from_a_subdirectory(
    store, monkeypatch, tmp_path
) -> None:
    import subprocess

    root, _ = store
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "add", "sim/mload005/d1"], check=True
    )
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "x"], check=True)
    monkeypatch.chdir(root / "sim")
    result = _run("import", "--dry-run", "mload005")
    assert result.exit_code == 0, result.output
    cmds = [ln for ln in result.output.splitlines() if "git " in ln and "rm --cached" in ln]
    assert cmds, result.output
    assert f"git -C {root.resolve()} --literal-pathspecs rm --cached" in cmds[0]
    assert "[dry-run] import mload005/d1" in result.output


def test_hint_commands_name_paths_as_typed_from_the_current_directory(
    store, monkeypatch
) -> None:
    from protonfs.commands.gitmove import verify_exported

    root, _ = store
    assert _run("push", ".").exit_code == 0
    (root / "sim" / "mload005" / "d1").write_bytes(b"changed on disk")
    monkeypatch.chdir(root / "sim")
    failures = verify_exported(load_context(), ["sim/mload005/d1"])
    assert failures, "the edit must be detected"
    assert "`protonfs push mload005/d1`" in failures[0]["error"]
