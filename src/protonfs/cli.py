"""The ``protonfs`` command-line entry point: the Click command tree.

Each command is a thin wrapper that lazily imports and calls its
:mod:`protonfs.commands` implementation, so ``--help`` and shell completion stay
fast (no Drive/keyring imports until a command actually runs). The frozen 1.0
surface these commands expose is documented in ``docs/stability.rst``.

.. versionadded:: 1.0.0
"""

from __future__ import annotations

import functools

import click

from protonfs import __version__
from protonfs.argv import reorder_argv

# Shown at the bottom of every subcommand's --help so the global flags are discoverable
# there, not only on `protonfs --help`. They are accepted in any position (see
# PositionalFlagGroup); this is documentation, they are not per-command options.
_GLOBAL_OPTIONS_HELP = (
    "Global options (accepted before or after the command, e.g. `protonfs push -v PATH`):\n"
    "  -v, --verbose                       increase console detail (-v..-vvvv)\n"
    "  --progress-inline/--progress-lines  progress render style\n"
    "  --event-log/--no-event-log          write .protonfs/events.log\n"
    "Run `protonfs --help` for details."
)


class _GlobalsEpilog:
    """Mixin: append the global-options block to a command/group's ``--help``."""

    def format_epilog(self, ctx, formatter):
        """Render the normal epilog, then the shared global-options block."""
        super().format_epilog(ctx, formatter)
        formatter.write_paragraph()
        for line in _GLOBAL_OPTIONS_HELP.splitlines():
            formatter.write_text(line)


class _EpilogCommand(_GlobalsEpilog, click.Command):
    """A leaf command whose ``--help`` documents the global options."""


class _EpilogGroup(_GlobalsEpilog, click.Group):
    """A subgroup (config/trash) whose ``--help`` documents the global options, and
    whose own subcommands inherit :class:`_EpilogCommand`."""

    command_class = _EpilogCommand


class PositionalFlagGroup(click.Group):
    """The top-level group: rewrites argv so global flags and subcommand flags may
    appear in any position (see :func:`~protonfs.argv.reorder_argv`), and hands its
    subcommands/subgroups the global-options epilog classes.

    .. versionadded:: 1.4.0
    """

    command_class = _EpilogCommand
    group_class = _EpilogGroup

    def parse_args(self, ctx, args):
        """Reorder ``args`` into Click's canonical form before the normal parse."""
        return super().parse_args(ctx, reorder_argv(list(args), frozenset(self.commands)))


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
                f"{exc}\nRun `protonfs auth login` to re-authenticate, then retry this command."
            ) from exc
        except DriveError as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper


def _normalize_paths(paths: tuple[str, ...]) -> list[str | None]:
    """Collapse a variadic ``PATH...`` argument into the subpath list a command loops over.

    #92: shell globs expand to several arguments (``protonfs pull 03pol02*`` arrives as
    five paths), so every PATH-taking command accepts many. No paths (or ``.``/``/``)
    means the whole repo (``[None]``); otherwise dedupe (order-preserving, trailing
    slashes stripped) and drop paths nested inside another given path, so overlapping
    pathspecs are never processed twice.
    """
    stripped: list[str] = []
    for raw in paths:
        p = raw.rstrip("/")
        if p in ("", "."):
            return [None]  # repo root subsumes every other pathspec
        if p not in stripped:
            stripped.append(p)
    if not stripped:
        return [None]
    return [p for p in stripped if not any(r != p and p.startswith(f"{r}/") for r in stripped)]


def _accumulate_transfer(total, part) -> None:
    """Fold one per-path TransferResult into the running total (multi-path loops, #92)."""
    total.transferred_items += part.transferred_items
    total.skipped_items += part.skipped_items
    total.failed_items += part.failed_items
    total.failures += part.failures


@click.group(cls=PositionalFlagGroup)
@click.version_option(__version__, prog_name="protonfs")
@click.option("-v", "--verbose", count=True, help="Increase console detail (-v..-vvvv).")
@click.option(
    "--progress-inline/--progress-lines",
    "progress_inline",
    default=None,
    help="Update progress in place (inline) vs. print each poll on a new line. "
    "Default: config (defaults.progress_style), else inline on a TTY.",
)
@click.option(
    "--event-log/--no-event-log",
    "event_log",
    default=None,
    help="Write a full debug event log to .protonfs/events.log. "
    "Default: config (defaults.event_log), else off.",
)
def main(verbose: int, progress_inline: bool | None, event_log: bool | None) -> None:
    """Sync a local directory tree with Proton Drive."""
    from pathlib import Path

    from protonfs.config import load_layered_config
    from protonfs.logs import configure_logging

    # Resolve flag -> config -> built-in default for the two persisted knobs. A broken
    # repo config (unparseable .protonfs/config.json) must never take down every
    # command's group callback: `doctor`/`config set` are how you FIX a broken config,
    # and the command itself will surface config errors where they actually matter.
    try:
        cfg = load_layered_config(Path.cwd())
    except Exception:
        cfg = None
    cfg_style = cfg.defaults.progress_style if cfg else "inline"
    cfg_event = cfg.defaults.event_log if cfg else False
    if progress_inline is None:
        style = cfg_style
    else:
        style = "inline" if progress_inline else "lines"
    use_event_log = cfg_event if event_log is None else event_log
    configure_logging(verbose, progress_style=style, event_log=use_event_log, root=Path.cwd())


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
@click.argument("path", nargs=-1)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["plain", "json"]),
    default="plain",
    show_default=True,
    help="Output format: the classic state-per-line counts, or one JSON object.",
)
def status(path: tuple[str, ...], fmt: str) -> None:
    """Summarize sync state (counts by local-only/remote-only/synced/conflict).

    Accepts any number of PATHs (e.g. from a shell glob); counts are combined.
    Exit code, so an unattended caller can branch without parsing the counts:
    0 = clean (everything synced or intentionally remote-only), 1 = drift present
    (something to push/pull/prune), 2 = conflict present (needs a human or --resolve).
    Conflict outranks drift when both are present.
    """
    from collections import Counter

    from protonfs.commands.status import compute_status, status_exit_code
    from protonfs.context import load_context
    from protonfs.diff import SyncState

    ctx = load_context()
    counts: Counter = Counter()
    for subpath in _normalize_paths(path):
        counts.update(compute_status(ctx, subpath))
    code = status_exit_code(counts)
    if fmt == "json":
        import json

        click.echo(
            json.dumps(
                {
                    "counts": {state.value: counts.get(state.value, 0) for state in SyncState},
                    "exit_code": code,
                }
            )
        )
    else:
        for state in SyncState:
            click.echo(f"{state.value}: {counts.get(state.value, 0)}")
    if code != 0:
        raise click.exceptions.Exit(code)


# The frozen SyncState values (docs/stability.rst): spelled out literally so `--state`'s
# Choice needs no protonfs import at CLI-definition time (startup cost). A unit test
# asserts this stays equal to diff.SyncState's values.
_STATE_CHOICES = (
    "synced",
    "local-only",
    "remote-only",
    "metadata-only",
    "conflict",
    "local-modified",
    "remote-modified",
    "both-modified",
    "local-deleted",
    "remote-changed",
    "remote-deleted",
    "lfs-pointer",
)


@main.command()
@click.argument("path", nargs=-1)
@click.option("--remote", is_flag=True, help="Force a live Drive listing instead of the index.")
@click.option("--trash", is_flag=True, help="List /trash instead.")
@click.option(
    "--dirs",
    is_flag=True,
    help="Aggregate per immediate subdirectory: file counts by state plus cumulative "
    "local/indexed sizes, instead of listing every file.",
)
@click.option(
    "--state",
    "states",
    multiple=True,
    type=click.Choice(_STATE_CHOICES),
    help="Only show files in this sync state (repeatable).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "plain", "json"]),
    default="table",
    show_default=True,
    help="Output format: rich table, tab-separated lines, or JSON.",
)
@click.option(
    "--visual",
    type=click.Choice(["treemap", "waffle"]),
    default=None,
    help="Draw a per-directory storage-usage chart (by each dir's apparent footprint) "
    "instead of the listing. Implies directory aggregation; terminal-only (not for "
    "--format/--trash).",
)
@_drive_error_boundary
def ls(
    path: tuple[str, ...],
    remote: bool,
    trash: bool,
    dirs: bool,
    states: tuple[str, ...],
    fmt: str,
    visual: str | None,
) -> None:
    """List tracked files with their sync state (any number of PATHs).

    --dirs summarizes each immediate subdirectory (counts by state, cumulative
    local/indexed sizes) instead of listing thousands of files; --state filters to
    the states you care about; --format plain|json makes the output scriptable;
    --visual treemap|waffle draws a storage-usage chart of those directories.
    """
    from rich.console import Console

    from protonfs.commands.ls import render_ls
    from protonfs.context import load_context

    # A chart is an interactive terminal view -- it has no plain/json serialization and
    # no meaning over the (untracked, size-less) trash listing. Fail fast and clearly.
    if visual is not None:
        if fmt != "table":
            raise click.UsageError("--visual cannot be combined with --format plain/json.")
        if trash:
            raise click.UsageError("--visual has nothing to chart for --trash.")

    ctx = load_context()
    console = Console()
    subpaths = _normalize_paths(path)
    for subpath in subpaths:
        if len(subpaths) > 1 and fmt == "table":
            console.print(f"[bold]{subpath}:[/bold]")
        render_ls(
            ctx,
            subpath,
            remote,
            trash,
            console,
            dirs=dirs,
            states=states,
            fmt=fmt,
            visual=visual,
            echo=click.echo,
        )


@main.command()
@click.argument("path", nargs=-1)
@click.option("--resolve", type=click.Choice(["merge", "keep-both", "replace", "skip"]))
@click.option("--dry-run", is_flag=True)
@_drive_error_boundary
def push(path: tuple[str, ...], resolve: str | None, dry_run: bool) -> None:
    """Upload local-only/changed files to Drive (any number of PATHs)."""
    from protonfs.commands.push import push as push_files
    from protonfs.context import load_context
    from protonfs.drive import TransferResult
    from protonfs.locking import repo_lock

    ctx = load_context()
    result = TransferResult(0, 0, 0, [])
    with repo_lock(ctx.root):
        for subpath in _normalize_paths(path):
            _accumulate_transfer(result, push_files(ctx, subpath, resolve, dry_run))
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
@click.argument("path", nargs=-1)
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
def pull(path: tuple[str, ...], resolve: str | None, dry_run: bool, refresh: bool) -> None:
    """Download remote-only/changed files from Drive (any number of PATHs).

    Diverged files (edited locally AND changed on the remote since the last sync) are
    left untouched unless you pass --resolve; they are reported and pull exits non-zero.
    """
    from protonfs.commands.pull import pull as pull_files
    from protonfs.context import load_context
    from protonfs.drive import TransferResult
    from protonfs.locking import repo_lock

    ctx = load_context()
    if not refresh and not ctx.index.all():
        click.echo("index empty; run `protonfs refresh` first (or `pull --refresh`)")
        return
    result = TransferResult(0, 0, 0, [])
    with repo_lock(ctx.root):
        for subpath in _normalize_paths(path):
            _accumulate_transfer(
                result,
                pull_files(ctx, subpath, resolve, dry_run, refresh=refresh),
            )
    click.echo(
        f"transferred={result.transferred_items} skipped={result.skipped_items} "
        f"failed={result.failed_items}"
    )
    for failure in result.failures:
        click.echo(f"  FAILED {failure['name']}: {failure['error']}")
    if result.failed_items:
        raise click.exceptions.Exit(1)


@main.command()
@click.argument("path", nargs=-1)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip re-verifying files against the remote before deleting local bytes (unsafe).",
)
@click.option("--dry-run", is_flag=True, help="Preview what would be offloaded; delete nothing.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@_drive_error_boundary
def offload(path: tuple[str, ...], no_verify: bool, dry_run: bool, yes: bool) -> None:
    """Delete local bytes of protonfs-tracked files confirmed present on Drive.

    Accepts any number of PATHs (e.g. from a shell glob). The inverse of `pull`:
    reclaims local disk space while leaving the Drive copy intact. Reversible -- a
    later `pull` restores the file in full. By default every file is re-verified
    against a live remote listing (not just the index) before its local copy is
    deleted; pass --no-verify to skip that check.
    """
    from protonfs.commands.offload import OffloadResult
    from protonfs.commands.offload import offload as offload_files
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    verify = not no_verify
    subpaths = _normalize_paths(path)

    if not yes and not dry_run:
        shown = ", ".join(f"'{p}'" for p in subpaths if p) or "'.'"
        click.confirm(
            f"Delete local copies of tracked files under {shown} (Drive copies are kept)?",
            abort=True,
        )

    result = OffloadResult()
    with repo_lock(ctx.root):
        for subpath in subpaths:
            part = offload_files(ctx, subpath, verify=verify, dry_run=dry_run)
            result.offloaded += part.offloaded
            result.skipped_unverified += part.skipped_unverified
            result.skipped_modified += part.skipped_modified
            result.bytes_reclaimed += part.bytes_reclaimed
            result.offloaded_paths += part.offloaded_paths
            result.skipped_paths += part.skipped_paths
            result.modified_paths += part.modified_paths

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
@click.argument("path", nargs=-1, required=True)
@click.option("-r", "--recursive", is_flag=True)
@click.option("-f", "--force", is_flag=True, help="Permanently delete (trash, then delete).")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@_drive_error_boundary
def rm(path: tuple[str, ...], recursive: bool, force: bool, yes: bool) -> None:
    """Remove files/directories from Drive (trash by default, -f for permanent)."""
    from protonfs.commands.rm import rm as rm_path
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    with repo_lock(ctx.root):
        # No None-mapping here: unlike the scan-scoped commands, rm has no
        # "whole repo" default -- each given path is removed as-is.
        for subpath in dict.fromkeys(path):
            rm_path(ctx, subpath, recursive, force, confirmed=yes)


@main.command()
@click.argument("path", nargs=-1, required=True)
@_drive_error_boundary
def restore(path: tuple[str, ...]) -> None:
    """Restore trashed files/directories on Drive."""
    from protonfs.commands.restore import restore as restore_path
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    with repo_lock(ctx.root):
        for subpath in dict.fromkeys(path):  # dedupe only; no whole-repo default for restore
            restore_path(ctx, subpath)


@main.command()
@click.argument("path", nargs=-1)
@click.option("--prune", is_flag=True, help="Drop index entries for files deleted on the remote.")
@_drive_error_boundary
def refresh(path: tuple[str, ...], prune: bool) -> None:
    """Discover remote files and seed the local index (any number of PATHs)."""
    from protonfs.commands.refresh import RefreshResult
    from protonfs.commands.refresh import refresh as refresh_index
    from protonfs.context import load_context
    from protonfs.locking import repo_lock

    ctx = load_context()
    result = RefreshResult()
    with repo_lock(ctx.root):
        for subpath in _normalize_paths(path):
            part = refresh_index(ctx, subpath, prune)
            result.seeded += part.seeded
            result.remote_changed += part.remote_changed
            result.remote_deleted += part.remote_deleted
            result.pruned += part.pruned
            result.changed_paths += part.changed_paths
            result.deleted_paths += part.deleted_paths
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


@main.command("completions")
@click.argument("shell", type=click.Choice(("bash", "zsh", "fish")))
@click.option("--install", is_flag=True, help="Install the completion script (idempotent).")
@click.option("--uninstall", is_flag=True, help="Remove the installed completion script.")
def completions(shell: str, install: bool, uninstall: bool) -> None:
    """Print or install shell completion (bash|zsh|fish).

    Global flags typed *after* a subcommand are not offered (Click completes in canonical
    order); command names and per-subcommand options complete normally.
    """
    from protonfs.commands.completions import (
        completion_script,
        install_completion,
        uninstall_completion,
    )

    if install and uninstall:
        raise click.UsageError("--install and --uninstall are mutually exclusive.")
    if install:
        path = install_completion(shell)
        click.echo(f"Installed {shell} completion -> {path}")
        click.echo("Start a new shell (or source your rc) to activate it.")
    elif uninstall:
        removed = uninstall_completion(shell)
        click.echo("Removed completion." if removed else "No completion was installed.")
    else:
        click.echo(completion_script(shell))


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
@click.option("--global", "scope_global", is_flag=True, help="Write to the global user config.")
@click.option("--local", "scope_local", is_flag=True, help="Write to the per-device local config.")
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
