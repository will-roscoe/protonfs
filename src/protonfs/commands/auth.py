# src/protonfs/commands/auth.py
"""`protonfs auth {login,logout,status}` — a thin passthrough to `proton-drive auth`.

Auth is left entirely to proton-drive (D3.3): it prints a URL to open on any
device and persists the session to the OS keyring, so this works headlessly with
no custom handling. We inherit stdio (no --json, no capture) so the interactive
login URL reaches the user's terminal.
"""
from __future__ import annotations

import subprocess

from protonfs.drive import binary_path

AUTH_SUBCOMMANDS = ("login", "logout", "status")


def auth_passthrough(subcommand: str, binary: str | None = None, runner=subprocess.run) -> int:
    """Invoke `proton-drive auth <subcommand>` with inherited stdio; return exit code."""
    bin_path = binary or binary_path()
    result = runner([bin_path, "auth", subcommand])
    return result.returncode
