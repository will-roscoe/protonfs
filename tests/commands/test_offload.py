from __future__ import annotations

from pathlib import Path

from protonfs.commands.offload import offload
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.diff import SyncState, classify
from protonfs.index import IndexEntry
from protonfs.localscan import scan


def _index_entry(remote_path: str, *, local_state: str = "present", size: int = 4) -> IndexEntry:
    return IndexEntry(
        size=size,
        mtime=1.0,
        sha256="h",
        sha1="",
        remote_path=remote_path,
        origin_device="d1",
        local_state=local_state,
        last_synced="2026-07-08T00:00:00+00:00",
    )


def test_offload_deletes_verified_file_and_marks_metadata_only(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    (tmp_path / "dump_0001").write_bytes(b"data")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _index_entry("/my-files/test/dump_0001", size=4))
    fake = make_fake_drive()
    ctx.drive = fake
    # Populate the remote listing so remote_identities() reports this file as present
    # with the matching plaintext size (mirrors what a verified push would have left).
    fake.upload([tmp_path / "dump_0001"], "/my-files/test")

    result = offload(ctx, None)

    assert result.offloaded == 1
    assert result.bytes_reclaimed == 4
    assert result.offloaded_paths == ["dump_0001"]
    assert not (tmp_path / "dump_0001").exists()
    entry = ctx.index.get("dump_0001")
    assert entry is not None
    assert entry.local_state == "metadata-only"


def test_offload_leaves_untracked_file_alone(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    (tmp_path / "untracked.py").write_bytes(b"source")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    result = offload(ctx, None)

    assert result.offloaded == 0
    assert (tmp_path / "untracked.py").exists()


def test_offload_leaves_already_metadata_only_entry_alone(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "gone", _index_entry("/my-files/test/gone", local_state="metadata-only")
    )
    ctx.drive = make_fake_drive()

    result = offload(ctx, None)

    assert result.offloaded == 0
    assert result.skipped_unverified == 0


def test_offload_skips_file_absent_from_remote(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    (tmp_path / "dump_0001").write_bytes(b"data")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _index_entry("/my-files/test/dump_0001", size=4))
    ctx.drive = make_fake_drive()  # nothing "uploaded" -- remote_identities returns {}

    result = offload(ctx, None)

    assert result.offloaded == 0
    assert result.skipped_unverified == 1
    assert result.skipped_paths == ["dump_0001"]
    assert (tmp_path / "dump_0001").exists()
    assert ctx.index.get("dump_0001").local_state == "present"


def test_offload_skips_file_with_mismatched_remote_size(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    (tmp_path / "dump_0001").write_bytes(b"data")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _index_entry("/my-files/test/dump_0001", size=4))
    fake = make_fake_drive(remote_size_overrides={"dump_0001": 1})
    ctx.drive = fake
    fake.upload([tmp_path / "dump_0001"], "/my-files/test")

    result = offload(ctx, None)

    assert result.offloaded == 0
    assert result.skipped_unverified == 1
    assert (tmp_path / "dump_0001").exists()


def test_offload_dry_run_deletes_nothing(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    (tmp_path / "dump_0001").write_bytes(b"data")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _index_entry("/my-files/test/dump_0001", size=4))
    fake = make_fake_drive()
    ctx.drive = fake
    fake.upload([tmp_path / "dump_0001"], "/my-files/test")

    result = offload(ctx, None, dry_run=True)

    assert result.offloaded == 1
    assert result.offloaded_paths == ["dump_0001"]
    assert (tmp_path / "dump_0001").exists()
    assert ctx.index.get("dump_0001").local_state == "present"


def test_offload_subpath_scoping_leaves_outside_files_untouched(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "in_scope").write_bytes(b"data")
    (tmp_path / "outside").write_bytes(b"data")
    ctx = load_context(tmp_path)
    ctx.index.set("subdir/in_scope", _index_entry("/my-files/test/subdir/in_scope", size=4))
    ctx.index.set("outside", _index_entry("/my-files/test/outside", size=4))
    fake = make_fake_drive()
    ctx.drive = fake
    fake.upload([tmp_path / "subdir" / "in_scope"], "/my-files/test/subdir")
    fake.upload([tmp_path / "outside"], "/my-files/test")

    result = offload(ctx, "subdir")

    assert result.offloaded == 1
    assert result.offloaded_paths == ["subdir/in_scope"]
    assert not (tmp_path / "subdir" / "in_scope").exists()
    assert (tmp_path / "outside").exists()
    assert ctx.index.get("outside").local_state == "present"


def test_offload_reversible_via_classify_metadata_only(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    (tmp_path / "dump_0001").write_bytes(b"data")
    ctx = load_context(tmp_path)
    ctx.index.set("dump_0001", _index_entry("/my-files/test/dump_0001", size=4))
    fake = make_fake_drive()
    ctx.drive = fake
    fake.upload([tmp_path / "dump_0001"], "/my-files/test")

    offload(ctx, None)

    from protonfs.ignore import IgnoreMatcher

    ignore = IgnoreMatcher.from_file(tmp_path)
    local = scan(tmp_path, Path("."), ignore, ctx.index, low_io=False)
    diff_entries = classify(local, ctx.index)
    states = {e.rel_path: e.state for e in diff_entries}
    assert states["dump_0001"] == SyncState.METADATA_ONLY
