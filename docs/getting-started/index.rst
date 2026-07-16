Getting Started
===============

.. Page contents are rendered by furo's right-hand sidebar; an explicit
.. `.. contents::` directive collides with it (furo TOC JS error), so it is omitted.

Installation
------------

.. code-block:: bash

   pip install protonfs


Post Installation Setup
-----------------------
`protonfs` also requires the `proton-drive <https://proton.me/download/drive/cli/index.html>`_
CLI. The latest compatible version can be installed via 

.. code-block:: bash

   protonfs install-drive

Alternatively you can supply the binary yourself by passing it via `PROTONFS_DRIVE_BIN` env variable, or ensuring it is added to `PATH`.

Supported platforms
~~~~~~~~~~~~~~~~~~~

``install-drive`` installs a checksum-pinned official ``proton-drive`` prebuilt for:

- **linux-x64** (requires AVX2; instructive build-from-source fallback otherwise)
- **linux-arm64**
- **macOS x64 / arm64** (darwin)

**Windows:** the supported path is WSL, which behaves exactly like linux-x64.
Native Windows is out of scope for 1.0 — upstream publishes Windows prebuilts,
but protonfs itself (Secret Service keyring integration, POSIX path handling) is
untested there.

Other builds published upstream (e.g. musl variants) can be installed by setting
``PROTONFS_DRIVE_SHA512`` to the official checksum from
`version.json <https://proton.me/download/drive/cli/version.json>`_ — protonfs
never installs a binary it cannot verify.

You should then log in to proton drive if you have not already done so.
Follow the standard proton-drive auth instructions or login interactively using :code:`protonfs auth login`.

Configuration
-------------
``protonfs setup`` writes ``.protonfs/config.json`` (the shared, committed remote
root and defaults) and ``.protonfs/ignore``. Config is layered — environment
variable, then per-device ``.protonfs/config.local.json``, then the shared
``.protonfs/config.json``, then a global ``~/.config/protonfs/config.json``, then
a built-in default — and read/written with :code:`protonfs config get/set`. See
:doc:`../reference/index` for the ``config`` command and :doc:`../stability` for
the full precedence list and environment variables.

By default every file under scope is synced, minus anything matched by
``.protonfs/ignore``. To sync *only* certain file types instead, add an
allowlist at ``.protonfs/include`` (same gitignore syntax); see the "Scoping
what gets synced" section of the project README for details.

Next steps
----------
* :doc:`syncing` — the day-to-day push/pull/status workflow, and how multiple
  machines stay in sync.
* :doc:`../reference/index` — every command's arguments, behavior, and examples.
* :doc:`../guarantees` — what protonfs guarantees about durability and how it
  resolves drift.

