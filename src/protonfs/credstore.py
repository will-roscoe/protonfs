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
import shutil
import subprocess
from dataclasses import dataclass, field
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


_RUN_TIMEOUT = 30  # gpg keygen can be the slow one, but must not hang forever
GPG_IDENTITY = "protonfs (Proton Drive CLI session store) <protonfs@localhost>"


@dataclass
class PassResult:
    """Outcome of :func:`ensure_pass_store`."""

    ready: bool
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _run(cmd: list[str], env: dict[str, str], stdin: str | None = None):
    """Run a subprocess with a bounded timeout (the injectable runner)."""
    return subprocess.run(
        cmd, env=env, input=stdin, capture_output=True, text=True, timeout=_RUN_TIMEOUT
    )


def pass_tools_present() -> bool:
    """Whether both `pass` and `gpg` are on PATH (required to use the pass store)."""
    return shutil.which("pass") is not None and shutil.which("gpg") is not None


def pass_store_initialized() -> bool:
    """Whether the managed pass store has been `pass init`'d (`.gpg-id` present)."""
    return (password_store_dir() / ".gpg-id").exists()


def pass_env(base: dict[str, str]) -> dict[str, str]:
    """`base` plus the pass selector and managed dirs, without clobbering user-set ones."""
    out = dict(base)
    out[STORE_ENV] = PASS
    out.setdefault("PASSWORD_STORE_DIR", str(password_store_dir()))
    out.setdefault("GNUPGHOME", str(gnupg_home()))
    return out


def _managed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Env for running gpg/pass against the managed GNUPGHOME + store dir."""
    env = dict(os.environ if base is None else base)
    env["GNUPGHOME"] = str(gnupg_home())
    env["PASSWORD_STORE_DIR"] = str(password_store_dir())
    return env


def _gpg_fingerprint(env: dict[str, str], runner) -> str | None:
    """The fingerprint of the first secret key in the managed GNUPGHOME, or None."""
    result = runner(["gpg", "--list-secret-keys", "--with-colons"], env)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("fpr:"):
            return line.split(":")[9]
    return None


def ensure_pass_store(runner=_run) -> PassResult:
    """Make the managed pass store usable: ensure a GPG key + `pass init`. Idempotent."""
    if not pass_tools_present():
        missing = " and ".join(
            t for t in ("pass", "gpg") if shutil.which(t) is None
        )
        return PassResult(
            ready=False,
            warnings=[
                f"cannot use the `pass` credentials store: {missing} not on PATH "
                f"(install `pass` and `gnupg2`)."
            ],
        )
    import stat as _stat

    gnupg_home().mkdir(parents=True, exist_ok=True)
    gnupg_home().chmod(_stat.S_IRWXU)  # gpg refuses a world-readable GNUPGHOME
    password_store_dir().mkdir(parents=True, exist_ok=True)
    if pass_store_initialized():
        return PassResult(ready=True)

    env = _managed_env()
    actions: list[str] = []
    fpr = _gpg_fingerprint(env, runner)
    if fpr is None:
        gen = runner(
            [
                "gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                "--quick-generate-key", GPG_IDENTITY, "default", "default", "never",
            ],
            env,
        )
        if gen.returncode != 0:
            return PassResult(
                ready=False,
                warnings=[f"gpg key generation failed: {gen.stderr.strip() or gen.returncode}"],
            )
        actions.append("generated a protonfs GPG key (passphrase-less)")
        fpr = _gpg_fingerprint(env, runner)
    if fpr is None:
        return PassResult(ready=False, warnings=["could not read the generated GPG key"])

    init = runner(["pass", "init", fpr], env)
    if init.returncode != 0 or not pass_store_initialized():
        return PassResult(
            ready=False,
            warnings=[f"`pass init` failed: {init.stderr.strip() or init.returncode}"],
        )
    actions.append(f"initialized the pass store at {password_store_dir()}")
    return PassResult(ready=True, actions=actions)
