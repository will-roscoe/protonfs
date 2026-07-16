# tests/test_cli_surface.py
"""Freeze test for the CLI surface (#12, M4.1).

The command set, and every option/argument name per command, is documented as a
stable 1.0 contract in ``docs/stability.rst``. This test asserts the *exact* set --
not a subset -- so an accidental addition, removal, or rename of a command/option
fails CI here, pointing back at that doc, rather than silently shipping a breaking
change with no major-version bump.

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
        "rm",
        "restore",
        "refresh",
        "install-drive",
        "upgrade",
        "doctor",
        "shell-init",
        "auth",
        "config",
        "trash",
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
    "status": frozenset({"--format"}),
    "ls": frozenset({"--remote", "--trash", "--dirs", "--state", "--format"}),
    "push": frozenset({"--resolve", "--dry-run"}),
    "pull": frozenset({"--resolve", "--dry-run", "--refresh"}),
    "offload": frozenset({"--no-verify", "--dry-run", "--yes"}),
    "rm": frozenset({"-r", "--recursive", "-f", "--force", "--yes"}),
    "restore": frozenset(),
    "refresh": frozenset({"--prune"}),
    "install-drive": frozenset({"--version", "--skip-keyring"}),
    "upgrade": frozenset({"--check", "--drive-only", "--repo-only"}),
    "doctor": frozenset({"--fix"}),
    "shell-init": frozenset(),
    "auth": frozenset(),
    "config.get": frozenset(),
    "config.set": frozenset({"--global", "--local"}),
    "trash.list": frozenset(),
    "trash.empty": frozenset({"--yes"}),
}

EXPECTED_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "setup": (),
    "deinit": (),
    "status": ("path",),
    "ls": ("path",),
    "push": ("path",),
    "pull": ("path",),
    "offload": ("path",),
    "rm": ("path",),
    "restore": ("path",),
    "refresh": ("path",),
    "install-drive": (),
    "upgrade": (),
    "doctor": (),
    "shell-init": (),
    "auth": ("action",),
    "config.get": ("key",),
    "config.set": ("key", "value"),
    "trash.list": (),
    "trash.empty": (),
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
