from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path

import pytest

from protonfs.commands.push import (
    CONFLICT_KIND,
    LFS_POINTER_KIND,
    UNVERIFIED_KIND,
    ensure_remote_root,
    push,
)
from protonfs.commands.status import compute_status
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.diff import DiffEntry, SyncState
from protonfs.drive import DriveError, RemoteEntry, RemoteIdentity, TransferResult
from protonfs.index import IndexStore
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
    # #185: a new directory goes up whole, into its parent
    assert fake.upload_calls[0][:2] == ((str(tmp_path / "run1"),), "/my-files/test")
    assert fake.folder_strategies == ["merge"]
    assert set(fake.revision_uids) == {"/my-files/test/run1/dump_0001"}
    assert ctx.index.get("run1/dump_0001") is not None
    assert ctx.index.get("run1/dump_0001").remote_path == "/my-files/test/run1/dump_0001"


def test_push_never_uploads_git_metadata(tmp_path: Path, make_fake_drive) -> None:
    # a store at a repository's root, holding a nested repo's gitlink file
    for rel, data in {
        ".git/HEAD": b"ref: refs/heads/main\n",
        ".git/objects/ab/cdef": b"blob",
        "run1/dump_0001": b"data",
        "sub/.git": b"gitdir: ../.git/modules/sub\n",
        "sub/table.csv": b"1,2",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 2
    assert set(fake.revision_uids) == {
        "/my-files/test/run1/dump_0001", "/my-files/test/sub/table.csv",
    }
    assert set(ctx.index.all()) == {"run1/dump_0001", "sub/table.csv"}


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
    # multi-group coverage (v0.1 review gap): files under different parents all land in
    # their own folders, and every successful file is indexed. #185: two new directories
    # under one parent share one upload call.
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
    assert [call[1] for call in fake.upload_calls] == ["/my-files/test"]
    assert set(fake.revision_uids) == {"/my-files/test/run1/a", "/my-files/test/run2/b"}
    assert ctx.index.get("run1/a") is not None
    assert ctx.index.get("run2/b") is not None


def test_push_carries_on_when_a_progress_save_fails(
    tmp_path: Path, make_fake_drive, first_index_save_fails
) -> None:
    # #170: the save after the first directory failed (a rename on a FUSE mount). push must
    # not abort there: the second directory is still pushed, and the final save persists
    # both, since the in-memory index never lost anything.
    for rel in ("run1/a", "run2/b"):
        (tmp_path / rel).parent.mkdir()
        (tmp_path / rel).write_bytes(b"x")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 2
    assert first_index_save_fails[0] is False
    on_disk = IndexStore(tmp_path)
    assert on_disk.get("run1/a") is not None and on_disk.get("run2/b") is not None


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


@pytest.mark.parametrize(
    "resolve,strategy",
    [("local", "replace"), ("remote", "skip"), ("both", "keep-both")],
)
def test_push_canonical_resolve_maps_to_proton_strategy(
    tmp_path: Path, make_fake_drive, resolve: str, strategy: str
) -> None:
    # #124: canonical remote|local|both map to proton-drive `-f` strategies.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    push(ctx, None, resolve=resolve, dry_run=False)

    assert fake.upload_calls[0][2] == strategy


def test_push_resolve_skip_leaves_skipped_files_unindexed(
    tmp_path: Path, make_fake_drive
) -> None:
    # #145: a skip is only an aggregate count, so the file is verified against the
    # remote rather than assumed. Here nothing landed there, so it stays unindexed --
    # the conservative outcome, now reached by checking instead of by refusing to look.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=0, skipped_items=1, failed_items=0, failures=[]
        ),
        dropped_files={"dump_0001"},  # skipped AND absent from the remote
    )

    result = push(ctx, None, resolve="skip", dry_run=False)

    assert result.skipped_items == 1
    assert ctx.index.get("dump_0001") is None  # not marked present on an ambiguous skip


def test_push_skip_adopts_a_byte_identical_remote_copy(
    tmp_path: Path, make_fake_drive
) -> None:
    # #145: proton-drive skips a file it considers already present, and reports only an
    # aggregate count. Indexing none of the batch left such a file local-only forever,
    # because the next push took the same branch. Verified strictly against the remote
    # and byte-identical, it is adopted: local and remote agree, so synced is the truth.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=0, skipped_items=1, failed_items=0, failures=[]
        )
    )

    push(ctx, None, resolve="skip", dry_run=False)

    entry = ctx.index.get("dump_0001")
    assert entry is not None  # converges instead of staying local-only
    assert entry.local_state == "present"


def test_push_skip_does_not_adopt_a_differing_remote_copy(
    tmp_path: Path, make_fake_drive
) -> None:
    # #145: adoption is strict. A skipped file whose remote copy differs in size is a
    # real divergence, so it must NOT be recorded as synced just because the name is
    # taken -- that is the mistake that let a stale remote copy look verified.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=0, skipped_items=1, failed_items=0, failures=[]
        ),
        remote_size_overrides={"dump_0001": 999},  # remote holds different content
    )

    push(ctx, None, resolve="skip", dry_run=False)

    assert ctx.index.get("dump_0001") is None


def test_push_skip_with_mixed_batch_indexes_nothing_in_that_batch(
    tmp_path: Path, make_fake_drive
) -> None:
    # #145: a single batch may report transferred AND skipped together (aggregate
    # counts). Not knowing which file was skipped means every one of them is verified
    # against the remote; neither landed there, so neither is indexed.
    (tmp_path / "a").write_bytes(b"aa")
    (tmp_path / "b").write_bytes(b"bb")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    # both files are one batch (same parent); report 1 transferred + 1 skipped
    ctx.drive = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=1, skipped_items=1, failed_items=0, failures=[]
        ),
        dropped_files={"a", "b"},  # neither actually reached the remote
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


def test_push_reuploads_a_file_appended_to_after_its_first_push(
    tmp_path: Path, make_fake_drive
) -> None:
    # #144: a long-running job appends to its output for the whole run. The file was already
    # pushed once, so it is in the index; the second push must notice the changed content and
    # send it again. Asserted on the remote's plaintext size, because that is the surface the
    # original divergence was visible in -- Drive held 99182 bytes of an 888442-byte series.
    first = b"line1\n"
    appended = b"line1\nline2\nline3\n"
    grow = tmp_path / "grow.txt"
    grow.write_bytes(first)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    push(ctx, None, resolve=None, dry_run=False)
    assert fake.remote_identities("/my-files/test")["grow.txt"].claimed_size == len(first)

    grow.write_bytes(appended)  # append, as the running job would
    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 1
    assert result.failed_items == 0
    assert fake.remote_identities("/my-files/test")["grow.txt"].claimed_size == len(appended)
    assert ctx.index.get("grow.txt").size == len(appended)
    # Sent as a new revision of the same node, so Drive's version history keeps the
    # earlier copy; not a replacement, which would trash the node and its history.
    assert fake.upload_calls[1][2] == "merge"
    assert fake.revisions["/my-files/test/grow.txt"] == 2
    assert fake.trashed == []


def test_push_does_not_stack_a_revision_on_a_remote_copy_that_changed_too(
    tmp_path: Path, make_fake_drive
) -> None:
    # Both sides moved since the last sync: another host pushed different content. That is
    # a real conflict and must surface as one, not be buried under our own revision.
    (tmp_path / "f.txt").write_bytes(b"ours v1")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    push(ctx, None, resolve=None, dry_run=False)

    fake._remote_files["/my-files/test"]["f.txt"] = 99  # their change
    fake._remote_sha1["/my-files/test"]["f.txt"] = "f" * 40
    (tmp_path / "f.txt").write_bytes(b"ours v2")
    result = push(ctx, None, resolve=None, dry_run=False)

    assert all(call[2] is None for call in fake.upload_calls)  # never merged
    assert fake.revisions["/my-files/test/f.txt"] == 1
    assert result.failures[0]["kind"] == CONFLICT_KIND
    assert ctx.index.get("f.txt").size == len(b"ours v1")


def test_push_does_not_stack_a_revision_on_a_same_size_remote_with_another_digest(
    tmp_path: Path, make_fake_drive
) -> None:
    # Size alone would call these the same copy; the sha1 says the remote was rewritten.
    (tmp_path / "f.txt").write_bytes(b"ours v1")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    push(ctx, None, resolve=None, dry_run=False)

    fake._remote_sha1["/my-files/test"]["f.txt"] = "f" * 40  # same size, other content
    (tmp_path / "f.txt").write_bytes(b"ours v2")
    result = push(ctx, None, resolve=None, dry_run=False)

    assert all(call[2] is None for call in fake.upload_calls)
    assert result.failures[0]["kind"] == CONFLICT_KIND


def test_push_recreates_a_changed_file_that_is_gone_from_the_remote(
    tmp_path: Path, make_fake_drive
) -> None:
    # Nothing of that name is left to add a revision to, so it is an ordinary upload.
    (tmp_path / "f.txt").write_bytes(b"v1")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    push(ctx, None, resolve=None, dry_run=False)

    del fake._remote_files["/my-files/test"]["f.txt"]
    (tmp_path / "f.txt").write_bytes(b"v2 longer")
    result = push(ctx, None, resolve=None, dry_run=False)

    assert fake.upload_calls[-1][2] is None
    assert result.transferred_items == 1
    assert ctx.index.get("f.txt").size == len(b"v2 longer")


def test_push_reports_a_skipped_file_whose_remote_digest_differs_as_under_delivered(
    tmp_path: Path, make_fake_drive
) -> None:
    from protonfs.commands.push import UNDERDELIVERED_KIND

    (tmp_path / "f.txt").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(
        upload_result=TransferResult(
            transferred_items=0, skipped_items=1, failed_items=0, failures=[]
        ),
        dropped_files={"f.txt"},
    )
    fake._remote_files["/my-files/test"] = {"f.txt": 4}
    fake._remote_sha1["/my-files/test"] = {"f.txt": "0" * 40}
    ctx.drive = fake

    result = push(ctx, None, resolve="skip", dry_run=False)

    assert result.failures[0]["kind"] == UNDERDELIVERED_KIND
    assert ctx.index.get("f.txt") is None


@pytest.mark.parametrize("resolve", ["skip", "both"])
def test_push_never_records_a_remote_copy_it_did_not_write_as_its_own(
    tmp_path: Path, make_fake_drive, resolve: str
) -> None:
    # #168: skip and keep-both leave the node at that name alone, so a copy there that
    # fails the check is not this push's upload. Recording it would let the next push
    # overwrite another device's file with a revision.
    (tmp_path / "f.txt").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    fake._remote_files["/my-files/test"] = {"f.txt": 9}
    fake._remote_sha1["/my-files/test"] = {"f.txt": "0" * 40}
    fake.revision_uids["/my-files/test/f.txt"] = "rev-theirs"
    ctx.drive = fake

    push(ctx, None, resolve=resolve, dry_run=False)

    assert ctx.index.underdelivered("f.txt") is None


def test_push_does_not_send_a_revision_for_a_path_this_device_never_held(
    tmp_path: Path, make_fake_drive
) -> None:
    # A metadata-only entry (seeded by refresh, never materialised here) says nothing
    # about which copy is newer, so a local file appearing at that path is not assumed
    # to be a revision of the remote one.
    from protonfs.index import IndexEntry

    (tmp_path / "f.txt").write_bytes(b"local")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    fake._remote_files["/my-files/test"] = {"f.txt": 6}
    ctx.index.set(
        "f.txt",
        IndexEntry(
            size=6, mtime=0.0, sha256="", sha1="", remote_path="/my-files/test/f.txt",
            origin_device="other", local_state="metadata-only", last_synced="",
        ),
    )
    ctx.drive = fake

    push(ctx, None, resolve=None, dry_run=False)

    assert all(call[2] is None for call in fake.upload_calls)


def test_push_never_records_synced_while_the_remote_holds_the_older_copy(
    tmp_path: Path, make_fake_drive
) -> None:
    # #144, the failure mode itself: the append is uploaded but the remote keeps the SHORTER
    # copy. Verification must catch that and leave the index alone, so the file keeps
    # reporting as locally-changed instead of `synced`. The bug was that claimedSize was read
    # from the wrong level of the listing entry (#147), came back None, and _verify_remote
    # passed on name presence -- recording the local hash as delivered while Drive disagreed.
    first = b"line1\n"
    appended = b"line1\nline2\nline3\n"
    grow = tmp_path / "grow.txt"
    grow.write_bytes(first)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    # The remote reports the pre-append size no matter what is uploaded.
    fake = make_fake_drive(remote_size_overrides={"grow.txt": len(first)})
    ctx.drive = fake

    push(ctx, None, resolve=None, dry_run=False)
    grow.write_bytes(appended)
    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.failed_items == 1
    assert result.transferred_items == 0
    # The index must still describe the copy Drive actually holds, not the local one.
    assert ctx.index.get("grow.txt").size == len(first)
    # Never `synced`. Without a remote view status cannot attribute a direction, so it
    # falls back to the conservative conflict-class state -- which is the point: the file
    # is flagged for attention rather than silently counted as safely on Drive.
    counts = compute_status(ctx, None)
    assert counts[SyncState.LOCALLY_INDEXED.value] == 0
    assert counts[SyncState.CONFLICT.value] == 1


# --- #144: a remote identity with no plaintext size is not verification ----------------


def test_push_uploads_but_does_not_index_a_file_the_listing_cannot_size(
    tmp_path: Path, make_fake_drive
) -> None:
    # The upload happens; only the claim of delivery is withheld. Before #144 a listing
    # without claimedSize passed on name presence and the file was indexed as delivered.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(report_claimed_size=False)
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert len(fake.upload_calls) == 1
    assert result.transferred_items == 0
    assert result.failed_items == 1
    assert result.failures[0]["kind"] == UNVERIFIED_KIND
    assert ctx.index.get("dump_0001") is None
    counts = compute_status(ctx, None)
    assert counts[SyncState.LOCALLY_INDEXED.value] == 0
    assert counts[SyncState.LOCAL_ONLY.value] == 1


def test_push_keeps_the_previous_entry_when_a_changed_file_cannot_be_verified(
    tmp_path: Path, make_fake_drive
) -> None:
    # The residual exposure on #144: the first push verifies, the listing then stops
    # reporting sizes, and the appended file is pushed again. The index must keep
    # describing the copy that WAS verified, so status never reports the append synced.
    first = b"line1\n"
    appended = b"line1\nline2\nline3\n"
    grow = tmp_path / "grow.txt"
    grow.write_bytes(first)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    push(ctx, None, resolve=None, dry_run=False)
    grow.write_bytes(appended)
    fake.report_claimed_size = False
    result = push(ctx, None, resolve=None, dry_run=False)

    assert len(fake.upload_calls) == 2
    assert fake.upload_calls[1][2] == "merge"  # the append was still sent, as a revision
    assert fake.revisions["/my-files/test/grow.txt"] == 2
    assert result.transferred_items == 0
    assert result.failures[0]["kind"] == UNVERIFIED_KIND
    assert ctx.index.get("grow.txt").size == len(first)
    counts = compute_status(ctx, None)
    assert counts[SyncState.LOCALLY_INDEXED.value] == 0


def test_push_retries_an_unverified_file_and_indexes_it_once_the_listing_sizes_it(
    tmp_path: Path, make_fake_drive
) -> None:
    # The retry finds the first upload already on Drive (a name conflict), and adopts it
    # once the listing can confirm it is this content.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(report_claimed_size=False)
    ctx.drive = fake

    push(ctx, None, resolve=None, dry_run=False)
    assert ctx.index.get("dump_0001") is None

    fake.report_claimed_size = True
    result = push(ctx, None, resolve=None, dry_run=False)

    assert len(fake.upload_calls) == 2  # retried, not left behind
    assert result.adopted_items == 1
    assert result.failed_items == 0
    assert ctx.index.get("dump_0001").size == 4


def test_push_does_not_adopt_a_name_conflict_the_listing_cannot_size(
    tmp_path: Path, make_fake_drive
) -> None:
    # Adoption records a remote copy WITHOUT uploading, so an unsized identity is even
    # less of a basis for it: nothing of ours was sent, and nothing about theirs is known.
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(
        upload_result=_conflict_upload_result("dump_0001"), report_claimed_size=False
    )
    fake._remote_files["/my-files/test"] = {"dump_0001": 999}
    ctx.drive = fake

    result = push(ctx, None, None, dry_run=False)

    assert result.adopted_items == 0
    assert result.failed_items == 1
    assert result.failures[0]["kind"] == UNVERIFIED_KIND
    assert ctx.index.get("dump_0001") is None


def test_push_cli_unverified_prints_retry_hint_not_resolve(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(report_claimed_size=False)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["push"])

    assert result.exit_code == 1
    assert "could not be verified" in result.output
    assert "retried on the next push" in result.output
    assert "--resolve" not in result.output


@pytest.mark.parametrize(
    "ident,strict,expected",
    [
        (None, False, "refuted"),
        (RemoteIdentity(claimed_size=4, sha1=None), False, "verified"),
        (RemoteIdentity(claimed_size=3, sha1=None), False, "refuted"),
        (RemoteIdentity(claimed_size=None, sha1=None), False, "unverifiable"),
        (RemoteIdentity(claimed_size=None, sha1=None), True, "unverifiable"),
        (RemoteIdentity(claimed_size=None, sha1="ab"), True, "refuted"),
        (RemoteIdentity(claimed_size=4, sha1="ab"), True, "refuted"),
        (RemoteIdentity(claimed_size=4, sha1="cd"), True, "verified"),
    ],
)
def test_verify_remote_verdicts(ident, strict: bool, expected: str) -> None:
    from protonfs.commands.push import _verify_remote
    from protonfs.localscan import ScanEntry

    entry = ScanEntry(rel_path="f", size=4, mtime=0.0, sha256="x", sha1="cd")

    assert _verify_remote(ident, entry, strict_sha1=strict).value == expected


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
    # D2.1: a default push that hits a real conflict (another file already holds the
    # name on Drive) instructs the user to re-run with --resolve, and exits non-zero.
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    fake._remote_files["/my-files/test"] = {"dump_0001": 99}
    fake._remote_sha1["/my-files/test"] = {"dump_0001": "f" * 40}
    ctx.drive = fake
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

    # scan() also narrates progress now, so isolate the UPLOAD cadence: progress calls
    # after the "uploading" phase marker.
    up = rep.calls.index(("phase", "uploading"))
    progress_calls = [c[1:] for c in rep.calls[up:] if c[0] == "progress"]
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


# --- self-heal: adopt files already on the remote (upload name-conflict) -----------------


def _conflict_upload_result(name: str) -> TransferResult:
    """An upload result where `name` failed because it already exists on the remote."""
    return TransferResult(
        transferred_items=0,
        skipped_items=0,
        failed_items=1,
        failures=[{
            "name": name,
            "error": f'ValidationError: Name conflict on "{name}" (file) already exists',
        }],
    )


def test_push_adopts_already_existing_file_that_matches_remote(
    tmp_path: Path, make_fake_drive
) -> None:
    # Self-heal: a file already on Drive but absent from the index (an earlier push
    # uploaded it but never saved the index) fails upload with a name-conflict. push must
    # verify the remote copy matches and ADOPT it into the index rather than fail forever.
    (tmp_path / "dump_0001").write_bytes(b"data")  # size 4
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(upload_result=_conflict_upload_result("dump_0001"))
    # The file already exists on the remote at the matching plaintext size.
    fake._remote_files["/my-files/test"] = {"dump_0001": 4}
    ctx.drive = fake

    result = push(ctx, None, None, dry_run=False)

    assert result.adopted_items == 1
    assert result.failed_items == 0
    assert result.failures == []
    entry = ctx.index.get("dump_0001")
    assert entry is not None and entry.local_state == "present"


def test_push_does_not_adopt_conflict_when_remote_size_differs(
    tmp_path: Path, make_fake_drive
) -> None:
    # A name-conflict where the remote copy does NOT match the local file is a real
    # conflict: never adopt (that would falsely mark diverged content as synced).
    (tmp_path / "dump_0001").write_bytes(b"data")  # size 4
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(upload_result=_conflict_upload_result("dump_0001"))
    fake._remote_files["/my-files/test"] = {"dump_0001": 999}  # different size
    ctx.drive = fake

    result = push(ctx, None, None, dry_run=False)

    assert result.adopted_items == 0
    assert result.failed_items == 1
    assert result.failures and result.failures[0]["kind"] == CONFLICT_KIND
    assert ctx.index.get("dump_0001") is None


def test_push_cli_reports_adopted_count(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    # The CLI summary surfaces adopted files (only when non-zero) so the user sees that a
    # re-push of already-uploaded content healed the index instead of failing.
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(upload_result=_conflict_upload_result("dump_0001"))
    fake._remote_files["/my-files/test"] = {"dump_0001": 4}
    ctx.drive = fake
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["push"])

    assert result.exit_code == 0
    assert "adopted=1" in result.output
    assert "failed=0" in result.output


def _remote_holds_prefix(tmp_path: Path, fake, rel: str, prefix: bytes, full: bytes) -> None:
    """Put `prefix` on the fake remote as `rel`'s copy, then grow the local file to `full`:
    what a push of a file still being written leaves behind (#168)."""
    local = tmp_path / rel
    local.write_bytes(prefix)
    fake.upload([local], "/my-files/test")
    local.write_bytes(full)


def test_push_replaces_its_own_older_copy_with_a_revision(
    tmp_path: Path, make_fake_drive
) -> None:
    # #168: a time series was pushed while still being written, so Drive holds its first
    # bytes and the file is not indexed. The next push meets "already exists", and the
    # remote copy fails the adoption check because it is shorter. It is a byte-prefix of
    # the local file, though: an older copy of this same file, not a foreign one. So it is
    # replaced with a revision instead of being reported as a conflict on every push.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    _remote_holds_prefix(tmp_path, fake, "run.ev", b"line1\n", b"line1\nline2\nline3\n")

    result = push(ctx, None, None, dry_run=False)

    assert result.failed_items == 0, result.failures
    assert result.transferred_items == 1
    assert fake.upload_calls[-1][2] == "merge"
    assert fake.revisions["/my-files/test/run.ev"] == 2
    assert fake.remote_identities("/my-files/test")["run.ev"].claimed_size == 18
    assert ctx.index.get("run.ev").size == 18


@pytest.mark.parametrize(
    ("remote", "local"),
    [
        (b"other\n", b"line1\nline2\n"),  # shorter, but not a prefix: a different file
        (b"line1\nline2\nline3\n", b"line1\n"),  # longer than the local file
    ],
)
def test_push_still_reports_a_remote_copy_that_is_not_an_older_prefix_as_a_conflict(
    tmp_path: Path, make_fake_drive, remote: bytes, local: bytes
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    _remote_holds_prefix(tmp_path, fake, "run.ev", remote, local)

    result = push(ctx, None, None, dry_run=False)

    assert [f["kind"] for f in result.failures] == [CONFLICT_KIND]
    assert fake.revisions["/my-files/test/run.ev"] == 1
    assert ctx.index.get("run.ev") is None


def _underdeliver_first_push(tmp_path: Path, make_fake_drive):
    """First push of `grow.bin`: the listing then claims 3 bytes, with no sha1, as when
    a file grew mid-upload or the transfer was cut short (#168). Returns (ctx, fake)."""
    (tmp_path / "grow.bin").write_bytes(b"0123456789")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(remote_size_overrides={"grow.bin": 3})
    ctx.drive = fake
    first = push(ctx, None, None, dry_run=False)
    assert [f["kind"] for f in first.failures] == ["under-delivered"]
    fake._remote_size_overrides.clear()  # the next upload lands in full
    return ctx, fake


def test_push_replaces_its_own_underdelivered_upload_with_a_revision(
    tmp_path: Path, make_fake_drive
) -> None:
    # #168, the issue's regression test: the first push under-delivers, so the file is
    # left unindexed and Drive keeps the short copy. The listing gives no sha1, so the
    # byte-prefix check cannot claim that copy. But the first push recorded the remote
    # revision it uploaded, and the node still holds it, so the copy is provably this
    # push's own. The second push replaces it with a revision and indexes the file.
    ctx, fake = _underdeliver_first_push(tmp_path, make_fake_drive)
    assert ctx.index.underdelivered("grow.bin") is not None
    assert IndexStore(tmp_path).underdelivered("grow.bin") is not None  # persisted

    second = push(ctx, None, None, dry_run=False)

    assert second.failed_items == 0, second.failures
    assert second.transferred_items == 1
    assert fake.upload_calls[-1][2] == "merge"
    assert fake.revisions["/my-files/test/grow.bin"] == 2
    assert ctx.index.get("grow.bin").size == 10
    assert ctx.index.underdelivered("grow.bin") is None


def test_push_does_not_replace_an_underdelivered_upload_someone_else_has_revised(
    tmp_path: Path, make_fake_drive
) -> None:
    # The record proves ownership only while the node still holds that revision. Once
    # another device has uploaded over it, the remote copy is theirs: a real conflict.
    ctx, fake = _underdeliver_first_push(tmp_path, make_fake_drive)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "grow.bin").write_bytes(b"theirs")
    fake.upload([other / "grow.bin"], "/my-files/test", file_strategy="merge")

    second = push(ctx, None, None, dry_run=False)

    assert [f["kind"] for f in second.failures] == [CONFLICT_KIND]
    assert fake.revisions["/my-files/test/grow.bin"] == 2
    assert ctx.index.get("grow.bin") is None


def test_push_does_not_adopt_conflict_absent_from_remote(
    tmp_path: Path, make_fake_drive
) -> None:
    # A name-conflict for a file the remote listing does not return is not adoptable;
    # keep it a failure (defensive: never index something we could not confirm present).
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(upload_result=_conflict_upload_result("dump_0001"))
    # remote has nothing under the parent
    ctx.drive = fake

    result = push(ctx, None, None, dry_run=False)

    assert result.adopted_items == 0
    assert result.failed_items == 1
    assert ctx.index.get("dump_0001") is None


def test_push_reports_a_phantom_node_distinctly_from_a_real_conflict(
    tmp_path: Path, make_fake_drive
) -> None:
    """#138: upload rejected as "already exists" while the remote listing does NOT contain
    the file describes a phantom node holding the name, not a different file occupying it.
    Saying "a different file already exists" is wrong and invites --resolve=remote, which
    keeps the phantom and loses the data."""
    from protonfs.commands.push import PHANTOM_KIND

    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    # conflict on upload, and nothing of that name in the remote listing
    ctx.drive = make_fake_drive(upload_result=_conflict_upload_result("dump_0001"))

    result = push(ctx, None, None, dry_run=False)

    assert result.failed_items == 1
    failure = result.failures[0]
    assert failure["kind"] == PHANTOM_KIND
    assert failure["kind"] != CONFLICT_KIND
    # the message must name the remedy, and must not claim a different file is there
    assert "different file" not in failure["error"]
    assert "--resolve=local" in failure["error"]


def test_push_still_reports_a_real_conflict_as_a_conflict(
    tmp_path: Path, make_fake_drive
) -> None:
    """#138 guard: when the remote genuinely holds a DIFFERENT file of that name, the
    existing conflict wording stays -- only the absent case changes."""
    (tmp_path / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(upload_result=_conflict_upload_result("dump_0001"))
    # Seed the remote listing directly: the configured upload REPORTS a conflict, so
    # calling it would not record anything. What matters here is that the file IS
    # listable, at a size that does not match the local one.
    ctx.drive._remote_files["/my-files/test"] = {"dump_0001": 999}

    result = push(ctx, None, None, dry_run=False)

    assert result.failed_items == 1
    assert result.failures[0]["kind"] == CONFLICT_KIND
    assert "different file" in result.failures[0]["error"]


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
    # the sibling file NOT named, and not under the named dir, stays local
    assert set(fake.revision_uids) == {"/my-files/test/a/one_00001", "/my-files/test/b/two_00001"}


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


def test_push_only_pushes_just_the_named_files(tmp_path: Path, make_fake_drive) -> None:
    # #171: `only` restricts one push pass to these files, wherever they live.
    for rel in ("run1/a", "run1/b", "run1/c", "run2/d"):
        (tmp_path / rel).parent.mkdir(exist_ok=True)
        (tmp_path / rel).write_bytes(rel.encode())
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    result = push(ctx, None, None, dry_run=False, only={"run1/a", "run1/b", "run2/d"})

    assert result.transferred_items == 3
    assert set(ctx.index.all()) == {"run1/a", "run1/b", "run2/d"}
    # run2 holds only a named file, so it goes up whole; run1 also holds unnamed run1/c
    assert sorted(call[1] for call in fake.upload_calls) == [
        "/my-files/test", "/my-files/test/run1",
    ]


def test_push_cli_file_paths_share_one_pass(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    # #171: N file paths in one directory cost one pass -- one upload batch, one listing
    # of the directory and a bounded number of index saves -- not N of each.
    from click.testing import CliRunner

    from protonfs.cli import main

    (tmp_path / "run1").mkdir()
    for n in range(1, 5):
        (tmp_path / "run1" / f"dump_000{n}").write_bytes(b"d" * n)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    _inject_ctx(monkeypatch, ctx)
    saves = []
    real_save = IndexStore.save
    monkeypatch.setattr(IndexStore, "save", lambda self: saves.append(1) or real_save(self))

    result = CliRunner().invoke(
        main, ["push", "run1/dump_0001", "run1/dump_0002", "run1/dump_0003"]
    )

    assert result.exit_code == 0, result.output
    assert "transferred=3" in result.output
    assert len(fake.upload_calls) == 1
    assert fake.identity_calls == ["/my-files/test/run1"]
    assert len(saves) <= 2
    assert set(ctx.index.all()) == {f"run1/dump_000{n}" for n in (1, 2, 3)}


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


# --- settle window (#168) --------------------------------------------------------------


def _aged(path: Path, seconds_ago: float, now: float) -> None:
    os.utime(path, (now - seconds_ago, now - seconds_ago))


def test_push_min_age_holds_back_a_file_modified_within_the_window(
    tmp_path: Path, make_fake_drive
) -> None:
    now = time.time()
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "run101.ev").write_bytes(b"still growing")
    (tmp_path / "run1" / "run1_00001").write_bytes(b"finished dump")
    _aged(tmp_path / "run1" / "run1_00001", 7200, now)
    _aged(tmp_path / "run1" / "run101.ev", 60, now)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False, min_age=3600, now=now)

    assert (result.transferred_items, result.unsettled_items) == (1, 1)
    assert [Path(p).name for call in fake.upload_calls for p in call[0]] == ["run1_00001"]
    assert ctx.index.get("run1/run1_00001") is not None
    assert ctx.index.get("run1/run101.ev") is None


def test_push_uploads_a_held_file_once_it_has_settled(tmp_path: Path, make_fake_drive) -> None:
    now = time.time()
    (tmp_path / "run101.ev").write_bytes(b"final")
    _aged(tmp_path / "run101.ev", 60, now)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    first = push(ctx, None, resolve=None, dry_run=False, min_age=3600, now=now)
    second = push(ctx, None, resolve=None, dry_run=False, min_age=3600, now=now + 3600)

    assert (first.transferred_items, first.unsettled_items) == (0, 1)
    assert (second.transferred_items, second.unsettled_items) == (1, 0)
    assert ctx.index.get("run101.ev") is not None


def test_push_dry_run_counts_held_files_apart_from_the_upload_count(
    tmp_path: Path, make_fake_drive
) -> None:
    now = time.time()
    (tmp_path / "old_00001").write_bytes(b"a")
    (tmp_path / "new.ev").write_bytes(b"b")
    _aged(tmp_path / "old_00001", 7200, now)
    _aged(tmp_path / "new.ev", 10, now)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=True, min_age=3600, now=now)

    assert (result.transferred_items, result.unsettled_items) == (1, 1)
    assert fake.upload_calls == []


def test_push_without_min_age_uploads_a_file_modified_just_now(
    tmp_path: Path, make_fake_drive
) -> None:
    (tmp_path / "run101.ev").write_bytes(b"just written")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    result = push(ctx, None, resolve=None, dry_run=False)

    assert (result.transferred_items, result.unsettled_items) == (1, 0)


# --- #183: images this host cannot thumbnail ------------------------------------------


def test_push_retries_an_unthumbnailable_file_without_a_thumbnail(
    tmp_path: Path, make_fake_drive
) -> None:
    (tmp_path / "scans").mkdir()
    (tmp_path / "scans" / "IMG_1.tif").write_bytes(b"tiff bytes")
    (tmp_path / "scans" / "IMG_2.jpg").write_bytes(b"jpeg bytes")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(no_thumbnail_codec=(".tif",))
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.failed_items == 0 and result.failures == []
    assert result.transferred_items == 2
    assert fake.thumbnail_skips == [False, True]
    assert fake.upload_calls[1][0] == (str(tmp_path / "scans" / "IMG_1.tif"),)
    assert ctx.index.get("scans/IMG_1.tif") is not None


def test_push_skips_thumbnails_everywhere_when_configured(
    tmp_path: Path, make_fake_drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROTONFS_SKIP_THUMBNAILS", "1")
    (tmp_path / "IMG_1.tif").write_bytes(b"tiff bytes")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(no_thumbnail_codec=(".tif",))
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 1
    assert fake.thumbnail_skips == [True]  # one call, no retry needed


def test_push_reports_a_thumbnail_failure_that_persists_without_thumbnails(
    tmp_path: Path, make_fake_drive
) -> None:
    # A failure from an upload already sent with -t is a real failure, never retried again.
    (tmp_path / "IMG_1.tif").write_bytes(b"tiff bytes")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(
        upload_result=TransferResult(
            0, 0, 1, [{"name": "IMG_1.tif", "error": "Failed to generate thumbnails"}]
        )
    )
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert fake.thumbnail_skips == [False, True]
    assert result.failed_items == 1 and result.transferred_items == 0
    assert ctx.index.get("IMG_1.tif") is None


def test_skip_thumbnails_config_key_and_env_override(tmp_path: Path, monkeypatch) -> None:
    from protonfs.commands.config import config_get, config_set

    init_config(tmp_path, "/my-files/test")
    assert load_context(tmp_path).config.defaults.skip_thumbnails is False
    config_set(tmp_path, "defaults.skip_thumbnails", "true")
    assert load_context(tmp_path).config.defaults.skip_thumbnails is True
    assert config_get(tmp_path, "defaults.skip_thumbnails") == "True"
    monkeypatch.setenv("PROTONFS_SKIP_THUMBNAILS", "0")
    assert load_context(tmp_path).config.defaults.skip_thumbnails is False


# --- #185: whole-directory uploads and one create-folder per folder --------------------


def _photo_tree(root: Path, days: int = 3, per_day: int = 2) -> list[str]:
    rels = []
    for day in range(1, days + 1):
        for n in range(per_day):
            rel = f"photos/2019/2019-05-0{day}/IMG_{day}{n}.jpg"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(f"{day}-{n}".encode())
            rels.append(rel)
    return rels


def test_push_uploads_new_date_folders_in_shared_calls(
    tmp_path: Path, make_fake_drive
) -> None:
    rels = _photo_tree(tmp_path)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.config.defaults.batch_size = 4  # photos/ and photos/2019 hold 6 files: too many
    fake = make_fake_drive()
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 6 and result.failed_items == 0
    day = str(tmp_path / "photos" / "2019" / "2019-05-0{}")
    assert [call[:2] for call in fake.upload_calls] == [
        ((day.format(1), day.format(2)), "/my-files/test/photos/2019"),
        ((day.format(3),), "/my-files/test/photos/2019"),
    ]
    assert fake.folder_strategies == ["merge", "merge"]
    # the parent chain is created once, and the date folders not at all (proton-drive does)
    assert fake.created_folders == [
        ("/my-files", "test"), ("/my-files/test", "photos"), ("/my-files/test/photos", "2019"),
    ]
    assert set(ctx.index.all()) == set(rels)
    assert all(ctx.index.get(r).remote_path == f"/my-files/test/{r}" for r in rels)


def test_push_creates_each_remote_folder_once(tmp_path: Path, make_fake_drive) -> None:
    from protonfs.commands.push import _ensure_remote_dir

    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    known = {"/my-files/test"}

    _ensure_remote_dir(ctx, "/my-files/test/photos/2019/a", known)
    _ensure_remote_dir(ctx, "/my-files/test/photos/2019/b", known)

    assert fake.created_folders == [
        ("/my-files/test", "photos"),
        ("/my-files/test/photos", "2019"),
        ("/my-files/test/photos/2019", "a"),
        ("/my-files/test/photos/2019", "b"),
    ]


@pytest.mark.parametrize("extra", ["ignored", "indexed", "symlink", "empty-dir"])
def test_push_never_uploads_a_directory_whole_when_it_holds_something_else(
    tmp_path: Path, make_fake_drive, extra: str
) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "new").write_bytes(b"new")
    init_config(tmp_path, "/my-files/test")
    if extra == "ignored":
        (tmp_path / ".protonfs" / "ignore").write_text("*.tmp\n")
        (tmp_path / "run" / "scratch.tmp").write_bytes(b"x")
    elif extra == "indexed":
        (tmp_path / "run" / "old").write_bytes(b"old")
    elif extra == "symlink":
        (tmp_path / "run" / "link").symlink_to(tmp_path / "run" / "new")
    else:
        (tmp_path / "run" / "sub").mkdir()
    ctx = load_context(tmp_path)
    fake = make_fake_drive()
    ctx.drive = fake
    if extra == "indexed":
        push(ctx, "run/old", resolve=None, dry_run=False)
        fake.upload_calls.clear()

    push(ctx, "run/new", resolve=None, dry_run=False)

    assert fake.upload_calls[0][:2] == ((str(tmp_path / "run" / "new"),), "/my-files/test/run")
    assert "/my-files/test/run/scratch.tmp" not in fake.revision_uids


def test_push_never_sends_two_files_of_one_name_in_one_call(
    tmp_path: Path, make_fake_drive
) -> None:
    # proton-drive names a failed file by basename only, so one call must never hold two
    # files a failure could both name: photos/ (holding both IMG_0.jpg) is not uploaded
    # whole, and its date folders go in separate calls.
    for day in (1, 2):
        for n in (0, 1):
            rel = tmp_path / f"photos/2019-05-0{day}/IMG_{n}.jpg"
            rel.parent.mkdir(parents=True, exist_ok=True)
            rel.write_bytes(f"{day}{n}".encode())
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(
        upload_result=TransferResult(1, 0, 1, [{"name": "IMG_0.jpg", "error": "boom"}])
    )
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert [call[0] for call in fake.upload_calls] == [
        (str(tmp_path / "photos" / "2019-05-01"),), (str(tmp_path / "photos" / "2019-05-02"),),
    ]
    assert result.failed_items == 2 and result.transferred_items == 2
    assert set(ctx.index.all()) == {"photos/2019-05-01/IMG_1.jpg", "photos/2019-05-02/IMG_1.jpg"}


def test_push_rechecks_a_directory_just_before_uploading_it_whole(
    tmp_path: Path, make_fake_drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file written into the directory after the scan must not ride along unchosen.
    _photo_tree(tmp_path, days=2, per_day=1)
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.config.defaults.batch_size = 1  # one call per date folder
    fake = make_fake_drive()
    ctx.drive = fake
    late = tmp_path / "photos/2019/2019-05-02/late.jpg"
    real_upload = fake.upload

    def upload_then_write(*args, **kwargs):
        late.write_bytes(b"still being written")
        return real_upload(*args, **kwargs)

    fake.upload = upload_then_write

    push(ctx, None, resolve=None, dry_run=False)

    assert "/my-files/test/photos/2019/2019-05-02/late.jpg" not in fake.revision_uids
    late_call = (str(tmp_path / "photos/2019/2019-05-02/IMG_20.jpg"),)
    assert fake.upload_calls[1][:2] == (late_call, "/my-files/test/photos/2019/2019-05-02")
    assert "/my-files/test/photos/2019/2019-05-02/IMG_20.jpg" in fake.revision_uids
    assert ctx.index.get("photos/2019/2019-05-02/IMG_20.jpg") is not None


def test_ensure_remote_dir_retries_a_folder_whose_creation_failed(
    tmp_path: Path, make_fake_drive
) -> None:
    from protonfs.commands.push import _ensure_remote_dir

    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    attempts: list[str] = []

    def flaky(parent, name):
        attempts.append(name)
        if len(attempts) == 1:
            raise DriveError("503 service unavailable")
        if len(attempts) == 3:
            raise DriveError("A file or folder with that name already exists")
        return {}

    ctx.drive = make_fake_drive()
    ctx.drive.create_folder = flaky
    known: set[str] = set()

    _ensure_remote_dir(ctx, "/my-files/test/a", known)  # fails: not remembered
    _ensure_remote_dir(ctx, "/my-files/test/a", known)  # created
    _ensure_remote_dir(ctx, "/my-files/test/b", known)  # already there: remembered
    _ensure_remote_dir(ctx, "/my-files/test/b", known)

    assert attempts == ["a", "a", "b"]
    assert known == {"/my-files/test/a", "/my-files/test/b"}


def test_push_retries_an_unthumbnailable_file_from_a_whole_directory(
    tmp_path: Path, make_fake_drive
) -> None:
    (tmp_path / "scans" / "2019").mkdir(parents=True)
    (tmp_path / "scans" / "2019" / "a.tif").write_bytes(b"tiff")
    (tmp_path / "scans" / "2019" / "b.jpg").write_bytes(b"jpeg")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(no_thumbnail_codec=(".tif",))
    ctx.drive = fake

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 2 and result.failed_items == 0
    assert fake.thumbnail_skips == [False, True]
    assert fake.upload_calls[1][:2] == (
        (str(tmp_path / "scans" / "2019" / "a.tif"),), "/my-files/test/scans/2019"
    )
    # the directory upload made scans/ and scans/2019, so the retry creates nothing
    assert fake.created_folders == [("/my-files", "test")]
    assert set(ctx.index.all()) == {"scans/2019/a.tif", "scans/2019/b.jpg"}


# --- #197: a local move or rename becomes a remote move or rename ---------------------

ROOT = "/my-files/test"


def _pushed_tree(tmp_path: Path, make_fake_drive, files: dict[str, bytes], **fake_kwargs):
    """A repo whose ``files`` were pushed once, so the index and the fake Drive hold them."""
    init_config(tmp_path, ROOT)
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(**fake_kwargs)
    for rel, data in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)
    push(ctx, None, resolve=None, dry_run=False)
    ctx.drive.upload_calls.clear()
    return ctx


def _move(tmp_path: Path, old: str, new: str) -> None:
    (tmp_path / new).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / old).rename(tmp_path / new)


def _uploaded(fake) -> list[str]:
    """Every file sent by an upload call, a whole directory (#185) expanded to its files."""
    files = []
    for call in fake.upload_calls:
        for sent in map(Path, call[0]):
            files += sorted(map(str, sent.rglob("*"))) if sent.is_dir() else [str(sent)]
    return files


def _on_drive(fake, remote_path: str) -> bool:
    parent, name = remote_path.rsplit("/", 1)
    return name in fake.remote_identities(parent)


def test_push_applies_a_local_move_as_one_remote_move_without_uploading(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo", "a/y.jpg": b"other"})
    before = ctx.index.get("a/x.jpg")
    revision = ctx.drive.revision_uids[f"{ROOT}/a/x.jpg"]
    _move(tmp_path, "a/x.jpg", "b/x.jpg")

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.move_calls == [((f"{ROOT}/a/x.jpg",), f"{ROOT}/b")]
    assert ctx.drive.rename_calls == []
    assert _uploaded(ctx.drive) == []
    assert result.moved_items == 1
    assert result.transferred_items == 0
    assert ctx.index.get("a/x.jpg") is None
    after = ctx.index.get("b/x.jpg")
    assert (after.sha256, after.sha1, after.size) == (before.sha256, before.sha1, before.size)
    assert after.remote_path == f"{ROOT}/b/x.jpg"
    assert after.local_state == "present"
    # the same Drive node, so its revision history and share links survive
    assert ctx.drive.revision_uids[f"{ROOT}/b/x.jpg"] == revision
    assert compute_status(ctx, None)[SyncState.LOCALLY_INDEXED.value] == 2


def test_push_applies_a_rename_in_place_as_a_remote_rename(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"})
    _move(tmp_path, "a/x.jpg", "a/renamed.jpg")

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.rename_calls == [(f"{ROOT}/a/x.jpg", "renamed.jpg")]
    assert ctx.drive.move_calls == []
    assert _uploaded(ctx.drive) == []
    assert result.moved_items == 1
    assert ctx.index.get("a/renamed.jpg").remote_path == f"{ROOT}/a/renamed.jpg"
    assert ctx.index.get("a/x.jpg") is None


def test_push_moves_then_renames_when_folder_and_name_both_change(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"})
    _move(tmp_path, "a/x.jpg", "b/c/y.jpg")

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.move_calls == [((f"{ROOT}/a/x.jpg",), f"{ROOT}/b/c")]
    assert ctx.drive.rename_calls == [(f"{ROOT}/b/c/x.jpg", "y.jpg")]
    assert (f"{ROOT}/b", "c") in ctx.drive.created_folders  # target made first
    assert _uploaded(ctx.drive) == []
    assert result.moved_items == 1
    assert _on_drive(ctx.drive, f"{ROOT}/b/c/y.jpg")
    assert not _on_drive(ctx.drive, f"{ROOT}/a/x.jpg")


def test_push_pairs_identical_files_by_basename_and_leaves_the_rest_deleted(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"same", "c/z.jpg": b"same"})
    _move(tmp_path, "a/x.jpg", "b/x.jpg")
    (tmp_path / "c" / "z.jpg").unlink()

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.move_calls == [((f"{ROOT}/a/x.jpg",), f"{ROOT}/b")]
    assert result.moved_items == 1
    # the unpaired deletion is left to the deletion policy: still indexed, still on Drive
    assert ctx.index.get("c/z.jpg") is not None
    assert _on_drive(ctx.drive, f"{ROOT}/c/z.jpg")


def test_push_uploads_a_file_edited_then_moved_and_keeps_the_old_path_indexed(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"before"})
    (tmp_path / "a" / "x.jpg").write_bytes(b"after the edit")
    _move(tmp_path, "a/x.jpg", "b/x.jpg")

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.move_calls == []
    assert _uploaded(ctx.drive) == [str(tmp_path / "b" / "x.jpg")]
    assert result.moved_items == 0
    assert result.transferred_items == 1
    assert ctx.index.get("a/x.jpg") is not None


def test_push_falls_back_to_uploading_when_the_move_fails(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"}, fail_moves=True)
    _move(tmp_path, "a/x.jpg", "b/x.jpg")

    result = push(ctx, None, resolve=None, dry_run=False)

    assert len(ctx.drive.move_calls) == 1
    assert _uploaded(ctx.drive) == [str(tmp_path / "b" / "x.jpg")]
    assert result.moved_items == 0
    assert result.transferred_items == 1
    assert result.failed_items == 0
    assert ctx.index.get("a/x.jpg").remote_path == f"{ROOT}/a/x.jpg"
    assert ctx.index.get("b/x.jpg") is not None


def test_push_does_not_trust_a_move_the_listing_does_not_show(
    tmp_path: Path, make_fake_drive
) -> None:
    # the counting rule: a move reported as done is checked against the listing
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"}, ignored_moves=True)
    _move(tmp_path, "a/x.jpg", "b/x.jpg")

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.moved_items == 0
    assert _uploaded(ctx.drive) == [str(tmp_path / "b" / "x.jpg")]
    assert ctx.index.get("a/x.jpg").remote_path == f"{ROOT}/a/x.jpg"


def test_push_records_where_the_node_is_when_neither_the_rename_nor_the_move_back_works(
    tmp_path: Path, make_fake_drive
) -> None:
    from protonfs.drive import DriveError

    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"}, fail_renames=True)
    _move(tmp_path, "a/x.jpg", "b/y.jpg")
    real_move = ctx.drive.move

    def move_once(paths, target):              # the move lands; moving it back fails
        if ctx.drive.move_calls:
            ctx.drive.move_calls.append((tuple(paths), target))
            raise DriveError("simulated move-back failure")
        return real_move(paths, target)

    ctx.drive.move = move_once

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.moved_items == 0
    assert _uploaded(ctx.drive) == [str(tmp_path / "b" / "y.jpg")]
    # the old path's index entry follows its node, so a later trash or restore finds it
    assert ctx.index.get("a/x.jpg").remote_path == f"{ROOT}/b/x.jpg"
    assert _on_drive(ctx.drive, f"{ROOT}/b/x.jpg")


def test_push_skips_the_move_when_the_target_name_is_taken_on_drive(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo", "b/x.jpg": b"photo"})
    # another host's copy holds b/x.jpg on Drive; this host forgets it locally
    ctx.index.remove("b/x.jpg")
    _move(tmp_path, "a/x.jpg", "c/x.jpg")
    (tmp_path / "b" / "x.jpg").unlink()
    (tmp_path / "c" / "x.jpg").rename(tmp_path / "b" / "x.jpg")

    push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.move_calls == []
    assert ctx.index.get("a/x.jpg") is not None  # untouched; the upload path adopts b/x.jpg


def test_push_dry_run_counts_moves_and_changes_nothing(tmp_path: Path, make_fake_drive) -> None:
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"})
    _move(tmp_path, "a/x.jpg", "b/x.jpg")

    result = push(ctx, None, resolve=None, dry_run=True)

    assert result.moved_items == 1
    assert result.transferred_items == 0
    assert ctx.drive.move_calls == []
    assert ctx.index.get("a/x.jpg") is not None


def test_push_scoped_to_a_subpath_never_pairs_a_deletion_outside_it(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"})
    _move(tmp_path, "a/x.jpg", "b/x.jpg")

    result = push(ctx, "b", resolve=None, dry_run=False)

    assert ctx.drive.move_calls == []
    assert result.transferred_items == 1
    assert ctx.index.get("a/x.jpg") is not None


def test_push_of_named_files_never_pairs_with_paths_it_did_not_scan(
    tmp_path: Path, make_fake_drive
) -> None:
    # with `only`, every other indexed file is unscanned, not deleted
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"})
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "copy.jpg").write_bytes(b"photo")

    push(ctx, None, resolve=None, dry_run=False, only={"b/copy.jpg"})

    assert ctx.drive.move_calls == ctx.drive.rename_calls == []
    assert ctx.index.get("a/x.jpg") is not None


def test_push_updates_the_remote_manifest_for_a_move(tmp_path: Path, make_fake_drive) -> None:
    from protonfs import manifest
    from protonfs.manifest import Manifest

    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"})
    ctx.config.defaults.manifest = True
    ident = ctx.drive.remote_identities(f"{ROOT}/a")["x.jpg"]
    listed = RemoteEntry(
        "a/x.jpg", False, ident.claimed_size, claimed_size=ident.claimed_size,
        sha1=ident.sha1, revision=ident.revision,
    )
    manifest.rebuild(ctx, {"a/x.jpg": listed}, None)
    path = f"{ROOT}/.protonfs/manifest.json"
    assert "a/x.jpg" in Manifest.from_bytes(ctx.drive._remote_content[path]).entries
    _move(tmp_path, "a/x.jpg", "b/x.jpg")

    push(ctx, None, resolve=None, dry_run=False)

    written = Manifest.from_bytes(ctx.drive._remote_content[path])
    assert "a/x.jpg" not in written.entries
    entry = written.entries["b/x.jpg"]
    assert entry.sha256 == ctx.index.get("b/x.jpg").sha256
    assert entry.revision == ctx.drive.revision_uids[f"{ROOT}/b/x.jpg"]


def test_cli_push_reports_moved_files(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"})
    ctx.index.save()
    _move(tmp_path, "a/x.jpg", "b/x.jpg")
    _inject_ctx(monkeypatch, ctx)

    result = CliRunner().invoke(main, ["push"])

    assert result.exit_code == 0, result.output
    assert "moved=1" in result.output
    assert "nothing to push" not in result.output


# --- #197: the deletion policy (keep | trash) ---------------------------------------------


def _photos(tmp_path: Path, make_fake_drive, count: int = 20, **fake_kwargs):
    files = {f"photos/p{i:02d}.jpg": f"photo {i}".encode() for i in range(count)}
    files["top.txt"] = b"root file"
    return _pushed_tree(tmp_path, make_fake_drive, files, **fake_kwargs)


def test_push_keeps_a_file_deleted_locally_by_default(tmp_path: Path, make_fake_drive) -> None:
    ctx = _photos(tmp_path, make_fake_drive)
    (tmp_path / "photos" / "p00.jpg").unlink()

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.trashed == []
    assert result.trashed_items == 0
    assert ctx.index.get("photos/p00.jpg") is not None


def test_push_with_deletions_trash_trashes_the_remote_copy_and_drops_the_entry(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _photos(tmp_path, make_fake_drive)
    (tmp_path / "photos" / "p00.jpg").unlink()

    result = push(ctx, None, resolve=None, dry_run=False, deletions="trash")

    assert ctx.drive.trashed == [f"{ROOT}/photos/p00.jpg"]
    assert result.trashed_items == 1
    assert result.trashed == ["photos/p00.jpg"]
    assert result.failed_items == 0
    assert ctx.index.get("photos/p00.jpg") is None
    assert not _on_drive(ctx.drive, f"{ROOT}/photos/p00.jpg")


def test_push_takes_the_deletion_policy_from_config(tmp_path: Path, make_fake_drive) -> None:
    ctx = _photos(tmp_path, make_fake_drive)
    ctx.config.defaults.deletions = "trash"
    (tmp_path / "photos" / "p00.jpg").unlink()

    push(ctx, None, resolve=None, dry_run=False)
    assert ctx.drive.trashed == [f"{ROOT}/photos/p00.jpg"]

    (tmp_path / "photos" / "p01.jpg").unlink()
    push(ctx, None, resolve=None, dry_run=False, deletions="keep")  # the flag wins
    assert ctx.index.get("photos/p01.jpg") is not None


def test_push_dry_run_lists_what_would_be_trashed(tmp_path: Path, make_fake_drive) -> None:
    ctx = _photos(tmp_path, make_fake_drive)
    (tmp_path / "photos" / "p00.jpg").unlink()
    (tmp_path / "photos" / "p01.jpg").unlink()

    result = push(ctx, None, resolve=None, dry_run=True, deletions="trash")

    assert result.trashed == ["photos/p00.jpg", "photos/p01.jpg"]
    assert result.trashed_items == 2
    assert ctx.drive.trashed == []
    assert ctx.index.get("photos/p00.jpg") is not None


def test_push_never_trashes_a_path_that_is_now_ignored(tmp_path: Path, make_fake_drive) -> None:
    # ignoring a path stops syncing it; it must not delete Drive's copy
    ctx = _photos(tmp_path, make_fake_drive)
    (tmp_path / ".protonfs" / "ignore").write_text("photos/p00.jpg\n")
    (tmp_path / "photos" / "p00.jpg").unlink()

    result = push(ctx, None, resolve=None, dry_run=False, deletions="trash")

    assert ctx.drive.trashed == []
    assert result.trashed_items == 0


def test_push_never_trashes_a_path_listed_in_the_import_block(
    tmp_path: Path, make_fake_drive
) -> None:
    # #205: a file protonfs import moved out of git that is gone locally was removed by
    # another clone's `git pull`, not deleted by the user; its Drive copy must survive.
    from protonfs import importblock

    ctx = _photos(tmp_path, make_fake_drive)
    importblock.add_paths(tmp_path, ["photos/p00.jpg"])
    (tmp_path / "photos" / "p00.jpg").unlink()

    result = push(ctx, None, resolve=None, dry_run=False, deletions="trash")

    assert ctx.drive.trashed == []
    assert result.trashed_items == 0
    assert result.failed_items == 0
    assert result.local_deleted_items == 1
    assert ctx.index.get("photos/p00.jpg") is not None


def test_push_dry_run_does_not_list_import_block_paths_for_trash(
    tmp_path: Path, make_fake_drive
) -> None:
    from protonfs import importblock

    ctx = _photos(tmp_path, make_fake_drive)
    importblock.add_paths(tmp_path, ["photos/p00.jpg"])
    (tmp_path / "photos" / "p00.jpg").unlink()
    (tmp_path / "photos" / "p01.jpg").unlink()

    result = push(ctx, None, resolve=None, dry_run=True, deletions="trash")

    assert result.trashed == ["photos/p01.jpg"]


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True, capture_output=True,
    )


def test_two_clones_import_then_git_pull_then_trash_push_keeps_drive_copies(
    tmp_path: Path, make_fake_drive
) -> None:
    # #205 data-loss guard: clone A imports two files (untracks + ignores them, commits);
    # clone B pulls that commit, which deletes them from B's work tree; B's push with
    # --deletions trash must leave their Drive copies alone, and pull restores them.
    from protonfs import gitops, importblock
    from protonfs.commands.pull import pull

    files = {f"frames/f{i}.mp4": f"frame {i}".encode() for i in range(3)}
    files["frames/keep.txt"] = b"stays in git"
    a_repo = tmp_path / "a"
    a_store = a_repo / "store"
    for rel, data in files.items():
        (a_store / rel).parent.mkdir(parents=True, exist_ok=True)
        (a_store / rel).write_bytes(data)
    _git(a_repo, "init", "-q")
    _git(a_repo, "add", "-A")
    _git(a_repo, "commit", "-q", "-m", "data in git")
    _git(tmp_path, "clone", "-q", str(a_repo), "b")
    b_store = tmp_path / "b" / "store"

    drive = make_fake_drive()
    init_config(a_store, ROOT)
    a_ctx = load_context(a_store)
    a_ctx.drive = drive
    push(a_ctx, None, resolve=None, dry_run=False)
    init_config(b_store, ROOT)
    b_ctx = load_context(b_store)
    b_ctx.drive = drive
    push(b_ctx, None, resolve=None, dry_run=False)  # B now holds the files too
    assert b_ctx.index.get("frames/f0.mp4") is not None

    imported = ["frames/f0.mp4", "frames/f1.mp4"]
    importblock.add_paths(a_store, imported)
    gitops.rm_cached(a_store, imported)
    gitops.add(a_store, [".gitignore"])
    _git(a_repo, "commit", "-q", "-m", "import")
    _git(tmp_path / "b", "pull", "-q")
    assert not (b_store / "frames" / "f0.mp4").exists()  # git removed them on B

    result = push(b_ctx, None, resolve=None, dry_run=False, deletions="trash")

    assert drive.trashed == []
    assert result.failed_items == 0
    assert result.local_deleted_items == 2
    pull(b_ctx, None, None, False)
    assert (b_store / "frames" / "f0.mp4").read_bytes() == b"frame 0"
    assert (b_store / "frames" / "f1.mp4").read_bytes() == b"frame 1"


def test_push_refuses_to_trash_a_vanished_top_level_directory_without_the_override(
    tmp_path: Path, make_fake_drive
) -> None:
    import shutil

    ctx = _photos(tmp_path, make_fake_drive)
    shutil.rmtree(tmp_path / "photos")

    refused = push(ctx, None, resolve=None, dry_run=False, deletions="trash")

    assert ctx.drive.trashed == []
    assert refused.trashed_items == 0
    assert refused.failed_items == 1
    assert refused.failures[0]["kind"] == "deletion-refused"
    assert "photos" in refused.failures[0]["error"]
    assert ctx.index.get("photos/p00.jpg") is not None

    allowed = push(
        ctx, None, resolve=None, dry_run=False, deletions="trash", allow_mass_delete=True
    )

    assert allowed.trashed_items == 20
    assert allowed.failed_items == 0


def test_push_refuses_to_trash_everything_this_machine_held_even_with_the_override(
    tmp_path: Path, make_fake_drive
) -> None:
    # every held file gone at once looks like an unmounted disk or the wrong directory
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a.txt": b"a", "b.txt": b"b"})
    (tmp_path / "a.txt").unlink()
    (tmp_path / "b.txt").unlink()

    result = push(
        ctx, None, resolve=None, dry_run=False, deletions="trash", allow_mass_delete=True
    )

    assert ctx.drive.trashed == []
    assert result.failures[0]["kind"] == "deletion-refused"
    assert "protonfs rm" in result.failures[0]["error"]


def test_push_refuses_a_mass_deletion_over_the_per_run_cap(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _photos(tmp_path, make_fake_drive)
    for i in range(12):  # 12 of 21 tracked files
        (tmp_path / "photos" / f"p{i:02d}.jpg").unlink()

    refused = push(ctx, None, resolve=None, dry_run=True, deletions="trash")

    assert refused.failures[0]["kind"] == "deletion-refused"
    assert "--allow-mass-delete" in refused.failures[0]["error"]
    assert "12" in refused.failures[0]["error"]

    allowed = push(
        ctx, None, resolve=None, dry_run=False, deletions="trash", allow_mass_delete=True
    )
    assert allowed.trashed_items == 12


def test_push_caps_a_run_at_the_absolute_limit(
    tmp_path: Path, make_fake_drive, monkeypatch
) -> None:
    monkeypatch.setattr("protonfs.commands.push.MASS_DELETE_LIMIT", 2)
    monkeypatch.setattr("protonfs.commands.push.MASS_DELETE_FRACTION", 1.0)
    ctx = _photos(tmp_path, make_fake_drive)
    for i in range(3):
        (tmp_path / "photos" / f"p{i:02d}.jpg").unlink()

    result = push(ctx, None, resolve=None, dry_run=False, deletions="trash")

    assert ctx.drive.trashed == []
    assert result.failures[0]["kind"] == "deletion-refused"


def test_push_reports_a_trash_that_failed_and_keeps_the_entry(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _photos(tmp_path, make_fake_drive, fail_trash=True)
    (tmp_path / "photos" / "p00.jpg").unlink()

    result = push(ctx, None, resolve=None, dry_run=False, deletions="trash")

    assert result.trashed_items == 0
    assert result.failed_items == 1
    assert result.failures[0]["kind"] == "trash"
    assert ctx.index.get("photos/p00.jpg") is not None


def test_push_trashes_the_old_path_of_a_move_it_had_to_upload_instead(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _photos(tmp_path, make_fake_drive, fail_moves=True)
    _move(tmp_path, "photos/p00.jpg", "misc/p00.jpg")

    result = push(ctx, None, resolve=None, dry_run=False, deletions="trash")

    assert result.moved_items == 0
    assert result.transferred_items == 1
    assert result.trashed == ["photos/p00.jpg"]
    assert ctx.index.get("photos/p00.jpg") is None
    assert ctx.index.get("misc/p00.jpg") is not None


def test_push_never_trashes_the_old_path_of_a_move_it_applied(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _photos(tmp_path, make_fake_drive)
    _move(tmp_path, "photos/p00.jpg", "misc/p00.jpg")

    result = push(ctx, None, resolve=None, dry_run=False, deletions="trash")

    assert result.moved_items == 1
    assert ctx.drive.trashed == []
    assert _on_drive(ctx.drive, f"{ROOT}/misc/p00.jpg")


def test_push_with_a_subpath_or_named_files_trashes_nothing_outside_them(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _pushed_tree(
        tmp_path, make_fake_drive,
        {"a/x": b"x", "a/keep": b"k", "b/y": b"y", "b/keep": b"k2"},
    )
    (tmp_path / "a" / "x").unlink()
    (tmp_path / "b" / "y").unlink()

    push(ctx, "b", resolve=None, dry_run=False, deletions="trash")
    push(ctx, None, resolve=None, dry_run=False, deletions="trash", only={"b/keep"})

    assert ctx.drive.trashed == [f"{ROOT}/b/y"]
    assert ctx.index.get("a/x") is not None


def test_push_drops_a_trashed_file_from_the_remote_manifest(
    tmp_path: Path, make_fake_drive
) -> None:
    from protonfs import manifest
    from protonfs.manifest import Manifest

    ctx = _photos(tmp_path, make_fake_drive)
    ctx.config.defaults.manifest = True
    ident = ctx.drive.remote_identities(f"{ROOT}/photos")["p00.jpg"]
    listed = RemoteEntry(
        "photos/p00.jpg", False, ident.claimed_size, claimed_size=ident.claimed_size,
        sha1=ident.sha1, revision=ident.revision,
    )
    manifest.rebuild(ctx, {"photos/p00.jpg": listed}, None)
    (tmp_path / "photos" / "p00.jpg").unlink()

    push(ctx, None, resolve=None, dry_run=False, deletions="trash")

    path = f"{ROOT}/.protonfs/manifest.json"
    assert "photos/p00.jpg" not in Manifest.from_bytes(ctx.drive._remote_content[path]).entries


def test_cli_push_deletions_trash_reports_and_lists_on_a_dry_run(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    ctx = _photos(tmp_path, make_fake_drive)
    ctx.index.save()
    (tmp_path / "photos" / "p00.jpg").unlink()
    _inject_ctx(monkeypatch, ctx)

    preview = CliRunner().invoke(main, ["push", "--dry-run", "--deletions", "trash"])

    assert preview.exit_code == 0, preview.output
    assert "trashed=1" in preview.output
    assert "trash photos/p00.jpg" in preview.output
    assert ctx.drive.trashed == []

    done = CliRunner().invoke(main, ["push", "--deletions", "trash"])

    assert done.exit_code == 0, done.output
    assert "trashed=1" in done.output
    assert ctx.drive.trashed == [f"{ROOT}/photos/p00.jpg"]


def test_cli_push_refused_deletion_exits_1_and_names_the_override(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    import shutil

    from click.testing import CliRunner

    from protonfs.cli import main

    ctx = _photos(tmp_path, make_fake_drive)
    ctx.index.save()
    shutil.rmtree(tmp_path / "photos")
    _inject_ctx(monkeypatch, ctx)

    result = CliRunner().invoke(main, ["push", "--deletions", "trash"])

    assert result.exit_code == 1, result.output
    assert "--allow-mass-delete" in result.output
    assert "--resolve" not in result.output  # not a remote conflict


def test_push_never_trashes_the_old_path_of_a_move_held_by_the_settle_window(
    tmp_path: Path, make_fake_drive
) -> None:
    # a copy-then-delete move gives the new file a fresh mtime, so --min-age holds it
    # back; until it is pushed, Drive's copy at the old path is the only one
    ctx = _photos(tmp_path, make_fake_drive)
    data = (tmp_path / "photos" / "p00.jpg").read_bytes()
    (tmp_path / "photos" / "p00.jpg").unlink()
    (tmp_path / "misc").mkdir()
    (tmp_path / "misc" / "p00.jpg").write_bytes(data)

    result = push(ctx, None, resolve=None, dry_run=False, deletions="trash", min_age=3600)

    assert result.unsettled_items == 1
    assert ctx.drive.trashed == []
    assert ctx.index.get("photos/p00.jpg") is not None


# --- #197: push reports the local deletions it left on Drive ------------------------------


def test_push_counts_the_local_deletions_it_kept(tmp_path: Path, make_fake_drive) -> None:
    ctx = _photos(tmp_path, make_fake_drive)
    (tmp_path / "photos" / "p00.jpg").unlink()
    (tmp_path / "photos" / "p01.jpg").unlink()
    _move(tmp_path, "photos/p02.jpg", "misc/p02.jpg")

    preview = push(ctx, None, resolve=None, dry_run=True)
    result = push(ctx, None, resolve=None, dry_run=False)

    assert preview.local_deleted_items == 2
    assert result.local_deleted_items == 2  # the move is not a deletion
    assert result.moved_items == 1

    trashed = push(ctx, None, resolve=None, dry_run=False, deletions="trash")
    assert trashed.local_deleted_items == 0
    assert trashed.trashed_items == 2


def test_cli_push_prints_every_per_run_total_on_its_summary_line(
    tmp_path: Path, monkeypatch, make_fake_drive
) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    ctx = _photos(tmp_path, make_fake_drive)
    ctx.index.save()
    (tmp_path / "photos" / "p00.jpg").unlink()
    _move(tmp_path, "photos/p01.jpg", "misc/p01.jpg")
    _inject_ctx(monkeypatch, ctx)

    result = CliRunner().invoke(main, ["push"])

    assert result.exit_code == 0, result.output
    summary = next(line for line in result.output.splitlines() if "transferred=" in line)
    assert "moved=1" in summary and "local-deleted=1" in summary


# --- #197 review fixes ---------------------------------------------------------------------


def test_push_uploads_a_regenerated_identical_file_instead_of_moving_the_old_one(
    tmp_path: Path, make_fake_drive
) -> None:
    # git-LFS use: run1/cal.dat removed to free disk, an identical run2/cal.dat written
    # later. mv keeps the mtime; regenerating does not, so this is not a move and run1's
    # Drive copy must stay where it is.
    import os

    ctx = _pushed_tree(tmp_path, make_fake_drive, {"run1/cal.dat": b"calibration"})
    (tmp_path / "run1" / "cal.dat").unlink()
    (tmp_path / "run2").mkdir()
    (tmp_path / "run2" / "cal.dat").write_bytes(b"calibration")
    old = ctx.index.get("run1/cal.dat").mtime
    os.utime(tmp_path / "run2" / "cal.dat", (old + 3600, old + 3600))

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.move_calls == [] and ctx.drive.rename_calls == []
    assert result.moved_items == 0 and result.transferred_items == 1
    assert _on_drive(ctx.drive, f"{ROOT}/run1/cal.dat")
    assert ctx.index.get("run1/cal.dat") is not None


def test_push_never_pairs_empty_files_as_moves(tmp_path: Path, make_fake_drive) -> None:
    # every empty file has the same content, so content says nothing about which moved
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/.keep": b"", "a/x.jpg": b"photo"})
    _move(tmp_path, "a/.keep", "b/.keep")

    result = push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.move_calls == []
    assert result.moved_items == 0


def test_push_finishes_a_move_whose_index_save_was_interrupted(
    tmp_path: Path, make_fake_drive
) -> None:
    # the Drive move landed, then the process died before the index was saved: the next
    # push must recognise the node at the new path, not upload a second copy beside it
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"})
    _move(tmp_path, "a/x.jpg", "b/x.jpg")
    ctx.drive.move([f"{ROOT}/a/x.jpg"], f"{ROOT}/b")
    ctx.drive.move_calls.clear()

    result = push(ctx, None, resolve=None, dry_run=False)

    assert _uploaded(ctx.drive) == []
    assert ctx.drive.move_calls == []
    assert result.moved_items == 1
    assert ctx.index.get("a/x.jpg") is None
    assert ctx.index.get("b/x.jpg").remote_path == f"{ROOT}/b/x.jpg"


def test_push_moves_the_node_back_when_the_rename_after_its_move_fails(
    tmp_path: Path, make_fake_drive
) -> None:
    # otherwise a stray b/x.jpg is left on Drive that no host ever had
    ctx = _pushed_tree(tmp_path, make_fake_drive, {"a/x.jpg": b"photo"}, fail_renames=True)
    _move(tmp_path, "a/x.jpg", "b/y.jpg")

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.moved_items == 0
    assert _uploaded(ctx.drive) == [str(tmp_path / "b" / "y.jpg")]
    assert ctx.drive.move_calls[-1] == ((f"{ROOT}/b/x.jpg",), f"{ROOT}/a")
    assert not _on_drive(ctx.drive, f"{ROOT}/b/x.jpg")
    assert _on_drive(ctx.drive, f"{ROOT}/a/x.jpg")
    assert ctx.index.get("a/x.jpg").remote_path == f"{ROOT}/a/x.jpg"


def test_push_mass_delete_cap_counts_only_files_held_here(
    tmp_path: Path, make_fake_drive
) -> None:
    # offloaded entries are not on this machine, so they cannot dilute the fraction: 12
    # of 21 held files gone is a mass deletion however many offloaded entries exist
    ctx = _photos(tmp_path, make_fake_drive)
    for i in range(500):
        ctx.index.set(f"cold/c{i:03d}", dataclasses.replace(
            ctx.index.get("top.txt"), remote_path=f"{ROOT}/cold/c{i:03d}",
            local_state="metadata-only",
        ))
    for i in range(12):
        (tmp_path / "photos" / f"p{i:02d}.jpg").unlink()

    refused = push(ctx, None, resolve=None, dry_run=True, deletions="trash")

    assert refused.failures and refused.failures[0]["kind"] == "deletion-refused"
