from __future__ import annotations

from pathlib import Path

from protonfs.commands.push import push
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import TransferResult


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
