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
