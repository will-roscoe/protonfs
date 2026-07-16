Getting Started
===============

.. contents:: Contents
   :local:
   :depth: 1

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

.. Quick Start
.. -----------


.. Configuration
.. -------------

