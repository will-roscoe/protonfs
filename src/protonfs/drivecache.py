"""Where proton-drive keeps its sqlite cache, and whether that is on a network
filesystem (#178).

proton-drive reads and writes two sqlite files (``cache-entities.sqlite``,
``cache-crypto.sqlite``) in its cache directory on every call. On a home directory
that is a network mount (GlusterFS, NFS, Lustre, GPFS, CIFS) each of those small
reads pays a network round trip; on one HPC login node a listing went from 8 s to
past a 600 s timeout.

:envvar:`PROTONFS_DRIVE_CACHE_HOME` moves the cache: protonfs passes it to
proton-drive as ``XDG_CACHE_HOME``, so the cache lands in
``$PROTONFS_DRIVE_CACHE_HOME/proton-drive-cli``. proton-drive creates that directory
itself on every start, so a location under ``/tmp`` survives being wiped. Only the
cache moves; proton-drive's client ID, event state and logs stay where they are.

- :func:`apply_cache_home` -- add the override to the environment proton-drive gets.
- :func:`cache_dir` -- the cache directory proton-drive will use for an environment.
- :func:`filesystem_type` -- the filesystem a path lives on (Linux).

.. versionadded:: 2.6.0
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

#: Directory protonfs passes to proton-drive as ``XDG_CACHE_HOME``. Unset or empty
#: leaves proton-drive's own default (``$XDG_CACHE_HOME``, else ``~/.cache``).
CACHE_HOME_ENV = "PROTONFS_DRIVE_CACHE_HOME"

#: The directory name proton-drive appends to its cache base (every version 0.4.6-0.9.0).
APP_DIR_NAME = "proton-drive-cli"

#: ``/proc/self/mounts`` filesystem types that are network mounts.
NETWORK_FILESYSTEMS = frozenset(
    {"fuse.glusterfs", "glusterfs", "nfs", "nfs4", "lustre", "gpfs", "cifs", "smb3"}
)


def apply_cache_home(env: dict[str, str]) -> dict[str, str]:
    """Return ``env`` with ``XDG_CACHE_HOME`` set from :data:`CACHE_HOME_ENV`, if that is set.

    :param env: the environment proton-drive is about to get; not modified.
    :returns: ``env`` itself when the override is unset or empty, else a copy with
        ``XDG_CACHE_HOME`` replaced.
    """
    home = env.get(CACHE_HOME_ENV)
    if not home:
        return env
    return {**env, "XDG_CACHE_HOME": home}


def cache_dir(env: dict[str, str], system: str | None = None) -> Path:
    """Return the directory proton-drive keeps its cache in when run with ``env``.

    Follows proton-drive's own rule: ``PROTON_DRIVE_CACHE_DIR`` wins; on macOS it is
    ``~/Library/Caches/proton-drive-cli``; elsewhere ``$XDG_CACHE_HOME/proton-drive-cli``,
    with ``~/.cache`` when ``XDG_CACHE_HOME`` is unset or empty.

    :param env: the environment proton-drive runs with (apply :func:`apply_cache_home`
        first to see where protonfs puts it).
    :param system: :func:`platform.system` override, for tests.
    """
    override = env.get("PROTON_DRIVE_CACHE_DIR")
    if override:
        return Path(override)
    home = Path(env.get("HOME") or Path.home())
    if (system or platform.system()) == "Darwin":
        return home / "Library" / "Caches" / APP_DIR_NAME
    return Path(env.get("XDG_CACHE_HOME") or home / ".cache") / APP_DIR_NAME


def _unescape(field: str) -> str:
    # /proc/mounts writes space, tab, newline and backslash as octal escapes.
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(code, char)
    return field


def _visible(path: Path) -> bool:
    # exists() raises PermissionError beneath a directory this user cannot search
    try:
        return path.exists()
    except OSError:
        return False


def filesystem_type(path: Path, mounts_text: str | None = None) -> str | None:
    """Return the type of the filesystem ``path`` is on, e.g. ``"ext4"`` or ``"fuse.glusterfs"``.

    Symlinks are followed, and a path that does not exist yet (or cannot be seen) is
    judged by its nearest visible parent (where proton-drive would create it).

    :param path: the file or directory to locate.
    :param mounts_text: the contents of ``/proc/self/mounts``, for tests; read from
        disk when ``None``.
    :returns: the filesystem type of the deepest mount containing ``path``, or
        ``None`` when it cannot be told (no ``/proc/self/mounts``, as on macOS).
    """
    if mounts_text is None:
        try:
            mounts_text = Path("/proc/self/mounts").read_text()
        except OSError:
            return None
    target = Path(path)
    while not _visible(target) and target != target.parent:
        target = target.parent
    resolved = os.path.realpath(target)
    best, best_type = "", None
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mount = _unescape(fields[1])
        inside = resolved == mount or resolved.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) >= len(best):
            best, best_type = mount, fields[2]
    return best_type
