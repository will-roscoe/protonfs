from pathlib import Path

import pytest
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


# --- #131: PATH arguments that are glob patterns, expanded by protonfs itself (not the
# shell), so a scheduled job's quoted pattern keeps matching as new directories appear ---


def test_is_pattern_detects_glob_metacharacters() -> None:
    from protonfs.cli import _is_pattern

    assert _is_pattern("mload*") is True
    assert _is_pattern("a?b") is True
    assert _is_pattern("a[bc]d") is True
    assert _is_pattern("mload003_calib_1m") is False
    assert _is_pattern("a/b") is False


def test_expand_patterns_leaves_literal_paths_untouched() -> None:
    from protonfs.cli import _expand_patterns

    expanded, unmatched = _expand_patterns(("a", "b"), matcher=lambda pat: ["should not be called"])

    assert expanded == ("a", "b")
    assert unmatched == []


def test_expand_patterns_splices_in_matcher_results() -> None:
    from protonfs.cli import _expand_patterns

    matcher = lambda pat: ["mload003_calib_1m", "mload003_calib_2m"]  # noqa: E731

    expanded, unmatched = _expand_patterns(("mload*", "literal"), matcher=matcher)

    assert expanded == ("mload003_calib_1m", "mload003_calib_2m", "literal")
    assert unmatched == []


def test_expand_patterns_records_zero_match_patterns_as_unmatched() -> None:
    from protonfs.cli import _expand_patterns

    expanded, unmatched = _expand_patterns(("nomatch*",), matcher=lambda pat: [])

    assert expanded == ()
    assert unmatched == ["nomatch*"]


def test_glob_local_matches_top_level_entries(tmp_path: Path) -> None:
    from protonfs.cli import _glob_local

    (tmp_path / "mload003_calib_1m").mkdir()
    (tmp_path / "mload003_calib_2m").mkdir()
    (tmp_path / "other").mkdir()

    assert _glob_local(tmp_path, "mload*") == ["mload003_calib_1m", "mload003_calib_2m"]


def test_glob_local_matches_multi_segment_pattern(tmp_path: Path) -> None:
    from protonfs.cli import _glob_local

    (tmp_path / "mload003_calib_1m").mkdir()
    (tmp_path / "mload003_calib_1m" / "output.ev").write_bytes(b"x")
    (tmp_path / "mload003_calib_1m" / "dump_0001").write_bytes(b"y")

    assert _glob_local(tmp_path, "mload003_calib_*/*.ev") == ["mload003_calib_1m/output.ev"]


def test_glob_local_returns_empty_for_no_matches(tmp_path: Path) -> None:
    from protonfs.cli import _glob_local

    assert _glob_local(tmp_path, "nomatch*") == []


def test_expand_pattern_index_matches_by_deduped_prefix() -> None:
    from protonfs.cli import _expand_pattern_index

    # #131: an offloaded file has no local presence at all, but IS in the index --
    # pattern matching must find it there, not on disk (Path.glob() would miss it).
    index_keys = [
        "mload003_calib_1m/dump_0001",
        "mload003_calib_1m/output.ev",
        "mload003_calib_2m/output.ev",
        "other/dump_0001",
    ]

    # single-segment pattern matches the whole directory family by its deduped top-level
    # prefix -- not just files that are themselves exactly one segment deep.
    assert _expand_pattern_index(index_keys, "mload*") == [
        "mload003_calib_1m",
        "mload003_calib_2m",
    ]


def test_expand_pattern_index_matches_multi_segment_pattern() -> None:
    from protonfs.cli import _expand_pattern_index

    index_keys = [
        "mload003_calib_1m/dump_0001",
        "mload003_calib_1m/output.ev",
        "mload003_calib_2m/output.ev",
        "other/dump_0001",
    ]

    assert _expand_pattern_index(index_keys, "mload003_calib_*/*.ev") == [
        "mload003_calib_1m/output.ev",
        "mload003_calib_2m/output.ev",
    ]


def test_expand_pattern_index_returns_empty_for_no_matches() -> None:
    from protonfs.cli import _expand_pattern_index

    assert _expand_pattern_index(["other/dump_0001"], "mload*") == []


def test_resolve_pathspecs_zero_match_never_widens_to_whole_repo() -> None:
    """The dangerous case: `_normalize_paths(())` means WHOLE REPO, so a pattern that
    matched nothing must not be allowed to fall through to it -- `push 'nomatch*'` has to
    push nothing, never everything."""
    from protonfs.cli import _resolve_pathspecs

    subpaths, unmatched = _resolve_pathspecs(("nomatch*",), matcher=lambda pat: [])

    assert subpaths == []  # NOT [None], which would mean the whole repo
    assert unmatched == ["nomatch*"]


def test_resolve_pathspecs_no_arguments_still_means_whole_repo() -> None:
    from protonfs.cli import _resolve_pathspecs

    # no pathspecs at all is the established "whole repo" default and must be preserved
    assert _resolve_pathspecs((), matcher=lambda pat: []) == ([None], [])


def test_resolve_pathspecs_mixes_literals_with_expanded_patterns() -> None:
    from protonfs.cli import _resolve_pathspecs

    subpaths, unmatched = _resolve_pathspecs(
        ("literal", "m*", "nomatch*"), matcher=lambda pat: ["m1", "m2"] if pat == "m*" else []
    )

    assert subpaths == ["literal", "m1", "m2"]
    assert unmatched == ["nomatch*"]


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


# --- #131: PATH arguments protonfs expands itself as glob patterns -------------------


def test_cli_push_expands_a_local_glob_pattern(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    for rel in ("mload003_calib_1m/dump", "mload003_calib_2m/dump", "other/dump"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"data")
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["push", "mload*"])

    assert result.exit_code == 0, result.output
    assert "transferred=2" in result.output
    # the decisive assertion: `other/dump` did NOT match `mload*` and was never uploaded
    uploaded = sorted(p for call in ctx.drive.upload_calls for p in call[0])
    assert uploaded == [
        str(tmp_path / "mload003_calib_1m" / "dump"),
        str(tmp_path / "mload003_calib_2m" / "dump"),
    ]


def test_cli_push_pattern_matching_nothing_is_lenient_by_default(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["push", "nomatch*"])

    assert result.exit_code == 0, result.output
    assert "matched nothing" in result.output.lower()


def test_cli_push_pattern_matching_nothing_with_strict_fails(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["push", "nomatch*", "--strict"])

    assert result.exit_code == 1, result.output


def test_cli_pull_expands_index_pattern_including_offloaded_file(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    """#131: a pattern must match files the index knows about but that are absent from
    disk (offloaded) -- a filesystem-only glob would miss exactly this case."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "mload003_calib_1m/output.ev", _tracked_entry("/my-files/test/mload003_calib_1m/output.ev")
    )
    ctx.index.save()
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["pull", "mload*"])

    assert result.exit_code == 0, result.output
    assert "transferred=1" in result.output
    downloaded = [p for call in ctx.drive.download_calls for p in call[0]]
    assert downloaded == ["/my-files/test/mload003_calib_1m/output.ev"]


def test_cli_pull_pattern_matching_nothing_is_lenient_by_default(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    # A non-matching entry, so the index is NOT empty: pull short-circuits on an empty
    # index before it ever looks at pathspecs, which would make this pass for the wrong
    # reason. This way the zero-match really is the pattern failing to match.
    ctx.index.set("other/dump", _tracked_entry("/my-files/test/other/dump"))
    ctx.index.save()
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["pull", "nomatch*"])

    assert result.exit_code == 0, result.output
    assert "matched nothing" in result.output.lower()
    assert ctx.drive.download_calls == []  # and it did NOT widen to the whole repo


def test_cli_pull_pattern_matching_nothing_with_strict_fails(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("other/dump", _tracked_entry("/my-files/test/other/dump"))
    ctx.index.save()
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["pull", "nomatch*", "--strict"])

    assert result.exit_code == 1, result.output
    assert ctx.drive.download_calls == []  # --strict fails BEFORE transferring anything


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


def test_cli_survives_corrupt_repo_config(tmp_path: Path, monkeypatch) -> None:
    """A corrupt .protonfs/config.json must not crash the group callback itself (#F2):
    diagnostics (doctor/config set) are how you FIX a broken config, so they -- and any
    other command whose body does not need config -- must still run."""
    protonfs_dir = tmp_path / ".protonfs"
    protonfs_dir.mkdir()
    (protonfs_dir / "config.json").write_text("{corrupt")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["shell-init"])

    assert result.exit_code == 0
    assert result.exception is None


# --- position-independent flags (argv reorder) -----------------------------------------


def test_cli_global_flag_after_subcommand(tmp_path, monkeypatch, make_fake_drive) -> None:
    """`protonfs status -v` (global flag after the subcommand) must work, not error."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["status", "-v"])

    assert result.exit_code == 0, result.output
    assert "synced: 0" in result.output


def test_cli_subcommand_flag_before_subcommand(tmp_path, monkeypatch, make_fake_drive) -> None:
    """`protonfs --dry-run push` (a subcommand flag before the subcommand) must work."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["--dry-run", "push"])

    assert result.exit_code == 0, result.output
    assert "transferred=0" in result.output


def test_cli_global_flag_after_subgroup_chain(tmp_path, monkeypatch, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["config", "set", "defaults.low_io", "true", "-v"])

    assert result.exit_code == 0, result.output
    assert "Set defaults.low_io" in result.output


def test_cli_subcommand_help_documents_global_options() -> None:
    result = CliRunner().invoke(main, ["push", "--help"])
    assert result.exit_code == 0
    assert "Global options" in result.output
    assert "--event-log" in result.output


def test_cli_global_flag_names_match_group_options() -> None:
    """Freshness guard: argv.GLOBAL_FLAG_NAMES + -v must equal the group's real options."""
    import click

    from protonfs.argv import GLOBAL_FLAG_NAMES

    group_opts: set[str] = set()
    for p in main.params:
        if isinstance(p, click.Option):
            group_opts.update(p.opts)
            group_opts.update(p.secondary_opts)
    # Every long global flag the reorderer hoists must be a real group option.
    assert GLOBAL_FLAG_NAMES <= group_opts
    # And the group exposes exactly these long options plus -v/--verbose and --version.
    assert group_opts == GLOBAL_FLAG_NAMES | {"-v", "--verbose", "--version"}


def test_push_interrupt_saves_progress_and_exits_130(tmp_path, monkeypatch, make_fake_drive):
    # Ctrl+C during a push must persist the index (files already synced stay recorded so a
    # re-run resumes), print a resumable message, and exit 130 -- not Click's bare Aborted!.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    def fake_push_files(ctx, subpath, resolve, dry_run, reporter=None):
        # Simulate one file getting indexed, then the user interrupting mid-run.
        ctx.index.set(
            "dump_0001",
            IndexEntry(
                size=4, mtime=0.0, sha256="x", sha1="y",
                remote_path="/my-files/test/dump_0001", origin_device="d",
                local_state="present", last_synced="t",
            ),
        )
        raise KeyboardInterrupt

    monkeypatch.setattr("protonfs.commands.push.push", fake_push_files)

    result = CliRunner().invoke(main, ["push"])

    assert result.exit_code == 130
    assert "interrupted" in result.output.lower()
    # The in-memory index change was flushed to disk, so a re-run resumes.
    from protonfs.index import IndexStore

    assert IndexStore(tmp_path).get("dump_0001") is not None


def test_pull_interrupt_exits_130(tmp_path, monkeypatch, make_fake_drive):
    # Same resumable-interrupt contract for pull.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    ctx.index.set(
        "seed",
        IndexEntry(
            size=1, mtime=0.0, sha256="", sha1="", remote_path="/my-files/test/seed",
            origin_device="d", local_state="metadata-only", last_synced="t",
        ),
    )
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    def fake_pull_files(ctx, subpath, resolve, dry_run, refresh=False, reporter=None):
        raise KeyboardInterrupt

    monkeypatch.setattr("protonfs.commands.pull.pull", fake_pull_files)

    result = CliRunner().invoke(main, ["pull"])

    assert result.exit_code == 130
    assert "interrupted" in result.output.lower()


# --- #136: SIGTERM/SIGHUP must not orphan an in-flight proton-drive child ---------------


def test_install_signal_handlers_makes_sigterm_raise() -> None:
    """#136: `subprocess.run` already kills its child on any exception (its bare `except:`
    calls process.kill()), so Ctrl-C is safe -- verified empirically. SIGTERM and SIGHUP
    are not: their default action terminates Python immediately, that cleanup never runs,
    and the `proton-drive` child survives holding an exclusive lock on its SQLite cache,
    which then fails every later run on the host.

    Installing handlers that RAISE converts those signals into the path subprocess.run
    already handles correctly.
    """
    import signal

    from protonfs.cli import _install_signal_handlers

    previous = signal.getsignal(signal.SIGTERM)
    try:
        _install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler), "SIGTERM must not be left at its default action"
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_install_signal_handlers_covers_sighup() -> None:
    """SIGHUP is the one that matters on a headless box: when the ssh session drops, an
    in-flight transfer's child would otherwise be orphaned on the remote host."""
    import signal

    from protonfs.cli import _install_signal_handlers

    if not hasattr(signal, "SIGHUP"):  # pragma: no cover - POSIX only
        pytest.skip("no SIGHUP on this platform")
    previous = signal.getsignal(signal.SIGHUP)
    try:
        _install_signal_handlers()
        handler = signal.getsignal(signal.SIGHUP)
        assert callable(handler)
        with pytest.raises(SystemExit):
            handler(signal.SIGHUP, None)
    finally:
        signal.signal(signal.SIGHUP, previous)
