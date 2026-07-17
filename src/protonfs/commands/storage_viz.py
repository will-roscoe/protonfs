# src/protonfs/commands/storage_viz.py
"""Terminal storage-usage visuals for ``protonfs ls --dirs --visual`` (#94).

Two chart types over the per-directory :class:`~protonfs.commands.ls.DirSummary`
aggregates:

* **waffle** — a fixed grid whose cells are handed out in proportion to each
  directory's share of total storage; easy to eyeball "this dir is ~40%".
* **treemap** — a squarified treemap (Bruls/Huizing/van Wijk): nested rectangles
  whose *areas* are proportional to size and whose aspect ratios are kept as close
  to square as possible, so the biggest directories read as the biggest blocks.

Everything renders with :mod:`rich`'s coloured backgrounds -- no image library and
no new dependency. The geometry (:func:`squarify`) is a pure function so it can be
tested without touching the terminal.
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.style import Style
from rich.text import Text

# A palette of distinguishable rich colours cycled across directories. When there are
# more directories than colours, the smallest are lumped into a single "(other)" slice
# (see :func:`prepare_slices`) so a colour never stands for two different directories.
PALETTE = (
    "bright_blue",
    "bright_green",
    "bright_magenta",
    "bright_cyan",
    "bright_yellow",
    "bright_red",
    "blue",
    "green",
    "magenta",
    "cyan",
    "yellow",
    "red",
)
OTHER_COLOR = "grey50"
OTHER_LABEL = "(other)"


@dataclass
class Slice:
    """One directory's contribution to a storage chart.

    :ivar label: the directory name (or ``"(other)"`` for the lumped remainder).
    :ivar value: the size in bytes this slice represents.
    :ivar color: the rich colour name used for this slice's cells/blocks and legend.
    """

    label: str
    value: int
    color: str


def prepare_slices(
    items: list[tuple[str, int]], max_slices: int = len(PALETTE)
) -> list[Slice]:
    """Turn ``(label, size)`` pairs into coloured :class:`Slice` s, largest first.

    Zero-size entries are dropped (nothing to draw). When more non-zero directories
    remain than ``max_slices``, the smallest are merged into one ``"(other)"`` slice
    so every colour maps to exactly one legend entry.

    :param items: ``(directory_label, size_in_bytes)`` pairs.
    :param max_slices: maximum number of individually-coloured slices before the
        remainder is lumped into ``"(other)"``.
    :returns: slices sorted by size descending; empty when every size is zero.
    """
    ranked = sorted((it for it in items if it[1] > 0), key=lambda it: it[1], reverse=True)
    if not ranked:
        return []
    if len(ranked) <= max_slices:
        head, tail = ranked, []
    else:
        # Keep max_slices-1 individually so there is room for the "(other)" slice.
        head, tail = ranked[: max_slices - 1], ranked[max_slices - 1 :]
    slices = [
        Slice(label, value, PALETTE[i % len(PALETTE)])
        for i, (label, value) in enumerate(head)
    ]
    if tail:
        slices.append(Slice(OTHER_LABEL, sum(v for _, v in tail), OTHER_COLOR))
    return slices


def _human(n: int) -> str:
    """Local binary-size formatter (kept independent of ls.human_size to avoid a cycle)."""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover


def _legend(slices: list[Slice], total: int) -> Text:
    """A colour-swatch legend: ``██ dir  1.2 MiB  (34.5%)`` per slice."""
    text = Text()
    for s in slices:
        pct = (s.value / total * 100) if total else 0.0
        text.append("██", style=Style(color=s.color))
        text.append(f" {s.label}  {_human(s.value)}  ({pct:.1f}%)\n")
    return text


# --- waffle ---------------------------------------------------------------------------


def waffle_counts(values: list[int], total_cells: int) -> list[int]:
    """Apportion ``total_cells`` grid cells across ``values`` by largest-remainder.

    Every non-zero value gets at least one cell (so a small-but-present directory
    never disappears), then the rest are handed out by proportional share with the
    leftover going to the largest fractional remainders -- the cell counts always sum
    to exactly ``total_cells``.
    """
    total = sum(values)
    if total <= 0 or total_cells <= 0:
        return [0 for _ in values]
    exact = [v / total * total_cells for v in values]
    counts = [max(1, int(e)) if v > 0 else 0 for e, v in zip(exact, values)]
    # Reconcile to exactly total_cells: trim from / add to the largest remainders.
    diff = total_cells - sum(counts)
    order = sorted(range(len(values)), key=lambda i: exact[i] - int(exact[i]), reverse=True)
    order = [i for i in order if values[i] > 0]
    idx = 0
    while diff != 0 and order:
        i = order[idx % len(order)]
        if diff > 0:
            counts[i] += 1
            diff -= 1
        elif counts[i] > 1:
            counts[i] -= 1
            diff += 1
        idx += 1
    return counts


def render_waffle(
    slices: list[Slice], console: Console, *, cols: int = 25, rows: int = 8
) -> None:
    """Render a ``rows``×``cols`` waffle grid plus a legend to ``console``."""
    total = sum(s.value for s in slices)
    if total <= 0:
        console.print("(no storage to chart)")
        return
    counts = waffle_counts([s.value for s in slices], cols * rows)
    # Flat list of one colour per cell, in slice order.
    cells: list[str] = []
    for s, count in zip(slices, counts):
        cells.extend([s.color] * count)
    cells = cells[: cols * rows]

    for r in range(rows):
        line = Text()
        row_cells = cells[r * cols : (r + 1) * cols]
        if not row_cells:
            break
        for color in row_cells:
            line.append("█", style=Style(color=color))
        console.print(line)
    console.print()
    console.print(_legend(slices, total))


# --- treemap --------------------------------------------------------------------------


@dataclass
class Rect:
    """An axis-aligned rectangle in canvas (character-cell) coordinates.

    :ivar x: left edge, :ivar y: top edge, :ivar dx: width, :ivar dy: height (floats;
        the renderer rounds them to cell boundaries).
    """

    x: float
    y: float
    dx: float
    dy: float


def _layout_row(sizes: list[float], x: float, y: float, dy: float) -> list[Rect]:
    width = sum(sizes) / dy
    rects, cursor = [], y
    for size in sizes:
        rects.append(Rect(x, cursor, width, size / width))
        cursor += size / width
    return rects


def _layout_col(sizes: list[float], x: float, y: float, dx: float) -> list[Rect]:
    height = sum(sizes) / dx
    rects, cursor = [], x
    for size in sizes:
        rects.append(Rect(cursor, y, size / height, height))
        cursor += size / height
    return rects


def _layout(sizes: list[float], x: float, y: float, dx: float, dy: float) -> list[Rect]:
    return _layout_row(sizes, x, y, dy) if dx >= dy else _layout_col(sizes, x, y, dx)


def _worst_ratio(sizes: list[float], x: float, y: float, dx: float, dy: float) -> float:
    return max(max(r.dx / r.dy, r.dy / r.dx) for r in _layout(sizes, x, y, dx, dy))


def squarify(values: list[float], x: float, y: float, dx: float, dy: float) -> list[Rect]:
    """Squarified treemap layout (Bruls/Huizing/van Wijk).

    Places one rectangle per value inside the region ``(x, y, dx, dy)`` such that each
    rectangle's *area* is proportional to its value and aspect ratios are kept as close
    to square as the algorithm can manage. ``values`` should be positive; callers pass
    them largest-first for the best-looking result.

    :returns: one :class:`Rect` per input value, in input order, tiling the region.
    """
    sizes = [float(v) for v in values]
    if not sizes:
        return []
    total = sum(sizes)
    if total <= 0:
        return []
    # Scale the raw values so their sum equals the region's area (dx*dy).
    scale = (dx * dy) / total
    scaled = [s * scale for s in sizes]
    return _squarify_scaled(scaled, x, y, dx, dy)


def _squarify_scaled(sizes: list[float], x: float, y: float, dx: float, dy: float) -> list[Rect]:
    if not sizes:
        return []
    if len(sizes) == 1:
        return _layout(sizes, x, y, dx, dy)
    # Grow the current row while it keeps aspect ratios from getting worse.
    i = 1
    while i < len(sizes) and _worst_ratio(sizes[:i], x, y, dx, dy) >= _worst_ratio(
        sizes[: i + 1], x, y, dx, dy
    ):
        i += 1
    current, remaining = sizes[:i], sizes[i:]
    placed = _layout(current, x, y, dx, dy)
    covered = sum(current)
    if dx >= dy:
        width = covered / dy
        rest = _squarify_scaled(remaining, x + width, y, dx - width, dy)
    else:
        height = covered / dx
        rest = _squarify_scaled(remaining, x, y + height, dx, dy - height)
    return placed + rest


def render_treemap(
    slices: list[Slice], console: Console, *, width: int = 60, height: int = 20
) -> None:
    """Render a squarified treemap of ``slices`` (plus a legend) to ``console``."""
    total = sum(s.value for s in slices)
    if total <= 0:
        console.print("(no storage to chart)")
        return
    rects = squarify([s.value for s in slices], 0, 0, width, height)

    # Paint a colour grid, then overlay each rect's label at its top-left when it fits.
    grid = [[None] * width for _ in range(height)]  # type: list[list[int | None]]
    chars = [[" "] * width for _ in range(height)]
    for idx, rect in enumerate(rects):
        x0, y0 = int(round(rect.x)), int(round(rect.y))
        x1, y1 = int(round(rect.x + rect.dx)), int(round(rect.y + rect.dy))
        for row in range(max(0, y0), min(height, y1)):
            for col in range(max(0, x0), min(width, x1)):
                grid[row][col] = idx
        label = f"{slices[idx].label} {_human(slices[idx].value)}"
        if (x1 - x0) >= 3 and (y1 - y0) >= 1 and 0 <= y0 < height:
            for offset, ch in enumerate(label[: max(0, x1 - x0 - 1)]):
                if x0 + 1 + offset < width:
                    chars[y0][x0 + 1 + offset] = ch

    for row in range(height):
        line = Text()
        for col in range(width):
            idx = grid[row][col]
            if idx is None:
                line.append(" ")
            else:
                color = slices[idx].color
                # Label chars ride on the slice colour with a contrasting foreground.
                line.append(chars[row][col], style=Style(bgcolor=color, color="black"))
        console.print(line)
    console.print()
    console.print(_legend(slices, total))


def render_storage_visual(
    kind: str, items: list[tuple[str, int]], console: Console
) -> None:
    """Render ``kind`` ('waffle' | 'treemap') for ``(label, size)`` items.

    Slices are prepared once (largest-first, overflow lumped into ``(other)``) and
    handed to the matching renderer. Prints a friendly note when there is nothing to
    chart (no tracked storage under the listed path).
    """
    slices = prepare_slices(items)
    if not slices:
        console.print("(no storage to chart)")
        return
    if kind == "waffle":
        render_waffle(slices, console)
    else:
        render_treemap(slices, console)
