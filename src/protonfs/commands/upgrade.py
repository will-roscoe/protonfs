# src/protonfs/commands/upgrade.py
"""`protonfs upgrade`: bring proton-drive and this repo's on-disk state current (#66).

`install-drive` installs the pinned default but has no policy voice; `upgrade` adds it:
detect the installed proton-drive version, upgrade it to -- and never past -- this
protonfs release's `highest_supported()`, advise (without acting) when upstream has
something newer, and run any pending repo-state migrations (#67) when inside a
protonfs root.

The binary swap is atomic and verify-first: `install_drive` stages the download via a
`.part` file, verifies its SHA-512, and only then `replace()`s the final binary -- a
failed download or checksum mismatch never leaves a broken `proton-drive` behind.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import click

from protonfs import __version__
from protonfs.config import config_path
from protonfs.drive import DriveClient
from protonfs.install import DOWNLOAD_TIMEOUT, highest_supported, install_drive
from protonfs.migrations import pending_migrations, run_migrations

# Same official release manifest the repin script reads; upstream's current Stable
# version lives in it. Fetched only for the "upstream is ahead" advisory.
MANIFEST_URL = "https://proton.me/download/drive/cli/version.json"


def _semver_tuple(version: str) -> tuple[int, ...]:
    """Parse a plain ``X.Y.Z`` version into a comparable integer tuple.

    :param version: a dotted numeric version string (no pre-release suffix).
    :returns: the components as an ``int`` tuple, orderable with ``<``/``>``.
    """
    return tuple(int(part) for part in version.split("."))


def upstream_stable_version(opener=None) -> str | None:
    """Upstream's current Stable proton-drive version from the official version.json
    manifest, or None on ANY failure (offline, HTTP error, manifest shape change).

    The upstream check is an advisory and must fail soft (#66): being offline never
    blocks upgrading to the pinned version -- the caller just skips the advisory.
    """
    opener = opener or (lambda url: urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT))
    try:
        with opener(MANIFEST_URL) as resp:
            manifest = json.load(resp)
        stable = next(r for r in manifest["Releases"] if r.get("CategoryName") == "Stable")
        version = stable["Version"]
    except Exception:  # noqa: BLE001 -- advisory only; any failure means "unknown"
        return None
    return version if isinstance(version, str) else None


def upstream_ahead_message(upstream: str, highest: str) -> str:
    """The advisory printed when upstream Stable is newer than this release's highest
    supported proton-drive. Message contract shared with `protonfs doctor` (#73): a
    protonfs release only ever upgrades proton-drive to the highest version it
    supports; a newer upstream requires a newer protonfs."""
    return (
        f"upstream proton-drive {upstream} exists but this protonfs ({__version__}) "
        f"supports at most {highest}; upgrade protonfs to get {upstream}."
    )


def run_upgrade(
    root: Path,
    *,
    check: bool = False,
    drive_only: bool = False,
    repo_only: bool = False,
    client: DriveClient | None = None,
    installer=install_drive,
    upstream_fetch=upstream_stable_version,
) -> int:
    """Run (or, with `check`, preview) the upgrade. Returns the process exit code:
    with `check`, 0 == fully current and 1 == an upgrade/migration is available;
    without it, 0 on success (failures raise)."""
    client = client or DriveClient()
    highest = highest_supported()
    actions_available = False

    if not repo_only:
        installed = client.drive_version()
        if installed is None:
            click.echo(f"proton-drive: not installed; highest supported is {highest}.")
        else:
            click.echo(f"proton-drive: installed {installed}; highest supported {highest}.")

        needs_binary = installed is None or _semver_tuple(installed) < _semver_tuple(highest)
        if installed is not None and _semver_tuple(installed) > _semver_tuple(highest):
            # Never downgrade: a newer-than-supported binary was the user's explicit
            # doing (env override or manual install); flag it, leave it.
            click.echo(
                f"  installed {installed} is newer than this protonfs supports "
                f"({highest}); leaving it in place."
            )
            needs_binary = False

        if needs_binary:
            actions_available = True
            if check:
                click.echo(f"  would upgrade proton-drive to {highest}.")
            else:
                result = installer(version=highest)
                click.echo(
                    f"  upgraded proton-drive to {highest} at {result.path} "
                    f"(SHA-512 verified, swapped atomically)."
                )
                for warning in result.warnings:
                    click.echo(f"  ! {warning}")
                # The binary changed under an existing session: verify it survived.
                if client.is_authenticated():
                    click.echo("  session: still authenticated.")
                else:
                    click.echo(
                        "  session: NOT authenticated after the upgrade -- run "
                        "`protonfs auth login` to sign in again."
                    )
        elif installed == highest:
            click.echo("  proton-drive is current.")

        upstream = upstream_fetch()
        if upstream is not None and _semver_tuple(upstream) > _semver_tuple(highest):
            click.echo(upstream_ahead_message(upstream, highest))

    if not drive_only:
        if config_path(root).exists():
            pending = pending_migrations(root)
            if not pending:
                click.echo("repo state: current (no pending migrations).")
            else:
                actions_available = True
                click.echo(f"repo state: {len(pending)} pending migration(s):")
                for migration in pending:
                    verb = "would apply" if check else "applying"
                    click.echo(f"  {verb}: {migration.id} -- {migration.description}")
                if not check:
                    run_migrations(root)
                    click.echo("repo state: migrations applied.")
        elif repo_only:
            raise click.ClickException(
                f"not inside a protonfs root ({root} has no .protonfs/config.json); "
                "nothing to migrate."
            )
        else:
            click.echo("repo state: not inside a protonfs root; skipping migrations.")

    if check:
        click.echo(
            "upgrade --check: "
            + ("everything is current." if not actions_available else "upgrade available.")
        )
        return 1 if actions_available else 0
    return 0
