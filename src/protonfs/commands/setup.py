# src/protonfs/commands/setup.py
from __future__ import annotations

import subprocess
from pathlib import Path

import click

from protonfs.commands.push import push as push_files
from protonfs.config import Config, init_config, load_config
from protonfs.context import RepoContext
from protonfs.drive import DriveClient
from protonfs.ignore import init_ignore
from protonfs.index import IndexStore
from protonfs.lfs import find_pointer_stubs, is_lfs_tracked


def ensure_cli_present(drive: DriveClient) -> str:
    version = drive.version()
    if version is None:
        raise click.ClickException(
            "proton-drive CLI not found. Install it from "
            "https://proton.me/download/drive/cli/index.html and ensure it's on PATH, "
            "then re-run `protonfs setup`."
        )
    return version


def ensure_authenticated(drive: DriveClient) -> None:
    if not drive.is_authenticated():
        raise click.ClickException(
            "Not authenticated with Proton Drive. Run `proton-drive auth login`, "
            "then re-run `protonfs setup`."
        )


def ensure_config(root: Path) -> Config:
    existing = load_config(root)
    if existing is not None:
        return existing
    remote_root = click.prompt("Remote Drive root path for this repo (e.g. /my-files/myproject)")
    return init_config(root, remote_root)


def _untrack_lfs_patterns(root: Path) -> list[str]:
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
    gitignore = root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    lines_to_add = [p for p in patterns if p not in existing]
    if not lines_to_add:
        return
    with gitignore.open("a") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write("\n".join(lines_to_add) + "\n")


def migrate_lfs(ctx: RepoContext, dry_run: bool = False) -> bool:
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
    subprocess.run(["git", "-C", str(root), "add", ".gitattributes", ".gitignore"], check=True)
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
    click.echo("Migration commit created. Review it before pushing.")
    return True


def clean_pointer_stubs(root: Path) -> int:
    stubs = find_pointer_stubs(root, Path("."))
    for stub in stubs:
        stub.unlink()
    return len(stubs)


def maybe_uninstall_lfs_filters(root: Path) -> None:
    gitattributes = root / ".gitattributes"
    still_has_lfs = gitattributes.exists() and "filter=lfs" in gitattributes.read_text()
    if still_has_lfs:
        return
    result = subprocess.run(["git", "-C", str(root), "lfs", "env"], capture_output=True, text=True)
    if result.returncode != 0:
        return  # git-lfs not installed at all -- nothing to uninstall
    click.echo("No LFS patterns remain in .gitattributes; running `git lfs uninstall`.")
    subprocess.run(["git", "-C", str(root), "lfs", "uninstall"], check=True)


def run_setup(root: Path, dry_run: bool = False) -> None:
    drive = DriveClient()
    version = ensure_cli_present(drive)
    click.echo(f"proton-drive CLI found: {version}")

    ensure_authenticated(drive)
    click.echo("Authenticated with Proton Drive.")

    config = ensure_config(root)
    init_ignore(root)
    config_file = root / ".protonfs" / "config.json"
    click.echo(f"Config ready at {config_file} (remote_root={config.remote_root}).")

    ctx = RepoContext(root=root, config=config, index=IndexStore(root), drive=drive)
    migrated = migrate_lfs(ctx, dry_run=dry_run)

    removed = clean_pointer_stubs(root)
    if removed:
        click.echo(f"Removed {removed} leftover git-lfs pointer stub file(s).")
    else:
        click.echo("No leftover git-lfs pointer stubs found.")

    if migrated and not dry_run:
        maybe_uninstall_lfs_filters(root)

    click.echo("protonfs setup complete.")
