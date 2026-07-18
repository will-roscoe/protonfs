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

.. confval:: device_id
   :type: str

   Stable identifier for this client device in the index. Overridable via
   :envvar:`PROTONFS_DEVICE_ID`.

.. confval:: defaults.on_conflict
   :type: str
   :default: "skip"

   Action when a file is in :class:`~protonfs.diff.SyncState` conflict. Overridable
   via :envvar:`PROTONFS_ON_CONFLICT`.

.. confval:: defaults.low_io
   :type: bool
   :default: false

   Skip hashing unchanged files. Overridable via :envvar:`PROTONFS_LOW_IO`.

.. confval:: defaults.event_log
   :type: bool
   :default: false

   Enable the structured rotating event log. Overridable via :envvar:`PROTONFS_EVENT_LOG`.

.. confval:: defaults.progress_style
   :type: str
   :default: "inline"

   Progress display style, ``inline`` or ``lines``. Overridable via
   :envvar:`PROTONFS_PROGRESS_STYLE`.

Environment variables
---------------------

.. envvar:: PROTONFS_CONFIG

   Overrides the global config file path outright.

.. envvar:: PROTONFS_REMOTE_ROOT

   Per-key override for :confval:`remote_root`.

.. envvar:: PROTONFS_DEVICE_ID

   Per-key override for :confval:`device_id`.

.. envvar:: PROTONFS_ON_CONFLICT

   Per-key override for :confval:`defaults.on_conflict`.

.. envvar:: PROTONFS_LOW_IO

   Per-key override for :confval:`defaults.low_io`.

.. envvar:: PROTONFS_EVENT_LOG

   Per-key override for :confval:`defaults.event_log`.

.. envvar:: PROTONFS_PROGRESS_STYLE

   Per-key override for :confval:`defaults.progress_style`.
