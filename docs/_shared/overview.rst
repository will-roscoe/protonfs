.. Single source of truth for the project overview, shared by the docs homepage
.. (docs/index.rst, via `.. include::`) and README.md (via .github/scripts/sync_readme.py,
.. which converts this constrained rST to Markdown between the SYNC markers). Keep to the
.. subset the converter supports: section headers, paragraphs, ``inline literals``,
.. `text <url>`_ links, ``- `` bullet lists, and ``.. code-block::`` blocks. Do NOT use
.. :doc:/:ref: roles here -- they do not survive the Markdown conversion.

Sync a local directory tree with `Proton Drive <https://proton.me/drive>`_, via the
official `Proton Drive CLI <https://github.com/ProtonDriveApps/sdk/tree/main/cli>`_,
with conflict-aware push/pull and a local sync manifest.

Originally built to replace git-lfs as the storage layer for large, write-once
simulation output -- data that does not need version history, just somewhere durable to
live and a way to fetch it back on demand.

Why protonfs
------------

- **Conflict-aware push/pull** over a local sync manifest (``.protonfs/index.json``), so
  each machine knows what it has, what the remote has, and what diverged.
- **Verify-before-delete offload** -- reclaim local disk space only for files proven
  byte-for-byte present on Drive (via Proton's plaintext size/digest, not the encrypted
  size).
- **Headless-first**: a keyring/session-bus bootstrap and a ``doctor`` that diagnoses and
  repairs the Secret Service, so it works over SSH with no desktop.
- **Durable by design**: atomic index writes, an advisory repo lock, resumable refresh
  under API throttling, and SHA-512-pinned proton-drive binaries.
- **A frozen 1.0 command surface**: every command, option, exit code, and config key is a
  documented, stable contract.

Requirements
------------

- Python >= 3.9
- The ``proton-drive`` CLI binary -- install it with ``protonfs install-drive``, or supply
  your own on ``PATH`` / via ``PROTONFS_DRIVE_BIN``.

Install
-------

.. code-block:: bash

   pip install protonfs
   protonfs install-drive     # downloads + SHA-512-verifies the official proton-drive binary
   protonfs auth login        # opens a URL to authenticate (passthrough to proton-drive)

Quickstart
----------

.. code-block:: bash

   cd ~/my-project
   protonfs setup             # init .protonfs/, prompt for the Drive path to sync into
   protonfs push --dry-run    # preview what would upload (changes nothing)
   protonfs push              # upload local files to Drive
   protonfs status            # confirm everything is in sync (exit 0 == clean)

On a headless server, run ``protonfs doctor --fix`` before ``auth login`` to prepare the
keyring first.
