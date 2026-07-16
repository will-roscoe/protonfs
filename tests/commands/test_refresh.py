# tests/commands/test_refresh.py
from __future__ import annotations

from pathlib import Path

from protonfs.commands.refresh import refresh
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import RemoteEntry
from protonfs.index import IndexEntry


def test_refresh_seeds_metadata_only_for_new_remote_files(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        walk_entries=[
            RemoteEntry("run1", is_dir=True, size=0),
            RemoteEntry("run1/dump_0001", is_dir=False, size=100),
        ]
    )

    result = refresh(ctx, None, prune=False)

    assert result.seeded == 1
    entry = ctx.index.get("run1/dump_0001")
    assert entry is not None
    assert entry.local_state == "metadata-only"
    assert entry.size == 100
    assert entry.sha256 == ""
    assert entry.remote_path == "/my-files/test/run1/dump_0001"


def test_refresh_is_idempotent(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(walk_entries=[RemoteEntry("a", is_dir=False, size=10)])
    refresh(ctx, None, prune=False)
    result2 = refresh(ctx, None, prune=False)
    assert result2.seeded == 0


def test_refresh_flags_remote_deleted_without_prune(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "gone",
        IndexEntry(
            1,
            1.0,
            "",
            "",
            "/my-files/test/gone",
            "d1",
            "metadata-only",
            "2026-07-09T00:00:00+00:00",
        ),
    )
    ctx.drive = make_fake_drive(walk_entries=[])  # remote empty -> 'gone' is remote-deleted

    result = refresh(ctx, None, prune=False)

    assert result.remote_deleted == 1
    assert "gone" in result.deleted_paths
    assert ctx.index.get("gone") is not None  # NOT pruned without --prune


def test_refresh_prune_removes_remote_deleted(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "gone",
        IndexEntry(
            1,
            1.0,
            "",
            "",
            "/my-files/test/gone",
            "d1",
            "metadata-only",
            "2026-07-09T00:00:00+00:00",
        ),
    )
    ctx.drive = make_fake_drive(walk_entries=[])

    result = refresh(ctx, None, prune=True)

    assert result.pruned == 1
    assert ctx.index.get("gone") is None


def test_refresh_subpath_seeds_reprefixed_paths(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    # walk is scoped to remote_root/run5 and returns paths relative to that root;
    # refresh must re-prefix them with the subpath to match index rel_path keys.
    ctx.drive = make_fake_drive(walk_entries=[RemoteEntry("dump_0002", is_dir=False, size=200)])

    result = refresh(ctx, "run5", prune=False)

    assert result.seeded == 1
    entry = ctx.index.get("run5/dump_0002")
    assert entry is not None
    assert entry.size == 200
    assert entry.remote_path == "/my-files/test/run5/dump_0002"


def test_refresh_subpath_prune_leaves_out_of_scope_index_entries(
    tmp_path: Path, make_fake_drive
) -> None:
    # Regression: a subpath-scoped refresh --prune must NOT classify/prune index
    # entries that live outside the walked subpath. The remote walk never visited
    # them, so their absence from the (scoped) remote map is not a deletion signal.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "otherdir/unrelated.bin",
        IndexEntry(
            5,
            1.0,
            "",
            "",
            "/my-files/test/otherdir/unrelated.bin",
            "d1",
            "metadata-only",
            "2026-07-09T00:00:00+00:00",
        ),
    )
    ctx.index.save()
    ctx.drive = make_fake_drive(walk_entries=[RemoteEntry("newfile", is_dir=False, size=100)])

    result = refresh(ctx, "somedir", prune=True)

    # out-of-scope entry is untouched
    assert result.remote_deleted == 0
    assert result.pruned == 0
    assert ctx.index.get("otherdir/unrelated.bin") is not None
    # in-scope new file is still seeded
    assert ctx.index.get("somedir/newfile") is not None


def test_refresh_flags_remote_changed(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "f",
        IndexEntry(
            10, 1.0, "", "", "/my-files/test/f", "d1", "metadata-only", "2026-07-09T00:00:00+00:00"
        ),
    )
    ctx.drive = make_fake_drive(walk_entries=[RemoteEntry("f", is_dir=False, size=999)])

    result = refresh(ctx, None, prune=False)

    assert result.remote_changed == 1
    assert "f" in result.changed_paths


def test_refresh_persists_seeded_entries_before_a_throttle_interruption(
    tmp_path: Path, make_fake_drive
) -> None:
    # #33: seeding is per-directory and persisted, so if the walk wedges under throttle on
    # a later directory, the progress already made survives for the next run.
    import pytest

    from protonfs.drive import DriveThrottleError
    from protonfs.index import IndexStore

    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    def wedging_walk(remote_root, on_directory=None, *, sleep=None):
        # First directory seeds and persists...
        on_directory([RemoteEntry(rel_path="run1/a", is_dir=False, size=4)])
        # ...then the next directory throttles past the retry budget.
        raise DriveThrottleError("remote is throttling `list /root/run2`")

    ctx.drive.walk = wedging_walk

    with pytest.raises(DriveThrottleError):
        refresh(ctx, None, prune=False)

    # A fresh IndexStore (what a re-run loads) sees the seeded entry -> progress not lost.
    reloaded = IndexStore(tmp_path)
    assert reloaded.get("run1/a") is not None
    assert reloaded.get("run1/a").local_state == "metadata-only"
