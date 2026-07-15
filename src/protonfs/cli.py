from __future__ import annotations

import functools

import click

from protonfs import __version__


def _drive_error_boundary(func):
    """Wrap a command so DriveError/DriveAuthError become clean ClickExceptions.

    Runtime commands call into the Drive CLI; a mid-run failure there (auth
    expiry, network, missing path) would otherwise surface as a raw Python
    traceback instead of a readable message.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from protonfs.drive import DriveAuthError, DriveError, DriveSecretsError
        from protonfs.locking import RepoLockError

        try:
            return func(*args, **kwargs)
        except RepoLockError as exc:
            # Not a Drive fault, but this boundary already fronts every mutating command,
            # so surface the "another process holds the lock" message cleanly here too.
            raise click.ClickException(str(exc)) from exc
        except DriveSecretsError as exc:
            # Ordered before DriveAuthError: a keyring fault is not an auth fault, and
            # its message already carries its own remedy (`protonfs doctor --fix`).
            raise click.ClickException(str(exc)) from exc
        except DriveAuthError as exc:
            raise click.ClickException(
                f"{exc}\nRun `protonfs auth login` to re-authenticate, "
                "then retry this command."
            ) from exc
        except DriveError as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper


@click.group()
@click.version_option(__version__, prog_name="protonfs")
def main() -> None:
    """Sync a local directory tree with Proton Drive."""


@main.command()
@click.option("--dry-run", is_flag=True, help="Preview the LFS migration without making changes.")
def setup(dry_run: bool) -> None:
    """Install/verify the proton-drive CLI, init .protonfs/, migrate off git-lfs if present."""
    from pathlib import Path

    from protonfs.commands.setup import run_setup

    run_setup(Path.cwd(), dry_run=dry_run)


@main.command()
@click.argument("path", required=False)
def status(path: str | None) -> None:
    """Summarize sync state (counts by local-only/remote-only/synced/conflict)."""
    from protonfs.commands.status import compute_status
    from protonfs.context import load_context
    from protonfs.diff import SyncState

    ctx = load_context()
    counts = compute_status(ctx, path)
    for state in SyncState:
        click.echo(f"{state.value}: {counts.get(state.value, 0)}")


@main.command()
@click.argument("path", required=False)
@click.option("--remote", is_flag=True, help="Force a live Drive listing instead of the index.")
@click.option("--trash", is_flag=True, help="List /trash instead.")
@_drive_error_boundary
def ls(path: str | None, remote: bool, trash: bool) -> None:
    """List tracked files with their sync state."""
    from rich.console import Console

    from protonfs.commands.ls import render_ls
    from protonfs.context import load_context

    ctx = load_context()
    render_ls(ctx, path, remote, trash, Console())


@main.command()
@click.argument("path", required=False)
@click.option("--resolve", type=click.Choice(["merge", "keep-both", "replace", "skip"]))
@click.option("--dry-run", is_flag=True)
@_drive_error_boundary
def push(path: str | None, resolve: str | None, dry_run: bool) -> None:
    """Upload local-only/changed files to Drive."""
    from protonfs.commands.push import push as push_files
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    with repo_lock(ctx.root):
        result = push_files(ctx, path, resolve, dry_run)
    click.echo(
        f"transferred={result.transferred_items} skipped={result.skipped_items} "
        f"failed={result.failed_items}"
    )
    for failure in result.failures:
        click.echo(f"  FAILED {failure['name']}: {failure['error']}")
    if result.failed_items:
        if not resolve:
            click.echo(
                "  -> these are remote conflicts; re-run with "
                "--resolve=merge|keep-both|replace|skip to resolve them."
            )
        raise click.exceptions.Exit(1)


@main.command()
@click.argument("path", required=False)
@click.option("--resolve", type=click.Choice(["merge", "keep-both", "replace", "skip"]))
@click.option("--dry-run", is_flag=True)
@click.option(
    "--refresh",
    is_flag=True,
    help="Discover remote files (seed the index) before pulling.",
)
@_drive_error_boundary
def pull(path: str | None, resolve: str | None, dry_run: bool, refresh: bool) -> None:
    """Download remote-only/changed files from Drive."""
    from protonfs.commands.pull import pull as pull_files
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    if not refresh and not ctx.index.all():
        click.echo("index empty; run `protonfs refresh` first (or `pull --refresh`)")
        return
    with repo_lock(ctx.root):
        result = pull_files(ctx, path, resolve, dry_run, refresh=refresh)
    click.echo(
        f"transferred={result.transferred_items} skipped={result.skipped_items} "
        f"failed={result.failed_items}"
    )
    for failure in result.failures:
        click.echo(f"  FAILED {failure['name']}: {failure['error']}")
    if result.failed_items:
        raise click.exceptions.Exit(1)


@main.command()
@click.argument("path")
@click.option("-r", "--recursive", is_flag=True)
@click.option("-f", "--force", is_flag=True, help="Permanently delete (trash, then delete).")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@_drive_error_boundary
def rm(path: str, recursive: bool, force: bool, yes: bool) -> None:
    """Remove a file/directory from Drive (trash by default, -f for permanent)."""
    from protonfs.commands.rm import rm as rm_path
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    with repo_lock(ctx.root):
        rm_path(ctx, path, recursive, force, confirmed=yes)


@main.command()
@click.argument("path")
@_drive_error_boundary
def restore(path: str) -> None:
    """Restore a trashed file/directory on Drive."""
    from protonfs.commands.restore import restore as restore_path
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    with repo_lock(ctx.root):
        restore_path(ctx, path)


@main.command()
@click.argument("path", required=False)
@click.option("--prune", is_flag=True, help="Drop index entries for files deleted on the remote.")
@_drive_error_boundary
def refresh(path: str | None, prune: bool) -> None:
    """Discover remote files and seed the local index (metadata-only)."""
    from protonfs.commands.refresh import refresh as refresh_index
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    with repo_lock(ctx.root):
        result = refresh_index(ctx, path, prune)
    click.echo(f"Discovered {result.seeded} new remote file(s) (metadata-only).")
    if result.remote_changed:
        click.echo(f"  {result.remote_changed} file(s) changed on the remote (remote-changed):")
        for p in result.changed_paths:
            click.echo(f"      {p}")
        click.echo(
            "    -> `protonfs pull --resolve=replace <path>` to take the remote version, "
            "or `protonfs push --resolve=replace <path>` to overwrite it with your local copy."
        )
    if result.remote_deleted:
        verb = "pruned" if prune else "found"
        click.echo(f"  {result.remote_deleted} file(s) deleted on the remote ({verb}):")
        for p in result.deleted_paths:
            click.echo(f"      {p}")
        if not prune:
            click.echo("    -> `protonfs refresh --prune` to drop them from your local index.")
    if result.seeded:
        click.echo(f"Run `protonfs pull` to download the {result.seeded} discovered file(s).")


@main.command("install-drive")
@click.option("--version", default=None, help="proton-drive version to install (default: pinned).")
@click.option(
    "--skip-keyring",
    is_flag=True,
    help="Do not prepare the OS keyring after installing.",
)
def install_drive_cmd(version: str | None, skip_keyring: bool) -> None:
    """Download and verify the official proton-drive CLI binary."""
    from protonfs.install import InstallError, install_drive
    from protonfs.secretservice import SecretServiceError, ensure_secret_service

    try:
        result = install_drive(version=version)
    except InstallError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Installed proton-drive to {result.path} (SHA-512 verified).")
    for warning in result.warnings:
        click.echo(f"  ! {warning}")

    # Prepare the keyring here, not at first login. Otherwise the failure lands after
    # the user has completed a browser sign-in, and the session is discarded.
    if not skip_keyring:
        try:
            secrets = ensure_secret_service()
        except SecretServiceError as exc:
            raise click.ClickException(
                f"proton-drive is installed, but this host has no usable OS keyring "
                f"to store its session in:\n  {exc}\n"
                f"Run `protonfs doctor` for details."
            ) from exc
        for action in secrets.actions:
            click.echo(f"  keyring: {action}")
        for warning in secrets.warnings:
            click.echo(f"  ! {warning}")

    click.echo("Next: run `protonfs auth login` to authenticate.")


@main.command()
@click.option("--fix", is_flag=True, help="Repair what protonfs can (bootstrap the keyring).")
def doctor(fix: bool) -> None:
    """Check this host can run proton-drive (binary, session bus, OS keyring)."""
    from protonfs.commands.doctor import doctor as run

    if not run(fix=fix):
        raise click.exceptions.Exit(1)


@main.command("shell-init")
def shell_init() -> None:
    """Print shell exports so `proton-drive` run by hand sees the same keyring."""
    from protonfs.commands.doctor import shell_exports

    for line in shell_exports():
        click.echo(f"export {line}")


@main.command()
@click.argument("action", type=click.Choice(["login", "logout", "status"]))
@_drive_error_boundary
def auth(action: str) -> None:
    """Authenticate the proton-drive CLI: login | logout | status (passthrough)."""
    from protonfs.commands.auth import auth_passthrough

    code = auth_passthrough(action)
    if code != 0:
        raise click.exceptions.Exit(code)


if __name__ == "__main__":
    main()
