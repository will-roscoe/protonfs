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
@click.option(
    "--migrate-lfs/--no-migrate-lfs",
    default=None,
    help=(
        "Force or skip the repo-wide git-LFS migration. Default: migrate only when this "
        "directory is the git toplevel, so setting up a subdirectory never migrates the "
        "enclosing repo off LFS."
    ),
)
@_drive_error_boundary
def setup(dry_run: bool, migrate_lfs: bool | None) -> None:
    """Install/verify the proton-drive CLI, init .protonfs/, migrate off git-lfs if present."""
    from pathlib import Path

    from protonfs.commands.setup import run_setup

    run_setup(Path.cwd(), dry_run=dry_run, migrate=migrate_lfs)


@main.command()
@click.option("--dry-run", is_flag=True, help="List what would be removed; delete nothing.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@_drive_error_boundary
def deinit(dry_run: bool, yes: bool) -> None:
    """Remove .protonfs/ from this directory: clean teardown of a protonfs root.

    Only protonfs's own bookkeeping under .protonfs/ (config, index, ignore/include,
    control .gitattributes/.gitignore) is removed -- synced payload files, local or
    remote, are never touched.
    """
    from pathlib import Path

    from protonfs.commands.deinit import ensure_deinit_target, run_deinit
    from protonfs.locking import repo_lock

    root = Path.cwd()
    ensure_deinit_target(root)
    with repo_lock(root):
        run_deinit(root, dry_run=dry_run, yes=yes)


@main.command()
@click.argument("path", required=False)
def status(path: str | None) -> None:
    """Summarize sync state (counts by local-only/remote-only/synced/conflict).

    Exit code, so an unattended caller can branch without parsing the counts:
    0 = clean (everything synced or intentionally remote-only), 1 = drift present
    (something to push/pull/prune), 2 = conflict present (needs a human or --resolve).
    Conflict outranks drift when both are present.
    """
    from protonfs.commands.status import compute_status, status_exit_code
    from protonfs.context import load_context
    from protonfs.diff import SyncState

    ctx = load_context()
    counts = compute_status(ctx, path)
    for state in SyncState:
        click.echo(f"{state.value}: {counts.get(state.value, 0)}")
    code = status_exit_code(counts)
    if code != 0:
        raise click.exceptions.Exit(code)


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
        under_delivered = [f for f in result.failures if f.get("kind") == "under-delivered"]
        conflicts = [f for f in result.failures if f.get("kind") != "under-delivered"]
        if under_delivered:
            click.echo(
                f"  -> {len(under_delivered)} file(s) were reported transferred but did not "
                "land on Drive; they were NOT indexed and will be retried on the next push."
            )
        if conflicts and not resolve:
            click.echo(
                "  -> these are remote conflicts; re-run with "
                "--resolve=merge|keep-both|replace|skip to resolve them."
            )
        raise click.exceptions.Exit(1)


@main.command()
@click.argument("path", required=False)
@click.option(
    "--resolve",
    type=click.Choice(["remote", "local", "both"]),
    help=(
        "How to reconcile a file that changed on BOTH sides since the last sync: "
        "remote=overwrite local, local=keep local (stays queued for push), "
        "both=fetch the remote copy under a .remote suffix for a manual merge. "
        "Without this, pull leaves diverged files untouched and reports them."
    ),
)
@click.option("--dry-run", is_flag=True)
@click.option(
    "--refresh",
    is_flag=True,
    help="Discover remote files (seed the index) before pulling.",
)
@_drive_error_boundary
def pull(path: str | None, resolve: str | None, dry_run: bool, refresh: bool) -> None:
    """Download remote-only/changed files from Drive.

    Diverged files (edited locally AND changed on the remote since the last sync) are
    left untouched unless you pass --resolve; they are reported and pull exits non-zero.
    """
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
@click.argument("path", required=False)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip re-verifying files against the remote before deleting local bytes (unsafe).",
)
@click.option("--dry-run", is_flag=True, help="Preview what would be offloaded; delete nothing.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@_drive_error_boundary
def offload(path: str | None, no_verify: bool, dry_run: bool, yes: bool) -> None:
    """Delete local bytes of protonfs-tracked files confirmed present on Drive.

    The inverse of `pull`: reclaims local disk space while leaving the Drive copy
    intact. Reversible -- a later `pull` restores the file in full. By default every
    file is re-verified against a live remote listing (not just the index) before its
    local copy is deleted; pass --no-verify to skip that check.
    """
    from protonfs.commands.offload import offload as offload_files
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    verify = not no_verify

    if not yes and not dry_run:
        click.confirm(
            f"Delete local copies of tracked files under '{path or '.'}' "
            f"(Drive copies are kept)?",
            abort=True,
        )

    with repo_lock(ctx.root):
        result = offload_files(ctx, path, verify=verify, dry_run=dry_run)

    verb = "would offload" if dry_run else "offloaded"
    click.echo(
        f"{verb}={result.offloaded} bytes_reclaimed={result.bytes_reclaimed} "
        f"skipped_unverified={result.skipped_unverified} skipped_modified={result.skipped_modified}"
    )
    for p in result.offloaded_paths:
        click.echo(f"  {'WOULD OFFLOAD' if dry_run else 'offloaded'} {p}")
    if result.skipped_unverified:
        click.echo(
            f"  -> {result.skipped_unverified} file(s) could not be confirmed on the "
            "remote and were left untouched locally:"
        )
        for p in result.skipped_paths:
            click.echo(f"      {p}")
    if result.skipped_modified:
        click.echo(
            f"  -> {result.skipped_modified} file(s) have unsynced local edits and were "
            "left untouched; `push` them first, then offload:"
        )
        for p in result.modified_paths:
            click.echo(f"      {p}")


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
@click.option(
    "--check",
    is_flag=True,
    help="Report what would happen; change nothing. Exit 0 if fully current, 1 if an "
    "upgrade or migration is available.",
)
@click.option("--drive-only", is_flag=True, help="Only the proton-drive binary; skip migrations.")
@click.option("--repo-only", is_flag=True, help="Only repo-state migrations; skip the binary.")
@_drive_error_boundary
def upgrade(check: bool, drive_only: bool, repo_only: bool) -> None:
    """Upgrade proton-drive to the highest supported version + migrate repo state.

    A protonfs release only ever upgrades proton-drive to its own highest supported
    version; a newer upstream release requires a newer protonfs (reported, never
    installed). Inside a protonfs root, pending repo-state migrations run too.
    """
    from pathlib import Path

    from protonfs.commands.upgrade import run_upgrade
    from protonfs.install import InstallError

    if drive_only and repo_only:
        raise click.UsageError("--drive-only and --repo-only are mutually exclusive.")
    try:
        code = run_upgrade(Path.cwd(), check=check, drive_only=drive_only, repo_only=repo_only)
    except InstallError as exc:
        raise click.ClickException(str(exc)) from exc
    if code != 0:
        raise click.exceptions.Exit(code)


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
    """Authenticate the proton-drive CLI: login | logout | status."""
    from protonfs.commands.auth import auth_passthrough, auth_status

    if action == "status":
        code = auth_status()
    else:
        code = auth_passthrough(action)
    if code != 0:
        raise click.exceptions.Exit(code)


@main.group()
def trash() -> None:
    """Inspect and empty Proton Drive's trash (#70)."""


@trash.command("list")
@_drive_error_boundary
def trash_list_cmd() -> None:
    """List /trash: name, original parent (best-effort), same-name duplicate count.

    A nonzero duplicate count is the ambiguity `restore` refuses to resolve on its
    own (#56): proton-drive resolves /trash paths by name, first match wins.
    """
    from rich.console import Console

    from protonfs.commands.trash import list_trash
    from protonfs.context import load_context

    ctx = load_context()
    list_trash(ctx, Console())


@trash.command("empty")
@click.option("--yes", is_flag=True, help="Skip the typed confirmation prompt.")
@_drive_error_boundary
def trash_empty_cmd(yes: bool) -> None:
    """Permanently empty /trash for the whole account (irreversible, account-global).

    Requires typing an exact confirmation phrase unless --yes is passed. This is NOT
    scoped to this repo's remote_root -- it empties every trashed item on the account.
    """
    from protonfs.commands.trash import empty_trash
    from protonfs.context import load_context

    ctx = load_context()
    empty_trash(ctx, confirmed=yes)


@main.group()
def config() -> None:
    """Get/set protonfs config (#21): env > local > shared > global > built-in default."""


@config.command("get")
@click.argument("key")
def config_get_cmd(key: str) -> None:
    """Print the RESOLVED value of KEY (e.g. `remote_root`, `defaults.low_io`)."""
    from pathlib import Path

    from protonfs.commands.config import config_get

    click.echo(config_get(Path.cwd(), key))


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option(
    "--global", "scope_global", is_flag=True, help="Write to the global user config."
)
@click.option(
    "--local", "scope_local", is_flag=True, help="Write to the per-device local config."
)
def config_set_cmd(key: str, value: str, scope_global: bool, scope_local: bool) -> None:
    """Set KEY = VALUE. Default scope is the shared per-repo config (committed)."""
    from pathlib import Path

    from protonfs.commands.config import config_set

    if scope_global and scope_local:
        raise click.ClickException("--global and --local are mutually exclusive.")
    scope = "global" if scope_global else "local" if scope_local else "shared"
    path = config_set(Path.cwd(), key, value, scope=scope)
    click.echo(f"Set {key} in {path}.")


if __name__ == "__main__":
    main()
