# tests/test_drive.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from protonfs.drive import (
    DriveAuthError,
    DriveClient,
    DriveError,
    DriveThrottleError,
    TransferResult,
)


def _stub_run(stdout: str, returncode: int = 0, stderr: str = ""):
    # **kwargs: DriveClient passes env= (the keyring-bootstrapped environment) to
    # every proton-drive invocation.
    def _run(args, capture_output, text, **kwargs):
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


def test_remote_identities_parses_claimed_fields_and_skips_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #22: remote_identities reads the PLAINTEXT claimedSize / claimedDigests.sha1, ignores
    # folders, and drops entries whose name could not be decrypted.
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    payload = (
        "["
        '{"name": {"ok": true, "value": "f1"}, "type": "file", "claimedSize": 100,'
        ' "claimedDigests": {"sha1": "aa"}, "totalStorageSize": 128},'
        '{"name": {"ok": true, "value": "sub"}, "type": "folder"},'
        '{"name": {"ok": false, "value": ""}, "type": "file", "claimedSize": 7},'
        '{"name": {"ok": true, "value": "f2"}, "type": "file", "claimedSize": 200}'
        "]"
    )
    monkeypatch.setattr(subprocess, "run", _stub_run(payload))

    identities = client.remote_identities("/my-files/test")

    assert set(identities) == {"f1", "f2"}  # folder + undecryptable entry excluded
    assert identities["f1"].claimed_size == 100  # plaintext size, not totalStorageSize (128)
    assert identities["f1"].sha1 == "aa"
    assert identities["f2"].claimed_size == 200
    assert identities["f2"].sha1 is None


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
    monkeypatch.setattr(client, "list", lambda path, timeout=None: tree[path])

    entries = client.walk("/root")
    by_rel = {e.rel_path: e for e in entries}

    assert by_rel["top.txt"].is_dir is False
    assert by_rel["top.txt"].size == 5
    assert by_rel["run1"].is_dir is True
    assert by_rel["run1/dump_0001"].size == 100
    assert by_rel["run1/dump_0002"].size == 200
    # every file's rel_path is relative to root, POSIX, no leading slash
    assert all(not e.rel_path.startswith("/") for e in entries)


def test_walk_surfaces_plaintext_claimed_identity(monkeypatch):
    client = DriveClient(binary="proton-drive")
    tree = {
        "/root": [
            {
                "name": {"ok": True, "value": "f.txt"},
                "type": "file",
                "totalStorageSize": 13,  # encrypted size, runs larger than plaintext
                "claimedSize": 10,  # plaintext size
                "claimedDigests": {"sha1": "abc123"},
            },
        ],
    }
    monkeypatch.setattr(client, "list", lambda path, timeout=None: tree[path])

    entry = client.walk("/root")[0]
    assert entry.size == 13  # encrypted size preserved for backwards-compat
    assert entry.claimed_size == 10
    assert entry.sha1 == "abc123"


def test_walk_skips_undecryptable_names(monkeypatch):
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr(
        client,
        "list",
        lambda path, timeout=None: [
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
        lambda path, timeout=None: [{"name": {"ok": False}, "type": "file", "totalStorageSize": 9}],
    )
    with caplog.at_level(logging.WARNING):
        client.walk("/root")
    assert any("undecryptable" in r.message for r in caplog.records)


def test_walk_empty_remote(monkeypatch):
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr(client, "list", lambda path, timeout=None: [])
    assert client.walk("/root") == []


def test_keyring_failure_is_not_misreported_as_an_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exo2 regression: proton-drive's keyring errors carry no auth wording, so the
    old classifier fell through to a bare DriveError -- and is_authenticated() turned
    that into "not logged in", sending the user to `auth login`, which completes the
    browser flow and then dies at the same locked collection."""
    from protonfs.drive import DriveSecretsError

    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(
        subprocess,
        "run",
        _stub_run(
            "",
            returncode=1,
            stderr=(
                "Failed to load session from secrets (ensure you have secrets available): "
                "Cannot autolaunch D-Bus without X11 $DISPLAY"
            ),
        ),
    )

    with pytest.raises(DriveSecretsError, match="doctor"):
        client.list("/")


def test_locked_collection_error_is_classified_as_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protonfs.drive import DriveSecretsError

    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(
        subprocess,
        "run",
        _stub_run(
            "",
            returncode=1,
            stderr="Cannot create an item in a locked collection (code: 2)",
        ),
    )

    with pytest.raises(DriveSecretsError):
        client.list("/")


def test_is_authenticated_propagates_secrets_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from protonfs.drive import DriveSecretsError

    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(
        subprocess, "run", _stub_run("", returncode=1, stderr="ERR_SECRETS_PLATFORM_ERROR")
    )

    with pytest.raises(DriveSecretsError):
        client.is_authenticated()


# --- #33: throttle-resilient listing --------------------------------------------------


def _client(monkeypatch: pytest.MonkeyPatch) -> DriveClient:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    return client


def test_list_with_backoff_retries_on_timeout_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    calls = {"n": 0}

    def flaky(path, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise subprocess.TimeoutExpired(cmd="proton-drive", timeout=timeout)
        return [{"name": {"ok": True, "value": "a"}, "type": "file"}]

    monkeypatch.setattr(client, "list", flaky)
    slept: list[float] = []
    out = client.list_with_backoff("/x", base_delay=1, sleep=slept.append)

    assert calls["n"] == 3
    assert out == [{"name": {"ok": True, "value": "a"}, "type": "file"}]
    assert slept == [1, 2]  # exponential backoff between attempts


def test_list_with_backoff_raises_throttle_error_after_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    def always_timeout(path, timeout=None):
        raise subprocess.TimeoutExpired(cmd="proton-drive", timeout=timeout)

    monkeypatch.setattr(client, "list", always_timeout)
    with pytest.raises(DriveThrottleError):
        client.list_with_backoff("/x", retries=2, base_delay=0.01, sleep=lambda _: None)


def test_list_with_backoff_does_not_retry_a_non_throttle_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    calls = {"n": 0}

    def permanent_failure(path, timeout=None):
        calls["n"] += 1
        raise DriveError("no such remote path")

    monkeypatch.setattr(client, "list", permanent_failure)
    with pytest.raises(DriveError) as excinfo:
        client.list_with_backoff("/x", sleep=lambda _: None)

    assert not isinstance(excinfo.value, DriveThrottleError)
    assert calls["n"] == 1  # raised immediately, never retried


def test_list_with_backoff_caps_the_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def always_timeout(path, timeout=None):
        raise subprocess.TimeoutExpired(cmd="proton-drive", timeout=timeout)

    monkeypatch.setattr(client, "list", always_timeout)
    slept: list[float] = []
    with pytest.raises(DriveThrottleError):
        client.list_with_backoff(
            "/x", retries=5, base_delay=10, cap=15, sleep=slept.append
        )

    assert slept == [10, 15, 15, 15, 15]  # 10, 20->15, 40->15, ... capped at 15


def test_walk_invokes_on_directory_per_directory_with_file_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    tree = {
        "/root": [
            {"name": {"ok": True, "value": "sub"}, "type": "folder"},
            {"name": {"ok": True, "value": "top.txt"}, "type": "file", "totalStorageSize": 3},
        ],
        "/root/sub": [
            {"name": {"ok": True, "value": "inner.txt"}, "type": "file", "totalStorageSize": 7},
        ],
    }
    monkeypatch.setattr(client, "list", lambda path, timeout=None: tree[path])

    seen: list[list[str]] = []
    client.walk("/root", on_directory=lambda files: seen.append([f.rel_path for f in files]))

    # One callback per directory, each carrying only that directory's file entries.
    assert seen == [["top.txt"], ["sub/inner.txt"]]


# --- restore against proton-drive >= 0.5.0 trash semantics (#56) ------------

def _stub_run_seq(responses: list[tuple[str, int]]):
    """Sequential stub: each call consumes the next (stdout, returncode) pair and
    records the argv it was invoked with."""
    calls: list[list[str]] = []

    def _run(args, capture_output, text, **kwargs):
        calls.append(list(args))
        stdout, returncode = responses[len(calls) - 1]
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    _run.calls = calls
    return _run


def _trash_entry(name: str, uid: str, parent_uid: str) -> str:
    return (
        f'{{"uid": "{uid}", "parentUid": "{parent_uid}", '
        f'"name": {{"ok": true, "value": "{name}"}}}}'
    )


def test_restore_original_path_form_used_when_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 0.4.6 accepts the original path directly: no fallback, single invocation.
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run = _stub_run_seq([('[{"uid": "u1", "ok": true}]', 0)])
    monkeypatch.setattr(subprocess, "run", run)

    result = client.restore(["/my-files/test/a.bin"])

    assert result == [{"uid": "u1", "ok": True}]
    assert len(run.calls) == 1
    assert run.calls[0][1:4] == ["filesystem", "restore", "/my-files/test/a.bin"]


def test_restore_falls_back_to_trash_name_on_0_5_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 0.5.0 rejects the original path -> translate to /trash/<name>, verify uid.
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run = _stub_run_seq(
        [
            ('Path "/my-files/test/a.bin" is not supported', 1),  # original form
            (f"[{_trash_entry('a.bin', 'share~n1', 'share~parent1')}]", 0),  # list /trash
            ('{"uid": "share~parent1"}', 0),  # info on parent
            ('[{"uid": "share~n1", "ok": true}]', 0),  # restore /trash/a.bin
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)

    result = client.restore(["/my-files/test/a.bin"])

    assert result == [{"uid": "share~n1", "ok": True}]
    assert run.calls[1][1:4] == ["filesystem", "list", "/trash"]
    assert run.calls[2][1:4] == ["filesystem", "info", "/my-files/test"]
    assert run.calls[3][1:4] == ["filesystem", "restore", "/trash/a.bin"]


def test_restore_refuses_when_stale_same_named_entry_is_first_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stale same-named trash entry from another parent precedes ours: proton-drive
    # would act on the WRONG node (first-match-wins), so restore must refuse.
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    trash = (
        f"[{_trash_entry('a.bin', 'share~stale', 'share~otherparent')}, "
        f"{_trash_entry('a.bin', 'share~mine', 'share~parent1')}]"
    )
    run = _stub_run_seq(
        [
            ('Path "/my-files/test/a.bin" is not supported', 1),
            (trash, 0),
            ('{"uid": "share~parent1"}', 0),
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(DriveError, match="first match") as excinfo:
        client.restore(["/my-files/test/a.bin"])
    assert len(run.calls) == 3  # never issued the restore
    # #70: the ambiguity error points the user at `protonfs trash list`/`empty`.
    assert "protonfs trash list" in str(excinfo.value)
    assert "protonfs trash empty" in str(excinfo.value)


def test_restore_errors_when_not_in_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run = _stub_run_seq(
        [
            ('Path "/my-files/test/a.bin" is not supported', 1),
            ("[]", 0),  # empty trash
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(DriveError, match="no trashed item"):
        client.restore(["/my-files/test/a.bin"])


def test_restore_errors_when_wrong_node_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defense in depth: if proton-drive restores a different uid than the one we
    # selected (or reports ok=false), surface it instead of claiming success.
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run = _stub_run_seq(
        [
            ('Path "/my-files/test/a.bin" is not supported', 1),
            (f"[{_trash_entry('a.bin', 'share~n1', 'share~parent1')}]", 0),
            ('{"uid": "share~parent1"}', 0),
            ('[{"uid": "share~n1", "ok": false, "error": {"code": 2511}}]', 0),
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(DriveError, match="failed"):
        client.restore(["/my-files/test/a.bin"])


def test_parent_name_returns_decrypted_name(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run = _stub_run(
        '{"uid": "share~p1", "name": {"ok": true, "value": "run1"}}'
    )
    monkeypatch.setattr(subprocess, "run", run)

    assert client.parent_name("share~p1") == "run1"


def test_parent_name_returns_none_on_drive_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # #70: best-effort -- a failed lookup must not blow up `trash list`.
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run = _stub_run("not found", returncode=1)
    monkeypatch.setattr(subprocess, "run", run)

    assert client.parent_name("share~unknown") is None


def test_empty_trash_invokes_empty_trash_command(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run = _stub_run_seq([("{}", 0)])
    monkeypatch.setattr(subprocess, "run", run)

    client.empty_trash()

    assert run.calls[0][1:4] == ["filesystem", "empty-trash", "--json"]
