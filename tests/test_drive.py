# tests/test_drive.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from protonfs.drive import DriveAuthError, DriveClient, DriveError, TransferResult


def _stub_run(stdout: str, returncode: int = 0, stderr: str = ""):
    def _run(args, capture_output, text):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    return _run


def test_version_returns_none_when_binary_missing() -> None:
    client = DriveClient(binary="/nonexistent/proton-drive")
    assert client.version() is None


def test_version_returns_stripped_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_bin = tmp_path / "proton-drive"
    fake_bin.write_text("#!/bin/sh\necho 'Proton Drive CLI cli-drive@0.4.6'\n")
    fake_bin.chmod(0o755)
    client = DriveClient(binary=str(fake_bin))
    assert client.version() == "Proton Drive CLI cli-drive@0.4.6"


def test_list_returns_parsed_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run('[{"name": {"value": "a"}}]'))
    entries = client.list("/my-files/test")
    assert entries == [{"name": {"value": "a"}}]


def test_list_raises_drive_error_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run("not json", returncode=1))
    with pytest.raises(DriveError):
        client.list("/my-files/test")


def test_upload_partial_conflict_failure_returns_transfer_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DriveClient(binary="proton-drive")
    payload = (
        '{"transferredItems":0,"skippedItems":0,"failedItems":1,'
        '"failures":[{"name":"x","error":"ValidationError: Name conflict on '
        '\\"x\\" (file) already exists"}]}'
    )
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run(payload, returncode=1))
    result = client.upload([Path("x")], "/my-files/test")
    assert isinstance(result, TransferResult)
    assert result.failed_items == 1
    assert result.failures[0]["name"] == "x"


def test_upload_hard_failure_with_no_summary_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run("", returncode=1, stderr="unauthenticated"))
    with pytest.raises(DriveAuthError):
        client.upload([Path("x")], "/my-files/test")


def test_is_authenticated_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run("[]"))
    assert client.is_authenticated() is True


def test_is_authenticated_false_on_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run("", returncode=1, stderr="unauthenticated"))
    assert client.is_authenticated() is False
