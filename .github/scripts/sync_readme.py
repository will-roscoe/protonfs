#!/usr/bin/env python3
"""Keep README.md's overview in sync with the docs homepage's shared source.

``docs/_shared/overview.rst`` is the single source of truth for the project overview;
the docs homepage includes it verbatim (``.. include::``) and this script renders it to
Markdown and injects it into README.md between the sync markers::

    <!-- SYNC:overview START - generated from docs/_shared/overview.rst, do not edit here -->
    ...generated markdown...
    <!-- SYNC:overview END -->

CLI:
    sync_readme.py --check   # exit 1 if README's block is stale (used in CI)
    sync_readme.py --write   # regenerate README's block in place

Only the constrained rST subset the fragment is written in is supported: section
headers (``===``/``---`` underlines), paragraphs, ``- `` bullet lists, ``inline
literals``, ``text <url>`_`` links, ``**bold**``, and ``.. code-block:: LANG`` blocks.
Comment lines (``.. `` with no directive) are dropped.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FRAGMENT = _REPO_ROOT / "docs" / "_shared" / "overview.rst"
_README = _REPO_ROOT / "README.md"

_START = "<!-- SYNC:overview START - generated from docs/_shared/overview.rst, do not edit here -->"
_END = "<!-- SYNC:overview END -->"

_INLINE_LITERAL = re.compile(r"``([^`]+)``")
_LINK = re.compile(r"`([^`<]+?) <([^>]+)>`_")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _inline(text: str) -> str:
    """Convert rST inline markup in a single line to Markdown."""
    text = _LINK.sub(r"[\1](\2)", text)
    text = _INLINE_LITERAL.sub(r"`\1`", text)
    text = _BOLD.sub(r"**\1**", text)
    return text


def rst_to_markdown(rst: str) -> str:
    """Render the constrained-subset rST ``rst`` fragment to Markdown.

    :param rst: the fragment source (as in ``docs/_shared/overview.rst``).
    :returns: the equivalent Markdown.
    """
    lines = rst.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # Comment / directive-comment lines: skip (including their indented body).
        if line.startswith(".. ") and not line.startswith(".. code-block::"):
            i += 1
            while i < n and (lines[i].startswith("   ") or not lines[i].strip()):
                # stop the skip at a blank line that is followed by a non-indented line
                if not lines[i].strip():
                    nxt = lines[i + 1] if i + 1 < n else ""
                    if not nxt.startswith("   "):
                        break
                i += 1
            continue

        # Code block.
        if line.startswith(".. code-block::"):
            lang = line.split("::", 1)[1].strip()
            i += 1
            while i < n and not lines[i].strip():
                i += 1
            body: list[str] = []
            while i < n and (lines[i].startswith("   ") or not lines[i].strip()):
                body.append(lines[i][3:] if lines[i].startswith("   ") else "")
                i += 1
            while body and not body[-1].strip():
                body.pop()
            out.append(f"```{lang}")
            out.extend(body)
            out.append("```")
            out.append("")
            continue

        # Section header: a line followed by an underline of === or ---.
        if i + 1 < n and re.fullmatch(r"[=\-]{3,}", lines[i + 1] or ""):
            level = "##" if lines[i + 1][0] == "=" else "###"
            out.append(f"{level} {_inline(line.strip())}")
            out.append("")
            i += 2
            continue

        # Bullet list item (may wrap onto indented continuation lines).
        if re.match(r"- ", line):
            item = _inline(line[2:].strip())
            i += 1
            while i < n and lines[i].startswith("  ") and lines[i].strip():
                item += " " + _inline(lines[i].strip())
                i += 1
            out.append(f"- {item}")
            continue

        # Blank line.
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue

        # Ordinary paragraph line (join wrapped lines into one Markdown paragraph).
        para = [_inline(line.strip())]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"- ", lines[i]) \
                and not lines[i].startswith(".. ") \
                and not (i + 1 < n and re.fullmatch(r"[=\-]{3,}", lines[i + 1] or "")):
            para.append(_inline(lines[i].strip()))
            i += 1
        out.append(" ".join(para))
        out.append("")

    # Collapse trailing blanks to a single newline-terminated block.
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def render_block() -> str:
    """Return the full marker-delimited README block from the current fragment."""
    md = rst_to_markdown(_FRAGMENT.read_text(encoding="utf-8"))
    return f"{_START}\n\n{md}\n{_END}"


def _replace_block(readme: str, block: str) -> str:
    start = readme.index(_START)
    end = readme.index(_END) + len(_END)
    return readme[:start] + block + readme[end:]


def _main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("--check", "--write"):
        print("usage: sync_readme.py --check | --write", file=sys.stderr)
        return 2
    readme = _README.read_text(encoding="utf-8")
    if _START not in readme or _END not in readme:
        print(f"README.md is missing the sync markers ({_START!r} / {_END!r}).", file=sys.stderr)
        return 2
    block = render_block()
    updated = _replace_block(readme, block)

    if argv[0] == "--write":
        if updated != readme:
            _README.write_text(updated, encoding="utf-8")
            print("README.md overview block regenerated from docs/_shared/overview.rst.")
        else:
            print("README.md already in sync.")
        return 0

    # --check
    if updated != readme:
        print(
            "README.md overview block is out of sync with docs/_shared/overview.rst.\n"
            "Run: python .github/scripts/sync_readme.py --write",
            file=sys.stderr,
        )
        return 1
    print("README.md overview block is in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
