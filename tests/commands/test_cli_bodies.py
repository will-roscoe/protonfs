# tests/commands/test_cli_bodies.py
"""CLI wrapper bodies for commands whose happy-path/branch output was uncovered (#88).

These are the thin `cli.py` command functions that delegate to a command module. The
delegated logic is unit-tested in its own module; here we drive each wrapper through
`CliRunner` to lock in the wiring: option parsing, exit codes, and the per-branch
console output the wrapper itself is responsible for printing.

Pattern: the CLI imports its collaborators lazily inside each command
(`from protonfs.commands.X import Y`), so we monkeypatch them at their source module
(`protonfs.commands.X.Y`) and fake `load_context` to skip real Drive/config I/O.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from protonfs.cli import main
from protonfs.commands.offload import OffloadResult
from protonfs.commands.refresh import RefreshResult
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import TransferResult
from protonfs.index import IndexEntry


def _ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive, **drive_kwargs):
    """A real RepoContext (so repo_lock has a valid .protonfs root) with a fake drive,
    wired in as the return of load_context everywhere the CLI resolves it."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(**drive_kwargs)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    return ctx


def _tracked_entry(remote_path: str) -> IndexEntry:
    return IndexEntry(
        size=1,
        mtime=1.0,
        sha256="h",
        sha1="",
        remote_path=remote_path,
        origin_device="d1",
        local_state="present",
        last_synced="2026-07-08T00:00:00+00:00",
    )


# --- deinit ---------------------------------------------------------------------------


def test_cli_deinit_runs_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_config(tmp_path, "/my-files/test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("protonfs.commands.deinit.ensure_deinit_target", lambda root: None)
    removed: list[tuple] = []
    monkeypatch.setattr(
        "protonfs.commands.deinit.run_deinit",
        lambda root, dry_run, yes: removed.append((root, dry_run, yes)),
    )

    result = CliRunner().invoke(main, ["deinit", "--yes"])

    assert result.exit_code == 0, result.output
    assert removed and removed[0][2] is True  # yes=True threaded through


# --- ls -------------------------------------------------------------------------------


def test_cli_ls_delegates_to_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    _ctx(tmp_path, monkeypatch, make_fake_drive)
    calls: list[tuple] = []
    monkeypatch.setattr(
        "protonfs.commands.ls.render_ls",
        # **kwargs absorbs the presentation options (dirs/states/fmt/echo, #97).
        lambda ctx, path, remote, trash, console, **kwargs: calls.append((path, remote, trash)),
    )

    result = CliRunner().invoke(main, ["ls", "sub", "--remote", "--trash"])

    assert result.exit_code == 0, result.output
    assert calls == [("sub", True, True)]


# --- push failure branch --------------------------------------------------------------


def test_cli_push_reports_failures_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    _ctx(tmp_path, monkeypatch, make_fake_drive)
    monkeypatch.setattr(
        "protonfs.commands.push.push",
        lambda ctx, path, resolve, dry_run, **kwargs: TransferResult(
            0, 0, 1, [{"name": "f", "error": "boom", "kind": "conflict"}]
        ),
    )

    result = CliRunner().invoke(main, ["push"])

    assert result.exit_code == 1
    assert "FAILED f: boom" in result.output
    assert "--resolve=merge|keep-both|replace|skip" in result.output


# --- pull failure branch --------------------------------------------------------------


def test_cli_pull_reports_failures_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    ctx = _ctx(tmp_path, monkeypatch, make_fake_drive)
    ctx.index.set("f", _tracked_entry("/my-files/test/f"))  # non-empty index -> no early return
    ctx.index.save()
    monkeypatch.setattr(
        "protonfs.commands.pull.pull",
        lambda ctx, path, resolve, dry_run, refresh, **kwargs: TransferResult(
            0, 0, 1, [{"name": "f", "error": "network"}]
        ),
    )

    result = CliRunner().invoke(main, ["pull"])

    assert result.exit_code == 1
    assert "FAILED f: network" in result.output


def test_cli_pull_empty_index_prompts_for_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    _ctx(tmp_path, monkeypatch, make_fake_drive)

    result = CliRunner().invoke(main, ["pull"])

    assert result.exit_code == 0
    assert "index empty" in result.output


# --- offload --------------------------------------------------------------------------


def test_cli_offload_prints_every_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    _ctx(tmp_path, monkeypatch, make_fake_drive)
    monkeypatch.setattr(
        "protonfs.commands.offload.offload",
        lambda ctx, path, verify, dry_run: OffloadResult(
            offloaded=1,
            skipped_unverified=1,
            skipped_modified=1,
            bytes_reclaimed=1024,
            offloaded_paths=["a"],
            skipped_paths=["b"],
            modified_paths=["c"],
        ),
    )

    result = CliRunner().invoke(main, ["offload", "--yes"])

    assert result.exit_code == 0, result.output
    assert "offloaded=1 bytes_reclaimed=1024" in result.output
    assert "could not be confirmed on the remote" in result.output
    assert "unsynced local edits" in result.output


def test_cli_offload_confirmation_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    _ctx(tmp_path, monkeypatch, make_fake_drive)
    monkeypatch.setattr(
        "protonfs.commands.offload.offload",
        lambda *a, **k: pytest.fail("offload must not run when the prompt is declined"),
    )

    # No --yes, not a dry run: declining the confirm aborts (Click exit code 1).
    result = CliRunner().invoke(main, ["offload"], input="n\n")

    assert result.exit_code == 1


# --- refresh branches -----------------------------------------------------------------


def test_cli_refresh_prints_changed_and_deleted_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    _ctx(tmp_path, monkeypatch, make_fake_drive)
    monkeypatch.setattr(
        "protonfs.commands.refresh.refresh",
        lambda ctx, path, prune: RefreshResult(
            seeded=1,
            remote_changed=1,
            remote_deleted=1,
            changed_paths=["changed.txt"],
            deleted_paths=["gone.txt"],
        ),
    )

    result = CliRunner().invoke(main, ["refresh"])

    assert result.exit_code == 0, result.output
    assert "changed on the remote" in result.output
    assert "changed.txt" in result.output
    assert "deleted on the remote (found)" in result.output  # prune=False -> "found"
    assert "refresh --prune" in result.output  # hint only shown when not pruning


# --- install-drive --------------------------------------------------------------------


def test_cli_install_drive_prints_warnings_and_keyring_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protonfs.install import InstallResult
    from protonfs.secretservice import SecretsResult

    monkeypatch.setattr(
        "protonfs.install.install_drive",
        lambda version=None: InstallResult(
            path=Path("/opt/proton-drive"), on_path=False, sha512="abc", warnings=["not on PATH"]
        ),
    )
    monkeypatch.setattr(
        "protonfs.secretservice.ensure_secret_service",
        lambda: SecretsResult(
            env={}, ready=True, actions=["started keyring"], warnings=["isolated"]
        ),
    )

    result = CliRunner().invoke(main, ["install-drive"])

    assert result.exit_code == 0, result.output
    assert "SHA-512 verified" in result.output
    assert "! not on PATH" in result.output
    assert "keyring: started keyring" in result.output
    assert "auth login" in result.output


def test_cli_install_drive_skip_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    from protonfs.install import InstallResult

    monkeypatch.setattr(
        "protonfs.install.install_drive",
        lambda version=None: InstallResult(
            path=Path("/opt/proton-drive"), on_path=True, sha512="abc"
        ),
    )
    monkeypatch.setattr(
        "protonfs.secretservice.ensure_secret_service",
        lambda: pytest.fail("--skip-keyring must not touch the keyring"),
    )

    result = CliRunner().invoke(main, ["install-drive", "--skip-keyring"])

    assert result.exit_code == 0, result.output


def test_cli_install_drive_keyring_failure_is_click_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protonfs.install import InstallResult
    from protonfs.secretservice import SecretServiceError

    monkeypatch.setattr(
        "protonfs.install.install_drive",
        lambda version=None: InstallResult(
            path=Path("/opt/proton-drive"), on_path=True, sha512="abc"
        ),
    )

    def _boom():
        raise SecretServiceError("no usable keyring")

    monkeypatch.setattr("protonfs.secretservice.ensure_secret_service", _boom)

    result = CliRunner().invoke(main, ["install-drive"])

    assert result.exit_code != 0
    assert "no usable keyring" in result.output
    assert "protonfs doctor" in result.output


# --- upgrade --------------------------------------------------------------------------


def test_cli_upgrade_mutually_exclusive_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(main, ["upgrade", "--drive-only", "--repo-only"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_cli_upgrade_nonzero_check_code_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "protonfs.commands.upgrade.run_upgrade",
        lambda root, check, drive_only, repo_only: 1,
    )
    result = CliRunner().invoke(main, ["upgrade", "--check"])
    assert result.exit_code == 1


def test_cli_upgrade_install_error_is_click_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from protonfs.install import InstallError

    def _boom(*a, **k):
        raise InstallError("checksum mismatch")

    monkeypatch.setattr("protonfs.commands.upgrade.run_upgrade", _boom)

    result = CliRunner().invoke(main, ["upgrade"])

    assert result.exit_code != 0
    assert "checksum mismatch" in result.output


# --- doctor / shell-init --------------------------------------------------------------


def test_cli_doctor_exit_code_reflects_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("protonfs.commands.doctor.doctor", lambda fix: False)
    assert CliRunner().invoke(main, ["doctor"]).exit_code == 1

    monkeypatch.setattr("protonfs.commands.doctor.doctor", lambda fix: True)
    assert CliRunner().invoke(main, ["doctor", "--fix"]).exit_code == 0


def test_cli_shell_init_prints_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "protonfs.commands.doctor.shell_exports",
        lambda: ["DBUS_SESSION_BUS_ADDRESS=unix:abstract=/tmp/bus"],
    )

    result = CliRunner().invoke(main, ["shell-init"])

    assert result.exit_code == 0
    assert "export DBUS_SESSION_BUS_ADDRESS=unix:abstract=/tmp/bus" in result.output


# --- trash group ----------------------------------------------------------------------


def test_cli_trash_list_delegates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    _ctx(tmp_path, monkeypatch, make_fake_drive)
    calls: list[object] = []
    monkeypatch.setattr(
        "protonfs.commands.trash.list_trash", lambda ctx, console: calls.append(ctx)
    )

    result = CliRunner().invoke(main, ["trash", "list"])

    assert result.exit_code == 0, result.output
    assert calls


def test_cli_trash_empty_delegates_with_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    _ctx(tmp_path, monkeypatch, make_fake_drive)
    calls: list[bool] = []
    monkeypatch.setattr(
        "protonfs.commands.trash.empty_trash", lambda ctx, confirmed: calls.append(confirmed)
    )

    result = CliRunner().invoke(main, ["trash", "empty", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == [True]
