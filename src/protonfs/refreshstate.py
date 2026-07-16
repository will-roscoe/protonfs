# src/protonfs/refreshstate.py
"""Resumable-refresh state: the persisted BFS frontier for `protonfs refresh` (#33 item 2).

A whole-tree `refresh` walks the remote breadth-first. Under API throttle a run can wedge
partway; without resumability the next run restarts from the root and re-triggers the same
throttle. This module persists the walk's *frontier* -- the queue of directories not yet
listed -- to `.protonfs/refresh-state.json` after each directory, so a re-invoked refresh
continues from where it stopped. The seeded index entries themselves persist separately
(per-directory, via the index), so together they make refresh resumable.

The state is scoped to the pass's remote `root`: a saved frontier for a different root (e.g.
a previous `refresh <other-subpath>`) is stale for this pass and ignored.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

REFRESH_STATE_FILE = "refresh-state.json"

# A frontier item is (absolute remote path, rel-path prefix) -- the walk's queue element.
FrontierItem = tuple[str, str]


def _state_path(repo_root: Path) -> Path:
    return repo_root / ".protonfs" / REFRESH_STATE_FILE


def save_frontier(repo_root: Path, root: str, frontier: list[FrontierItem]) -> None:
    """Atomically persist `frontier` for the pass rooted at `root`.

    Written via a temp file + os.replace (same crash-safe idiom as the index), so a reader
    -- or a crash mid-write -- never sees a torn state file.
    """
    path = _state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"root": root, "frontier": [list(item) for item in frontier]}
    data = json.dumps(document, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".refresh-state.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_frontier(repo_root: Path, root: str) -> list[FrontierItem] | None:
    """Return the saved frontier for `root`, or None when there is none or it is stale
    (persisted for a different root, so not resumable for this pass)."""
    path = _state_path(repo_root)
    if not path.exists():
        return None
    document = json.loads(path.read_text())
    if document.get("root") != root:
        return None
    return [tuple(item) for item in document.get("frontier", [])]


def clear(repo_root: Path) -> None:
    """Remove any saved frontier (the pass completed, so nothing is left to resume)."""
    _state_path(repo_root).unlink(missing_ok=True)
