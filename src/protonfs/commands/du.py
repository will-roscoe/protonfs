"""``protonfs du``: how much Proton Drive space remote paths take, trash included.

The terminal front end over :mod:`protonfs.usage`. It parses the flags, resolves the
targets, and renders the engine's rows as a table, plain lines or JSON.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import click

if TYPE_CHECKING:
    from protonfs.usage.engine import UsageEngine

_DEPTH_RE = re.compile(r"^(\d+)?(-)?(\d+)?$")


def parse_depth(text: str) -> tuple[int, int | None]:
    """Parse a ``-d`` value into ``(mindepth, maxdepth)``.

    ``N`` is a maximum depth (as in ``du -d N``), so the minimum is 0. ``MIN-MAX`` is a
    range, ``MIN-`` has no maximum, and ``-MAX`` is the same as ``MAX``.

    :param text: the option value, e.g. ``"2"``, ``"1-3"``, ``"2-"`` or ``"-2"``.
    :returns: ``(mindepth, maxdepth)``, with ``maxdepth`` ``None`` for an open range.
    :raises click.BadParameter: for anything else, or when MIN exceeds MAX.

    .. versionadded:: 3.4.0
    """
    match = _DEPTH_RE.match(text.strip())
    if not match or not (match.group(1) or match.group(3)):
        raise click.BadParameter(f"{text!r}: use N, MIN-MAX, MIN- or -MAX (e.g. 2 or 1-3)")
    low, dash, high = match.groups()
    if not dash:
        return 0, int(low)
    mindepth = int(low) if low else 0
    maxdepth = int(high) if high else None
    if maxdepth is not None and mindepth > maxdepth:
        raise click.BadParameter(f"{text!r}: MIN ({mindepth}) is greater than MAX ({maxdepth})")
    return mindepth, maxdepth


@dataclass
class DuOptions:
    """Every parsed ``du`` flag.

    :ivar exclude_updates: ``-x`` patterns (frozen: no Drive calls beneath).
    :ivar exclude: ``-X`` patterns (hidden).
    :ivar mindepth: shallowest depth shown.
    :ivar maxdepth: deepest depth shown, ``None`` for no limit.
    :ivar dirs_only: hide file rows.
    :ivar refresh: refresh policy text (``5d``, ``0``, ``never``).
    :ivar fmt: ``table``, ``plain`` or ``json``.
    :ivar plan_size: plan size in bytes, or ``None``.
    :ivar progress: report Drive calls on stderr.
    :ivar gui: ``--gui`` was given.
    :ivar listen: ``--listen`` value.
    :ivar port: ``--port`` value.
    :ivar log: ``--log`` value.

    .. versionadded:: 3.4.0
    """

    exclude_updates: tuple = ()
    exclude: tuple = ()
    mindepth: int = 0
    maxdepth: int | None = 1
    dirs_only: bool = False
    refresh: str = "5d"
    fmt: str = "table"
    plan_size: int | None = None
    progress: bool = False
    gui: bool = False
    listen: str | None = None
    port: str | None = None
    log: str | None = None


def _depth_range(
    depth: str | None, mindepth: int | None, maxdepth: int | None
) -> tuple[int, int | None]:
    """Resolve ``-d``/``--mindepth``/``--maxdepth`` into ``(low, high)``.

    ``--mindepth`` alone keeps a bounded walk: ``high`` is ``max(low, 1)``.

    :raises click.UsageError: for ``-d`` with the long forms, or ``low > high``.
    :raises click.BadParameter: for a malformed ``-d``.
    """
    if depth is not None:
        if mindepth is not None or maxdepth is not None:
            raise click.UsageError("-d/--depth cannot be combined with --mindepth/--maxdepth.")
        return parse_depth(depth)
    low = 0 if mindepth is None else mindepth
    high = max(low, 1) if maxdepth is None else maxdepth
    if low > high:
        raise click.UsageError(f"--mindepth ({low}) is greater than --maxdepth ({high}).")
    return low, high


def build_options(
    *,
    exclude_updates=(),
    exclude=(),
    mindepth=None,
    maxdepth=None,
    depth=None,
    dirs_only=False,
    refresh=None,
    fmt=None,
    as_json=False,
    progress=False,
    gui=False,
    listen=None,
    port=None,
    log=None,
    n_paths=0,
) -> DuOptions:
    """Validate raw flag values and return the :class:`DuOptions`.

    Resolves ``refresh`` and the plan size from ``du.refresh``/``du.plan_size`` (env var,
    then global config) when not given.

    :raises click.UsageError: for any invalid combination of flags.
    :raises click.ClickException: when the stored plan size is not a valid size.

    .. versionadded:: 3.4.0
    """
    from protonfs.commands.config import resolve_du_setting
    from protonfs.retention import RetentionError
    from protonfs.usage.policy import RefreshPolicy, parse_size

    if as_json:
        if fmt not in (None, "json"):
            raise click.UsageError("--json cannot be combined with --format " + fmt + ".")
        fmt = "json"
    low, high = _depth_range(depth, mindepth, maxdepth)
    if not gui and (listen is not None or port is not None or log is not None):
        raise click.UsageError("--listen, --port and --log are only valid with --gui.")
    if gui:
        if n_paths > 1:
            raise click.UsageError("--gui takes at most one PATH.")
        if fmt is not None or progress or mindepth is not None or low > 0:
            raise click.UsageError(
                "--gui cannot be combined with --format/--json, -p, --mindepth or a "
                "-d with a minimum depth."
            )
    text = refresh if refresh is not None else (resolve_du_setting("du.refresh") or "5d")
    try:
        RefreshPolicy.parse(text)
    except RetentionError as exc:
        raise click.UsageError(f"--refresh {text!r}: {exc}") from exc
    plan_text = resolve_du_setting("du.plan_size")
    plan = None
    if plan_text:
        try:
            plan = parse_size(plan_text)
        except ValueError as exc:
            raise click.ClickException(f"du.plan_size: {exc}") from exc
    return DuOptions(
        tuple(exclude_updates), tuple(exclude), low, high, dirs_only, text,
        fmt or "table", plan, progress, gui, listen, port, log,
    )


def default_engine() -> UsageEngine:
    """Return the production engine: the ``proton-drive`` CLI behind the on-disk cache.

    .. versionadded:: 3.4.0
    """
    from protonfs.drive import DriveClient
    from protonfs.usage.cache import UsageCache, default_cache_path
    from protonfs.usage.engine import UsageEngine
    from protonfs.usage.source import CliDriveSource

    return UsageEngine(
        CliDriveSource(DriveClient(), concurrency=4), UsageCache(default_cache_path())
    )


PLAN_HINT = "% of plan needs your plan size: protonfs config set --global du.plan_size 3TB"

#: Printed once to stderr when a ``size`` call is held up by Proton's rate limit.
THROTTLE_NOTICE = (
    "du: Proton is rate-limiting folder sizes (about 20 at a time, then a pause of a few "
    "minutes), so this can take a while. Sizes measured so far are kept: Ctrl-C and run "
    "again later to continue from them (not with --refresh 0)."
)


def _trash_lines(summary) -> list:
    from protonfs.commands.ls import human_size

    if summary is None:
        return ["trash: not measured (no cached trash pass)"]
    lines = []
    if summary.partial:
        lines.append(
            f"trash: partial, >= {human_size(summary.total_known)} measured ({summary.partial})"
        )
    if summary.unresolved_folders:
        lines.append(f"{summary.unresolved_folders} trashed folders, size unknown")
    return lines


def _attach_throttle_notice(engine, err) -> None:
    """Print :data:`THROTTLE_NOTICE` once when the engine's source reports a slow size."""
    source = getattr(engine, "source", None)
    if source is None or not hasattr(source, "on_slow_size"):
        return
    lock = threading.Lock()
    shown = []

    def on_slow_size(path: str) -> None:
        with lock:
            if shown:
                return
            shown.append(path)
            err.write(THROTTLE_NOTICE + "\n")
            err.flush()

    source.on_slow_size = on_slow_size


def run_du(
    targets: list,
    options: DuOptions,
    *,
    engine_factory: Callable[[], UsageEngine] | None = None,
    out=None,
    err=None,
) -> int:
    """Measure each target and print the result in ``options.fmt``.

    Output is always printed in full, including rows that failed. With several targets,
    one target whose own figure failed makes the whole run exit ``1``; the others still
    print.

    :param targets: resolved targets, one section each.
    :param options: the parsed flags.
    :param engine_factory: builds the engine; defaults to :func:`default_engine`.
    :param out: stream for the result (default ``sys.stdout``).
    :param err: stream for warnings, the plan hint and ``-p`` progress (default
        ``sys.stderr``).
    :returns: ``0`` complete; ``1`` a target's own figure failed; ``3`` partial results
        (a row in state ``error``, a ``/`` total that leaves out a section, an unmeasurable
        ``/devices`` row, or an incomplete trash pass). A user's ``-x``/``-X`` alone is not
        partial.

    .. versionadded:: 3.4.0
    """
    import json

    from rich.console import Console

    from protonfs.reporting import Reporter, get_reporter
    from protonfs.usage.excludes import ExcludeRules
    from protonfs.usage.policy import RefreshPolicy
    from protonfs.usage.report import collect_rows, render_plain, render_table, select_rows, to_json

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    engine = (engine_factory or default_engine)()
    _attach_throttle_notice(engine, err)
    policy = RefreshPolicy.parse(options.refresh)
    rules = ExcludeRules(options.exclude_updates, options.exclude)
    if options.progress:
        reporter = Reporter(
            max(get_reporter().level, 1),
            progress_style="inline" if get_reporter().inline else "lines",
            stream=err,
        )
        state = {"n": 0, "t": 0.0}
        lock = threading.Lock()

        def on_call(op: str, path: str) -> None:
            with lock:
                state["n"] += 1
                now = time.monotonic()
                if now - state["t"] >= 0.5:
                    state["t"] = now
                    reporter.phase("du", calls=state["n"], last=f"{op} {path}")

        engine.on_call = on_call
    engine.check_account(policy)

    fatal = partial = need_used = False
    docs = []
    hint = False
    console = Console(file=out, width=None if getattr(out, "isatty", lambda: False)() else 200,
                      highlight=False, markup=False)
    for target in targets:
        now = engine.clock()
        rows = collect_rows(engine, target.remote, policy, rules, maxdepth=options.maxdepth,
                            plan_size=options.plan_size, now=now)
        fatal = fatal or rows[0].state == "error"
        partial = partial or (rows[0].state == "partial"
                           and not (options.exclude or options.exclude_updates)) or any(
            r.state == "error"
            or (r.kind == "section" and r.size is None and r.exclusion == "none"
                and r.path != "/photos")
            for r in rows[1:]
        )
        shown = select_rows(rows, mindepth=options.mindepth, dirs_only=options.dirs_only)
        for r in rows:
            if r.state == "error":
                err.write(f"! {r.path}: {r.error or 'measurement failed'}\n")
        summary = engine.trash_summary() if target.remote == "/" else None
        partial = partial or bool(
            summary is not None and (summary.partial or summary.unresolved_folders > 0)
        )
        if options.fmt == "json":
            docs.append(to_json(shown, target=target.remote, trash=summary,
                                used_total=engine.used_total(), plan_size=options.plan_size,
                                now=now))
        elif options.fmt == "plain":
            for line in render_plain(shown):
                out.write(line + "\n")
        else:
            render_table(shown, console, title=target.remote if len(targets) > 1 else None)
            if target.remote == "/":
                for line in _trash_lines(summary):
                    out.write(line + "\n")
            hint = hint or options.plan_size is None
            need_used = need_used or (engine.used_total() is None and options.fmt == "table")
    if options.fmt == "json":
        out.write(json.dumps(docs[0] if len(docs) == 1 else docs, indent=2) + "\n")
    if hint:
        err.write(PLAN_HINT + "\n")
    if need_used:
        err.write("(%Used needs `protonfs du /` once)\n")
    return 1 if fatal else 3 if partial else 0
