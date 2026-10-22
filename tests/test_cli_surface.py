# tests/test_cli_surface.py
"""Freeze test for the CLI surface (#12, M4.1).

The command set, every option/argument name per command, the summary-line keys and
the ``--format json`` keys are documented as the stable 3.x contract in
``docs/stability.rst``. These tests assert the *exact* sets -- not subsets -- so an
accidental addition, removal, or rename fails CI here, pointing back at that doc,
rather than silently shipping a breaking change. A removal or rename needs 4.0.0.

If you are here because this test just failed: either you made an unintentional
change (revert it), or you're intentionally growing/changing the CLI surface, in
which case update this test AND ``docs/stability.rst`` together, and treat it per
the versioning policy documented there (a removal/rename is breaking; a new
optional flag or new command is additive).
"""
from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from protonfs.cli import main

# Exact registered top-level commands (including `auth` and the `config` group, but
# not `config`'s own subcommands -- those are asserted separately below).
EXPECTED_TOP_LEVEL_COMMANDS = frozenset(
    {
        "setup",
        "deinit",
        "status",
        "ls",
        "push",
        "pull",
        "offload",
        "prune",
        "rm",
        "restore",
        "refresh",
        "install-drive",
        "upgrade",
        "doctor",
        "shell-init",
        "completions",
        "schedule",
        "auth",
        "config",
        "trash",
        "verify",
    }
)

EXPECTED_CONFIG_SUBCOMMANDS = frozenset({"get", "set"})
EXPECTED_TRASH_SUBCOMMANDS = frozenset({"list", "empty"})

# For each command: the exact set of option flag-strings (as passed to `click.option`,
# e.g. "-r"/"--recursive") and argument names, keyed by command name (dotted for a
# subcommand of `config`).
EXPECTED_OPTIONS: dict[str, frozenset[str]] = {
    "setup": frozenset({"--dry-run", "--migrate-lfs", "--no-migrate-lfs"}),
    "deinit": frozenset({"--dry-run", "--yes"}),
    "status": frozenset({"--format", "--remote"}),
    "ls": frozenset({"--remote", "--trash", "--dirs", "--state", "--format", "--visual"}),
    "push": frozenset(
        {"--resolve", "--dry-run", "--strict", "--min-age", "--deletions", "--allow-mass-delete"}
    ),
    "pull": frozenset(
        {"--resolve", "--dry-run", "--refresh", "--strict", "--no-restore"}
    ),
    "offload": frozenset({"--no-verify", "--min-age", "--dry-run", "--yes"}),
    "prune": frozenset({"--keep", "--min-age", "--no-push", "--dry-run", "--yes"}),
    "rm": frozenset({"-r", "--recursive", "-f", "--force", "--yes"}),
    "restore": frozenset(),
    "refresh": frozenset({"--prune"}),
    "install-drive": frozenset({"--version", "--skip-keyring"}),
    "upgrade": frozenset({"--check", "--drive-only", "--repo-only"}),
    "doctor": frozenset({"--fix"}),
    "shell-init": frozenset(),
    "completions": frozenset({"--install", "--uninstall"}),
    "schedule": frozenset(
        {
            "--list", "--add", "--uninstall", "-U", "--all", "--every", "--cron",
            "--at", "--command", "--path", "--resolve", "--strict", "--min-age", "--keep",
            "--deletions", "--no-restore", "--label", "--json",
        }
    ),
    "auth": frozenset(),
    "config.get": frozenset(),
    "config.set": frozenset({"--global", "--local"}),
    "trash.list": frozenset(),
    "trash.empty": frozenset({"--yes"}),
    "verify": frozenset({"--repair", "--index"}),
}

EXPECTED_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "setup": (),
    "deinit": (),
    "status": ("path",),
    "ls": ("path",),
    "push": ("path",),
    "pull": ("path",),
    "offload": ("path",),
    "prune": ("path",),
    "rm": ("path",),
    "restore": ("path",),
    "refresh": ("path",),
    "install-drive": (),
    "upgrade": (),
    "doctor": (),
    "shell-init": (),
    "completions": ("shell",),
    "schedule": (),
    "auth": ("action",),
    "config.get": ("key",),
    "config.set": ("key", "value"),
    "trash.list": (),
    "trash.empty": (),
    "verify": (),
}


def _leaf_commands() -> dict[str, click.Command]:
    """Flatten `main`'s command tree into {name or "group.sub": Command}."""
    leaves: dict[str, click.Command] = {}
    for name, cmd in main.commands.items():
        if isinstance(cmd, click.Group):
            for sub_name, sub_cmd in cmd.commands.items():
                leaves[f"{name}.{sub_name}"] = sub_cmd
        else:
            leaves[name] = cmd
    return leaves


def test_top_level_command_set_is_frozen() -> None:
    assert set(main.commands.keys()) == EXPECTED_TOP_LEVEL_COMMANDS, (
        "Registered top-level commands changed -- see docs/stability.rst. A removal "
        "or rename is a breaking change (major bump post-1.0); a new command is additive."
    )


def test_config_subcommand_set_is_frozen() -> None:
    config_group = main.commands["config"]
    assert isinstance(config_group, click.Group)
    assert set(config_group.commands.keys()) == EXPECTED_CONFIG_SUBCOMMANDS


def test_trash_subcommand_set_is_frozen() -> None:
    trash_group = main.commands["trash"]
    assert isinstance(trash_group, click.Group)
    assert set(trash_group.commands.keys()) == EXPECTED_TRASH_SUBCOMMANDS


@pytest.mark.parametrize("dotted_name", sorted(EXPECTED_OPTIONS))
def test_command_option_surface_is_frozen(dotted_name: str) -> None:
    cmd = _leaf_commands()[dotted_name]
    actual_opts: set[str] = set()
    for param in cmd.params:
        if isinstance(param, click.Option):
            actual_opts.update(param.opts)
            actual_opts.update(param.secondary_opts)
    assert actual_opts == set(EXPECTED_OPTIONS[dotted_name]), (
        f"Option surface of `{dotted_name.replace('.', ' ')}` changed -- see "
        "docs/stability.rst."
    )


@pytest.mark.parametrize("dotted_name", sorted(EXPECTED_ARGUMENTS))
def test_command_argument_surface_is_frozen(dotted_name: str) -> None:
    cmd = _leaf_commands()[dotted_name]
    actual_args = tuple(
        param.name for param in cmd.params if isinstance(param, click.Argument)
    )
    assert actual_args == EXPECTED_ARGUMENTS[dotted_name], (
        f"Argument surface of `{dotted_name.replace('.', ' ')}` changed -- see "
        "docs/stability.rst."
    )


def test_every_leaf_command_is_covered_by_the_frozen_tables() -> None:
    """Guards the guard: a new command/subcommand must be added to the tables above
    (and to docs/stability.rst), not silently pass by being absent from them."""
    leaves = set(_leaf_commands().keys())
    assert leaves == set(EXPECTED_OPTIONS.keys())
    assert leaves == set(EXPECTED_ARGUMENTS.keys())


# -- Exit-code sample: a representative usage error and a representative DriveError
# path, per the contract documented in docs/stability.rst. Full per-command exit-code
# coverage for the success/failure paths lives in tests/commands/test_*.py and
# tests/commands/test_cli_errors.py; this just anchors the two universal cases.


def test_usage_error_exits_2() -> None:
    # `rm` requires PATH; omitting it is a Click usage error.
    result = CliRunner().invoke(main, ["rm"])
    assert result.exit_code == 2


def test_unknown_command_exits_2() -> None:
    result = CliRunner().invoke(main, ["not-a-real-command"])
    assert result.exit_code == 2


def test_drive_error_path_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    from protonfs.drive import DriveError

    def _raise(*args, **kwargs):
        raise DriveError("boom")

    monkeypatch.setattr("protonfs.context.load_context", _raise)

    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 1


def test_group_level_global_options_are_frozen() -> None:
    """The `main` group's global flags (-v, progress style, event log) are a frozen set."""
    opts = set()
    for p in main.params:
        if isinstance(p, click.Option):
            opts.update(p.opts)
            opts.update(p.secondary_opts)
    assert {
        "-v", "--verbose", "--progress-inline", "--progress-lines",
        "--event-log", "--no-event-log",
    } <= opts


# --- the output contract: summary-line keys (3.x) ---------------------------------------
#
# Each transfer command prints one `[<label>: ]key=value ...` summary line on stdout. The
# key names are part of the 3.x contract (docs/stability.rst, "Output contract"): within
# 3.x a key is never renamed, removed or given a new meaning. Optional keys print only
# when non-zero, so every stub below sets every counter non-zero. Adding a key is
# additive for callers (they ignore unknown keys) but must update this set and the doc
# together.

EXPECTED_SUMMARY_KEYS = {
    "push": frozenset(
        {"transferred", "adopted", "moved", "trashed", "local-deleted", "skipped", "failed",
         "unsettled"}
    ),
    "pull": frozenset({"transferred", "skipped", "failed", "local-deleted"}),
    "offload": frozenset(
        {"offloaded", "bytes_reclaimed", "skipped_unverified", "skipped_modified",
         "skipped_unsettled"}
    ),
    "offload --dry-run": frozenset(
        {"would offload", "bytes_reclaimed", "skipped_unverified", "skipped_modified",
         "skipped_unsettled"}
    ),
    "prune pushed:": frozenset({"transferred", "skipped", "failed"}),
}

_SUMMARY_MSG = (
    "summary keys changed -- update docs/stability.rst 'Output contract' and this set "
    "together"
)


def _summary_keys(line: str) -> set[str]:
    """The key names on one summary line, without an optional ``<label>: `` prefix.

    ``would offload=N`` (offload's dry-run form) is one key, ``would offload``.
    """
    label, sep, rest = line.partition(": ")
    body = rest if sep and "=" not in label else line
    keys = set()
    if body.startswith("would offload="):
        keys.add("would offload")
        body = body[len("would offload="):]
    keys |= {token.split("=", 1)[0] for token in body.split() if "=" in token}
    return keys


def _summary_line(output: str, marker: str) -> str:
    for line in output.splitlines():
        if line.startswith(marker) or f" {marker}" in line:
            return line
    raise AssertionError(f"no summary line containing {marker!r} in:\n{output}")


def _store(tmp_path, monkeypatch):
    from protonfs.config import init_config
    from protonfs.context import load_context

    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    return ctx


def _all_counters_transfer():
    from protonfs.drive import TransferResult

    return TransferResult(
        1, 2, 3, [], adopted_items=4, unsettled_items=5, local_deleted_items=6,
        moved_items=7, trashed_items=8, trashed=["t"],
    )


def _all_counters_offload():
    from protonfs.commands.offload import OffloadResult

    return OffloadResult(
        offloaded=1, skipped_unverified=2, skipped_modified=3, bytes_reclaimed=4,
        skipped_unsettled=5,
    )


def test_push_summary_key_set_is_frozen(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "protonfs.commands.push.push", lambda *a, **k: _all_counters_transfer()
    )
    out = CliRunner().invoke(main, ["push"]).output
    assert _summary_keys(_summary_line(out, "transferred=")) == EXPECTED_SUMMARY_KEYS[
        "push"
    ], _SUMMARY_MSG


def test_pull_summary_key_set_is_frozen(tmp_path, monkeypatch) -> None:
    from protonfs.index import IndexEntry

    ctx = _store(tmp_path, monkeypatch)
    ctx.index.set("a.bin", IndexEntry(1, 0.0, "", "", "/my-files/test/a.bin", "d", "present", ""))
    monkeypatch.setattr(
        "protonfs.commands.pull.pull", lambda *a, **k: _all_counters_transfer()
    )
    out = CliRunner().invoke(main, ["pull"]).output
    assert _summary_keys(_summary_line(out, "transferred=")) == EXPECTED_SUMMARY_KEYS[
        "pull"
    ], _SUMMARY_MSG


@pytest.mark.parametrize(
    ("args", "key", "marker"),
    [
        (["offload", "--yes"], "offload", "offloaded="),
        (["offload", "--dry-run"], "offload --dry-run", "would offload="),
    ],
)
def test_offload_summary_key_set_is_frozen(tmp_path, monkeypatch, args, key, marker) -> None:
    _store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "protonfs.commands.offload.offload", lambda *a, **k: _all_counters_offload()
    )
    out = CliRunner().invoke(main, args).output
    assert _summary_keys(_summary_line(out, marker)) == EXPECTED_SUMMARY_KEYS[key], (
        _SUMMARY_MSG
    )


def test_prune_pushed_line_key_set_is_frozen(tmp_path, monkeypatch) -> None:
    from protonfs.commands.prune import PruneResult

    _store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "protonfs.commands.prune.prune",
        lambda *a, **k: PruneResult(pushed=_all_counters_transfer(), considered=1),
    )
    out = CliRunner().invoke(main, ["prune", "--yes"]).output
    assert _summary_keys(_summary_line(out, "pushed: ")) == EXPECTED_SUMMARY_KEYS[
        "prune pushed:"
    ], _SUMMARY_MSG


def _summary_emitters() -> dict[str, int]:
    """``{function or command name: count}`` of ``key={...}`` f-strings in cli.py.

    Scans module-level functions and every Click command's callback (Click replaces the
    decorated function with a Command, so a plain function scan misses them).
    """
    import inspect
    import re

    import protonfs.cli as cli

    pattern = re.compile(r'f"[^"\n]*?[a-z_-]+=\{')
    funcs = {
        name: fn for name, fn in inspect.getmembers(cli, inspect.isfunction)
        if fn.__module__ == cli.__name__
    }
    for name, cmd in _leaf_commands().items():
        if cmd.callback is not None and cmd.callback.__module__ == cli.__name__:
            funcs[name] = cmd.callback
    emitters = {}
    for name, fn in funcs.items():
        hits = pattern.findall(inspect.getsource(inspect.unwrap(fn)))
        if hits:
            emitters[name] = len(hits)
    return emitters


def test_no_other_command_prints_a_key_value_summary() -> None:
    """Every ``key={...}`` summary emitter in cli.py is one the tests above cover; a new
    emitter fails here, pointing at the output contract."""
    covered = {"push", "pull", "prune", "_echo_offload_result"}
    emitters = _summary_emitters()
    assert {"push", "pull", "prune"} <= set(emitters), emitters  # the scan sees commands
    assert set(emitters) <= covered, (
        f"new key=value summary emitter(s) {sorted(set(emitters) - covered)} -- add them "
        "to EXPECTED_SUMMARY_KEYS and docs/stability.rst 'Output contract'"
    )
