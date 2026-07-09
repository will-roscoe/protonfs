from click.testing import CliRunner

from protonfs.cli import main


def test_help() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Sync a local directory tree with Proton Drive" in result.output


def test_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "protonfs" in result.output


def test_unimplemented_subcommands_fail_cleanly() -> None:
    runner = CliRunner()
    for subcommand in ["push", "pull"]:
        result = runner.invoke(main, [subcommand])
        assert result.exit_code != 0
        assert "not yet implemented" in result.output
