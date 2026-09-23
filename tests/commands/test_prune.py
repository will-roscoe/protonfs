"""``protonfs prune``: retention through offload (#158)."""
from __future__ import annotations

import os
import time
from pathlib import Path

from click.testing import CliRunner

from protonfs.cli import main
from protonfs.commands.prune import prune
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import TransferResult

DAY = 86400.0


def _repo(tmp_path: Path, make_fake_drive, files: dict[str, float], **fake_kwargs):
    """A repo whose `files` ({rel: days since last modification}) are pushed and
    verified. Returns (ctx, now)."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(**fake_kwargs)
    now = time.time()
    for rel, days in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode())
        mtime = now - days * DAY
        os.utime(path, (mtime, mtime))
    from protonfs.commands.push import push

    push(ctx, None, resolve=None, dry_run=False)
    return ctx, now


def test_prune_keeps_the_newest_per_directory_and_offloads_older_settled_files(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx, now = _repo(tmp_path, make_fake_drive, {
        "run1/d1": 9, "run1/d2": 8, "run1/d3": 7, "run1/d4": 6,
        "run2/d1": 9, "run2/d2": 8,
    })

    result = prune(ctx, None, keep=2, min_age=DAY, push_first=False, now=now)

    assert result.considered == 6
    assert result.candidates == ["run1/d1", "run1/d2"]
    assert result.offload.offloaded_paths == ["run1/d1", "run1/d2"]
    assert not (tmp_path / "run1/d1").exists() and (tmp_path / "run1/d3").exists()
    assert (tmp_path / "run2/d1").exists()  # its directory holds only `keep` files
    assert ctx.index.get("run1/d1").local_state == "metadata-only"


def test_prune_needs_both_rules_to_release_a_file(tmp_path: Path, make_fake_drive) -> None:
    # Outside the newest two, but modified an hour ago: may still be being written.
    ctx, now = _repo(tmp_path, make_fake_drive, {
        "run/old": 5, "run/fresh": 0.04, "run/newer": 0.02, "run/newest": 0.01,
    })

    result = prune(ctx, None, keep=2, min_age=DAY, push_first=False, now=now)

    assert result.candidates == ["run/old"]
    assert (tmp_path / "run/fresh").exists()


def test_prune_never_removes_what_offload_would_refuse(tmp_path: Path, make_fake_drive) -> None:
    # Retention releases both, but Drive holds a short copy of one: offload's live check
    # keeps it.
    ctx, now = _repo(tmp_path, make_fake_drive, {"run/a": 5, "run/b": 5})
    ctx.drive._remote_files["/my-files/test/run"]["a"] = 1

    result = prune(ctx, None, keep=0, min_age=DAY, push_first=False, now=now)

    assert result.candidates == ["run/a", "run/b"]
    assert result.offload.offloaded_paths == ["run/b"]
    assert result.offload.skipped_paths == ["run/a"]
    assert (tmp_path / "run/a").exists()


def test_prune_pushes_first_so_new_files_are_on_drive(tmp_path: Path, make_fake_drive) -> None:
    ctx, now = _repo(tmp_path, make_fake_drive, {"run/a": 5})
    fresh = tmp_path / "run" / "new"
    fresh.write_bytes(b"new")

    result = prune(ctx, None, keep=5, min_age=DAY, now=now)

    assert result.pushed is not None and result.pushed.transferred_items == 1
    assert ctx.index.get("run/new") is not None


def test_prune_dry_run_pushes_and_deletes_nothing(tmp_path: Path, make_fake_drive) -> None:
    ctx, now = _repo(tmp_path, make_fake_drive, {"run/a": 5, "run/b": 5})
    uploads = len(ctx.drive.upload_calls)

    result = prune(ctx, None, keep=0, min_age=DAY, dry_run=True, now=now)

    assert result.pushed is None
    assert len(ctx.drive.upload_calls) == uploads
    assert result.offload.offloaded == 2  # reported as "would offload"
    assert (tmp_path / "run/a").exists() and (tmp_path / "run/b").exists()


def test_prune_respects_the_subpath_and_ignore_rules(tmp_path: Path, make_fake_drive) -> None:
    ctx, now = _repo(tmp_path, make_fake_drive, {"keep/a": 5, "go/a": 5, "go/b.tmp": 5})
    # tracked before the ignore rule existed: prune must still honour the rule
    (tmp_path / ".protonfs" / "ignore").write_text("*.tmp\n")
    assert ctx.index.get("go/b.tmp") is not None

    result = prune(ctx, "go", keep=0, min_age=DAY, push_first=False, now=now)

    assert result.candidates == ["go/a"]
    assert (tmp_path / "keep/a").exists() and (tmp_path / "go/b.tmp").exists()


def test_cli_prune_reports_and_fails_when_the_push_fails(
    tmp_path: Path, make_fake_drive, monkeypatch
) -> None:
    ctx, _now = _repo(tmp_path, make_fake_drive, {"run/a": 5})
    (tmp_path / "run" / "new").write_bytes(b"new")
    ctx.drive._upload_result = TransferResult(
        0, 0, 1, [{"name": "new", "error": "quota exceeded"}]
    )
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["prune", "--yes", "--keep", "0"])

    assert result.exit_code == 1
    assert "pushed: transferred=0" in result.output and "quota exceeded" in result.output
    assert "offloaded=1" in result.output  # what was verified was still reclaimed
    assert not (tmp_path / "run" / "a").exists()


def test_cli_prune_rejects_a_bad_settle_window(tmp_path: Path, monkeypatch) -> None:
    init_config(tmp_path, "/my-files/test")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["prune", "--yes", "--min-age", "12"])

    assert result.exit_code == 2
    assert "--min-age" in result.output


def test_cli_prune_confirmation_abort(tmp_path: Path, make_fake_drive, monkeypatch) -> None:
    ctx, _now = _repo(tmp_path, make_fake_drive, {"run/a": 5})
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["prune"], input="n\n")

    assert result.exit_code == 1
    assert (tmp_path / "run" / "a").exists()
