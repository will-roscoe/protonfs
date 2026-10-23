# tests/commands/test_gitmove.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from protonfs import gitops, importblock
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
    assert any(c.startswith("git --literal-pathspecs rm --cached") for c in result.git_commands)
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
    assert "reset -q --pathspec-from-file=" in message
    listed = message.split("--pathspec-from-file=")[1].split()[0]
    assert b"frames/f0.mp4\0" in Path(listed).read_bytes()


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


# --- the `protonfs import` command ----------------------------------------------------------


def _cli(ctx, monkeypatch, *args: str):
    from click.testing import CliRunner

    from protonfs.cli import main

    monkeypatch.chdir(ctx.root)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    return CliRunner().invoke(main, ["import", *args])


def test_cli_import_glob_ignores_untracked_matches(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _store(
        tmp_path, make_fake_drive, {"run/a.reduced.fits": b"r", ".gitignore": b"*.grids.fits\n"}
    )
    (ctx.root / "run" / "a.grids.fits").write_bytes(b"g")

    result = _cli(ctx, monkeypatch, "**/*.fits")

    assert result.exit_code == 0, result.output
    assert "REFUSED" not in result.output
    assert "untracked=1" in result.output
    assert "run/a.reduced.fits" not in gitops.tracked_files(ctx.root)


def test_cli_import_literal_untracked_path_is_refused_exit_1(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)
    (ctx.root / "new.mp4").write_bytes(b"n")

    result = _cli(ctx, monkeypatch, "new.mp4")

    assert result.exit_code == 1
    assert "REFUSED new.mp4: not tracked by git" in result.output
    assert ctx.drive.upload_calls == []


def test_cli_import_a_directory_means_its_tracked_files(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)
    (ctx.root / "frames" / "untracked.mp4").write_bytes(b"u")

    result = _cli(ctx, monkeypatch, "frames")

    assert result.exit_code == 0, result.output
    assert "untracked=2" in result.output


def test_cli_import_strict_fails_on_unmatched_glob(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)

    result = _cli(ctx, monkeypatch, "--strict", "*.nothing", "frames/f0.mp4")

    assert result.exit_code == 1
    assert ctx.drive.upload_calls == []
    assert "frames/f0.mp4" in gitops.tracked_files(ctx.root)


def test_cli_import_summary_line_has_untracked_key(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)

    result = _cli(ctx, monkeypatch, *IMPORT)

    summary = next(line for line in result.output.splitlines() if "transferred=" in line)
    assert summary.startswith("transferred=2 ") and summary.endswith(" untracked=2")


def test_cli_import_prints_other_clones_and_history_notes(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)

    out = _cli(ctx, monkeypatch, *IMPORT).output

    assert "added 2 line(s) to .gitignore" in out
    assert 'git commit -m "chore: move 2 file(s) from git into protonfs"' in out
    assert "run `protonfs pull` there" in out
    assert "git filter-repo" in out


def test_cli_import_fails_exit_1_when_verification_fails(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)
    ctx.drive = make_fake_drive(remote_size_overrides={"f1.mp4": 3})

    result = _cli(ctx, monkeypatch, *IMPORT)

    assert result.exit_code == 1
    assert "FAILED frames/f1.mp4" in result.output
    assert "nothing changed in git" in result.output


@pytest.mark.parametrize(("extra", "code"), [((), 0), (("new.mp4",), 1)])
def test_cli_import_dry_run_exit_codes(tmp_path, make_fake_drive, monkeypatch, extra, code) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)
    (ctx.root / "new.mp4").write_bytes(b"n")

    result = _cli(ctx, monkeypatch, "--dry-run", *IMPORT, *extra)

    assert result.exit_code == code, result.output
    if code == 0:
        assert "[dry-run] /frames/f0.mp4" in result.output
        assert "[dry-run] git --literal-pathspecs rm --cached" in result.output
        assert "verified on Drive and against HEAD when run for real" in result.output
    assert _status(ctx) == ""


# --- review fixes: refusals that save a wasted upload ----------------------------------


@pytest.mark.parametrize("rel", [".gitignore", "d/.gitignore", "d/.gitattributes"])
def test_check_import_refuses_git_control_files(tmp_path, make_fake_drive, rel) -> None:
    ctx = _store(tmp_path, make_fake_drive, {rel: b"*.tmp\n", "a.mp4": b"a"})

    assert "git control file" in _reasons(check_import(ctx, [rel]))[rel]


def test_check_import_refuses_a_name_with_a_newline(tmp_path, make_fake_drive) -> None:
    # A .gitignore line cannot match "x\nb" exactly; written as-is it would ignore "b".
    ctx = _store(tmp_path, make_fake_drive, {"x\nb": b"1", "a.mp4": b"a"})

    assert "newline" in _reasons(check_import(ctx, ["x\nb"]))["x\nb"]


def test_check_import_refuses_a_tracked_symlink(tmp_path, make_fake_drive) -> None:
    import os

    ctx = _store(tmp_path, make_fake_drive, {"run3.mp4": b"m"})
    os.symlink("run3.mp4", ctx.root / "latest.mp4")
    _git(ctx.root, "add", "latest.mp4")
    _git(ctx.root, "commit", "-q", "-m", "link")

    assert "symlink" in _reasons(check_import(ctx, ["latest.mp4"]))["latest.mp4"]


def test_check_import_refuses_a_git_lfs_file(tmp_path, make_fake_drive) -> None:
    ctx = _store(
        tmp_path, make_fake_drive,
        {".gitattributes": b"*.bin filter=lfs diff=lfs merge=lfs -text\n", "a.bin": b"real"},
    )

    reason = _reasons(check_import(ctx, ["a.bin"]))["a.bin"]

    assert "git-LFS" in reason and "setup --migrate-lfs" in reason


def test_import_accepts_a_file_git_converts_on_checkout(tmp_path, make_fake_drive) -> None:
    # *.csv eol=crlf: the work tree holds CRLF, HEAD's blob holds LF. The bytes differ
    # only by git's own conversion, so the file verifies (Drive gets the work-tree form).
    ctx = _store(
        tmp_path, make_fake_drive, {".gitattributes": b"*.csv text eol=crlf\n", "t.csv": b"a\nb\n"}
    )
    (ctx.root / "t.csv").unlink()
    _git(ctx.root, "checkout", "--", "t.csv")

    result = import_from_git(ctx, ["t.csv"])

    assert result.failures == [], result.failures
    assert result.untracked == 1


# --- review fixes: interrupt and rollback -----------------------------------------------


@pytest.mark.parametrize("failing", ["add_paths", "rm_cached", "add"])
def test_import_rolls_back_on_keyboard_interrupt(
    tmp_path, make_fake_drive, monkeypatch, failing
) -> None:
    from protonfs import importblock

    ctx = _store(tmp_path, make_fake_drive, FILES)
    module = importblock if failing == "add_paths" else gitops
    real = getattr(module, failing)

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(module, failing, interrupted)

    with pytest.raises(KeyboardInterrupt):
        import_from_git(ctx, IMPORT)

    assert _status(ctx) == ""
    assert not (ctx.root / ".gitignore").exists()
    assert set(IMPORT) <= gitops.tracked_files(ctx.root)


def test_undo_instructions_work_in_a_subdirectory_store(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    import click

    ctx = _store(tmp_path, make_fake_drive, {**FILES, ".gitignore": b"*.tmp\n"})
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
    assert f"git -C {ctx.root} checkout HEAD -- .gitignore" in message  # the store's file
    assert "HEAD:.gitignore" not in message  # that names the repo root's .gitignore
    assert "--literal-pathspecs" in message
    assert "wrote the .gitignore lines" in message and "untracked" in message
    listed = message.split("--pathspec-from-file=")[1].split()[0]
    assert Path(listed).read_bytes().split(b"\0")[:2] == [b"frames/f0.mp4", b"frames/f1.mp4"]


# --- review fixes: the command ----------------------------------------------------------


def test_cli_import_dot_skips_protonfs_and_git_control_files(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _store(tmp_path, make_fake_drive, {**FILES, ".gitignore": b"*.tmp\n"})

    result = _cli(ctx, monkeypatch, ".")

    assert result.exit_code == 0, result.output
    assert "untracked=3" in result.output
    assert {".gitignore", ".protonfs/config.json"} <= gitops.tracked_files(ctx.root)


def test_cli_import_star_glob_skips_control_files(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _store(tmp_path, make_fake_drive, {**FILES, ".gitignore": b"*.tmp\n"})

    result = _cli(ctx, monkeypatch, "*")

    assert result.exit_code == 0, result.output
    assert "untracked=3" in result.output  # keep.txt, plus frames/ (a directory: its files)
    assert "REFUSED" not in result.output


def test_cli_import_outside_a_work_tree_refuses_exit_1(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    init_config(tmp_path, ROOT)
    (tmp_path / "a.mp4").write_bytes(b"a")
    ctx = load_context(tmp_path)

    result = _cli(ctx, monkeypatch, "*.mp4")

    assert result.exit_code == 1
    assert "not inside a git work tree" in result.output


def test_cli_import_git_error_is_a_clean_exit_1(tmp_path, make_fake_drive, monkeypatch) -> None:
    # A repo with no commits: `git diff HEAD` fails. No traceback, exit 1.
    repo = tmp_path / "r"
    store = repo / "store"
    store.mkdir(parents=True)
    (store / "a.mp4").write_bytes(b"a")
    init_config(store, ROOT)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    ctx = load_context(store)
    ctx.drive = make_fake_drive()

    result = _cli(ctx, monkeypatch, "a.mp4")

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "git" in result.output


def test_cli_import_dry_run_lists_verdicts_and_the_size(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)
    (ctx.root / "new.mp4").write_bytes(b"n")

    ok = _cli(ctx, monkeypatch, "--dry-run", *IMPORT).output
    refused = _cli(ctx, monkeypatch, "--dry-run", *IMPORT, "new.mp4")

    assert "[dry-run] import frames/f0.mp4" in ok
    assert "[dry-run] would push 2 file(s)" in ok and " B" in ok
    assert "transferred=" not in ok
    assert refused.exit_code == 1
    assert "[dry-run] import frames/f0.mp4" in refused.output
    assert "REFUSED new.mp4: not tracked by git" in refused.output


# --- export (#205): files the store holds back into git ---------------------------------

from protonfs.commands.gitmove import check_export, export_to_git  # noqa: E402
from protonfs.ignore import IgnoreMatcher  # noqa: E402
from protonfs.index import IndexStore  # noqa: E402


def _imported(tmp_path, make_fake_drive, files=None):
    """A store set up as `setup` leaves it (with .protonfs/ignore) whose IMPORT files
    were imported and the import committed."""
    from protonfs.ignore import init_ignore

    ctx = _store(tmp_path, make_fake_drive, files or FILES)
    init_ignore(ctx.root)
    _git(ctx.root, "add", ".protonfs/ignore")
    _git(ctx.root, "commit", "-q", "-m", "ignore")
    result = import_from_git(ctx, IMPORT)
    assert result.untracked == len(IMPORT), result
    _git(ctx.root, "commit", "-q", "-m", "import")
    return ctx


def test_check_export_is_clear_for_imported_files(tmp_path, make_fake_drive) -> None:
    ctx = _imported(tmp_path, make_fake_drive)

    assert check_export(ctx, IMPORT) == []


def test_check_export_refuses_outside_a_git_work_tree(tmp_path, make_fake_drive) -> None:
    init_config(tmp_path, ROOT)
    ctx = load_context(tmp_path)

    assert "not inside a git work tree" in _reasons(check_export(ctx, ["a.mp4"]))["."]


def test_check_export_refuses_what_the_store_does_not_hold_or_git_tracks(
    tmp_path, make_fake_drive
) -> None:
    ctx = _imported(tmp_path, make_fake_drive)

    reasons = _reasons(check_export(ctx, ["keep.txt", "nope.mp4", "../x", ".protonfs/ignore"]))

    assert "already tracked by git" in reasons["keep.txt"]
    assert "not held by the store" in reasons["nope.mp4"]
    assert "outside the store" in reasons["../x"]
    assert ".protonfs/" in reasons[".protonfs/ignore"]


def test_check_export_names_another_rule_that_would_still_ignore_the_file(
    tmp_path, make_fake_drive
) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    (ctx.root / "frames" / ".gitignore").write_text("*.mp4\n")  # deeper file wins
    _git(ctx.root, "add", "frames/.gitignore")
    _git(ctx.root, "commit", "-q", "-m", "rule")

    reasons = _reasons(check_export(ctx, IMPORT))

    assert "store/frames/.gitignore:1:*.mp4" in reasons["frames/f0.mp4"]


@pytest.mark.parametrize("target", [".gitignore", ".protonfs/ignore"])
@pytest.mark.parametrize("state", ["unstaged", "staged"])
def test_check_export_refuses_ignore_files_with_uncommitted_changes(
    tmp_path, make_fake_drive, target, state
) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    with open(ctx.root / target, "a") as fh:
        fh.write("*.log\n")
    if state == "staged":
        _git(ctx.root, "add", target)

    assert "uncommitted changes" in _reasons(check_export(ctx, IMPORT))[target]


def test_export_tracks_the_files_and_stops_syncing_them(tmp_path, make_fake_drive) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    head = _git(ctx.root, "rev-parse", "HEAD")

    result = export_to_git(ctx, IMPORT)

    assert result.refusals == [] and result.failures == []
    assert result.tracked == 2
    status = _status(ctx)
    assert "A  store/frames/f0.mp4" in status and "A  store/frames/f1.mp4" in status
    assert "M  store/.gitignore" in status and "M  store/.protonfs/ignore" in status
    assert _git(ctx.root, "rev-parse", "HEAD") == head  # never commits
    assert gitops.check_ignore(ctx.root, IMPORT) == {}
    assert importblock.export_block_paths(ctx.root) == set(IMPORT)
    assert all(IgnoreMatcher.from_file(ctx.root).matches(rel) for rel in IMPORT)
    on_disk = IndexStore(ctx.root)
    assert not set(IMPORT) & set(on_disk.all())
    assert (ctx.root / "frames" / "f0.mp4").read_bytes() == b"frame zero"
    assert ctx.drive.trashed == []
    parent, name = f"{ROOT}/frames/f0.mp4".rsplit("/", 1)
    assert name in ctx.drive.remote_identities(parent)  # the Drive copy is kept


def test_export_pulls_an_offloaded_file_first(tmp_path, make_fake_drive) -> None:
    from protonfs.commands.offload import offload

    ctx = _imported(tmp_path, make_fake_drive)
    offload(ctx, "frames", min_age=0)
    assert not (ctx.root / "frames" / "f0.mp4").exists()

    result = export_to_git(ctx, IMPORT)

    assert result.pulled.transferred_items == 2
    assert result.tracked == 2
    assert (ctx.root / "frames" / "f0.mp4").read_bytes() == b"frame zero"


def test_export_changes_nothing_in_git_when_a_download_is_dropped(
    tmp_path, make_fake_drive
) -> None:
    from protonfs.commands.offload import offload

    ctx = _imported(tmp_path, make_fake_drive)
    offload(ctx, "frames", min_age=0)
    drive = ctx.drive
    drive._download_dropped_files = {"f1.mp4"}

    result = export_to_git(ctx, IMPORT)

    assert result.tracked == 0
    assert {f["name"]: f["kind"] for f in result.failures} == {"frames/f1.mp4": "not-delivered"}
    assert _status(ctx) == ""
    assert importblock.block_paths(ctx.root) == set(IMPORT)


def test_export_refuses_a_file_drive_holds_a_different_version_of(
    tmp_path, make_fake_drive
) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    ctx.drive._remote_files[f"{ROOT}/frames"]["f1.mp4"] = 99  # pushed from elsewhere

    result = export_to_git(ctx, IMPORT)

    assert {f["name"]: f["kind"] for f in result.failures} == {
        "frames/f1.mp4": "unverified-drive"
    }
    assert result.tracked == 0
    assert _status(ctx) == ""


def test_export_refuses_a_local_change_that_was_never_pushed(tmp_path, make_fake_drive) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    (ctx.root / "frames" / "f0.mp4").write_bytes(b"edited, not pushed")

    result = export_to_git(ctx, IMPORT)

    failure = {f["name"]: f for f in result.failures}["frames/f0.mp4"]
    assert failure["kind"] == "unverified-local"
    assert "protonfs push frames/f0.mp4" in failure["error"]
    assert _status(ctx) == ""


def _export_snapshot(ctx):
    return (
        (ctx.root / ".gitignore").read_bytes(),
        (ctx.root / ".protonfs" / "ignore").read_bytes(),
        sorted(IndexStore(ctx.root).all()),
    )


@pytest.mark.parametrize(
    "failing", ["remove_paths", "add#1", "add_export_paths", "add#2"]
)
@pytest.mark.parametrize("exc", [gitops.GitError(["git"], 1, "injected"), KeyboardInterrupt()])
def test_export_rolls_back_every_git_change(
    tmp_path, make_fake_drive, monkeypatch, failing, exc
) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    before = _export_snapshot(ctx)
    name, _, nth = failing.partition("#")
    module = gitops if name == "add" else importblock
    real = getattr(module, name)
    calls = []

    def boom(*args):
        calls.append(1)
        value = real(*args)  # fail after the step took effect: the worst case
        if not nth or len(calls) == int(nth):
            raise exc
        return value

    monkeypatch.setattr(module, name, boom)

    if isinstance(exc, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            export_to_git(ctx, IMPORT)
    else:
        result = export_to_git(ctx, IMPORT)
        assert result.rolled_back and [f["kind"] for f in result.failures] == ["git"]
        assert result.tracked == 0
    assert _status(ctx) == ""
    assert _export_snapshot(ctx) == before
    assert gitops.tracked_files(ctx.root).isdisjoint(IMPORT)


def test_export_refuses_and_rolls_back_when_an_earlier_rule_still_ignores_it(
    tmp_path, make_fake_drive
) -> None:
    # git reports only the last matching rule, so `*.mp4` above the import block is only
    # visible once the block's own lines are gone
    ctx = _imported(tmp_path, make_fake_drive)
    gitignore = ctx.root / ".gitignore"
    gitignore.write_bytes(b"*.mp4\n" + gitignore.read_bytes())
    _git(ctx.root, "add", ".gitignore")
    _git(ctx.root, "commit", "-q", "-m", "rule")
    before = _export_snapshot(ctx)

    result = export_to_git(ctx, IMPORT)

    reasons = _reasons(result.refusals)
    assert "store/.gitignore:1:*.mp4" in reasons["frames/f0.mp4"]
    assert result.tracked == 0
    assert _status(ctx) == ""
    assert _export_snapshot(ctx) == before


def test_export_trash_remote_trashes_the_drive_copies_after_the_git_step(
    tmp_path, make_fake_drive
) -> None:
    ctx = _imported(tmp_path, make_fake_drive)

    result = export_to_git(ctx, IMPORT, trash_remote=True)

    assert result.tracked == 2 and result.trashed == 2
    assert sorted(ctx.drive.trashed) == [f"{ROOT}/frames/f0.mp4", f"{ROOT}/frames/f1.mp4"]
    assert not set(IMPORT) & set(IndexStore(ctx.root).all())


def test_export_trash_remote_failure_keeps_git_changes_and_reports_it(
    tmp_path, make_fake_drive
) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    ctx.drive._fail_trash = True

    result = export_to_git(ctx, IMPORT, trash_remote=True)

    assert result.tracked == 2 and result.trashed == 0
    assert {f["kind"] for f in result.failures} == {"trash"}
    assert "A  store/frames/f0.mp4" in _status(ctx)


def test_export_dry_run_changes_nothing(tmp_path, make_fake_drive) -> None:
    from protonfs.commands.offload import offload

    ctx = _imported(tmp_path, make_fake_drive)
    offload(ctx, "frames/f0.mp4", min_age=0)
    before = _export_snapshot(ctx)

    result = export_to_git(ctx, IMPORT, dry_run=True)

    assert result.to_download == ["frames/f0.mp4"]
    assert result.gitignore_removed == ["/frames/f0.mp4", "/frames/f1.mp4"]
    assert result.ignore_lines == ["/frames/f0.mp4", "/frames/f1.mp4"]
    assert any(c.startswith("git --literal-pathspecs add") for c in result.git_commands)
    assert _export_snapshot(ctx) == before
    assert ctx.drive.download_calls == []
    assert _status(ctx) == ""


def test_export_then_push_never_uploads_or_trashes_the_files(tmp_path, make_fake_drive) -> None:
    from protonfs.commands.push import push

    ctx = _imported(tmp_path, make_fake_drive)
    export_to_git(ctx, IMPORT)
    _git(ctx.root, "commit", "-q", "-m", "export")
    uploads = len(ctx.drive.upload_calls)

    result = push(load_context(ctx.root), None, None, False, deletions="trash")

    assert len(ctx.drive.upload_calls) == uploads and ctx.drive.trashed == []
    assert result.trashed_items == 0


def test_import_explains_a_path_listed_in_the_export_block(tmp_path, make_fake_drive) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    export_to_git(ctx, IMPORT)
    _git(ctx.root, "commit", "-q", "-m", "export")

    reason = _reasons(check_import(load_context(ctx.root), ["frames/f0.mp4"]))["frames/f0.mp4"]

    assert "exported" in reason and "# protonfs export" in reason


def test_export_creates_and_stages_a_missing_protonfs_ignore(tmp_path, make_fake_drive) -> None:
    ctx = _store(tmp_path, make_fake_drive, FILES)
    import_from_git(ctx, IMPORT)
    _git(ctx.root, "commit", "-q", "-m", "import")
    assert not (ctx.root / ".protonfs" / "ignore").exists()

    result = export_to_git(ctx, IMPORT)

    assert result.tracked == 2
    assert "A  store/.protonfs/ignore" in _status(ctx)
    assert importblock.export_block_paths(ctx.root) == set(IMPORT)


def test_export_a_file_that_was_never_in_git(tmp_path, make_fake_drive) -> None:
    from protonfs.commands.push import push

    ctx = _imported(tmp_path, make_fake_drive)
    (ctx.root / "new.bin").write_bytes(b"only ever synced")
    push(ctx, None, None, False)

    result = export_to_git(ctx, ["new.bin"])

    assert result.tracked == 1 and result.gitignore_removed == []
    assert "A  store/new.bin" in _status(ctx)
    assert "store/.gitignore" not in _status(ctx)


def _cli_export(ctx, monkeypatch, *args: str):
    from click.testing import CliRunner

    from protonfs.cli import main

    monkeypatch.chdir(ctx.root)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    return CliRunner().invoke(main, ["export", *args])


def test_cli_export_prints_summary_and_notes(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _imported(tmp_path, make_fake_drive)

    result = _cli_export(ctx, monkeypatch, *IMPORT)

    assert result.exit_code == 0, result.output
    assert "transferred=0 skipped=2 failed=0 tracked=2" in result.output
    assert "git commit" in result.output
    assert "Drive copies are kept" in result.output
    assert "other clones" in result.output


def test_cli_export_glob_matches_offloaded_files(tmp_path, make_fake_drive, monkeypatch) -> None:
    from protonfs.commands.offload import offload

    ctx = _imported(tmp_path, make_fake_drive)
    offload(ctx, "frames", min_age=0)

    result = _cli_export(ctx, monkeypatch, "frames/*")

    assert result.exit_code == 0, result.output
    assert "transferred=2 skipped=0 failed=0 tracked=2" in result.output


def test_cli_export_dot_skips_protonfs_and_git_control_files(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    from protonfs.commands.push import push

    ctx = _imported(tmp_path, make_fake_drive)
    push(ctx, None, None, False)  # the store syncs its own .gitignore too
    assert ctx.index.get(".gitignore") is not None

    result = _cli_export(ctx, monkeypatch, ".")

    assert result.exit_code == 0, result.output
    assert "tracked=2" in result.output


def test_cli_export_refusal_exits_1(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _imported(tmp_path, make_fake_drive)

    result = _cli_export(ctx, monkeypatch, "frames/f0.mp4", "nope.bin")

    assert result.exit_code == 1
    assert "REFUSED nope.bin: not held by the store" in result.output
    assert _status(ctx) == ""


def test_cli_export_strict_fails_on_unmatched_glob(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _imported(tmp_path, make_fake_drive)

    result = _cli_export(ctx, monkeypatch, "--strict", "nomatch*")

    assert result.exit_code == 1


def test_cli_export_dry_run_lists_the_plan(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _imported(tmp_path, make_fake_drive)

    result = _cli_export(ctx, monkeypatch, "--dry-run", "frames")

    assert result.exit_code == 0, result.output
    assert "[dry-run] export frames/f0.mp4" in result.output
    assert "[dry-run] would remove 2 line(s) from .gitignore" in result.output
    assert "[dry-run] would add 2 line(s) to .protonfs/ignore" in result.output
    assert "[dry-run] git --literal-pathspecs add" in result.output
    assert _status(ctx) == ""


def test_cli_export_trash_remote_summary(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _imported(tmp_path, make_fake_drive)

    result = _cli_export(ctx, monkeypatch, "--trash-remote", *IMPORT)

    assert result.exit_code == 0, result.output
    assert "tracked=2 trashed=2" in result.output


def test_cli_export_verification_failure_exits_1(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    (ctx.root / "frames" / "f0.mp4").write_bytes(b"edited, not pushed")

    result = _cli_export(ctx, monkeypatch, *IMPORT)

    assert result.exit_code == 1
    assert "FAILED frames/f0.mp4" in result.output
    assert _status(ctx) == ""


def test_cli_export_reports_a_dropped_download_once(tmp_path, make_fake_drive, monkeypatch) -> None:
    from protonfs.commands.offload import offload

    ctx = _imported(tmp_path, make_fake_drive)
    offload(ctx, "frames", min_age=0)
    ctx.drive._download_dropped_files = {"f1.mp4"}

    result = _cli_export(ctx, monkeypatch, *IMPORT)

    assert result.exit_code == 1
    assert "transferred=1 skipped=0 failed=1 tracked=0" in result.output
    assert result.output.count("FAILED") == 1
    assert "FAILED frames/f1.mp4: not downloaded from Drive" in result.output


# --- final review fixes ------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_export_refuses_a_shadowed_path_before_downloading_anything(
    tmp_path, make_fake_drive, dry_run
) -> None:
    from protonfs.commands.offload import offload

    ctx = _imported(tmp_path, make_fake_drive)
    gitignore = ctx.root / ".gitignore"
    gitignore.write_bytes(b"*.mp4\n" + gitignore.read_bytes())
    _git(ctx.root, "add", ".gitignore")
    _git(ctx.root, "commit", "-q", "-m", "rule")
    offload(ctx, "frames", min_age=0)
    before = _export_snapshot(ctx)

    result = export_to_git(ctx, IMPORT, dry_run=dry_run)

    assert "store/.gitignore:1:*.mp4" in _reasons(result.refusals)["frames/f0.mp4"]
    assert ctx.drive.download_calls == []
    assert _export_snapshot(ctx) == before


def test_pull_and_refresh_never_bring_back_an_exported_file(tmp_path, make_fake_drive) -> None:
    # its Drive copy is kept; once git has moved or deleted the file, neither this clone
    # (refresh seeds it) nor another (its index entry survives) may download it again
    from protonfs.commands.pull import pull
    from protonfs.drive import RemoteEntry

    ctx = _imported(tmp_path, make_fake_drive)
    export_to_git(ctx, IMPORT)
    _git(ctx.root, "commit", "-q", "-m", "export")
    _git(ctx.root, "mv", "frames/f0.mp4", "frames/g0.mp4")
    _git(ctx.root, "commit", "-q", "-m", "rename")
    ctx = load_context(ctx.root)
    ctx.drive = make_fake_drive(
        walk_entries=[RemoteEntry(rel_path=r, is_dir=False, size=9) for r in IMPORT]
    )

    pull(ctx, None, None, False, refresh=True)
    assert ctx.drive.download_calls == []
    assert ctx.index.get("frames/f0.mp4") is None

    ctx.index.set("frames/f1.mp4", _synced_like(ctx, "frames/f1.mp4"))  # another clone's entry
    (ctx.root / "frames" / "f1.mp4").unlink()
    pull(ctx, None, None, False)
    assert ctx.drive.download_calls == []
    assert not (ctx.root / "frames" / "f0.mp4").exists()


def _synced_like(ctx, rel):
    from protonfs.index import IndexEntry

    return IndexEntry(9, 1.0, "h", "", f"{ROOT}/{rel}", "d", "present", "t")


def test_check_export_refuses_a_path_git_lfs_would_store(tmp_path, make_fake_drive) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    (ctx.root / ".gitattributes").write_text("*.mp4 filter=lfs diff=lfs merge=lfs -text\n")
    _git(ctx.root, "add", ".gitattributes")
    _git(ctx.root, "commit", "-q", "-m", "lfs")

    assert "git-LFS" in _reasons(check_export(ctx, IMPORT))["frames/f0.mp4"]


def test_cli_export_counts_each_path_once(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    (ctx.root / "frames" / "f0.mp4").write_bytes(b"edited, not pushed")

    result = _cli_export(ctx, monkeypatch, *IMPORT)

    assert "transferred=0 skipped=1 failed=1 tracked=0" in result.output


def test_export_trash_failure_message_does_not_promise_a_later_push(
    tmp_path, make_fake_drive
) -> None:
    ctx = _imported(tmp_path, make_fake_drive)
    ctx.drive._fail_trash = True

    result = export_to_git(ctx, IMPORT, trash_remote=True)

    assert len(result.failures) == 2
    for failure in result.failures:
        assert "next push" not in failure["error"]
        assert f"protonfs rm {failure['name']}" in failure["error"]


def test_export_undo_instructions_cover_files_that_did_not_exist(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    import click

    ctx = _store(tmp_path, make_fake_drive, FILES)
    import_from_git(ctx, IMPORT)
    _git(ctx.root, "commit", "-q", "-m", "import")  # no .protonfs/ignore in HEAD
    real_add = gitops.add

    def add_then_fail(root, paths):
        real_add(root, paths)
        if ".protonfs/ignore" in paths:
            raise gitops.GitError(["git", "add"], 1, "injected")

    monkeypatch.setattr("protonfs.gitops.add", add_then_fail)
    monkeypatch.setattr(
        "protonfs.gitops.reset_paths",
        lambda root, paths: (_ for _ in ()).throw(gitops.GitError(["git", "reset"], 1, "x")),
    )

    with pytest.raises(click.ClickException) as excinfo:
        export_to_git(ctx, IMPORT)

    message = excinfo.value.format_message()
    assert "--literal-pathspecs" in message
    assert f"rm {ctx.root / '.protonfs' / 'ignore'}" in message
    assert "checkout HEAD -- .gitignore" in message
