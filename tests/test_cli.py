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


# --- #92: multiple pathspecs (shell globs expand to several arguments) ----------------


def test_normalize_paths_empty_means_whole_repo() -> None:
    from protonfs.cli import _normalize_paths

    assert _normalize_paths(()) == [None]


def test_normalize_paths_dot_or_root_subsumes_everything() -> None:
    from protonfs.cli import _normalize_paths

    assert _normalize_paths((".", "a")) == [None]
    assert _normalize_paths(("a", "/")) == [None]


def test_normalize_paths_dedupes_and_drops_nested() -> None:
    from protonfs.cli import _normalize_paths

    # duplicates collapse; a path nested inside another given path is dropped
    # (it would be processed twice); order of the surviving roots is preserved.
    assert _normalize_paths(("a/", "a", "a/b", "c")) == ["a", "c"]
    # nesting is detected regardless of argument order
    assert _normalize_paths(("a/b", "a")) == ["a"]
    # sibling with a common name prefix is NOT nested
    assert _normalize_paths(("a", "ab")) == ["a", "ab"]


def test_cli_pull_accepts_multiple_paths_from_a_glob(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    """#92 repro: `protonfs pull 03pol02*` arrives as several arguments; previously a
    Click usage error ("Got unexpected extra arguments"), now each path is pulled."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    for rel in ("03pol021/dump", "03pol022/dump", "elsewhere/dump"):
        ctx.index.set(rel, _tracked_entry(f"/my-files/test/{rel}"))
    # entries are `present` in the index but absent on disk -> classify REMOTE_ONLY
    ctx.index.save()
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["pull", "03pol021", "03pol022"])

    assert result.exit_code == 0, result.output
    assert "transferred=2" in result.output
    downloaded = [p for call in ctx.drive.download_calls for p in call[0]]
    assert sorted(downloaded) == [
        "/my-files/test/03pol021/dump",
        "/my-files/test/03pol022/dump",
    ]


def test_cli_status_combines_counts_across_paths(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "f1").write_bytes(b"x")
    (tmp_path / "b" / "f2").write_bytes(b"y")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["status", "a", "b"])

    assert result.exit_code == 1, result.output  # drift: two local-only files
    assert "local-only: 2" in result.output


def test_cli_rm_accepts_multiple_paths(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    for rel in ("f1", "f2"):
        ctx.index.set(rel, _tracked_entry(f"/my-files/test/{rel}"))
    ctx.index.save()
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["rm", "f1", "f2", "--yes"])

    assert result.exit_code == 0, result.output
    assert ctx.drive.trashed == ["/my-files/test/f1", "/my-files/test/f2"]


def test_cli_rm_still_requires_at_least_one_path() -> None:
    result = CliRunner().invoke(main, ["rm"])
    assert result.exit_code == 2  # usage error, unchanged from the 1.0 contract


# --- #97: --format on status / ls flags -------------------------------------------------


def test_cli_status_format_json(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    import json

    (tmp_path / "new_file").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["status", "--format", "json"])

    assert result.exit_code == 1  # drift exit code is preserved in json mode
    payload = json.loads(result.output)
    assert payload["counts"]["local-only"] == 1
    assert payload["exit_code"] == 1


def test_cli_ls_dirs_state_and_format_flags(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    import json

    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "f").write_bytes(b"12345")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(
        main, ["ls", "--dirs", "--state", "local-only", "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == [
        {
            "path": "run1",
            "files": 1,
            "local_bytes": 5,
            "indexed_bytes": 0,
            "apparent_bytes": 5,  # local-only file: apparent size falls back to local
            "states": {"local-only": 1},
        }
    ]


# --- #94: ls --visual storage charts ---------------------------------------------------


def test_cli_ls_visual_treemap_renders(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "f").write_bytes(b"x" * 500)
    (tmp_path / "small").mkdir()
    (tmp_path / "small" / "g").write_bytes(b"y" * 20)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["ls", "--visual", "treemap"], color=True)

    assert result.exit_code == 0, result.output
    assert "big" in result.output and "small" in result.output


def test_cli_ls_visual_waffle_renders(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    (tmp_path / "d1").mkdir()
    (tmp_path / "d1" / "f").write_bytes(b"x" * 100)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["ls", "--visual", "waffle"], color=True)

    assert result.exit_code == 0, result.output
    assert "d1" in result.output


def test_cli_ls_visual_rejects_json_format(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["ls", "--visual", "treemap", "--format", "json"])

    assert result.exit_code == 2  # usage error
    assert "cannot be combined with --format" in result.output


def test_cli_ls_visual_rejects_trash(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["ls", "--visual", "waffle", "--trash"])

    assert result.exit_code == 2
    assert "nothing to chart" in result.output


def test_cli_verbose_count_configures_reporter(tmp_path, monkeypatch, make_fake_drive) -> None:
    """``-vv`` on any subcommand configures a Reporter at level 2 before it runs."""
    from collections import Counter

    from protonfs.reporting import get_reporter

    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    captured = {}

    def _fake_compute_status(c, p):
        captured["lvl"] = get_reporter().level
        return Counter()

    monkeypatch.setattr("protonfs.commands.status.compute_status", _fake_compute_status)
    CliRunner().invoke(main, ["-vv", "status"])
    assert captured["lvl"] == 2


def test_cli_event_log_flag_writes_file(tmp_path, monkeypatch, make_fake_drive) -> None:
    """``--event-log`` on the group makes the subcommand write ``.protonfs/events.log``."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["-v", "--event-log", "status"])
    assert (tmp_path / ".protonfs" / "events.log").exists()


def test_cli_no_verbose_stdout_unchanged(tmp_path, monkeypatch, make_fake_drive) -> None:
    """Regression: default invocation still prints exactly the state counts on stdout."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    result = CliRunner().invoke(main, ["status"])
    assert "synced: 0" in result.output
