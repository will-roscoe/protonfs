# src/protonfs/commands/setup.py
"""``protonfs setup``: install/verify proton-drive, init ``.protonfs/``, migrate off git-LFS.

Composed of small ``ensure_*`` guards (binary, keyring, auth, config) run in a
deliberate order, plus the optional git-LFS → Proton Drive migration and its
control-file bookkeeping.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import click

from protonfs.commands.push import ensure_remote_root
from protonfs.commands.push import push as push_files
from protonfs.config import Config, init_config, load_layered_config, migrate_device_id_to_local
from protonfs.context import RepoContext
from protonfs.drive import DriveClient
from protonfs.ignore import init_ignore, init_include
from protonfs.index import IndexStore
from protonfs.lfs import find_pointer_stubs, is_lfs_tracked
from protonfs.secretservice import SecretServiceError, ensure_secret_service


def ensure_cli_present(drive: DriveClient) -> str:
    """Return the proton-drive CLI version, or fail with install guidance if absent.

    :param drive: the drive client to probe.
    :returns: the raw ``proton-drive version`` string.
    :raises click.ClickException: when the binary is not found/runnable.
    """
    version = drive.version()
    if version is None:
        raise click.ClickException(
            "proton-drive CLI not found. Run `protonfs install-drive`, or install it from "
            "https://proton.me/download/drive/cli/index.html and ensure it's on PATH, "
            "then re-run `protonfs setup`."
        )
    return version


def ensure_secrets(drive: DriveClient) -> None:
    """Guarantee a writable OS keyring before anything asks the user to log in.

    Ordered ahead of the auth check on purpose. Without it, a headless host reports
    "not authenticated", the user logs in, the browser flow succeeds, and the CLI
    then fails to persist the session -- leaving them back at "not authenticated"
    with no indication that the keyring, not their credentials, is the problem.
    """
    try:
        result = ensure_secret_service()
    except SecretServiceError as exc:
        raise click.ClickException(
            f"No usable OS keyring for proton-drive to store its session in:\n  {exc}\n"
            "Run `protonfs doctor` for a full diagnosis."
        ) from exc
    for action in result.actions:
        click.echo(f"  keyring: {action}")
    for warning in result.warnings:
        click.echo(f"  ! {warning}")


def ensure_authenticated(drive: DriveClient) -> None:
    """Fail unless proton-drive has an active session.

    :param drive: the drive client to check.
    :raises click.ClickException: when not authenticated (points at ``auth login``).
    """
    if not drive.is_authenticated():
        raise click.ClickException(
            "Not authenticated with Proton Drive. Run `protonfs auth login`, "
            "then re-run `protonfs setup`."
        )


def ensure_config(root: Path) -> Config:
    """Load the repo's config, or interactively create it, returning the result.

    An existing repo is migrated in passing (any ``device_id`` in the shared config
    is relocated to ``config.local.json``); a fresh repo prompts for its Drive
    ``remote_root``.

    :param root: the protonfs root.
    :returns: the loaded or newly-initialised :class:`~protonfs.config.Config`.
    """
    existing = load_layered_config(root)
    if existing is not None:
        # #21: repos set up before per-device layering existed may still have device_id
        # in the shared config.json -- move it to config.local.json now that we're here
        # anyway. Straightforward and idempotent; doesn't change the resolved config.
        migrate_device_id_to_local(root)
        return existing
    remote_root = click.prompt("Remote Drive root path for this repo (e.g. /my-files/myproject)")
    return init_config(root, remote_root)


def is_git_toplevel(root: Path) -> bool:
    """True when `root` is the top level of a git repo (not a subdirectory of one, and not
    outside git entirely). Used to decide whether an LFS migration -- which rewrites
    .gitattributes and commits across the WHOLE repo -- is appropriate to run here (#19)."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False  # not a git repo at all
    return Path(result.stdout.strip()).resolve() == root.resolve()


# .protonfs/.gitattributes content: keep protonfs's OWN control files as normal git objects,
# never git-LFS pointers, so a clone without an LFS pull still receives the real sync contract
# (config.json + ignore + include) rather than 130-byte pointer stubs (#20).
_PROTONFS_GITATTRIBUTES = (
    "# Managed by `protonfs setup` (#20). Keep protonfs's control files as normal git\n"
    "# objects even if the enclosing repo routes this path through git-LFS, so a clone\n"
    "# without an LFS pull still gets the real sync contract, not pointer stubs.\n"
    "* !filter !diff !merge text\n"
)
# .protonfs/.gitignore content: the sync contract (config.json + ignore + include) is
# committed and shared; per-device/transient state (index.json, the resumable-refresh
# cursor, config.local.json) is local-only. `include` (#18) is deliberately NOT listed
# here -- it belongs with config.json and ignore in the tracked/shared set.
_PROTONFS_GITIGNORE = (
    "# Managed by `protonfs setup` (#20, #21). Local-only, per-device state -- never commit\n"
    "# these; config.json, ignore, and include ARE committed (the shared sync contract).\n"
    "index.json\n"
    "refresh-state.json\n"
    "config.local.json\n"
)


def _ensure_lines(path: Path, content: str) -> bool:
    """Write `content` to `path` if absent; if present, append any of its lines that are
    missing. Idempotent and non-destructive to a user's own edits. Returns True if it wrote."""
    if not path.exists():
        path.write_text(content)
        return True
    existing = path.read_text()
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing = [ln for ln in content.splitlines() if ln.strip() and ln.strip() not in existing_lines]
    if not missing:
        return False
    with path.open("a") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write("\n".join(missing) + "\n")
    return True


def write_git_control_files(root: Path) -> None:
    """Write `.protonfs/.gitattributes` (exempt control files from LFS) and
    `.protonfs/.gitignore` (ignore local-only state) so the committed-vs-local split is
    correct by default (#20). Idempotent; safe to run when `root` is not a git repo."""
    protonfs_dir = root / ".protonfs"
    protonfs_dir.mkdir(parents=True, exist_ok=True)
    wrote_attrs = _ensure_lines(protonfs_dir / ".gitattributes", _PROTONFS_GITATTRIBUTES)
    wrote_ignore = _ensure_lines(protonfs_dir / ".gitignore", _PROTONFS_GITIGNORE)
    if wrote_attrs or wrote_ignore:
        click.echo(
            "Wrote .protonfs/.gitattributes + .protonfs/.gitignore "
            "(control files exempt from git-LFS; index.json kept local)."
        )


def _untrack_lfs_patterns(root: Path) -> list[str]:
    """Strip ``filter=lfs`` rules from the repo's root ``.gitattributes``, returning them.

    :param root: the git repo root.
    :returns: the pathspec patterns whose LFS rules were removed (empty if none / no file).
    """
    gitattributes = root / ".gitattributes"
    if not gitattributes.exists():
        return []
    removed_patterns: list[str] = []
    kept_lines: list[str] = []
    for line in gitattributes.read_text().splitlines():
        if "filter=lfs" in line:
            removed_patterns.append(line.split()[0])
        else:
            kept_lines.append(line)
    gitattributes.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""))
    return removed_patterns


def _append_gitignore(root: Path, patterns: list[str]) -> None:
    """Append ``patterns`` to the repo's root ``.gitignore``, skipping any already present.

    :param root: the git repo root.
    :param patterns: pathspecs to gitignore (typically the just-untracked LFS patterns).
    """
    gitignore = root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    existing_lines = {line.strip() for line in existing.splitlines()}
    lines_to_add = [p for p in patterns if p.strip() not in existing_lines]
    if not lines_to_add:
        return
    with gitignore.open("a") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write("\n".join(lines_to_add) + "\n")


def migrate_lfs(ctx: RepoContext, dry_run: bool = False) -> bool:
    """Migrate a repo's git-LFS-tracked files to Proton Drive, then untrack them in git.

    Materialises LFS content (``git lfs pull``), uploads it to Drive via ``push``, and
    only then — after confirmation — removes the ``filter=lfs`` rules, gitignores the
    paths, ``git rm --cached`` them, and makes a single migration commit. Uploads
    happen before any git tracking is touched, so a failed upload aborts with the repo
    untouched.

    :param ctx: the loaded repo context.
    :param dry_run: when true, print the plan and change nothing.
    :returns: ``True`` if a migration ran (or would, under dry-run); ``False`` when the
        repo has no LFS tracking.
    :raises click.ClickException: on a failed upload or a failed git step (the message
        states whether uploads had already succeeded).

    .. warning:: This rewrites ``.gitattributes`` and commits across the WHOLE
        enclosing repo; :func:`is_git_toplevel` gates it so a subdirectory setup never
        migrates the parent repo off LFS.
    """
    root = ctx.root
    if not is_lfs_tracked(root):
        click.echo("No git-lfs tracking found -- skipping migration.")
        return False

    click.echo("git-lfs tracking detected.")
    if dry_run:
        click.echo("[dry-run] Would run: git lfs pull")
        click.echo("[dry-run] Would upload LFS-tracked files to Drive via `protonfs push`.")
        click.echo(
            "[dry-run] Would remove LFS filter rules from .gitattributes, add to .gitignore,"
        )
        click.echo("[dry-run] git rm --cached the affected paths, and commit.")
        return True

    click.echo("Materializing real file content (git lfs pull)...")
    subprocess.run(["git", "-C", str(root), "lfs", "pull"], check=True)

    click.echo("Uploading LFS-tracked files to Drive before touching git tracking...")
    result = push_files(ctx, subpath=None, resolve=None, dry_run=False)
    if result.failed_items:
        raise click.ClickException(
            f"{result.failed_items} file(s) failed to upload -- aborting migration before "
            "touching git tracking. Re-run `protonfs setup` once uploads succeed."
        )

    click.confirm(
        f"Uploaded {result.transferred_items} file(s) to Drive. This will now remove LFS "
        "filter rules from .gitattributes, gitignore the same paths, `git rm --cached` them, "
        "and create one commit. Continue?",
        abort=True,
    )
    patterns = _untrack_lfs_patterns(root)
    _append_gitignore(root, patterns)
    try:
        subprocess.run(
            ["git", "-C", str(root), "add", ".gitattributes", ".gitignore"], check=True
        )
        for pattern in patterns:
            subprocess.run(
                ["git", "-C", str(root), "rm", "-r", "--cached", "--ignore-unmatch", pattern],
                check=True,
            )
        subprocess.run(
            [
                "git", "-C", str(root), "commit", "-m",
                "chore: migrate dump storage from git-lfs to protonfs/Proton Drive",
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            f"git step of the LFS migration failed ({exc}). The files were already "
            "uploaded to Drive successfully, but your working tree may now be "
            "mid-migration -- inspect `git status` and revert/complete the git state "
            "manually before re-running `protonfs setup`."
        ) from exc
    click.echo("Migration commit created. Review it before pushing.")
    return True


def clean_pointer_stubs(root: Path) -> int:
    """Delete leftover git-LFS pointer-stub files under ``root``, returning the count.

    :param root: the repo root to sweep.
    :returns: the number of stub files removed.

    .. warning:: Only call as part of an ACTUAL migration; outside one, pointer stubs
        are legitimately LFS-managed files and deleting them is data loss (#19).
    """
    stubs = find_pointer_stubs(root, Path("."))
    for stub in stubs:
        stub.unlink()
    return len(stubs)


def maybe_uninstall_lfs_filters(root: Path) -> None:
    """Run ``git lfs uninstall`` when no ``filter=lfs`` rules remain in ``.gitattributes``.

    No-op when LFS rules still exist or git-lfs is not installed.

    :param root: the git repo root.
    """
    gitattributes = root / ".gitattributes"
    still_has_lfs = gitattributes.exists() and "filter=lfs" in gitattributes.read_text()
    if still_has_lfs:
        return
    result = subprocess.run(["git", "-C", str(root), "lfs", "env"], capture_output=True, text=True)
    if result.returncode != 0:
        return  # git-lfs not installed at all -- nothing to uninstall
    click.echo("No LFS patterns remain in .gitattributes; running `git lfs uninstall`.")
    subprocess.run(["git", "-C", str(root), "lfs", "uninstall"], check=True)


def run_setup(root: Path, dry_run: bool = False, migrate: bool | None = None) -> None:
    """Run the full ``protonfs setup`` flow for ``root``.

    In order: verify the proton-drive binary, bootstrap the keyring, check auth,
    create/load config, write the ``.protonfs/`` control files, ensure the remote root
    exists on Drive, and — when appropriate — migrate the repo off git-LFS.

    :param root: the directory to set up as a protonfs root.
    :param dry_run: when true, preview without persisting or touching Drive/git.
    :param migrate: force (``True``) or skip (``False``) the git-LFS migration;
        ``None`` migrates only when ``root`` is the git toplevel.
    :raises click.ClickException: on any failed precondition (binary, keyring, auth,
        Drive error, failed LFS upload, declined confirmation).

    .. seealso:: :func:`migrate_lfs` for the LFS migration this may invoke.
    """
    drive = DriveClient()
    version = ensure_cli_present(drive)
    click.echo(f"proton-drive CLI found: {version}")

    ensure_secrets(drive)
    ensure_authenticated(drive)
    click.echo("Authenticated with Proton Drive.")

    config = ensure_config(root)
    init_ignore(root)
    init_include(root)
    write_git_control_files(root)
    config_file = root / ".protonfs" / "config.json"
    click.echo(f"Config ready at {config_file} (remote_root={config.remote_root}).")

    ctx = RepoContext(root=root, config=config, index=IndexStore(root), drive=drive)
    # #17: create the whole remote_root path now (fail fast with a precise error if it is not
    # under a valid Drive area), so the first push does not fail because the folder is absent.
    if not dry_run:
        ensure_remote_root(ctx)
        click.echo(f"Ensured remote root exists on Drive: {config.remote_root}")

    # #19: the LFS migration rewrites .gitattributes and commits across the WHOLE enclosing
    # repo, so only run it when this root IS the git toplevel -- unless the user explicitly
    # opts in/out via --migrate-lfs/--no-migrate-lfs. Initialising a sync directory that is a
    # subdirectory of a larger repo must not migrate that repo off LFS.
    should_migrate = migrate if migrate is not None else is_git_toplevel(root)
    if not should_migrate:
        if migrate is False:
            click.echo("Skipping git-LFS migration (--no-migrate-lfs).")
        else:
            click.echo(
                "protonfs root is not the git toplevel (or not a git repo); skipping the "
                "repo-wide git-LFS migration. Pass --migrate-lfs to force it here."
            )
        migrated = False
    else:
        migrated = migrate_lfs(ctx, dry_run=dry_run)

    # Only reap leftover pointer stubs as part of an ACTUAL migration. When migration was
    # skipped (a subdirectory root, or --no-migrate-lfs), any pointer stubs present are
    # legitimately git-LFS-managed files the user is keeping -- deleting them would be data
    # loss (#19).
    if migrated and not dry_run:
        removed = clean_pointer_stubs(root)
        if removed:
            click.echo(f"Removed {removed} leftover git-lfs pointer stub file(s).")
        else:
            click.echo("No leftover git-lfs pointer stubs found.")
        maybe_uninstall_lfs_filters(root)

    click.echo("protonfs setup complete.")
