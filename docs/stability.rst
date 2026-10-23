Stability Promise (M4.1)
=========================

.. Page contents are rendered by furo's right-hand sidebar; an explicit
.. `.. contents::` directive collides with it (furo TOC JS error), so it is omitted.

This page is the ``protonfs`` contract for **3.x**, as of 3.0.0: every command,
option/argument name, exit code, summary-line key, ``--format json`` key, sync-state
name, config file location, config key and environment variable listed here is a
stable public contract. A caller (human or script) that depends only on what is
documented on this page keeps working across ``3.x`` releases; breaking any of it
requires 4.0.0. ``tests/test_cli_surface.py`` checks the frozen sets in CI.

1.0.0 set the first contract. 2.0.0 broke it deliberately (the sync-state vocabulary,
#150; see `Changes since the 1.0 contract`_). 3.0.0 is a major release by accident: a
withdrawn breaking change's commit message reached the release tooling. Nothing in
3.0.0 breaks anything relative to 2.6.0.

Not part of the contract: the Python API (the ``protonfs.*`` modules), console output
at ``-v`` and above, the ``phase``/``done`` progress lines, the event log
(``.protonfs/events.log``), and the formats of the index, hash cache and other files
under ``.protonfs/`` that protonfs writes for itself.

Versioning policy
------------------
``protonfs`` releases are cut by the shared `ghtools
<https://github.com/will-roscoe/.ghtools>`_ pipeline (configured in
``.github/ghtools.toml``), which computes the next version from Conventional Commits: a
``feat`` commit bumps **minor**, a
``fix``, ``perf`` or ``revert`` bumps **patch**, and a breaking commit (``type!:`` or a
``BREAKING CHANGE:`` footer) bumps **major** -- *once the project is at 1.0 or later*. Before 1.0, that
same breaking-change signal is demoted to a **minor** bump instead, so day-to-day 0.x
development never jumps to 1.0.0 by accident. ``v1.0.0`` itself is therefore always a
deliberate, manually-created tag, not something the automated release pipeline produces
on its own.

From ``v1.0.0`` onward, any change that breaks something documented on this page --
removing or renaming a command/option, changing an exit code's meaning, moving a config
file, dropping an environment variable -- is a breaking change and requires that manual
major-version bump. Additive changes (a new command, a new optional flag, a new config
key with a backward-compatible default) remain minor/patch as normal.

Only the first line of a commit is read for its type (and ``!``), but a
``BREAKING CHANGE:`` footer line counts anywhere in the message. A squash merge's message
carries the bodies of every commit it squashed, so a ``BREAKING CHANGE:`` footer from a
sub-commit whose break was later withdrawn still cuts a major. Delete such lines from
the squash message before merging. (This is how 3.0.0 happened.)

Release-override directives (from 1.0.0)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A ``+:<spec>`` token standing **alone on its own line** of a commit message
(footer-style) overrides the Conventional Commit classification for that release
entirely, where ``<spec>`` is ``major`` | ``minor`` | ``patch`` | ``pre`` |
``prepre`` | ``rc``. Prose mentions and quoted examples (like the ones on this page)
never actuate a release. The pre-release forms follow SemVer 2.0.0 precedence with
channels ``alpha < beta < rc``:

* From a final release ``X.Y.Z``: ``+:pre`` -> ``X.(Y+1).0-alpha``, ``+:prepre`` ->
  ``X.(Y+1).0-alpha.0``, ``+:rc`` -> ``X.(Y+1).0-rc`` -- a pre-release always belongs
  to the *next* minor, so it sorts after the released base.
* Within a pre-release: ``+:prepre`` increments the number (``-alpha`` ->
  ``-alpha.1``); ``+:pre`` advances the channel (``alpha`` -> ``beta`` -> ``rc`` ->
  the final release); ``+:rc`` jumps any earlier channel to ``-rc`` and increments
  when already there (``-rc`` -> ``-rc.1``); ``+:patch``/``+:minor`` finalize.
* Plain Conventional Commits landing during a pre-release only increment the
  pre-release number -- leaving the channel always requires a directive.

Directives are imperative: they are not subject to the pre-1.0 breaking-change
demotion, and the highest-impact directive wins when several land in one release
batch. Pre-release tags are marked as GitHub pre-releases and publish to PyPI as
PEP 440 pre-releases (``1.1.0-alpha.1`` -> ``1.1.0a1``), which ``pip`` skips unless
``--pre`` is passed.

Release notes
~~~~~~~~~~~~~~
Each release's CHANGELOG entry is any hand-written ``[Unreleased]`` content followed
by notes generated from the release's Conventional Commits: grouped by type
(Features, Bug fixes, Performance, Reverts, Refactors, Documentation, Tests, Build,
CI, Style), chronological within each group (earliest first). ``chore`` commits and
``[skip ci]`` housekeeping are excluded from release notes.

Exit-code contract
-------------------
Every ``protonfs`` command follows the same top-level convention:

* ``0`` -- success.
* ``2`` -- usage error: bad/missing arguments, unknown option, invalid choice value.
  This is Click's own default behaviour and is not overridden anywhere.
* ``1`` -- everything else: an operational failure (a Proton Drive error, a lock
  conflict, a config problem) or a command-specific non-zero outcome documented below.
  A user declining a confirmation prompt (no ``--yes``/``--force``) also exits ``1``.

``status`` and ``auth status`` layer additional meaning onto exit code ``1``/``2`` for
unattended callers, documented in the table below.

Output contract
----------------
**Summary lines.** Each transfer command prints one summary line on stdout:
``[<label>: ]key=value key=value ...``, space-separated, every value a non-negative
integer. Any failures follow it, one ``  FAILED <name>: <error>`` line each. Required
keys always print; optional keys print only when non-zero.

.. list-table::
   :header-rows: 1
   :widths: 22 30 48

   * - Line
     - Required keys
     - Optional keys
   * - ``push``
     - ``transferred``, ``skipped``, ``failed``
     - ``adopted``, ``moved``, ``trashed``, ``local-deleted``, ``unsettled``
   * - ``pull``
     - ``transferred``, ``skipped``, ``failed``
     - ``local-deleted``
   * - ``offload``
     - ``offloaded`` (``would offload`` with ``--dry-run``), ``bytes_reclaimed``,
       ``skipped_unverified``, ``skipped_modified``, ``skipped_unsettled``
     -
   * - ``prune``
     - ``pushed:`` line: ``transferred``, ``skipped``, ``failed``; then offload's line
     -
   * - ``import``
     - push's keys, plus ``untracked``
     - push's optional keys
   * - ``export``
     - ``transferred``, ``skipped``, ``failed``, ``tracked``
     - ``trashed`` (with ``--trash-remote``)

What each key counts:

* ``transferred`` -- files actually uploaded (push) or downloaded (pull).
* ``skipped`` -- files proton-drive skipped: already in sync, or excluded.
* ``failed`` -- files that failed; each has a ``FAILED`` line.
* ``adopted`` -- files already on Drive that push verified match the local copy and
  indexed without uploading.
* ``moved`` -- files moved or renamed on Drive to follow a local move.
* ``trashed`` -- Drive copies of files deleted locally, moved to the Drive trash
  (``--deletions trash``; with ``--dry-run``, the ones that would be).
* ``local-deleted`` -- files this machine held that are gone locally and were left
  alone on Drive.
* ``unsettled`` -- files held back because they were modified within ``--min-age``.
* ``untracked`` -- files ``import`` stopped git tracking (added in 3.1.0).
* ``tracked`` -- files ``export`` staged for git to track (added in 3.1.0). For
  ``export``, ``transferred`` counts offloaded files downloaded, ``skipped`` files
  already on disk, and ``trashed`` Drive copies moved to the Drive trash.
* ``offloaded`` / ``would offload`` -- local copies removed (or that would be);
  ``bytes_reclaimed`` -- their total size in bytes; ``skipped_unverified``,
  ``skipped_modified``, ``skipped_unsettled`` -- files offload refused, by reason.

Rules: within 3.x a key is never renamed, removed or given a different meaning. New
keys may appear in a minor release, so a caller must ignore keys it does not know. Key
order is not part of the contract.

**The counting rule.** An exit code of ``0``, or a progress bar at 100%, is not proof
that every file was transferred. A run delivered everything a caller asked for only when
``failed=0`` and ``transferred``, ``skipped`` and ``adopted`` together account for every
file the caller expected.

**``--format json``.** ``status --format json`` prints one object with the keys
``counts`` (``{<state>: n}`` over every state in `Sync states`_), ``exit_code`` and
``remote``. ``ls --format json`` prints a list of row objects, whose keys depend on the
view:

* files (the default): ``path``, ``state``;
* ``--dirs``: ``path``, ``files``, ``local_bytes``, ``indexed_bytes``,
  ``apparent_bytes``, ``states``;
* ``--trash``: ``name``, ``type``.

The same rules apply: no key is renamed, removed or redefined within 3.x, and new keys
may appear.

Changes since the 1.0 contract
-------------------------------
What a caller written against 1.0 needs to know. Only contract changes are listed;
see the CHANGELOG for everything else.

**Broken in 2.0.0** (#150):

* The ``synced`` state is now ``locally-indexed`` in ``status``, ``status --format
  json`` keys and ``ls``. Read the new name; ``ls --state synced`` is accepted, with a
  warning, until 4.0.0.
* 2.0.0 also reported a file this machine held that is now gone locally as
  ``local-deleted`` without ``--remote``; 2.2.1 restored ``remote-only`` for that case,
  as in 1.0, so only the ``synced`` rename remains a difference from 1.0.

**Added:**

* Commands: ``completions`` (1.5.0), ``schedule`` (1.8.0), ``verify`` (2.1.0),
  ``prune`` (2.2.0), ``import``, ``export`` (3.1.0).
* Global options: ``-v``/``--verbose``, ``--progress-inline``/``--progress-lines``,
  ``--event-log``/``--no-event-log`` (1.3.0).
* ``PATH...``: every command that takes a ``PATH`` accepts several, e.g. from a shell
  glob (1.1.0).
* ``ls``: ``--dirs``, ``--format``, ``--state`` (1.1.0); ``--visual`` (1.2.0).
* ``status``: ``--format`` (1.1.0); ``--remote`` (2.0.0).
* ``push``/``pull``: ``--strict`` (1.11.0). ``push``: ``--min-age`` (2.4.0);
  ``--deletions``, ``--allow-mass-delete`` (3.0.0). ``pull``: ``--no-restore`` (3.0.0).
* ``offload``: ``--min-age`` (2.2.0).
* ``verify``: ``--repair`` (2.1.0), ``--index`` (2.4.0).
* ``schedule``: ``--strict`` (1.11.0); ``--keep``, ``--min-age`` (2.2.0);
  ``--deletions``, ``--no-restore`` (3.0.0).
* Sync states: the full table in `Sync states`_ (2.0.0), including ``lfs-pointer`` and
  the remote-view states.
* Config keys: ``defaults.event_log``, ``defaults.progress_style`` (1.3.0);
  ``defaults.manifest`` (2.1.0); ``defaults.batch_size``, ``defaults.skip_thumbnails``
  (2.5.0); ``defaults.deletions`` (3.0.0).
* Environment variables: ``PROTONFS_EVENT_LOG``, ``PROTONFS_PROGRESS_STYLE`` (1.3.0);
  ``PROTONFS_MANIFEST``, ``PROTONFS_NO_MANIFEST`` (2.1.0); ``PROTONFS_BATCH_SIZE``,
  ``PROTONFS_SKIP_THUMBNAILS``, ``PROTONFS_TRANSFER_MIN_RATE`` (2.5.0);
  ``PROTONFS_DRIVE_CACHE_HOME`` (2.6.0).
* The `Output contract`_ above (summary keys and ``--format json`` keys) is new in the
  3.x contract; the keys themselves shipped with the options that print them.

**Deprecated:** ``ls --state synced``, until 4.0.0.

**Removed:** nothing.

Global options (before the subcommand)
--------------------------------------
These flags are given before the subcommand (e.g. ``protonfs -vv --event-log pull``)
and apply to every command. A flag, when given, overrides its config key; when unset,
the resolved config value is used, falling back to the built-in default.

* ``-v`` / ``--verbose`` -- repeatable, ``-v``..``-vvvv``; raises console detail.
* ``--progress-inline`` / ``--progress-lines`` -- progress render style (default:
  :confval:`defaults.progress_style`, else inline on a TTY).
* ``--event-log`` / ``--no-event-log`` -- write ``.protonfs/events.log`` (default:
  :confval:`defaults.event_log`, else off).

Command surface
-----------------
One-line contract and exit codes for every registered command. "Options" lists every
flag/argument name; these names, not just their presence, are frozen.

.. list-table::
   :header-rows: 1
   :widths: 14 40 46

   * - Command
     - Contract
     - Exit codes
   * - ``setup``
     - Install/verify the proton-drive CLI, init ``.protonfs/``, migrate off git-lfs if
       present. Options: ``--dry-run``, ``--migrate-lfs/--no-migrate-lfs``.
     - ``0`` done; ``1`` operational failure (missing binary, keyring, not
       authenticated, Drive error, failed LFS upload, declined confirmation); ``2``
       usage error.
   * - ``deinit``
     - Remove ``.protonfs/`` from this directory (config, local config, index,
       refresh state, ignore/include, control ``.gitattributes``/``.gitignore``) after
       a summary + confirmation. Never touches synced payload files, local or remote.
       Reports (does not run) follow-up git steps when inside a git repo. Options:
       ``--dry-run``, ``--yes``.
     - ``0`` done (including dry-run); ``1`` not a protonfs root, lock held by another
       process, or declined confirmation; ``2`` usage error.
   * - ``status``
     - Summarize sync state as a count per state (see `Sync states`_).
       Argument: ``PATH...`` (optional, repeatable; e.g. from a shell glob).
       Options: ``--format [plain|json]``, ``--remote`` (added in 2.0.0).
       Without ``--remote`` nothing is checked on Drive: the comparison is against
       the local index only, so ``locally-indexed`` means "matches what protonfs
       last recorded", not "verified present on Drive". ``--format json`` emits
       ``{"counts": {<state>: n, ...}, "exit_code": n, "remote": bool}``.
     - ``0`` clean (every file in a *clean* state below); ``1`` drift present
       (something to push/pull/prune); ``2`` conflict present (a *conflict*-class
       state below). Conflict outranks drift when both are present. (Usage errors
       also use ``2``, but status's own ``2`` is a data outcome, not a usage error.)
   * - ``ls``
     - List tracked files with their sync state. Argument: ``PATH...`` (optional, repeatable; e.g. from a shell glob).
       Options: ``--remote``, ``--trash``, ``--dirs``, ``--state`` (repeatable; any
       state name below, plus the deprecated alias ``synced`` for ``locally-indexed``,
       accepted with a warning until 4.0.0),
       ``--format [table|plain|json]``, ``--visual [treemap|waffle]``.
     - ``0`` success; ``1`` Drive/auth error; ``2`` usage error.
   * - ``push``
     - Upload local-only/changed files to Drive. Argument: ``PATH...`` (optional,
       repeatable; a shell glob, or a quoted pattern protonfs expands itself against the
       local tree).
       Options: ``--resolve [remote|local|both]`` (the proton-drive strategy names
       ``merge|keep-both|replace|skip`` remain accepted as synonyms), ``--dry-run``,
       ``--strict``, ``--min-age DURATION`` (settle window, default ``0``; added in
       2.4.0). A file held back by ``--min-age`` is counted as ``unsettled=`` and is not
       a failure. Since 3.0.0 a file moved or renamed locally is moved on Drive instead of
       uploaded, and counted as ``moved=``. ``--deletions [keep|trash]`` and
       ``--allow-mass-delete`` (added in 3.0.0): with ``trash``, files deleted locally
       are trashed on Drive and counted as ``trashed=``; a guard refusing the trash step
       is a failure (exit ``1``) with nothing trashed. Files deleted locally that stay on Drive are counted as
       ``local-deleted=`` (since 3.0.0).
     - ``0`` all transferred/skipped (including a pattern that matched nothing, which is
       reported and skipped); ``1`` one or more files failed to transfer, a pattern
       matched nothing under ``--strict``, or a Drive/lock error; ``2`` usage error.
   * - ``import``
     - Move files git tracks in this store out of git and into the store (added in
       3.1.0). Argument: ``PATH...`` (required; a file, a directory meaning its tracked
       files, or a quoted glob matching tracked files only). Options: ``--dry-run``,
       ``--strict``. Guarantees: git is changed only after every file is verified on
       Drive and against the blob in ``HEAD``, and for every ``PATH`` or none; changes
       are staged, never committed; git history is never touched; no imported file is
       changed or deleted; every refusal is reported before anything is done.
     - ``0`` imported, or a dry run with nothing refused; ``1`` a refusal (untracked,
       uncommitted changes, a symlink, a git-LFS file or pointer stub, a
       ``.gitignore``/``.gitattributes``, excluded by the store, outside the store, a
       ``.gitignore`` with uncommitted changes, no git work tree), a file that
       failed verification, a git failure (rolled back), or a Drive/lock error; ``2``
       usage error.
   * - ``export``
     - Move files this store holds back into git (added in 3.1.0). Argument:
       ``PATH...`` (required; a file, a directory meaning the files the store holds
       under it that git does not track, or a quoted glob matching the files the store
       holds, offloaded ones included). Options: ``--dry-run``, ``--strict``,
       ``--trash-remote``. Guarantees: offloaded files are downloaded first; git is
       changed only after every file is verified against its index entry and a fresh
       Drive listing, and for every ``PATH`` or none; changes are staged, never
       committed; git history is never touched; no file is changed or deleted; protonfs
       stops syncing the files on every machine; their Drive copies are kept unless
       ``--trash-remote``, which trashes them only after the git step; every refusal is
       reported before anything is done.
     - ``0`` exported, or a dry run with nothing refused; ``1`` a refusal (not held by
       the store, already tracked by git, still ignored by a git rule export did not
       write, a ``.gitignore``/``.gitattributes``, outside the store, a ``.gitignore``
       or ``.protonfs/ignore`` with uncommitted changes, no git work tree), a file not
       downloaded or not verified, a git failure (rolled back), a Drive copy
       ``--trash-remote`` could not trash, or a Drive/lock error; ``2`` usage error.
   * - ``pull``
     - Download remote-only/changed files from Drive. Argument: ``PATH...`` (optional,
       repeatable; a shell glob, or a quoted pattern protonfs expands itself against the
       index).
       Options: ``--resolve [remote|local|both]`` (``replace`` accepted as an alias for
       ``remote``), ``--dry-run``, ``--refresh``, ``--strict``, ``--no-restore`` (added
       in 3.0.0: files this machine held that are gone locally are left alone and
       counted as ``local-deleted=``, not a failure; a PATH given literally that names
       one is still restored).
     - ``0`` all transferred/skipped (including the "index empty, run refresh first"
       early-exit message -- which since 2.1.0 appears only when the remote has no
       manifest to seed the index from -- and a pattern that matched nothing, which is
       reported and skipped); ``1`` one or more files failed to transfer, a pattern
       matched nothing under ``--strict``, or a Drive/lock error; ``2`` usage error.
   * - ``offload``
     - Delete local bytes of protonfs-tracked files confirmed present on Drive.
       Argument: ``PATH...`` (optional, repeatable; e.g. from a shell glob). Options: ``--no-verify``, ``--dry-run``,
       ``--yes``, ``--min-age DURATION`` (added in 2.2.0; default ``1d``): a file
       modified more recently than this is never offloaded.
     - ``0`` success (files that could not be verified, have unsynced edits, or are
       not yet settled are reported and left untouched -- this is not treated as
       failure); ``1`` Drive/lock error or declined confirmation; ``2`` usage error.
   * - ``prune``
     - Push, then offload by a retention policy: per directory keep the newest
       ``--keep`` tracked files, and offload any other file unmodified for
       ``--min-age``. Every deletion goes through ``offload``. Argument: ``PATH...``
       (optional, repeatable). Options: ``--keep N`` (default ``10``), ``--min-age
       DURATION`` (default ``1d``), ``--no-push``, ``--dry-run``, ``--yes``. Added in
       2.2.0.
     - ``0`` success (files offload refuses are reported, not failed); ``1`` the
       push that runs first failed for a file, a Drive/lock error, or a declined
       confirmation; ``2`` usage error.
   * - ``rm``
     - Remove a file/directory from Drive (trash by default, ``-f`` for permanent).
       Argument: ``PATH...`` (one or more required). Options: ``-r``/``--recursive``,
       ``-f``/``--force``, ``--yes``.
     - ``0`` success (including the "duplicate basenames in trash" case, which is
       reported, not failed); ``1`` not a directory without ``-r``, Drive/lock error, or
       declined confirmation; ``2`` usage error.
   * - ``restore``
     - Restore a trashed file/directory on Drive. Argument: ``PATH...`` (one or more required).
       If proton-drive can't disambiguate the requested item from a same-named trash
       entry (#56), the error points at ``protonfs trash list``/``protonfs trash
       empty`` to resolve it.
     - ``0`` success; ``1`` Drive/lock error (including that ambiguity); ``2`` usage
       error.
   * - ``trash list``
     - List every item in ``/trash``: name, original parent (best-effort), and how
       many other trashed items share the same name -- the ambiguity ``restore``
       can refuse to resolve on its own (#56).
     - ``0`` success; ``1`` Drive error; ``2`` usage error.
   * - ``trash empty``
     - Permanently empty ``/trash`` for the whole account (irreversible, and NOT
       scoped to this repo's ``remote_root``). Option: ``--yes``. Without it, a
       user must type an exact confirmation phrase; anything else aborts.
       Deliberately does not support deleting individual trashed items by UID --
       proton-drive does not accept UIDs for ``/trash`` paths (#56).
     - ``0`` success; ``1`` Drive error or declined/mismatched confirmation; ``2``
       usage error.
   * - ``refresh``
     - Discover remote files and seed the local index (metadata-only). Argument:
       ``PATH`` (optional). Option: ``--prune``.
     - ``0`` success; ``1`` Drive/lock error; ``2`` usage error.
   * - ``verify``
     - Check the remote manifest (``<remote_root>/.protonfs/manifest.json``) against a
       full listing of the remote; ``--repair`` rewrites it to match (and is the only
       way one is created). Options: ``--repair``, ``--index`` (check the local index
       against per-directory listings instead, reading no local file; with ``--repair``,
       apply what that proves). Added in 2.1.0; ``--index`` added in 2.4.0.
     - ``0`` every manifest entry matches Drive, there is no manifest, or ``--repair``
       rewrote it; ``1`` entries are missing from Drive or differ in size/sha1, the
       manifest cannot be read or written, or a Drive/lock error; ``2`` usage error.
       With ``--index``: ``0`` every index entry matches (with ``--repair``: every entry
       was checked and none was left untouched); ``1`` otherwise, or a Drive/lock error.
   * - ``install-drive``
     - Download and verify the official proton-drive CLI binary. Options:
       ``--version``, ``--skip-keyring``.
     - ``0`` success (installed, keyring warnings are non-fatal); ``1`` install
       failure or unusable keyring; ``2`` usage error.
   * - ``upgrade``
     - Upgrade proton-drive to this release's highest supported version (verify-first,
       atomic swap; never past ``highest_supported()`` -- a newer upstream is reported,
       not installed) and, inside a protonfs root, run pending repo-state migrations
       and re-verify an index recorded under an older check level against Drive (since
       2.4.0; a Drive failure there is reported, not fatal).
       Options: ``--check`` (preview, change nothing), ``--drive-only``, ``--repo-only``.
     - ``0`` success, or with ``--check``: fully current; ``1`` failure, or with
       ``--check``: an upgrade, migration or re-verification is due; ``2`` usage error (including
       ``--drive-only`` with ``--repo-only``).
   * - ``doctor``
     - Check this host can run proton-drive (binary, session bus, OS keyring) and
       version/state currency (support matrix, upstream advisory, index schema,
       pending migrations, config layering). Warn-level findings suggest
       ``protonfs upgrade`` without failing the exit code. Option: ``--fix``.
     - ``0`` every check passed (warnings allowed); ``1`` at least one check failed;
       ``2`` usage error.
   * - ``shell-init``
     - Print shell exports so ``proton-drive`` run by hand sees the same keyring.
     - ``0`` always (nothing to fail on; prints zero or more ``export`` lines).
   * - ``completions``
     - Print, ``--install``, or ``--uninstall`` shell completion. Argument: ``SHELL``
       (choice: ``bash``/``zsh``/``fish``). Added in 1.5.0.
     - ``0`` success; ``2`` usage error (unknown shell, or both ``--install`` and
       ``--uninstall``).
   * - ``schedule``
     - Install/list/remove cron jobs running protonfs on a schedule. ``--add`` (with
       ``--every``/``--cron``/``--at``, ``--command [push|pull|sync|offload|prune]``,
       ``--path``, ``--resolve``, ``--strict``, ``--min-age``, ``--keep``,
       ``--deletions``, ``--no-restore``, ``--label``); ``--uninstall <id|index>`` (``-U``), ``--uninstall --all``; bare or
       ``--list`` lists. Added in 1.8.0. ``--path`` accepts a glob pattern (re-expanded
       by protonfs on every run) and ``--strict`` was added in 1.11.0. The ``offload``
       and ``prune`` commands, ``--min-age``/``--keep``, the job-conflict checks and
       one-job-at-a-time per repo were added in 2.2.0; ``--min-age`` on a ``push`` job in
       2.4.0; ``--deletions [keep|trash]`` (push and sync jobs) and ``--no-restore``
       (pull and sync jobs) in 3.0.0.
     - ``0`` success (including a job added with a conflict *warning*); ``2`` usage
       error (bad cadence, unknown id, not a repo, conflicting mode flags, an option
       the command does not take, or a job refused because it conflicts with one
       already scheduled).
   * - ``auth login`` / ``auth logout``
     - Passthrough to ``proton-drive auth <action>`` with inherited stdio. Argument:
       ``ACTION`` (choice: ``login``/``logout``/``status``).
     - Exit code is whatever ``proton-drive`` itself returns; ``1`` if the
       ``proton-drive`` binary is not installed; ``2`` usage error (unknown action).
   * - ``auth status``
     - Check for a valid session without invoking ``proton-drive``.
     - ``0`` authenticated; ``1`` not authenticated, or a keyring fault; ``2`` usage
       error.
   * - ``config get``
     - Print the resolved value of ``KEY`` across all layers. Argument: ``KEY``
       (required).
     - ``0`` success; ``1`` unknown key, key not set in any layer, or repo not set up;
       ``2`` usage error.
   * - ``config set``
     - Set ``KEY`` = ``VALUE`` in one config layer. Arguments: ``KEY``, ``VALUE``
       (both required). Options: ``--global``, ``--local``.
     - ``0`` success; ``1`` unknown key, ``--global``/``--local`` both given, or no
       shared config yet for the repo; ``2`` usage error.

Sync states
-----------
The state names printed by ``status``, emitted as ``status --format json`` keys,
shown by ``ls`` and accepted by ``ls --state`` are part of this contract, and so is
the exit-code class each one maps to. Without ``--remote`` no state says anything
checked on Drive; the states marked *remote view* are only produced by
``status --remote``/``ls --remote``.

.. list-table::
   :header-rows: 1
   :widths: 20 62 18

   * - State
     - Meaning
     - ``status`` exit class
   * - ``locally-indexed``
     - Local matches the index (and, with a remote view, the remote agrees).
       Renamed from ``synced`` in 2.0.0.
     - clean (``0``)
   * - ``metadata-only``
     - Indexed but deliberately not materialised on this machine.
     - clean (``0``)
   * - ``lfs-pointer``
     - An un-smudged git-LFS pointer stub; deliberately left alone.
     - clean (``0``)
   * - ``local-only``
     - Present locally, not in the index.
     - drift (``1``)
   * - ``local-deleted``
     - *Remote view.* Held by this machine, now gone locally, still listed on Drive.
     - drift (``1``)
   * - ``remote-only``
     - Absent locally, with the copy on Drive: listed on Drive and absent from the
       index (remote view), or held by this machine and now gone locally, as the
       index records (no remote view).
     - drift (``1``)
   * - ``local-modified`` / ``remote-modified``
     - *Remote view.* One side diverged from the index; the other did not.
     - drift (``1``)
   * - ``remote-changed`` / ``remote-deleted``
     - *Remote view.* A file not held locally whose remote copy moved or vanished.
     - drift (``1``)
   * - ``conflict``
     - Local diverged from the index with no remote view to attribute a direction.
     - conflict (``2``)
   * - ``both-modified``
     - *Remote view.* Local and remote both diverged from the index.
     - conflict (``2``)

Known keys for ``config get``/``config set``: :confval:`remote_root`,
:confval:`device_id`, :confval:`defaults.on_conflict`, :confval:`defaults.low_io`,
:confval:`defaults.event_log`, :confval:`defaults.progress_style`,
:confval:`defaults.manifest` (added in 2.1.0), :confval:`defaults.skip_thumbnails` (added
in 2.5.0), :confval:`defaults.deletions` (added in 3.0.0). Each is defined in the
Configuration
section of :doc:`reference/index`.

Config files and precedence
-----------------------------
Layered configuration, highest precedence first:

#. Environment variables (see below) -- always win, per-key.
#. ``.protonfs/config.local.json`` -- per-device, gitignored.
#. ``.protonfs/config.json`` -- per-repo shared, committed (the sync contract).
#. ``~/.config/protonfs/config.json`` -- global user defaults. ``$XDG_CONFIG_HOME``
   relocates the ``~/.config`` base; ``$PROTONFS_CONFIG`` overrides the full path
   outright.
#. Built-in defaults (``defaults.on_conflict=skip``, ``defaults.low_io=false``,
   ``defaults.manifest=true``, ``defaults.skip_thumbnails=false``,
   ``defaults.deletions=keep``,
   ``defaults.event_log=false``, ``defaults.progress_style=inline``).

``config get`` always reports the fully resolved value across all four layers.
``config set`` writes to exactly one layer: the shared repo file by default, or the
global/local file with ``--global``/``--local`` (mutually exclusive).

Environment variables
------------------------
.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Purpose
   * - :envvar:`PROTONFS_CONFIG`
     - Overrides the global config file path outright (points directly at the file).
   * - :envvar:`PROTONFS_REMOTE_ROOT`
     - Per-key override for the resolved :confval:`remote_root` config value.
   * - :envvar:`PROTONFS_DEVICE_ID`
     - Per-key override for the resolved :confval:`device_id` config value.
   * - :envvar:`PROTONFS_ON_CONFLICT`
     - Per-key override for the resolved :confval:`defaults.on_conflict` config value.
   * - :envvar:`PROTONFS_LOW_IO`
     - Per-key override for the resolved :confval:`defaults.low_io` config value (boolean:
       ``1``/``true``/``yes``/``on``).
   * - :envvar:`PROTONFS_EVENT_LOG`
     - Per-key override for the resolved :confval:`defaults.event_log` config value (boolean:
       ``1``/``true``/``yes``/``on``).
   * - :envvar:`PROTONFS_PROGRESS_STYLE`
     - Per-key override for the resolved :confval:`defaults.progress_style` config value
       (``inline``/``lines``).
   * - :envvar:`PROTONFS_MANIFEST`
     - Per-key override for the resolved :confval:`defaults.manifest` config value
       (boolean: ``1``/``true``/``yes``/``on``). Added in 2.1.0.
   * - :envvar:`PROTONFS_SKIP_THUMBNAILS`
     - Per-key override for the resolved :confval:`defaults.skip_thumbnails` config value
       (boolean: ``1``/``true``/``yes``/``on``). Added in 2.5.0.
   * - :envvar:`PROTONFS_NO_MANIFEST`
     - Set (to any truthy value) to switch every remote-manifest read and write off on
       this host, whatever :confval:`defaults.manifest` says. Added in 2.1.0.
   * - :envvar:`PROTONFS_DRIVE_BIN`
     - Path/name of the ``proton-drive`` binary to invoke, in place of the default.
   * - :envvar:`PROTONFS_DRIVE_CACHE_HOME`
     - Directory proton-drive's cache is kept under (passed to it as ``XDG_CACHE_HOME``;
       Linux). Added in 2.6.0.
   * - :envvar:`PROTONFS_DRIVE_VERSION`
     - Overrides the ``proton-drive`` version ``install-drive`` installs when
       ``--version`` is not passed.
   * - :envvar:`PROTONFS_DRIVE_SHA512`
     - Explicit SHA-512 to verify a ``proton-drive`` download against, required for
       versions/platforms without a built-in checksum pin.
   * - :envvar:`PROTONFS_LIST_TIMEOUT`
     - Timeout in seconds for a Drive listing call (default ``45``).
   * - :envvar:`PROTONFS_LIST_RETRIES`
     - Max retries for a Drive listing call (default ``4``).
   * - :envvar:`PROTONFS_LIST_BACKOFF`
     - Base backoff in seconds between Drive listing retries (default ``2``).
   * - :envvar:`PROTONFS_LIST_BACKOFF_CAP`
     - Cap in seconds on the Drive listing retry backoff (default ``60``).
   * - :envvar:`PROTONFS_BATCH_SIZE`
     - Per-key override for the resolved :confval:`defaults.batch_size` config value
       (integer, at least ``1``).
   * - :envvar:`PROTONFS_TRANSFER_TIMEOUT`
     - Minimum timeout in seconds for a Drive upload/download call (default ``300``).
   * - :envvar:`PROTONFS_TRANSFER_MIN_RATE`
     - Slowest transfer rate in bytes per second that still counts as progress; scales
       each upload/download call's timeout with its data (default ``524288``).
   * - :envvar:`PROTONFS_TRANSFER_RETRIES`
     - Max retries for a Drive upload/download call (default ``4``).
   * - :envvar:`PROTONFS_TRANSFER_BACKOFF`
     - Base backoff in seconds between Drive upload/download retries (default ``2``).
   * - :envvar:`PROTONFS_TRANSFER_BACKOFF_CAP`
     - Cap in seconds on the Drive upload/download retry backoff (default ``60``).
   * - :envvar:`PROTONFS_KEYRING_PASSWORD`
     - Supplies the password for the protonfs-owned keyring bootstrap, instead of
       generating one.
   * - :envvar:`PROTONFS_NO_KEYRING_BOOTSTRAP`
     - Set (to any truthy value) to disable protonfs's Secret Service/keyring
       bootstrap entirely; the caller is responsible for providing one.

Proton Drive support matrix
------------------------------
``protonfs`` states, as a checkable contract, which ``proton-drive`` CLI versions
each of its own releases supports. ``src/protonfs/install.py`` exposes this as
``SUPPORTED_DRIVE_VERSIONS`` (an explicit set of supported versions),
``highest_supported()`` (the version ``install-drive``/the upgrade command installs
-- always equal to ``DEFAULT_VERSION``), and ``is_supported(version)``. The
installed CLI's own version is available via ``DriveClient.drive_version()``, which
parses ``proton-drive version`` output (e.g. ``Proton Drive CLI
cli-drive@0.5.0+73e40d90``) down to the comparable semver ``"0.5.0"``.

.. list-table::
   :header-rows: 1
   :widths: 20 30 50

   * - protonfs release
     - Supported proton-drive versions
     - Notes
   * - 3.0.x
     - ``0.9.0`` (highest supported), ``0.8.0``, ``0.7.0``, ``0.6.0``, ``0.5.0``,
       ``0.4.6``
     - ``0.9.0`` is the version ``install-drive``/upgrade installs by default. The
       older versions remain installable via ``PROTONFS_DRIVE_VERSION`` for hosts that
       have not yet moved off them (each has pinned, verified checksums for every
       supported platform).
   * - 1.0.x
     - ``0.5.0`` (highest supported), ``0.4.6``
     - The first contracted matrix.

Upgrade policy
~~~~~~~~~~~~~~~~
A given protonfs release only ever upgrades ``proton-drive`` up to its own
``highest_supported()`` -- it will never install a ``proton-drive`` version newer
than that, even if one exists upstream. Picking up a newer upstream
``proton-drive`` release requires upgrading protonfs itself: a maintainer runs
``python .github/scripts/repin_proton_drive.py`` to independently verify and pin the
new version's checksums for every supported platform, adds it to
``SUPPORTED_DRIVE_VERSIONS``, and cuts a new protonfs release with that as its
``highest_supported()``. This keeps the installed ``proton-drive`` version always
within the range a given protonfs release was built and tested against.

``protonfs upgrade`` is the user-facing voice of this policy: it upgrades an
outdated binary to ``highest_supported()`` (SHA-512-verified before an atomic
swap), reports -- without installing -- when upstream's Stable release is ahead,
and runs pending repo-state migrations. ``protonfs upgrade --check`` previews all
of it, exiting ``0`` when fully current and ``1`` when an upgrade is available.

See also
---------
* :doc:`getting-started/index` for installation and first-run setup.
* :doc:`upgrading` for the upgrade story built on this support matrix.
* :doc:`getting-started/syncing` for the push/pull/status workflow this contract
  supports.
