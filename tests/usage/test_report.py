"""Tests for :mod:`protonfs.usage.report`: rows, JSON and cell rendering."""

from __future__ import annotations

import io

from rich.console import Console
from tests.usage.treedrive import TreeDrive

from protonfs.usage.engine import UsageEngine
from protonfs.usage.excludes import ExcludeRules
from protonfs.usage.model import GapKind
from protonfs.usage.policy import RefreshPolicy
from protonfs.usage.report import (
    GAP_LABELS,
    JSON_ROW_KEYS,
    DuRow,
    build_rows,
    render_plain,
    render_table,
    to_json,
    trash_cell,
)

POLICY = RefreshPolicy.parse("5d")


def _engine(usage_cache, clock):
    d = TreeDrive()
    d.add_file("/my-files/A/f", 10)
    d.add_file("/my-files/A/s/g", 90)
    d.add_file("/my-files/A/t/h", 5)
    return UsageEngine(d, usage_cache, clock=clock)


def test_rows_preorder_sorted_target_first(usage_cache, clock):
    eng = _engine(usage_cache, clock)
    rows = build_rows(eng, "/my-files/A", POLICY, ExcludeRules(), maxdepth=2, now=clock())
    assert [(r.depth, r.name) for r in rows] == [
        (0, "A"), (1, "s"), (2, "g"), (1, "f"), (1, "t"), (2, "h")]
    assert rows[0].pct_parent is None
    assert rows[1].pct_parent == round(90 / 105 * 100, 2)


def test_mindepth_filters_and_target_is_depth_zero(usage_cache, clock):
    eng = _engine(usage_cache, clock)
    rows = build_rows(eng, "/my-files/A", POLICY, ExcludeRules(), mindepth=1, now=clock())
    assert rows and min(r.depth for r in rows) == 1


def test_gap_row_follows_children(usage_cache, clock):
    eng = _engine(usage_cache, clock)
    eng.source.trash("/my-files/A/f")
    rows = build_rows(eng, "/my-files/A", POLICY, ExcludeRules(), now=clock())
    gap = rows[-1]
    assert gap.kind == "gap" and gap.name == GAP_LABELS[GapKind.TRASH] and gap.size == 10


def test_json_and_plain_shapes(usage_cache, clock):
    eng = _engine(usage_cache, clock)
    rows = build_rows(eng, "/my-files/A", POLICY, ExcludeRules(), now=clock())
    doc = to_json(rows, target="/my-files/A", trash=None, used_total=None,
                  plan_size=None, now=clock())
    assert doc["trash"] is None and set(doc["rows"][0]) == set(JSON_ROW_KEYS)
    assert all(ln.count("\t") == len(JSON_ROW_KEYS) - 1 for ln in render_plain(rows))


def _row(**kw):
    base = dict(depth=0, path="/p", name="p", kind="folder", size=1000, descendants=1,
                pct_parent=None, pct_used=None, pct_plan=None, in_trash=500,
                in_trash_complete=False, unchecked_bytes=200, checked_at=0.0,
                age_seconds=60.0, state="listed", exclusion="none", gap_kind="none")
    base.update(kw)
    return DuRow(**base)


def test_trash_cell_forms():
    assert trash_cell(_row(in_trash_complete=True)) == "500 B"
    assert trash_cell(_row()) == "≥ 500 B (200 B / 20% unchecked)"
    assert trash_cell(_row(unchecked_bytes=None)) == "≥ 500 B (unchecked: unknown)"


def test_trash_cell_never_claims_more_than_the_row_unchecked():
    """N7: a newer figure behind the bound can exceed the row's own size."""
    assert trash_cell(_row(unchecked_bytes=1010)) == "≥ 500 B (1000 B / 100% unchecked)"


def test_table_has_columns_and_blank_size(usage_cache, clock):
    buf = io.StringIO()
    render_table([_row(size=None, state="excluded", exclusion="freeze")],
                 Console(file=buf, width=200))
    text = buf.getvalue()
    for col in ("Name", "Size", "%Parent", "%Used", "%Plan", "Items", "In trash", "Age", "State"):
        assert col in text
    assert "excluded (freeze)" in text


def test_row_unchecked_bound_never_exceeds_the_row(usage_cache, clock, monkeypatch):
    # the folders behind a figure's bound can have been measured after the row's own size
    # (seen at "/" in a randomised check), so the row caps the bound at its size
    import dataclasses

    from protonfs.usage import trash

    eng = _engine(usage_cache, clock)
    eng.check_account(POLICY)  # as du does first: a later scoped listing must not wipe
    eng.refresh_trash(POLICY, ExcludeRules())
    real = trash.figures_for

    def inflated(engine, paths):
        return {p: dataclasses.replace(f, complete=False, unchecked_bytes=10 ** 9)
                for p, f in real(engine, paths).items()}

    monkeypatch.setattr(trash, "figures_for", inflated)
    rows = build_rows(eng, "/my-files/A", POLICY, ExcludeRules(), maxdepth=2, now=clock())
    sized = [r for r in rows if r.unchecked_bytes is not None and r.size is not None]
    assert sized, "no row carries a bound"
    assert all(r.unchecked_bytes <= r.size for r in sized), [
        (r.path, r.unchecked_bytes, r.size) for r in sized]
