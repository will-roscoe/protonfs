# tests/commands/test_gitmove.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from protonfs.commands.gitmove import Refusal, check_import
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.lfs import POINTER_SIGNATURE

ROOT = "/my-files/test"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _store(tmp_path: Path, make_fake_drive, files: dict[str, bytes]):
    """A git repo at ``tmp_path/r`` whose subdirectory ``store`` is a protonfs store
    holding ``files``, all committed."""
    repo = tmp_path / "r"
    store = repo / "store"
    store.mkdir(parents=True)
    for rel, data in files.items():
        (store / rel).parent.mkdir(parents=True, exist_ok=True)
        (store / rel).write_bytes(data)
    init_config(store, ROOT)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    ctx = load_context(store)
    ctx.drive = make_fake_drive()
    return ctx


def _reasons(refusals: list[Refusal]) -> dict[str, str]:
    return {r.path: r.reason for r in refusals}


def test_check_import_is_clear_for_clean_tracked_files(tmp_path, make_fake_drive) -> None:
    ctx = _store(tmp_path, make_fake_drive, {"a.mp4": b"a", "d/b.mp4": b"b"})

    assert check_import(ctx, ["a.mp4", "d/b.mp4"]) == []


def test_check_import_refuses_outside_a_git_work_tree(tmp_path, make_fake_drive) -> None:
    init_config(tmp_path, ROOT)
    (tmp_path / "a.mp4").write_bytes(b"a")
    ctx = load_context(tmp_path)

    assert "not inside a git work tree" in _reasons(check_import(ctx, ["a.mp4"]))["."]


def test_check_import_refuses_when_git_is_missing(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _store(tmp_path, make_fake_drive, {"a.mp4": b"a"})
    monkeypatch.setattr("shutil.which", lambda name: None)

    assert "git is not installed" in _reasons(check_import(ctx, ["a.mp4"]))["."]


def test_check_import_refuses_paths_outside_the_store(tmp_path, make_fake_drive) -> None:
    ctx = _store(tmp_path, make_fake_drive, {"a.mp4": b"a"})

    reasons = _reasons(check_import(ctx, ["../elsewhere.mp4", "/abs/x.mp4"]))

    assert "outside the store" in reasons["../elsewhere.mp4"]
    assert "outside the store" in reasons["/abs/x.mp4"]


def test_check_import_refuses_protonfs_control_files(tmp_path, make_fake_drive) -> None:
    ctx = _store(tmp_path, make_fake_drive, {"a.mp4": b"a"})

    assert "inside .protonfs/" in _reasons(check_import(ctx, [".protonfs/config.json"]))[
        ".protonfs/config.json"
    ]


def test_check_import_refuses_an_untracked_file(tmp_path, make_fake_drive) -> None:
    ctx = _store(tmp_path, make_fake_drive, {"a.mp4": b"a"})
    (ctx.root / "new.mp4").write_bytes(b"n")

    assert "not tracked by git" in _reasons(check_import(ctx, ["new.mp4"]))["new.mp4"]


@pytest.mark.parametrize("change", ["staged", "unstaged", "deleted"])
def test_check_import_refuses_uncommitted_changes(tmp_path, make_fake_drive, change) -> None:
    ctx = _store(tmp_path, make_fake_drive, {"a.mp4": b"a"})
    if change == "deleted":
        (ctx.root / "a.mp4").unlink()
    else:
        (ctx.root / "a.mp4").write_bytes(b"changed")
        if change == "staged":
            _git(ctx.root, "add", "a.mp4")

    assert "has uncommitted changes" in _reasons(check_import(ctx, ["a.mp4"]))["a.mp4"]


@pytest.mark.parametrize("state", ["unstaged", "staged", "untracked"])
def test_check_import_refuses_a_gitignore_with_uncommitted_changes(
    tmp_path, make_fake_drive, state
) -> None:
    # Staging the import block must never also stage the user's own pending edits.
    files = {"a.mp4": b"a"}
    if state != "untracked":
        files[".gitignore"] = b"*.tmp\n"
    ctx = _store(tmp_path, make_fake_drive, files)
    (ctx.root / ".gitignore").write_text("*.tmp\n*.log\n")
    if state == "staged":
        _git(ctx.root, "add", ".gitignore")

    reasons = _reasons(check_import(ctx, ["a.mp4"]))

    assert ".gitignore has uncommitted changes" in reasons[".gitignore"]


def test_check_import_refuses_an_lfs_pointer_stub(tmp_path, make_fake_drive) -> None:
    stub = f"{POINTER_SIGNATURE}\noid sha256:{'0' * 64}\nsize 12345\n".encode()
    ctx = _store(tmp_path, make_fake_drive, {"run/Makefile": stub})

    reason = _reasons(check_import(ctx, ["run/Makefile"]))["run/Makefile"]

    assert "git-LFS pointer stub" in reason and "git lfs pull" in reason


def test_check_import_refuses_a_path_the_store_ignores(tmp_path, make_fake_drive) -> None:
    ctx = _store(tmp_path, make_fake_drive, {"a.mp4": b"a", "b.tmp": b"t"})
    (ctx.root / ".protonfs" / "ignore").write_text("*.tmp\n")
    _git(ctx.root, "add", "-A")
    _git(ctx.root, "commit", "-q", "-m", "ignore")

    reason = _reasons(check_import(ctx, ["b.tmp"]))["b.tmp"]

    assert "excluded by .protonfs/ignore pattern *.tmp" in reason


def test_check_import_refuses_a_path_the_include_allowlist_misses(
    tmp_path, make_fake_drive
) -> None:
    ctx = _store(tmp_path, make_fake_drive, {"a.mp4": b"a", "b.webp": b"w"})
    (ctx.root / ".protonfs" / "include").write_text("*.mp4\n")
    _git(ctx.root, "add", "-A")
    _git(ctx.root, "commit", "-q", "-m", "include")

    reasons = _reasons(check_import(ctx, ["a.mp4", "b.webp"]))

    assert set(reasons) == {"b.webp"}
    assert "not matched by .protonfs/include" in reasons["b.webp"]


def test_check_import_reports_every_problem_at_once(tmp_path, make_fake_drive) -> None:
    ctx = _store(tmp_path, make_fake_drive, {"a.mp4": b"a", "b.mp4": b"b"})
    (ctx.root / "new.mp4").write_bytes(b"n")
    (ctx.root / "a.mp4").write_bytes(b"changed")

    reasons = _reasons(check_import(ctx, ["a.mp4", "new.mp4", "../x", "b.mp4"]))

    assert set(reasons) == {"a.mp4", "new.mp4", "../x"}
