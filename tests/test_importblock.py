# tests/test_importblock.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from protonfs import gitops, importblock

NAMES = [
    "a.bin", "#hash", "!bang", "sp ace", "trail ", "st*ar", "q?", "br[a]", "back\\slash",
    "ünï.fits", "d/e/f.mp4",
]
# A sibling each escaped line must NOT match, when the name has glob characters.
DECOYS = {"st*ar": "stXar", "q?": "qX", "br[a]": "bra", "back\\slash": "backslash"}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    store = repo / "store"
    store.mkdir()
    return store


@pytest.mark.parametrize("name", NAMES)
def test_escape_line_round_trips_through_block_paths(tmp_path: Path, name: str) -> None:
    importblock.add_paths(tmp_path, [name])
    assert importblock.block_paths(tmp_path) == {name}


def test_each_escaped_line_matches_only_its_own_file_per_git(tmp_path: Path) -> None:
    store = _repo(tmp_path)
    for rel in NAMES + list(DECOYS.values()):
        (store / rel).parent.mkdir(parents=True, exist_ok=True)
        (store / rel).write_bytes(b"x")

    importblock.add_paths(store, NAMES)
    hits = gitops.check_ignore(store, NAMES + list(DECOYS.values()))

    assert set(hits) == set(NAMES)
    assert all(".gitignore:" in hit for hit in hits.values())


def test_add_paths_dedupes_into_one_block_and_preserves_user_lines(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.tmp\n# mine\n")

    first = importblock.add_paths(tmp_path, ["a.bin", "b.bin"])
    second = importblock.add_paths(tmp_path, ["b.bin", "c.bin"])

    text = (tmp_path / ".gitignore").read_text()
    assert first == ["/a.bin", "/b.bin"]
    assert second == ["/c.bin"]
    assert text.startswith("*.tmp\n# mine\n")
    assert text.count(importblock.BLOCK_START) == 1
    assert text.count("/b.bin\n") == 1
    assert importblock.block_paths(tmp_path) == {"a.bin", "b.bin", "c.bin"}


def test_add_paths_creates_the_file_when_absent(tmp_path: Path) -> None:
    importblock.add_paths(tmp_path, ["a.bin"])

    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines == [importblock.BLOCK_START, "/a.bin", importblock.BLOCK_END]


def test_add_paths_appends_after_a_file_without_a_trailing_newline(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.tmp")

    importblock.add_paths(tmp_path, ["a.bin"])

    assert (tmp_path / ".gitignore").read_text().splitlines()[0] == "*.tmp"


def test_remove_paths_drops_an_emptied_block(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.tmp\n")
    importblock.add_paths(tmp_path, ["a.bin", "b.bin"])

    assert importblock.remove_paths(tmp_path, ["a.bin"]) == ["/a.bin"]
    assert importblock.block_paths(tmp_path) == {"b.bin"}
    importblock.remove_paths(tmp_path, ["b.bin"])

    assert (tmp_path / ".gitignore").read_text() == "*.tmp\n"


def test_block_paths_is_empty_without_a_file_or_block(tmp_path: Path) -> None:
    assert importblock.block_paths(tmp_path) == set()
    (tmp_path / ".gitignore").write_text("/a.bin\n")
    assert importblock.block_paths(tmp_path) == set()


def test_add_paths_keeps_crlf_line_endings_of_user_lines(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_bytes(b"*.tmp\r\n# mine\r\n")

    importblock.add_paths(tmp_path, ["a.bin"])

    data = (tmp_path / ".gitignore").read_bytes()
    assert data.startswith(b"*.tmp\r\n# mine\r\n")
    assert b"\n" not in data.replace(b"\r\n", b"")


def test_a_non_utf8_gitignore_is_read_and_kept_byte_for_byte(tmp_path: Path) -> None:
    # push --deletions trash reads the block; a latin-1 .gitignore must not crash it.
    (tmp_path / ".gitignore").write_bytes(b"caf\xe9.tmp\n")

    assert importblock.block_paths(tmp_path) == set()
    importblock.add_paths(tmp_path, ["a.bin"])

    assert (tmp_path / ".gitignore").read_bytes().startswith(b"caf\xe9.tmp\n")
    assert importblock.block_paths(tmp_path) == {"a.bin"}
