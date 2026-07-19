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
