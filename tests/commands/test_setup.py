# tests/commands/test_setup.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click
import pytest

from protonfs.commands import setup as setup_mod
from protonfs.commands.setup import (
    _append_gitignore,
    _ensure_lines,
    _untrack_lfs_patterns,
    clean_pointer_stubs,
    ensure_authenticated,
    ensure_cli_present,
    ensure_config,
    ensure_secrets,
    is_git_toplevel,
    maybe_uninstall_lfs_filters,
    migrate_lfs,
    run_setup,
    write_git_control_files,
)
from protonfs.config import load_config
from protonfs.context import RepoContext
from protonfs.drive import TransferResult
from protonfs.index import IndexStore
from protonfs.secretservice import SecretServiceError, SecretsResult


def _git_init(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)


def test_is_git_toplevel_true_at_repo_root(tmp_path: Path) -> None:
    _git_init(tmp_path)
    assert is_git_toplevel(tmp_path) is True


def test_is_git_toplevel_false_in_subdirectory(tmp_path: Path) -> None:
    _git_init(tmp_path)
    sub = tmp_path / "sim"
    sub.mkdir()
    # A subdir of a git repo is NOT the toplevel -> migration must not run there (#19).
    assert is_git_toplevel(sub) is False


def test_is_git_toplevel_false_when_not_a_git_repo(tmp_path: Path) -> None:
    assert is_git_toplevel(tmp_path) is False


def test_write_git_control_files_creates_exempting_attributes_and_gitignore(
    tmp_path: Path,
) -> None:
    write_git_control_files(tmp_path)

    attrs = (tmp_path / ".protonfs" / ".gitattributes").read_text()
    assert "!filter" in attrs and "!diff" in attrs and "!merge" in attrs  # exempt from LFS (#20)
    ignore = (tmp_path / ".protonfs" / ".gitignore").read_text()
    pattern_lines = [
        ln.strip()
        for ln in ignore.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert "index.json" in pattern_lines
    assert "refresh-state.json" in pattern_lines
    assert "config.local.json" in pattern_lines  # #21: per-device config, never committed
    # The shared contract stays tracked -- config.json / ignore must NOT be gitignored.
    assert "config.json" not in pattern_lines
    assert "ignore" not in pattern_lines


def test_write_git_control_files_is_idempotent_and_preserves_user_lines(tmp_path: Path) -> None:
    protonfs_dir = tmp_path / ".protonfs"
    protonfs_dir.mkdir()
    (protonfs_dir / ".gitignore").write_text("index.json\nmy-own-scratch/\n")

    write_git_control_files(tmp_path)

    ignore = (protonfs_dir / ".gitignore").read_text()
    assert ignore.count("index.json") == 1  # not duplicated
    assert "my-own-scratch/" in ignore  # user's line preserved
    assert "refresh-state.json" in ignore  # missing managed line appended


def test_ensure_cli_present_raises_when_missing(make_fake_drive) -> None:
    with pytest.raises(click.ClickException):
        ensure_cli_present(make_fake_drive(version=None))


def test_ensure_cli_present_returns_version_string(make_fake_drive) -> None:
    assert ensure_cli_present(make_fake_drive(version="v0.4.6")) == "v0.4.6"


def test_ensure_authenticated_raises_when_not_authed(make_fake_drive) -> None:
    with pytest.raises(click.ClickException):
        ensure_authenticated(make_fake_drive(authed=False))


def test_ensure_config_reuses_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from protonfs.config import init_config

    existing = init_config(tmp_path, "/my-files/existing")

    def _fail_if_prompted(*args, **kwargs):
        raise AssertionError("should not prompt when config already exists")

    monkeypatch.setattr(click, "prompt", _fail_if_prompted)
    result = ensure_config(tmp_path)
    assert result.remote_root == "/my-files/existing"
    assert result.device_id == existing.device_id


def test_ensure_config_prompts_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(click, "prompt", lambda *a, **k: "/my-files/new")
    result = ensure_config(tmp_path)
    assert result.remote_root == "/my-files/new"
    assert load_config(tmp_path) is not None


def test_ensure_config_new_repo_writes_device_id_to_local_file_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(click, "prompt", lambda *a, **k: "/my-files/new")
    ensure_config(tmp_path)
    shared_on_disk = json.loads((tmp_path / ".protonfs" / "config.json").read_text())
    assert "device_id" not in shared_on_disk
    assert (tmp_path / ".protonfs" / "config.local.json").exists()


def test_ensure_config_migrates_old_layout_device_id_to_local(tmp_path: Path) -> None:
    from protonfs.config import Config, load_local_config, save_config

    save_config(tmp_path, Config(remote_root="/my-files/old", device_id="old-device"))

    result = ensure_config(tmp_path)

    assert result.remote_root == "/my-files/old"
    assert result.device_id == "old-device"
    shared_on_disk = json.loads((tmp_path / ".protonfs" / "config.json").read_text())
    assert "device_id" not in shared_on_disk
    assert load_local_config(tmp_path)["device_id"] == "old-device"


def test_migrate_lfs_is_noop_when_not_lfs_tracked(tmp_path: Path, make_fake_drive) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    performed = migrate_lfs(ctx, dry_run=False)

    assert performed is False


def test_migrate_lfs_dry_run_reports_without_acting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0),
    )

    performed = migrate_lfs(ctx, dry_run=True)

    assert performed is True
    assert calls == []  # dry-run must not invoke git at all
    assert (
        tmp_path / ".gitattributes"
    ).read_text() == "sim/*/* filter=lfs diff=lfs merge=lfs -text\n"


def test_migrate_lfs_full_success_mutates_git_only_after_upload_and_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    # Single shared event log so we can prove push happened strictly before
    # any git mutation (add / rm --cached / commit).
    events: list[tuple] = []

    def fake_push_files(*args, **kwargs):
        events.append(("push",))
        return TransferResult(3, 0, 0, [])

    monkeypatch.setattr("protonfs.commands.setup.push_files", fake_push_files)

    confirm_calls = []

    def fake_confirm(*args, **kwargs):
        confirm_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(click, "confirm", fake_confirm)

    def fake_run(cmd, *args, **kwargs):
        events.append(("run", cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    performed = migrate_lfs(ctx, dry_run=False)

    assert performed is True
    assert confirm_calls  # click.confirm(..., abort=True) was invoked
    assert any(e[0] == "push" for e in events)  # push_files was called

    push_index = next(i for i, e in enumerate(events) if e[0] == "push")
    mutation_indices = [
        i
        for i, e in enumerate(events)
        if e[0] == "run" and e[1][3] in ("add", "rm", "commit")
    ]
    assert mutation_indices  # git add/rm --cached/commit all ran
    assert all(push_index < i for i in mutation_indices)

    assert "filter=lfs" not in (tmp_path / ".gitattributes").read_text()


def test_migrate_lfs_confirm_declined_leaves_git_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    monkeypatch.setattr(
        "protonfs.commands.setup.push_files",
        lambda *a, **k: TransferResult(3, 0, 0, []),
    )

    def fake_confirm(*args, **kwargs):
        raise click.exceptions.Abort()

    monkeypatch.setattr(click, "confirm", fake_confirm)

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(click.exceptions.Abort):
        migrate_lfs(ctx, dry_run=False)

    assert (
        tmp_path / ".gitattributes"
    ).read_text() == "sim/*/* filter=lfs diff=lfs merge=lfs -text\n"
    mutation_calls = [cmd for cmd in calls if cmd[3] in ("add", "rm", "commit")]
    assert mutation_calls == []


def test_append_gitignore_adds_pattern_that_is_substring_of_existing_line(
    tmp_path: Path,
) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("sim/build_output/\n")

    _append_gitignore(tmp_path, ["sim/"])

    lines = gitignore.read_text().splitlines()
    assert "sim/build_output/" in lines
    assert "sim/" in lines  # must be added on its own line, not skipped as a substring


def test_migrate_lfs_wraps_git_mutation_failure_in_click_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    monkeypatch.setattr(
        "protonfs.commands.setup.push_files",
        lambda *a, **k: TransferResult(3, 0, 0, []),
    )
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)

    def fake_run(cmd, *args, **kwargs):
        # First git-mutation call (`git add ...`) fails; everything before it
        # (git lfs pull, push_files) is unaffected since it's monkeypatched away.
        if cmd[3] == "add":
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(click.ClickException) as excinfo:
        migrate_lfs(ctx, dry_run=False)

    assert not isinstance(excinfo.value, subprocess.CalledProcessError)
    message = str(excinfo.value)
    assert "git" in message.lower()
    assert "Drive" in message


def test_clean_pointer_stubs_removes_stub_files(tmp_path: Path) -> None:
    stub = tmp_path / "dump_0001"
    stub.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 1\n")
    real = tmp_path / "dump_0002"
    real.write_bytes(b"real data")

    removed = clean_pointer_stubs(tmp_path)

    assert removed == 1
    assert not stub.exists()
    assert real.exists()


# --- ensure_secrets -------------------------------------------------------------------


def test_ensure_secrets_echoes_actions_and_warnings(
    monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(setup_mod.click, "echo", lambda msg="": lines.append(msg))
    monkeypatch.setattr(
        setup_mod,
        "ensure_secret_service",
        lambda: SecretsResult(
            env={}, ready=True, actions=["started keyring"], warnings=["isolated keyring"]
        ),
    )

    ensure_secrets(make_fake_drive())

    assert any("keyring: started keyring" in line for line in lines)
    assert any("! isolated keyring" in line for line in lines)


def test_ensure_secrets_wraps_failure_in_click_exception(
    monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    def _boom():
        raise SecretServiceError("no usable keyring")

    monkeypatch.setattr(setup_mod, "ensure_secret_service", _boom)

    with pytest.raises(click.ClickException) as excinfo:
        ensure_secrets(make_fake_drive())
    message = str(excinfo.value)
    assert "no usable keyring" in message
    assert "protonfs doctor" in message


# --- _ensure_lines / _untrack_lfs_patterns / _append_gitignore edge branches ----------


def test_ensure_lines_appends_newline_before_missing_when_file_lacks_trailing_newline(
    tmp_path: Path,
) -> None:
    target = tmp_path / "f"
    target.write_text("keep-me")  # no trailing newline

    wrote = _ensure_lines(target, "keep-me\nadd-me\n")

    assert wrote is True
    # The missing line is appended on its own line, not glued onto "keep-me".
    assert target.read_text().splitlines() == ["keep-me", "add-me"]


def test_ensure_lines_returns_false_when_all_lines_present(tmp_path: Path) -> None:
    target = tmp_path / "f"
    _ensure_lines(target, "a\nb\n")  # first write
    assert _ensure_lines(target, "a\nb\n") is False  # nothing missing on the second pass


def test_untrack_lfs_patterns_returns_empty_when_no_gitattributes(tmp_path: Path) -> None:
    assert _untrack_lfs_patterns(tmp_path) == []


def test_untrack_lfs_patterns_keeps_non_lfs_lines(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text(
        "*.txt text\n" "sim/*/* filter=lfs diff=lfs merge=lfs -text\n" "*.md text\n"
    )

    removed = _untrack_lfs_patterns(tmp_path)

    assert removed == ["sim/*/*"]
    kept = (tmp_path / ".gitattributes").read_text()
    assert "*.txt text" in kept and "*.md text" in kept
    assert "filter=lfs" not in kept


def test_append_gitignore_noop_when_all_patterns_present(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("sim/\n")

    _append_gitignore(tmp_path, ["sim/"])  # already present -> nothing to add

    assert gitignore.read_text() == "sim/\n"


def test_append_gitignore_inserts_newline_when_file_lacks_trailing_newline(
    tmp_path: Path,
) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("existing")  # no trailing newline

    _append_gitignore(tmp_path, ["sim/"])

    assert gitignore.read_text().splitlines() == ["existing", "sim/"]


# --- migrate_lfs failed-upload guard --------------------------------------------------


def test_migrate_lfs_aborts_when_upload_reports_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    from protonfs.config import init_config

    config = init_config(tmp_path, "/my-files/test")
    ctx = RepoContext(
        root=tmp_path, config=config, index=IndexStore(tmp_path), drive=make_fake_drive()
    )

    # One file failed to upload -> migration must abort BEFORE touching git tracking.
    monkeypatch.setattr(
        "protonfs.commands.setup.push_files",
        lambda *a, **k: TransferResult(2, 0, 1, [{"name": "x", "error": "boom"}]),
    )
    mutated: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *a, **k: mutated.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    with pytest.raises(click.ClickException) as excinfo:
        migrate_lfs(ctx, dry_run=False)

    assert "failed to upload" in str(excinfo.value)
    # .gitattributes still carries the LFS rule -- nothing was untracked.
    assert "filter=lfs" in (tmp_path / ".gitattributes").read_text()


# --- maybe_uninstall_lfs_filters ------------------------------------------------------


def test_maybe_uninstall_lfs_filters_noop_when_lfs_rules_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs -text\n")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *a, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    maybe_uninstall_lfs_filters(tmp_path)

    assert calls == []  # LFS rules still present -> git untouched


def test_maybe_uninstall_lfs_filters_noop_when_git_lfs_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".gitattributes").write_text("*.txt text\n")  # no LFS rules

    def fake_run(cmd, *a, **k):
        # `git lfs env` returns nonzero when git-lfs is not installed at all.
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    maybe_uninstall_lfs_filters(tmp_path)  # must not raise / must not call uninstall


def test_maybe_uninstall_lfs_filters_runs_uninstall_when_no_rules_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".gitattributes").write_text("*.txt text\n")  # no LFS rules
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)  # `git lfs env` succeeds -> installed

    monkeypatch.setattr(subprocess, "run", fake_run)

    maybe_uninstall_lfs_filters(tmp_path)

    assert any(cmd[-1] == "uninstall" for cmd in calls)


# --- run_setup end-to-end wiring ------------------------------------------------------
#
# The guards (ensure_cli_present/ensure_secrets/ensure_authenticated/ensure_config) are
# tested individually above; here we fake them and drive run_setup's tail: remote-root
# creation, the migrate/skip branching (#19), and the pointer-stub/uninstall cleanup.


def _stub_setup_guards(monkeypatch: pytest.MonkeyPatch, config) -> None:
    monkeypatch.setattr(setup_mod, "ensure_cli_present", lambda drive: "v0.5.0")
    monkeypatch.setattr(setup_mod, "ensure_secrets", lambda drive: None)
    monkeypatch.setattr(setup_mod, "ensure_authenticated", lambda drive: None)
    monkeypatch.setattr(setup_mod, "ensure_config", lambda root: config)
    monkeypatch.setattr(setup_mod, "init_ignore", lambda root: None)
    monkeypatch.setattr(setup_mod, "init_include", lambda root: None)
    monkeypatch.setattr(setup_mod, "write_git_control_files", lambda root: None)
    monkeypatch.setattr(setup_mod, "DriveClient", lambda: None)


def test_run_setup_skips_migration_with_no_migrate_lfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from protonfs.config import Config

    config = Config(remote_root="/my-files/test", device_id="d1")
    _stub_setup_guards(monkeypatch, config)
    ensured: list[object] = []
    monkeypatch.setattr(setup_mod, "ensure_remote_root", lambda ctx: ensured.append(ctx))
    # migrate must NOT be consulted when explicitly disabled.
    monkeypatch.setattr(
        setup_mod, "migrate_lfs", lambda *a, **k: pytest.fail("migration must not run")
    )
    lines: list[str] = []
    monkeypatch.setattr(setup_mod.click, "echo", lambda msg="": lines.append(msg))

    run_setup(tmp_path, dry_run=False, migrate=False)

    assert ensured  # remote root was ensured (not a dry run)
    assert any("--no-migrate-lfs" in line for line in lines)
    assert any("setup complete" in line for line in lines)


def test_run_setup_skips_migration_when_not_git_toplevel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from protonfs.config import Config

    config = Config(remote_root="/my-files/test", device_id="d1")
    _stub_setup_guards(monkeypatch, config)
    monkeypatch.setattr(setup_mod, "ensure_remote_root", lambda ctx: None)
    monkeypatch.setattr(setup_mod, "is_git_toplevel", lambda root: False)
    monkeypatch.setattr(
        setup_mod, "migrate_lfs", lambda *a, **k: pytest.fail("migration must not run")
    )
    lines: list[str] = []
    monkeypatch.setattr(setup_mod.click, "echo", lambda msg="": lines.append(msg))

    run_setup(tmp_path, dry_run=False, migrate=None)

    assert any("not the git toplevel" in line for line in lines)


def test_run_setup_runs_migration_and_cleanup_when_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from protonfs.config import Config

    config = Config(remote_root="/my-files/test", device_id="d1")
    _stub_setup_guards(monkeypatch, config)
    monkeypatch.setattr(setup_mod, "ensure_remote_root", lambda ctx: None)
    monkeypatch.setattr(setup_mod, "migrate_lfs", lambda ctx, dry_run: True)
    monkeypatch.setattr(setup_mod, "clean_pointer_stubs", lambda root: 2)
    uninstalled: list[Path] = []
    monkeypatch.setattr(setup_mod, "maybe_uninstall_lfs_filters", uninstalled.append)
    lines: list[str] = []
    monkeypatch.setattr(setup_mod.click, "echo", lambda msg="": lines.append(msg))

    run_setup(tmp_path, dry_run=False, migrate=True)

    assert uninstalled == [tmp_path]
    assert any("Removed 2 leftover" in line for line in lines)


def test_run_setup_reports_no_stubs_when_migration_finds_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from protonfs.config import Config

    config = Config(remote_root="/my-files/test", device_id="d1")
    _stub_setup_guards(monkeypatch, config)
    monkeypatch.setattr(setup_mod, "ensure_remote_root", lambda ctx: None)
    monkeypatch.setattr(setup_mod, "migrate_lfs", lambda ctx, dry_run: True)
    monkeypatch.setattr(setup_mod, "clean_pointer_stubs", lambda root: 0)  # migration, but no stubs
    monkeypatch.setattr(setup_mod, "maybe_uninstall_lfs_filters", lambda root: None)
    lines: list[str] = []
    monkeypatch.setattr(setup_mod.click, "echo", lambda msg="": lines.append(msg))

    run_setup(tmp_path, dry_run=False, migrate=True)

    assert any("No leftover git-lfs pointer stubs found" in line for line in lines)


def test_run_setup_dry_run_skips_remote_root_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from protonfs.config import Config

    config = Config(remote_root="/my-files/test", device_id="d1")
    _stub_setup_guards(monkeypatch, config)
    monkeypatch.setattr(
        setup_mod, "ensure_remote_root", lambda ctx: pytest.fail("dry run must not touch Drive")
    )
    monkeypatch.setattr(setup_mod, "is_git_toplevel", lambda root: True)
    monkeypatch.setattr(setup_mod, "migrate_lfs", lambda ctx, dry_run: True)
    monkeypatch.setattr(
        setup_mod, "clean_pointer_stubs", lambda root: pytest.fail("dry run must not clean stubs")
    )
    lines: list[str] = []
    monkeypatch.setattr(setup_mod.click, "echo", lambda msg="": lines.append(msg))

    run_setup(tmp_path, dry_run=True, migrate=None)

    assert any("setup complete" in line for line in lines)


def test_run_setup_narrates_steps_through_reporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recording_reporter_cls
) -> None:
    from protonfs.config import Config

    config = Config(remote_root="/my-files/test", device_id="d1")
    _stub_setup_guards(monkeypatch, config)
    monkeypatch.setattr(setup_mod, "ensure_remote_root", lambda ctx: None)
    monkeypatch.setattr(setup_mod, "migrate_lfs", lambda ctx, dry_run: False)
    monkeypatch.setattr(setup_mod.click, "echo", lambda msg="": None)

    reporter = recording_reporter_cls()
    run_setup(tmp_path, dry_run=False, migrate=True, reporter=reporter)

    phases = [name for kind, name in reporter.calls if kind == "phase"]
    assert phases == [
        "checking proton-drive CLI",
        "preparing keyring",
        "checking authentication",
        "writing config + control files",
        "ensuring remote root",
        "git-LFS migration",
    ]
    assert ("done", "setup complete") in reporter.calls
