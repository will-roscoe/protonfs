What do I want to do?
=====================

.. Page contents are rendered by furo's right-hand sidebar; an explicit
.. `.. contents::` directive collides with it (furo TOC JS error), so it is omitted.

A task-first index into protonfs: find what you are trying to do, run the command,
and follow the worked walkthrough if you want the full picture. For the exact
behaviour of any command see :doc:`../reference/index`; for the frozen 1.0 contract
(exit codes, option names) see :doc:`../stability`.

Quick task index
----------------

.. list-table::
   :header-rows: 1
   :widths: 55 45

   * - I want to…
     - Run *(each links to its command reference)*
   * - Set up syncing in a directory for the first time
     - :ref:`protonfs setup <cmd-setup>`
   * - Check my keyring/CLI works on a headless server *before* logging in
     - :ref:`protonfs doctor <cmd-doctor>` (then ``--fix``)
   * - Log in to Proton
     - :ref:`protonfs auth login <cmd-auth>`
   * - Upload my local files to Drive
     - :ref:`protonfs push <cmd-push>`
   * - Download files that are on Drive but not here
     - :ref:`protonfs pull <cmd-pull>`
   * - See what would sync without doing it
     - :ref:`protonfs push --dry-run <cmd-push>` /
       :ref:`protonfs pull --dry-run <cmd-pull>`
   * - See the sync state of every tracked file
     - :ref:`protonfs status <cmd-status>` / :ref:`protonfs ls <cmd-ls>`
   * - See which directories use the most storage
     - :ref:`protonfs ls --dirs <cmd-ls>` (add
       :ref:`--visual treemap|waffle <cmd-ls>` for a chart)
   * - Get machine-readable output for a script
     - :ref:`protonfs status --format json <cmd-status>` /
       :ref:`protonfs ls --format json|plain <cmd-ls>`
   * - Learn about files on Drive I have never pulled
     - :ref:`protonfs refresh <cmd-refresh>`
   * - Free local disk space but keep the files on Drive
     - :ref:`protonfs offload <cmd-offload>`
   * - Remove a file from Drive (recoverably)
     - :ref:`protonfs rm PATH <cmd-rm>`
   * - Get back something I removed
     - :ref:`protonfs restore PATH <cmd-restore>`
   * - See or empty Drive's trash
     - :ref:`protonfs trash list <cmd-trash-list>` /
       :ref:`protonfs trash empty <cmd-trash-empty>`
   * - Exclude or force-include files from syncing
     - edit ``.protonfs/ignore`` / ``.protonfs/include``
       (see :ref:`Controlling what syncs <controlling-what-syncs>`)
   * - Update the proton-drive binary (safely)
     - :ref:`protonfs upgrade <cmd-upgrade>`
   * - Bring an old repo's ``.protonfs/`` state up to date
     - :ref:`protonfs upgrade <cmd-upgrade>` (migrations run automatically)
   * - Change a config value
     - :ref:`protonfs config set KEY VALUE <cmd-config>`
   * - Tear protonfs out of a directory
     - :ref:`protonfs deinit <cmd-deinit>`

Walkthrough: your first sync
----------------------------
Set up a directory, log in, and push its contents to Drive.

.. code-block:: bash

   cd ~/my-project

   # 1. Install/verify the proton-drive CLI, prepare the keyring, and create
   #    .protonfs/. You are prompted for the Drive path to sync into.
   protonfs setup
   #    Remote Drive root path for this repo: /my-files/my-project

   # 2. Authenticate (opens a URL; on a server, see the headless walkthrough below).
   protonfs auth login

   # 3. See what a push would upload — always safe, changes nothing.
   protonfs push --dry-run

   # 4. Upload.
   protonfs push

   # 5. Confirm everything is in sync (exit code 0 == clean).
   protonfs status

On another machine, run ``protonfs setup`` pointed at the **same** Drive path, then
``protonfs pull`` to bring the files down.

.. seealso:: :doc:`syncing` for the push/pull/status model in depth.

Walkthrough: a headless server (SSH, no desktop)
------------------------------------------------
proton-drive stores its session in the OS keyring, which over SSH usually has no
session bus and no unlocked Secret Service. Diagnose and repair that *before* logging
in, so a successful browser login is not thrown away.

.. code-block:: bash

   # 1. Diagnose the environment (binary, session bus, keyring). Read-only.
   protonfs doctor

   # 2. Repair what protonfs can — bootstraps a protonfs-owned session bus + keyring.
   protonfs doctor --fix

   # 3. Now the session has somewhere to live; log in.
   protonfs auth login

   # To run the raw `proton-drive` binary by hand in the same shell:
   eval "$(protonfs shell-init)"

.. note::
   Every ``protonfs`` command sets up this keyring environment for itself;
   ``shell-init`` is only needed for manual ``proton-drive`` invocations.

Walkthrough: free up local disk, keep the data on Drive
-------------------------------------------------------
``offload`` deletes the *local* bytes of files it can prove are already on Drive,
leaving the index entry so the file still shows up and can be pulled back.

.. code-block:: bash

   # 1. Make sure everything is uploaded first.
   protonfs push

   # 2. Preview what would be freed (verifies each file against the remote first).
   protonfs offload --dry-run

   # 3. Free the local copies. Files that cannot be verified present on Drive, or
   #    have unsynced local edits, are reported and left untouched.
   protonfs offload

   # Later, bring one (or everything) back:
   protonfs pull

.. warning::
   ``offload`` only deletes a local file after confirming a byte-for-byte match on
   Drive (via Proton's plaintext ``claimedSize``/digest, not the encrypted size).
   Pass ``--no-verify`` at your own risk.

Walkthrough: remove and restore
-------------------------------
.. code-block:: bash

   # Move a file to Drive's trash (recoverable).
   protonfs rm reports/old.csv

   # Changed your mind:
   protonfs restore reports/old.csv

   # If restore complains it cannot disambiguate a same-named trash entry (#56),
   # inspect the trash to see the duplicates:
   protonfs trash list

   # Permanently empty the trash (irreversible, account-global — typed confirmation):
   protonfs trash empty

.. seealso:: :doc:`../reference/index` for ``rm``'s ``-r``/``-f`` flags and the exact
   trash-resolution behaviour.

Walkthrough: keeping proton-drive and an old repo current
---------------------------------------------------------
.. code-block:: bash

   # Upgrade protonfs itself first (PyPI):
   pip install --upgrade protonfs

   # Preview: installed vs highest-supported proton-drive, plus any pending
   # repo-state migrations. Exits 1 if anything is out of date, 0 if current.
   protonfs upgrade --check

   # Apply: SHA-512-verified atomic binary swap + repo-state migrations.
   protonfs upgrade

.. seealso:: :doc:`../upgrading` for the full upgrade story and the support-matrix
   policy (why ``upgrade`` never installs a proton-drive newer than this release
   supports).

.. _controlling-what-syncs:

Controlling what syncs
----------------------
Two committed files under ``.protonfs/`` decide which files are in scope:

- ``.protonfs/ignore`` — gitignore-syntax exclusions (always wins).
- ``.protonfs/include`` — a gitignore-syntax **allowlist**: when present and
  non-empty, only matching files sync (and still never those matched by ``ignore``).

.. code-block:: bash

   # Sync only the phantom dump files under an otherwise-excluded tree:
   printf '%s\n' '*.ev' '*.sink' '*_[0-9][0-9][0-9][0-9][0-9]' > .protonfs/include

.. seealso:: :doc:`syncing` and the ``ignore``/``include`` notes in
   :doc:`../reference/index`.
