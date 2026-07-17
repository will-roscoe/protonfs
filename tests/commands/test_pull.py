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
            sha1="",
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
            sha1="",
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
                sha1="",
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
            "",
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


def _diverged_setup(tmp_path: Path):
    """A file present locally with content differing from its index entry, and a remote
    walk entry whose sha1 differs from the index -> classify() sees BOTH_MODIFIED."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump").write_bytes(b"LOCAL-EDIT")
    ctx.index.set(
        "run1/dump",
        IndexEntry(
            size=4,
            mtime=1.0,
            sha256="index-sha256",  # != hash of b"LOCAL-EDIT" -> local changed
            sha1="index-sha1",  # != remote sha1 below -> remote changed
            remote_path="/my-files/test/run1/dump",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    return ctx


def _both_modified_walk():
    # remote copy of run1/dump with a sha1 differing from the index -> remote diverged
    return [RemoteEntry("run1/dump", is_dir=False, size=9, claimed_size=9, sha1="remote-sha1")]


def test_pull_resolve_remote_overwrites_local(tmp_path: Path, make_fake_drive) -> None:
    ctx = _diverged_setup(tmp_path)
    ctx.drive = make_fake_drive(walk_entries=_both_modified_walk())

    result = pull(ctx, None, resolve="remote", dry_run=False)

    assert result.transferred_items == 1
    assert (tmp_path / "run1" / "dump").read_bytes() == b"downloaded"  # remote won
    assert ctx.index.get("run1/dump").local_state == "present"
    assert result.failed_items == 0


def test_pull_resolve_local_keeps_local_and_skips(tmp_path: Path, make_fake_drive) -> None:
    ctx = _diverged_setup(tmp_path)
    ctx.drive = make_fake_drive(walk_entries=_both_modified_walk())

    result = pull(ctx, None, resolve="local", dry_run=False)

    assert result.skipped_items == 1
    assert (tmp_path / "run1" / "dump").read_bytes() == b"LOCAL-EDIT"  # untouched
    assert ctx.index.get("run1/dump").sha256 == "index-sha256"  # index unchanged
    assert result.failed_items == 0


def test_pull_resolve_both_fetches_remote_under_suffix(tmp_path: Path, make_fake_drive) -> None:
    ctx = _diverged_setup(tmp_path)
    ctx.drive = make_fake_drive(walk_entries=_both_modified_walk())

    result = pull(ctx, None, resolve="both", dry_run=False)

    assert (tmp_path / "run1" / "dump").read_bytes() == b"LOCAL-EDIT"  # local untouched
    assert (tmp_path / "run1" / "dump.remote").read_bytes() == b"downloaded"  # remote alongside
    assert ctx.index.get("run1/dump.remote") is None  # suffixed copy is not tracked
    assert result.transferred_items == 1


def test_pull_no_resolve_reports_conflict_and_does_not_touch(
    tmp_path: Path, make_fake_drive
) -> None:
    ctx = _diverged_setup(tmp_path)
    ctx.drive = make_fake_drive()  # no walk needed; resolve=None takes no remote view

    result = pull(ctx, None, resolve=None, dry_run=False)

    assert result.failed_items == 1
    assert result.failures[0]["kind"] == "conflict"
    assert (tmp_path / "run1" / "dump").read_bytes() == b"LOCAL-EDIT"  # untouched
    assert result.transferred_items == 0


def _metadata_only_entry(remote_path: str) -> IndexEntry:
    return IndexEntry(
        size=1,
        mtime=1.0,
        sha256="placeholder",
        sha1="",
        remote_path=remote_path,
        origin_device="other-device",
        local_state="metadata-only",
        last_synced="2026-07-08T00:00:00+00:00",
    )


def test_pull_subpath_never_touches_entries_outside_it(
    tmp_path: Path, make_fake_drive
) -> None:
    """#96: `pull SUBPATH` must be scoped to SUBPATH. classify() reasons over the
    whole repo-wide index, so without a within_subpath filter every metadata-only
    entry elsewhere in the repo (e.g. every other sim dir seeded by refresh) lands
    in to_pull -- pulling unrelated directories and hammering the API."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("wanted/dump_0001", _metadata_only_entry("/my-files/test/wanted/dump_0001"))
    ctx.index.set(
        "unrelated/dump_0002", _metadata_only_entry("/my-files/test/unrelated/dump_0002")
    )
    ctx.drive = make_fake_drive()

    result = pull(ctx, "wanted", resolve=None, dry_run=False)

    assert result.transferred_items == 1
    assert (tmp_path / "wanted" / "dump_0001").exists()
    # The out-of-scope entry: no download, no local file, index untouched.
    assert not (tmp_path / "unrelated" / "dump_0002").exists()
    downloaded = [p for call in ctx.drive.download_calls for p in call[0]]
    assert downloaded == ["/my-files/test/wanted/dump_0001"]
    assert ctx.index.get("unrelated/dump_0002").local_state == "metadata-only"


def test_pull_subpath_scopes_true_remote_only_entries_too(
    tmp_path: Path, make_fake_drive
) -> None:
    """#96 companion: an index entry recorded `present` but missing on disk classifies
    REMOTE_ONLY (no remote view) -- out-of-scope ones must not be re-downloaded either."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("wanted/dump_0001", _metadata_only_entry("/my-files/test/wanted/dump_0001"))
    gone = _metadata_only_entry("/my-files/test/elsewhere/dump_0003")
    gone.local_state = "present"  # was materialized once; now absent on disk
    ctx.index.set("elsewhere/dump_0003", gone)
    ctx.drive = make_fake_drive()

    result = pull(ctx, "wanted", resolve=None, dry_run=False)

    assert result.transferred_items == 1
    assert not (tmp_path / "elsewhere" / "dump_0003").exists()


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


# --- #93: progress reporting via the Reporter -------------------------------------------


class _RecordingReporter:
    def __init__(self):
        self.calls = []

    def phase(self, name, **f):
        self.calls.append(("phase", name))

    def progress(self, d, t, **f):
        self.calls.append(("progress", d, t))

    def item(self, a, p):
        self.calls.append(("item", p))

    def warn(self, m):
        self.calls.append(("warn", m))

    def done(self, m, **f):
        self.calls.append(("done", m))

    import contextlib

    @contextlib.contextmanager
    def timed(self, name):
        self.calls.append(("phase", name))
        yield
        self.calls.append(("done", name))


def test_pull_narrates_phases(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("a/f", _metadata_only_entry("/my-files/test/a/f"))
    ctx.drive = make_fake_drive()
    rep = _RecordingReporter()

    pull(ctx, None, resolve=None, dry_run=False, reporter=rep)

    kinds = [c[0] for c in rep.calls]
    assert "phase" in kinds and "done" in kinds


def test_pull_reports_progress_per_batch(tmp_path: Path, monkeypatch, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    for rel in ("run1/a", "run1/b", "run2/c"):
        ctx.index.set(rel, _metadata_only_entry(f"/my-files/test/{rel}"))
    ctx.drive = make_fake_drive()
    # Single-file batches so the callback cadence is asserted, not just the final call.
    monkeypatch.setattr(
        "protonfs.commands.pull.batches", lambda items, size=1: [[i] for i in items]
    )

    rep = _RecordingReporter()
    result = pull(ctx, None, resolve=None, dry_run=False, reporter=rep)

    progress_calls = [c[1:] for c in rep.calls if c[0] == "progress"]
    # The final forced call repeats (3, 3); drop it to check per-batch cadence separately.
    assert result.transferred_items == 3
    assert [t for _, t in progress_calls] == [3, 3, 3, 3]  # total is the whole pull
    assert [d for d, _ in progress_calls] == [1, 2, 3, 3]  # monotonic, forced final repeat


def test_pull_single_file_pathspec_downloads_only_that_file(
    tmp_path: Path, make_fake_drive
) -> None:
    """#96 follow-up: a pathspec naming a single FILE (not a directory) pulls exactly
    that file -- siblings and unrelated dirs stay untouched. Reported as 'pulling a
    single file locks up': the pre-fix code pulled the whole repo-wide backlog."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    for rel in ("a/wanted.bin", "a/other.bin", "b/unrelated.bin"):
        ctx.index.set(rel, _metadata_only_entry(f"/my-files/test/{rel}"))
    ctx.drive = make_fake_drive()

    result = pull(ctx, "a/wanted.bin", resolve=None, dry_run=False)

    assert result.transferred_items == 1
    downloaded = [p for call in ctx.drive.download_calls for p in call[0]]
    assert downloaded == ["/my-files/test/a/wanted.bin"]
    assert not (tmp_path / "a" / "other.bin").exists()
    assert not (tmp_path / "b").exists()
