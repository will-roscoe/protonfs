# src/protonfs/commands/auth.py
"""`protonfs auth {login,logout,status}` — a thin passthrough to `proton-drive auth`.

Auth is left entirely to proton-drive: it prints a URL to open on any device and
persists the session to the OS keyring. We inherit stdio (no --json, no capture) so
the interactive login URL reaches the user's terminal.

The keyring is *not* free on a headless host, though, which is why this passthrough
bootstraps the environment first (see protonfs.secretservice). `auth login` is the
command that writes the session, so it is exactly where a missing or locked Secret
Service bites: the browser flow completes, and only then does the CLI die with
`Cannot create an item in a locked collection`, having thrown the session away.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from protonfs.drive import DriveError, binary_path
from protonfs.secretservice import drive_env

AUTH_SUBCOMMANDS = ("login", "logout", "status")


def auth_passthrough(subcommand: str, binary: str | None = None, runner=subprocess.run) -> int:
    """Invoke `proton-drive auth <subcommand>` with inherited stdio; return exit code.

    Raises DriveError (rendered cleanly by the CLI error boundary) if the subcommand
    is unknown or the proton-drive binary is not installed/on PATH -- so a first-time
    user who runs `auth login` before `install-drive` gets an instructive message
    instead of a raw FileNotFoundError.
    """
    if subcommand not in AUTH_SUBCOMMANDS:
        raise ValueError(f"unknown auth subcommand: {subcommand!r}")
    bin_path = binary or binary_path()
    if shutil.which(bin_path) is None and not Path(bin_path).exists():
        raise DriveError(
            f"proton-drive binary not found: {bin_path}. Run `protonfs install-drive` first."
        )
    result = runner([bin_path, "auth", subcommand], env=drive_env())
    return result.returncode
