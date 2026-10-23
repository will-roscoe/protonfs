Moving files from git into a store
===================================

.. Page contents are rendered by furo's right-hand sidebar; an explicit
.. `.. contents::` directive collides with it (furo TOC JS error), so it is omitted.

Generated files sometimes get committed and then outgrow git: rendered movies, reduced
data products, images. If the directory holding them is (or becomes) a protonfs store,
``protonfs import`` moves them out of git and into the store without ever leaving them
with no verified copy.

This walkthrough uses a research repository whose ``products/`` directory holds
committed outputs: ``*.reduced.fits`` files, ``*.mp4`` movies and ``*.webp`` images.
Large ``*.grids.fits`` files sit next to them, already gitignored.

1. Make the directory a store
------------------------------
A store may be any directory inside the repository; it does not have to be the top
level::

    cd products
    protonfs setup        # remote_root e.g. /my-files/myrepo/products

Commit the ``.protonfs/`` files ``setup`` creates (``config.json``, ``ignore``,
``include`` and the control ``.gitignore``) so other clones share the store's settings.
``import`` refuses a store whose ``.gitignore`` has uncommitted changes, so commit
before importing.

2. Preview
-----------
Quote the patterns so protonfs expands them. It matches them against the files git
tracks, so ``**/*.fits`` picks up the committed ``*.reduced.fits`` files and never the
gitignored ``*.grids.fits``::

    protonfs import --dry-run '**/*.reduced.fits' '**/*.mp4' '**/*.webp'

The dry run lists what push would upload, every line it would add to ``.gitignore``,
and the git commands it would run. If anything is refused (a file with uncommitted
changes, a git-LFS pointer stub, a file the store's ``.protonfs/ignore`` excludes), each
refusal is listed and the exit code is 1; fix them and preview again.

3. Import
----------
::

    protonfs import '**/*.reduced.fits' '**/*.mp4' '**/*.webp'

``import`` pushes the files, then reads each one and checks it against what push
recorded, the blob committed in ``HEAD`` and a fresh listing of Drive. Only when every
file passes does it touch git: it adds one line per file to a block in
``products/.gitignore``, runs ``git rm --cached`` on them and stages the
``.gitignore``. The summary ends with ``untracked=N``. If any file fails, nothing in
git changes and the failures are listed; running the command again uploads only what
is missing.

4. Commit
----------
::

    git status            # the files show as deleted from the index, still on disk
    git commit -m "chore: move 130 file(s) from git into protonfs"

5. Other clones
----------------
When another clone pulls this commit, git removes the imported files from its work
tree. Restore them from Drive there::

    git pull
    cd products && protonfs pull

Until then that clone's ``push`` counts them as ``local-deleted`` and leaves them on
Drive, even with ``--deletions trash``.

6. History
-----------
The files are still in git history, so the repository is no smaller yet. Removing them
from history is a separate step with ``git filter-repo``, which protonfs does not run.
Do it only once the import commit is pushed and every clone that needs the files has
pulled them from Drive.

7. Moving a file back into git
-------------------------------
``protonfs export`` is the reverse, for a file that should be versioned again::

    cd products
    protonfs export --dry-run run01/frame0001.reduced.fits
    protonfs export run01/frame0001.reduced.fits
    git commit -m "chore: move 1 file(s) from protonfs into git"

It downloads the file if it was offloaded, checks it is the version Drive holds (push
it first if you changed it locally), removes its line from the import block in
``.gitignore``, stages it, and adds a line for it to ``.protonfs/ignore`` so no machine
syncs it any more. The Drive copy is kept; add ``--trash-remote`` to move it to the
Drive trash once git has the file.

On other clones, run ``protonfs push`` before pulling the commit: git replaces their
copy of the file with the committed one.
