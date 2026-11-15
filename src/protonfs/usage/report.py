"""Rows and renderings for ``protonfs du``: a :class:`DuRow` list as table, plain lines or JSON.

Main entry points: :func:`build_rows` (engine to rows), :func:`render_table`,
:func:`render_plain` and :func:`to_json`. The row field order, :data:`JSON_ROW_KEYS`, is the
column order of the plain format and the key set of the JSON rows.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model import Breakdown, Entry, GapKind, NodeState
from .paths import is_within, relative

if TYPE_CHECKING:
    from rich.console import Console

    from .engine import UsageEngine
    from .excludes import ExcludeRules
    from .policy import RefreshPolicy
    from .trash import TrashSummary

GAP_LABELS = {
    GapKind.TRASH: "(in trash)",
    GapKind.UNATTRIBUTED: "(unattributed, incl. excluded)",
    GapKind.UNRECONCILED: "(unreconciled)",
}
_UNBOUNDED = 10**9


@dataclass(frozen=True)
class DuRow:
    """One output row.

    :ivar depth: 0 for the target, 1 for its children, and so on.
    :ivar path: escaped Drive path (of the folder, for a gap row).
    :ivar name: display name; ``(in trash)``, ``(unattributed, incl. excluded)`` or
        ``(unreconciled)`` for a gap row.
    :ivar kind: ``"file"``, ``"folder"``, ``"section"`` or ``"gap"``.
    :ivar size: bytes, or ``None`` when unknown.
    :ivar descendants: node count beneath a folder, or ``None``.
    :ivar pct_parent: percent (0 to 100) of the parent row's size, or ``None``.
    :ivar pct_used: percent of the account's used total, or ``None`` when that is unknown.
    :ivar pct_plan: percent of the configured plan size, or ``None`` when unset.
    :ivar in_trash: measured trash bytes whose original parent is within this folder (a
        lower bound unless ``in_trash_complete``), or ``None`` when not applicable or no
        trash pass is stored. It combines the last trash pass with any listing made since
        (see :func:`~protonfs.usage.trash.figure_for`).
    :ivar in_trash_complete: whether ``in_trash`` is the whole answer, or ``None``.
    :ivar unchecked_bytes: when incomplete, an upper bound on the trash ``in_trash``
        leaves out (the bytes of the folders whose trash is unknown), or ``None`` when no
        bound is known.
    :ivar checked_at: Unix time the figure was measured, or ``None``.
    :ivar age_seconds: ``now - checked_at``, or ``None``.
    :ivar state: ``listed``, ``sized``, ``excluded``, ``unmeasured``, ``error``,
        ``partial`` (the ``/`` or ``/devices`` view leaving out a section or device) or
        ``gap``.
    :ivar exclusion: ``none``, ``freeze`` or ``hide``.
    :ivar gap_kind: ``none``, ``in_trash``, ``unattributed`` or ``unreconciled``.
    :ivar error: the failure text when ``state`` is ``error``, else ``None``.
    :ivar trash_at: Unix time of the oldest measurement behind ``in_trash`` (the trash
        pass, or listings made after it), or ``None`` when ``in_trash`` is ``None``. It can
        be older than ``checked_at``: ``--refresh`` below ``/`` does not rerun the trash
        pass.
    :ivar trash_unresolved: on the ``/`` row, the trashed folders whose size is unknown
        because the folder they came from is not cached (``in_trash`` is then a lower
        bound); ``0`` on every other row and when no trash pass is stored.

    .. versionadded:: 3.4.0
    """

    depth: int
    path: str
    name: str
    kind: str
    size: int | None
    descendants: int | None
    pct_parent: float | None
    pct_used: float | None
    pct_plan: float | None
    in_trash: int | None
    in_trash_complete: bool | None
    unchecked_bytes: int | None
    checked_at: float | None
    age_seconds: float | None
    state: str
    exclusion: str
    gap_kind: str
    error: str | None = None
    trash_at: float | None = None
    trash_unresolved: int = 0


#: Row field names in order: the plain-format columns and the JSON row keys.
JSON_ROW_KEYS = tuple(f.name for f in dataclasses.fields(DuRow))


def _pct(part: int | None, whole: int | None) -> float | None:
    if part is None or not whole:
        return None
    return round(part / whole * 100, 2)


def _sort_key(entry: Entry):
    return (entry.size is None, -(entry.size or 0), entry.name)


class _Collector:
    def __init__(self, engine, policy, rules, target, maxdepth, plan_size, now):
        self.engine = engine
        self.policy = policy
        self.rules = rules
        self.target = target
        self.maxdepth = maxdepth
        self.plan_size = plan_size
        self.now = now
        self.rows: list[DuRow] = []
        self.used = None
        # Row indexes whose In-trash cells are filled by fill_trash, once every breakdown
        # (and the root's trash pass) has run.
        self.trash_rows: list[int] = []

    def row(self, depth, entry, parent_size, *, bd: Breakdown | None = None, at=None):
        size = bd.size if bd is not None and bd.size is not None else entry.size
        desc = bd.descendants if bd is not None and bd.descendants is not None \
            else entry.descendants
        state = bd.state if bd is not None else entry.state
        when = at if at is not None else entry.at
        if entry.kind != "file" and state in (
                NodeState.LISTED, NodeState.SIZED, NodeState.PARTIAL):
            self.trash_rows.append(len(self.rows))
        self.rows.append(DuRow(
            depth, entry.path, entry.name, entry.kind, size, desc,
            _pct(size, parent_size), _pct(size, self.used), _pct(size, self.plan_size),
            None, None, None, when,
            None if when is None else max(0.0, self.now - when),
            state.value, entry.exclusion.value, GapKind.NONE.value,
            # The row's own breakdown, when there is one, decides both state and error.
            bd.error if bd is not None else entry.error,
        ))

    def fill_trash(self) -> None:
        """Fill the In-trash cells from the stored trash pass; blank when none is stored.

        Without a stored pass every folder's figure would be a vacuous "≥ 0 B".
        """
        from .trash import figures_for

        summary = self.engine.trash_summary()
        if summary is None or not self.trash_rows:
            return
        figures = figures_for(self.engine, [self.rows[i].path for i in self.trash_rows])
        for i in self.trash_rows:
            path = self.rows[i].path
            fig = figures[path]
            self.rows[i] = dataclasses.replace(
                self.rows[i], in_trash=fig.known, in_trash_complete=fig.complete,
                unchecked_bytes=fig.unchecked_bytes, trash_at=fig.at,
                trash_unresolved=summary.unresolved_folders if path == "/" else 0,
            )

    def gap(self, depth, bd: Breakdown):
        if bd.gap_kind is GapKind.NONE or bd.gap is None:
            return
        self.rows.append(DuRow(
            depth, bd.path, GAP_LABELS[bd.gap_kind], "gap", bd.gap, None,
            _pct(bd.gap, bd.size), _pct(bd.gap, self.used), _pct(bd.gap, self.plan_size),
            None, None, None, bd.oldest_at,
            None if bd.oldest_at is None else max(0.0, self.now - bd.oldest_at),
            "gap", "none", bd.gap_kind.value,
        ))

    def children(self, depth, bd: Breakdown):
        for entry in sorted(bd.entries, key=_sort_key):
            sub = None
            recurse = (
                entry.kind != "file" and depth + 1 < self.maxdepth
                # ERROR is retried: the child's own breakdown sizes it again. The /devices
                # placeholder is not a folder; the root view retries the device list itself.
                and entry.state not in (NodeState.EXCLUDED, NodeState.UNMEASURED)
                and entry.path != "/devices"
                and entry.exclusion.value == "none"
            )
            if recurse:
                sub = self.engine.breakdown(entry.path, self.policy, self.rules, self.target)
            self.row(depth + 1, entry, bd.size, bd=sub,
                     at=(sub.oldest_at or sub.at) if sub is not None else None)
            if sub is not None:
                self.children(depth + 1, sub)
        self.gap(depth + 1, bd)


def collect_rows(
    engine: UsageEngine,
    target: str,
    policy: RefreshPolicy,
    rules: ExcludeRules,
    *,
    maxdepth: int | None = 1,
    plan_size: int | None = None,
    now: float,
    base: str | None = None,
) -> list[DuRow]:
    """Return every row down to ``maxdepth``, target first, in pre-order.

    Children are sorted by size descending (unknown last). A gap pseudo-row follows a
    folder's children when its remainder is labelled. The first row is always the target.
    For ``target == "/"`` the trash pass
    (:meth:`~protonfs.usage.engine.UsageEngine.refresh_trash`) runs after every breakdown
    shown, so it can resolve the folders they listed; the In-trash cells are filled
    last, from the stored pass.

    :param engine: the usage engine (makes the Drive calls the policy allows).
    :param target: escaped Drive path; depth 0.
    :param policy: refresh policy.
    :param rules: freeze/hide rules relative to ``target``.
    :param maxdepth: deepest level shown; ``None`` for no limit.
    :param plan_size: plan size in bytes for the ``pct_plan`` column, or ``None``.
    :param now: Unix time used for ages.
    :param base: escaped Drive path ``rules`` are matched relative to; defaults to ``target``.
        Must contain ``target``.
    :returns: the rows.
    :raises ValueError: if ``target`` is not within ``base``.

    .. versionadded:: 3.4.0
    """
    limit = _UNBOUNDED if maxdepth is None else maxdepth
    base = target if base is None else base
    if not is_within(target, base):
        raise ValueError(f"{target!r} is not within the rules base {base!r}")
    col = _Collector(engine, policy, rules, base, limit, plan_size, now)
    if limit == 0 and target != "/":
        entry = engine.figure(target, policy)
        col.used = None
        col.row(0, entry, None)
        col.rows[0] = _with_used(col.rows[0], engine.used_total())
        col.fill_trash()
        return col.rows
    if target == "/":
        bd = engine.root(policy, rules, trash=False)
    else:
        bd = engine.breakdown(target, policy, rules, base)
    col.used = engine.used_total()
    entry = Entry(
        "/" if target == "/" else target.rsplit("/", 1)[-1], target,
        "folder", None, bd.size, bd.descendants, bd.at, bd.state,
        rules.match(relative(target, base), True), None, True, bd.error,
    )
    col.row(0, entry, None, bd=bd, at=bd.oldest_at if bd.oldest_at is not None else bd.at)
    if limit > 0:
        col.children(0, bd)
    if target == "/":
        engine.refresh_trash(policy, rules)
    col.fill_trash()
    # used_total is only known once the breakdowns have populated the cache
    used = engine.used_total()
    if used != col.used:
        col.rows = [_with_used(r, used) for r in col.rows]
    return col.rows


def _with_used(row: DuRow, used: int | None) -> DuRow:
    return dataclasses.replace(row, pct_used=_pct(row.size, used))


def select_rows(rows: list[DuRow], *, mindepth: int = 0, dirs_only: bool = False) -> list[DuRow]:
    """Return ``rows`` without those shallower than ``mindepth`` (and files if ``dirs_only``).

    .. versionadded:: 3.4.0
    """
    return [r for r in rows if r.depth >= mindepth and not (dirs_only and r.kind == "file")]


def build_rows(
    engine: UsageEngine,
    target: str,
    policy: RefreshPolicy,
    rules: ExcludeRules,
    *,
    mindepth: int = 0,
    maxdepth: int | None = 1,
    dirs_only: bool = False,
    plan_size: int | None = None,
    now: float,
) -> list[DuRow]:
    """Return the rows of :func:`collect_rows` filtered by :func:`select_rows`.

    :param engine: the usage engine.
    :param target: escaped Drive path; depth 0.
    :param policy: refresh policy.
    :param rules: freeze/hide rules relative to ``target``.
    :param mindepth: drop rows shallower than this.
    :param maxdepth: deepest level shown; ``None`` for no limit.
    :param dirs_only: drop file rows.
    :param plan_size: plan size in bytes, or ``None``.
    :param now: Unix time used for ages.
    :returns: the rows.

    .. versionadded:: 3.4.0
    """
    rows = collect_rows(engine, target, policy, rules, maxdepth=maxdepth,
                        plan_size=plan_size, now=now)
    return select_rows(rows, mindepth=mindepth, dirs_only=dirs_only)


def to_json(
    rows: list[DuRow],
    *,
    target: str,
    trash: TrashSummary | None,
    used_total: int | None,
    plan_size: int | None,
    now: float,
) -> dict:
    """Return the JSON document for one target.

    :param rows: the rows to include.
    :param target: the target's Drive path.
    :param trash: the stored trash summary (whatever the target), or ``None`` when no
        pass has run.
    :param used_total: account used bytes, or ``None``.
    :param plan_size: plan size in bytes, or ``None``.
    :param now: Unix time of generation.
    :returns: a dict with keys ``target``, ``generated_at``, ``used_total``, ``plan_size``,
        ``trash`` and ``rows``; ``trash`` is ``None`` or ``{at, known, complete, partial,
        unresolved_folders, unattributed}``, where ``unattributed`` lists the groups of
        trashed folders whose bytes are unknown as ``{parent, folders, reason}``.

    .. versionadded:: 3.4.0
    """
    trash_doc = None
    if trash is not None:
        trash_doc = {
            "at": trash.at, "known": trash.total_known, "complete": trash.complete,
            "partial": trash.partial, "unresolved_folders": trash.unresolved_folders,
            "unattributed": [
                {"parent": g.parent_path, "folders": g.folders, "reason": g.reason}
                for g in trash.groups if g.folders and g.bytes is None
            ],
        }
    return {
        "target": target, "generated_at": now, "used_total": used_total,
        "plan_size": plan_size, "trash": trash_doc,
        "rows": [{k: getattr(r, k) for k in JSON_ROW_KEYS} for r in rows],
    }


def render_plain(rows: list[DuRow]) -> list[str]:
    """Return one tab-separated line per row in :data:`JSON_ROW_KEYS` order.

    ``None`` is an empty string and booleans are ``true``/``false``.

    .. versionadded:: 3.4.0
    """
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    return ["\t".join(cell(getattr(r, k)) for k in JSON_ROW_KEYS) for r in rows]


def trash_cell(row: DuRow, *, now: float | None = None) -> str:
    """Return the ``In trash`` cell text for ``row``.

    Complete: the size. Incomplete: ``≥ X (Y / Z% unchecked)``, or ``≥ X (unchecked:
    unknown)`` when the unchecked bytes are unknown. With ``now``, the figure's own age is
    added (``0 B (2d old)``, ``≥ X (Y / Z% unchecked, 2d old)``) whenever it reads
    differently from the row's Age column.

    :param row: the row.
    :param now: Unix time the ages are measured at, or ``None`` to leave the age out.
    :returns: the cell text; empty when the row has no trash figure, ``—`` for a file.

    .. versionadded:: 3.4.0
    """
    from protonfs.commands.ls import human_size
    from protonfs.retention import format_age

    if row.in_trash is None:
        return "—" if row.kind == "file" else ""
    age = None
    if now is not None and row.trash_at is not None:
        age = format_age(max(0.0, now - row.trash_at))
        if row.age_seconds is not None and age == format_age(row.age_seconds):
            age = None
    old = [] if age is None else [f"{age} old"]
    if row.in_trash_complete:
        return human_size(row.in_trash) + (f" ({old[0]})" if old else "")
    head = f"≥ {human_size(row.in_trash)}"
    if row.unchecked_bytes is None or not row.size:
        return f"{head} ({', '.join(['unchecked: unknown', *old])})"
    unchecked = min(row.unchecked_bytes, row.size)  # never more than the row itself
    share = round(unchecked / row.size * 100)
    note = f"{human_size(unchecked)} / {share}% unchecked"
    return f"{head} ({', '.join([note, *old])})"


def render_table(
    rows: list[DuRow], console: Console, *, title: str | None = None, now: float | None = None
) -> None:
    """Print ``rows`` as a rich table (names indented two spaces per depth).

    :param rows: the rows to print.
    :param console: where to print them.
    :param title: printed above the table, if given.
    :param now: Unix time the rows' ages were taken at; with it, an In-trash figure older
        than its row says its own age (see :func:`trash_cell`).

    .. versionadded:: 3.4.0
    """
    from rich.table import Table
    from rich.text import Text

    from protonfs.commands.ls import human_size
    from protonfs.retention import format_age

    def pct(v, row):
        if v is not None:
            return f"{v:.1f}%"
        return "" if row.size is None else "—"

    table = Table(title=title, title_justify="left", box=None, pad_edge=False)
    table.add_column("Name", no_wrap=True)
    for col in ("Size", "%Parent", "%Used", "%Plan", "Items", "In trash", "Age"):
        table.add_column(col, justify="right")
    table.add_column("State")
    for r in rows:
        name = Text("  " * r.depth + r.name, style="dim italic" if r.kind == "gap" else "")
        items = "" if r.descendants is None and r.kind != "file" else (
            "—" if r.descendants is None else str(r.descendants))
        table.add_row(
            name,
            "" if r.size is None else human_size(r.size),
            pct(r.pct_parent, r), pct(r.pct_used, r), pct(r.pct_plan, r),
            items, trash_cell(r, now=now),
            "" if r.age_seconds is None else format_age(r.age_seconds),
            r.state if r.exclusion == "none" else f"{r.state} ({r.exclusion})",
        )
    console.print(table)
