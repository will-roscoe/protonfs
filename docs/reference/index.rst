Command Reference
==================

.. Page contents are rendered by furo's right-hand sidebar; an explicit
.. `.. contents::` directive collides with it (furo TOC JS error), so it is omitted.

Every ``protonfs`` command, with its synopsis, arguments/options, what it actually
does, and worked examples. This page describes *behavior*; for the frozen contract
(exact exit codes, option names, config keys, env vars that will not change without
a major version bump) see :doc:`../stability`.

Global behavior
----------------
Every command that mutates a repo's index (``push``, ``pull``, ``rm``, ``restore``,
``refresh``, ``offload``) takes an exclusive, non-blocking advisory lock on
``.protonfs/lock`` for its duration, so two ``protonfs`` processes never interleave
writes to ``index.json``. If another process already holds it, the command fails
fast with an instructive message rather than blocking or racing (see
:doc:`../guarantees`).

Runtime commands that shell out to ``proton-drive`` (everything except ``config``
and ``shell-init``) share an error boundary: a Drive/auth failure, a locked
keyring, or a held repo lock all surface as a clean one-line error instead of a
Python traceback. An auth failure additionally suggests ``protonfs auth login``.

``setup``
---------
**Synopsis:** ``protonfs setup [--dry-run] [--migrate-lfs/--no-migrate-lfs]``

Installs/verifies the ``proton-drive`` CLI, initializes ``.protonfs/`` in the
current directory (``config.json``, ``ignore``, a ``.gitignore`` that excludes
``index.json``/``refresh-state.json``, and a ``.gitattributes`` that exempts those
control files from git-LFS), and migrates the repo off git-LFS if it is tracked.
It also creates the configured ``remote_root`` on Drive if it does not exist yet,
so the first ``push`` has somewhere to land without hand-creating folders.

- ``--dry-run`` — preview what would change without writing anything or touching Drive.
- ``--migrate-lfs`` / ``--no-migrate-lfs`` — force or skip the git-LFS migration.
  Default: migrate only when the current directory *is* the git toplevel, so running
  ``setup`` in a subdirectory of a larger repo never migrates the enclosing repo off
  LFS by surprise.

Examples::

    protonfs setup                        # first-time setup in the current directory
    protonfs setup --dry-run               # see what setup would do first
    protonfs setup --no-migrate-lfs        # set up without touching git-LFS either way

``status``
----------
**Synopsis:** ``protonfs status [PATH]...``

Scans the local tree (optionally scoped to ``PATH``), compares it against the
local index, and prints a count per sync state (``synced``, ``local-only``,
``remote-only``, ``metadata-only``, ``conflict``, ``local-modified``,
``remote-modified``, ``both-modified``, ``local-deleted``, ``remote-changed``,
``remote-deleted``, ``lfs-pointer``). It does not talk to Drive beyond the index
already on disk — run ``refresh`` first for an up-to-date picture of the remote.

Exit code: ``0`` clean, ``1`` drift present, ``2`` conflict present (conflict
outranks drift). See :doc:`../stability` for the exact mapping and
:doc:`../guarantees` for how states are classified.

Examples::

    protonfs status
    protonfs status subdir/
    protonfs status; echo "exit=$?"

``ls``
------
**Synopsis:** ``protonfs ls [PATH]... [--remote] [--trash]``

Lists tracked files with their sync state, as a table of ``path`` / ``state``.

- ``--remote`` — force a live Drive listing to compute state, instead of relying on
  the local index alone (slower, but catches remote changes ``refresh`` hasn't seen
  yet).
- ``--trash`` — list ``/trash`` instead of the working tree (name and type only; no
  sync-state column, since trashed items aren't tracked in the index).

Examples::

    protonfs ls
    protonfs ls --remote subdir/
    protonfs ls --trash

``push``
--------
**Synopsis:** ``protonfs push [PATH]... [--resolve {merge,keep-both,replace,skip}] [--dry-run]``

Uploads local-only and locally-modified files under ``PATH`` (or the whole repo)
to Drive. When run interactively (stderr is a terminal), a running
``push: N/M file(s)`` progress line is shown on stderr after each uploaded batch;
scripts and redirected output see only the frozen summary on stdout.
Without ``--resolve``, a genuine remote conflict is reported as a named
per-file failure rather than silently resolved or skipped. Every batch is
re-verified against a live remote listing after upload (matching each file's
plaintext ``claimedSize``) before it is recorded in the index — proton-drive can
report a transfer as successful when it did not actually land; an unverified file
is left unindexed and retried on the next push instead of falsely marked synced.
A file that is an un-smudged git-LFS pointer stub is never pushed, even if
misclassified upstream, because that would overwrite real Drive content with a
131-byte placeholder. See :doc:`../guarantees` for the full mechanism.

- ``--resolve`` — conflict strategy passed through to ``proton-drive`` for files
  that changed on both sides: ``merge``, ``keep-both``, ``replace``, ``skip``.
- ``--dry-run`` — report what would be pushed without transferring anything.

Progress is saved after each directory group, so an interrupted push resumes
rather than restarting.

Examples::

    protonfs push                         # everything in scope that is new/changed
    protonfs push subdir/ --resolve replace
    protonfs push --dry-run

``pull``
--------
**Synopsis:** ``protonfs pull [PATH]... [--resolve {remote,local,both}] [--dry-run] [--refresh]``

Downloads remote-only and (with ``--resolve``) remote-modified files under
``PATH`` (or the whole repo). When run interactively (stderr is a terminal), a
running ``pull: N/M file(s)`` progress line is shown on stderr after each
transferred batch; scripts and redirected output see only the frozen summary on
stdout. A file edited **locally** and changed on the
**remote** since the last sync (a divergence) is left untouched by a bare
``pull`` — it is reported and the command exits non-zero, so a local edit is
never silently overwritten. Choose a side with ``--resolve``:

- ``remote`` — overwrite the local copy with the remote one.
- ``local`` — keep the local copy; it stays queued for the next ``push``.
- ``both`` — fetch the remote copy alongside the local one under a ``.remote``
  suffix (untracked) for a manual merge.

``--refresh`` seeds the index from a fresh remote listing before pulling (metadata
only, no download) — useful on a machine whose index doesn't yet know everything
already on Drive. Without an index yet, plain ``pull`` refuses to run and tells
you to run ``refresh`` first (or pass ``--refresh``).

``--dry-run`` previews transfers and unresolved conflicts without downloading
anything.

Examples::

    protonfs refresh && protonfs pull       # typical first pull on a new machine
    protonfs pull --refresh                 # equivalent, one command
    protonfs pull path/to/file --resolve remote
    protonfs pull --resolve both            # fetch remote copies as *.remote for merging

``offload``
-----------
**Synopsis:** ``protonfs offload [PATH]... [--no-verify] [--dry-run] [--yes]``

Deletes the *local* bytes of protonfs-tracked files already confirmed present on
Drive, reclaiming disk space while leaving the index entry as
``local_state=metadata-only`` — a later ``pull`` restores the file in full. This
is the inverse of ``pull``; it never touches the remote copy.

Before deleting anything, every candidate is (a) checked for unsynced local
edits — a file whose live content hash differs from what the index last recorded
is never offloaded, verify or not, since offloading it would destroy the only
copy of that edit — and (b) by default, re-verified against a *live* remote
listing (not just the index), requiring the remote's plaintext ``claimedSize`` to
match the local file's byte size. A file that fails either check is left alone
and reported (``skipped_modified`` / ``skipped_unverified``); this is not treated
as command failure.

- ``--no-verify`` — skip the live remote re-verification (the unsynced-edit guard
  in (a) always still applies). Unsafe if the remote could have changed since the
  index was last updated.
- ``--dry-run`` — preview what would be offloaded without deleting anything.
- ``--yes`` — skip the confirmation prompt.

Examples::

    protonfs offload subdir/                  # prompts for confirmation
    protonfs offload --dry-run
    protonfs offload --yes --no-verify         # unsafe: trust the index alone

``rm``
------
**Synopsis:** ``protonfs rm PATH... [-r/--recursive] [-f/--force] [--yes]``

Trashes ``PATH`` on Drive (reversible via ``restore``). Requires ``-r``/
``--recursive`` for a directory. The command removes the matching index entries
locally regardless of whether the permanent-delete step below runs.

``-f``/``--force`` additionally attempts to *permanently* delete the trashed
node after trashing it. proton-drive addresses a trashed node only by
``/trash/<basename>`` (no working UID addressing), so when two or more trashed
items share a basename, protonfs cannot safely tell which is yours: it leaves
the item trashed (still reversible) and reports the ambiguity rather than
guessing. See :doc:`../guarantees` for the exact boundary.

``--yes`` skips the confirmation prompt.

Examples::

    protonfs rm old-dump.ev
    protonfs rm -r stale-dir/ --yes
    protonfs rm -f duplicate.tmp             # trash, then attempt permanent delete

``restore``
-----------
**Synopsis:** ``protonfs restore PATH...``

Restores a previously trashed file/directory on Drive by its original path.
On proton-drive versions that reject original-path restore (0.5.0+), protonfs
falls back to resolving the trashed entry by name — and refuses to act (raising
an error naming the ambiguity) rather than guess when more than one trashed item
shares that name under a different original parent. See the boundary spelled out
in :doc:`../guarantees` and in ``DriveClient.restore``'s docstring
(``src/protonfs/drive.py``).

Examples::

    protonfs restore old-dump.ev
    protonfs restore stale-dir/

``trash list``
---------------
**Synopsis:** ``protonfs trash list``

Lists every item currently in ``/trash``: its name, its original parent (resolved
on a best-effort basis — shown as ``?`` when proton-drive can't resolve it), and
how many *other* trashed items share the same name. A nonzero duplicate count is
exactly the ambiguity ``restore`` can refuse to resolve on its own (#56): proton-
drive resolves ``/trash`` paths by name, first match wins, so same-named entries
can silently block a restore or shadow one another.

Examples::

    protonfs trash list

``trash empty``
-----------------
**Synopsis:** ``protonfs trash empty [--yes]``

Permanently empties ``/trash`` for the whole Proton Drive account by calling
``proton-drive filesystem empty-trash``. This is **irreversible** and **not**
scoped to this repo's ``remote_root`` — it deletes every trashed item on the
account, including ones unrelated to this repo. Without ``--yes``, the command
prints that warning and requires typing an exact confirmation phrase; anything
else aborts without emptying trash.

Deliberately out of scope: permanently deleting a single trashed item by UID.
proton-drive does not accept node UIDs for ``/trash`` paths (see ``rm``'s
duplicate-basename limitation above and #56's analysis), so there is no safe way
to target one item there — use ``trash list`` to find and resolve duplicates via
the Drive web UI, or ``trash empty`` to clear everything.

Examples::

    protonfs trash list                     # see what's there and any duplicates
    protonfs trash empty                    # prompts for typed confirmation
    protonfs trash empty --yes              # scripts / non-interactive use

``refresh``
-----------
**Synopsis:** ``protonfs refresh [PATH]... [--prune]``

Walks Drive under the configured ``remote_root`` (or ``PATH`` within it) and seeds
the local index with metadata-only entries for anything found there that this
machine's index doesn't already know about. This is the cross-client primitive: a
fresh machine (or one that missed files another client pushed) becomes aware of
everything already on Drive without downloading any content, so it will not
re-upload what's already there. It also reports files that changed or were
deleted on the remote since they were last seen.

- ``--prune`` — drop index entries for files that were deleted on the remote
  (without it, they are reported but kept, so a later push doesn't lose track
  of them by accident).

The walk is resumable: progress (both the seeded entries and the walk's own
frontier) is saved incrementally, so a run interrupted by an API throttle picks
up where it left off on the next invocation instead of restarting from the root.
Change/deletion detection only runs after a *complete* pass, though — a resumed
partial pass seeds but does not yet report changes/deletions.

Examples::

    protonfs refresh                      # seed everything not yet known
    protonfs refresh subdir/ --prune       # scope to a subtree, drop remote-deleted entries

``install-drive``
------------------
**Synopsis:** ``protonfs install-drive [--version VERSION] [--skip-keyring]``

Downloads the official ``proton-drive`` CLI binary for the current platform
(linux-x64 requires AVX2, linux-arm64, macOS x64/arm64), verifies its SHA-512
against a pinned checksum before installing it (never installs an unverified
binary), and by default also prepares the OS keyring proton-drive will use to
store its session — done here, not at first login, so a keyring failure surfaces
before a browser sign-in is thrown away.

- ``--version`` — install a specific ``proton-drive`` version instead of the
  pinned default.
- ``--skip-keyring`` — skip the keyring bootstrap step.

Examples::

    protonfs install-drive
    protonfs install-drive --version 0.5.0
    protonfs install-drive --skip-keyring

``upgrade``
------------
**Synopsis:** ``protonfs upgrade [--check] [--drive-only | --repo-only]``

Upgrades the installed ``proton-drive`` binary to the highest version this
protonfs release supports (SHA-512-verified before an atomic swap; a newer
upstream release is reported but never installed), verifies the session survived
the swap, and -- inside a protonfs root -- runs any pending repo-state
migrations. See :doc:`../upgrading` for the full upgrade story.

- ``--check`` — preview everything, change nothing; exits ``0`` when fully
  current, ``1`` when an upgrade or migration is available.
- ``--drive-only`` — only the binary; skip migrations.
- ``--repo-only`` — only the migrations; skip the binary.

Examples::

    protonfs upgrade --check     # what would happen?
    protonfs upgrade             # binary + migrations
    protonfs upgrade --repo-only # just bring .protonfs/ current

``doctor``
----------
**Synopsis:** ``protonfs doctor [--fix]``

Checks that this host can actually run ``proton-drive``: the binary is present
and runnable, and on Linux, that a D-Bus session bus and a usable (unlocked)
Secret Service keyring are reachable. Written for headless hosts (SSH, no
desktop), where a graphical login's sealed ``login.keyring`` is the most common
silent failure.

- ``--fix`` — additionally bootstrap a protonfs-owned session bus and keyring
  rather than only reporting the problem.

Examples::

    protonfs doctor
    protonfs doctor --fix

``shell-init``
--------------
**Synopsis:** ``protonfs shell-init``

Prints ``export VAR=value`` lines so that running the ``proton-drive`` binary by
hand (outside of ``protonfs``) sees the same session bus/keyring environment
protonfs sets up for itself. Every ``protonfs`` command does this internally;
this is only needed for manual ``proton-drive`` invocations.

Example::

    eval "$(protonfs shell-init)"
    proton-drive filesystem list /my-files

``auth``
--------
**Synopsis:** ``protonfs auth {login,logout,status}``

- ``login`` / ``logout`` — passthrough to ``proton-drive auth <action>`` with
  inherited stdio, so an interactive login URL/prompt reaches your terminal
  directly. Exit code is whatever ``proton-drive`` returns.
- ``status`` — checks for a valid session directly (without invoking
  ``proton-drive``), printing ``authenticated`` or a reminder to log in. Exit
  ``0`` if authenticated, ``1`` otherwise.

Examples::

    protonfs auth login
    protonfs auth status
    protonfs auth logout

``config``
----------
**Synopsis:** ``protonfs config get KEY`` / ``protonfs config set KEY VALUE [--global | --local]``

Reads or writes protonfs's layered configuration. Known keys:
``remote_root``, ``device_id``, ``defaults.on_conflict``, ``defaults.low_io``.

``get`` always prints the fully **resolved** value across every layer (env var >
per-device local config > shared per-repo config > global user config > built-in
default) — see :doc:`../stability` for the complete precedence list and the
environment variables that can override each key.

``set`` writes to exactly one layer:

- default (no flag) — the shared per-repo file, ``.protonfs/config.json``
  (the file you commit, so every clone syncs to the same place).
- ``--local`` — the per-device file, ``.protonfs/config.local.json``
  (gitignored).
- ``--global`` — the user-wide file, ``~/.config/protonfs/config.json``
  (or ``$PROTONFS_CONFIG``).

``--global`` and ``--local`` are mutually exclusive.

Examples::

    protonfs config get remote_root
    protonfs config set defaults.low_io true --local
    protonfs config set remote_root /my-files/sim-data --global

See also
--------
* :doc:`../stability` for the frozen exit-code/option contract.
* :doc:`../guarantees` for durability and drift-resolution guarantees.
* :doc:`../getting-started/index` and :doc:`../getting-started/syncing` for
  workflow-oriented walkthroughs.
