"""Render ``status.svg`` from the per-source data fragments in ``data/``.

Each workflow that has something to report writes its own rich JSON fragment under
``.github/status/data/`` (``ci.json``, ``docs.json``, ``proton_drive.json`` …); the
static project metadata lives in ``project.json``. This script is the *adapter*: it
loads whatever fragments exist, assembles the exact variable shape ``template.svg.j2``
expects, and renders. Fragments may carry far more detail than the template currently
uses (run URLs, per-version test counts, durations …) — extra keys are preserved in the
assembled model under their source name for future template extensions, but ignored by
the current template.

Design notes:
- Missing or partial fragments never crash the render: every template variable has a
  defensible default, so a first run (or a workflow that hasn't reported yet) still
  produces a valid SVG.
- The template is intentionally NOT coupled to the fragment schema — only this file is.
  Change fragment structure freely; keep the ``build_model`` output keys stable and the
  template needs no edits.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
# The header logo, embedded (not linked) so the rendered SVG is self-contained.
LOGO_PATH = ROOT.parent.parent / "docs" / "_static" / "logo.svg"


def _logo_data_uri() -> str:
    """Return the header logo as a base64 ``data:`` URI, or ``""`` if unavailable.

    GitHub serves raw SVGs under ``Content-Security-Policy: default-src 'none'``,
    which blocks every external subresource — so an ``<image href="https://…logo.svg">``
    silently fails to load when status.svg is shown via ``<img>`` in the README. Inlining
    the logo's bytes as a data URI removes the fetch entirely, so it renders under CSP.
    Base64 (not raw ``utf8``) avoids having to XML-escape the logo's ``#``/``<``/quotes.
    """
    try:
        raw = LOGO_PATH.read_bytes()
    except OSError:
        return ""
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _load(name: str) -> dict[str, Any]:
    """Load ``data/<name>.json``; return ``{}`` if it is absent or unreadable."""
    path = DATA_DIR / f"{name}.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _wrap(text: str, max_chars: int) -> list[str]:
    """Greedy word-wrap ``text`` into lines of at most ``max_chars`` characters.

    SVG ``<text>`` does not reflow, so the description must be pre-split into lines
    that the template emits as ``<tspan>``s. A long word is kept whole (placed on its
    own overflowing line) rather than hyphenated. Returns at least one line ([""] for
    empty input) so the header always has something to render.
    """
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _latest_timestamp(*fragments: dict[str, Any]) -> str:
    """The most recent ``updated`` across fragments (ISO-8601 strings sort correctly)."""
    stamps = [f["updated"] for f in fragments if isinstance(f.get("updated"), str)]
    return max(stamps) if stamps else ""


def build_model() -> dict[str, Any]:
    """Assemble the template variable model from the data fragments."""
    project = _load("project")
    ci = _load("ci")
    docs = _load("docs")
    proton_drive = _load("proton_drive")

    tests = ci.get("tests") or {}
    coverage = ci.get("coverage") or {}
    ruff = ci.get("ruff") or {}

    # Wrap the header description; the links row drops one line-height per extra line so
    # it never collides with a multi-line description. 74 chars keeps the current 137-char
    # description to two lines within the 900-wide canvas.
    description = project.get("description", "")
    description_lines = _wrap(description, 74)
    _line_height = 18

    model: dict[str, Any] = {
        "project": {
            "name": project.get("name", "protonfs"),
            "description": description,
            "version": project.get("version", "0.0.0"),
        },
        "description_lines": description_lines,
        "line_height": _line_height,
        # Links baseline: 120 for a single-line description, pushed down one line per extra.
        "link_y": 120 + (len(description_lines) - 1) * _line_height,
        "links": {
            "pypi": project.get("links", {}).get("pypi", ""),
            "docs": project.get("links", {}).get("docs", ""),
            "github": project.get("links", {}).get("github", ""),
        },
        "tests": {
            "passed": tests.get("passed", 0),
            "failed": tests.get("failed", 0),
            "skipped": tests.get("skipped", 0),
        },
        # Template does ``coverage ~ "%"`` and ``coverage > 80`` — must be a number.
        "coverage": coverage.get("line", 0),
        "ruff": {"status": ruff.get("status", "unknown")},
        "docs": {"coverage": docs.get("coverage", 0)},
        # List of {version, status}; template reads only those two fields per entry.
        "python": ci.get("python", []),
        # {os: {arch: status}}; template iterates os then arch.
        "builds": ci.get("builds", {}),
        # Not consumed by the current template, but assembled so it can be added later
        # without touching this adapter again.
        "proton_drive": {
            "pinned": proton_drive.get("pinned", ""),
            "latest": proton_drive.get("latest", ""),
        },
        "updated": _latest_timestamp(project, ci, docs, proton_drive),
        # Header logo inlined as a data URI so status.svg renders standalone on GitHub.
        "logo_href": _logo_data_uri(),
    }
    return model


def render() -> str:
    env = Environment(
        loader=FileSystemLoader(ROOT),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("template.svg.j2")
    return template.render(**build_model())


def main() -> None:
    svg = render()
    (ROOT / "status.svg").write_text(svg, encoding="utf-8")
    print("Generated status.svg")


if __name__ == "__main__":
    main()
