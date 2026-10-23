# tests/test_gitops.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from protonfs import gitops


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path, files: dict[str, bytes], store: str = "store") -> Path:
    """A git repo at ``tmp_path/r`` with ``files`` committed under ``r/<store>``; returns
    the store directory."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    root = repo / store
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return root


def test_in_work_tree(tmp_path: Path) -> None:
    store = _repo(tmp_path, {"a.bin": b"a"})
    assert gitops.in_work_tree(store)
    outside = tmp_path / "plain"
    outside.mkdir()
    assert not gitops.in_work_tree(outside)


def test_tracked_files_are_relative_to_a_subdirectory_store(tmp_path: Path) -> None:
    store = _repo(tmp_path, {"a.bin": b"a", "d/b.bin": b"b"})
    (store.parent / "outside.txt").write_text("x")
    _git(store.parent, "add", "outside.txt")
    _git(store.parent, "commit", "-q", "-m", "outside")

    assert gitops.tracked_files(store) == {"a.bin", "d/b.bin"}


def test_head_blobs_returns_the_committed_blob_id(tmp_path: Path) -> None:
    store = _repo(tmp_path, {"a.bin": b"hello", "d/b.bin": b"world"})
    expected = _git(store, "hash-object", "a.bin").strip()

    blobs = gitops.head_blobs(store, ["a.bin", "d/b.bin", "missing.bin"])

    assert blobs["a.bin"] == expected
    assert set(blobs) == {"a.bin", "d/b.bin"}


def test_dirty_paths_reports_staged_unstaged_and_deleted(tmp_path: Path) -> None:
    store = _repo(
        tmp_path, {"staged.bin": b"1", "unstaged.bin": b"2", "deleted.bin": b"3", "clean.bin": b"4"}
    )
    (store / "staged.bin").write_bytes(b"1x")
    _git(store, "add", "staged.bin")
    (store / "unstaged.bin").write_bytes(b"2x")
    (store / "deleted.bin").unlink()

    dirty = gitops.dirty_paths(
        store, ["staged.bin", "unstaged.bin", "deleted.bin", "clean.bin"]
    )

    assert dirty == {"staged.bin", "unstaged.bin", "deleted.bin"}


def test_rm_cached_keeps_the_file_on_disk_and_untracks_it(tmp_path: Path) -> None:
    store = _repo(tmp_path, {"a.bin": b"a", "b.bin": b"b"})

    gitops.rm_cached(store, ["a.bin"])

    assert (store / "a.bin").read_bytes() == b"a"
    assert gitops.tracked_files(store) == {"b.bin"}


def test_rm_cached_handles_3000_paths(tmp_path: Path) -> None:
    files = {f"d{i // 100}/f{i:04d}-with-a-longish-name.bin": b"" for i in range(3000)}
    store = _repo(tmp_path, files)

    gitops.rm_cached(store, sorted(files))

    assert gitops.tracked_files(store) == set()
    assert len(gitops.head_blobs(store, sorted(files))) == 3000


def test_add_and_reset_paths_round_trip(tmp_path: Path) -> None:
    store = _repo(tmp_path, {"a.bin": b"a"})
    gitops.rm_cached(store, ["a.bin"])

    gitops.reset_paths(store, ["a.bin"])
    assert gitops.tracked_files(store) == {"a.bin"}

    (store / "new.txt").write_text("n")
    gitops.add(store, ["new.txt"])
    assert "new.txt" in gitops.tracked_files(store)


def test_check_ignore_names_the_rule(tmp_path: Path) -> None:
    store = _repo(tmp_path, {"a.bin": b"a"})
    (store / ".gitignore").write_text("*.tmp\n")
    (store / "x.tmp").write_text("t")

    hits = gitops.check_ignore(store, ["x.tmp", "a.bin"])

    assert set(hits) == {"x.tmp"}
    assert hits["x.tmp"].endswith(":1:*.tmp")


def test_git_failure_raises_git_error_with_stderr(tmp_path: Path) -> None:
    store = _repo(tmp_path, {"a.bin": b"a"})

    with pytest.raises(gitops.GitError) as excinfo:
        gitops.rm_cached(store, ["no-such-file.bin"])

    assert excinfo.value.returncode != 0
    assert "no-such-file.bin" in excinfo.value.stderr


def test_paths_are_literal_not_globs(tmp_path: Path) -> None:
    store = _repo(tmp_path, {"st*ar.bin": b"1", "star.bin": b"2", "stXar.bin": b"3"})

    gitops.rm_cached(store, ["st*ar.bin"])

    assert gitops.tracked_files(store) == {"star.bin", "stXar.bin"}
    assert gitops.dirty_paths(store, ["st*ar.bin"]) == {"st*ar.bin"}
    gitops.reset_paths(store, ["st*ar.bin"])
    assert gitops.dirty_paths(store, ["st*ar.bin", "star.bin"]) == set()


def test_add_and_reset_never_use_pathspec_from_file(tmp_path: Path, monkeypatch) -> None:
    # git < 2.25 has no --pathspec-from-file; the forward step and the rollback must
    # both work there (review finding: rollback needed a newer git than the forward step).
    store = _repo(tmp_path, {f"f{i}.bin": b"" for i in range(1500)})
    real = subprocess.run
    argvs = []

    def spy(cmd, *a, **k):
        argvs.append(cmd)
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    paths = sorted(f"f{i}.bin" for i in range(1500))
    gitops.rm_cached(store, paths)
    gitops.reset_paths(store, paths)
    (store / "new.txt").write_text("n")
    gitops.add(store, ["new.txt"])

    assert not any("--pathspec-from-file=-" in cmd for cmd in argvs)
    assert {f"f{i}.bin" for i in range(1500)} <= gitops.tracked_files(store)


def test_file_modes_report_symlinks_and_regular_files(tmp_path: Path) -> None:
    import os

    store = _repo(tmp_path, {"run3.mp4": b"m"})
    os.symlink("run3.mp4", store / "latest.mp4")
    _git(store, "add", "latest.mp4")
    _git(store, "commit", "-q", "-m", "link")

    modes = gitops.file_modes(store, ["run3.mp4", "latest.mp4"])

    assert modes == {"run3.mp4": "100644", "latest.mp4": "120000"}


def test_attribute_reports_filter_per_path(tmp_path: Path) -> None:
    store = _repo(tmp_path, {"a.bin": b"a", "b.txt": b"b"})
    (store / ".gitattributes").write_text("*.bin filter=lfs\n")

    assert gitops.attribute(store, ["a.bin", "b.txt"], "filter") == {"a.bin": "lfs"}


def test_hash_object_applies_the_paths_conversions(tmp_path: Path) -> None:
    store = _repo(tmp_path, {".gitattributes": b"*.csv text eol=crlf\n", "t.csv": b"a\nb\n"})
    (store / "t.csv").unlink()
    _git(store, "checkout", "--", "t.csv")  # re-checkout writes CRLF
    assert (store / "t.csv").read_bytes() == b"a\r\nb\r\n"

    assert gitops.hash_object(store, "t.csv") == gitops.head_blobs(store, ["t.csv"])["t.csv"]


def test_check_ignore_with_judges_a_replaced_store_gitignore_without_writing_it(
    tmp_path: Path,
) -> None:
    store = _repo(tmp_path, {"d/a.mp4": b"a", "d/b.bin": b"b", "d/c.txt": b"c"})
    repo = store.parent
    (repo / ".gitignore").write_text("*.mp4\n")
    (repo / ".git" / "info" / "exclude").write_text("b.bin\n")
    (store / ".gitignore").write_bytes(b"/d/a.mp4\n/d/c.txt\n")

    hits = gitops.check_ignore_with(store, ["d/a.mp4", "d/b.bin", "d/c.txt"], b"!c.txt\n")

    assert hits["d/a.mp4"] == ".gitignore:1:*.mp4"  # shadowed by the store's own line
    assert hits["d/b.bin"].endswith("info/exclude:1:b.bin")
    assert "d/c.txt" not in hits
    assert (store / ".gitignore").read_bytes() == b"/d/a.mp4\n/d/c.txt\n"  # untouched
    assert gitops.check_ignore_with(store, ["d/a.mp4"], None) == {
        "d/a.mp4": ".gitignore:1:*.mp4"
    }
