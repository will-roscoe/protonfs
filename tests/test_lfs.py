# tests/test_lfs.py
from __future__ import annotations

import subprocess
from pathlib import Path

from protonfs.lfs import find_pointer_stubs, is_lfs_tracked, is_pointer_stub


def test_is_lfs_tracked_true_when_gitattributes_has_lfs_filter(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text("sim/*/* filter=lfs diff=lfs merge=lfs -text\n")
    assert is_lfs_tracked(tmp_path) is True


def test_is_lfs_tracked_false_when_no_gitattributes_and_no_lfs_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert is_lfs_tracked(tmp_path) is False


def test_is_pointer_stub_true_for_real_lfs_pointer(tmp_path: Path) -> None:
    stub = tmp_path / "pointer"
    stub.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:abc123\n"
        "size 12345\n"
    )
    assert is_pointer_stub(stub) is True


def test_is_pointer_stub_false_for_real_binary_file(tmp_path: Path) -> None:
    real_file = tmp_path / "dump_0001"
    real_file.write_bytes(b"\x00\x01\x02real binary data")
    assert is_pointer_stub(real_file) is False


def test_find_pointer_stubs_finds_only_stubs(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    stub = tmp_path / "run1" / "dump_0001"
    stub.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 1\n")
    real = tmp_path / "run1" / "dump_0002"
    real.write_bytes(b"real data")

    found = find_pointer_stubs(tmp_path, Path("."))

    assert found == [stub]
