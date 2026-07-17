# tests/commands/test_upgrade.py
"""`protonfs upgrade` (#66): policy around install_drive + migrations (#67)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import click
import pytest

from protonfs.commands.upgrade import (
    run_upgrade,
    upstream_ahead_message,
    upstream_stable_version,
)
from protonfs.config import init_config
from protonfs.install import InstallResult, highest_supported

HIGHEST = highest_supported()


class FakeVersionClient:
    def __init__(self, installed: str | None, authed: bool = True) -> None:
        self._installed = installed
        self._authed = authed

    def drive_version(self) -> str | None:
        return self._installed

    def is_authenticated(self) -> bool:
        return self._authed


class FakeInstaller:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def __call__(self, version: str | None = None) -> InstallResult:
        self.calls.append(version)
        return InstallResult(
            path=Path("/fake/proton-drive"), on_path=True, sha512="0" * 128
        )


def _run(root: Path, capsys, **kwargs) -> tuple[int, str]:
    code = run_upgrade(root, **kwargs)
    return code, capsys.readouterr().out


# --- --check exit codes ------------------------------------------------------------


def test_check_exit_0_when_fully_current(tmp_path: Path, capsys) -> None:
    code, out = _run(
        tmp_path,
        capsys,
        check=True,
        client=FakeVersionClient(HIGHEST),
        installer=FakeInstaller(),
        upstream_fetch=lambda: HIGHEST,
    )
    assert code == 0
    assert "proton-drive is current" in out


def test_check_exit_1_when_binary_outdated_and_nothing_installed(
    tmp_path: Path, capsys
) -> None:
    installer = FakeInstaller()
    code, out = _run(
        tmp_path,
        capsys,
        check=True,
        client=FakeVersionClient("0.4.6"),
        installer=installer,
        upstream_fetch=lambda: None,
    )
    assert code == 1
    assert f"would upgrade proton-drive to {HIGHEST}" in out
    assert installer.calls == []  # --check never installs


# --- binary upgrade ---------------------------------------------------------------


def test_outdated_binary_upgraded_to_highest_supported(tmp_path: Path, capsys) -> None:
    installer = FakeInstaller()
    code, out = _run(
        tmp_path,
        capsys,
        client=FakeVersionClient("0.4.6"),
        installer=installer,
        upstream_fetch=lambda: None,
    )
    assert code == 0
    assert installer.calls == [HIGHEST]
    assert "session: still authenticated" in out


def test_missing_binary_installed(tmp_path: Path, capsys) -> None:
    installer = FakeInstaller()
    code, out = _run(
        tmp_path,
        capsys,
        client=FakeVersionClient(None),
        installer=installer,
        upstream_fetch=lambda: None,
    )
    assert installer.calls == [HIGHEST]
    assert "not installed" in out


def test_lost_session_reported_after_swap(tmp_path: Path, capsys) -> None:
    code, out = _run(
        tmp_path,
        capsys,
        client=FakeVersionClient("0.4.6", authed=False),
        installer=FakeInstaller(),
        upstream_fetch=lambda: None,
    )
    assert "NOT authenticated after the upgrade" in out
    assert "protonfs auth login" in out


def test_newer_than_supported_binary_left_alone(tmp_path: Path, capsys) -> None:
    installer = FakeInstaller()
    code, out = _run(
        tmp_path,
        capsys,
        client=FakeVersionClient("9.9.9"),
        installer=installer,
        upstream_fetch=lambda: None,
    )
    assert installer.calls == []
    assert "leaving it in place" in out


# --- upstream advisory -------------------------------------------------------------


def test_upstream_ahead_advisory_printed_but_not_installed(tmp_path: Path, capsys) -> None:
    installer = FakeInstaller()
    code, out = _run(
        tmp_path,
        capsys,
        client=FakeVersionClient(HIGHEST),
        installer=installer,
        upstream_fetch=lambda: "9.9.9",
    )
    assert installer.calls == []
    assert upstream_ahead_message("9.9.9", HIGHEST) in out
    assert "upgrade protonfs to get 9.9.9" in out


def test_offline_upstream_fails_soft(tmp_path: Path, capsys) -> None:
    # Offline (fetch -> None): no advisory, and the pinned upgrade still happens.
    installer = FakeInstaller()
    code, out = _run(
        tmp_path,
        capsys,
        client=FakeVersionClient("0.4.6"),
        installer=installer,
        upstream_fetch=lambda: None,
    )
    assert installer.calls == [HIGHEST]
    assert "upstream" not in out


def test_upstream_stable_version_fails_soft_on_network_error() -> None:
    def broken_opener(url):
        raise OSError("offline")

    assert upstream_stable_version(opener=broken_opener) is None


def test_upstream_stable_version_parses_manifest() -> None:
    manifest = {"Releases": [{"CategoryName": "Stable", "Version": "0.5.0"}]}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    assert (
        upstream_stable_version(opener=lambda url: _Resp(json.dumps(manifest).encode()))
        == "0.5.0"
    )


# --- migrations --------------------------------------------------------------------


def _old_layout_repo(tmp_path: Path) -> Path:
    """A repo with a pending migration: device_id still in the shared config."""
    init_config(tmp_path, "/my-files/test")
    shared = tmp_path / ".protonfs" / "config.json"
    data = json.loads(shared.read_text())
    data["device_id"] = "legacy-device"
    shared.write_text(json.dumps(data))
    return tmp_path


def test_migrations_run_inside_root(tmp_path: Path, capsys) -> None:
    root = _old_layout_repo(tmp_path)
    code, out = _run(
        root,
        capsys,
        client=FakeVersionClient(HIGHEST),
        installer=FakeInstaller(),
        upstream_fetch=lambda: None,
    )
    assert code == 0
    assert "migrations applied" in out
    assert "device_id" not in json.loads((root / ".protonfs" / "config.json").read_text())


def test_check_lists_migrations_without_applying(tmp_path: Path, capsys) -> None:
    root = _old_layout_repo(tmp_path)
    code, out = _run(
        root,
        capsys,
        check=True,
        client=FakeVersionClient(HIGHEST),
        installer=FakeInstaller(),
        upstream_fetch=lambda: None,
    )
    assert code == 1
    assert "would apply" in out
    # Nothing changed on disk.
    assert "device_id" in json.loads((root / ".protonfs" / "config.json").read_text())


def test_drive_only_skips_migrations(tmp_path: Path, capsys) -> None:
    root = _old_layout_repo(tmp_path)
    code, out = _run(
        root,
        capsys,
        drive_only=True,
        client=FakeVersionClient(HIGHEST),
        installer=FakeInstaller(),
        upstream_fetch=lambda: None,
    )
    assert "repo state" not in out
    assert "device_id" in json.loads((root / ".protonfs" / "config.json").read_text())


def test_repo_only_skips_binary(tmp_path: Path, capsys) -> None:
    root = _old_layout_repo(tmp_path)
    installer = FakeInstaller()
    code, out = _run(
        root,
        capsys,
        repo_only=True,
        client=FakeVersionClient("0.4.6"),
        installer=installer,
        upstream_fetch=lambda: pytest.fail("upstream must not be fetched with --repo-only"),
    )
    assert installer.calls == []
    assert "proton-drive" not in out
    assert "migrations applied" in out


def test_repo_only_outside_root_errors(tmp_path: Path, capsys) -> None:
    with pytest.raises(click.ClickException, match="not inside a protonfs root"):
        run_upgrade(
            tmp_path,
            repo_only=True,
            client=FakeVersionClient(HIGHEST),
            installer=FakeInstaller(),
            upstream_fetch=lambda: None,
        )


def test_outside_root_without_flags_skips_migrations(tmp_path: Path, capsys) -> None:
    code, out = _run(
        tmp_path,
        capsys,
        client=FakeVersionClient(HIGHEST),
        installer=FakeInstaller(),
        upstream_fetch=lambda: None,
    )
    assert code == 0
    assert "skipping migrations" in out


# --- CLI wiring --------------------------------------------------------------------


def test_cli_mutually_exclusive_flags_usage_error() -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    result = CliRunner().invoke(main, ["upgrade", "--drive-only", "--repo-only"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_run_upgrade_narrates_steps_through_reporter(
    tmp_path: Path, capsys, recording_reporter_cls
) -> None:
    root = _old_layout_repo(tmp_path)
    reporter = recording_reporter_cls()
    run_upgrade(
        root,
        client=FakeVersionClient("0.4.6"),
        installer=FakeInstaller(),
        upstream_fetch=lambda: None,
        reporter=reporter,
    )
    phases = [name for kind, name in reporter.calls if kind == "phase"]
    assert phases == ["checking proton-drive version", "running repo migrations"]
    assert ("done", "upgrade complete") in reporter.calls
