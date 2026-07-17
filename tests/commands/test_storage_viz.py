# tests/commands/test_storage_viz.py
"""Storage-usage visuals for `ls --dirs --visual` (#94): slice prep, waffle
apportionment, squarified-treemap geometry, and rendering smoke tests."""
from __future__ import annotations

import io

from rich.console import Console

from protonfs.commands.storage_viz import (
    OTHER_LABEL,
    PALETTE,
    Rect,
    prepare_slices,
    render_storage_visual,
    render_treemap,
    render_waffle,
    squarify,
    waffle_counts,
)

# --- prepare_slices -------------------------------------------------------------------


def test_prepare_slices_sorts_desc_and_drops_zeroes() -> None:
    slices = prepare_slices([("a", 10), ("b", 0), ("c", 30), ("d", 20)])
    assert [(s.label, s.value) for s in slices] == [("c", 30), ("d", 20), ("a", 10)]
    # each gets a distinct palette colour, cycled in order
    assert slices[0].color == PALETTE[0] and slices[1].color == PALETTE[1]


def test_prepare_slices_empty_when_all_zero() -> None:
    assert prepare_slices([("a", 0), ("b", 0)]) == []


def test_prepare_slices_lumps_overflow_into_other() -> None:
    items = [(f"d{i}", i + 1) for i in range(20)]  # 20 dirs, palette is smaller
    slices = prepare_slices(items, max_slices=4)

    assert len(slices) == 4
    assert slices[-1].label == OTHER_LABEL
    # the "(other)" slice carries the summed remainder, so the total is conserved
    assert sum(s.value for s in slices) == sum(v for _, v in items)


# --- waffle_counts --------------------------------------------------------------------


def test_waffle_counts_sum_to_total_cells() -> None:
    counts = waffle_counts([48, 32, 13, 7], 100)
    assert sum(counts) == 100
    # monotonic with size, and roughly proportional
    assert counts[0] > counts[1] > counts[2] > counts[3]
    assert counts[0] == 48 or abs(counts[0] - 48) <= 1


def test_waffle_counts_small_nonzero_gets_at_least_one_cell() -> None:
    # A directory that is a rounding-error fraction of the whole still shows up.
    counts = waffle_counts([10_000, 1], 100)
    assert counts[1] >= 1
    assert sum(counts) == 100


def test_waffle_counts_all_zero_is_all_zero() -> None:
    assert waffle_counts([0, 0], 100) == [0, 0]


# --- squarify geometry ----------------------------------------------------------------


def _area(r: Rect) -> float:
    return r.dx * r.dy


def test_squarify_areas_are_proportional_to_values() -> None:
    values = [50.0, 30.0, 20.0]
    rects = squarify(values, 0, 0, 100, 100)  # region area 10_000
    total_area = 100 * 100
    for value, rect in zip(values, rects):
        expected = value / sum(values) * total_area
        assert abs(_area(rect) - expected) / expected < 1e-6


def test_squarify_rectangles_stay_within_region() -> None:
    rects = squarify([5, 3, 2, 1, 1], 0, 0, 60, 20)
    for r in rects:
        assert r.x >= -1e-9 and r.y >= -1e-9
        assert r.x + r.dx <= 60 + 1e-6
        assert r.y + r.dy <= 20 + 1e-6


def test_squarify_edge_cases() -> None:
    assert squarify([], 0, 0, 10, 10) == []
    assert squarify([0, 0], 0, 0, 10, 10) == []
    one = squarify([7], 0, 0, 10, 4)
    assert len(one) == 1 and abs(_area(one[0]) - 40) < 1e-6


# --- rendering smoke tests ------------------------------------------------------------


def _render(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=True)
    fn(*args, console, **kwargs)
    return buf.getvalue()


def test_render_waffle_emits_grid_and_legend() -> None:
    slices = prepare_slices([("big", 80), ("small", 20)])
    out = _render(render_waffle, slices, cols=10, rows=4)
    assert "█" in out  # cells drawn
    assert "big" in out and "small" in out  # legend present
    assert "80.0%" in out or "80" in out


def test_render_treemap_emits_blocks_and_labels() -> None:
    slices = prepare_slices([("alpha", 70), ("beta", 30)])
    out = _render(render_treemap, slices, width=40, height=10)
    assert "alpha" in out  # label overlaid on the biggest rect
    assert "beta" in out


def test_render_storage_visual_handles_empty() -> None:
    out = _render(render_storage_visual, "treemap", [("a", 0)])
    assert "no storage to chart" in out


def test_render_storage_visual_dispatches_by_kind() -> None:
    waffle = _render(render_storage_visual, "waffle", [("a", 3), ("b", 1)])
    treemap = _render(render_storage_visual, "treemap", [("a", 3), ("b", 1)])
    # both draw something and label both dirs; they are not identical renderings
    assert "a" in waffle and "b" in treemap
    assert waffle != treemap
