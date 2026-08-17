"""The ``protonfs`` command-line entry point: the Click command tree.

Each command is a thin wrapper that lazily imports and calls its
:mod:`protonfs.commands` implementation, so ``--help`` and shell completion stay
fast (no Drive/keyring imports until a command actually runs). The frozen 1.0
surface these commands expose is documented in ``docs/stability.rst``.

.. versionadded:: 1.0.0
"""

from __future__ import annotations

import contextlib
import fnmatch
import functools
import signal
from pathlib import Path
from typing import Callable

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
    """A leaf command whose ``--help`` documents the global options.

    Its ``shell_complete`` also offers the position-independent global flags (those
    :data:`~protonfs.argv.GLOBAL_FLAG_NAMES` that :func:`~protonfs.argv.reorder_argv`
    hoists) when completing an option after the subcommand, so completion matches what
    the parser actually accepts.
    """

    def shell_complete(self, ctx, incomplete):
        """Complete this command's own options plus the global flags accepted anywhere."""
        results = super().shell_complete(ctx, incomplete)
        if incomplete[:1] == "-":
            from click.shell_completion import CompletionItem

            from protonfs.argv import GLOBAL_FLAG_NAMES

            seen = {item.value for item in results}
            root = ctx.find_root().command
            for param in root.get_params(ctx):
                if not isinstance(param, click.Option):  # pragma: no cover - group has no args
                    continue
                for opt in (*param.opts, *param.secondary_opts):
                    if opt in GLOBAL_FLAG_NAMES and opt.startswith(incomplete) and opt not in seen:
                        results.append(CompletionItem(opt, help=param.help or ""))
                        seen.add(opt)
        return results


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


def _install_signal_handlers() -> None:
    """Make ``SIGTERM``/``SIGHUP`` raise, so an in-flight ``proton-drive`` child is never
    orphaned (#136).

    :func:`subprocess.run` already kills its child on *any* exception -- its bare
    ``except:`` calls ``process.kill()`` -- so ``SIGINT`` (Ctrl-C, which Python turns into
    ``KeyboardInterrupt``) is handled correctly and needs nothing here. ``SIGTERM`` and
    ``SIGHUP`` are the gap: their default action terminates the interpreter outright, so
    no Python-level cleanup runs at all and the ``proton-drive`` child survives its parent.

    That orphan keeps an exclusive lock on proton-drive's SQLite cache, after which *every*
    later proton-drive call on the host fails (see :class:`~protonfs.drive.DriveLockError`)
    until someone finds and kills it by hand. ``SIGHUP`` matters most on a headless box,
    where a dropped ssh session would otherwise leave a wedged remote.

    Raising :class:`SystemExit` puts both signals on the path ``subprocess.run`` already
    handles. Exit status follows the ``128 + signum`` convention.

    Installed only from the CLI entry point: a library import must not commandeer the
    host application's signal handling.

    .. versionadded:: 1.11.0
    """

    def _raise_system_exit(signum, _frame):
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:  # pragma: no cover - non-POSIX
            continue
        try:
            signal.signal(sig, _raise_system_exit)
        except (ValueError, OSError):  # pragma: no cover - not on the main thread
            pass


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


# #131: PATH arguments that are glob patterns (mload*, mload*/*.ev), expanded by protonfs
# itself rather than relying on the shell -- so a `schedule` job's quoted pattern keeps
# matching as new directories appear, which a shell glob (expanded once, at install time)
# cannot do.
_GLOB_CHARS = frozenset("*?[")


def _is_pattern(pathspec: str) -> bool:
    """True if `pathspec` contains a glob metacharacter, so it should be expanded rather
    than treated as a literal path."""
    return any(c in pathspec for c in _GLOB_CHARS)


def _expand_patterns(
    paths: tuple[str, ...], matcher: Callable[[str], list[str]]
) -> tuple[tuple[str, ...], list[str]]:
    """Expand every glob-looking entry in `paths` via `matcher`, leaving literal paths
    untouched; the result feeds straight into :func:`_normalize_paths`.

    :param matcher: resolves one pattern to the literal paths it matches right now
        (:func:`_glob_local` for push's local-filesystem view, :func:`_expand_pattern_index`
        for pull's index view).
    :returns: ``(expanded_paths, unmatched_patterns)`` -- the second is every pattern that
        matched nothing, for the caller to apply ``--strict`` policy against. Deliberately
        NOT folded into ``expanded_paths``: an empty ``expanded_paths`` must still be
        distinguished from "no paths given at all" (which means the whole repo) by the
        caller, so a pattern matching nothing never silently widens to everything.
    """
    expanded: list[str] = []
    unmatched: list[str] = []
    for raw in paths:
        if _is_pattern(raw):
            matches = matcher(raw)
            if not matches:
                unmatched.append(raw)
                continue
            expanded.extend(matches)
        else:
            expanded.append(raw)
    return tuple(expanded), unmatched


def _glob_local(root: Path, pattern: str) -> list[str]:
    """Expand `pattern` against the live local tree under `root` (push's matcher).

    Uses :meth:`Path.glob`, so semantics match a shell glob exactly: a pattern segment
    never crosses ``/`` (``mload*`` matches top-level entries only; ``**`` would recurse,
    but is not needed by any case this issue asks for). Returns sorted repo-relative
    POSIX paths.
    """
    return sorted(m.relative_to(root).as_posix() for m in root.glob(pattern))


def _expand_pattern_index(index_keys: list[str] | set[str], pattern: str) -> list[str]:
    """Expand `pattern` against known index paths (pull's matcher).

    Matches component-by-component (a pattern segment never crosses ``/``, mirroring
    :func:`_glob_local`), but -- unlike a filesystem glob -- also finds a file with no
    local presence at all: an offloaded (metadata-only) entry is in the index but absent
    from disk, and pull's whole purpose is bringing exactly that back (#131).

    A pattern shorter than the full path (e.g. the single segment ``mload*`` against
    ``mload003_calib_1m/dump_0001``) matches by its DEDUPED PREFIX at the pattern's own
    depth -- one result per matched top-level family, not one per file -- so it feeds into
    the existing subtree-pull logic (:func:`~protonfs.diff.within_subpath`) exactly the way
    a shell-expanded literal directory name already does today.
    """
    pattern_parts = pattern.split("/")
    depth = len(pattern_parts)
    matched: set[str] = set()
    for rel in index_keys:
        parts = rel.split("/")
        if len(parts) < depth:
            continue
        prefix = parts[:depth]
        if all(fnmatch.fnmatchcase(c, p) for c, p in zip(prefix, pattern_parts)):
            matched.add("/".join(prefix))
    return sorted(matched)


def _resolve_pathspecs(
    paths: tuple[str, ...], matcher: Callable[[str], list[str]]
) -> tuple[list[str | None], list[str]]:
    """Expand any glob patterns in `paths`, then normalize into the subpath list to loop over.

    :returns: ``(subpaths, unmatched_patterns)``.

    When every given pathspec was a pattern that matched nothing, `subpaths` is **empty**
    (``[]``) -- deliberately never ``[None]``. ``[None]`` is :func:`_normalize_paths`'s
    "no pathspecs given, so act on the whole repo" default, and letting a zero-match
    pattern reach it would turn ``push 'nomatch*'`` into a push of the ENTIRE repo. A
    pattern that matches nothing must leave the command with nothing to do (#131).
    """
    expanded, unmatched = _expand_patterns(paths, matcher)
    if paths and not expanded:
        return [], unmatched
    return _normalize_paths(expanded), unmatched


def _report_unmatched(unmatched: list[str], strict: bool, verb: str) -> None:
    """Report pathspec patterns that matched nothing; under ``--strict``, make them fatal.

    Fails BEFORE any lock is taken or byte transferred, mirroring push's existing
    nonexistent-literal-path check: ``--strict`` asks for all-or-nothing, so transferring
    part of the request while an expected pattern went unsatisfied is the wrong outcome.

    Exits ``1`` (an operational failure), NOT the ``2`` used for a nonexistent literal
    PATH. That distinction is deliberate: a literal path that does not exist can only be a
    typo, whereas here the command line is perfectly well-formed and the user has simply
    asked -- via ``--strict`` -- to treat a legitimate runtime state as an error.
    """
    if not unmatched:
        return
    listed = ", ".join(map(repr, unmatched))
    if strict:
        click.echo(f"error: pattern(s) matched nothing: {listed}", err=True)
        raise click.exceptions.Exit(1)
    click.echo(
        f"pattern(s) matched nothing, skipped (nothing to {verb}): {listed} "
        "-- pass --strict to make this an error"
    )


@contextlib.contextmanager
def _resumable_on_interrupt(ctx, verb: str):
    """Persist index progress and exit cleanly (130) when the user interrupts a transfer.

    Used *inside* ``repo_lock`` so the flush happens while the lock is still held. Turns
    Ctrl+C during a long push/pull from Click's bare ``Aborted!`` (progress that never
    saved, no context) into a saved, resumable stop: files already uploaded-and-verified
    are recorded, so a re-run resumes rather than restarting.

    .. versionadded:: 1.8.0
    """
    try:
        yield
    except KeyboardInterrupt:
        ctx.index.save()
        click.echo(
            f"\ninterrupted -- progress saved; re-run to resume the {verb}.", err=True
        )
        raise click.exceptions.Exit(130) from None


def _accumulate_transfer(total, part) -> None:
    """Fold one per-path TransferResult into the running total (multi-path loops, #92)."""
    total.transferred_items += part.transferred_items
    total.adopted_items += part.adopted_items
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

    # #136: before anything can spawn proton-drive, make a kill signal unwind cleanly
    # rather than leave an orphaned child holding the Drive cache lock.
    _install_signal_handlers()

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
@click.option(
    "--remote",
    is_flag=True,
    help="Force a live Drive listing to compute state instead of relying on the local "
    "index alone (slower, but catches remote changes that refresh hasn't seen yet). "
    "Without it, 'synced' means the file matches the index, not that it was verified "
    "on Drive.",
)
def status(path: tuple[str, ...], fmt: str, remote: bool) -> None:
    """Summarize sync state (counts by local-only/remote-only/synced/conflict).

    Without ``--remote`` the comparison is against the local index only, so ``synced``
    means "matches what protonfs last recorded" rather than "verified on Drive".

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
        counts.update(compute_status(ctx, subpath, remote=remote))
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
@click.option(
    "--remote",
    is_flag=True,
    help="Force a live Drive listing to compute state instead of relying on the local "
    "index alone (slower, but catches remote changes that refresh hasn't seen yet).",
)
@click.option(
    "--trash",
    is_flag=True,
    help="List /trash instead of the working tree (name and type only; no sync-state "
    "column, since trashed items aren't tracked in the index).",
)
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
@click.option(
    "--resolve",
    type=click.Choice(["remote", "local", "both", "merge", "keep-both", "replace", "skip"]),
    help="How to reconcile a file that changed on BOTH sides since the last sync: "
    "remote=keep the remote copy (skip the upload), local=overwrite the remote with "
    "your local copy, both=upload your local copy alongside the remote. The proton-drive "
    "strategy names merge|keep-both|replace|skip are also accepted (replace=local, "
    "skip=remote, keep-both=both).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be pushed without transferring anything.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Fail (exit 1) if a PATH pattern matches nothing, instead of skipping it.",
)
@_drive_error_boundary
def push(path: tuple[str, ...], resolve: str | None, dry_run: bool, strict: bool) -> None:
    """Upload local-only/changed files to Drive (any number of PATHs).

    Each PATH may be a directory, a single file, or a glob pattern. A quoted pattern
    (``'mload*'``, ``'mload*/*.ev'``) is expanded by protonfs itself against the local
    tree at run time; an unquoted one is expanded by the shell first, as before. A PATH
    that does not exist locally is a usage error (exit 2): push uploads local files, so a
    missing path can only be a typo -- this is distinct from the exit 1 used for transfer
    failures.

    A pattern matching nothing is reported and skipped (it never widens to the whole
    repo); pass ``--strict`` to make that an error instead.

    .. versionchanged:: 1.5.2
       PATH may now name a single file (previously only directories were scanned, so a
       file/glob pathspec silently uploaded nothing). A nonexistent PATH is now a usage
       error instead of a silent no-op, and an empty push prints ``nothing to push``.

    .. versionchanged:: 1.11.0
       A quoted PATH glob pattern is now expanded by protonfs against the local tree,
       rather than depending on the shell to expand it first (#131) -- so a scheduled job
       can carry a pattern that keeps matching as new directories appear. Added
       ``--strict``.
    """
    from protonfs.commands.push import push as push_files
    from protonfs.context import load_context
    from protonfs.drive import TransferResult
    from protonfs.locking import repo_lock

    ctx = load_context()
    # #131: expand protonfs-side glob patterns against the local tree first -- push only
    # ever acts on files physically present, so the filesystem IS the right namespace here
    # (pull differs; see its matcher). Literal paths pass through untouched.
    subpaths, unmatched = _resolve_pathspecs(path, lambda p: _glob_local(ctx.root, p))
    _report_unmatched(unmatched, strict, "push")
    # Validate BEFORE taking the lock or hashing anything: a nonexistent local pathspec
    # can only be a typo (a shell glob expands to existing paths only), so fail fast and
    # loud rather than scan it to nothing and report a misleading success. Report every
    # bad path at once so the user fixes them in a single round trip.
    missing = [s for s in subpaths if s is not None and not (ctx.root / s).exists()]
    if missing:
        listed = ", ".join(map(repr, missing))
        raise click.UsageError(
            f"no such local path(s): {listed} "
            "(push uploads local files -- check the path or your shell glob)"
        )
    result = TransferResult(0, 0, 0, [])
    with repo_lock(ctx.root), _resumable_on_interrupt(ctx, "push"):
        for subpath in subpaths:
            _accumulate_transfer(result, push_files(ctx, subpath, resolve, dry_run))
    if result.transferred_items + result.skipped_items + result.failed_items == 0:
        # An existing path that yields no candidates (nothing changed, or everything under
        # it is excluded by the ignore rules) would otherwise print only the bare zero
        # summary. Say so in plain language -- and at DEFAULT verbosity, which the Reporter
        # cannot do (reporter.done() renders only at -v and above).
        click.echo("nothing to push")
    adopted = f" adopted={result.adopted_items}" if result.adopted_items else ""
    click.echo(
        f"transferred={result.transferred_items}{adopted} "
        f"skipped={result.skipped_items} failed={result.failed_items}"
    )
    for failure in result.failures:
        click.echo(f"  FAILED {failure['name']}: {failure['error']}")
    if result.failed_items:
        under_delivered = [f for f in result.failures if f.get("kind") == "under-delivered"]
        phantoms = [f for f in result.failures if f.get("kind") == "phantom"]
        conflicts = [
            f for f in result.failures if f.get("kind") not in ("under-delivered", "phantom")
        ]
        if under_delivered:
            click.echo(
                f"  -> {len(under_delivered)} file(s) were reported transferred but did not "
                "land on Drive; they were NOT indexed and will be retried on the next push."
            )
        if phantoms:
            # #138: deliberately NOT offered --resolve=remote -- for a phantom the remote
            # copy is the unusable thing holding the name, so keeping it loses the data.
            click.echo(
                f"  -> {len(phantoms)} file(s) hit a name conflict but are absent from the "
                "remote listing (a partial/draft upload holding the name). Re-run with "
                "--resolve=local to overwrite them."
            )
        if conflicts and not resolve:
            click.echo(
                "  -> these are remote conflicts; re-run with "
                "--resolve=remote|local|both to resolve them."
            )
        raise click.exceptions.Exit(1)


@main.command()
@click.argument("path", nargs=-1)
@click.option(
    "--resolve",
    type=click.Choice(["remote", "local", "both", "replace"]),
    help=(
        "How to reconcile a file that changed on BOTH sides since the last sync: "
        "remote=overwrite local, local=keep local (stays queued for push), "
        "both=fetch the remote copy under a .remote suffix for a manual merge. "
        "replace is an alias for remote (replace the local copy). "
        "Without this, pull leaves diverged files untouched and reports them."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview transfers and unresolved conflicts without downloading anything.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Discover remote files (seed the index) before pulling.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Fail (exit 1) if a PATH pattern matches nothing, instead of skipping it.",
)
@_drive_error_boundary
def pull(
    path: tuple[str, ...], resolve: str | None, dry_run: bool, refresh: bool, strict: bool
) -> None:
    """Download remote-only/changed files from Drive (any number of PATHs).

    Each PATH may be a directory, a single file, or a glob pattern. A quoted pattern
    (``'mload*'``, ``'mload*/*.ev'``) is expanded by protonfs itself, against the paths
    the INDEX knows about -- not the local filesystem, because pull's whole purpose is
    fetching files that are absent locally (an offloaded file has no local presence to
    glob). Run ``refresh`` first on a repo whose index has not seen those files yet.

    A pattern matching nothing is reported and skipped (it never widens to the whole
    repo); pass ``--strict`` to make that an error instead.

    Diverged files (edited locally AND changed on the remote since the last sync) are
    left untouched unless you pass --resolve; they are reported and pull exits non-zero.

    .. versionchanged:: 1.11.0
       A quoted PATH glob pattern is now expanded by protonfs against the index, rather
       than depending on the shell to expand it first (#131) -- so a scheduled job can
       carry a pattern that keeps matching as new runs appear, and so a pattern can name
       offloaded files that no filesystem glob could find. Added ``--strict``.
    """
    from protonfs.commands.pull import pull as pull_files
    from protonfs.context import load_context
    from protonfs.drive import TransferResult
    from protonfs.locking import repo_lock

    ctx = load_context()
    if not refresh and not ctx.index.all():
        click.echo("index empty; run `protonfs refresh` first (or `pull --refresh`)")
        return
    # #131: match patterns against the INDEX namespace (see the docstring) -- a filesystem
    # glob would silently miss exactly the offloaded files pull exists to bring back.
    subpaths, unmatched = _resolve_pathspecs(
        path, lambda p: _expand_pattern_index(list(ctx.index.all()), p)
    )
    _report_unmatched(unmatched, strict, "pull")
    result = TransferResult(0, 0, 0, [])
    with repo_lock(ctx.root), _resumable_on_interrupt(ctx, "pull"):
        for subpath in subpaths:
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
            "    -> `protonfs pull <path>` to take the remote version, "
            "or `protonfs push --resolve=local <path>` to keep (and re-upload) your local copy."
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

    Command names, per-subcommand options, and the global flags (in any position) all
    complete.
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


@main.command("schedule")
@click.option("--list", "list_", is_flag=True, help="List this machine's scheduled jobs.")
@click.option("--add", "add", is_flag=True, help="Install a new job from the options below.")
@click.option(
    "--uninstall", "-U", "uninstall", metavar="ID",
    help="Remove the job with this id (or --list index). Use --all to remove every job.",
)
@click.option("--all", "all_", is_flag=True, help="Remove every scheduled job.")
@click.option("--every", metavar="SPEC", help="Cadence: hourly | daily | weekly | <N>h | <N>m.")
@click.option("--cron", "cron_expr", metavar="EXPR", help="Raw 5-field cron expression.")
@click.option("--at", metavar="HOURS", help="Run daily at these hours (0-23, comma-separated).")
@click.option(
    "--command", "command", type=click.Choice(["push", "pull", "sync"]), default="push",
    show_default=True, help="What the job runs (sync = pull then push).",
)
@click.option(
    # NB: no bare glob characters in this help string -- sphinx-click renders help as RST,
    # where a stray asterisk parses as an unterminated emphasis marker and fails the
    # -W docs build. The concrete pattern examples live in the docstring's literal block.
    "--path", "sched_path", metavar="PATHSPEC",
    help="Scope the job to a subtree, or to a quoted glob pattern that protonfs "
    "re-expands on every run (see the examples below).",
)
@click.option("--resolve", "sched_resolve", help="Conflict strategy passed to push/pull.")
@click.option(
    "--strict", "sched_strict", is_flag=True,
    help="Pass --strict to push/pull, so a run whose --path pattern matches nothing fails.",
)
@click.option("--label", default="", help="Human label shown in --list.")
@click.option("--json", "as_json", is_flag=True, help="With --list, emit JSON.")
def schedule(
    list_, add, uninstall, all_, every, cron_expr, at, command, sched_path,
    sched_resolve, sched_strict, label, as_json,
) -> None:
    """Manage scheduled push/pull cron jobs for this repo.

    Bare ``protonfs schedule`` lists the jobs (it never installs implicitly). ``--add``
    installs a job (needs a cadence: ``--every``/``--cron``/``--at``) and prints its id;
    ``--uninstall <id>`` (or ``-U``, accepting the id or a ``--list`` index) removes one,
    ``--uninstall --all`` removes them all. Each job runs under ``flock`` with an absolute
    ``proton-drive`` path and tuned timeouts, logging to ``.protonfs/schedule/<id>.log``.

    ``--path`` takes a subtree or a glob pattern. A pattern is stored (and passed to
    push/pull) unexpanded, so protonfs re-expands it on every run -- one job can cover a
    whole family of runs and keeps matching as new ones appear::

        protonfs schedule --add --every daily --command pull --path 'mload*/*.ev'

    .. versionadded:: 1.8.0

    .. versionchanged:: 1.11.0
       ``--path`` accepts a glob pattern, re-expanded at run time (#131). Added
       ``--strict``.
    """
    import json as _json
    from pathlib import Path

    from protonfs.commands import schedule as sched
    from protonfs.context import load_context

    if sum(bool(x) for x in (add, uninstall, list_)) > 1:
        raise click.UsageError("--add, --uninstall, and --list are mutually exclusive.")

    ctx = load_context()
    repo = Path(ctx.root)
    try:
        if add:
            job = sched.add_job(
                repo, every=every, cron=cron_expr, at=at, command=command,
                path=sched_path, resolve=sched_resolve, strict=sched_strict, label=label,
            )
            click.echo(f"scheduled job {job.id}: {job.cron}  {job.command}")
            click.echo(f"  wrapper: {job.wrapper_path}")
            click.echo(f"  log:     {job.log_path}")
        elif uninstall or all_:
            if all_:
                removed = sched.remove_all(repo)
                click.echo(f"removed {len(removed)} job(s)." if removed else "no jobs to remove.")
            else:
                job = sched.remove_job(repo, uninstall)
                click.echo(f"removed job {job.id}.")
        else:
            jobs = sched.list_jobs(repo)
            if as_json:
                from dataclasses import asdict

                click.echo(_json.dumps([asdict(j) for j in jobs], indent=2))
            elif not jobs:
                click.echo("no scheduled jobs. Add one with `protonfs schedule --add --every ...`.")
            else:
                for i, j in enumerate(jobs, 1):
                    lbl = f"  {j.label}" if j.label else ""
                    scope = f" {j.path}" if j.path else ""
                    click.echo(f"[{i}] {j.id}  {j.cron!r}  {j.command}{scope}{lbl}")
    except sched.ScheduleError as exc:
        raise click.UsageError(str(exc)) from exc


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
