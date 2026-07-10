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
    # D5.2: use the real `filesystem list` name shape {"ok": true, "value": ...}
    # (v0.1 used a simplified {"value": ...}).
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(
        subprocess,
        "run",
        _stub_run('[{"name": {"ok": true, "value": "a"}, "type": "file", "totalStorageSize": 5}]'),
    )
    entries = client.list("/my-files/test")
    assert entries == [
        {"name": {"ok": True, "value": "a"}, "type": "file", "totalStorageSize": 5}
    ]


def test_run_json_auth_signal_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # D5.1: a genuine auth signal ("not logged in") surfaces as DriveAuthError.
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(
        subprocess, "run", _stub_run('{"error": "not logged in"}', returncode=1)
    )
    with pytest.raises(DriveAuthError):
        client.list("/my-files/test")


def test_run_json_non_auth_error_not_misclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    # D5.1: the tightened check must NOT treat "unauthorized" (a permission/quota
    # error, contains the substring "auth") as an auth failure -- the false positive
    # the old broad `"auth" in message` check produced.
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(
        subprocess,
        "run",
        _stub_run('{"error": "unauthorized: quota exceeded"}', returncode=1),
    )
    with pytest.raises(DriveError) as excinfo:
        client.list("/my-files/test")
    assert not isinstance(excinfo.value, DriveAuthError)


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


def test_walk_flattens_nested_tree(monkeypatch):
    client = DriveClient(binary="proton-drive")

    tree = {
        "/root": [
            {"name": {"ok": True, "value": "run1"}, "type": "folder"},
            {"name": {"ok": True, "value": "top.txt"}, "type": "file", "totalStorageSize": 5},
        ],
        "/root/run1": [
            {"name": {"ok": True, "value": "dump_0001"}, "type": "file", "totalStorageSize": 100},
            {"name": {"ok": True, "value": "dump_0002"}, "type": "file", "totalStorageSize": 200},
        ],
    }
    monkeypatch.setattr(client, "list", lambda path: tree[path])

    entries = client.walk("/root")
    by_rel = {e.rel_path: e for e in entries}

    assert by_rel["top.txt"].is_dir is False
    assert by_rel["top.txt"].size == 5
    assert by_rel["run1"].is_dir is True
    assert by_rel["run1/dump_0001"].size == 100
    assert by_rel["run1/dump_0002"].size == 200
    # every file's rel_path is relative to root, POSIX, no leading slash
    assert all(not e.rel_path.startswith("/") for e in entries)


def test_walk_skips_undecryptable_names(monkeypatch):
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr(
        client,
        "list",
        lambda path: [
            {"name": {"ok": True, "value": "good"}, "type": "file", "totalStorageSize": 1},
            {"name": {"ok": False}, "type": "file", "totalStorageSize": 9},
        ],
    )
    entries = client.walk("/root")
    assert [e.rel_path for e in entries] == ["good"]


def test_walk_logs_warning_for_undecryptable_names(monkeypatch, caplog):
    import logging

    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr(
        client,
        "list",
        lambda path: [{"name": {"ok": False}, "type": "file", "totalStorageSize": 9}],
    )
    with caplog.at_level(logging.WARNING):
        client.walk("/root")
    assert any("undecryptable" in r.message for r in caplog.records)


def test_walk_empty_remote(monkeypatch):
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr(client, "list", lambda path: [])
    assert client.walk("/root") == []
