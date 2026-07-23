from __future__ import annotations

from pathlib import Path

import pytest

from protonfs.commands.push import LFS_POINTER_KIND, ensure_remote_root, push
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.diff import DiffEntry, SyncState
from protonfs.drive import DriveError, TransferResult
from protonfs.lfs import POINTER_SIGNATURE


def test_ensure_remote_root_creates_each_missing_segment(tmp_path: Path, make_fake_drive) -> None:
    # #17: remote_root itself (not just dirs below it) is created, segment by segment.
    init_config(tmp_path, "/my-files/proj/sim")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    ensure_remote_root(ctx)

    assert ("/my-files", "proj") in fake.created_folders
    assert ("/my-files/proj", "sim") in fake.created_folders


def test_ensure_remote_root_rejects_path_outside_a_known_area(
    tmp_path: Path, make_fake_drive
) -> None:
    # A remote_root that is not under /my-files can never be created -> precise error (#17).
    init_config(tmp_path, "/myproject")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    with pytest.raises(DriveError, match="my-files"):
        ensure_remote_root(ctx)


def test_push_creates_remote_root_before_uploading(tmp_path: Path, make_fake_drive) -> None:
    (tmp_path / "dump").write_bytes(b"data")
    init_config(tmp_path, "/my-files/proj/sim")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    push(ctx, None, resolve=None, dry_run=False)

    assert ("/my-files", "proj") in fake.created_folders
    assert ("/my-files/proj", "sim") in fake.created_folders


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


# --- #93: progress reporting via the Reporter -------------------------------------------


def test_push_narrates_phases(tmp_path: Path, make_fake_drive, recording_reporter_cls) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    (tmp_path / "f1").write_bytes(b"data")
    ctx.drive = make_fake_drive()
    rep = recording_reporter_cls()

    push(ctx, None, None, dry_run=False, reporter=rep)

    kinds = [c[0] for c in rep.calls]
    assert "phase" in kinds and "done" in kinds


def test_push_uses_configured_batch_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive
) -> None:
    # The per-batch `filesystem upload` size comes from config (defaults.batch_size), so a
    # slow/throttled link can shrink it to keep each upload call under the transfer timeout.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.config.defaults.batch_size = 2
    for name in ("f1", "f2", "f3", "f4", "f5"):
        (tmp_path / name).write_bytes(b"data")
    ctx.drive = make_fake_drive()

    seen_sizes: list[int] = []
    import protonfs.commands.push as push_mod

    real_batches = push_mod.batches

    def spy_batches(items, size=200):
        seen_sizes.append(size)
        return real_batches(items, size)

    monkeypatch.setattr("protonfs.commands.push.batches", spy_batches)

    push(ctx, None, None, dry_run=False)

    assert seen_sizes and all(s == 2 for s in seen_sizes)
    # 5 files at size 2 -> upload called for 3 batches (2, 2, 1)
    assert len(ctx.drive.upload_calls) == 3


def test_push_reports_progress_per_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_drive, recording_reporter_cls
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    for name in ("f1", "f2", "f3"):
        (tmp_path / name).write_bytes(b"data")
    ctx.drive = make_fake_drive()
    # Single-file batches so the callback cadence (not just the final call) is asserted.
    monkeypatch.setattr(
        "protonfs.commands.push.batches", lambda items, size=1: [[i] for i in items]
    )

    rep = recording_reporter_cls()
    result = push(ctx, None, None, dry_run=False, reporter=rep)

    progress_calls = [c[1:] for c in rep.calls if c[0] == "progress"]
    assert result.transferred_items == 3
    # monotonic, ends with a forced final repeat at done == total
    assert progress_calls == [(1, 3), (2, 3), (3, 3), (3, 3)]


def test_push_narrates_no_item_for_a_failed_upload(
    tmp_path: Path, make_fake_drive, recording_reporter_cls
) -> None:
    # F5: a failed batch member must not get a "^" item line -- it never landed remotely.
    (tmp_path / "ok").write_bytes(b"data")
    (tmp_path / "broken").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=1,
            skipped_items=0,
            failed_items=1,
            failures=[{"name": "broken", "error": "boom"}],
        )
    )
    rep = recording_reporter_cls()

    push(ctx, None, None, dry_run=False, reporter=rep)

    item_paths = [c[1] for c in rep.calls if c[0] == "item"]
    assert "ok" in item_paths
    assert "broken" not in item_paths


# --- file pathspecs at the CLI layer (#push-file-pathspecs) ------------------------------


def _inject_ctx(monkeypatch, ctx) -> None:
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)


def test_push_cli_uploads_a_single_file_pathspec(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    (tmp_path / "run1" / "dump_0002").write_bytes(b"other")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    _inject_ctx(monkeypatch, ctx)

    result = CliRunner().invoke(main, ["push", "run1/dump_0001"])

    assert result.exit_code == 0
    uploaded = {name for call in fake.upload_calls for name in call[0]}
    assert any(u.endswith("run1/dump_0001") for u in uploaded)
    assert not any(u.endswith("run1/dump_0002") for u in uploaded)


def test_push_cli_uploads_mixed_file_and_dir_pathspecs(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # A single invocation naming one file and one directory must handle both branches
    # (is_file -> [base] vs rglob). Simulates e.g. `push a/one_00001 b/`.
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one_00001").write_bytes(b"x")
    (tmp_path / "a" / "one_00002").write_bytes(b"y")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "two_00001").write_bytes(b"z")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    _inject_ctx(monkeypatch, ctx)

    result = CliRunner().invoke(main, ["push", "a/one_00001", "b"])

    assert result.exit_code == 0
    uploaded = {name for call in fake.upload_calls for name in call[0]}
    assert any(u.endswith("a/one_00001") for u in uploaded)
    assert any(u.endswith("b/two_00001") for u in uploaded)
    # the sibling file NOT named, and not under the named dir, stays local
    assert not any(u.endswith("a/one_00002") for u in uploaded)


def test_push_cli_several_file_pathspecs_glob_expansion(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # The shell expands `dump_000{1,2,3}` to three argv before protonfs sees them.
    from click.testing import CliRunner

    from protonfs.cli import main

    for n in (1, 2, 3, 4):
        (tmp_path / f"dump_000{n}").write_bytes(b"d")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    _inject_ctx(monkeypatch, ctx)

    result = CliRunner().invoke(
        main, ["push", "dump_0001", "dump_0002", "dump_0003"]
    )

    assert result.exit_code == 0
    uploaded = {name for call in fake.upload_calls for name in call[0]}
    assert sum(u.endswith(f"dump_000{n}") for u in uploaded for n in (1, 2, 3)) == 3
    assert not any(u.endswith("dump_0004") for u in uploaded)


def test_push_cli_nonexistent_path_is_usage_error_no_drive_no_lock(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # A path that does not exist locally can only be a typo (the shell emits only
    # existing paths from a glob). Fail loudly with a usage error (exit 2), before any
    # Drive work and before the repo lock is taken.
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "real_0001").write_bytes(b"d")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    _inject_ctx(monkeypatch, ctx)

    lock_calls = []
    import contextlib

    @contextlib.contextmanager
    def spy_lock(root):
        lock_calls.append(root)
        yield

    # push imports repo_lock locally (from protonfs.locking), so patch it at the source.
    monkeypatch.setattr("protonfs.locking.repo_lock", spy_lock)

    result = CliRunner().invoke(main, ["push", "nope_9999"])

    assert result.exit_code == 2
    assert "nope_9999" in result.output
    assert fake.upload_calls == []
    assert lock_calls == []  # validation runs before the lock is acquired


def test_push_cli_nonexistent_paths_are_all_listed(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # Multiple bad paths are reported together, so the user fixes them in one round trip.
    from click.testing import CliRunner

    from protonfs.cli import main

    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    _inject_ctx(monkeypatch, ctx)

    result = CliRunner().invoke(main, ["push", "bad_a", "bad_b"])

    assert result.exit_code == 2
    assert "bad_a" in result.output
    assert "bad_b" in result.output


def test_push_cli_existing_but_ignored_file_is_nothing_to_push_not_error(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # The B/C seam: an ignored file EXISTS on disk (so it passes the existence check)
    # but scans to {} (ignore contract). That must be a clean exit 0 "nothing to push",
    # never the exit-2 missing-path error.
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "scratch.tmp").write_bytes(b"y")
    init_config(tmp_path, "/my-files/test")
    (tmp_path / ".protonfs" / "ignore").write_text("*.tmp\n")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    _inject_ctx(monkeypatch, ctx)

    result = CliRunner().invoke(main, ["push", "scratch.tmp"])

    assert result.exit_code == 0
    assert "nothing to push" in result.output
    assert fake.upload_calls == []


def test_push_cli_empty_directory_reports_nothing_to_push(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # A valid directory with no pushable candidates must say so at DEFAULT verbosity
    # (level 0) -- the whole point of the fix, since reporter.done() is silent at level 0.
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "empty").mkdir()
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    _inject_ctx(monkeypatch, ctx)

    result = CliRunner().invoke(main, ["push", "empty"])

    assert result.exit_code == 0
    assert "nothing to push" in result.output
    assert fake.upload_calls == []
