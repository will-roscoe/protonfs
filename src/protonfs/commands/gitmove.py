"""Move files between git and a protonfs store (#205).

:func:`import_from_git` takes files git tracks under a store, pushes them, verifies each
one on Drive and against the blob committed in ``HEAD`` from a fresh read, and only then
stops git tracking them: it adds a literal ignore line per file to the store's
``.gitignore`` (:mod:`protonfs.importblock`), runs ``git rm --cached``, and stages the
result. It never commits, never touches history and never changes a local file; git is
changed for every path or for none.

.. code-block:: python

    ctx = load_context()
    result = import_from_git(ctx, ["run01/frame0001.reduced.fits"])
    if result.failures or result.refusals:
        ...  # nothing in git changed

.. versionadded:: 3.1.0
"""

from __future__ import annotations

import posixpath
import shutil
from dataclasses import dataclass

from protonfs import gitops, importblock
from protonfs.context import RepoContext
from protonfs.ignore import IgnoreMatcher, ignore_path
from protonfs.lfs import is_pointer_stub


@dataclass
class Refusal:
    """A reason an import cannot start.

    :ivar path: the store-relative path refused, or ``"."`` for the whole store.
    :ivar reason: what is wrong, in words a user can act on.
    """

    path: str
    reason: str


def _ignore_pattern(root, rel: str) -> str | None:
    """The last active ``.protonfs/ignore`` pattern that excludes ``rel``, if any."""
    import pathspec

    path = ignore_path(root)
    if not path.exists():
        return None
    hit = None
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            if pathspec.GitIgnoreSpec.from_lines([line]).match_file(rel):
                hit = line.strip()
    return hit


def _inside_store(rel: str) -> bool:
    if rel.startswith("/") or not rel:
        return False
    norm = posixpath.normpath(rel)
    return norm != ".." and not norm.startswith("../")


def check_import(ctx: RepoContext, paths: list[str]) -> list[Refusal]:
    """Check every precondition of importing ``paths``, collecting every failure.

    A path is refused when it is outside the store or inside ``.protonfs/``; not tracked
    by git; has staged or unstaged changes or is deleted from the work tree; is a git-LFS
    pointer stub; or is excluded by ``.protonfs/ignore`` or missed by an active
    ``.protonfs/include``. The whole store (``"."``) is refused when git is missing or the
    store is not in a git work tree; ``.gitignore`` is refused when it has any
    uncommitted change (staged, unstaged or untracked), since import stages it.

    :param ctx: the loaded store context.
    :param paths: store-relative file paths.
    :returns: every refusal; empty when the import may go ahead.
    :raises protonfs.gitops.GitError: when a git query itself fails.
    """
    if shutil.which("git") is None:
        return [Refusal(".", "git is not installed")]
    if not gitops.in_work_tree(ctx.root):
        return [Refusal(".", "not inside a git work tree")]

    refusals: list[Refusal] = []
    candidates = []
    for rel in paths:
        if not _inside_store(rel):
            refusals.append(Refusal(rel, "outside the store"))
        elif posixpath.normpath(rel).split("/")[0] == ".protonfs":
            refusals.append(Refusal(rel, "inside .protonfs/ (protonfs's own files)"))
        else:
            candidates.append(posixpath.normpath(rel))

    tracked = gitops.tracked_files(ctx.root)
    gitignore = importblock.gitignore_path(ctx.root)
    if gitignore.exists() and (
        ".gitignore" not in tracked or gitops.dirty_paths(ctx.root, [".gitignore"])
    ):
        refusals.append(
            Refusal(".gitignore", ".gitignore has uncommitted changes; commit them first")
        )

    in_git = [rel for rel in candidates if rel in tracked]
    for rel in candidates:
        if rel not in tracked:
            refusals.append(Refusal(rel, "not tracked by git"))
    dirty = gitops.dirty_paths(ctx.root, in_git)
    matcher = IgnoreMatcher.from_file(ctx.root)
    for rel in in_git:
        if rel in dirty:
            refusals.append(Refusal(rel, "has uncommitted changes; commit them first"))
        elif is_pointer_stub(ctx.root / rel):
            refusals.append(
                Refusal(rel, "is a git-LFS pointer stub; run `git lfs pull` first")
            )
        elif matcher.matches(rel):
            pattern = _ignore_pattern(ctx.root, rel)
            refusals.append(
                Refusal(
                    rel,
                    f"excluded by .protonfs/ignore pattern {pattern}"
                    if pattern
                    else "not matched by .protonfs/include",
                )
            )
    return refusals
