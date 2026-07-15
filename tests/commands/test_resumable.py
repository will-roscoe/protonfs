"""Resumability / idempotency of push & pull (#3).

Re-running after a Ctrl-C or dropped connection must converge: no double-upload, and a
file is only marked present once its bytes are confirmed (on the remote for push, on disk
for pull). Progress is persisted per parent group so an interrupted run resumes.
"""

from __future__ import annotations

from pathlib import Path

from protonfs.commands.pull import pull
from protonfs.commands.push import push
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import RemoteEntry
from protonfs.index import IndexStore


def test_push_is_idempotent_no_double_upload(tmp_path: Path, make_fake_drive) -> None:
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    first = push(ctx, None, resolve=None, dry_run=False)
    uploads_after_first = len(fake.upload_calls)
    second = push(ctx, None, resolve=None, dry_run=False)

    assert first.transferred_items == 1
    assert second.transferred_items == 0  # already synced -> nothing to do
    assert len(fake.upload_calls) == uploads_after_first  # no second upload


def test_push_persists_index_per_parent_group(
    tmp_path: Path, make_fake_drive, monkeypatch
) -> None:
    # Two parent groups -> the index is saved incrementally (per group), so an interruption
    # after group N leaves groups 1..N durably indexed instead of losing the whole run.
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "a").write_bytes(b"a")
    (tmp_path / "run2").mkdir()
    (tmp_path / "run2" / "b").write_bytes(b"b")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    saves = {"n": 0}
    original = ctx.index.save

    def counting_save() -> None:
        saves["n"] += 1
        original()

    monkeypatch.setattr(ctx.index, "save", counting_save)
    push(ctx, None, resolve=None, dry_run=False)

    assert saves["n"] >= 2  # persisted per group, not only once at the very end


def test_push_interrupted_on_later_group_keeps_earlier_group_indexed(
    tmp_path: Path, make_fake_drive
) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "a").write_bytes(b"a")
    (tmp_path / "run2").mkdir()
    (tmp_path / "run2" / "b").write_bytes(b"b")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    # Simulate a dropped connection on whichever group is processed second.
    real_upload = fake.upload
    seen: list[str] = []

    def flaky_upload(local_paths, remote_parent, **kwargs):
        seen.append(remote_parent)
        if len(seen) > 1:  # second group's upload fails hard
            raise RuntimeError("connection dropped")
        return real_upload(local_paths, remote_parent, **kwargs)

    fake.upload = flaky_upload

    try:
        push(ctx, None, resolve=None, dry_run=False)
    except RuntimeError:
        pass

    # The first group completed and was persisted to disk before the interruption; a fresh
    # IndexStore (as a re-run would load) sees it, so the re-run will not re-upload it.
    reloaded = IndexStore(tmp_path)
    first_parent = Path(seen[0]).name  # "run1" or "run2", whichever ran first
    assert reloaded.get(f"{first_parent}/{'a' if first_parent == 'run1' else 'b'}") is not None


def test_pull_is_idempotent(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        walk_entries=[RemoteEntry(rel_path="dump_0001", is_dir=False, size=4)]
    )

    first = pull(ctx, None, resolve=None, dry_run=False, refresh=True)
    second = pull(ctx, None, resolve=None, dry_run=False, refresh=True)

    assert first.transferred_items == 1
    assert second.transferred_items == 0  # already present + indexed -> nothing to pull
