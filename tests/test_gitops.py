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
