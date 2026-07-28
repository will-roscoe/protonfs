"""Select and bootstrap the proton-drive credentials store (keychain vs pass).

proton-drive 0.6.0 persists its session to `keychain` (freedesktop Secret Service,
the default) or `pass` (password-store), chosen by PROTON_DRIVE_CREDENTIALS_STORE.
On a headless host the Secret Service is expensive to provide (see
:mod:`protonfs.secretservice`); `pass` needs no D-Bus at all. This module picks the
store, falling back to a protonfs-managed `pass` store when the Secret Service cannot
be made ready, and makes that choice sticky so a later command never reads a different
(empty) store and reports "not authenticated".

.. versionadded:: 1.9.0
"""
from __future__ import annotations

import os
from pathlib import Path

from protonfs.secretservice import state_dir

STORE_ENV = "PROTON_DRIVE_CREDENTIALS_STORE"
PROTONFS_STORE_ENV = "PROTONFS_CREDENTIALS_STORE"
KEYCHAIN = "keychain"
PASS = "pass"
AUTO = "auto"
_VALID_STORES = (KEYCHAIN, PASS)


def gnupg_home() -> Path:
    """The protonfs-managed GNUPGHOME handed to proton-drive's `pass`/`gpg`."""
    return state_dir() / "gnupg"


def password_store_dir() -> Path:
    """The protonfs-managed PASSWORD_STORE_DIR handed to proton-drive's `pass`."""
    return state_dir() / "password-store"


def store_choice_file() -> Path:
    """The file recording the sticky credentials-store choice for this host."""
    return state_dir() / "credentials-store"


def read_store_choice() -> str | None:
    """The persisted store choice (`keychain`/`pass`), or None if unset/unrecognized."""
    path = store_choice_file()
    if not path.exists():
        return None
    value = path.read_text().strip()
    return value if value in _VALID_STORES else None


def write_store_choice(store: str) -> None:
    """Persist the sticky store choice. Raises ValueError on an unknown store."""
    if store not in _VALID_STORES:
        raise ValueError(f"unknown credentials store: {store!r}")
    path = store_choice_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store)
