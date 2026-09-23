"""The remote manifest: one JSON object per synced root, recording what protonfs verified.

``<remote_root>/.protonfs/manifest.json`` lists every file protonfs has uploaded (or
adopted) and verified on Drive: its plaintext size, sha256, sha1 and the uid of the
Drive revision that was verified. Reading it is one small download instead of a walk of
the whole tree, so a host can learn what is on the remote, and whether its own view is
current, cheaply (#146).

The manifest is a **cache, not an authority**. Changes made outside protonfs (the web
UI, another client) are invisible to it until something walks the remote, so:

- It lags reality and never leads it. An entry is written only after the upload it
  describes was verified against a live listing, and the manifest is never *created*
  from anything but a full listing (:func:`rebuild`, i.e. ``protonfs verify --repair``).
  Push and rm only update a manifest that already exists.
- Nothing destructive trusts it. ``offload`` keeps its own live verification and never
  reads the manifest; a missing or stale manifest can only make a read slower or less
  complete, never delete data.

Concurrency. Several hosts may write the same manifest, and ``locking.repo_lock`` only
serialises processes on one machine. Each write therefore checks the manifest's Drive
revision uid first (a changed uid means another host wrote since this one read) and
replays its own changes onto the newer copy. The upload is a new revision of the
manifest node (``-f merge``), so Drive's version history keeps every earlier one. After
uploading, the listing is read again to confirm the active revision is the one just
written; if another host's write landed on top, the loop re-reads and replays. The
proton-drive CLI offers no conditional write, so a narrow window remains in which a
concurrent write can drop another host's changes from the newest revision. The result
is a manifest that is behind (an entry missing, which is tolerated and repaired by
``verify --repair``), or, for a lost removal, an entry naming a file that is gone,
which ``verify`` reports and every reader already treats as possibly stale.

.. versionadded:: 2.1.0
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from protonfs.context import RepoContext
from protonfs.drive import DriveError, RemoteEntry, RemoteIdentity, decrypted_name

logger = logging.getLogger(__name__)

REMOTE_CONTROL_DIR = ".protonfs"
MANIFEST_FILE_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1
# Write attempts before giving up on a manifest another host keeps changing under us.
SAVE_ATTEMPTS = 3
# Environment switch that disables every manifest read and write on this host, whatever
# the repo config says -- an escape hatch if the manifest itself misbehaves.
DISABLE_ENV = "PROTONFS_NO_MANIFEST"


class ManifestError(RuntimeError):
    """The manifest could not be read or written consistently (never fatal to a sync)."""


def is_control_path(rel_path: str) -> bool:
    """Whether a repo-relative path lies in protonfs's own control directory.

    ``.protonfs/`` holds protonfs state on both sides (the local index and config, the
    remote manifest). It is never synced as data, so every listing consumer drops it.
    """
    return rel_path == REMOTE_CONTROL_DIR or rel_path.startswith(f"{REMOTE_CONTROL_DIR}/")


def disabled() -> bool:
    """Whether ``$PROTONFS_NO_MANIFEST`` turns the manifest off on this host."""
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def maintenance_enabled(ctx: RepoContext) -> bool:
    """Whether push/rm should keep the manifest current: ``defaults.manifest`` is on and
    the host has not disabled it."""
    return bool(ctx.config.defaults.manifest) and not disabled()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class ManifestEntry:
    """One verified remote file.

    :ivar size: plaintext size in bytes (Drive's ``claimedSize``).
    :ivar sha256: protonfs's content digest, ``""`` when never computed by any host
        (e.g. an entry rebuilt from a listing for a file this host never held).
    :ivar sha1: proton's plaintext ``claimedDigests.sha1``.
    :ivar revision: uid of the Drive revision that was verified, ``""`` if unknown.
    :ivar recorded: ISO-8601 time this entry was written.
    """

    size: int
    sha256: str
    sha1: str
    revision: str
    recorded: str

    @classmethod
    def from_dict(cls, data: dict) -> ManifestEntry:
        """Build an entry from its JSON form, tolerating unknown extra keys."""
        return cls(
            size=int(data["size"]),
            sha256=str(data.get("sha256") or ""),
            sha1=str(data.get("sha1") or ""),
            revision=str(data.get("revision") or ""),
            recorded=str(data.get("recorded") or ""),
        )


@dataclass
class Manifest:
    """The manifest document.

    :ivar generation: incremented on every successful write.
    :ivar updated: ISO-8601 time of the last write.
    :ivar updated_by: ``device_id`` of the host that wrote it.
    :ivar entries: ``{repo-relative path: ManifestEntry}``.
    """

    generation: int = 0
    updated: str = ""
    updated_by: str = ""
    entries: dict[str, ManifestEntry] = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        """Serialise deterministically (sorted keys), so equal manifests hash equally."""
        document = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "generation": self.generation,
            "updated": self.updated,
            "updated_by": self.updated_by,
            "entries": {rel: asdict(e) for rel, e in sorted(self.entries.items())},
        }
        return (json.dumps(document, indent=1, sort_keys=True) + "\n").encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> Manifest:
        """Parse a manifest.

        :raises ManifestError: when it is malformed, or a newer schema than this build
            understands -- never guessed at, and never overwritten, because rewriting a
            newer manifest with an older shape would drop what the newer build recorded.
        """
        try:
            raw = json.loads(data.decode())
            version = raw["schema_version"]
            if not isinstance(version, int):
                raise TypeError("schema_version is not an integer")
            if version > MANIFEST_SCHEMA_VERSION:
                raise ManifestError(
                    f"the remote manifest is schema v{version}, but this protonfs "
                    f"understands up to v{MANIFEST_SCHEMA_VERSION}; upgrade protonfs"
                )
            return cls(
                generation=int(raw.get("generation", 0)),
                updated=str(raw.get("updated", "")),
                updated_by=str(raw.get("updated_by", "")),
                entries={
                    rel: ManifestEntry.from_dict(e)
                    for rel, e in (raw.get("entries") or {}).items()
                },
            )
        except ManifestError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ManifestError(f"the remote manifest is malformed: {exc}") from exc


def _remote_root(ctx: RepoContext) -> str:
    return ctx.config.remote_root.rstrip("/")


def _manifest_dir(ctx: RepoContext) -> str:
    return f"{_remote_root(ctx)}/{REMOTE_CONTROL_DIR}"


def probe(ctx: RepoContext) -> RemoteIdentity | None:
    """The manifest's remote identity (size, sha1, revision uid), or ``None`` if absent.

    Two small listings -- the remote root, then its ``.protonfs`` folder -- and no
    download. The root is listed first because listing a path that does not exist is
    not a reliable "absent" signal from proton-drive.
    """
    root = _remote_root(ctx)
    has_dir = any(
        entry.get("type") == "folder" and decrypted_name(entry) == REMOTE_CONTROL_DIR
        for entry in ctx.drive.list_with_backoff(root)
    )
    if not has_dir:
        return None
    return ctx.drive.remote_identities(_manifest_dir(ctx)).get(MANIFEST_FILE_NAME)


def _download(ctx: RepoContext, ident: RemoteIdentity) -> Manifest:
    """Download and parse the manifest, checking the bytes against its listing."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx.drive.download(
            [f"{_manifest_dir(ctx)}/{MANIFEST_FILE_NAME}"], Path(tmp), file_strategy="replace"
        )
        landed = Path(tmp) / MANIFEST_FILE_NAME
        if not landed.exists():
            raise ManifestError("the manifest download did not land")
        data = landed.read_bytes()
    if ident.claimed_size is not None and ident.claimed_size != len(data):
        raise ManifestError(
            f"the manifest download is {len(data)} bytes but Drive lists "
            f"{ident.claimed_size}; it changed while being read"
        )
    if ident.sha1 and hashlib.sha1(data).hexdigest() != ident.sha1:
        raise ManifestError("the manifest download does not match its listed sha1")
    return Manifest.from_bytes(data)


class RemoteManifest:
    """A manifest read from Drive, the revision it was read at, and pending changes.

    Build one with :meth:`load` (an existing manifest) or :meth:`empty` (only for
    :func:`rebuild`). Changes are queued with :meth:`record`/:meth:`forget` and written
    by :meth:`save`, which replays them onto a newer copy if another host wrote first.
    """

    def __init__(self, ctx: RepoContext, manifest: Manifest, revision: str | None) -> None:
        """Wrap ``manifest`` as read at Drive revision ``revision`` (``None``: not on Drive)."""
        self._ctx = ctx
        self.manifest = manifest
        self.revision = revision
        self.loaded_generation = manifest.generation
        self.loaded_revision = revision
        # True once a save had to replay onto a copy another host wrote in between.
        self.replayed = False
        self._changes: list[tuple[str, str, ManifestEntry | None]] = []

    @classmethod
    def load(cls, ctx: RepoContext) -> RemoteManifest | None:
        """Read the remote manifest, or return ``None`` when the root has none.

        :raises ManifestError: when it exists but cannot be read consistently.
        :raises protonfs.drive.DriveError: on a Drive failure.
        """
        ident = probe(ctx)
        if ident is None:
            return None
        return cls(ctx, _download(ctx, ident), ident.revision)

    @classmethod
    def empty(cls, ctx: RepoContext) -> RemoteManifest:
        """A new, unsaved manifest (used only when rebuilding from a full listing)."""
        return cls(ctx, Manifest(), None)

    @property
    def dirty(self) -> bool:
        """Whether changes are queued for the next :meth:`save`."""
        return bool(self._changes)

    def record(self, rel_path: str, entry: ManifestEntry) -> None:
        """Queue ``rel_path`` as verified on Drive with ``entry``."""
        self._changes.append(("set", rel_path, entry))
        self.manifest.entries[rel_path] = entry

    def forget(self, rel_path: str) -> None:
        """Queue the removal of ``rel_path`` and everything beneath it."""
        self._changes.append(("forget", rel_path, None))
        _apply_forget(self.manifest, rel_path)

    def save(self, *, force: bool = False) -> int:
        """Write the queued changes as a new manifest revision; returns the generation.

        :param force: write even with nothing queued (creating an empty manifest).
        :raises ManifestError: when the manifest vanished since it was read (it is never
            recreated partially), or another host kept writing for every attempt.
        :raises protonfs.drive.DriveError: on a Drive failure.
        """
        if not self._changes and not force:
            return self.manifest.generation
        ctx = self._ctx
        for _ in range(SAVE_ATTEMPTS):
            current = probe(ctx)
            current_rev = current.revision if current is not None else None
            if current is None and self.revision is not None:
                raise ManifestError(
                    "the remote manifest was removed since it was read; not recreating a "
                    "partial one (run `protonfs verify --repair` to rebuild it)"
                )
            if current is not None and current_rev != self.revision:
                self._replay_onto(_download(ctx, current))
                self.revision = current_rev
            written = Manifest(
                generation=self.manifest.generation + 1,
                updated=_now(),
                updated_by=ctx.config.device_id,
                entries=dict(self.manifest.entries),
            )
            data = written.to_bytes()
            if current is None:
                try:
                    ctx.drive.create_folder(_remote_root(ctx), REMOTE_CONTROL_DIR)
                except DriveError:
                    pass  # already exists -- a real failure surfaces on the upload below
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / MANIFEST_FILE_NAME
                path.write_bytes(data)
                result = ctx.drive.upload([path], _manifest_dir(ctx), file_strategy="merge")
            if result.failures:
                raise ManifestError(
                    f"uploading the manifest failed: {result.failures[0].get('error')}"
                )
            after = ctx.drive.remote_identities(_manifest_dir(ctx)).get(MANIFEST_FILE_NAME)
            if (
                after is not None
                and after.claimed_size == len(data)
                and (not after.sha1 or after.sha1 == hashlib.sha1(data).hexdigest())
            ):
                self.manifest = written
                self.revision = after.revision
                self._changes.clear()
                return written.generation
            # Another host's revision landed on top of ours (or ours never landed): go
            # round again, which re-reads it and replays our changes.
            logger.info("manifest write was overtaken by another writer; retrying")
        raise ManifestError(
            f"could not write the manifest in {SAVE_ATTEMPTS} attempts: another host kept "
            "changing it; it will be behind until the next write or `verify --repair`"
        )

    def _replay_onto(self, fresh: Manifest) -> None:
        """Adopt ``fresh`` (another host's newer manifest) and re-apply our changes."""
        for op, rel, entry in self._changes:
            if op == "set":
                fresh.entries[rel] = entry
            else:
                _apply_forget(fresh, rel)
        self.manifest = fresh
        self.replayed = True


def _apply_forget(manifest: Manifest, rel_path: str) -> None:
    prefix = f"{rel_path}/"
    for rel in [r for r in manifest.entries if r == rel_path or r.startswith(prefix)]:
        del manifest.entries[rel]


def update(
    ctx: RepoContext,
    *,
    record: dict[str, ManifestEntry] | None = None,
    forget: list[str] | None = None,
    reporter=None,
) -> int | None:
    """Apply verified changes to the remote manifest, if this repo maintains one.

    A no-op when maintenance is off (``defaults.manifest`` / ``$PROTONFS_NO_MANIFEST``)
    or there is nothing to change. Only an existing manifest is updated: without one this
    warns and writes nothing, because a manifest started mid-history would look complete
    while missing everything uploaded before it.

    Never raises: the manifest is an optimisation, so a failure to maintain it is
    reported and the calling command's own result stands. When the index was current
    with the manifest this read, and no other host wrote in between, the index is marked
    current with the new generation too (saved by the caller's next index save).

    :returns: the new generation, or ``None`` when nothing was written.
    """
    if not maintenance_enabled(ctx) or not (record or forget):
        return None
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
    try:
        handle = RemoteManifest.load(ctx)
        if handle is None:
            reporter.warn(
                "defaults.manifest is on but the remote has no manifest, so none was "
                "written; run `protonfs verify --repair` once to build it from a listing"
            )
            return None
        for rel in forget or []:
            handle.forget(rel)
        for rel, entry in (record or {}).items():
            handle.record(rel, entry)
        was_current = _index_current(ctx, handle.loaded_generation, handle.loaded_revision)
        generation = handle.save()
    except (ManifestError, DriveError) as exc:
        reporter.warn(f"the remote manifest was not updated: {exc}")
        return None
    if was_current and not handle.replayed:
        ctx.index.set_manifest_state(generation, handle.revision or "")
    return generation


def _index_current(ctx: RepoContext, generation: int, revision: str | None) -> bool:
    state = ctx.index.manifest_state
    return (
        state is not None
        and state["generation"] == generation
        and state["revision"] == (revision or "")
    )


def entry_for_index(index_entry, revision: str | None, recorded: str) -> ManifestEntry:
    """A manifest entry for a file push just verified, from its index entry."""
    return ManifestEntry(
        size=index_entry.size,
        sha256=index_entry.sha256,
        sha1=index_entry.sha1,
        revision=revision or "",
        recorded=recorded,
    )


@dataclass
class VerifyReport:
    """The outcome of comparing the manifest with a full remote listing.

    :ivar manifest: the manifest read (``None`` when the root has none).
    :ivar remote_files: number of files in the listing.
    :ivar missing: in the manifest, absent from Drive (a fault).
    :ivar differs: in both, but size or sha1 disagree (a fault).
    :ivar untracked: on Drive, absent from the manifest (tolerated: the manifest lags).
    :ivar revision_moved: same content, but Drive now holds a different revision.
    :ivar unsized: listed without a plaintext size, so not comparable.
    """

    manifest: Manifest | None
    remote_files: int = 0
    missing: list[str] = field(default_factory=list)
    differs: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    revision_moved: list[str] = field(default_factory=list)
    unsized: list[str] = field(default_factory=list)

    @property
    def faults(self) -> int:
        """Entries the manifest promises that Drive contradicts."""
        return len(self.missing) + len(self.differs)


def remote_listing(ctx: RepoContext) -> dict[str, RemoteEntry]:
    """Every data file under the remote root (control files excluded), by rel path."""
    return {
        e.rel_path: e
        for e in ctx.drive.walk(_remote_root(ctx))
        if not e.is_dir and not is_control_path(e.rel_path)
    }


def compare(manifest: Manifest | None, remote: dict[str, RemoteEntry]) -> VerifyReport:
    """Compare ``manifest`` with a full listing (pure; no Drive calls)."""
    report = VerifyReport(manifest=manifest, remote_files=len(remote))
    if manifest is None:
        return report
    for rel, entry in sorted(manifest.entries.items()):
        found = remote.get(rel)
        if found is None:
            report.missing.append(rel)
        elif found.claimed_size is None:
            report.unsized.append(rel)
        elif found.claimed_size != entry.size or (
            found.sha1 and entry.sha1 and found.sha1 != entry.sha1
        ):
            report.differs.append(rel)
        elif entry.revision and found.revision and found.revision != entry.revision:
            report.revision_moved.append(rel)
    report.untracked = sorted(set(remote) - set(manifest.entries))
    return report


def rebuild(
    ctx: RepoContext, remote: dict[str, RemoteEntry], handle: RemoteManifest | None
) -> tuple[int, int, list[str]]:
    """Rewrite the manifest to match a full listing (``protonfs verify --repair``).

    Every listed file with a plaintext size gets an entry carrying Drive's size, sha1 and
    revision. The sha256 is taken from this host's index only when the index entry agrees
    with the listing (same size, and same sha1 where both are known); otherwise it is left
    unknown rather than guessed. Files listed without a plaintext size are left out:
    the manifest only promises what was verified.

    :returns: ``(generation, entries, skipped unsized paths)``.
    :raises ManifestError: when the manifest cannot be written.
    :raises protonfs.drive.DriveError: on a Drive failure.
    """
    handle = handle or RemoteManifest.empty(ctx)
    now = _now()
    skipped: list[str] = []
    wanted: dict[str, ManifestEntry] = {}
    for rel, found in sorted(remote.items()):
        if found.claimed_size is None:
            skipped.append(rel)
            continue
        indexed = ctx.index.get(rel)
        sha256 = ""
        if (
            indexed is not None
            and indexed.sha256
            and indexed.size == found.claimed_size
            and (not (indexed.sha1 and found.sha1) or indexed.sha1 == found.sha1)
        ):
            sha256 = indexed.sha256
        wanted[rel] = ManifestEntry(
            size=found.claimed_size,
            sha256=sha256,
            sha1=found.sha1 or "",
            revision=found.revision or "",
            recorded=now,
        )
    for rel in list(handle.manifest.entries):
        if rel not in wanted:
            handle.forget(rel)
    for rel, entry in wanted.items():
        current = handle.manifest.entries.get(rel)
        if (
            current is not None
            and not entry.sha256
            and current.sha256
            and (current.size, current.sha1) == (entry.size, entry.sha1)
        ):
            # Another host recorded the sha256 of exactly this content; keep it.
            entry.sha256 = current.sha256
        if current is None or (
            current.size, current.sha1, current.revision, current.sha256
        ) != (entry.size, entry.sha1, entry.revision, entry.sha256):
            handle.record(rel, entry)
    if not handle.dirty and handle.revision is not None:
        return handle.manifest.generation, len(handle.manifest.entries), skipped
    # With no manifest on Drive yet, write one even for an empty listing, so the root is
    # marked as maintained from here on.
    generation = handle.save(force=handle.revision is None)
    return generation, len(handle.manifest.entries), skipped


def seed_index(ctx: RepoContext) -> tuple[int, int] | None:
    """Seed an EMPTY index with metadata-only entries from the remote manifest.

    For a fresh clone's first ``pull``, which otherwise has nothing to pull until a full
    ``refresh`` walk. Reading only, and ``pull`` verifies every download itself, so a
    stale entry costs a reported failure rather than anything silent. The manifest may
    lag, so the caller must say that files it does not list were not included.

    :returns: ``(entries seeded, manifest generation)``, or ``None`` when there is no
        readable manifest (or ``$PROTONFS_NO_MANIFEST`` is set).
    """
    if disabled():
        return None
    from protonfs.index import IndexEntry
    from protonfs.reporting import get_reporter

    try:
        handle = RemoteManifest.load(ctx)
    except (ManifestError, DriveError) as exc:
        get_reporter().warn(f"could not read the remote manifest: {exc}")
        return None
    if handle is None:
        return None
    now = _now()
    root = _remote_root(ctx)
    seeded = 0
    for rel, entry in handle.manifest.entries.items():
        if is_control_path(rel) or ctx.index.get(rel) is not None:
            continue
        ctx.index.set(
            rel,
            IndexEntry(
                size=entry.size,
                mtime=0.0,
                sha256=entry.sha256,
                sha1=entry.sha1,
                remote_path=f"{root}/{rel}",
                origin_device="unknown",
                local_state="metadata-only",
                last_synced=now,
            ),
        )
        seeded += 1
    ctx.index.set_manifest_state(handle.manifest.generation, handle.revision or "")
    ctx.index.save()
    return seeded, handle.manifest.generation


def staleness_note(ctx: RepoContext) -> str | None:
    """A one-line note when the remote manifest changed since this index was reconciled.

    Costs two small listings and no download, and only runs for a repo that maintains a
    manifest and an index that has been reconciled with one. Best effort: any failure to
    check says nothing, since the caller's own work does not depend on it.
    """
    state = ctx.index.manifest_state
    if state is None or not maintenance_enabled(ctx):
        return None
    try:
        ident = probe(ctx)
    except DriveError:
        return None
    if ident is None or (ident.revision or "") == state["revision"]:
        return None
    return (
        f"note: the remote manifest has changed since this index was last reconciled "
        f"(at generation {state['generation']}); other hosts may have pushed files this "
        "index does not list yet. `protonfs pull --refresh` picks them up."
    )
