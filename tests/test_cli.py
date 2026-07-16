from pathlib import Path

from click.testing import CliRunner

from protonfs.cli import main
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import RemoteEntry
from protonfs.index import IndexEntry


def test_help() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Sync a local directory tree with Proton Drive" in result.output


def test_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "protonfs" in result.output


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


# The mutating commands run their work under the repo lock (#2); these happy-path CLI
# tests exercise that wiring end-to-end (acquire + release around the command body).


def test_cli_status_exit_code_clean(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    # No files, nothing tracked -> clean -> exit 0.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["status"])

    assert result.exit_code == 0, result.output


def test_cli_status_exit_code_drift(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    # An untracked local file is local-only drift -> exit 1.
    (tmp_path / "new_file").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["status"])

    assert result.exit_code == 1, result.output
    assert "local-only: 1" in result.output


def test_cli_refresh_runs_under_lock(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(walk_entries=[RemoteEntry("f", False, 3)])
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["refresh"])

    assert result.exit_code == 0, result.output


def test_cli_pull_refresh_runs_under_lock(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(walk_entries=[RemoteEntry("f", False, 3)])
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["pull", "--refresh"])

    assert result.exit_code == 0, result.output


def test_cli_rm_runs_under_lock(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("f", _tracked_entry("/my-files/test/f"))
    ctx.index.save()
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["rm", "f", "--yes"])

    assert result.exit_code == 0, result.output
    assert ctx.drive.trashed == ["/my-files/test/f"]


def test_cli_restore_runs_under_lock(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["restore", "f"])

    assert result.exit_code == 0, result.output
