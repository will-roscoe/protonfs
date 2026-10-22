# tests/commands/test_gitmove.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from protonfs import gitops
from protonfs.commands.gitmove import Refusal, check_import, import_from_git
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


# --- import_from_git ----------------------------------------------------------------------

FILES = {"frames/f0.mp4": b"frame zero", "frames/f1.mp4": b"frame one", "keep.txt": b"k"}
IMPORT = ["frames/f0.mp4", "frames/f1.mp4"]


def _status(ctx) -> str:
    return _git(ctx.root, "status", "--porcelain", "--untracked-files=no", ".")


def test_import_untracks_and_ignores_after_verifying_every_path(
    tmp_path, make_fake_drive
) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)
    head = _git(ctx.root, "rev-parse", "HEAD")

    result = import_from_git(ctx, IMPORT)

    assert result.failures == [] and result.refusals == []
    assert result.untracked == 2
    assert gitops.tracked_files(ctx.root).isdisjoint(IMPORT)
    assert (ctx.root / "frames" / "f0.mp4").read_bytes() == b"frame zero"
    status = _status(ctx)
    assert "D  store/frames/f0.mp4" in status and "D  store/frames/f1.mp4" in status
    assert "A  store/.gitignore" in status
    assert _git(ctx.root, "rev-parse", "HEAD") == head  # never commits
    assert set(gitops.check_ignore(ctx.root, IMPORT)) == set(IMPORT)
    for rel in IMPORT:
        parent, name = f"{ROOT}/{rel}".rsplit("/", 1)
        assert name in ctx.drive.remote_identities(parent)


def test_import_makes_no_git_change_when_one_path_fails_drive_verification(
    tmp_path, make_fake_drive
) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)
    ctx.drive = make_fake_drive(remote_size_overrides={"f1.mp4": 3})

    result = import_from_git(ctx, IMPORT)

    assert [f["name"] for f in result.failures] == ["frames/f1.mp4"]
    assert result.failures[0]["kind"] in ("unverified-drive", "push")
    assert result.untracked == 0
    assert _status(ctx) == ""
    assert not (ctx.root / ".gitignore").exists()


def test_import_detects_a_same_size_same_mtime_change_under_low_io(
    tmp_path, make_fake_drive
) -> None:
    import os

    from protonfs.commands.push import push

    ctx = _store(tmp_path, make_fake_drive, FILES)
    ctx.config.defaults.low_io = True
    push(ctx, None, None, False)
    target = ctx.root / "frames" / "f0.mp4"
    stat = target.stat()
    target.write_bytes(b"FRAME ZERO")  # same size, different bytes
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    _git(ctx.root, "add", "frames/f0.mp4")
    _git(ctx.root, "commit", "-q", "-m", "regenerated")

    result = import_from_git(ctx, IMPORT)

    kinds = {f["name"]: f["kind"] for f in result.failures}
    assert kinds == {"frames/f0.mp4": "low-io-stale"}
    assert "PROTONFS_LOW_IO=0" in result.failures[0]["error"]
    assert _status(ctx) == ""


def test_import_skips_already_synced_files_and_still_imports_them(
    tmp_path, make_fake_drive
) -> None:
    from protonfs.commands.push import push

    ctx = _store(tmp_path, make_fake_drive, FILES)
    push(ctx, None, None, False)

    result = import_from_git(ctx, IMPORT)

    assert result.pushed.transferred_items == 0
    assert result.untracked == 2


def test_import_dry_run_changes_nothing_and_lists_lines_and_commands(
    tmp_path, make_fake_drive
) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)

    result = import_from_git(ctx, IMPORT, dry_run=True)

    assert result.gitignore_lines == ["/frames/f0.mp4", "/frames/f1.mp4"]
    assert any(c.startswith("git rm --cached") for c in result.git_commands)
    assert result.untracked == 0
    assert _status(ctx) == ""
    assert not (ctx.root / ".gitignore").exists()
    assert ctx.drive.upload_calls == []


@pytest.mark.parametrize("failing", ["rm_cached", "add"])
@pytest.mark.parametrize("had_gitignore", [True, False])
def test_import_rolls_back_when_a_git_step_fails(
    tmp_path, make_fake_drive, monkeypatch, failing, had_gitignore
) -> None:
    files = dict(FILES)
    if had_gitignore:
        files[".gitignore"] = b"*.tmp\n"
    ctx = _store(tmp_path, make_fake_drive, files)
    before = (ctx.root / ".gitignore").read_bytes() if had_gitignore else None
    real = getattr(gitops, failing)

    def boom(root, paths):
        real(root, paths)  # fail after the step took effect: the worst case
        raise gitops.GitError(["git", failing], 1, "injected")

    monkeypatch.setattr(f"protonfs.gitops.{failing}", boom)

    result = import_from_git(ctx, IMPORT)

    assert result.rolled_back
    assert [f["kind"] for f in result.failures] == ["git"]
    assert result.untracked == 0
    assert _status(ctx) == ""
    assert set(IMPORT) <= gitops.tracked_files(ctx.root)
    if had_gitignore:
        assert (ctx.root / ".gitignore").read_bytes() == before
    else:
        assert not (ctx.root / ".gitignore").exists()


def test_import_reports_undo_commands_when_rollback_fails(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    import click

    ctx = _store(tmp_path, make_fake_drive, FILES)
    real_rm = gitops.rm_cached

    def rm_then_fail(root, paths):
        real_rm(root, paths)
        raise gitops.GitError(["git", "rm"], 1, "injected")

    def reset_fails(root, paths):
        raise gitops.GitError(["git", "reset"], 1, "also broken")

    monkeypatch.setattr("protonfs.gitops.rm_cached", rm_then_fail)
    monkeypatch.setattr("protonfs.gitops.reset_paths", reset_fails)

    with pytest.raises(click.ClickException) as excinfo:
        import_from_git(ctx, IMPORT)

    message = excinfo.value.format_message()
    assert "git reset -q --" in message and "frames/f0.mp4" in message


def test_import_never_trashes_even_with_deletions_trash(tmp_path, make_fake_drive) -> None:
    from protonfs.commands.push import push

    ctx = _store(tmp_path, make_fake_drive, FILES)
    push(ctx, None, None, False)
    ctx.config.defaults.deletions = "trash"
    (ctx.root / "keep.txt").unlink()
    _git(ctx.root, "rm", "-q", "keep.txt")
    _git(ctx.root, "commit", "-q", "-m", "drop keep")

    import_from_git(ctx, IMPORT)

    assert ctx.drive.trashed == []


def test_import_sets_trash_policy_warning_from_shared_config(
    tmp_path, make_fake_drive
) -> None:
    import json

    ctx = _store(tmp_path, make_fake_drive, FILES)
    shared = ctx.root / ".protonfs" / "config.json"
    data = json.loads(shared.read_text())
    data.setdefault("defaults", {})["deletions"] = "trash"
    shared.write_text(json.dumps(data))
    _git(ctx.root, "commit", "-q", "-am", "trash policy")

    assert import_from_git(ctx, IMPORT).trash_policy_warning
