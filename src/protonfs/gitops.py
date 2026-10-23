"""Store-relative git operations for :mod:`protonfs.commands.gitmove` (#205).

Every function runs ``git -C <root>``, where ``root`` is the store root (which may be a
subdirectory of the repository), and takes and returns POSIX paths relative to ``root``.
Paths are literal: glob characters in a file name match only that name.
Paths are passed in batches of :data:`BATCH`, so an import of thousands of files never
exceeds the argument-length limit, and no command needs ``--pathspec-from-file`` (which
git only gained in 2.25, and which ``git rm`` rejects in a subdirectory). A git command that
fails raises :class:`GitError`.

.. code-block:: python

    from protonfs import gitops

    if gitops.in_work_tree(root):
        gitops.rm_cached(root, ["frames/0001.mp4"])

.. versionadded:: 3.1.0
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Paths per git invocation.
BATCH = 1000


class GitError(RuntimeError):
    """A git command exited non-zero.

    :ivar cmd: the argv that failed.
    :ivar returncode: git's exit status.
    :ivar stderr: git's error output.

    .. versionadded:: 3.1.0
    """

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        """:param cmd: the argv that failed.
        :param returncode: git's exit status.
        :param stderr: git's error output."""
        super().__init__(f"{' '.join(cmd)} exited {returncode}: {stderr.strip()}")
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr


def _run(
    root: Path,
    args: list[str],
    *,
    stdin: bytes | None = None,
    ok: tuple[int, ...] = (0,),
    literal: bool = True,
) -> bytes:
    # Literal pathspecs: a file named ``st*ar`` must match only itself, never a glob.
    # check-ignore takes plain path names, not pathspecs, and rejects the option.
    cmd = ["git", *(["--literal-pathspecs"] if literal else []), "-C", str(root), *args]
    proc = subprocess.run(cmd, input=stdin, capture_output=True, check=False)
    if proc.returncode not in ok:
        raise GitError(cmd, proc.returncode, proc.stderr.decode("utf-8", "surrogateescape"))
    return proc.stdout


def _nul(paths: list[str]) -> bytes:
    return b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)


def _split(out: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def _batches(paths: list[str]):
    for i in range(0, len(paths), BATCH):
        yield paths[i:i + BATCH]


def in_work_tree(root: Path) -> bool:
    """Return whether ``root`` is inside a git work tree (``False`` when git is missing).

    .. versionadded:: 3.1.0
    """
    try:
        return _run(root, ["rev-parse", "--is-inside-work-tree"]).strip() == b"true"
    except (GitError, FileNotFoundError):
        return False


def tracked_files(root: Path) -> set[str]:
    """Return every file git tracks under ``root``, relative to ``root``.

    :param root: the store root.
    :returns: tracked paths (the index, so staged additions count and staged removals
        do not).
    :raises GitError: when git fails.

    .. versionadded:: 3.1.0
    """
    return set(_split(_run(root, ["ls-files", "-z"])))


def head_blobs(root: Path, paths: list[str]) -> dict[str, str]:
    """Return the blob id each of ``paths`` has in ``HEAD``.

    :param root: the store root.
    :param paths: store-relative file paths.
    :returns: ``{path: blob id}``; a path that is not a file in ``HEAD`` is absent.
    :raises GitError: when git fails (including a repo with no commits).

    .. versionadded:: 3.1.0
    """
    blobs: dict[str, str] = {}
    for batch in _batches(paths):
        out = _run(root, ["ls-tree", "-r", "-z", "HEAD", "--", *batch])
        for record in _split(out):
            meta, _, path = record.partition("\t")
            kind, blob = meta.split()[1:3]
            if kind == "blob":
                blobs[path] = blob
    return blobs


def dirty_paths(root: Path, paths: list[str]) -> set[str]:
    """Return which of ``paths`` differ from ``HEAD`` in the index or the work tree.

    Covers staged changes, unstaged changes and files deleted from the work tree.

    :param root: the store root.
    :param paths: store-relative file paths.
    :returns: the subset of ``paths`` with any uncommitted change.
    :raises GitError: when git fails.

    .. versionadded:: 3.1.0
    """
    dirty: set[str] = set()
    for batch in _batches(paths):
        for extra in ([], ["--cached"]):
            out = _run(
                root, ["diff", *extra, "--name-only", "-z", "--relative", "HEAD", "--", *batch]
            )
            dirty.update(_split(out))
    return dirty


def rm_cached(root: Path, paths: list[str]) -> None:
    """Untrack ``paths`` (``git rm --cached``), leaving the files on disk.

    :param root: the store root.
    :param paths: store-relative tracked file paths.
    :raises GitError: when git fails, e.g. a path is not tracked.

    .. versionadded:: 3.1.0
    """
    for batch in _batches(paths):
        _run(root, ["rm", "--cached", "-q", "--", *batch])


def add(root: Path, paths: list[str]) -> None:
    """Stage ``paths`` (``git add``).

    :param root: the store root.
    :param paths: store-relative paths.
    :raises GitError: when git fails.

    .. versionadded:: 3.1.0
    """
    for batch in _batches(paths):
        _run(root, ["add", "--", *batch])


def reset_paths(root: Path, paths: list[str]) -> None:
    """Reset the index entries of ``paths`` to ``HEAD`` (``git reset -q``), leaving the
    work tree alone. Used to undo :func:`rm_cached` and :func:`add`.

    :param root: the store root.
    :param paths: store-relative paths.
    :raises GitError: when git fails.

    .. versionadded:: 3.1.0
    """
    for batch in _batches(paths):
        _run(root, ["reset", "-q", "--", *batch])


def check_ignore(root: Path, paths: list[str]) -> dict[str, str]:
    """Return which of ``paths`` git ignores, and the rule that does it.

    Tracked paths are judged too (``--no-index``), so this answers "would this rule hide
    the file once untracked".

    :param root: the store root.
    :param paths: store-relative paths.
    :returns: ``{path: "source:line:pattern"}`` for each ignored path, ``source`` as git
        reports it (relative to the repository root, e.g. ``products/.gitignore``); a
        path matched only by a negation (``!pattern``) is not ignored and is absent.
    :raises GitError: when git fails.

    .. versionadded:: 3.1.0
    """
    if not paths:
        return {}
    out = _run(
        root, ["check-ignore", "-v", "-z", "--no-index", "--stdin"],
        stdin=_nul(paths), ok=(0, 1), literal=False,
    )
    return _ignore_hits(out)


def _ignore_hits(out: bytes) -> dict[str, str]:
    fields = _split_keep_empty(out)
    hits: dict[str, str] = {}
    for i in range(0, len(fields) - 3, 4):
        source, line, pattern, path = fields[i:i + 4]
        if pattern and not pattern.startswith("!"):
            hits[path] = f"{source}:{line}:{pattern}"
    return hits


def check_ignore_with(root: Path, paths: list[str], gitignore: bytes | None) -> dict[str, str]:
    """As :func:`check_ignore`, but as if the store root's ``.gitignore`` held
    ``gitignore`` (``None``: no such file), without changing any file.

    Git is asked about a scratch work tree holding copies of only the ``.gitignore``
    files on the way from the repository root to each path, with the store's replaced;
    the repository's ``info/exclude`` and ``core.excludesFile`` apply as usual.

    :param root: the store root.
    :param paths: store-relative paths.
    :param gitignore: the store ``.gitignore``'s content to judge by.
    :returns: ``{path: "source:line:pattern"}`` for each path still ignored, ``source``
        relative to the repository root (or absolute, for ``info/exclude``).
    :raises GitError: when git fails.

    .. versionadded:: 3.1.0
    """
    import shutil
    import tempfile

    if not paths:
        return {}
    def rev_parse(option: str) -> str:
        out = _run(root, ["rev-parse", option], literal=False)
        return out.decode("utf-8", "surrogateescape").rstrip("\n")

    top = Path(rev_parse("--show-toplevel"))
    git_dir = rev_parse("--absolute-git-dir")
    prefix_dir = rev_parse("--show-prefix").rstrip("/")
    with tempfile.TemporaryDirectory(prefix="protonfs-ignore-") as tmp:
        mirror = Path(tmp)
        dirs = {""}
        for path in paths:
            parts = [*filter(None, prefix_dir.split("/")), *path.split("/")[:-1]]
            dirs.update("/".join(parts[:i]) for i in range(len(parts) + 1))
        for rel_dir in dirs:
            source = top / rel_dir / ".gitignore"
            if source.is_file():
                (mirror / rel_dir).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, mirror / rel_dir / ".gitignore")
        store = mirror / prefix_dir
        store.mkdir(parents=True, exist_ok=True)
        (store / ".gitignore").unlink(missing_ok=True)
        if gitignore is not None:
            (store / ".gitignore").write_bytes(gitignore)
        out = _run(
            store,
            [f"--git-dir={git_dir}", f"--work-tree={mirror}", "check-ignore", "-v", "-z",
             "--no-index", "--stdin"],
            stdin=_nul(paths), ok=(0, 1), literal=False,
        )
    return _ignore_hits(out)


def _split_keep_empty(out: bytes) -> list[str]:
    parts = out.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [p.decode("utf-8", "surrogateescape") for p in parts]


def file_modes(root: Path, paths: list[str]) -> dict[str, str]:
    """Return the mode git's index records for each of ``paths``.

    :param root: the store root.
    :param paths: store-relative paths.
    :returns: ``{path: mode}``, e.g. ``"100644"`` (file), ``"100755"`` (executable),
        ``"120000"`` (symlink), ``"160000"`` (submodule); untracked paths are absent.
    :raises GitError: when git fails.

    .. versionadded:: 3.1.0
    """
    modes: dict[str, str] = {}
    for batch in _batches(paths):
        for record in _split(_run(root, ["ls-files", "-s", "-z", "--", *batch])):
            meta, _, path = record.partition("\t")
            modes[path] = meta.split()[0]
    return modes


def attribute(root: Path, paths: list[str], name: str) -> dict[str, str]:
    """Return the value of the git attribute ``name`` for each of ``paths`` that sets it.

    :param root: the store root.
    :param paths: store-relative paths.
    :param name: the attribute, e.g. ``"filter"`` or ``"eol"``.
    :returns: ``{path: value}`` for paths where it is set (``set`` for a bare attribute);
        paths where it is unspecified or unset are absent.
    :raises GitError: when git fails.

    .. versionadded:: 3.1.0
    """
    values: dict[str, str] = {}
    for batch in _batches(paths):
        fields = _split_keep_empty(_run(root, ["check-attr", "-z", name, "--", *batch]))
        for i in range(0, len(fields) - 2, 3):
            path, _, value = fields[i:i + 3]
            if value not in ("unspecified", "unset"):
                values[path] = value
    return values


def hash_object(root: Path, path: str) -> str:
    """Return the blob id git would store for the file ``path`` now, applying the path's
    attributes (end-of-line conversion, clean filters), as ``git add`` would.

    :param root: the store root.
    :param path: a store-relative file path.
    :returns: the blob id.
    :raises GitError: when git fails.

    .. versionadded:: 3.1.0
    """
    return _run(root, ["hash-object", "--", path]).decode().strip()
