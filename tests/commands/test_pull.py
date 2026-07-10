# tests/commands/test_pull.py
from __future__ import annotations

from pathlib import Path

from protonfs.commands.pull import pull
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import RemoteEntry
from protonfs.index import IndexEntry


def test_pull_downloads_metadata_only_files_and_updates_index(
    tmp_path: Path, make_fake_drive
) -> None:
    # NOTE: seeds local_state="metadata-only", so this exercises the METADATA_ONLY
    # path (renamed from the misleading "...remote_only..." name; see the dedicated
    # REMOTE_ONLY test below).
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "run1/dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="placeholder",
            remote_path="/my-files/test/run1/dump_0001",
            origin_device="other-device",
            local_state="metadata-only",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.drive = make_fake_drive()

    result = pull(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 1
    assert (tmp_path / "run1" / "dump_0001").exists()
    updated = ctx.index.get("run1/dump_0001")
    assert updated.local_state == "present"
    assert updated.origin_device == "other-device"  # origin is preserved, not overwritten


def test_pull_downloads_true_remote_only_file(tmp_path: Path, make_fake_drive) -> None:
    # A genuine REMOTE_ONLY: the index says the file is present (local_state !=
    # metadata-only) but it is absent on disk, so classify -> REMOTE_ONLY and pull
    # re-downloads it. (v0.1 review gap: no end-to-end REMOTE_ONLY coverage.)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "gone/dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            remote_path="/my-files/test/gone/dump_0001",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.drive = make_fake_drive()

    result = pull(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 1
    assert (tmp_path / "gone" / "dump_0001").exists()
    assert ctx.index.get("gone/dump_0001").local_state == "present"


def test_pull_multiple_parent_groups_downloads_all(tmp_path: Path, make_fake_drive) -> None:
    # multi-group coverage (v0.1 review gap): metadata-only entries under different
    # parents are all fetched.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    for rel in ("run1/a", "run2/b"):
        ctx.index.set(
            rel,
            IndexEntry(
                size=1,
                mtime=1.0,
                sha256="h",
                remote_path=f"/my-files/test/{rel}",
                origin_device="d1",
                local_state="metadata-only",
                last_synced="2026-07-08T00:00:00+00:00",
            ),
        )
    ctx.drive = make_fake_drive()

    result = pull(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 2
    assert (tmp_path / "run1" / "a").exists()
    assert (tmp_path / "run2" / "b").exists()


def test_pull_dry_run_does_not_call_download(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "dump_0001",
        IndexEntry(
            1,
            1.0,
            "h",
            "/my-files/test/dump_0001",
            "d1",
            "metadata-only",
            "2026-07-08T00:00:00+00:00",
        ),
    )
    fake = make_fake_drive()
    ctx.drive = fake

    result = pull(ctx, None, resolve=None, dry_run=True)

    assert result.transferred_items == 1
    assert fake.download_calls == []


def test_pull_no_remote_only_files_returns_zero_result(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    result = pull(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 0


def test_pull_refresh_seeds_then_downloads_on_empty_index(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(walk_entries=[RemoteEntry("run1/dump_0001", is_dir=False, size=9)])
    ctx.drive = fake

    # empty index: a bare pull would do nothing; with refresh=True it seeds then pulls
    result = pull(ctx, None, resolve=None, dry_run=False, refresh=True)

    assert result.transferred_items == 1
    assert (tmp_path / "run1" / "dump_0001").exists()


def test_pull_without_refresh_on_empty_index_is_noop(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    result = pull(ctx, None, resolve=None, dry_run=False, refresh=False)

    assert result.transferred_items == 0


def test_pull_refresh_dry_run_previews_seeded_files_without_persisting(
    tmp_path: Path, make_fake_drive
) -> None:
    # pull --refresh --dry-run must preview the files a real pull --refresh would
    # fetch (seeding in-memory), but must NOT persist the seed to index.json.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(walk_entries=[RemoteEntry("run1/dump_0001", is_dir=False, size=9)])
    ctx.drive = fake

    result = pull(ctx, None, resolve=None, dry_run=True, refresh=True)

    assert result.transferred_items == 1  # accurate preview, not a stale 0
    assert fake.download_calls == []  # dry-run downloads nothing
    # dry-run left the on-disk index untouched
    assert load_context(tmp_path).index.all() == {}


def test_pull_cli_empty_index_without_refresh_prints_hint(
    tmp_path: Path, monkeypatch
) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)  # empty index
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["pull"])

    assert result.exit_code == 0
    assert "protonfs refresh" in result.output
    assert "pull --refresh" in result.output
