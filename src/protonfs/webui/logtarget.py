"""Where ``du --gui`` writes its log: a directory, a file or a character device.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True)
class LogTarget:
    """A resolved ``--log`` destination.

    :ivar kind: ``"dir"`` (a new file named after the start time inside a directory),
        ``"file"`` (appended to) or ``"device"`` (a tty or similar, written directly).
    :ivar path: the file or device to open.

    .. versionadded:: 3.4.0
    """

    kind: str
    path: Path

    def open(self) -> TextIO:
        """Open the destination for writing, creating parent directories for files.

        :returns: a line-buffered text stream; the caller closes it.
        :raises OSError: when the destination cannot be created or opened.
        """
        if self.kind != "device":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        return open(self.path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115


def default_log_dir() -> Path:
    """``$XDG_STATE_HOME/protonfs/logs``, falling back to ``~/.local/state/protonfs/logs``.

    :returns: the directory (not created).

    .. versionadded:: 3.4.0
    """
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "protonfs" / "logs"


def resolve_log_target(arg: str | None, *, now: datetime) -> LogTarget:
    """Resolve a ``--log`` value into where the GUI server's log goes.

    - ``None``: a new file in :func:`default_log_dir`.
    - An existing directory, or a value ending in ``/``: a new file in that directory,
      named ``du-gui-<start time>.log`` (e.g. ``du-gui-2026-11-13T13-44-05.log``).
    - A character device (``/dev/tty``, ``/dev/stderr``, ...): written to directly.
    - Anything else: a file, appended to and created if missing.

    :param arg: the ``--log`` value, or ``None`` for the default.
    :param now: the process start time, used to name new files.
    :returns: the resolved :class:`LogTarget` (nothing is created until it is opened).

    .. versionadded:: 3.4.0
    """
    name = f"du-gui-{now.strftime('%Y-%m-%dT%H-%M-%S')}.log"
    if arg is None:
        return LogTarget("dir", default_log_dir() / name)
    path = Path(arg).expanduser()
    if arg.endswith(("/", os.sep)) or path.is_dir():
        return LogTarget("dir", path / name)
    try:
        if stat.S_ISCHR(path.stat().st_mode):
            return LogTarget("device", path)
    except OSError:
        pass
    return LogTarget("file", path)
