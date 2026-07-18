"""Tests for `protonfs completions` (shell completion generation + install/uninstall)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from protonfs.cli import main
from protonfs.commands import completions as C


@pytest.mark.parametrize(
    "shell,needle",
    [("bash", "_protonfs_completion"), ("zsh", "#compdef protonfs"), ("fish", "complete")],
)
def test_completion_script_nonempty_per_shell(shell, needle):
    script = C.completion_script(shell)
    assert script.strip()
    assert "_PROTONFS_COMPLETE" in script
    assert needle in script


def test_completion_script_unknown_shell():
    with pytest.raises(ValueError):
        C.completion_script("tcsh")


def test_install_creates_script_and_marker(tmp_path):
    p = C.install_completion("bash", home=tmp_path)
    assert p == tmp_path / ".local/share/protonfs/completion.bash"
    assert p.read_text().strip()
    rc = (tmp_path / ".bashrc").read_text()
    assert C.MARKER_BEGIN in rc and C.MARKER_END in rc
    assert str(p) in rc
    assert C.is_installed("bash", home=tmp_path)


def test_install_is_idempotent(tmp_path):
    C.install_completion("bash", home=tmp_path)
    C.install_completion("bash", home=tmp_path)
    rc = (tmp_path / ".bashrc").read_text()
    assert rc.count(C.MARKER_BEGIN) == 1


def test_fish_install_needs_no_rc(tmp_path):
    p = C.install_completion("fish", home=tmp_path)
    assert p == tmp_path / ".config/fish/completions/protonfs.fish"
    assert p.exists()
    assert not (tmp_path / ".config/fish/config.fish").exists()


def test_uninstall_removes_script_and_marker(tmp_path):
    C.install_completion("bash", home=tmp_path)
    assert C.uninstall_completion("bash", home=tmp_path) is True
    assert not (tmp_path / ".local/share/protonfs/completion.bash").exists()
    assert C.MARKER_BEGIN not in (tmp_path / ".bashrc").read_text()
    assert C.uninstall_completion("bash", home=tmp_path) is False


def test_refresh_only_touches_installed(tmp_path):
    C.install_completion("zsh", home=tmp_path)
    assert C.refresh_installed(home=tmp_path) == ["zsh"]
    assert C.refresh_installed(home=tmp_path) == ["zsh"]


def test_targets_rejects_unknown_shell():
    with pytest.raises(ValueError):
        C._targets("tcsh", None)


def test_install_preserves_rc_without_trailing_newline(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text("export FOO=1")  # no trailing newline
    C.install_completion("bash", home=tmp_path)
    text = rc.read_text()
    assert "export FOO=1\n" in text  # a newline was inserted before the marker block
    assert text.count(C.MARKER_BEGIN) == 1


def test_is_installed_true_for_fish(tmp_path):
    C.install_completion("fish", home=tmp_path)
    assert C.is_installed("fish", home=tmp_path) is True
    assert C.is_installed("bash", home=tmp_path) is False


# --- CLI wiring (Task 3) ---


def test_cli_completions_prints_script():
    r = CliRunner().invoke(main, ["completions", "bash"])
    assert r.exit_code == 0
    assert "_PROTONFS_COMPLETE" in r.output


def test_cli_completions_install(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    r = CliRunner().invoke(main, ["completions", "bash", "--install"])
    assert r.exit_code == 0
    assert (tmp_path / ".local/share/protonfs/completion.bash").exists()


def test_cli_completions_install_and_uninstall_mutually_exclusive():
    r = CliRunner().invoke(main, ["completions", "bash", "--install", "--uninstall"])
    assert r.exit_code != 0


def test_cli_completions_rejects_unknown_shell():
    r = CliRunner().invoke(main, ["completions", "tcsh"])
    assert r.exit_code != 0


def test_cli_completions_uninstall(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    CliRunner().invoke(main, ["completions", "bash", "--install"])
    r = CliRunner().invoke(main, ["completions", "bash", "--uninstall"])
    assert r.exit_code == 0
    assert "Removed completion." in r.output
    # uninstalling again reports nothing was installed
    r2 = CliRunner().invoke(main, ["completions", "bash", "--uninstall"])
    assert "No completion was installed." in r2.output


# --- upgrade refresh (Task 4) ---


def test_upgrade_refreshes_installed_completions(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    C.install_completion("bash", home=tmp_path)
    script = tmp_path / ".local/share/protonfs/completion.bash"
    script.write_text("STALE")
    from protonfs.commands.upgrade import refresh_completions_step

    assert refresh_completions_step() == ["bash"]
    assert script.read_text() != "STALE"


def test_upgrade_refresh_noop_when_none_installed(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from protonfs.commands.upgrade import refresh_completions_step

    assert refresh_completions_step() == []


# --- position-independent global-flag completion (Task 5) ---


def _sub_ctx(command_name):
    import click

    root = click.Context(main, info_name="protonfs")
    cmd = main.commands[command_name]
    return cmd, click.Context(cmd, parent=root, info_name=command_name)


def test_global_flags_complete_after_subcommand():
    push, ctx = _sub_ctx("push")
    vals = {i.value for i in push.shell_complete(ctx, "--")}
    # global flags offered in post-subcommand position...
    assert {"--event-log", "--progress-inline"} <= vals
    # ...alongside the command's own options
    assert "--dry-run" in vals


def test_global_flag_prefix_filter_after_subcommand():
    push, ctx = _sub_ctx("push")
    vals = {i.value for i in push.shell_complete(ctx, "--eve")}
    assert vals == {"--event-log"}
