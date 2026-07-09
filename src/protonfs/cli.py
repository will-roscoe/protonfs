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
        from protonfs.drive import DriveAuthError, DriveError

        try:
            return func(*args, **kwargs)
        except DriveAuthError as exc:
            raise click.ClickException(
                f"{exc}\nRun `proton-drive auth login` to re-authenticate, "
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

    ctx = load_context()
    result = push_files(ctx, path, resolve, dry_run)
    click.echo(
        f"transferred={result.transferred_items} skipped={result.skipped_items} "
        f"failed={result.failed_items}"
    )
    for failure in result.failures:
        click.echo(f"  FAILED {failure['name']}: {failure['error']}")
    if result.failed_items:
        raise click.exceptions.Exit(1)


@main.command()
@click.argument("path", required=False)
@click.option("--resolve", type=click.Choice(["merge", "keep-both", "replace", "skip"]))
@click.option("--dry-run", is_flag=True)
@_drive_error_boundary
def pull(path: str | None, resolve: str | None, dry_run: bool) -> None:
    """Download remote-only/changed files from Drive."""
    from protonfs.commands.pull import pull as pull_files
    from protonfs.context import load_context

    ctx = load_context()
    result = pull_files(ctx, path, resolve, dry_run)
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

    ctx = load_context()
    rm_path(ctx, path, recursive, force, confirmed=yes)


@main.command()
@click.argument("path")
@_drive_error_boundary
def restore(path: str) -> None:
    """Restore a trashed file/directory on Drive."""
    from protonfs.commands.restore import restore as restore_path
    from protonfs.context import load_context

    ctx = load_context()
    restore_path(ctx, path)


if __name__ == "__main__":
    main()
