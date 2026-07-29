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
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from protonfs import secretservice
from protonfs.secretservice import state_dir

STORE_ENV = "PROTON_DRIVE_CREDENTIALS_STORE"
PROTONFS_STORE_ENV = "PROTONFS_CREDENTIALS_STORE"
KEYCHAIN = "keychain"
PASS = "pass"
AUTO = "auto"
_VALID_STORES = (KEYCHAIN, PASS)

# proton-drive first understood PROTON_DRIVE_CREDENTIALS_STORE=pass in 0.6.0. An older
# binary silently ignores the variable and uses its keychain store, so a host that has
# resolved to `pass` needs at least this version or the managed pass store is never used.
# `protonfs doctor` fails on this mismatch (see commands/doctor.py).
PASS_STORE_MIN_DRIVE = "0.6.0"


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

# Unattended, passphrase-less key generation as a `gpg --batch --gen-key` parameter
# file (fed over stdin). This form is portable across GnuPG 2.0 and 2.1+, unlike
# `--quick-generate-key`/`--pinentry-mode loopback` which only exist from 2.1.13 --
# a real blocker on the headless target hosts this fallback exists for (e.g. CentOS 7
# ships GnuPG 2.0.22). `Subkey-Type: RSA` gives the encryption subkey `pass` needs.
# `%no-protection` yields a passphrase-less key on 2.1+ and is harmlessly ignored on
# 2.0 (where a batch key with no `Passphrase:` line is passphrase-less anyway).
_GPG_GEN_PARAMS = (
    "Key-Type: RSA\n"
    "Key-Length: 2048\n"
    "Subkey-Type: RSA\n"
    "Subkey-Length: 2048\n"
    "Name-Real: protonfs\n"
    "Name-Comment: Proton Drive CLI session store\n"
    "Name-Email: protonfs@localhost\n"
    "Expire-Date: 0\n"
    "%no-protection\n"
    "%commit\n"
)


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
    """The fingerprint of the first secret key in the managed GNUPGHOME, or None.

    `--fingerprint` is required for GnuPG 2.0 to emit ``fpr:`` records in
    ``--with-colons`` secret-key listings (2.1+ emits them regardless).
    """
    result = runner(["gpg", "--list-secret-keys", "--with-colons", "--fingerprint"], env)
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
    gnupg_home().mkdir(parents=True, exist_ok=True)
    gnupg_home().chmod(stat.S_IRWXU)  # gpg refuses a world-readable GNUPGHOME
    password_store_dir().mkdir(parents=True, exist_ok=True)
    if pass_store_initialized():
        return PassResult(ready=True)

    env = _managed_env()
    actions: list[str] = []
    try:
        fpr = _gpg_fingerprint(env, runner)
        if fpr is None:
            gen = runner(["gpg", "--batch", "--gen-key"], env, _GPG_GEN_PARAMS)
            if gen.returncode != 0:
                return PassResult(
                    ready=False,
                    warnings=[
                        f"gpg key generation failed: {gen.stderr.strip() or gen.returncode}"
                    ],
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
    except (subprocess.SubprocessError, OSError) as exc:
        return PassResult(ready=False, warnings=[f"pass store setup failed: {exc}"])
    actions.append(f"initialized the pass store at {password_store_dir()}")
    return PassResult(ready=True, actions=actions)


def active_store(env: dict[str, str] | None = None) -> tuple[str, str]:
    """The store that would be used now and why, without bootstrapping anything."""
    base = dict(os.environ if env is None else env)
    if base.get(STORE_ENV) in _VALID_STORES:
        return base[STORE_ENV], "native-env"
    override = base.get(PROTONFS_STORE_ENV, AUTO).strip().lower()
    if override in _VALID_STORES:
        return override, "protonfs-env"
    sticky = read_store_choice()
    if sticky is not None:
        return sticky, "sticky"
    return KEYCHAIN, "auto-default"


def probe_pass_store(runner=_run) -> tuple[bool, str]:
    """Insert, read back and remove a throwaway pass entry. Returns (ok, detail)."""
    if not pass_tools_present():
        return False, "pass/gpg not installed"
    env = _managed_env()
    entry = "ch.proton.drive/drive-sdk-cli/protonfs-selftest"
    try:
        ins = runner(["pass", "insert", "-f", "-m", entry], env, "protonfs-selftest")
        if ins.returncode != 0:
            return False, ins.stderr.strip() or "pass insert failed"
        show = runner(["pass", "show", entry], env)
        if show.returncode != 0 or show.stdout.strip() != "protonfs-selftest":
            return False, show.stderr.strip() or "stored secret did not read back"
        runner(["pass", "rm", "-f", entry], env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return True, "inserted, read back and removed a test entry"


def drive_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every proton-drive subprocess inherits (selection only).

    Reads the sticky/override store choice and injects the right env vars. Never
    generates keys or raises -- establishment (which may keygen) is done by
    :func:`establish` at auth-login / doctor --fix time.
    """
    base = dict(os.environ if env is None else env)

    preset = base.get(STORE_ENV)
    if preset == PASS:
        return pass_env(base)
    if preset == KEYCHAIN:
        return secretservice.drive_env(base)

    override = base.get(PROTONFS_STORE_ENV, AUTO).strip().lower()
    if override == PASS:
        return pass_env(base)
    if override == KEYCHAIN:
        return secretservice.drive_env(base)

    choice = read_store_choice()
    if choice == PASS:
        return pass_env(base)
    # keychain sticky, or auto/unset: existing Secret Service behavior.
    return secretservice.drive_env(base)


@dataclass
class EstablishResult:
    """Outcome of :func:`establish` — the env to use plus the chosen store."""

    env: dict[str, str]
    store: str | None
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def establish(
    env: dict[str, str] | None = None,
    *,
    notify: Callable[[str], None] | None = None,
    runner=_run,
) -> EstablishResult:
    """Resolve and, if needed, bootstrap the credentials store (may generate a GPG key).

    Called at establishment moments (auth login, doctor --fix), never on the hot path.
    Persists the resolved store so :func:`drive_env` reads it thereafter.
    """
    base = dict(os.environ if env is None else env)

    preset = base.get(STORE_ENV)
    if preset in _VALID_STORES:
        # User configured proton-drive directly; honor without persisting our own choice.
        chosen_env = pass_env(base) if preset == PASS else secretservice.drive_env(base)
        return EstablishResult(env=chosen_env, store=preset)

    override = base.get(PROTONFS_STORE_ENV, AUTO).strip().lower()
    sticky = read_store_choice()
    forced = override if override in _VALID_STORES else sticky

    if forced == PASS:
        result = ensure_pass_store(runner=runner)
        if result.ready:
            write_store_choice(PASS)
            return EstablishResult(pass_env(base), PASS, result.actions, result.warnings)
        return EstablishResult(base, None, result.actions, result.warnings)
    if forced == KEYCHAIN:
        ss_res = secretservice.ensure_secret_service(base)
        write_store_choice(KEYCHAIN)
        return EstablishResult(ss_res.env, KEYCHAIN, ss_res.actions, ss_res.warnings)

    # auto + unset:
    if not secretservice.is_linux():
        write_store_choice(KEYCHAIN)
        return EstablishResult(base, KEYCHAIN)
    if os.environ.get(secretservice.DISABLE_ENV):
        # Read the process env, not `base`, to stay consistent with
        # secretservice.ensure_secret_service (which gates on os.environ): the
        # opt-out must hold identically for both, or one could bootstrap while
        # the other short-circuits.
        return EstablishResult(base, None)

    ss_res = secretservice.ensure_secret_service(base)
    base = ss_res.env  # carry the resolved/launched bus forward into every return below
    actions = list(ss_res.actions)
    if secretservice.secret_service_state(base) == "ready":
        write_store_choice(KEYCHAIN)
        return EstablishResult(base, KEYCHAIN, actions, ss_res.warnings)

    pass_res = ensure_pass_store(runner=runner)
    if pass_res.ready:
        if notify is not None:
            notify("no OS keyring available; using a protonfs-managed pass store")
        write_store_choice(PASS)
        return EstablishResult(
            pass_env(base), PASS, actions + pass_res.actions, ss_res.warnings + pass_res.warnings
        )
    return EstablishResult(
        base, None, actions + pass_res.actions, ss_res.warnings + pass_res.warnings
    )
