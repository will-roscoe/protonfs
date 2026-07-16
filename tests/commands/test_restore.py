from __future__ import annotations

from pathlib import Path

from protonfs.commands.restore import restore
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.index import IndexEntry


def test_restore_uses_indexed_remote_path(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            sha1="",
            remote_path="/my-files/test/dump_0001",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    fake = make_fake_drive()
    ctx.drive = fake

    restore(ctx, "dump_0001")

    assert fake.restored == ["/my-files/test/dump_0001"]


def test_restore_falls_back_to_computed_path_when_not_indexed(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    restore(ctx, "dump_0002")

    assert fake.restored == ["/my-files/test/dump_0002"]
