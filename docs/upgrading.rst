Upgrading
==========

.. contents:: Contents
   :local:
   :depth: 1

Three things can be upgraded, and they upgrade differently: **protonfs itself**
(pip), the **proton-drive CLI binary** it drives (``protonfs upgrade``), and a
repo's **on-disk state** under ``.protonfs/`` (repo-state migrations, also run by
``protonfs upgrade``).

Upgrading protonfs itself
--------------------------
protonfs is a normal PyPI package::

    pip install --upgrade protonfs

Release history and per-version upgrade notes live in the repository's
`CHANGELOG.md <https://github.com/will-roscoe/protonfs/blob/main/CHANGELOG.md>`_.
Versioning follows the policy in :doc:`stability`: within ``1.x``, everything
documented on that page keeps working; a breaking change to any of it requires a
major-version bump.

Upgrading proton-drive
-----------------------
``protonfs upgrade`` brings the installed ``proton-drive`` binary to the **highest
version this protonfs release supports** -- and deliberately never further::

    protonfs upgrade --check    # preview: installed / highest supported / upstream stable
    protonfs upgrade            # do it

``--check`` changes nothing and exits ``0`` when fully current, ``1`` when an
upgrade or migration is available -- script-friendly for provisioning.

Why the cap? Each protonfs release ships pinned SHA-512 checksums for the
proton-drive builds it was actually tested against (the support matrix in
:doc:`stability`). A newer upstream release is unverified by definition: protonfs
has no pin for it and no behavioral testing against it, so ``upgrade`` reports it
-- ``upstream X exists but this protonfs supports at most Z; upgrade protonfs to
get X`` -- and installs nothing. Upgrading protonfs itself (above) is the path to
a newer proton-drive: the new release re-pins and re-tests, then its own
``upgrade`` moves the binary forward.

The binary swap is verify-first and atomic: the download is staged to a temporary
file, its SHA-512 checked against the pin, and only then swapped into place. A
failed download or checksum mismatch never leaves a broken ``proton-drive``
behind.

The upstream check needs the network, but fails soft: offline, ``upgrade`` still
installs the pinned version and just skips the "upstream is ahead" advisory.

Session caveat after a binary swap
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
After replacing the binary, ``upgrade`` verifies the existing Proton session
still works and tells you either way. Sessions normally survive an upgrade (the
session lives in the OS keyring, not the binary), but if the check fails::

    protonfs auth login

is all that's needed. On headless hosts, make sure the keyring environment is in
place first -- ``protonfs doctor`` diagnoses it.

Repo-state migrations
----------------------
The layout of ``.protonfs/`` has evolved since 0.2.0. Old repos keep working --
every consumer migrates what it reads on the fly -- but ``protonfs upgrade`` run
inside a protonfs root also brings the on-disk state itself current, in one
explicit, previewable step:

- **index schema**: pre-0.13 indexes (a bare ``{path: entry}`` document, or an
  older ``schema_version``) are re-saved at the current schema.
- **device_id relocation**: 0.2.0-era repos carried ``device_id`` in the shared,
  committed ``config.json``; it belongs in the per-device, gitignored
  ``config.local.json``.
- **control-file backfill**: ``.protonfs/ignore``, ``include``, and the control
  ``.gitattributes``/``.gitignore`` that newer releases create at setup are
  backfilled where missing.

``protonfs upgrade --check`` lists pending migrations without applying anything
-- dry-run first on a repo you care about. Migrations are idempotent (running
twice is a no-op), probe actual on-disk state rather than trusting a version
marker, and never touch anything outside ``.protonfs/``. ``protonfs doctor``
also reports pending migrations, index-schema staleness, and support-matrix
currency as warn-level checks.

Scoping flags
--------------
- ``protonfs upgrade --drive-only`` -- just the binary, skip migrations.
- ``protonfs upgrade --repo-only`` -- just the migrations, skip the binary
  (errors when not inside a protonfs root).

See also
---------
* :doc:`stability` -- the support matrix, upgrade policy, and the frozen command
  surface (including ``upgrade``'s exact exit codes).
* :doc:`reference/index` -- full ``upgrade`` command reference.
* `CHANGELOG.md <https://github.com/will-roscoe/protonfs/blob/main/CHANGELOG.md>`_
  -- per-release upgrade notes.
