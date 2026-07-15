"""Advisory repo lock so two concurrent protonfs processes cannot interleave index writes.

`push`, `pull`, `rm`, `restore` and `refresh` all read-modify-write the per-device
`index.json`. If two processes ran at once their writes could interleave and corrupt the
manifest. `repo_lock` takes a non-blocking exclusive POSIX `flock` on `.protonfs/lock`, so
a second process fails fast with an instructive message rather than silently blocking or
racing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows); real locking is issue #9
    fcntl = None  # type: ignore[assignment]

LOCK_FILE_NAME = "lock"


class RepoLockError(RuntimeError):
    """Another protonfs process already holds this repo's index lock."""


@contextmanager
def repo_lock(repo_root: Path) -> Iterator[None]:
    """Hold an advisory exclusive lock over a repo's index-mutating section.

    Non-blocking: if another process holds the lock this raises `RepoLockError`
    immediately instead of waiting. `flock` is associated with the open file
    description, so the lock is released automatically if the process dies (no stale
    lock file to clean up). On a platform without `fcntl` this degrades to a no-op.
    """
    lock_dir = repo_root / ".protonfs"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / LOCK_FILE_NAME

    if fcntl is None:
        logger.debug("no fcntl on this platform; repo lock is a no-op")
        yield
        return

    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RepoLockError(
                "another protonfs process is operating on this repo "
                f"(lock held at {lock_path}). Wait for it to finish, then retry."
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
