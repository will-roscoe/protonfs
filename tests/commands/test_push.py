from __future__ import annotations

from pathlib import Path

from protonfs.commands.push import LFS_POINTER_KIND, push
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.diff import DiffEntry, SyncState
from protonfs.drive import TransferResult
from protonfs.lfs import POINTER_SIGNATURE


def test_push_uploads_local_only_files_and_updates_index(
    tmp_path: Path, make_fake_drive
) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 1
    assert fake.upload_calls[0][1] == "/my-files/test/run1"
    assert ctx.index.get("run1/dump_0001") is not None
    assert ctx.index.get("run1/dump_0001").remote_path == "/my-files/test/run1/dump_0001"


def test_push_dry_run_does_not_call_upload(tmp_path: Path, make_fake_drive) -> None:
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=True)

    assert result.transferred_items == 1  # reported as "would transfer"
    assert fake.upload_calls == []


def test_push_no_files_to_push_returns_zero_result(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 0


def test_push_does_not_index_failed_files(tmp_path: Path, make_fake_drive) -> None:
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=0,
            skipped_items=0,
            failed_items=1,
            failures=[{"name": "dump_0001", "error": "conflict"}],
        )
    )

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.failed_items == 1
    assert ctx.index.get("dump_0001") is None


def test_push_multiple_parent_groups_all_uploaded_and_indexed(
    tmp_path: Path, make_fake_drive
) -> None:
    # multi-group coverage (v0.1 review gap): files under different parents become
    # separate upload calls, and every successful file is indexed.
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "a").write_bytes(b"a")
    (tmp_path / "run2").mkdir()
    (tmp_path / "run2" / "b").write_bytes(b"b")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 2
    assert sorted(call[1] for call in fake.upload_calls) == [
        "/my-files/test/run1",
        "/my-files/test/run2",
    ]
    assert ctx.index.get("run1/a") is not None
    assert ctx.index.get("run2/b") is not None


def test_push_default_passes_no_conflict_strategy(tmp_path: Path, make_fake_drive) -> None:
    # D2.1: with no --resolve, push must NOT apply a conflict strategy (not even the
    # config default "skip") so conflicts come back as named failures, never silent skips.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    push(ctx, None, resolve=None, dry_run=False)

    assert fake.upload_calls[0][2] is None  # file_strategy passed to upload


def test_push_explicit_resolve_replace_passes_strategy(
    tmp_path: Path, make_fake_drive
) -> None:
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    push(ctx, None, resolve="replace", dry_run=False)

    assert fake.upload_calls[0][2] == "replace"


def test_push_resolve_skip_leaves_skipped_files_unindexed(
    tmp_path: Path, make_fake_drive
) -> None:
    # D2.1: --resolve=skip returns only an aggregate skippedItems count, so when a
    # batch reports any skip we cannot tell which file was skipped -> index none of
    # the batch's non-failed files (conservative; never records an unconfirmed hash).
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=0, skipped_items=1, failed_items=0, failures=[]
        )
    )

    result = push(ctx, None, resolve="skip", dry_run=False)

    assert result.skipped_items == 1
    assert ctx.index.get("dump_0001") is None  # not marked present on an ambiguous skip


def test_push_skip_with_mixed_batch_indexes_nothing_in_that_batch(
    tmp_path: Path, make_fake_drive
) -> None:
    # D2.1: a single batch may report transferred AND skipped together (aggregate
    # counts). Since we cannot tell which file was skipped, ANY skip in the batch
    # means none of its non-failed files are indexed. Locks in the conservative rule.
    (tmp_path / "a").write_bytes(b"aa")
    (tmp_path / "b").write_bytes(b"bb")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    # both files are one batch (same parent); report 1 transferred + 1 skipped
    ctx.drive = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=1, skipped_items=1, failed_items=0, failures=[]
        )
    )

    push(ctx, None, resolve="skip", dry_run=False)

    assert ctx.index.get("a") is None
    assert ctx.index.get("b") is None


def test_push_silent_drop_is_caught_and_not_indexed(tmp_path: Path, make_fake_drive) -> None:
    # #22: proton-drive reports the file transferred (count=1) but it never lands on the
    # remote. Verification against the remote must catch this: not indexed, reported failed,
    # and the honest transferred count is 0 -- not proton-drive's lie.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(
        dropped_files={"dump_0001"},
        upload_result=TransferResult(
            transferred_items=1, skipped_items=0, failed_items=0, failures=[]
        ),
    )
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.index.get("dump_0001") is None
    assert result.transferred_items == 0
    assert result.failed_items == 1
    assert "/my-files/test" in fake.identity_calls  # actually verified against the remote


def test_push_size_mismatch_is_treated_as_under_delivery(
    tmp_path: Path, make_fake_drive
) -> None:
    # A partial/truncated upload: present on the remote, but plaintext claimedSize does not
    # match the local size -> not verified, not indexed.
    (tmp_path / "dump_0001").write_bytes(b"the full contents")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(remote_size_overrides={"dump_0001": 3})

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.index.get("dump_0001") is None
    assert result.failed_items == 1


def test_push_partial_drop_indexes_only_verified_files(
    tmp_path: Path, make_fake_drive
) -> None:
    # One file lands, one is silently dropped: only the verified file is indexed.
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "a").write_bytes(b"a")
    (tmp_path / "run1" / "b").write_bytes(b"b")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(dropped_files={"b"})

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.index.get("run1/a") is not None
    assert ctx.index.get("run1/b") is None
    assert result.transferred_items == 1
    assert result.failed_items == 1


def test_push_cli_under_delivery_prints_retry_hint_not_resolve(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # An under-delivery is not a conflict: the CLI must NOT suggest --resolve (wrong remedy),
    # and must tell the user it will retry on the next push.
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(dropped_files={"dump_0001"})
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["push"])

    assert result.exit_code != 0
    assert "retried on the next push" in result.output
    assert "--resolve" not in result.output


def test_push_cli_conflict_failure_prints_resolve_hint(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # D2.1: a default push that hits conflicts (named failures) instructs the user to
    # re-run with --resolve, and exits non-zero.
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=0,
            skipped_items=0,
            failed_items=1,
            failures=[{"name": "dump_0001", "error": "conflict"}],
        )
    )
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["push"])

    assert result.exit_code != 0
    assert "--resolve" in result.output


def test_push_hard_guard_refuses_to_upload_lfs_pointer_stub(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # #32 defense-in-depth: even if a pointer stub somehow reaches the push candidate
    # list (classification bug, stale index, etc.), push must refuse to upload it rather
    # than clobber the real remote content. Force this by monkeypatching classify() to
    # report the pointer as LOCAL_ONLY, bypassing the normal LFS_POINTER short-circuit.
    (tmp_path / "big.bin").write_text(
        f"{POINTER_SIGNATURE}\noid sha256:{'0' * 64}\nsize 171008\n"
    )
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    monkeypatch.setattr(
        "protonfs.commands.push.classify",
        lambda local, index, remote=None: [DiffEntry("big.bin", SyncState.LOCAL_ONLY)],
    )

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 0
    assert result.failed_items == 1
    assert result.failures[0]["kind"] == LFS_POINTER_KIND
    assert fake.upload_calls == []
    assert ctx.index.get("big.bin") is None
