Syncing across machines
=======================

.. Page contents are rendered by furo's right-hand sidebar; an explicit
.. `.. contents::` directive collides with it (furo TOC JS error), so it is omitted.

protonfs is designed so several machines can sync the same directory tree to the
same Proton Drive location. This page describes that workflow and the pitfalls
worth knowing.

The sync contract
-----------------

A protonfs-managed directory keeps its state in ``.protonfs/``:

- ``config.json`` — the **remote root** and defaults. Shared: commit it so every
  clone syncs to the same place.
- ``ignore`` — which files are in scope (gitignore syntax). Shared: commit it so
  every clone syncs the same set.
- ``index.json`` — this machine's record of what it has uploaded (local hashes,
  mtimes, remote paths). **Local only** — gitignore it; it is rebuilt per device
  and would only cause churn and false conflicts if shared.

``protonfs setup`` writes this split for you: a ``.protonfs/.gitignore`` that
ignores ``index.json`` (and the transient ``refresh-state.json``) while keeping
``config.json`` and ``ignore`` tracked, plus a ``.protonfs/.gitattributes`` that
exempts these small control files from git-LFS — so a clone without an LFS pull
gets the real config rather than pointer stubs. Both are written idempotently and
preserve any lines you add yourself.

Setting up a **subdirectory** of a larger git repo is safe: ``setup`` runs the
repo-wide git-LFS migration only when the protonfs root is the git toplevel. In a
subdirectory it skips migration (and leaves any git-LFS pointer files there
untouched); pass ``--migrate-lfs`` to force it or ``--no-migrate-lfs`` to always
skip. ``setup`` also creates the configured ``remote_root`` on Drive if it does
not exist yet (it must live under ``/my-files``), so the first ``push`` works
without hand-creating folders.

First-time setup on a client
----------------------------

.. code-block:: bash

   cd <managed-dir>
   git pull                 # get the shared contract (.protonfs/config.json + ignore)
   protonfs doctor          # verify the proton-drive binary + OS keyring (add --fix if headless)
   protonfs auth login      # only if not already authenticated
   protonfs refresh         # learn what is already on Drive -> seeds THIS machine's index

``refresh`` is the key cross-client primitive: metadata-only, no download, it
makes a fresh machine aware of everything already on Drive so it will not
re-upload what another client already sent.

Uploading data
--------------

.. code-block:: bash

   protonfs status                          # what is local-only vs synced
   protonfs push <subpath> --resolve replace  # a subtree
   protonfs push --resolve replace            # everything in scope that is new

.. important::

   Prefer ``--resolve replace`` (or another strategy) over a bare ``push``. With
   no strategy, ``proton-drive`` falls back to an interactive conflict prompt;
   when its output is captured (which protonfs always does) or the run is
   headless, that prompt auto-fails per file and those files are skipped — while
   the run may still report them as transferred. A strategy makes uploads
   non-interactive and idempotent.

Verifying an upload
-------------------

``proton-drive`` can under-deliver silently, so confirm the delivered count
against local before relying on an upload (for example, before deleting local
copies to reclaim space):

.. code-block:: bash

   eval "$(protonfs shell-init)"     # put proton-drive on PATH with the keyring env
   remote=$(proton-drive filesystem list <remote-root>/<subpath> --json \
            | grep -c '"type":"file"')
   local=$(find <subpath> -type f | wc -l)
   echo "remote=$remote local=$local"
   # if short, re-push (replace makes it idempotent):
   protonfs push <subpath> --resolve replace

Removing files
--------------

.. code-block:: bash

   protonfs rm <path>        # trash the remote copy (reversible)
   protonfs rm -f <path>     # trash, then permanently delete

``rm -f`` has one **permanent limitation**. proton-drive addresses a trashed
node for permanent deletion by its path under ``/trash`` (``/trash/<basename>``),
and offers no working way to target a specific trashed node by its stable UID.
So when two or more trashed items share a basename, protonfs cannot safely tell
which one is yours and **refuses to permanently delete** — it leaves the item
trashed (still reversible) and tells you so.

To resolve a duplicate-basename case, empty that specific item from trash via the
Proton Drive app or web UI, or just leave it trashed (trash is reversible, so
nothing is lost). This is an upstream constraint, not a protonfs choice; a live
probe test flags it automatically if a future proton-drive lifts it.

Keeping clients in sync
-----------------------

The standard loop on any client:

.. code-block:: bash

   git pull                          # shared contract
   protonfs refresh                  # reconcile local index with Drive
                                     #   (--prune drops entries for files deleted on Drive)
   protonfs status                   # local-only / remote-only / synced / conflict
   protonfs push --resolve replace   # send local-only up
   protonfs pull --refresh           # bring remote-only down (if this machine wants them)

Resolving a divergence on pull
------------------------------

A file that was edited **locally** *and* changed on the **remote** since the last
sync is a divergence. A bare ``pull`` never touches such a file — it leaves it in
place, reports it, and exits non-zero — so a local edit is never silently
overwritten. Choose a side explicitly with ``--resolve``:

.. code-block:: bash

   protonfs pull --resolve remote <path>   # overwrite the local copy with the remote one
   protonfs pull --resolve local <path>    # keep local (it stays queued for the next push)
   protonfs pull --resolve both <path>     # fetch the remote copy as <name>.remote to merge

``--resolve=both`` writes the remote version alongside your file under a
``.remote`` suffix (untracked) so you can diff and merge by hand, then delete the
suffixed copy. Files that changed only on the remote (your local copy is still in
sync) are brought down normally by ``pull --resolve <any>``; no local edit is at
risk there.

``status`` also sets an **exit code** so an unattended caller can branch without
parsing the printed counts:

- ``0`` — clean: every file is synced or intentionally remote-only (nothing to reconcile).
- ``1`` — drift: non-conflict divergence exists (something to push, pull, or prune).
- ``2`` — conflict: at least one file needs a human or a ``--resolve`` strategy.

Conflict outranks drift when both are present, e.g.:

.. code-block:: bash

   protonfs status; case $? in
     0) echo "in sync" ;;
     1) echo "drift -- run push/pull" ;;
     2) echo "conflict -- resolve first" ;;
   esac
