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
