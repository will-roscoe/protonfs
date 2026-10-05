"""Tests for re-verifying the index against remote listings without a local scan (#169)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import DriveError, DriveThrottleError
from protonfs.index import IndexEntry
from protonfs.indexcheck import Verdict, check_index, repair_index

ROOT = "/my-files/test"


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _entry(rel: str, *, size: int, sha1: str = "", state: str = "present") -> IndexEntry:
    return IndexEntry(
        size=size,
        mtime=1.0,
        sha256="s" * 64 if state == "present" else "",
        sha1=sha1,
        remote_path=f"{ROOT}/{rel}",
        origin_device="d1",
        local_state=state,
        last_synced="2026-08-01T00:00:00+00:00",
    )


def _remote(fake, rel: str, data: bytes | None = None, *, size: int | None = None) -> None:
    """Put `rel` on the fake remote: real content, or just a claimed size (no sha1)."""
    parent, _, name = f"{ROOT}/{rel}".rpartition("/")
    if data is not None:
        fake._remote_files.setdefault(parent, {})[name] = len(data)
        fake._remote_sha1.setdefault(parent, {})[name] = _sha1(data)
    else:
        fake._remote_files.setdefault(parent, {})[name] = size
    fake.revision_uids[f"{parent}/{name}"] = f"rev-{rel}"


@pytest.fixture
def ctx(tmp_path: Path, make_fake_drive):
    init_config(tmp_path, ROOT)
    context = load_context(tmp_path)
    context.drive = make_fake_drive()
    return context


def _verdicts(check) -> dict[str, Verdict]:
    return {f.rel_path: f.verdict for f in check.findings}


def test_check_index_gives_every_entry_a_verdict_from_its_directory_listing(ctx) -> None:
    fake = ctx.drive
    ctx.index.set("run/ok", _entry("run/ok", size=4, sha1=_sha1(b"data")))
    _remote(fake, "run/ok", b"data")
    ctx.index.set("run/gone", _entry("run/gone", size=4))
    ctx.index.set("run/short", _entry("run/short", size=10, state="metadata-only"))
    _remote(fake, "run/short", b"abc")
    ctx.index.set("run/stub", _entry("run/stub", size=131))
    _remote(fake, "run/stub", b"x" * 5000)
    ctx.index.set("run/other", _entry("run/other", size=4, sha1=_sha1(b"mine")))
    _remote(fake, "run/other", b"them")
    ctx.index.set("run/nosize", _entry("run/nosize", size=4))
    _remote(fake, "run/nosize", size=None)

    check = check_index(ctx)

    assert _verdicts(check) == {
        "run/ok": Verdict.OK,
        "run/gone": Verdict.MISSING,
        "run/short": Verdict.REMOTE_SMALLER,
        "run/stub": Verdict.REMOTE_LARGER,
        "run/other": Verdict.DIGEST_DIFFERS,
        "run/nosize": Verdict.UNSIZED,
    }
    assert fake.identity_calls == [f"{ROOT}/run"]  # one listing per directory


def test_check_index_reads_no_local_file(ctx, monkeypatch) -> None:
    # The point of #169's read-only check: a host on a slow filesystem cannot afford a
    # local walk, so the comparison is index against listings only.
    ctx.index.set("run/a", _entry("run/a", size=4))
    _remote(ctx.drive, "run/a", b"data")
    monkeypatch.setattr(Path, "rglob", lambda *a, **k: pytest.fail("walked the tree"))
    monkeypatch.setattr(Path, "read_bytes", lambda *a, **k: pytest.fail("read a file"))

    assert _verdicts(check_index(ctx)) == {"run/a": Verdict.OK}


def test_check_index_lists_directories_holding_drive_only_copies_first(ctx) -> None:
    # Metadata-only entries come first: Drive holds their only copy.
    fake = ctx.drive
    ctx.index.set("a/present", _entry("a/present", size=1))
    ctx.index.set("b/offloaded", _entry("b/offloaded", size=1, state="metadata-only"))

    check_index(ctx)

    assert fake.identity_calls == [f"{ROOT}/b", f"{ROOT}/a"]


def test_check_index_reports_a_failed_listing_without_judging_its_entries(
    ctx, monkeypatch
) -> None:
    fake = ctx.drive
    ctx.index.set("a/x", _entry("a/x", size=1))
    ctx.index.set("b/y", _entry("b/y", size=4))
    _remote(fake, "b/y", b"data")
    real = fake.remote_identities

    def flaky(parent):
        if parent == f"{ROOT}/a":
            raise DriveError("list failed: no such folder")
        return real(parent)

    monkeypatch.setattr(fake, "remote_identities", flaky)

    check = check_index(ctx)

    assert _verdicts(check) == {"a/x": Verdict.UNLISTED, "b/y": Verdict.OK}
    assert check.unlisted == {f"{ROOT}/a": "list failed: no such folder"}


def test_check_index_aborts_on_a_throttled_listing(ctx, monkeypatch) -> None:
    # A throttled Drive is not evidence about any file; carrying on would only produce a
    # report full of holes.
    ctx.index.set("a/x", _entry("a/x", size=1))

    def throttled(parent):
        raise DriveThrottleError("throttled")

    monkeypatch.setattr(ctx.drive, "remote_identities", throttled)

    with pytest.raises(DriveThrottleError):
        check_index(ctx)


def test_check_index_honours_subpath_and_skips_control_paths(ctx) -> None:
    ctx.index.set("a/x", _entry("a/x", size=1))
    ctx.index.set("b/y", _entry("b/y", size=1))
    ctx.index.set(".protonfs/manifest.json", _entry(".protonfs/manifest.json", size=1))

    assert set(_verdicts(check_index(ctx, "a"))) == {"a/x"}
    assert set(_verdicts(check_index(ctx))) == {"a/x", "b/y"}


# --- repair ----------------------------------------------------------------------------


def test_repair_unindexes_a_present_file_whose_drive_copy_does_not_match(
    ctx, tmp_path: Path
) -> None:
    # The local copy is still here, so it is the one to keep: dropping the entry makes it
    # local-only, never counted as synced, never offloaded, and the next push uploads it.
    for rel in ("run/short", "run/gone"):
        (tmp_path / rel).parent.mkdir(exist_ok=True)
        (tmp_path / rel).write_bytes(b"0123456789")
        ctx.index.set(rel, _entry(rel, size=10))
    _remote(ctx.drive, "run/short", b"0123")

    outcome = repair_index(ctx, check_index(ctx))

    assert ctx.index.get("run/short") is None and ctx.index.get("run/gone") is None
    assert sorted(outcome.unindexed) == ["run/gone", "run/short"]


def test_repair_turns_a_present_entry_whose_file_is_gone_into_the_drive_copy(
    ctx,
) -> None:
    # The #169 comment's 8,024 entries: git-LFS pointer stubs hashed as content (131 B),
    # marked present, while the files are gone locally and Drive holds the real ones.
    # Drive's copy is the only one; the entry is rewritten to describe it, metadata-only.
    real = b"r" * 5000
    ctx.index.set("run/dump", _entry("run/dump", size=131))
    _remote(ctx.drive, "run/dump", real)

    outcome = repair_index(ctx, check_index(ctx))

    entry = ctx.index.get("run/dump")
    assert entry.local_state == "metadata-only"
    assert (entry.size, entry.sha1, entry.sha256) == (5000, _sha1(real), "")
    assert outcome.adopted_remote == ["run/dump"]


def test_repair_takes_a_larger_drive_copy_for_a_metadata_only_entry(ctx) -> None:
    real = b"r" * 5000
    ctx.index.set("run/dump", _entry("run/dump", size=131, state="metadata-only"))
    _remote(ctx.drive, "run/dump", real)

    repair_index(ctx, check_index(ctx))

    assert (ctx.index.get("run/dump").size, ctx.index.get("run/dump").sha1) == (
        5000, _sha1(real),
    )


@pytest.mark.parametrize("state", ["metadata-only", "present"])
@pytest.mark.parametrize("remote", [b"abc", b"0123456789", None])
def test_repair_never_hides_a_short_different_or_missing_only_copy(
    ctx, remote, state
) -> None:
    # Offloaded (or deleted locally) after a weaker check, with Drive now holding less
    # than was recorded, other bytes of the same size, or nothing. Drive is the only
    # copy; rewriting the entry to match would hide the loss, so it is reported and left
    # exactly as it was.
    before = _entry("run/dump", size=10, sha1=_sha1(b"9876543210"), state=state)
    ctx.index.set("run/dump", before)
    if remote is not None:
        _remote(ctx.drive, "run/dump", remote)

    outcome = repair_index(ctx, check_index(ctx))

    assert ctx.index.get("run/dump") == before
    assert outcome.suspect == ["run/dump"]


def test_repair_records_drive_sha1_for_an_offloaded_entry_that_had_none(ctx) -> None:
    # v1 -> v2 seeded sha1 = "" for every entry. For a metadata-only entry the index
    # describes Drive's copy, so a matching listing's digest belongs in it.
    ctx.index.set("run/dump", _entry("run/dump", size=4, state="metadata-only"))
    _remote(ctx.drive, "run/dump", b"data")

    repair_index(ctx, check_index(ctx))

    assert ctx.index.get("run/dump").sha1 == _sha1(b"data")


def test_repair_leaves_unverifiable_entries_alone(ctx, tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "nosize").write_bytes(b"data")
    before = _entry("run/nosize", size=4)
    ctx.index.set("run/nosize", before)
    _remote(ctx.drive, "run/nosize", size=None)

    outcome = repair_index(ctx, check_index(ctx))

    assert ctx.index.get("run/nosize") == before
    assert outcome.unindexed == outcome.adopted_remote == outcome.suspect == []


# --- report ----------------------------------------------------------------------------


def test_report_counts_verdicts_and_lists_each_mismatch_drive_only_copies_first(
    ctx,
) -> None:
    from protonfs.indexcheck import report_lines

    ctx.index.set("a/ok", _entry("a/ok", size=4))
    _remote(ctx.drive, "a/ok", b"data")
    ctx.index.set("a/stub", _entry("a/stub", size=131))
    _remote(ctx.drive, "a/stub", b"x" * 5000)
    ctx.index.set("b/short", _entry("b/short", size=10, state="metadata-only"))
    _remote(ctx.drive, "b/short", b"abc")

    lines = report_lines(check_index(ctx))

    assert lines[0] == (
        "checked 3 index entries in 2 directories: ok=1 remote-larger=1 remote-smaller=1"
    )
    text = "\n".join(lines)
    assert text.index("b/short") < text.index("a/stub")  # Drive-only copies first
    assert "b/short [metadata-only]: index 10 B, Drive 3 B" in text
    assert "--repair" in text


def test_report_after_repair_says_what_changed_and_what_was_left(ctx, tmp_path) -> None:
    from protonfs.indexcheck import report_lines

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "kept").write_bytes(b"0123456789")
    ctx.index.set("a/kept", _entry("a/kept", size=10))
    ctx.index.set("b/short", _entry("b/short", size=10, state="metadata-only"))
    _remote(ctx.drive, "b/short", b"abc")
    check = check_index(ctx)

    text = "\n".join(report_lines(check, repair_index(ctx, check)))

    assert "dropped 1 entry whose local copy is kept" in text
    assert "left 1 entry untouched" in text
    assert "--repair" not in text


def test_report_lists_directories_it_could_not_list(ctx, monkeypatch) -> None:
    from protonfs.indexcheck import report_lines

    ctx.index.set("a/x", _entry("a/x", size=1))
    monkeypatch.setattr(
        ctx.drive, "remote_identities", lambda parent: (_ for _ in ()).throw(DriveError("boom"))
    )

    text = "\n".join(report_lines(check_index(ctx)))

    assert f"{ROOT}/a: boom" in text
    assert "unlisted=1" in text


def test_report_on_a_clean_index_is_one_line(ctx) -> None:
    from protonfs.indexcheck import report_lines

    ctx.index.set("a/ok", _entry("a/ok", size=4))
    _remote(ctx.drive, "a/ok", b"data")

    assert report_lines(check_index(ctx)) == [
        "checked 1 index entry in 1 directory: ok=1"
    ]


# --- reverify_index --------------------------------------------------------------------


def _legacy(ctx) -> None:
    """Mark ctx's index as one an earlier release wrote (#169)."""
    ctx.index.set_check_level(0)


def test_reverify_repairs_saves_and_raises_the_check_level(ctx, tmp_path) -> None:
    from protonfs.index import CHECK_LEVEL, IndexStore
    from protonfs.indexcheck import reverify_index

    _legacy(ctx)
    ctx.index.set("run/dump", _entry("run/dump", size=131))
    _remote(ctx.drive, "run/dump", b"r" * 5000)

    check, repair = reverify_index(ctx)

    assert check.complete and repair.adopted_remote == ["run/dump"]
    on_disk = IndexStore(tmp_path)
    assert on_disk.check_level == CHECK_LEVEL
    assert on_disk.get("run/dump").local_state == "metadata-only"


@pytest.mark.parametrize("gap", ["unlisted", "unsized"])
def test_reverify_keeps_the_old_level_while_any_entry_is_unchecked(
    ctx, tmp_path, monkeypatch, gap
) -> None:
    # An entry that could not be checked is still only as trusted as before, so the
    # index must not claim the new level; the next upgrade tries again.
    from protonfs.index import IndexStore
    from protonfs.indexcheck import reverify_index

    _legacy(ctx)
    ctx.index.set("a/x", _entry("a/x", size=4))
    if gap == "unsized":
        _remote(ctx.drive, "a/x", size=None)
    else:
        monkeypatch.setattr(
            ctx.drive, "remote_identities",
            lambda parent: (_ for _ in ()).throw(DriveError("boom")),
        )

    check, _ = reverify_index(ctx)

    assert not check.complete
    assert IndexStore(tmp_path).check_level == 0


# --- `protonfs verify --index` ---------------------------------------------------------


@pytest.fixture
def cli_ctx(ctx, monkeypatch):
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    return ctx


def _invoke(*args: str):
    from click.testing import CliRunner

    from protonfs.cli import main

    return CliRunner().invoke(main, ["verify", "--index", *args])


def test_verify_index_reports_mismatches_changes_nothing_and_exits_1(cli_ctx) -> None:
    cli_ctx.index.set("run/dump", _entry("run/dump", size=131))
    _remote(cli_ctx.drive, "run/dump", b"r" * 5000)
    before = cli_ctx.index.all()

    result = _invoke()

    assert result.exit_code == 1, result.output
    assert "remote-larger: run/dump [present]: index 131 B, Drive 5000 B" in result.output
    assert "--repair" in result.output
    assert cli_ctx.index.all() == before
    assert not cli_ctx.drive.list_calls  # no remote walk: listings per directory only


def test_verify_index_exits_0_when_every_entry_matches(cli_ctx) -> None:
    cli_ctx.index.set("run/dump", _entry("run/dump", size=4))
    _remote(cli_ctx.drive, "run/dump", b"data")

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert "ok=1" in result.output


def test_verify_index_repair_applies_and_records_the_check_level(
    cli_ctx, tmp_path
) -> None:
    from protonfs.index import CHECK_LEVEL, IndexStore

    cli_ctx.index.set_check_level(0)
    cli_ctx.index.set("run/dump", _entry("run/dump", size=131))
    _remote(cli_ctx.drive, "run/dump", b"r" * 5000)
    cli_ctx.index.save()

    result = _invoke("--repair")

    assert result.exit_code == 0, result.output
    assert "rewrote 1 entry" in result.output
    assert f"check level {CHECK_LEVEL}" in result.output
    on_disk = IndexStore(tmp_path)
    assert on_disk.get("run/dump").local_state == "metadata-only"
    assert on_disk.check_level == CHECK_LEVEL


def test_verify_index_repair_exits_1_while_a_drive_only_copy_is_suspect(cli_ctx) -> None:
    cli_ctx.index.set("run/dump", _entry("run/dump", size=10, state="metadata-only"))
    _remote(cli_ctx.drive, "run/dump", b"abc")

    result = _invoke("--repair")

    assert result.exit_code == 1, result.output
    assert "left 1 entry untouched" in result.output
