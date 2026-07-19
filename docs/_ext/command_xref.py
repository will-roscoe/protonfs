"""Sphinx extension: auto-link cross-page ``protonfs <subcommand>`` mentions.

Rewrites plain-text and inline-literal mentions of a command (e.g. ``protonfs
--dry-run push``) on pages *other than the command reference itself* into ``:ref:``
cross-references to that command's ``.. _cmd-*:`` anchor. Shell-example blocks
(``literal_block``) are left untouched.
"""
from __future__ import annotations

import re


def build_target_map() -> dict[str, str]:
    """Map subcommand phrases to their ``.. _cmd-*:`` ref labels, from the live app."""
    from protonfs.cli import main

    target_map: dict[str, str] = {}
    for name, cmd in main.commands.items():
        target_map[name] = f"cmd-{name}"
        subcommands = getattr(cmd, "commands", None)
        if subcommands:
            for sub_name in subcommands:
                target_map[f"{name} {sub_name}"] = f"cmd-{name}-{sub_name}"
    return target_map


_FLAGS = r"(?:\s+-{1,2}[A-Za-z][\w-]*)*"


def _compile(target_map: dict[str, str]) -> re.Pattern[str]:
    # Longest phrase first so "trash list" wins over "trash".
    phrases = sorted(target_map, key=len, reverse=True)
    alternation = "|".join(re.escape(p) for p in phrases)
    return re.compile(rf"\bprotonfs{_FLAGS}\s+({alternation})\b")


def find_command_spans(
    text: str, target_map: dict[str, str]
) -> list[tuple[int, int, str]]:
    """Return non-overlapping ``(start, end, label)`` matches in ``text``."""
    pattern = _compile(target_map)
    return [
        (m.start(), m.end(), target_map[m.group(1)]) for m in pattern.finditer(text)
    ]


# docutils/sphinx are imported lazily inside the functions below, not at module level:
# this module's pure helpers (build_target_map, find_command_spans) must be importable in
# the plain test environment (the `.[dev]` extra has no docutils/sphinx), while the
# doctree transform only ever runs inside Sphinx, where both are always present.

_REFERENCE_DOCNAME = "reference/index"


def _under(node, types: tuple[type, ...]) -> bool:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, types):
            return True
        parent = parent.parent
    return False


def _make_xref(label: str, text: str, *, literal: bool):
    from docutils import nodes
    from sphinx import addnodes

    ref = addnodes.pending_xref(
        "",
        refdomain="std",
        reftype="ref",
        reftarget=label,
        refexplicit=True,
        refwarn=True,
    )
    ref += nodes.literal(text, text) if literal else nodes.inline(text, text)
    return ref


def process_command_xrefs(app, doctree) -> None:
    """Rewrite cross-page ``protonfs <subcommand>`` mentions into ``:ref:`` xrefs."""
    if app.env.docname == _REFERENCE_DOCNAME:
        return
    from docutils import nodes
    from sphinx import addnodes

    # Text inside these nodes is never linkified: shell examples, already-linked spans,
    # and (during the plain-text pass) inline literals, which the literal pass handles.
    skip_parents = (
        nodes.literal_block,
        nodes.doctest_block,
        nodes.FixedTextElement,
        nodes.reference,
        addnodes.pending_xref,
        nodes.comment,
    )
    target_map = build_target_map()

    # Pass 1: whole inline literals that are exactly a command phrase.
    for literal in list(doctree.findall(nodes.literal)):
        if _under(literal, skip_parents):
            continue
        spans = find_command_spans(literal.astext(), target_map)
        if len(spans) == 1 and spans[0][0] == 0 and spans[0][1] == len(literal.astext()):
            literal.replace_self(_make_xref(spans[0][2], literal.astext(), literal=True))

    # Pass 2: plain text nodes (skip literals — handled above — and skip blocks).
    for text_node in list(doctree.findall(nodes.Text)):
        if _under(text_node, skip_parents + (nodes.literal,)):
            continue
        source = text_node.astext()
        spans = find_command_spans(source, target_map)
        if not spans:
            continue
        new_nodes = []
        pos = 0
        for start, end, label in spans:
            if start > pos:
                new_nodes.append(nodes.Text(source[pos:start]))
            new_nodes.append(_make_xref(label, source[start:end], literal=False))
            pos = end
        if pos < len(source):
            new_nodes.append(nodes.Text(source[pos:]))
        text_node.parent.replace(text_node, new_nodes)


def setup(app):
    app.connect("doctree-read", process_command_xrefs)
    return {
        "version": "1.0",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
