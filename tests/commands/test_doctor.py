# tests/commands/test_doctor.py
"""Doctor's #73 currency checks: support matrix, upstream advisory, schema/layering.

The pre-existing runtime-environment checks (session bus, Secret Service) are
exercised via tests/test_secretservice.py; these tests cover the pre-upgrade
advisor added for #73, with fakes throughout.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from protonfs.commands import doctor as doctor_mod
from protonfs.commands.doctor import (
    Check,
    doctor,
    render,
    repo_currency_checks,
    run_doctor,
    shell_exports,
    upstream_currency_check,
    version_currency_check,
)
from protonfs.commands.upgrade import upstream_ahead_message
from protonfs.config import init_config
from protonfs.index import INDEX_SCHEMA_VERSION
from protonfs.install import SUPPORTED_DRIVE_VERSIONS, highest_supported
from protonfs.secretservice import BUS_ENV, DISABLE_ENV, SecretServiceError, SecretsResult

HIGHEST = highest_supported()


class FakeVersionDrive:
    def __init__(self, installed: str | None) -> None:
        self._installed = installed

    def drive_version(self) -> str | None:
        return self._installed


# --- version vs support matrix -------------------------------------------------------


def test_version_ok_at_highest_supported() -> None:
    check = version_currency_check(FakeVersionDrive(HIGHEST))
    assert check.ok and not check.warn
    assert "highest supported" in check.detail


def test_version_warn_when_older_but_supported() -> None:
    older = next(v for v in SUPPORTED_DRIVE_VERSIONS if v != HIGHEST)
    check = version_currency_check(FakeVersionDrive(older))
    assert check.ok and check.warn
    assert "protonfs upgrade" in check.hint


def test_version_fail_when_unsupported() -> None:
    check = version_currency_check(FakeVersionDrive("0.0.1"))
    assert not check.ok
    assert "not in this protonfs release's support matrix" in check.detail


def test_version_fail_when_unparseable() -> None:
    check = version_currency_check(FakeVersionDrive(None))
    assert not check.ok
    assert "unparseable" in check.detail


# --- upstream advisory ----------------------------------------------------------------


def test_upstream_ahead_warns_with_shared_message_contract() -> None:
    check = upstream_currency_check(upstream_fetch=lambda: "9.9.9")
    assert check.ok and check.warn
    assert check.hint == upstream_ahead_message("9.9.9", HIGHEST)


def test_upstream_current_is_ok() -> None:
    check = upstream_currency_check(upstream_fetch=lambda: HIGHEST)
    assert check.ok and not check.warn


def test_upstream_offline_fails_soft() -> None:
    check = upstream_currency_check(upstream_fetch=lambda: None)
    assert check.ok and not check.warn
    assert "advisory skipped" in check.detail


# --- repo checks ----------------------------------------------------------------------


def test_repo_checks_empty_outside_protonfs_root(tmp_path: Path) -> None:
    assert repo_currency_checks(tmp_path) == []


def test_repo_checks_all_ok_on_fresh_setup(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    from protonfs.commands.setup import write_git_control_files
    from protonfs.ignore import init_ignore, init_include

    init_ignore(tmp_path)
    init_include(tmp_path)
    write_git_control_files(tmp_path)

    checks = {c.name: c for c in repo_currency_checks(tmp_path)}
    assert checks["index schema"].ok
    assert checks["repo migrations"].ok and not checks["repo migrations"].warn
    assert checks["config layering"].ok and not checks["config layering"].warn


def test_repo_checks_warn_on_stale_index_schema(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    # A v0 legacy index: bare {rel_path: entry} document with no schema_version.
    (tmp_path / ".protonfs" / "index.json").write_text(json.dumps({"a.txt": {}}))

    checks = {c.name: c for c in repo_currency_checks(tmp_path)}
    assert checks["index schema"].warn
    assert "v0 on disk" in checks["index schema"].detail


def test_repo_checks_warn_on_pending_migrations_and_layering(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    shared = tmp_path / ".protonfs" / "config.json"
    data = json.loads(shared.read_text())
    data["device_id"] = "legacy-device"
    shared.write_text(json.dumps(data))

    checks = {c.name: c for c in repo_currency_checks(tmp_path)}
    assert checks["repo migrations"].warn
    assert "device-id-to-local-config" in checks["repo migrations"].detail
    assert checks["config layering"].warn
    assert "device_id" in checks["config layering"].detail


# --- render levels ----------------------------------------------------------------------


def test_render_warn_does_not_fail_doctor() -> None:
    lines: list[str] = []
    ok = render(
        [
            Check("a", True, "fine"),
            Check("b", True, "aging", warn=True, hint="upgrade"),
        ],
        console_echo=lines.append,
    )
    assert ok is True
    assert any(line.startswith("[warn] b:") for line in lines)


def test_render_fail_still_fails() -> None:
    ok = render([Check("a", False, "broken")], console_echo=lambda _line: None)
    assert ok is False


def test_repo_checks_ok_on_current_index_schema(tmp_path: Path) -> None:
    # An index already at the current schema is reported [ok] "current", not warned --
    # covers the equal-version branch that a fresh (no-index) setup never reaches.
    init_config(tmp_path, "/my-files/test")
    (tmp_path / ".protonfs" / "index.json").write_text(
        json.dumps({"schema_version": INDEX_SCHEMA_VERSION, "entries": {}})
    )

    checks = {c.name: c for c in repo_currency_checks(tmp_path)}
    assert checks["index schema"].ok and not checks["index schema"].warn
    assert f"v{INDEX_SCHEMA_VERSION} (current)" in checks["index schema"].detail


# --- run_doctor orchestration ---------------------------------------------------------
#
# run_doctor wires together DriveClient + the whole protonfs.secretservice surface. We
# fake every collaborator in the doctor module's namespace so the test asserts doctor's
# own branching (binary state, Linux gate, disabled-keyring gate, read-only vs --fix,
# bus-resolution failure) without launching a real dbus/gnome-keyring.


class FakeDoctorDrive:
    """Stand-in for DriveClient as run_doctor uses it."""

    def __init__(
        self, *, binary_available: bool = True, version: str | None = "v0.5.0"
    ) -> None:
        self.binary = "proton-drive"
        self._binary_available = binary_available
        self._version = version

    def binary_available(self) -> bool:
        return self._binary_available

    def version(self) -> str | None:
        return self._version

    def drive_version(self) -> str | None:
        return self._version


@pytest.fixture
def stub_currency(monkeypatch: pytest.MonkeyPatch):
    """Silence the #73 version/upstream advisors (they hit install/network) so
    run_doctor tests can focus on the runtime-environment branches."""
    monkeypatch.setattr(
        doctor_mod,
        "version_currency_check",
        lambda drive: Check("proton-drive version", True, "ok"),
    )
    monkeypatch.setattr(
        doctor_mod, "upstream_currency_check", lambda: Check("upstream proton-drive", True, "ok")
    )


def _checks_by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_run_doctor_binary_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        doctor_mod, "DriveClient", lambda: FakeDoctorDrive(binary_available=False, version=None)
    )
    monkeypatch.setattr(doctor_mod, "is_linux", lambda: False)

    checks = _checks_by_name(run_doctor(root=tmp_path))
    binary = checks["proton-drive binary"]
    assert not binary.ok
    assert "not found on PATH" in binary.detail
    assert "install-drive" in binary.hint


def test_run_doctor_binary_present_but_not_runnable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Installed but `version` fails -- almost always the keyring, so doctor must NOT
    # tell the user to reinstall a binary that is sitting right there.
    monkeypatch.setattr(
        doctor_mod, "DriveClient", lambda: FakeDoctorDrive(binary_available=True, version=None)
    )
    monkeypatch.setattr(doctor_mod, "is_linux", lambda: False)

    checks = _checks_by_name(run_doctor(root=tmp_path))
    binary = checks["proton-drive binary"]
    assert not binary.ok
    assert "installed but failed to run" in binary.detail
    assert "keyring" in binary.hint.lower()


def test_run_doctor_non_linux_stops_after_platform_keyring_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_currency
) -> None:
    monkeypatch.setattr(doctor_mod, "DriveClient", lambda: FakeDoctorDrive())
    monkeypatch.setattr(doctor_mod, "is_linux", lambda: False)

    checks = _checks_by_name(run_doctor(root=tmp_path))
    assert checks["proton-drive binary"].ok
    assert "platform keychain" in checks["keyring"].detail
    # Non-Linux short-circuits: none of the Linux-only tool/bus checks run.
    assert "session bus" not in checks


def test_run_doctor_disabled_keyring_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_currency
) -> None:
    monkeypatch.setattr(doctor_mod, "DriveClient", lambda: FakeDoctorDrive())
    monkeypatch.setattr(doctor_mod, "is_linux", lambda: True)
    monkeypatch.setenv(DISABLE_ENV, "1")

    checks = _checks_by_name(run_doctor(root=tmp_path))
    assert f"{DISABLE_ENV} is set" in checks["keyring"].detail
    assert "session bus" not in checks


def test_run_doctor_read_only_reports_full_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_currency
) -> None:
    # The DISABLE_ENV autouse fixture would short-circuit the Linux path -- clear it.
    monkeypatch.delenv(DISABLE_ENV, raising=False)
    monkeypatch.setattr(doctor_mod, "DriveClient", lambda: FakeDoctorDrive())
    monkeypatch.setattr(doctor_mod, "is_linux", lambda: True)
    monkeypatch.setattr(doctor_mod, "resolve_bus", lambda env: ("unix:abstract=/tmp/bus", "found"))
    monkeypatch.setattr(doctor_mod, "secret_service_state", lambda env: "ready")
    monkeypatch.setattr(doctor_mod, "probe_secret_service", lambda env: (True, "read+write ok"))

    checks = _checks_by_name(run_doctor(fix=False, root=tmp_path))
    assert checks["session bus"].ok
    assert checks["secret service"].ok and checks["secret service"].detail == "ready"
    assert checks["keyring read/write"].ok
    assert "keyring store" in checks


def test_run_doctor_read_only_bus_resolution_failure_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_currency
) -> None:
    monkeypatch.delenv(DISABLE_ENV, raising=False)
    monkeypatch.setattr(doctor_mod, "DriveClient", lambda: FakeDoctorDrive())
    monkeypatch.setattr(doctor_mod, "is_linux", lambda: True)

    def _no_bus(env):
        raise SecretServiceError("no session bus and cannot start one")

    monkeypatch.setattr(doctor_mod, "resolve_bus", _no_bus)

    checks = _checks_by_name(run_doctor(fix=False, root=tmp_path))
    assert not checks["session bus"].ok
    assert "no session bus" in checks["session bus"].detail
    # Failed bus resolution stops before the secret-service checks.
    assert "secret service" not in checks


def test_run_doctor_fix_bootstraps_keyring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_currency
) -> None:
    monkeypatch.delenv(DISABLE_ENV, raising=False)
    monkeypatch.setattr(doctor_mod, "DriveClient", lambda: FakeDoctorDrive())
    monkeypatch.setattr(doctor_mod, "is_linux", lambda: True)
    monkeypatch.setattr(
        doctor_mod,
        "ensure_secret_service",
        lambda env: SecretsResult(
            env={**env, BUS_ENV: "unix:abstract=/tmp/fixed"},
            ready=True,
            actions=["started gnome-keyring"],
            warnings=["using an isolated protonfs keyring"],
        ),
    )
    monkeypatch.setattr(doctor_mod, "secret_service_state", lambda env: "ready")
    monkeypatch.setattr(doctor_mod, "probe_secret_service", lambda env: (True, "ok"))

    checks = _checks_by_name(run_doctor(fix=True, root=tmp_path))
    assert checks["session bus"].detail == "unix:abstract=/tmp/fixed"
    assert checks["secret service"].ok


def test_run_doctor_fix_bootstrap_failure_reports_and_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_currency
) -> None:
    monkeypatch.delenv(DISABLE_ENV, raising=False)
    monkeypatch.setattr(doctor_mod, "DriveClient", lambda: FakeDoctorDrive())
    monkeypatch.setattr(doctor_mod, "is_linux", lambda: True)

    def _boom(env):
        raise SecretServiceError("could not start gnome-keyring")

    monkeypatch.setattr(doctor_mod, "ensure_secret_service", _boom)

    checks = _checks_by_name(run_doctor(fix=True, root=tmp_path))
    assert not checks["keyring bootstrap"].ok
    assert "gnome-keyring" in checks["keyring bootstrap"].detail
    assert "secret service" not in checks


# --- doctor() + shell_exports() -------------------------------------------------------


def test_doctor_prints_success_and_returns_true(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(doctor_mod, "run_doctor", lambda fix: [Check("a", True, "fine")])
    monkeypatch.setattr(doctor_mod.click, "echo", lambda msg="": lines.append(msg))

    assert doctor(fix=False) is True
    assert any("This host can run proton-drive" in line for line in lines)


def test_doctor_failure_suggests_fix_when_not_fixing(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(doctor_mod, "run_doctor", lambda fix: [Check("a", False, "broken")])
    monkeypatch.setattr(doctor_mod.click, "echo", lambda msg="": lines.append(msg))

    assert doctor(fix=False) is False
    assert any("doctor --fix" in line for line in lines)


def test_shell_exports_emits_bus_line_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod, "drive_env", lambda: {BUS_ENV: "unix:abstract=/tmp/bus"}
    )
    assert shell_exports() == [f"{BUS_ENV}=unix:abstract=/tmp/bus"]


def test_shell_exports_empty_when_no_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_mod, "drive_env", lambda: {})
    assert shell_exports() == []
