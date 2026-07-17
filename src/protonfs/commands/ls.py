# src/protonfs/commands/ls.py
"""``protonfs ls``: list tracked files with their sync state, or list Drive's trash.

Beyond the flat file table, ``ls`` can aggregate per directory (``--dirs``: counts by
state plus cumulative local/indexed sizes, #97/#94), filter to specific sync states
(``--state``), and emit machine-readable output (``--format plain|json``) for scripts.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from protonfs.context import RepoContext
from protonfs.diff import DiffEntry, classify, within_subpath
from protonfs.drive import RemoteEntry, decrypted_name
from protonfs.ignore import IgnoreMatcher
from protonfs.localscan import scan

# Output formats shared by `ls` and `status` (#97). "table" is the interactive rich
# rendering; "plain" is tab-separated lines; "json" is one JSON document per listing.
LS_FORMATS = ("table", "plain", "json")


def remote_rel_paths(ctx: RepoContext, subpath: str | None = None) -> dict[str, RemoteEntry]:
    """Recursive files-only remote listing, scoped to `subpath` when given. rel_paths
    are re-prefixed with the subpath so they match the index's repo-root-relative keys
    (same convention as `refresh`)."""
    remote_root = ctx.config.remote_root
    if subpath:
        remote_root = f"{remote_root}/{subpath}"
    result = {e.rel_path: e for e in ctx.drive.walk(remote_root) if not e.is_dir}
    if subpath:
        result = {f"{subpath}/{rel}": entry for rel, entry in result.items()}
    return result


def collect_entries(
    ctx: RepoContext,
    subpath: str | None,
    remote: bool,
    states: tuple[str, ...] = (),
    reporter=None,
) -> list[DiffEntry]:
    """Classify every tracked path under `subpath`, optionally filtered to `states`.

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree, or ``None`` for everything.
    :param remote: cross-reference a live remote walk (else local-vs-index only).
    :param states: when non-empty, keep only entries whose state value is listed.
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    :returns: the filtered, rel_path-sorted classification.
    """
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
    ignore = IgnoreMatcher.from_file(ctx.root)
    scan_root = Path(subpath) if subpath else Path(".")
    local = scan(ctx.root, scan_root, ignore, ctx.index, low_io=ctx.config.defaults.low_io)
    if remote:
        reporter.phase("walking remote", subpath=subpath or ".")
    remote_map = remote_rel_paths(ctx, subpath) if remote else None
    # classify reasons over the whole repo-wide index; when a subpath was given, the
    # local scan and remote walk are scoped to it, so restrict the rows to that
    # subpath too -- otherwise out-of-scope index entries (never scanned/walked) show
    # up, and with a remote view are misread as remote-deleted.
    entries = [
        e for e in classify(local, ctx.index, remote_map) if within_subpath(e.rel_path, subpath)
    ]
    if states:
        entries = [e for e in entries if e.state.value in states]
    return entries


@dataclass
class DirSummary:
    """Aggregate of one immediate subdirectory in ``ls --dirs`` (#97/#94).

    :ivar path: the immediate child directory of the listed path (``"."`` collects
        files sitting directly in the listed path itself).
    :ivar files: number of tracked files under it (after any --state filter).
    :ivar local_bytes: cumulative size of the files' local copies (0 when offloaded).
    :ivar indexed_bytes: cumulative size the index records for them -- the remote-side
        size for synced/metadata-only files (#94).
    :ivar apparent_bytes: the directory's true footprint: per file, whichever of the
        local/indexed size is known (they agree when synced; local for a not-yet-pushed
        file, indexed for an offloaded one). This is the size the ``--visual`` charts
        use, so a fresh local-only tree and an all-offloaded tree both chart correctly.
    :ivar states: per-:class:`~protonfs.diff.SyncState` file counts.
    """

    path: str
    files: int
    local_bytes: int
    indexed_bytes: int
    apparent_bytes: int
    states: dict[str, int]


def summarize_dirs(
    ctx: RepoContext, entries: list[DiffEntry], subpath: str | None
) -> list[DirSummary]:
    """Group `entries` by their immediate subdirectory under `subpath` and total them.

    Sizes come from what already exists (#94): the local stat for materialised files,
    and the index's recorded size (the remote-side size) for every tracked entry --
    no new manifest field is needed.
    """
    base = f"{subpath}/" if subpath else ""
    groups: dict[str, list[DiffEntry]] = {}
    for entry in entries:
        rest = (
            entry.rel_path[len(base):]
            if base and entry.rel_path.startswith(base)
            else entry.rel_path
        )
        child = rest.split("/", 1)[0] if "/" in rest else "."
        groups.setdefault(child, []).append(entry)

    summaries: list[DirSummary] = []
    for child in sorted(groups):
        members = groups[child]
        states: Counter = Counter()
        local_bytes = 0
        indexed_bytes = 0
        apparent_bytes = 0
        for entry in members:
            states[entry.state.value] += 1
            try:
                local_size = (ctx.root / entry.rel_path).stat().st_size
            except OSError:
                local_size = 0  # offloaded / metadata-only / vanished: no local bytes
            indexed = ctx.index.get(entry.rel_path)
            indexed_size = indexed.size if indexed is not None else 0
            local_bytes += local_size
            indexed_bytes += indexed_size
            # Per-file real footprint: the two agree when synced; take the known one
            # when the file is only local (not yet pushed) or only remote (offloaded).
            apparent_bytes += max(local_size, indexed_size)
        summaries.append(
            DirSummary(
                path=child,
                files=len(members),
                local_bytes=local_bytes,
                indexed_bytes=indexed_bytes,
                apparent_bytes=apparent_bytes,
                states=dict(states),
            )
        )
    return summaries


def human_size(n: int) -> str:
    """``1536`` -> ``"1.5 KiB"``: binary-unit rendering for the --dirs size columns."""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover -- loop always returns


def _states_summary(states: dict[str, int]) -> str:
    """Render a ``{state: count}`` map as a compact ``state=count ...`` string."""
    return " ".join(f"{state}={count}" for state, count in sorted(states.items()))


def render_ls(
    ctx: RepoContext,
    subpath: str | None,
    remote: bool,
    trash: bool,
    console: Console,
    *,
    dirs: bool = False,
    states: tuple[str, ...] = (),
    fmt: str = "table",
    visual: str | None = None,
    echo=print,
) -> None:
    """Print tracked files (or ``--dirs`` aggregates, or a trash listing).

    :param ctx: the loaded repo context.
    :param subpath: repo-root-relative subtree to list, or ``None`` for everything.
    :param remote: when true, cross-reference a recursive remote walk so remote-only
        and remote-changed states are shown (otherwise state is local-vs-index only).
    :param trash: when true, list ``/trash`` entries instead of tracked files;
        ``subpath``/``remote``/``dirs``/``states`` are ignored in this mode.
    :param console: the :class:`rich.console.Console` for ``table`` output.
    :param dirs: aggregate per immediate subdirectory (counts + sizes) instead of
        listing every file (#97/#94).
    :param states: sync-state filter; empty means all states (#97).
    :param fmt: ``table`` (rich, default) | ``plain`` (tab-separated) | ``json`` (#97).
    :param visual: ``"treemap"`` | ``"waffle"`` to draw a per-directory storage-usage
        chart (by each dir's :attr:`DirSummary.apparent_bytes` footprint) instead of the
        table; ``None`` for no chart. Implies directory aggregation (#94).
    :param echo: sink for plain/json lines (overridable for tests).

    .. seealso:: :func:`collect_entries` / :func:`summarize_dirs` for the data layer,
        and :mod:`protonfs.commands.storage_viz` for the chart renderers.
    """
    if trash:
        rows = [
            {"name": name, "type": entry.get("type", "")}
            for entry in ctx.drive.list("/trash")
            if (name := decrypted_name(entry)) is not None
        ]
        if fmt == "json":
            echo(json.dumps(rows))
        elif fmt == "plain":
            for row in rows:
                echo(f"{row['name']}\t{row['type']}")
        else:
            table = Table("name", "type")
            for row in rows:
                table.add_row(row["name"], row["type"])
            console.print(table)
        return

    entries = collect_entries(ctx, subpath, remote, states)

    if visual is not None:
        # A chart is inherently a per-directory, terminal-only view: aggregate and draw
        # by indexed (remote/full) size, which counts offloaded files that local size
        # would miss.
        from protonfs.commands.storage_viz import render_storage_visual

        summaries = summarize_dirs(ctx, entries, subpath)
        render_storage_visual(
            visual, [(s.path, s.apparent_bytes) for s in summaries], console
        )
        return

    if dirs:
        summaries = summarize_dirs(ctx, entries, subpath)
        if fmt == "json":
            echo(
                json.dumps(
                    [
                        {
                            "path": s.path,
                            "files": s.files,
                            "local_bytes": s.local_bytes,
                            "indexed_bytes": s.indexed_bytes,
                            "apparent_bytes": s.apparent_bytes,
                            "states": s.states,
                        }
                        for s in summaries
                    ]
                )
            )
        elif fmt == "plain":
            for s in summaries:
                echo(
                    f"{s.path}\t{s.files}\t{s.local_bytes}\t{s.indexed_bytes}\t"
                    f"{_states_summary(s.states)}"
                )
        else:
            table = Table("dir", "files", "local size", "indexed size", "states")
            for s in summaries:
                table.add_row(
                    s.path,
                    str(s.files),
                    human_size(s.local_bytes),
                    human_size(s.indexed_bytes),
                    _states_summary(s.states),
                )
            console.print(table)
        return

    if fmt == "json":
        echo(json.dumps([{"path": e.rel_path, "state": e.state.value} for e in entries]))
    elif fmt == "plain":
        for entry in entries:
            echo(f"{entry.rel_path}\t{entry.state.value}")
    else:
        table = Table("path", "state")
        for entry in entries:
            table.add_row(entry.rel_path, entry.state.value)
        console.print(table)
