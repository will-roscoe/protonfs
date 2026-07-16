Stability Promise (M4.1)
=========================

.. contents:: Contents
   :local:
   :depth: 1

This page freezes the ``protonfs`` command-line surface as of 1.0: every command,
option/argument name, exit code, config file location, config key, and environment
variable listed here is a stable public contract. A caller (human or script) that
depends only on what is documented on this page will keep working across ``1.x``
releases.

Versioning policy
------------------
``protonfs`` computes its next version from Conventional Commits (see
``.github/scripts/compute_next_version.py``): a ``feat`` commit bumps **minor**, a
``fix`` bumps **patch**, and a breaking commit (``type!:`` or a ``BREAKING CHANGE:``
footer) bumps **major** -- *once the project is at 1.0 or later*. Before 1.0, that
same breaking-change signal is demoted to a **minor** bump instead, so day-to-day 0.x
development never jumps to 1.0.0 by accident. ``v1.0.0`` itself is therefore always a
deliberate, manually-created tag, not something the automated release pipeline produces
on its own.

From ``v1.0.0`` onward, any change that breaks something documented on this page --
removing or renaming a command/option, changing an exit code's meaning, moving a config
file, dropping an environment variable -- is a breaking change and requires that manual
major-version bump. Additive changes (a new command, a new optional flag, a new config
key with a backward-compatible default) remain minor/patch as normal.

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
     - Summarize sync state (counts by local-only/remote-only/synced/conflict).
       Argument: ``PATH`` (optional).
     - ``0`` clean (synced or intentionally remote-only); ``1`` drift present
       (something to push/pull/prune); ``2`` conflict present (needs a human or
       ``--resolve``). Conflict outranks drift when both are present. (Usage errors
       also use ``2``, but status's own ``2`` is a data outcome, not a usage error.)
   * - ``ls``
     - List tracked files with their sync state. Argument: ``PATH`` (optional).
       Options: ``--remote``, ``--trash``.
     - ``0`` success; ``1`` Drive/auth error; ``2`` usage error.
   * - ``push``
     - Upload local-only/changed files to Drive. Argument: ``PATH`` (optional).
       Options: ``--resolve [merge|keep-both|replace|skip]``, ``--dry-run``.
     - ``0`` all transferred/skipped; ``1`` one or more files failed to transfer, or a
       Drive/lock error; ``2`` usage error.
   * - ``pull``
     - Download remote-only/changed files from Drive. Argument: ``PATH`` (optional).
       Options: ``--resolve [remote|local|both]``, ``--dry-run``, ``--refresh``.
     - ``0`` all transferred/skipped (including the "index empty, run refresh first"
       early-exit message); ``1`` one or more files failed to transfer, or a
       Drive/lock error; ``2`` usage error.
   * - ``offload``
     - Delete local bytes of protonfs-tracked files confirmed present on Drive.
       Argument: ``PATH`` (optional). Options: ``--no-verify``, ``--dry-run``,
       ``--yes``.
     - ``0`` success (files that could not be verified or have unsynced edits are
       reported and left untouched -- this is not treated as failure); ``1`` Drive/lock
       error or declined confirmation; ``2`` usage error.
   * - ``rm``
     - Remove a file/directory from Drive (trash by default, ``-f`` for permanent).
       Argument: ``PATH`` (required). Options: ``-r``/``--recursive``,
       ``-f``/``--force``, ``--yes``.
     - ``0`` success (including the "duplicate basenames in trash" case, which is
       reported, not failed); ``1`` not a directory without ``-r``, Drive/lock error, or
       declined confirmation; ``2`` usage error.
   * - ``restore``
     - Restore a trashed file/directory on Drive. Argument: ``PATH`` (required).
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
   * - ``install-drive``
     - Download and verify the official proton-drive CLI binary. Options:
       ``--version``, ``--skip-keyring``.
     - ``0`` success (installed, keyring warnings are non-fatal); ``1`` install
       failure or unusable keyring; ``2`` usage error.
   * - ``doctor``
     - Check this host can run proton-drive (binary, session bus, OS keyring).
       Option: ``--fix``.
     - ``0`` every check passed; ``1`` at least one check failed; ``2`` usage error.
   * - ``shell-init``
     - Print shell exports so ``proton-drive`` run by hand sees the same keyring.
     - ``0`` always (nothing to fail on; prints zero or more ``export`` lines).
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

Known keys for ``config get``/``config set``: ``remote_root``, ``device_id``,
``defaults.on_conflict``, ``defaults.low_io``.

Config files and precedence
-----------------------------
Layered configuration, highest precedence first:

#. Environment variables (see below) -- always win, per-key.
#. ``.protonfs/config.local.json`` -- per-device, gitignored.
#. ``.protonfs/config.json`` -- per-repo shared, committed (the sync contract).
#. ``~/.config/protonfs/config.json`` -- global user defaults. ``$XDG_CONFIG_HOME``
   relocates the ``~/.config`` base; ``$PROTONFS_CONFIG`` overrides the full path
   outright.
#. Built-in defaults (``defaults.on_conflict=skip``, ``defaults.low_io=false``).

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
   * - ``PROTONFS_CONFIG``
     - Overrides the global config file path outright (points directly at the file).
   * - ``PROTONFS_REMOTE_ROOT``
     - Per-key override for the resolved ``remote_root`` config value.
   * - ``PROTONFS_DEVICE_ID``
     - Per-key override for the resolved ``device_id`` config value.
   * - ``PROTONFS_ON_CONFLICT``
     - Per-key override for the resolved ``defaults.on_conflict`` config value.
   * - ``PROTONFS_LOW_IO``
     - Per-key override for the resolved ``defaults.low_io`` config value (boolean:
       ``1``/``true``/``yes``/``on``).
   * - ``PROTONFS_DRIVE_BIN``
     - Path/name of the ``proton-drive`` binary to invoke, in place of the default.
   * - ``PROTONFS_DRIVE_VERSION``
     - Overrides the ``proton-drive`` version ``install-drive`` installs when
       ``--version`` is not passed.
   * - ``PROTONFS_DRIVE_SHA512``
     - Explicit SHA-512 to verify a ``proton-drive`` download against, required for
       versions/platforms without a built-in checksum pin.
   * - ``PROTONFS_LIST_TIMEOUT``
     - Timeout in seconds for a Drive listing call (default ``45``).
   * - ``PROTONFS_LIST_RETRIES``
     - Max retries for a Drive listing call (default ``4``).
   * - ``PROTONFS_LIST_BACKOFF``
     - Base backoff in seconds between Drive listing retries (default ``2``).
   * - ``PROTONFS_LIST_BACKOFF_CAP``
     - Cap in seconds on the Drive listing retry backoff (default ``60``).
   * - ``PROTONFS_TRANSFER_TIMEOUT``
     - Timeout in seconds for a Drive upload/download call (default ``300``).
   * - ``PROTONFS_TRANSFER_RETRIES``
     - Max retries for a Drive upload/download call (default ``4``).
   * - ``PROTONFS_TRANSFER_BACKOFF``
     - Base backoff in seconds between Drive upload/download retries (default ``2``).
   * - ``PROTONFS_TRANSFER_BACKOFF_CAP``
     - Cap in seconds on the Drive upload/download retry backoff (default ``60``).
   * - ``PROTONFS_KEYRING_PASSWORD``
     - Supplies the password for the protonfs-owned keyring bootstrap, instead of
       generating one.
   * - ``PROTONFS_NO_KEYRING_BOOTSTRAP``
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
   * - 1.0.x
     - ``0.5.0`` (highest supported), ``0.4.6``
     - ``0.5.0`` is the version ``install-drive``/upgrade installs by default.
       ``0.4.6`` remains installable via ``PROTONFS_DRIVE_VERSION`` for hosts that
       have not yet moved off it (both have pinned, verified checksums for every
       supported platform).

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

See also
---------
* :doc:`getting-started/index` for installation and first-run setup.
* :doc:`getting-started/syncing` for the push/pull/status workflow this contract
  supports.
