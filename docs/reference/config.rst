Configuration & environment (reference)
=======================================

Canonical definitions for every config key and environment variable. The
:doc:`../stability` page states which of these are frozen; this page defines them.
Keys are set with ``protonfs config set <key> <value>`` and read from the layered
config (env var > per-repo local > global user file).

Config keys
-----------

.. confval:: remote_root
   :type: str

   Proton Drive path that maps to this repo's ProtonFS root. Overridable via
   :envvar:`PROTONFS_REMOTE_ROOT`.

   .. versionadded:: 1.0.0

.. confval:: device_id
   :type: str

   Stable identifier for this client device in the index. Overridable via
   :envvar:`PROTONFS_DEVICE_ID`.

   .. versionadded:: 1.0.0

.. confval:: defaults.on_conflict
   :type: str
   :default: "skip"

   Action when a file is in :class:`~protonfs.diff.SyncState` conflict. Overridable
   via :envvar:`PROTONFS_ON_CONFLICT`.

   .. versionadded:: 1.0.0

.. confval:: defaults.low_io
   :type: bool
   :default: false

   Skip hashing unchanged files. Overridable via :envvar:`PROTONFS_LOW_IO`.

   .. versionadded:: 1.0.0

.. confval:: defaults.event_log
   :type: bool
   :default: false

   Enable the structured rotating event log. Overridable via :envvar:`PROTONFS_EVENT_LOG`.

   .. versionadded:: 1.3.0

.. confval:: defaults.progress_style
   :type: str
   :default: "inline"

   Progress display style, ``inline`` or ``lines``. Overridable via
   :envvar:`PROTONFS_PROGRESS_STYLE`.

   .. versionadded:: 1.3.0

Environment variables
---------------------

.. envvar:: PROTONFS_CONFIG

   Overrides the global config file path outright.

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_REMOTE_ROOT

   Per-key override for :confval:`remote_root`.

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_DEVICE_ID

   Per-key override for :confval:`device_id`.

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_ON_CONFLICT

   Per-key override for :confval:`defaults.on_conflict`.

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_LOW_IO

   Per-key override for :confval:`defaults.low_io`.

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_EVENT_LOG

   Per-key override for :confval:`defaults.event_log`.

   .. versionadded:: 1.3.0

.. envvar:: PROTONFS_PROGRESS_STYLE

   Per-key override for :confval:`defaults.progress_style`.

   .. versionadded:: 1.3.0

Operational & tuning environment variables
------------------------------------------

These have no config-key equivalent; they tune how protonfs invokes and installs the
``proton-drive`` binary and how it bootstraps the keyring. All are part of the frozen
contract (see :doc:`../stability`).

.. envvar:: PROTONFS_DRIVE_BIN

   Path/name of the ``proton-drive`` binary to invoke, in place of the default.

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_DRIVE_VERSION

   Overrides the ``proton-drive`` version ``install-drive`` installs when ``--version``
   is not passed.

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_DRIVE_SHA512

   Explicit SHA-512 to verify a ``proton-drive`` download against, required for
   versions/platforms without a built-in checksum pin.

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_LIST_TIMEOUT

   Timeout in seconds for a Drive listing call (default ``45``).

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_LIST_RETRIES

   Max retries for a Drive listing call (default ``4``).

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_LIST_BACKOFF

   Base backoff in seconds between Drive listing retries (default ``2``).

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_LIST_BACKOFF_CAP

   Cap in seconds on the Drive listing retry backoff (default ``60``).

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_TRANSFER_TIMEOUT

   Timeout in seconds for a Drive upload/download call (default ``300``).

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_TRANSFER_RETRIES

   Max retries for a Drive upload/download call (default ``4``).

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_TRANSFER_BACKOFF

   Base backoff in seconds between Drive upload/download retries (default ``2``).

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_TRANSFER_BACKOFF_CAP

   Cap in seconds on the Drive upload/download retry backoff (default ``60``).

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_KEYRING_PASSWORD

   Supplies the password for the protonfs-owned keyring bootstrap, instead of
   generating one.

   .. versionadded:: 1.0.0

.. envvar:: PROTONFS_NO_KEYRING_BOOTSTRAP

   Set (to any truthy value) to disable protonfs's Secret Service/keyring bootstrap
   entirely; the caller is responsible for providing one.

   .. versionadded:: 1.0.0
