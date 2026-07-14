# src/protonfs/commands/doctor.py
"""`protonfs doctor` — check that this host can actually run proton-drive.

Written for the headless case, because that is where everything that "just works"
on a desktop quietly stops working: no session bus, no Secret Service, or a Secret
Service whose default collection is sealed with a password from a graphical login
that this user will never perform. Each check reports what it found; `--fix`
additionally bootstraps the keyring rather than only describing the problem.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

import click

from protonfs.drive import DriveClient
from protonfs.secretservice import (
    BUS_ENV,
    DISABLE_ENV,
    SecretServiceError,
    drive_env,
    ensure_secret_service,
    is_linux,
    keyring_password_file,
    probe_secret_service,
    resolve_bus,
    secret_service_state,
    secrets_home,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str | None = None


_STATE_HINTS = {
    "missing": (
        "No Secret Service owns org.freedesktop.secrets. proton-drive has nowhere to "
        "store its session. Run `protonfs doctor --fix` to start gnome-keyring."
    ),
    "locked": (
        "A Secret Service is running but its default collection is LOCKED -- this is the "
        "`Cannot create an item in a locked collection` failure. It is usually the "
        "login.keyring left behind by a graphical login, sealed with a password you "
        "cannot supply over SSH. `protonfs doctor --fix` sidesteps it with a "
        "protonfs-owned keyring."
    ),
    "unknown": (
        "Could not determine the Secret Service state (gdbus missing?). Install glib2 "
        "so protonfs can diagnose the keyring."
    ),
}


def run_doctor(fix: bool = False) -> list[Check]:
    checks: list[Check] = []

    drive = DriveClient()
    version = drive.version()
    checks.append(
        Check(
            name="proton-drive binary",
            ok=version is not None,
            detail=version or "not found on PATH",
            hint=None if version else "Run `protonfs install-drive`.",
        )
    )

    if not is_linux():
        checks.append(
            Check("keyring", True, "not Linux; proton-drive uses the platform keychain")
        )
        return checks

    if os.environ.get(DISABLE_ENV):
        checks.append(
            Check("keyring", True, f"{DISABLE_ENV} is set; keyring bootstrap disabled")
        )
        return checks

    for tool, package in (
        ("dbus-launch", "dbus-x11 / dbus"),
        ("gnome-keyring-daemon", "gnome-keyring"),
        ("gdbus", "glib2"),
    ):
        found = shutil.which(tool)
        checks.append(
            Check(
                name=f"tool: {tool}",
                ok=found is not None,
                detail=found or "not installed",
                hint=None if found else f"Install {package} (no sudo? ask your admin).",
            )
        )

    env = dict(os.environ)
    if fix:
        try:
            result = ensure_secret_service(env)
            env = result.env
            for action in result.actions:
                click.echo(f"  fix: {action}")
            for warning in result.warnings:
                click.echo(f"  ! {warning}")
        except SecretServiceError as exc:
            checks.append(Check("keyring bootstrap", False, str(exc)))
            return checks
    else:
        # Report on the environment as proton-drive would see it, without creating
        # anything: a read-only doctor must not launch daemons as a side effect.
        try:
            address, how = resolve_bus(env)
            env[BUS_ENV] = address
            checks.append(Check("session bus", True, f"{how}: {address}"))
        except SecretServiceError as exc:
            checks.append(Check("session bus", False, str(exc)))
            return checks

    if fix:
        checks.append(Check("session bus", True, env.get(BUS_ENV, "?")))

    state = secret_service_state(env)
    checks.append(
        Check(
            name="secret service",
            ok=state == "ready",
            detail=state,
            hint=_STATE_HINTS.get(state) if state != "ready" else None,
        )
    )

    ok, detail = probe_secret_service(env)
    checks.append(
        Check(
            name="keyring read/write",
            ok=ok,
            detail=detail,
            hint=None if ok else "Run `protonfs doctor --fix`.",
        )
    )

    checks.append(Check("keyring store", True, str(secrets_home() / "keyrings")))
    if keyring_password_file().exists():
        checks.append(Check("keyring password", True, f"{keyring_password_file()} (0600)"))

    return checks


def render(checks: list[Check], console_echo=click.echo) -> bool:
    all_ok = True
    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        console_echo(f"[{mark}] {check.name}: {check.detail}")
        if check.hint:
            console_echo(f"       -> {check.hint}")
        all_ok = all_ok and check.ok
    return all_ok


def doctor(fix: bool = False) -> bool:
    checks = run_doctor(fix=fix)
    ok = render(checks)
    if ok:
        click.echo("\nThis host can run proton-drive. Next: `protonfs auth login`.")
    elif not fix:
        click.echo("\nRe-run as `protonfs doctor --fix` to repair what protonfs can.")
    return ok


def shell_exports() -> list[str]:
    """`VAR=value` lines that make the *current shell* match the environment protonfs
    hands proton-drive. Only needed to run the `proton-drive` binary by hand; every
    protonfs command sets this up for itself."""
    env = drive_env()
    return [f"{BUS_ENV}={env[BUS_ENV]}"] if env.get(BUS_ENV) else []
