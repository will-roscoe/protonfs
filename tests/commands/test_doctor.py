# tests/commands/test_doctor.py
"""Doctor's #73 currency checks: support matrix, upstream advisory, schema/layering.

The pre-existing runtime-environment checks (session bus, Secret Service) are
exercised via tests/test_secretservice.py; these tests cover the pre-upgrade
advisor added for #73, with fakes throughout.
"""
from __future__ import annotations

import json
from pathlib import Path

from protonfs.commands.doctor import (
    Check,
    render,
    repo_currency_checks,
    upstream_currency_check,
    version_currency_check,
)
from protonfs.commands.upgrade import upstream_ahead_message
from protonfs.config import init_config
from protonfs.install import SUPPORTED_DRIVE_VERSIONS, highest_supported

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
