"""In-memory :class:`~protonfs.usage.source.DriveSource` fake with measured Drive semantics.

Trashed nodes stay counted by ``size()`` of every ancestor until purged, vanish from
``list_dir``, and show in ``list_trash`` only at their top level.
"""

from __future__ import annotations

from typing import Callable

from protonfs.drive import DriveError, SizeResult
from protonfs.usage import paths
from protonfs.usage.source import RawEntry


class _Node:
    def __init__(self, path: str, kind: str, uid: str, size: int | None = None) -> None:
        self.path = path
        self.kind = kind
        self.uid = uid
        self.size = size
        self.trashed = False


class TreeDrive:
    """A tree of nodes keyed by escaped Drive path, implementing ``DriveSource``."""

    def __init__(self, scope: str = "vol-1") -> None:
        self.scope = scope
        self.concurrency = 4
        self.calls: list[tuple[str, str]] = []
        self._nodes: dict[str, _Node] = {}
        self._n = 0
        self._failures: dict[tuple[str, str], list[BaseException]] = {}
        self._hooks: dict[tuple[str, str], list[Callable[[], None]]] = {}

    # building
    def _add(self, path: str, kind: str, size: int | None = None) -> str:
        parent = paths.parent_of(path)
        while parent not in (None, "/") and parent not in self._nodes:
            self._add(parent, "folder")
            parent = paths.parent_of(parent)
        self._n += 1
        uid = f"uid-{self._n}"
        self._nodes[path] = _Node(path, kind, uid, size)
        return uid

    def add_folder(self, path: str) -> str:
        """Add a folder (and any missing ancestors); return its uid."""
        return self._add(path, "folder")

    def add_file(self, path: str, size: int) -> str:
        """Add a file of ``size`` encrypted bytes; return its uid."""
        return self._add(path, "file", size)

    def add_device(self, name: str) -> str:
        """Add a device root under ``/devices``; return its uid."""
        return self._add(paths.join("/devices", name), "folder")

    def trash(self, path: str) -> None:
        """Move ``path`` to the trash (still counted by ancestors' ``size``)."""
        self._nodes[path].trashed = True

    def purge(self, path: str) -> None:
        """Remove ``path`` and everything beneath it permanently."""
        for p in [p for p in self._nodes if p == path or paths.is_within(p, path)]:
            del self._nodes[p]

    # scripting
    def fail_next(self, op: str, path: str, exc: BaseException, times: int = 1) -> None:
        """Raise ``exc`` on the next ``times`` calls of ``op`` for ``path``."""
        self._failures.setdefault((op, path), []).extend([exc] * times)

    def before(self, op: str, path: str, fn: Callable[[], None]) -> None:
        """Run ``fn()`` just before servicing the next matching ``(op, path)`` call."""
        self._hooks.setdefault((op, path), []).append(fn)

    def _enter(self, op: str, path: str) -> None:
        self.calls.append((op, path))
        hooks = self._hooks.get((op, path))
        if hooks:
            hooks.pop(0)()
        fails = self._failures.get((op, path))
        if fails:
            raise fails.pop(0)

    # DriveSource
    def _entry(self, node: _Node) -> RawEntry:
        segs = paths.split(node.path)
        parent = paths.parent_of(node.path)
        pnode = self._nodes.get(parent) if parent else None
        return RawEntry(
            name=segs[-1],
            kind=node.kind,
            uid=node.uid,
            size=node.size if node.kind == "file" else None,
            mtime="2026-01-01T00:00:00Z",
            parent_uid=pnode.uid if pnode else None,
            scope=self.scope,
            trash_time="2026-02-01T00:00:00Z" if node.trashed else None,
            decrypted=True,
        )

    def _check_live(self, path: str) -> None:
        if path == "/trash" or path.startswith("/trash/"):
            raise DriveError("Trashed node not found")

    def _top_level_trashed(self) -> list[_Node]:
        out = []
        for node in self._nodes.values():
            if not node.trashed:
                continue
            p = paths.parent_of(node.path)
            hidden = False
            while p and p != "/":
                anc = self._nodes.get(p)
                if anc is not None and anc.trashed:
                    hidden = True
                    break
                p = paths.parent_of(p)
            if not hidden:
                out.append(node)
        return out

    def list_dir(self, path: str, *, expected_entries: int | None = None) -> list[RawEntry]:
        self._enter("list", path)
        self._check_live(path)
        if path != "/" and path not in self._nodes and path not in ("/my-files", "/devices"):
            raise DriveError(f"Node not found: {path}")
        if path in self._nodes and self._nodes[path].trashed:
            raise DriveError("Trashed node not found")
        return [
            self._entry(n)
            for p, n in self._nodes.items()
            if paths.parent_of(p) == path and not n.trashed
        ]

    def size(self, path: str) -> SizeResult:
        self._enter("size", path)
        self._check_live(path)
        node = self._nodes.get(path)
        if node is None:
            if path in ("/", "/my-files", "/devices"):
                below = [n for p, n in self._nodes.items() if paths.is_within(p, path)]
                return SizeResult(sum(n.size or 0 for n in below), len(below))
            raise DriveError(f"Node not found: {path}")
        if node.trashed:
            raise DriveError("Trashed node not found")
        below = [n for p, n in self._nodes.items() if paths.is_within(p, path) and p != path]
        return SizeResult(sum(n.size or 0 for n in below), len(below))

    def list_trash(self, *, timeout: float) -> list[RawEntry]:
        self._enter("list_trash", "/trash")
        return [self._entry(n) for n in self._top_level_trashed()]
