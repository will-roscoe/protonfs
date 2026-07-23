from __future__ import annotations

import hashlib
from pathlib import Path

from protonfs.ignore import IgnoreMatcher
from protonfs.index import IndexEntry, IndexStore
from protonfs.lfs import POINTER_SIGNATURE
from protonfs.localscan import hash_file, hash_file_digests, scan


def test_hash_file_matches_hashlib_reference(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello world")
    assert hash_file(f) == hashlib.sha256(b"hello world").hexdigest()


def test_hash_file_digests_returns_both_sha256_and_sha1(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello world")
    sha256, sha1 = hash_file_digests(f)
    assert sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert sha1 == hashlib.sha1(b"hello world").hexdigest()


def test_scan_finds_files_and_computes_hash(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path("."), ignore, index, low_io=False)

    assert "run1/dump_0001" in result
    assert result["run1/dump_0001"].sha256 == hashlib.sha256(b"data").hexdigest()
    assert result["run1/dump_0001"].sha1 == hashlib.sha1(b"data").hexdigest()
    assert result["run1/dump_0001"].size == 4


def test_scan_skips_ignored_files(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "keep").write_bytes(b"x")
    (tmp_path / "run1" / "scratch.tmp").write_bytes(b"y")
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher(["*.tmp"])

    result = scan(tmp_path, Path("."), ignore, index, low_io=False)

    assert "run1/keep" in result
    assert "run1/scratch.tmp" not in result


def test_scan_low_io_trusts_cached_hash_when_size_and_mtime_match(tmp_path: Path) -> None:
    f = tmp_path / "dump_0001"
    f.write_bytes(b"data")
    stat = f.stat()
    index = IndexStore(tmp_path)
    # Seed the index with a deliberately WRONG hash to prove scan() trusts the
    # cache rather than recomputing when low_io=True and size/mtime match.
    index.set(
        "dump_0001",
        IndexEntry(
            size=stat.st_size,
            mtime=stat.st_mtime,
            sha256="wrong-hash-proves-cache-was-used",
            sha1="wrong-sha1-proves-cache-was-used",
            remote_path="/x",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path("."), ignore, index, low_io=True)

    assert result["dump_0001"].sha256 == "wrong-hash-proves-cache-was-used"
    assert result["dump_0001"].sha1 == "wrong-sha1-proves-cache-was-used"


def test_scan_low_io_recomputes_when_size_differs(tmp_path: Path) -> None:
    f = tmp_path / "dump_0001"
    f.write_bytes(b"data")
    index = IndexStore(tmp_path)
    index.set(
        "dump_0001",
        IndexEntry(
            size=999999,  # deliberately wrong, forces a cache miss
            mtime=f.stat().st_mtime,
            sha256="stale",
            sha1="stale-sha1",
            remote_path="/x",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path("."), ignore, index, low_io=True)

    assert result["dump_0001"].sha256 == hashlib.sha256(b"data").hexdigest()
    assert result["dump_0001"].sha1 == hashlib.sha1(b"data").hexdigest()


def test_scan_respects_include_allowlist_across_nested_dirs(tmp_path: Path) -> None:
    # Directory descent must not require any `!*/`-style trick: a plain include pattern
    # should reach files nested arbitrarily deep, since scan() walks all directories
    # unconditionally and only applies ignore/include filtering to file paths (#18).
    (tmp_path / "run1" / "nested").mkdir(parents=True)
    (tmp_path / "run1" / "nested" / "dump.ev").write_bytes(b"keep")
    (tmp_path / "run1" / "notes.txt").write_bytes(b"drop")
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher([], include_patterns=["*.ev"])

    result = scan(tmp_path, Path("."), ignore, index, low_io=False)

    assert "run1/nested/dump.ev" in result
    assert "run1/notes.txt" not in result


def test_scan_marks_pointer_stub_as_lfs_pointer(tmp_path: Path) -> None:
    f = tmp_path / "big.bin"
    f.write_text(
        f"{POINTER_SIGNATURE}\n"
        "oid sha256:9e5f00000000000000000000000000000000000000000000000000000000\n"
        "size 171008\n"
    )
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path("."), ignore, index, low_io=False)

    assert result["big.bin"].is_lfs_pointer is True


def test_scan_normal_small_file_is_not_lfs_pointer(tmp_path: Path) -> None:
    f = tmp_path / "small.txt"
    f.write_text("just some ordinary short content")
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path("."), ignore, index, low_io=False)

    assert result["small.txt"].is_lfs_pointer is False


def test_scan_large_file_starting_with_signature_line_follows_size_heuristic(
    tmp_path: Path,
) -> None:
    # A file that happens to start with the pointer signature line but is padded past
    # the 200-byte heuristic used by find_pointer_stubs -- must NOT be treated as a
    # pointer stub, matching the size-gated heuristic exactly.
    f = tmp_path / "large.bin"
    f.write_text(POINTER_SIGNATURE + "\n" + ("x" * 300))
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path("."), ignore, index, low_io=False)

    assert result["large.bin"].is_lfs_pointer is False


# --- file pathspecs: a subpath that resolves to a single file, not a directory --------
# Regression: `scan()` used base.rglob("*"), which yields nothing for a file (and nothing
# for a nonexistent path), so `push mload002/mload002_00134` silently scanned zero files.


def test_scan_file_subpath_returns_just_that_one_file(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    (tmp_path / "run1" / "dump_0002").write_bytes(b"other")
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path("run1/dump_0001"), ignore, index, low_io=False)

    assert set(result) == {"run1/dump_0001"}
    assert result["run1/dump_0001"].sha256 == hashlib.sha256(b"data").hexdigest()


def test_scan_file_subpath_that_is_ignored_returns_empty(tmp_path: Path) -> None:
    # Naming an ignored file explicitly still honours the ignore contract (it is not on
    # the sync allowlist); the CLI surfaces this as "nothing to push", not an upload.
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "scratch.tmp").write_bytes(b"y")
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher(["*.tmp"])

    result = scan(tmp_path, Path("run1/scratch.tmp"), ignore, index, low_io=False)

    assert result == {}


def test_scan_nonexistent_subpath_returns_empty(tmp_path: Path) -> None:
    # Load-bearing for pull/status/ls: a subpath absent locally (remote-only content,
    # offloaded data) must scan to {} rather than raise -- those commands fetch/report
    # from the index. push validates existence separately, at the CLI layer.
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path("no/such/path"), ignore, index, low_io=False)

    assert result == {}


def test_scan_file_subpath_inside_protonfs_returns_empty(tmp_path: Path) -> None:
    # The single-file branch bypasses the rglob walk, so it must still apply the
    # .protonfs control-dir skip that the walk applied implicitly.
    (tmp_path / ".protonfs").mkdir()
    (tmp_path / ".protonfs" / "index.json").write_bytes(b"{}")
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path(".protonfs/index.json"), ignore, index, low_io=False)

    assert result == {}


def test_scan_file_subpath_that_is_lfs_pointer_stub_is_flagged(tmp_path: Path) -> None:
    f = tmp_path / "big.bin"
    f.write_text(
        f"{POINTER_SIGNATURE}\n"
        "oid sha256:9e5f00000000000000000000000000000000000000000000000000000000\n"
        "size 171008\n"
    )
    index = IndexStore(tmp_path)
    ignore = IgnoreMatcher([])

    result = scan(tmp_path, Path("big.bin"), ignore, index, low_io=False)

    assert result["big.bin"].is_lfs_pointer is True
