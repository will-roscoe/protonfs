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
from dataclasses import dataclass, field

from protonfs import gitops, importblock
from protonfs.context import RepoContext
from protonfs.drive import TransferResult
from protonfs.ignore import IgnoreMatcher, ignore_path
from protonfs.lfs import is_pointer_stub

#: File names that configure git itself; import never moves them out of git.
GIT_CONTROL_FILES = frozenset({".gitignore", ".gitattributes"})


@dataclass
class Refusal:
    """A reason an import cannot start.

    :ivar path: the store-relative path refused, or ``"."`` for the whole store.
    :ivar reason: what is wrong, in words a user can act on.

    .. versionadded:: 3.1.0
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

    A path is refused when it is outside the store or inside ``.protonfs/``; is a
    ``.gitignore`` or ``.gitattributes``; has a newline or carriage return in its name; is
    not tracked by git; has staged or unstaged changes or is deleted from the work tree;
    is a symlink or submodule in git; is tracked by git-LFS or is a git-LFS pointer stub;
    or is excluded by ``.protonfs/ignore`` or missed by an active ``.protonfs/include``.
    The whole store (``"."``) is refused when git is missing or the store is not in a
    git work tree; ``.gitignore`` is refused when it has any
    uncommitted change (staged, unstaged or untracked), since import stages it.

    :param ctx: the loaded store context.
    :param paths: store-relative file paths.
    :returns: every refusal; empty when the import may go ahead.
    :raises protonfs.gitops.GitError: when a git query itself fails.

    .. versionadded:: 3.1.0
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
        elif posixpath.basename(rel) in GIT_CONTROL_FILES:
            refusals.append(Refusal(rel, "is a git control file; it stays in git"))
        elif "\n" in rel or "\r" in rel:
            refusals.append(Refusal(
                rel, "name contains a newline or carriage return, which no .gitignore "
                "line can match exactly",
            ))
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
    modes = gitops.file_modes(ctx.root, in_git)
    lfs = gitops.attribute(ctx.root, in_git, "filter")
    matcher = IgnoreMatcher.from_file(ctx.root)
    for rel in in_git:
        if rel in dirty:
            refusals.append(Refusal(rel, "has uncommitted changes; commit them first"))
        elif modes.get(rel) == "120000":
            refusals.append(
                Refusal(rel, "is a symlink in git; import the file it points to instead")
            )
        elif modes.get(rel) == "160000":
            refusals.append(Refusal(rel, "is a git submodule"))
        elif lfs.get(rel) == "lfs":
            refusals.append(Refusal(
                rel, "is tracked by git-LFS; use `protonfs setup --migrate-lfs` for LFS files",
            ))
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


@dataclass
class ImportResult:
    """The outcome of :func:`import_from_git`.

    :ivar paths: the store-relative paths asked for.
    :ivar refusals: precondition failures (:func:`check_import`); when any, nothing ran.
    :ivar pushed: what the push of ``paths`` did (``None`` when refused).
    :ivar failures: ``{"name", "error", "kind"}`` per path that failed verification
        (``kind``: ``"unverified-local"``, ``"unverified-drive"``, ``"low-io-stale"``) or
        one ``kind="git"`` entry when the git step failed and was rolled back. When any,
        git was not changed.
    :ivar untracked: files git stopped tracking.
    :ivar gitignore_lines: the lines added (or, in a dry run, that would be added) to the
        store's ``.gitignore``.
    :ivar git_commands: the git commands run (or, in a dry run, that would run).
    :ivar trash_policy_warning: the store's shared config trashes local deletions
        (``defaults.deletions = "trash"``), which other clones should know.
    :ivar rolled_back: a git command failed and every git change was undone.
    :ivar total_bytes: the combined size of the files in ``paths``.

    .. versionadded:: 3.1.0
    """

    paths: list[str]
    refusals: list[Refusal] = field(default_factory=list)
    pushed: TransferResult | None = None
    failures: list[dict] = field(default_factory=list)
    untracked: int = 0
    gitignore_lines: list[str] = field(default_factory=list)
    git_commands: list[str] = field(default_factory=list)
    trash_policy_warning: bool = False
    rolled_back: bool = False
    total_bytes: int = 0


def _fail(rel: str, kind: str, error: str) -> dict:
    return {"name": rel, "error": error, "kind": kind}


def verify_imported(ctx: RepoContext, paths: list[str], reporter=None) -> list[dict]:
    """Verify each of ``paths`` is on Drive and is the blob committed in ``HEAD``.

    Every file is read once, whatever ``defaults.low_io`` says, and must match: its index
    entry's sha256 (so push recorded these bytes), the blob ``HEAD`` records for it (as
    is, or after the end-of-line conversion git applies to that path), and a
    fresh listing of its Drive directory (:func:`~protonfs.indexcheck.check_index` verdict
    ``OK``, and Drive's sha1 where Drive reports one). The fresh hashes are written to the
    hash cache.

    :param ctx: the loaded store context.
    :param paths: store-relative file paths, already pushed.
    :param reporter: :class:`~protonfs.reporting.Reporter` for the listing's progress.
    :returns: one ``{"name", "error", "kind"}`` dict per failed path; empty when every
        path verified.
    :raises protonfs.drive.DriveThrottleError: when Drive throttles the listing, and
        :class:`~protonfs.drive.DriveAuthError` when not logged in.

    .. versionadded:: 3.1.0
    """
    from protonfs.hashcache import HashCache
    from protonfs.indexcheck import Verdict, check_index
    from protonfs.localscan import hash_file_digests_with_blob

    blobs = gitops.head_blobs(ctx.root, paths)
    findings = {
        f.rel_path: f for f in check_index(ctx, reporter=reporter, only=set(paths)).findings
    }
    cache = HashCache(ctx.root)
    failures = []
    for rel in paths:
        entry = ctx.index.get(rel)
        if entry is None or entry.local_state != "present":
            failures.append(_fail(rel, "unverified-drive", "push did not verify it on Drive"))
            continue
        path = ctx.root / rel
        try:
            stat = path.stat()
            sha256, sha1, blob = hash_file_digests_with_blob(path)
        except OSError as exc:
            failures.append(_fail(rel, "unverified-local", f"cannot read it: {exc}"))
            continue
        cache.set(rel, stat.st_size, stat.st_mtime, sha256, sha1)
        finding = findings.get(rel)
        if sha256 != entry.sha256:
            if entry.size == stat.st_size and entry.mtime == stat.st_mtime:
                failures.append(_fail(
                    rel, "low-io-stale",
                    "changed since its upload but kept its size and mtime, so push (with "
                    f"low_io) did not see it; run `PROTONFS_LOW_IO=0 protonfs push {rel}`",
                ))
            else:
                failures.append(_fail(rel, "unverified-local", "changed since push read it"))
        elif blobs.get(rel) != blob and blobs.get(rel) != gitops.hash_object(ctx.root, rel):
            # The second id applies the path's attributes (eol conversion): a file whose
            # bytes differ from HEAD's blob only by git's own checkout conversion matches.
            failures.append(_fail(rel, "unverified-local", "differs from the blob in HEAD"))
        elif finding is None or finding.verdict is not Verdict.OK:
            verdict = finding.verdict.value if finding else "not checked"
            failures.append(_fail(rel, "unverified-drive", f"Drive check: {verdict}"))
        elif finding.ident is not None and finding.ident.sha1 and finding.ident.sha1 != sha1:
            failures.append(_fail(rel, "unverified-drive", "Drive's sha1 differs"))
    cache.save()
    return failures


def _shared_trash_policy(ctx: RepoContext) -> bool:
    import json

    shared = ctx.root / ".protonfs" / "config.json"
    try:
        data = json.loads(shared.read_text())
    except (OSError, ValueError):
        return False
    return (data.get("defaults") or {}).get("deletions") == "trash"


def _commands(paths: list[str]) -> list[str]:
    import shlex

    return [
        "git --literal-pathspecs rm --cached -q -- " + " ".join(shlex.quote(p) for p in paths),
        "git add -- .gitignore",
    ]


def import_from_git(
    ctx: RepoContext, paths: list[str], *, dry_run: bool = False, reporter=None
) -> ImportResult:
    """Move git-tracked ``paths`` into the store: push, verify, then untrack and ignore.

    Steps: :func:`check_import` (any refusal returns at once, nothing done); push exactly
    ``paths`` and write the remote manifest; :func:`verify_imported` (any failure
    returns, git unchanged); then add a literal line per path to the store's
    ``.gitignore`` import block, ``git rm --cached`` the paths and stage ``.gitignore``.
    A failing git command rolls every git change back. Never commits; never changes a
    local file's content; an ``only`` push never trashes anything. The caller holds the
    repo lock.

    :param ctx: the loaded store context.
    :param paths: store-relative file paths, already expanded.
    :param dry_run: report the plan (push's dry run, the lines, the commands) and change
        nothing; Drive is not listed and no file is hashed.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through.
    :returns: an :class:`ImportResult`.
    :raises click.ClickException: when a git step failed and the rollback failed too;
        the message lists the commands that undo what was done.
    :raises protonfs.drive.DriveError: on a Drive failure push or the listing raises.

    .. versionadded:: 3.1.0
    """
    from protonfs import manifest
    from protonfs.commands.push import push

    result = ImportResult(paths=list(paths))
    result.refusals = check_import(ctx, paths)
    if result.refusals:
        return result
    already = importblock.block_paths(ctx.root)
    result.gitignore_lines = [importblock.escape_line(p) for p in paths if p not in already]
    result.git_commands = _commands(paths)
    result.trash_policy_warning = _shared_trash_policy(ctx)
    result.total_bytes = sum((ctx.root / p).stat().st_size for p in paths)
    if dry_run:
        result.pushed = push(ctx, None, None, True, reporter=reporter, only=set(paths))
        return result

    updates: dict = {}
    forget: list = []
    try:
        result.pushed = push(
            ctx, None, None, False, reporter=reporter, manifest_updates=updates,
            only=set(paths), manifest_forget=forget,
        )
    finally:
        if updates or forget:
            if manifest.update(ctx, record=updates or None, forget=forget or None) is not None:
                ctx.index.save()
    result.failures = verify_imported(ctx, paths, reporter=reporter)
    if result.failures:
        return result
    _git_step(ctx, paths, result)
    return result


def _git_step(ctx: RepoContext, paths: list[str], result: ImportResult) -> None:
    """Add the ignore lines, untrack ``paths`` and stage ``.gitignore``, all or nothing.

    Any exception, a Ctrl-C included, undoes every git change before it propagates (a
    git or file error becomes a ``kind="git"`` failure instead). When the undo fails too,
    a :class:`click.ClickException` says which steps ran and how to undo them by hand.
    """
    gitignore = importblock.gitignore_path(ctx.root)
    before = gitignore.read_bytes() if gitignore.exists() else None
    steps = [
        ("wrote the .gitignore lines", lambda: importblock.add_paths(ctx.root, paths)),
        ("untracked the files (git rm --cached)", lambda: gitops.rm_cached(ctx.root, paths)),
        ("staged .gitignore", lambda: gitops.add(ctx.root, [".gitignore"])),
    ]
    done: list[str] = []
    try:
        for label, step in steps:
            current = label
            value = step()
            if label.startswith("wrote"):
                result.gitignore_lines = value
            done.append(label)
    except BaseException as exc:  # noqa: BLE001 -- a Ctrl-C must roll back too
        _roll_back(ctx, paths, before, exc, done, current)
        if not isinstance(exc, (gitops.GitError, OSError)):
            raise
        result.rolled_back = True
        result.gitignore_lines = []
        result.failures = [_fail(".", "git", f"git step failed and was rolled back: {exc}")]
        return
    result.untracked = len(paths)


def _roll_back(
    ctx: RepoContext,
    paths: list[str],
    before: bytes | None,
    exc: BaseException,
    done: list[str],
    current: str,
) -> None:
    """Undo :func:`_git_step`'s changes: the index entries, then ``.gitignore``'s bytes."""
    import tempfile

    import click

    gitignore = importblock.gitignore_path(ctx.root)
    try:
        gitops.reset_paths(ctx.root, [*paths, ".gitignore"])
        if before is None:
            gitignore.unlink(missing_ok=True)
        else:
            gitignore.write_bytes(before)
    except (gitops.GitError, OSError) as undo_exc:
        fd, listing = tempfile.mkstemp(prefix="protonfs-import-undo-", suffix=".txt")
        with open(fd, "wb") as fh:
            fh.write(b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths))
        restore = (
            f"rm {gitignore}" if before is None
            else f"git -C {ctx.root} checkout HEAD -- .gitignore"
        )
        ran = "; ".join(done) or "nothing"
        raise click.ClickException(
            f"the git step failed ({exc}) and undoing it failed too ({undo_exc}). "
            f"Completed: {ran}. Interrupted while it: {current} (may be partly done). "
            "To undo by hand: "
            f"`git --literal-pathspecs -C {ctx.root} reset -q --pathspec-from-file={listing} "
            f"--pathspec-file-nul`, `git -C {ctx.root} reset -q -- .gitignore`, then "
            f"`{restore}`. Nothing was committed and no imported file was changed."
        ) from exc
