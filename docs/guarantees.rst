Durability and Drift-Resolution Guarantees
============================================

.. Page contents are rendered by furo's right-hand sidebar; an explicit
.. `.. contents::` directive collides with it (furo TOC JS error), so it is omitted.

This page writes down, in plain prose, what ``protonfs`` actually guarantees about
not losing your data and about how it decides what needs syncing. Every claim
below is grounded in a specific mechanism in the source, referenced by module —
where a guarantee has a boundary or a known limitation, it is stated explicitly
rather than glossed over. For the command-by-command behavior, see
:doc:`reference/index`; for the frozen CLI contract (exit codes, option names),
see :doc:`stability`.

Durability
----------

Atomic index and config writes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The local sync manifest (``.protonfs/index.json``) and the resumable-refresh
frontier (``.protonfs/refresh-state.json``) are never edited in place. Both are
written via the same idiom (``IndexStore.save`` in ``src/protonfs/index.py``,
``refreshstate.save_frontier`` in ``src/protonfs/refreshstate.py``): the new
document is written to a temp file in the *same directory* (so the following
rename is a true same-filesystem atomic operation), flushed and ``fsync``'d, then
swapped onto the real path with ``os.replace``. A reader — or a crash at any
point during the write — only ever sees the complete old file or the complete
new one; a torn or truncated index is not a failure mode this design permits.

Config files (``.protonfs/config.json``, ``config.local.json``, and the global
``~/.config/protonfs/config.json``) are written the same way by
``src/protonfs/config.py``.

Repo locking
~~~~~~~~~~~~
Every command that read-modifies-writes the index (``push``, ``pull``, ``rm``,
``restore``, ``refresh``, ``offload``) takes an exclusive, non-blocking POSIX
``flock`` on ``.protonfs/lock`` for its duration (``src/protonfs/locking.py``). A
second concurrent ``protonfs`` process on the same repo fails immediately with an
instructive message rather than blocking or silently interleaving writes with the
first. Because ``flock`` is tied to the open file description, a crashed process
releases its lock automatically — there is no stale lock file to clean up by
hand.

**Boundary:** this lock is advisory and POSIX-only (``fcntl``). On a platform
without ``fcntl`` (documented in the module as the case pending issue #9 — in
practice non-POSIX systems), it degrades to a no-op rather than blocking
progress; two processes on such a platform are not protected against each other.

Resumable, idempotent push and pull
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``push`` and ``pull`` (``src/protonfs/commands/push.py``,
``src/protonfs/commands/pull.py``) both group their work by parent directory and
call ``ctx.index.save()`` after each group completes, not just once at the end.
Combined with the atomic-write guarantee above, this makes an interrupted run
(Ctrl-C, a dropped connection, a killed process) crash-safe: the next invocation
of the same command sees the index as it stood after the last completed group and
only acts on what remains, rather than re-doing work already recorded or losing
track of what happened before the interruption.

Re-running ``push``/``pull`` on files that are already synced is a no-op — they
classify as ``synced`` and are excluded from the transfer set — so retrying a
command after a partial failure is always safe to do.

Resumable refresh with throttle backoff
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``refresh`` walks the remote tree breadth-first (``DriveClient.walk`` in
``src/protonfs/drive.py``). Each directory listing goes through
``list_with_backoff``, which retries a timeout or throttle-shaped error with
exponential backoff (bounded by ``PROTONFS_LIST_RETRIES`` /
``PROTONFS_LIST_BACKOFF`` / ``PROTONFS_LIST_BACKOFF_CAP``) before giving up on
that call, so a single wedged directory doesn't blow up an otherwise-healthy
walk. Beyond that, the walk's own frontier (the queue of directories not yet
listed) is persisted after each directory via the atomic writer described above
(``src/protonfs/refreshstate.py``), and metadata-only index entries are seeded
and saved per-directory as they're discovered
(``commands/refresh.py:_seed_directory``). If a run is interrupted partway — by
a throttle giving up, or by the process dying — the next ``refresh`` invocation
resumes from the saved frontier instead of restarting the whole tree from the
root (and re-triggering the same throttle).

**Boundary:** change/deletion detection (a file classified ``remote-changed`` or
``remote-deleted``) requires a *complete* pass over the remote listing, since a
deletion can only be inferred from a file's absence from the whole tree. A
resumed (partial) pass still seeds newly-discovered files but does not run
change/deletion detection; that only happens once a full pass completes without
being interrupted.

Verify-push-against-remote (never trust "transferred")
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``proton-drive`` can report a file as successfully transferred without the bytes
actually landing on Drive (an "under-delivery"). ``push`` never records a file as
synced on trust alone: after each upload batch it re-lists the destination
directory via ``DriveClient.remote_identities`` and only indexes a file once it
is confirmed present there *and* its **plaintext** ``claimedSize`` matches the
local byte size exactly (``src/protonfs/commands/push.py``). This deliberately
compares against Proton's decrypted ``claimedSize``/``claimedDigests``, never the
encrypted ``totalStorageSize`` (which runs larger due to encryption overhead and
would never match a plaintext local size). A file that fails this check is left
unindexed and reported as a distinct ``under-delivered`` failure (not a
conflict) — the fix is a plain retry on the next push, not a ``--resolve``
strategy.

``offload`` reuses the identical verify-against-remote idiom before it deletes
any local bytes (below), and a batch is only considered indexable at all if
``proton-drive`` did not report it skipped — a skip is only reported as an
aggregate count with no per-file attribution, so the whole batch is left
unindexed rather than guessing which files within it actually skipped.

Git-LFS pointer-stub protection
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A repo migrated off git-LFS can still contain un-smudged pointer stubs (tiny
text files starting with the line ``version
https://git-lfs.github.com/spec/v1``, checked by ``is_pointer_stub`` in
``src/protonfs/lfs.py``) — for instance a clone that never ran ``git lfs pull``.
Such a stub's own content hash has nothing to do with the real tracked file, so
naively diffing it against the index or remote would either mass-false-conflict
or, worse, classify it as pushable and let a normal push overwrite the real
Drive object with a ~131-byte placeholder.

This is guarded in two independent places: ``classify`` (``src/protonfs/diff.py``)
short-circuits any local file that parses as a pointer stub to a dedicated
``lfs-pointer`` state before any hash comparison runs, so it is never treated as
local-only or conflicting; and ``push`` (``src/protonfs/commands/push.py``)
independently re-checks every file it's about to upload and refuses any that
parse as a pointer stub, regardless of how it got into the candidate set. The
second check exists specifically as defense-in-depth in case the first is ever
wrong.

SHA-512-pinned binary installs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``protonfs install-drive`` downloads the official ``proton-drive`` binary and
verifies its SHA-512 digest against a pinned checksum (``pinned_sha512`` in
``src/protonfs/install.py``) before writing it to disk — a mismatch raises an
error and the binary is not installed. For a platform/version combination
without a built-in pin, ``PROTONFS_DRIVE_SHA512`` supplies the checksum to verify
against explicitly; there is no path that installs a binary without checking it
against *some* expected digest.

Drift resolution
-----------------

How ``status``/``ls`` classify sync state
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Both commands are thin renderers over the same classifier,
``classify()`` in ``src/protonfs/diff.py``, which compares up to three views of
each path — the local scan, the local index, and (when available) a live remote
listing — into one of the ``SyncState`` values:

- ``synced`` — local matches the index; nothing pending.
- ``local-only`` — present locally, no index entry (never pushed).
- ``remote-only`` / ``metadata-only`` — an index entry with no local file, and
  (without a remote view) treated as such by default; ``metadata-only``
  specifically means this device deliberately never materialized it (e.g. after
  ``refresh`` or ``offload``).
- ``local-modified`` — local content diverged from the index; the remote (when
  known) has not.
- ``remote-modified`` — the remote diverged from the index; local has not.
- ``both-modified`` — both sides diverged from the index *and* a remote view is
  available to prove that (a resolvable divergence).
- ``conflict`` — local diverged from the index but there is no remote view to
  attribute a direction to (no live listing was fetched, or the remote no longer
  lists the file) — a conservative fallback, never auto-resolved.
- ``local-deleted`` — synced down as a real file, now absent locally, still
  present remotely.
- ``remote-changed`` — a metadata-only entry whose remote size moved.
- ``remote-deleted`` — an index entry with nothing at that path in a full remote
  walk.
- ``lfs-pointer`` — an un-smudged git-LFS pointer stub (see above); deliberately
  inert, never treated as actionable drift.

Remote divergence itself (``_remote_diverged``) prefers comparing Proton's
plaintext ``claimedDigests.sha1`` against the index's stored sha1 when **both**
sides know it; when either is unknown (trust-on-first-use for a value protonfs
has never observed), it falls back to comparing plaintext ``claimedSize``
against the index's size — again, never the encrypted size, for the same
reason as the push-verification guarantee above.

``status``'s exit code (``0`` clean / ``1`` drift / ``2`` conflict, conflict
outranking drift) is a direct function of these states, not a separate
classification — see ``status_exit_code`` in ``src/protonfs/commands/status.py``
and the full table in :doc:`stability`.

``pull --resolve``: never silently overwrite a local edit
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A bare ``pull`` (no ``--resolve``) only ever brings down files that are absent
locally (``remote-only``/``metadata-only``) — it cannot overwrite a local file
because it never considers a locally-present file as a pull candidate without a
remote view. A file that changed on **both** sides since the last sync
(``both-modified``) or that locally diverged with no provable remote view
(``conflict``) is left completely untouched and reported as a failure; ``pull``
exits non-zero rather than guessing.

``--resolve`` picks a policy applied only to genuinely resolvable divergence
(``both-modified``, i.e. a remote copy exists to reconcile against):

- ``remote`` — download the remote copy, overwriting local (``file_strategy=
  "replace"`` against Drive).
- ``local`` — keep the local file untouched; it stays queued for the next push.
- ``both`` — fetch the remote copy into a sibling file under a ``.remote``
  suffix (deliberately **not** indexed — it is scratch for a manual merge), and
  leave the original local file alone.

Plain ``conflict`` entries (no remote view available to attribute direction) are
**never** resolved by ``--resolve`` — they are reported regardless, because
there is no proof of what "remote" even means for them without a live listing.

``offload``'s remote-verified guarantee before deleting local bytes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Before any local file is unlinked, ``offload`` (``src/protonfs/commands/
offload.py``) applies two checks, the first of which is **unconditional** (it
holds even under ``--no-verify``):

1. The file's *live* content hash must match what the index recorded at last
   sync. A file edited locally since it was last synced has content that is not
   on Drive; deleting it would destroy the only copy of that edit, and a
   same-size remote object would even pass a size-only verification — so this
   check compares actual content hash, not just size, and cannot be turned off.
2. By default (unless ``--no-verify``), the remote parent directory is freshly
   re-listed and the file must be present there with a plaintext
   ``claimedSize`` matching the local byte size — the same idiom as push's
   post-upload verification, applied here before a delete instead of after an
   upload.

A file failing either check is left on disk and reported
(``skipped_modified``/``skipped_unverified``) rather than silently kept or
force-deleted; this is not treated as command failure. ``--no-verify`` only
disables check 2 (trusting the index's record of what was last confirmed
on Drive); it never disables check 1.

Trash-not-delete semantics
~~~~~~~~~~~~~~~~~~~~~~~~~~~
``rm`` (``src/protonfs/commands/rm.py``) trashes the remote node by default —
this is always reversible via ``restore``. ``rm -f``/``--force`` additionally
*attempts* a permanent delete of the now-trashed node, but only as a best-effort
second step after the (always-succeeding) trash.

**Boundary — duplicate basenames:** ``proton-drive``'s ``filesystem delete``
addresses a trashed node only by its path under ``/trash`` (``/trash/<basename>``);
there is no working way to target a specific trashed node by its stable UID (as
of proton-drive 2026-07-16, both ``/trash/<uid>`` and a bare UID are rejected,
despite ``--help`` advertising UID addressing elsewhere — a live probe test in
the test suite flags it automatically if a future ``proton-drive`` lifts this).
Consequence: when two or more trashed items share a basename, ``rm -f`` cannot
safely tell which one you meant to permanently delete, so it **refuses** the
permanent-delete step and leaves the item trashed (still reversible), reporting
the ambiguity so you can resolve it via the Proton Drive app/web UI instead.
Nothing is ever lost by this refusal — the item stays exactly where trashing put
it.

**Boundary — restore ambiguity:** ``restore`` (``DriveClient.restore`` in
``src/protonfs/drive.py``) tries original-path restore first (accepted by
proton-drive 0.4.6). On proton-drive 0.5.0+, which removed that form, it falls
back to resolving the trashed entry by decrypted name under ``/trash``. Because
0.5.0 restores by name with first-match-wins and offers no UID disambiguation
under ``/trash`` either, this fallback explicitly checks that the resolved
same-named trash entry's original parent matches the path being restored; if
more than one trashed item shares that basename and the first match did not come
from the expected parent, restore **refuses** rather than risk restoring the
wrong node, raising an error that names the ambiguity and points at the Drive web
UI (or permanently deleting the older same-named entries first) as the way out.
This is an upstream ``proton-drive`` limitation (tracked as issue #56 in the
codebase), not a choice protonfs makes — see the full detail in
``DriveClient.restore``'s docstring.

See also
--------
* :doc:`reference/index` for the full command-by-command reference.
* :doc:`stability` for the frozen exit-code/option contract these guarantees sit
  underneath.
* :doc:`getting-started/syncing` for the day-to-day workflow these mechanisms
  support.
