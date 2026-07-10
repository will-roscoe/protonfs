# src/protonfs/commands/auth.py
"""`protonfs auth {login,logout,status}` — a thin passthrough to `proton-drive auth`.

Auth is left entirely to proton-drive (D3.3): it prints a URL to open on any
device and persists the session to the OS keyring, so this works headlessly with
no custom handling. We inherit stdio (no --json, no capture) so the interactive
login URL reaches the user's terminal.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from protonfs.drive import DriveError, binary_path

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
    result = runner([bin_path, "auth", subcommand])
    return result.returncode
