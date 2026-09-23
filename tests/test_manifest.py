"""The remote manifest (#146): format, maintenance by push/rm, concurrency, readers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from protonfs import manifest
from protonfs.cli import main
from protonfs.commands.push import push
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import RemoteEntry
from protonfs.index import IndexEntry, IndexStore
from protonfs.manifest import Manifest, ManifestEntry, ManifestError, RemoteManifest

ROOT = "/my-files/test"
MANIFEST_DIR = f"{ROOT}/.protonfs"
MANIFEST_PATH = f"{MANIFEST_DIR}/manifest.json"


def _ctx(tmp_path: Path, make_fake_drive, *, enabled: bool = True, **fake_kwargs):
    init_config(tmp_path, ROOT)
    ctx = load_context(tmp_path)
    ctx.config.defaults.manifest = enabled
    ctx.drive = make_fake_drive(**fake_kwargs)
    return ctx


def _create(ctx, remote: dict | None = None) -> RemoteManifest:
    """Create the manifest the only supported way: rebuild from a (given) listing."""
    manifest.rebuild(ctx, remote or {}, None)
    handle = RemoteManifest.load(ctx)
    assert handle is not None
    return handle


def _remote_manifest(ctx) -> Manifest:
    return Manifest.from_bytes(ctx.drive._remote_content[MANIFEST_PATH])


# --- format ----------------------------------------------------------------------------


def test_manifest_round_trips_deterministically() -> None:
    doc = Manifest(
        generation=3,
        updated="t",
        updated_by="dev",
        entries={"b/x": ManifestEntry(4, "s256", "s1", "rev-1", "t"),
                 "a/y": ManifestEntry(1, "", "", "", "t")},
    )
    data = doc.to_bytes()

    assert Manifest.from_bytes(data) == doc
    assert data == Manifest.from_bytes(data).to_bytes()
    assert list(json.loads(data)["entries"]) == ["a/y", "b/x"]  # sorted


def test_a_newer_manifest_schema_is_refused_not_guessed_at() -> None:
    newer = json.dumps({"schema_version": 99, "generation": 1, "entries": {}}).encode()

    with pytest.raises(ManifestError, match="upgrade protonfs"):
        Manifest.from_bytes(newer)


@pytest.mark.parametrize("data", [b"not json", b"{}", b'{"schema_version": "1"}',
                                  b'{"schema_version": 1, "entries": {"a": {}}}'])
def test_a_malformed_manifest_is_a_manifest_error(data: bytes) -> None:
    with pytest.raises(ManifestError, match="malformed"):
        Manifest.from_bytes(data)


def test_control_paths_are_the_protonfs_directory_only() -> None:
    assert manifest.is_control_path(".protonfs")
    assert manifest.is_control_path(".protonfs/manifest.json")
    assert not manifest.is_control_path(".protonfsx/a")
    assert not manifest.is_control_path("run/.protonfs/a")


# --- creation and maintenance ----------------------------------------------------------


def test_load_returns_none_when_the_root_has_no_manifest(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)

    assert RemoteManifest.load(ctx) is None
    assert ctx.drive.download_calls == []  # nothing to download


def test_rebuild_creates_the_manifest_from_a_listing(tmp_path, make_fake_drive) -> None:
    from protonfs.localscan import hash_file_digests

    ctx = _ctx(tmp_path, make_fake_drive)
    held = tmp_path / "held.bin"
    held.write_bytes(b"data")
    sha256, sha1 = hash_file_digests(held)
    ctx.index.set("held.bin", IndexEntry(4, 0.0, sha256, sha1, f"{ROOT}/held.bin", "d",
                                         "present", "t"))
    remote = {
        "held.bin": RemoteEntry("held.bin", False, 9, claimed_size=4, sha1=sha1, revision="r1"),
        "other.bin": RemoteEntry("other.bin", False, 9, claimed_size=7, sha1="bb", revision="r2"),
        "unsized.bin": RemoteEntry("unsized.bin", False, 9),
    }

    generation, entries, skipped = manifest.rebuild(ctx, remote, None)

    assert (generation, entries, skipped) == (1, 2, ["unsized.bin"])
    written = _remote_manifest(ctx)
    assert written.entries["held.bin"].sha256 == sha256  # this host's index agreed
    assert written.entries["held.bin"].revision == "r1"
    assert written.entries["other.bin"].sha256 == ""  # never guessed
    assert "unsized.bin" not in written.entries  # the manifest only promises the verified
    assert ctx.drive.upload_calls[-1][1:] == (MANIFEST_DIR, "merge")  # a new revision


def test_push_records_each_verified_upload_with_its_drive_revision(
    tmp_path, make_fake_drive
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump").write_bytes(b"data")

    push(ctx, None, resolve=None, dry_run=False)

    written = _remote_manifest(ctx)
    entry = written.entries["run1/dump"]
    assert written.generation == 2
    assert (entry.size, entry.sha256, entry.sha1) == (
        4, ctx.index.get("run1/dump").sha256, ctx.index.get("run1/dump").sha1
    )
    assert entry.revision == ctx.drive.revision_uids[f"{ROOT}/run1/dump"]


def test_a_changed_file_is_recorded_at_its_new_revision(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    grow = tmp_path / "grow.ev"
    grow.write_bytes(b"line1\n")
    push(ctx, None, resolve=None, dry_run=False)
    first = _remote_manifest(ctx).entries["grow.ev"].revision

    grow.write_bytes(b"line1\nline2\n")
    push(ctx, None, resolve=None, dry_run=False)

    entry = _remote_manifest(ctx).entries["grow.ev"]
    assert entry.size == len(b"line1\nline2\n")
    assert entry.revision != first
    assert entry.revision == ctx.drive.revision_uids[f"{ROOT}/grow.ev"]


def test_push_never_records_an_upload_it_could_not_verify(tmp_path, make_fake_drive) -> None:
    # The manifest may lag Drive but never run ahead of it.
    ctx = _ctx(tmp_path, make_fake_drive, dropped_files={"lost"})
    _create(ctx)
    (tmp_path / "lost").write_bytes(b"data")
    (tmp_path / "kept").write_bytes(b"data")

    push(ctx, None, resolve=None, dry_run=False)

    assert set(_remote_manifest(ctx).entries) == {"kept"}


def test_push_does_not_start_a_manifest_mid_history(tmp_path, make_fake_drive) -> None:
    # A manifest begun by a push would look complete while missing everything uploaded
    # before it; only a full listing (verify --repair) may create one.
    ctx = _ctx(tmp_path, make_fake_drive)
    (tmp_path / "f").write_bytes(b"data")

    push(ctx, None, resolve=None, dry_run=False)

    assert MANIFEST_PATH not in ctx.drive._remote_content
    assert ctx.index.get("f") is not None  # the push itself is unaffected


def test_push_leaves_the_manifest_alone_when_the_repo_has_not_opted_in(
    tmp_path, make_fake_drive
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive, enabled=False)
    _create(ctx)
    ctx.drive.list_calls.clear()
    (tmp_path / "f").write_bytes(b"data")

    push(ctx, None, resolve=None, dry_run=False)

    assert ctx.drive.list_calls == []  # not even probed: opting out costs nothing
    assert _remote_manifest(ctx).entries == {}


def test_the_host_switch_turns_maintenance_off(tmp_path, make_fake_drive, monkeypatch) -> None:
    monkeypatch.setenv("PROTONFS_NO_MANIFEST", "1")
    ctx = _ctx(tmp_path, make_fake_drive)
    manifest.rebuild(ctx, {}, None)  # an explicit repair still works
    (tmp_path / "f").write_bytes(b"data")

    push(ctx, None, resolve=None, dry_run=False)

    assert _remote_manifest(ctx).entries == {}


def test_a_manifest_failure_never_fails_the_push(tmp_path, make_fake_drive, monkeypatch) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)

    def broken_load(_ctx):
        raise ManifestError("boom")

    monkeypatch.setattr(RemoteManifest, "load", classmethod(lambda cls, c: broken_load(c)))
    (tmp_path / "f").write_bytes(b"data")

    result = push(ctx, None, resolve=None, dry_run=False)

    assert result.transferred_items == 1 and result.failed_items == 0


def test_rm_drops_the_path_and_everything_under_it(tmp_path, make_fake_drive) -> None:
    from protonfs.commands.rm import rm

    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "a").write_bytes(b"a")
    (tmp_path / "run1" / "b").write_bytes(b"b")
    (tmp_path / "keep").write_bytes(b"k")
    push(ctx, None, resolve=None, dry_run=False)

    rm(ctx, "run1", recursive=True, force=False, confirmed=True)

    assert set(_remote_manifest(ctx).entries) == {"keep"}


def test_cli_push_writes_the_manifest_once_for_several_paths(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f").write_bytes(name.encode())
    before = sum(1 for call in ctx.drive.upload_calls if call[1] == MANIFEST_DIR)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["push", "a", "b", "c"])

    assert result.exit_code == 0, result.output
    writes = sum(1 for call in ctx.drive.upload_calls if call[1] == MANIFEST_DIR) - before
    assert writes == 1
    assert set(_remote_manifest(ctx).entries) == {"a/f", "b/f", "c/f"}


# --- concurrency ------------------------------------------------------------------------


def _other_host_writes(ctx, rel: str) -> None:
    """Simulate another host recording `rel` directly on Drive."""
    theirs = RemoteManifest.load(ctx)
    theirs.record(rel, ManifestEntry(1, "", "", "", "t"))
    theirs.save()


def test_a_write_replays_onto_a_manifest_another_host_changed(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    ours = RemoteManifest.load(ctx)
    ours.record("ours", ManifestEntry(1, "", "", "", "t"))
    _other_host_writes(ctx, "theirs")  # lands between our read and our write

    generation = ours.save()

    written = _remote_manifest(ctx)
    assert set(written.entries) == {"ours", "theirs"}  # nothing lost
    assert generation == written.generation == 3
    assert ours.replayed


def test_a_write_overtaken_after_upload_is_replayed(tmp_path, make_fake_drive) -> None:
    # Another host's write lands on top of ours before we confirm it: the post-write
    # check sees a revision that is not ours, re-reads it and replays.
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    fake = ctx.drive
    real_upload = fake.upload
    state = {"interfered": False}

    def upload_then_interfere(paths, parent, file_strategy=None, folder_strategy=None):
        result = real_upload(paths, parent, file_strategy, folder_strategy)
        if parent == MANIFEST_DIR and not state["interfered"]:
            state["interfered"] = True
            theirs = Manifest.from_bytes(fake._remote_content[MANIFEST_PATH])
            theirs.entries.pop("ours", None)  # their copy was read before our write
            theirs.entries["theirs"] = ManifestEntry(1, "", "", "", "t")
            theirs.generation += 1
            tmp = Path(paths[0]).with_name("theirs") / "manifest.json"
            tmp.parent.mkdir()
            tmp.write_bytes(theirs.to_bytes())
            real_upload([tmp], parent, "merge")
        return result

    fake.upload = upload_then_interfere
    ours = RemoteManifest.load(ctx)
    ours.record("ours", ManifestEntry(1, "", "", "", "t"))

    ours.save()

    assert set(_remote_manifest(ctx).entries) == {"ours", "theirs"}


def test_a_manifest_removed_since_it_was_read_is_not_recreated(
    tmp_path, make_fake_drive
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    ours = RemoteManifest.load(ctx)
    ours.record("x", ManifestEntry(1, "", "", "", "t"))
    del ctx.drive._remote_files[MANIFEST_DIR]["manifest.json"]

    with pytest.raises(ManifestError, match="removed"):
        ours.save()


def test_a_torn_read_is_refused(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    ctx.drive._remote_content[MANIFEST_PATH] = b'{"schema_version": 1}'  # != listed size

    with pytest.raises(ManifestError, match="changed while being read"):
        RemoteManifest.load(ctx)


# --- the index's reconciled generation ---------------------------------------------------


def test_the_index_records_generation_only_when_it_was_current(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    handle = _create(ctx)
    ctx.index.set_manifest_state(handle.manifest.generation, handle.revision)
    (tmp_path / "f").write_bytes(b"data")

    push(ctx, None, resolve=None, dry_run=False)

    written = RemoteManifest.load(ctx)
    assert IndexStore(tmp_path).manifest_state == {
        "generation": written.manifest.generation, "revision": written.revision
    }

    # Another host writes; our next push must not claim to be current with their write.
    _other_host_writes(ctx, "theirs")
    (tmp_path / "g").write_bytes(b"more")
    push(ctx, None, resolve=None, dry_run=False)

    assert IndexStore(tmp_path).manifest_state["generation"] == written.manifest.generation


def test_index_manifest_state_survives_save_and_is_optional(tmp_path) -> None:
    store = IndexStore(tmp_path)
    assert store.manifest_state is None
    store.set_manifest_state(7, "rev-x")
    store.save()

    assert IndexStore(tmp_path).manifest_state == {"generation": 7, "revision": "rev-x"}
    # an older protonfs rewrites the index without the key: it reads as never reconciled
    doc = json.loads((tmp_path / ".protonfs" / "index.json").read_text())
    del doc["manifest"]
    (tmp_path / ".protonfs" / "index.json").write_text(json.dumps(doc))
    assert IndexStore(tmp_path).manifest_state is None


def test_refresh_records_the_generation_and_never_seeds_the_manifest(
    tmp_path, make_fake_drive
) -> None:
    from protonfs.commands.refresh import refresh

    ctx = _ctx(tmp_path, make_fake_drive)
    handle = _create(ctx)
    ctx.drive._walk_entries = [
        RemoteEntry(".protonfs/manifest.json", False, 10, claimed_size=10),
        RemoteEntry("data.bin", False, 10, claimed_size=4),
    ]

    refresh(ctx, None, prune=False)

    assert ctx.index.get(".protonfs/manifest.json") is None
    assert ctx.index.get("data.bin") is not None
    assert ctx.index.manifest_state == {
        "generation": handle.manifest.generation, "revision": handle.revision
    }


def test_classify_ignores_control_paths_an_older_host_indexed(tmp_path) -> None:
    from protonfs.diff import classify

    index = IndexStore(tmp_path)
    index.set(".protonfs/manifest.json", IndexEntry(1, 0.0, "", "", "x", "d", "metadata-only", ""))

    remote = {".protonfs/manifest.json": RemoteEntry(".protonfs/manifest.json", False, 1)}

    assert classify({}, index, remote) == []
    assert classify({}, index) == []


# --- readers ------------------------------------------------------------------------------


def test_pull_on_an_empty_index_seeds_from_the_manifest(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    # Another host pushed; this is a fresh clone with an empty index.
    other = tmp_path / "other"
    other.mkdir()
    ctx = _ctx(other, make_fake_drive)
    _create(ctx)
    (other / "run1").mkdir()
    (other / "run1" / "dump").write_bytes(b"payload")
    push(ctx, None, resolve=None, dry_run=False)

    clone = tmp_path / "clone"
    clone.mkdir()
    init_config(clone, ROOT)
    clone_ctx = load_context(clone)
    clone_ctx.drive = ctx.drive
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: clone_ctx)

    result = CliRunner().invoke(main, ["pull"])

    assert result.exit_code == 0, result.output
    assert "seeded 1 file(s) from the remote manifest" in result.output
    assert "does not list are not" in result.output  # honest about what it may miss
    assert (clone / "run1" / "dump").read_bytes() == b"payload"


def test_pull_on_an_empty_index_without_a_manifest_keeps_the_old_message(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["pull"])

    assert result.exit_code == 0
    assert "run `protonfs refresh` first" in result.output


def test_pull_notes_when_another_host_changed_the_manifest(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    handle = _create(ctx)
    ctx.index.set_manifest_state(handle.manifest.generation, handle.revision)
    ctx.index.set("x", IndexEntry(1, 0.0, "h", "", f"{ROOT}/x", "d", "present", "t"))
    (tmp_path / "x").write_bytes(b"x")
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    quiet = CliRunner().invoke(main, ["pull"])
    _other_host_writes(ctx, "theirs")
    noted = CliRunner().invoke(main, ["pull"])

    assert "manifest has changed" not in quiet.output
    assert "manifest has changed" in noted.output


def test_offload_never_reads_the_manifest(tmp_path, make_fake_drive) -> None:
    # Deletion is the one operation with no undo: it verifies live, whatever the
    # manifest claims.
    from protonfs.commands.offload import offload

    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    (tmp_path / "f").write_bytes(b"data")
    push(ctx, None, resolve=None, dry_run=False)
    ctx.drive.download_calls.clear()
    ctx.drive.list_calls.clear()

    offload(ctx, None, verify=True)

    assert ctx.drive.download_calls == []
    assert ctx.drive.list_calls == []  # remote_identities of the file's parent only
    assert ctx.drive.identity_calls[-1] == ROOT


# --- verify --------------------------------------------------------------------------------


def test_compare_sorts_every_disagreement() -> None:
    doc = Manifest(entries={
        "same": ManifestEntry(1, "", "aa", "r1", "t"),
        "moved": ManifestEntry(1, "", "aa", "r1", "t"),
        "gone": ManifestEntry(1, "", "", "", "t"),
        "short": ManifestEntry(9, "", "", "", "t"),
        "other": ManifestEntry(1, "", "aa", "", "t"),
        "blind": ManifestEntry(1, "", "", "", "t"),
    })
    remote = {
        "same": RemoteEntry("same", False, 1, claimed_size=1, sha1="aa", revision="r1"),
        "moved": RemoteEntry("moved", False, 1, claimed_size=1, sha1="aa", revision="r2"),
        "short": RemoteEntry("short", False, 1, claimed_size=4),
        "other": RemoteEntry("other", False, 1, claimed_size=1, sha1="bb"),
        "blind": RemoteEntry("blind", False, 1),
        "new": RemoteEntry("new", False, 1, claimed_size=1),
    }

    report = manifest.compare(doc, remote)

    assert report.missing == ["gone"]
    assert report.differs == ["other", "short"]
    assert report.revision_moved == ["moved"]
    assert report.unsized == ["blind"]
    assert report.untracked == ["new"]
    assert report.faults == 3


def test_cli_verify_exits_1_on_faults_and_repair_fixes_them(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    handle = _create(ctx)
    handle.record("gone", ManifestEntry(1, "", "", "", "t"))
    handle.save()
    ctx.drive._walk_entries = [RemoteEntry("here", False, 3, claimed_size=3, sha1="cc")]
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    checked = CliRunner().invoke(main, ["verify"])
    repaired = CliRunner().invoke(main, ["verify", "--repair"])
    rechecked = CliRunner().invoke(main, ["verify"])

    assert checked.exit_code == 1
    assert "missing (in the manifest, not on Drive): 1" in checked.output
    assert "untracked (on Drive, not in the manifest): 1" in checked.output
    assert repaired.exit_code == 0, repaired.output
    assert set(_remote_manifest(ctx).entries) == {"here"}
    assert rechecked.exit_code == 0


def test_cli_verify_without_a_manifest_explains_how_to_build_one(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive, enabled=False)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    checked = CliRunner().invoke(main, ["verify"])
    built = CliRunner().invoke(main, ["verify", "--repair"])

    assert checked.exit_code == 0
    assert "none on the remote" in checked.output and "--repair" in checked.output
    assert built.exit_code == 0, built.output
    assert "defaults.manifest is off" in built.output  # it will not be kept current
    assert _remote_manifest(ctx).generation == 1


def test_manifest_config_key_and_env_override(tmp_path, monkeypatch) -> None:
    from protonfs.commands.config import config_get, config_set

    init_config(tmp_path, ROOT)
    assert load_context(tmp_path).config.defaults.manifest is False
    config_set(tmp_path, "defaults.manifest", "true")
    assert load_context(tmp_path).config.defaults.manifest is True
    assert config_get(tmp_path, "defaults.manifest") == "True"
    monkeypatch.setenv("PROTONFS_MANIFEST", "0")
    assert load_context(tmp_path).config.defaults.manifest is False


# --- edges ---------------------------------------------------------------------------------


def _raise_manifest_error(cls, _ctx):
    raise ManifestError("unreadable")


def test_a_manifest_download_that_does_not_land_is_an_error(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive, download_dropped_files={"manifest.json"})
    manifest.rebuild(ctx, {}, None)

    with pytest.raises(ManifestError, match="did not land"):
        RemoteManifest.load(ctx)


def test_a_manifest_whose_digest_disagrees_with_its_listing_is_refused(
    tmp_path, make_fake_drive
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    ctx.drive._remote_sha1[MANIFEST_DIR]["manifest.json"] = "0" * 40

    with pytest.raises(ManifestError, match="sha1"):
        RemoteManifest.load(ctx)


def test_saving_nothing_writes_nothing(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    handle = _create(ctx)
    uploads = len(ctx.drive.upload_calls)

    assert handle.save() == handle.manifest.generation
    assert len(ctx.drive.upload_calls) == uploads


def test_a_failed_manifest_upload_is_a_manifest_error(tmp_path, make_fake_drive) -> None:
    from protonfs.drive import TransferResult

    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    handle = RemoteManifest.load(ctx)
    handle.record("x", ManifestEntry(1, "", "", "", "t"))
    ctx.drive._upload_result = TransferResult(
        0, 0, 1, [{"name": "manifest.json", "error": "quota exceeded"}]
    )

    with pytest.raises(ManifestError, match="quota exceeded"):
        handle.save()


def test_a_writer_that_is_overtaken_every_time_gives_up(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    fake = ctx.drive
    real_upload = fake.upload

    def always_overtaken(paths, parent, file_strategy=None, folder_strategy=None):
        result = real_upload(paths, parent, file_strategy, folder_strategy)
        if parent == MANIFEST_DIR and "theirs" not in str(paths[0]):
            # another host writes its own revision on top, every single time
            theirs = Manifest.from_bytes(fake._remote_content[MANIFEST_PATH])
            theirs.generation += 1
            theirs.updated_by = "other"
            tmp = Path(paths[0]).parent / "theirs" / "manifest.json"
            tmp.parent.mkdir(exist_ok=True)
            tmp.write_bytes(theirs.to_bytes())
            real_upload([tmp], parent, "merge")
        return result

    fake.upload = always_overtaken
    handle = RemoteManifest.load(ctx)
    handle.record("x", ManifestEntry(1, "", "", "", "t"))

    with pytest.raises(ManifestError, match="attempts"):
        handle.save()


def test_a_replayed_removal_stays_removed(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    handle = _create(ctx)
    handle.record("dir/a", ManifestEntry(1, "", "", "", "t"))
    handle.save()
    ours = RemoteManifest.load(ctx)
    ours.forget("dir")
    _other_host_writes(ctx, "theirs")

    ours.save()

    assert set(_remote_manifest(ctx).entries) == {"theirs"}


def test_rebuild_keeps_a_sha256_another_host_recorded_and_skips_a_no_op(
    tmp_path, make_fake_drive
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    handle = _create(ctx)
    handle.record("f", ManifestEntry(4, "their-sha256", "aa", "r1", "t"))
    handle.save()
    remote = {"f": RemoteEntry("f", False, 9, claimed_size=4, sha1="aa", revision="r1")}
    uploads = len(ctx.drive.upload_calls)

    generation, entries, _ = manifest.rebuild(ctx, remote, RemoteManifest.load(ctx))

    assert _remote_manifest(ctx).entries["f"].sha256 == "their-sha256"
    assert (generation, entries) == (_remote_manifest(ctx).generation, 1)
    assert len(ctx.drive.upload_calls) == uploads  # nothing changed, nothing written


def test_seed_index_is_off_under_the_host_switch_and_survives_errors(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    monkeypatch.setenv("PROTONFS_NO_MANIFEST", "yes")
    assert manifest.seed_index(ctx) is None
    monkeypatch.delenv("PROTONFS_NO_MANIFEST")

    monkeypatch.setattr(RemoteManifest, "load", classmethod(_raise_manifest_error))
    assert manifest.seed_index(ctx) is None


def test_seed_index_never_overwrites_or_seeds_control_paths(tmp_path, make_fake_drive) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    handle = _create(ctx)
    handle.record("known", ManifestEntry(4, "", "", "", "t"))
    handle.record("fresh", ManifestEntry(5, "s", "", "", "t"))
    handle.record(".protonfs/stray", ManifestEntry(1, "", "", "", "t"))
    handle.save()
    ctx.index.set("known", IndexEntry(4, 1.0, "mine", "", f"{ROOT}/known", "d", "present", "t"))

    seeded, _generation = manifest.seed_index(ctx)

    assert seeded == 1
    assert ctx.index.get("known").sha256 == "mine"
    assert ctx.index.get("fresh").local_state == "metadata-only"
    assert ctx.index.get(".protonfs/stray") is None


def test_staleness_note_is_silent_when_the_check_fails(tmp_path, make_fake_drive) -> None:
    from protonfs.drive import DriveError

    ctx = _ctx(tmp_path, make_fake_drive)
    ctx.index.set_manifest_state(1, "rev-old")

    def failing_list(*_a, **_k):
        raise DriveError("throttled")

    ctx.drive.list_with_backoff = failing_list

    assert manifest.staleness_note(ctx) is None


def test_refresh_records_nothing_when_the_manifest_cannot_be_read(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    from protonfs.commands.refresh import refresh

    ctx = _ctx(tmp_path, make_fake_drive)
    refresh(ctx, None, prune=False)  # no manifest at all
    assert ctx.index.manifest_state is None

    _create(ctx)
    monkeypatch.setattr(RemoteManifest, "load", classmethod(_raise_manifest_error))
    refresh(ctx, None, prune=False)
    assert ctx.index.manifest_state is None


def test_cli_verify_reports_a_current_index_long_lists_and_unsized_files(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    handle = _create(ctx)
    ctx.index.set_manifest_state(handle.manifest.generation, handle.revision)
    ctx.drive._walk_entries = [
        RemoteEntry(f"f{i:02d}", False, 1, claimed_size=1) for i in range(25)
    ] + [RemoteEntry("blind", False, 1)]
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    checked = CliRunner().invoke(main, ["verify"])
    repaired = CliRunner().invoke(main, ["verify", "--repair"])

    assert "(current)" in checked.output
    assert "untracked (on Drive, not in the manifest): 26" in checked.output
    assert "... and 6 more" in checked.output
    assert "left out 1 file(s)" in repaired.output


def test_cli_verify_turns_an_unreadable_manifest_into_a_clean_error(
    tmp_path, make_fake_drive, monkeypatch
) -> None:
    ctx = _ctx(tmp_path, make_fake_drive)
    _create(ctx)
    size = len(ctx.drive._remote_content[MANIFEST_PATH])
    ctx.drive._remote_content[MANIFEST_PATH] = b"x" * size
    ctx.drive._remote_sha1[MANIFEST_DIR].pop("manifest.json")
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)

    result = CliRunner().invoke(main, ["verify"])

    assert result.exit_code == 1
    assert "malformed" in result.output
    assert "Traceback" not in result.output


def test_revision_uid_reads_both_listing_shapes() -> None:
    from protonfs.drive import revision_uid

    assert revision_uid({"activeRevision": {"uid": "r1", "claimedSize": 1}}) == "r1"
    assert revision_uid({"activeRevision": {"ok": True, "value": {"uid": "r2"}}}) == "r2"
    assert revision_uid({"activeRevision": {"ok": False, "value": None}}) is None
    assert revision_uid({}) is None
    assert revision_uid({"activeRevision": {"uid": ""}}) is None


def test_offload_never_touches_the_control_directory(tmp_path, make_fake_drive) -> None:
    # An older host could have pulled the remote manifest down as if it were data.
    from protonfs.commands.offload import offload

    ctx = _ctx(tmp_path, make_fake_drive)
    from protonfs.localscan import hash_file_digests

    stray = tmp_path / ".protonfs" / "manifest.json"
    stray.write_bytes(b"{}")
    sha256, sha1 = hash_file_digests(stray)  # so only the control-path guard can stop it
    ctx.index.set(".protonfs/manifest.json",
                  IndexEntry(2, 0.0, sha256, sha1, MANIFEST_PATH, "d", "present", "t"))

    result = offload(ctx, None, verify=False)

    assert result.offloaded == 0 and result.skipped_modified == 0
    assert stray.exists()
