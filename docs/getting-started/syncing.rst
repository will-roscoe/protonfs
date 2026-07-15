Syncing across machines
=======================

.. contents:: Contents
   :local:
   :depth: 1

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

A ready-made ``.protonfs/.gitignore`` that ignores ``index.json`` while keeping
``config.json`` and ``ignore`` tracked makes this split correct by default. If
the managed directory lives inside a git-LFS repo, also add a
``.protonfs/.gitattributes`` exempting these small control files from LFS, so a
clone without an LFS pull gets the real config rather than pointer stubs.

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
