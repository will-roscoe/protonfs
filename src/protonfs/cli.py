from __future__ import annotations

import click

from protonfs import __version__


@click.group()
@click.version_option(__version__, prog_name="protonfs")
def main() -> None:
    """Sync a local directory tree with Proton Drive."""


@main.command()
def setup() -> None:
    """Install/verify the proton-drive CLI, init .protonfs/, migrate off git-lfs if present."""
    raise click.ClickException("not yet implemented")


@main.command()
@click.argument("path", required=False)
def status(path: str | None) -> None:
    """Summarize sync state (counts by local-only/remote-only/synced/conflict)."""
    raise click.ClickException("not yet implemented")


@main.command()
@click.argument("path", required=False)
@click.option("--remote", is_flag=True, help="Force a live Drive listing instead of the index.")
@click.option("--trash", is_flag=True, help="List /trash instead.")
def ls(path: str | None, remote: bool, trash: bool) -> None:
    """List tracked files with their sync state."""
    raise click.ClickException("not yet implemented")


@main.command()
@click.argument("path", required=False)
@click.option("--resolve", type=click.Choice(["merge", "keep-both", "replace", "skip"]))
@click.option("--dry-run", is_flag=True)
def push(path: str | None, resolve: str | None, dry_run: bool) -> None:
    """Upload local-only/changed files to Drive."""
    raise click.ClickException("not yet implemented")


@main.command()
@click.argument("path", required=False)
@click.option("--resolve", type=click.Choice(["merge", "keep-both", "replace", "skip"]))
@click.option("--dry-run", is_flag=True)
def pull(path: str | None, resolve: str | None, dry_run: bool) -> None:
    """Download remote-only/changed files from Drive."""
    raise click.ClickException("not yet implemented")


@main.command()
@click.argument("path")
@click.option("-r", "--recursive", is_flag=True)
@click.option("-f", "--force", is_flag=True, help="Permanently delete (trash, then delete).")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
def rm(path: str, recursive: bool, force: bool, yes: bool) -> None:
    """Remove a file/directory from Drive (trash by default, -f for permanent)."""
    raise click.ClickException("not yet implemented")


@main.command()
@click.argument("path")
def restore(path: str) -> None:
    """Restore a trashed file/directory on Drive."""
    raise click.ClickException("not yet implemented")


if __name__ == "__main__":
    main()
