"""Quoted glob PATHs (``prune "mload*"``) are expanded by protonfs for the scan-scoped commands."""
from __future__ import annotations

import os
import time
from pathlib import Path

from click.testing import CliRunner

from protonfs.cli import main
from protonfs.commands.push import push
from protonfs.config import init_config
from protonfs.context import load_context

DAY = 86400.0


def _repo(tmp_path: Path, monkeypatch, make_fake_drive, files: dict[str, float]):
    """A repo whose `files` ({rel: days since modification}) are pushed; wired into the CLI."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    now = time.time()
    for rel, days in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode())
        os.utime(path, (now - days * DAY, now - days * DAY))
    push(ctx, None, resolve=None, dry_run=False)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    return ctx


RUNS = {
    "mload005e_a/d1": 9, "mload005e_a/d2": 8, "mload005e_a/d3": 0.01,
    "mload005e_b/d1": 9, "mload005e_b/d2": 0.01,
    "other/d1": 9, "other/d2": 8,
}


def test_prune_quoted_glob_offloads_matching_directories_only(
    tmp_path, monkeypatch, make_fake_drive
) -> None:
    ctx = _repo(tmp_path, monkeypatch, make_fake_drive, RUNS)

    result = CliRunner().invoke(
        main, ["prune", "--yes", "--no-push", "mload005e_*", "--min-age=1d", "--keep=1"]
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "mload005e_a/d1").exists()
    assert not (tmp_path / "mload005e_b/d1").exists()
    assert (tmp_path / "mload005e_a/d3").exists()
    assert (tmp_path / "other/d1").exists()  # outside the pattern
    assert ctx.index.get("mload005e_a/d1").local_state == "metadata-only"


def test_offload_quoted_glob_offloads_matching_paths_only(
    tmp_path, monkeypatch, make_fake_drive
) -> None:
    _repo(tmp_path, monkeypatch, make_fake_drive, RUNS)

    result = CliRunner().invoke(main, ["offload", "--yes", "--min-age=1d", "mload005e_*"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "mload005e_a/d1").exists()
    assert (tmp_path / "mload005e_a/d3").exists()  # modified inside --min-age
    assert (tmp_path / "other/d1").exists()


def test_glob_finds_an_already_offloaded_directory(
    tmp_path, monkeypatch, make_fake_drive
) -> None:
    """A tracked path absent from disk still matches (the index is searched too)."""
    ctx = _repo(tmp_path, monkeypatch, make_fake_drive, {"mload005e_a/d1": 9})
    CliRunner().invoke(main, ["offload", "--yes", "mload005e_a"])
    assert not (tmp_path / "mload005e_a/d1").exists()

    result = CliRunner().invoke(main, ["status", "mload005e_*"])

    assert "no tracked path matches" not in result.output
    assert "metadata-only: 1" in result.output
    assert ctx.index.get("mload005e_a/d1") is not None


def test_glob_matching_nothing_warns_and_does_nothing(
    tmp_path, monkeypatch, make_fake_drive
) -> None:
    _repo(tmp_path, monkeypatch, make_fake_drive, RUNS)

    for cmd in (["prune", "--yes"], ["offload", "--yes"]):
        result = CliRunner().invoke(main, [*cmd, "nomatch_*"])
        assert result.exit_code == 0, result.output
        assert "no tracked path matches 'nomatch_*'" in result.output
        assert (tmp_path / "other/d1").exists()  # never widened to the whole repo
        assert (tmp_path / "mload005e_a/d1").exists()


def test_existing_literal_path_with_brackets_beats_glob_reading(
    tmp_path, monkeypatch, make_fake_drive
) -> None:
    """`run[1]` exists literally, so it names that folder, not `run1`."""
    _repo(tmp_path, monkeypatch, make_fake_drive, {"run[1]/d1": 9, "run1/d1": 9})

    result = CliRunner().invoke(main, ["offload", "--yes", "--min-age=1d", "run[1]"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "run[1]/d1").exists()
    assert (tmp_path / "run1/d1").exists()


def test_glob_with_brackets_still_expands_when_no_literal_exists(
    tmp_path, monkeypatch, make_fake_drive
) -> None:
    _repo(tmp_path, monkeypatch, make_fake_drive, {"run1/d1": 9, "run2/d1": 9, "run3/d1": 9})

    result = CliRunner().invoke(main, ["offload", "--yes", "--min-age=1d", "run[12]"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "run1/d1").exists() and not (tmp_path / "run2/d1").exists()
    assert (tmp_path / "run3/d1").exists()
