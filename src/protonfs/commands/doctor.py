# src/protonfs/commands/doctor.py
"""`protonfs doctor` — check that this host can actually run proton-drive.

Written for the headless case, because that is where everything that "just works"
on a desktop quietly stops working: no session bus, no Secret Service, or a Secret
Service whose default collection is sealed with a password from a graphical login
that this user will never perform. Each check reports what it found; `--fix`
additionally bootstraps the keyring rather than only describing the problem.

.. versionadded:: 1.0.0
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

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
    """One doctor check result.

    :ivar name: short label for the thing checked (e.g. ``"session bus"``).
    :ivar ok: whether the check passed; only ``ok=False`` fails doctor's exit code.
    :ivar detail: the concrete finding shown after the label.
    :ivar hint: optional remediation advice, printed under the result.
    :ivar warn: render as ``[warn]`` rather than ``[ok]`` -- advisory only, never
        fails the exit code; meaningful only alongside ``ok=True``.
    """

    name: str
    ok: bool
    detail: str
    hint: str | None = None
    # #73: warn-level checks (older-but-supported version, pending migrations) render
    # as [warn] and never fail the doctor exit code; only ok=False does that. `warn`
    # is only meaningful alongside ok=True.
    warn: bool = False


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


# --- #73: version/schema currency checks (pre-upgrade advisor) -------------------------


def version_currency_check(drive: DriveClient) -> Check:
    """proton-drive's installed version against this release's support matrix (#65):
    ok at highest_supported(), warn when older-but-supported, fail when unsupported
    or unparseable."""
    from protonfs.install import highest_supported, is_supported

    installed = drive.drive_version()
    highest = highest_supported()
    if installed is None:
        return Check(
            name="proton-drive version",
            ok=False,
            detail="unparseable `proton-drive version` output",
            hint="Run `protonfs upgrade` to install a supported, verified build.",
        )
    if installed == highest:
        return Check("proton-drive version", True, f"{installed} (highest supported)")
    if is_supported(installed):
        return Check(
            name="proton-drive version",
            ok=True,
            warn=True,
            detail=f"{installed} is supported, but {highest} is the highest supported",
            hint="Run `protonfs upgrade` to move to the highest supported version.",
        )
    return Check(
        name="proton-drive version",
        ok=False,
        detail=f"{installed} is not in this protonfs release's support matrix",
        hint="Run `protonfs upgrade` to install a supported, verified build.",
    )


def upstream_currency_check(upstream_fetch=None) -> Check:
    """Advisory on upstream's Stable release vs highest_supported() -- same message
    contract as `protonfs upgrade` (#66). Fails soft offline: an unreachable manifest
    is an [ok] 'unknown', never a failure."""
    from protonfs.commands.upgrade import (
        _semver_tuple,
        upstream_ahead_message,
        upstream_stable_version,
    )
    from protonfs.install import highest_supported

    fetch = upstream_fetch or upstream_stable_version
    highest = highest_supported()
    upstream = fetch()
    if upstream is None:
        return Check(
            "upstream proton-drive", True, "unknown (manifest unreachable; advisory skipped)"
        )
    if _semver_tuple(upstream) > _semver_tuple(highest):
        return Check(
            name="upstream proton-drive",
            ok=True,
            warn=True,
            detail=f"stable {upstream} is ahead of the supported {highest}",
            hint=upstream_ahead_message(upstream, highest),
        )
    return Check("upstream proton-drive", True, f"stable {upstream}; nothing newer than supported")


def repo_currency_checks(root: Path) -> list[Check]:
    """Inside a protonfs root: index schema version, pending repo-state migrations
    (#67 registry), and config layering sanity. Empty when `root` is not a protonfs
    root -- there is nothing to check."""
    import json

    from protonfs.config import config_path, local_config_path
    from protonfs.index import INDEX_FILE_NAME, INDEX_SCHEMA_VERSION
    from protonfs.migrations import pending_migrations

    if not config_path(root).exists():
        return []
    checks: list[Check] = []

    index_file = root / ".protonfs" / INDEX_FILE_NAME
    if not index_file.exists():
        checks.append(Check("index schema", True, "no index yet (nothing pushed/pulled)"))
    else:
        raw = json.loads(index_file.read_text())
        on_disk = raw.get("schema_version") if isinstance(raw.get("schema_version"), int) else 0
        if on_disk == INDEX_SCHEMA_VERSION:
            checks.append(Check("index schema", True, f"v{on_disk} (current)"))
        else:
            checks.append(
                Check(
                    name="index schema",
                    ok=True,
                    warn=True,
                    detail=f"v{on_disk} on disk; current is v{INDEX_SCHEMA_VERSION}",
                    hint="Run `protonfs upgrade` to persist the index at the current schema.",
                )
            )

    pending = pending_migrations(root)
    if pending:
        ids = ", ".join(m.id for m in pending)
        checks.append(
            Check(
                name="repo migrations",
                ok=True,
                warn=True,
                detail=f"{len(pending)} pending: {ids}",
                hint="Run `protonfs upgrade` (or `protonfs upgrade --check` to preview).",
            )
        )
    else:
        checks.append(Check("repo migrations", True, "none pending"))

    shared = json.loads(config_path(root).read_text())
    gitignore = root / ".protonfs" / ".gitignore"
    local_name = local_config_path(root).name
    layering_problems = []
    if "device_id" in shared:
        layering_problems.append("shared config.json still carries device_id")
    if not gitignore.exists() or local_name not in gitignore.read_text():
        layering_problems.append(f"{local_name} is not gitignored under .protonfs/")
    if layering_problems:
        checks.append(
            Check(
                name="config layering",
                ok=True,
                warn=True,
                detail="; ".join(layering_problems),
                hint="Run `protonfs upgrade` to migrate per-device state out of shared files.",
            )
        )
    else:
        checks.append(Check("config layering", True, "shared/local split is sane"))
    return checks


def run_doctor(fix: bool = False, root: Path | None = None) -> list[Check]:
    """Run every doctor check and return the results (does not print anything).

    Covers the runtime environment (proton-drive binary, D-Bus session bus, Secret
    Service keyring) and, from #73, version/state currency (support matrix, upstream
    advisory, index schema, pending migrations, config layering).

    :param fix: when true, actively bootstrap the keyring rather than only reporting
        on it.
    :param root: the directory whose repo-currency is checked; defaults to the cwd.
    :returns: the ordered list of :class:`Check` results.

    .. seealso:: :func:`render` to print these, :func:`doctor` for the full command.
    """
    checks: list[Check] = []
    root = root or Path.cwd()

    drive = DriveClient()
    installed = drive.binary_available()
    version = drive.version() if installed else None
    if version is not None:
        detail, hint = version.replace("\n", " / "), None
    elif installed:
        # Present but not runnable -- almost always the keyring, since proton-drive
        # loads its session before it will answer even `version`. Saying "not found"
        # here would send the user to reinstall a binary that is sitting right there.
        detail = f"{drive.binary} is installed but failed to run"
        hint = "Usually the keyring: see the checks below, then `protonfs doctor --fix`."
    else:
        detail, hint = "not found on PATH", "Run `protonfs install-drive`."
    checks.append(
        Check(name="proton-drive binary", ok=version is not None, detail=detail, hint=hint)
    )

    # #73: currency checks -- the pre-upgrade advisor. Version checks only make sense
    # with a runnable binary; the repo checks run regardless.
    if version is not None:
        checks.append(version_currency_check(drive))
        checks.append(upstream_currency_check())
    checks.extend(repo_currency_checks(root))

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
    """Print each check as ``[ok]``/``[warn]``/``[FAIL]`` and return overall success.

    :param checks: the results from :func:`run_doctor`.
    :param console_echo: sink for each line (defaults to :func:`click.echo`;
        overridable for tests).
    :returns: ``True`` when no check failed (``[warn]`` results do not count as
        failures).
    """
    all_ok = True
    for check in checks:
        mark = ("warn" if check.warn else "ok  ") if check.ok else "FAIL"
        console_echo(f"[{mark}] {check.name}: {check.detail}")
        if check.hint:
            console_echo(f"       -> {check.hint}")
        all_ok = all_ok and check.ok
    return all_ok


def doctor(fix: bool = False) -> bool:
    """Run the checks, print them, and return whether the host can run proton-drive.

    :param fix: when true, bootstrap the keyring rather than only reporting on it.
    :returns: ``True`` when every check passed (warnings allowed).

    .. seealso:: :func:`run_doctor` (the checks) and :func:`render` (the output).
    """
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
