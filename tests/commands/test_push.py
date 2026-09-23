from __future__ import annotations

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
from protonfs.drive import DriveError, RemoteIdentity, TransferResult
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
