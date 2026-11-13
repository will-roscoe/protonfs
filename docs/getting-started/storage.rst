Where has my storage gone?
==========================

.. Page contents are rendered by furo's right-hand sidebar; an explicit
.. `.. contents::` directive collides with it (furo TOC JS error), so it is omitted.

.. versionadded:: 3.4.0

``protonfs du`` shows how much Proton Drive space a remote path takes, from the whole
account (``/``) down to a single folder, including how much of it is sitting in the
trash. It only reads: deleting and emptying the trash stay with you, through the Proton
web app or a manual remote removal.

.. code-block:: bash

   protonfs du /                        # every section of the account, plus the trash total
   protonfs du /my-files/sph-dev -d 2   # two levels of breakdown, biggest first
   cd ~/sph-dev/sim && protonfs du      # inside a store: its remote location

What to point it at
-------------------

A ``PATH`` that starts with a Drive section (``/``, ``/my-files/...``,
``/devices/...``) is a remote path. Anything else is a local path inside a protonfs
store, translated to its remote location. It doesn't matter where you run the command
from, and the local folder doesn't have to exist (an offloaded folder works too):

.. code-block:: bash

   # from ~, with the store at ~/sph-dev/sim syncing to /my-files/sph-dev/sim
   protonfs du ~/sph-dev/sim/mload005   # -> /my-files/sph-dev/sim/mload005

With no ``PATH``, the current directory is used, which must be inside a store (at its
root or any subdirectory). A local path that is in no store is an error.

What the columns mean
---------------------

Size
   The encrypted size Proton charges against your plan (``totalStorageSize``), slightly
   larger than the file's own byte count.
%Parent / %Used / %Plan
   The share of the parent folder, of all the Drive storage ``du`` measured, and of your
   plan. ``%Plan`` needs your plan size, which Proton's CLI cannot report. Set the
   **shared** total that Mail, Drive and the rest draw on:
   ``protonfs config set --global du.plan_size 3TB``. ``%Used`` covers Drive only,
   because ``du`` cannot see Mail.
In trash
   Bytes inside this folder that are in the trash. Trashed items keep counting against
   your plan, and keep counting in their original folders' totals, until the trash is
   emptied. The column is blank until ``protonfs du /`` has measured the trash once.
Age
   How old the figure is. Figures come from a local cache (see below), so ``du`` always
   shows how old each one is.

The rows under a folder are its files and subfolders, biggest first. Whatever the
folder's total holds beyond them appears as one extra row:

``(in trash)``
   Measured trash: everything that went into the remainder was fetched together.
``(unattributed, incl. excluded)``
   A folder you excluded (``-x``/``-X``) has no figure, so the remainder holds its bytes
   as well as any trash.
``(unreconciled)``
   The figures were fetched at different times (or the Drive changed while they were
   being fetched), so they cannot be subtracted honestly. Run again with
   ``--refresh 0`` to fetch them together.

At ``/``, ``du`` also measures the whole trash. A trashed folder can only be measured
through the folder it was trashed from, so a figure ``du`` could not complete is shown as
a lower bound with how much it could not check, e.g. ``≥ 12.4 GiB (190.0 GiB / 13%
unchecked)``.

Caching and ``--refresh``
-------------------------

Listing a Drive folder costs about 2.5 s plus 40 ms per entry. A folder of 20,000 files
takes over ten minutes, and a full walk of a 1.8 TB Drive took three and a half hours.
Sizing a folder takes 1–4 s whatever it holds. So ``du`` sizes rather than walks,
lists only what it is asked to break down, and keeps everything in a per-machine cache
(:envvar:`PROTONFS_DU_CACHE`, default ``~/.cache/protonfs/du.sqlite``).

Proton rate-limits sizing, though: about 20 folders in a row, then a pause of a few
minutes. ``du`` sizes one folder at a time and waits the pauses out, so the first
breakdown of a folder with hundreds of subfolders is slow (163 simulation folders took
72 minutes). It prints a notice when this happens. Every size is kept as soon as it is
measured, so you can press Ctrl-C and run the same command later to carry on where it
stopped.

A figure older than the refresh threshold is re-checked when it is used. The threshold
is checked every time a figure is used, so in a long-running GUI a folder you revisit
after the threshold has passed is re-checked then:

``--refresh 5d`` (the default, or ``du.refresh`` in the global config)
   Re-check anything older than five days. Durations combine units: ``2h45m``, ``1w``,
   ``1mo`` (30 days), ``1y`` (365 days); ``m`` is minutes.
``--refresh 0``
   Fresh values only: everything shown is fetched now.
``--refresh never``
   Answer from the cache alone, with no Drive calls at all.

Re-checking a stale folder costs two calls: its total is re-sized and it is re-listed
(a listing is the only way to see what moved to the trash). Each subfolder is re-sized
only if its own figure is older than the threshold. When a folder's figures come from
different checks, its remainder is shown as ``(unreconciled)`` rather than ``(in
trash)``, because something may have changed in between; ``--refresh 0`` re-sizes
everything shown so the remainder can be attributed again.

Excluding folders
-----------------

Patterns use gitignore syntax and are matched below the target. ``some-dir`` and
``some-dir/`` both match any folder of that name at any depth, and a leading ``/``
anchors a pattern to the target.

``-x PATTERN`` / ``--exclude-updates PATTERN``
   Never contact Drive for matching folders. They still appear, with their cached figures
   and age, or with empty size cells if nothing is cached.
``-X PATTERN`` / ``--exclude PATTERN``
   Leave matching folders out entirely.

Depth, output and exit codes
----------------------------

``-d N`` limits the breakdown to depth ``N`` (the target is depth 0; the default is 1),
``-d MIN-MAX`` shows a range, and ``--mindepth``/``--maxdepth`` are the long forms.
``-D``/``--dirs-only`` hides files. ``--format table|plain|json`` (``--json`` for short)
chooses the output, and ``-p``/``--show-progress`` reports progress on stderr.

``du`` exits ``0`` when everything it was asked for was produced, ``1`` when it could not
produce its output, ``2`` for a usage error, and ``3`` when the output is complete but
some figures could not be fetched or the trash total is partial.

Browsing it in a web page
-------------------------

``protonfs du --gui [PATH]`` serves a page that shows the same figures as a sortable
table next to a treemap or sunburst, so you can click your way down to whatever is
taking the space. It runs until you press Ctrl-C.

.. code-block:: bash

   protonfs du --gui /my-files/sph-dev
   protonfs du --gui /my-files/sph-dev -x process --refresh 2d   # seed the page's settings

The address to open is the last line ``du`` prints, e.g.
``http://127.0.0.1:43017/?t=…``; on a desktop it also opens in your browser. The ``t=``
part is a one-off access token, so keep the line to yourself: it is what stops other
programs and users on the machine from using the page.

``--listen ADDR``
   The address to serve on (default ``127.0.0.1``, this machine only). Anything else
   prints a warning: the page and its token travel over plain HTTP, so only listen
   beyond loopback on a network you trust, or reach the page through an SSH tunnel.
``--port N|auto``
   The port (default ``auto``: any free one). A port already in use is an error.
``--log DIR|FILE|DEVICE``
   Where request logs go (default ``~/.local/state/protonfs/logs/``, one file per run;
   ``$XDG_STATE_HOME`` is respected). A directory gets a new file named after the start
   time, a file is appended to, and a device such as ``/dev/null`` is written to as is.
   Log files are readable only by you, and the token is never written to them.

``-d``/``--maxdepth`` (how many levels the charts nest, default 3), ``-D``, ``-x``,
``-X`` and ``--refresh`` set the page's starting settings, which you can change on the
page. ``--format``/``--json``, ``-p`` and ``--mindepth`` make no sense for a page and
are rejected, as is more than one ``PATH``.

The page never waits on Drive: it shows what is cached straight away and fills in
fresh figures as they arrive. When Proton is rate-limiting folder sizes it says so.

See :doc:`../reference/index` for every option, and :doc:`../stability` for the
output contract.
