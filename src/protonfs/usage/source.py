"""The read-only Drive interface the ``du`` engine and GUI consume.

:class:`DriveSource` is the protocol; :class:`CliDriveSource` implements it over
:class:`~protonfs.drive.DriveClient`. Only ``filesystem list``, ``filesystem size`` and
``filesystem list /trash`` are ever issued.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from ..drive import (
    LIST_TIMEOUT_SECONDS,
    DriveClient,
    DriveLockError,
    SizeResult,
    decrypted_name,
)

LOCK_RETRIES = 6
LOCK_BACKOFF_BASE = 0.5
LOCK_BACKOFF_CAP = 4.0


@dataclass(frozen=True)
class RawEntry:
    """One Drive node as listed, normalised from the CLI's JSON.

    :ivar name: decrypted name, or the node ``uid`` when it could not be decrypted.
    :ivar kind: ``"file"`` or ``"folder"`` (device roots are folders).
    :ivar uid: Drive node uid.
    :ivar size: encrypted ``totalStorageSize`` in bytes; ``None`` for folders.
    :ivar mtime: ``modificationTime`` string, if any.
    :ivar parent_uid: uid of the parent node, if listed.
    :ivar scope: volume id (``treeEventScopeId``), the same for every node in a volume.
    :ivar trash_time: ``trashTime`` string for a trashed entry, else ``None``.
    :ivar decrypted: whether the name was decrypted.

    .. versionadded:: 3.4.0
    """

    name: str
    kind: str
    uid: str | None = None
    size: int | None = None
    mtime: str | None = None
    parent_uid: str | None = None
    scope: str | None = None
    trash_time: str | None = None
    decrypted: bool = True


class DriveSource(Protocol):
    """Read-only view of Drive used by the ``du`` engine.

    :ivar concurrency: how many Drive calls may be in flight at once.

    .. versionadded:: 3.4.0
    """

    concurrency: int

    def list_dir(self, path: str, *, expected_entries: int | None = None) -> list[RawEntry]:
        """Return the live (untrashed) children of the folder at escaped Drive ``path``.

        :param path: escaped Drive path.
        :param expected_entries: a hint for sizing the call timeout.
        :raises DriveError: when the path is missing or the call fails.
        """
        ...

    def size(self, path: str) -> SizeResult:
        """Return the folder total at ``path``, trashed descendants included.

        :param path: escaped Drive path.
        :raises DriveError: when the path is missing or the output is malformed.
        """
        ...

    def list_trash(self, *, timeout: float) -> list[RawEntry]:
        """Return the top-level trashed nodes (folders carry ``size=None``).

        :param timeout: per-call timeout in seconds.
        :raises subprocess.TimeoutExpired: when the listing exceeds ``timeout``.
        """
        ...


def list_timeout(expected_entries: int | None) -> float:
    """Return the ``list`` timeout in seconds for a folder of ``expected_entries`` entries.

    ``30 + 0.06 * n`` capped at 3600; :data:`~protonfs.drive.LIST_TIMEOUT_SECONDS` when
    ``expected_entries`` is ``None``.

    .. versionadded:: 3.4.0
    """
    if expected_entries is None:
        return LIST_TIMEOUT_SECONDS
    return min(3600.0, 30.0 + 0.06 * expected_entries)


def normalise_entry(raw: dict) -> RawEntry:
    """Convert one raw ``filesystem list`` entry dict into a :class:`RawEntry`.

    A ``type`` of ``"folder"``, or any non-file type carrying ``rootFolderUid`` (device
    rows have ``type`` set to the OS name), is a folder. An undecryptable name becomes
    the ``uid`` with ``decrypted=False``.

    .. versionadded:: 3.4.0
    """
    typ = raw.get("type")
    is_folder = typ == "folder" or (typ != "file" and "rootFolderUid" in raw)
    uid = raw.get("uid")
    name = decrypted_name(raw)
    decrypted = name is not None
    if name is None:
        name = uid or ""
    size = raw.get("totalStorageSize")
    return RawEntry(
        name=name,
        kind="folder" if is_folder else "file",
        uid=uid,
        size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        mtime=raw.get("modificationTime"),
        parent_uid=raw.get("parentUid"),
        scope=raw.get("treeEventScopeId"),
        trash_time=raw.get("trashTime"),
        decrypted=decrypted,
    )


class CliDriveSource:
    """:class:`DriveSource` over a :class:`~protonfs.drive.DriveClient`.

    A bounded semaphore guards every subprocess call so all callers (the engine's
    executor and the GUI's threads) share one Drive budget. A
    :class:`~protonfs.drive.DriveLockError` is retried up to 6 times (0.5 s doubling to
    a 4 s cap) and then re-raised.

    :param client: the Drive client to call.
    :param concurrency: maximum simultaneous Drive calls.
    :param sleep: sleep function used between lock retries (injectable for tests).

    .. versionadded:: 3.4.0
    """

    def __init__(
        self,
        client: DriveClient,
        concurrency: int = 4,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.concurrency = concurrency
        self._sem = threading.BoundedSemaphore(concurrency)
        self._sleep = sleep

    def _call(self, fn: Callable[[], object]):
        delay = LOCK_BACKOFF_BASE
        for attempt in range(LOCK_RETRIES + 1):
            try:
                with self._sem:
                    return fn()
            except DriveLockError:
                if attempt == LOCK_RETRIES:
                    raise
            self._sleep(delay)
            delay = min(delay * 2, LOCK_BACKOFF_CAP)

    def list_dir(self, path: str, *, expected_entries: int | None = None) -> list[RawEntry]:
        """Return the live children of ``path`` (escaped Drive form), normalised.

        :param path: escaped Drive path.
        :param expected_entries: a hint used to scale the call timeout.
        :returns: the entries, in listing order.
        :raises DriveError: on a Drive failure, including a persistent cache lock.
        """
        timeout = list_timeout(expected_entries)
        raw = self._call(lambda: self._client.list_with_backoff(path, timeout=timeout))
        return [normalise_entry(e) for e in raw]

    def size(self, path: str) -> SizeResult:
        """Return the ``filesystem size`` total for ``path``.

        :param path: escaped Drive path.
        :returns: bytes and node count, trashed descendants included.
        :raises DriveError: on a missing path, malformed output or a persistent lock.
        """
        return self._call(lambda: self._client.size(path))

    def list_trash(self, *, timeout: float) -> list[RawEntry]:
        """Return the top-level trashed entries from ``filesystem list /trash``.

        :param timeout: per-call timeout in seconds.
        :returns: the trashed entries.
        :raises subprocess.TimeoutExpired: when the listing exceeds ``timeout``.
        :raises DriveError: on a Drive failure.
        """
        raw = self._call(lambda: self._client.list("/trash", timeout=timeout))
        return [normalise_entry(e) for e in raw]
