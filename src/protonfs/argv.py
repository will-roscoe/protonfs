# src/protonfs/argv.py
"""Make flags position-independent on the ``protonfs`` command line.

Click's canonical form is ``protonfs [GLOBAL-OPTS] SUBCOMMAND [SUB-OPTS/ARGS]``. Users
reasonably type ``protonfs push -v mload001`` (a global flag after the subcommand) or
``protonfs --dry-run push`` (a subcommand flag before it). :func:`reorder_argv` rewrites
either into the canonical order before Click parses, so both work; the canonical form is
unchanged and remains what the docs show.

The global flags are a fixed set and -- critically -- none of them take a value, which is
what makes the rewrite unambiguous: any recognised global token can be hoisted to the
front, and the first remaining token that names a subcommand becomes the subcommand.

.. versionadded:: 1.4.0
"""
from __future__ import annotations

import re

# The group-level (global) flags, accepted in any position. None takes a value.
GLOBAL_FLAG_NAMES = frozenset(
    {
        "--verbose",
        "--progress-inline",
        "--progress-lines",
        "--event-log",
        "--no-event-log",
    }
)
_STACKED_VERBOSE = re.compile(r"-v+\Z")  # -v, -vv, -vvv, -vvvv


def is_global_flag(token: str) -> bool:
    """Whether ``token`` is one of the position-independent global flags."""
    return token in GLOBAL_FLAG_NAMES or bool(_STACKED_VERBOSE.match(token))


def reorder_argv(args: list[str], command_names: frozenset[str]) -> list[str]:
    """Rewrite ``args`` into canonical ``[globals] subcommand [rest]`` order.

    :param args: the raw argument list after the program name.
    :param command_names: the registered top-level subcommand names.
    :returns: a reordered copy; unchanged when there is no subcommand present (e.g.
        ``--help`` or a bare invocation) so Click's own handling is untouched.

    .. versionadded:: 1.4.0
    """
    hoisted = [t for t in args if is_global_flag(t)]
    non_global = [t for t in args if not is_global_flag(t)]

    sub_index = next((i for i, t in enumerate(non_global) if t in command_names), None)
    if sub_index is None:
        return list(args)  # no subcommand to anchor on -- leave it for Click

    subcommand = non_global[sub_index]
    rest = non_global[:sub_index] + non_global[sub_index + 1 :]
    return hoisted + [subcommand] + rest
