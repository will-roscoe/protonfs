API Reference
=============

Module overview
---------------

.. autosummary::

   protonfs.argv
   protonfs.batching
   protonfs.cli
   protonfs.config
   protonfs.context
   protonfs.diff
   protonfs.drive
   protonfs.ignore
   protonfs.index
   protonfs.install
   protonfs.lfs
   protonfs.localscan
   protonfs.locking
   protonfs.logs
   protonfs.migrations
   protonfs.refreshstate
   protonfs.reporting
   protonfs.secretservice

Class hierarchies
-----------------

Drive error tree — every Drive failure mode subclasses :class:`~protonfs.drive.DriveError`,
so a single ``except DriveError`` at the CLI boundary catches them all:

.. inheritance-diagram:: protonfs.drive.DriveThrottleError protonfs.drive.DriveAuthError
   protonfs.drive.DriveSecretsError
   :parts: 1

CLI group/command classes — the epilog mix-in and the position-independent flag group
layered onto Click's base classes:

.. inheritance-diagram:: protonfs.cli.PositionalFlagGroup protonfs.cli._EpilogGroup
   protonfs.cli._EpilogCommand
   :parts: 1

Full module documentation
-------------------------

.. toctree::
   :maxdepth: 2

   protonfs/modules
