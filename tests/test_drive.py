# tests/test_drive.py
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import protonfs.drive as drive_mod
from protonfs.drive import (
    DriveAuthError,
    DriveClient,
    DriveClockSkewError,
    DriveError,
    DriveThrottleError,
    DriveTransferTimeoutError,
    TransferResult,
)
from protonfs.drive import proton_clock_offset as REAL_CLOCK_OFFSET


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


def test_drive_version_parses_semver_from_raw_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "proton-drive"
    fake_bin.write_text("#!/bin/sh\necho 'Proton Drive CLI cli-drive@0.5.0+73e40d90'\n")
    fake_bin.chmod(0o755)
    client = DriveClient(binary=str(fake_bin))
    assert client.drive_version() == "0.5.0"


def _banner_bin(tmp_path: Path, after: str) -> str:
    fake_bin = tmp_path / "proton-drive"
    fake_bin.write_text(
        "#!/bin/sh\necho 'Proton Drive CLI cli-drive@0.9.0+75612299'\n"
        f"echo 'Proton Drive SDK js@0.22.2+75612299'\n{after}\n"
    )
    fake_bin.chmod(0o755)
    return str(fake_bin)


def test_version_does_not_wait_for_the_update_check(tmp_path: Path) -> None:
    # cli-drive 0.8.0+ fetches proton.me's manifest (up to 5 s) after the banner.
    client = DriveClient(binary=_banner_bin(tmp_path, "sleep 30\necho 'You are running...'"))
    start = time.monotonic()
    assert client.version() == (
        "Proton Drive CLI cli-drive@0.9.0+75612299\nProton Drive SDK js@0.22.2+75612299"
    )
    assert time.monotonic() - start < 10
    assert client.drive_version() == "0.9.0"


def test_version_drops_the_update_hint(tmp_path: Path) -> None:
    client = DriveClient(binary=_banner_bin(tmp_path, "echo 'A newer version is available'"))
    assert "newer" not in client.version()


def test_version_returns_none_when_binary_fails_before_the_banner(tmp_path: Path) -> None:
    fake_bin = tmp_path / "proton-drive"
    fake_bin.write_text("#!/bin/sh\necho 'keyring locked' >&2\nexit 1\n")
    fake_bin.chmod(0o755)
    assert DriveClient(binary=str(fake_bin)).version() is None


def test_drive_version_returns_none_when_binary_missing() -> None:
    client = DriveClient(binary="/nonexistent/proton-drive")
    assert client.drive_version() is None


def test_drive_version_returns_none_on_unparseable_output(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "proton-drive"
    fake_bin.write_text("#!/bin/sh\necho 'not a version string'\n")
    fake_bin.chmod(0o755)
    client = DriveClient(binary=str(fake_bin))
    assert client.drive_version() is None


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
    assert entries == [{"name": {"ok": True, "value": "a"}, "type": "file", "totalStorageSize": 5}]


def test_run_json_auth_signal_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # D5.1: a genuine auth signal ("not logged in") surfaces as DriveAuthError.
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run('{"error": "not logged in"}', returncode=1))
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
        # cli-drive@0.8.0: activeRevision is flat
        '{"name": {"ok": true, "value": "f1"}, "type": "file", "totalStorageSize": 128,'
        ' "activeRevision": {"claimedSize": 100, "claimedDigests": {"sha1": "aa"}}},'
        '{"name": {"ok": true, "value": "sub"}, "type": "folder"},'
        '{"name": {"ok": false, "value": ""}, "type": "file",'
        ' "activeRevision": {"claimedSize": 7}},'
        # cli-drive@0.5.0: activeRevision is wrapped in the {ok, value} envelope
        '{"name": {"ok": true, "value": "f2"}, "type": "file",'
        ' "activeRevision": {"ok": true, "value": {"claimedSize": 200}}}'
        "]"
    )
    monkeypatch.setattr(subprocess, "run", _stub_run(payload))

    identities = client.remote_identities("/my-files/test")

    assert set(identities) == {"f1", "f2"}  # folder + undecryptable entry excluded
    assert identities["f1"].claimed_size == 100  # plaintext size, not totalStorageSize (128)
    assert identities["f1"].sha1 == "aa"
    assert identities["f2"].claimed_size == 200  # read through the 0.5.0 envelope
    assert identities["f2"].sha1 is None


def test_claimed_identity_reads_both_revision_shapes_and_flags_unreadable() -> None:
    # #147: claimedSize/claimedDigests live on activeRevision, which cli-drive@0.8.0
    # returns flat and 0.5.0 wraps in the {ok, value} envelope -- while `name` is
    # enveloped on both. Reading them from the entry itself yields None everywhere,
    # which every verify treats as a pass.
    from protonfs.drive import active_revision, claimed_identity

    flat = {
        "type": "file",
        "activeRevision": {"claimedSize": 100, "claimedDigests": {"sha1": "aa"}},
    }
    enveloped = {
        "type": "file",
        "activeRevision": {
            "ok": True,
            "value": {"claimedSize": 200, "claimedDigests": {"sha1": "bb"}},
        },
    }

    assert claimed_identity(flat) == (100, "aa")
    assert claimed_identity(enveloped) == (200, "bb")

    # Absent revision: nothing to read, but nothing went wrong either.
    assert active_revision({"type": "file"}) == {}
    assert claimed_identity({"type": "file"}) == (None, None)

    # Unreadable revision is NOT the same as an absent one: it means the remote copy
    # cannot be verified, and callers must be able to tell the two apart.
    assert active_revision({"activeRevision": {"ok": False, "value": None}}) is None
    assert claimed_identity({"activeRevision": {"ok": False, "value": None}}) == (None, None)


def test_remote_identities_is_throttle_resilient(monkeypatch: pytest.MonkeyPatch) -> None:
    # The verify-after-push path routes through list_with_backoff, so a throttled
    # `filesystem list` (the degrade-then-timeout signature) is retried rather than
    # left to hang on an unbounded single call. Without the fix this would surface the
    # first TimeoutExpired instead of retrying.
    client = _client(monkeypatch)
    calls = {"n": 0}

    def flaky(path, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise subprocess.TimeoutExpired(cmd="proton-drive", timeout=timeout)
        return [
            {
                "name": {"ok": True, "value": "f1"},
                "type": "file",
                "activeRevision": {"claimedSize": 100, "claimedDigests": {"sha1": "aa"}},
            },
        ]

    monkeypatch.setattr(client, "list", flaky)
    identities = client.remote_identities("/my-files/test", sleep=lambda _: None)

    assert calls["n"] == 3  # retried through the throttle, did not hang or surface attempt 1
    assert identities["f1"].claimed_size == 100


def test_remote_identities_raises_throttle_error_after_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A verify list that stays throttled past the retry budget fails cleanly with
    # DriveThrottleError (bounded), instead of hanging forever on an unbounded call.
    client = _client(monkeypatch)

    def always_timeout(path, timeout=None):
        raise subprocess.TimeoutExpired(cmd="proton-drive", timeout=timeout)

    monkeypatch.setattr(client, "list", always_timeout)
    with pytest.raises(DriveThrottleError):
        client.remote_identities("/x", retries=2, base_delay=0.0, sleep=lambda _: None)


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
                "activeRevision": {
                    "claimedSize": 10,  # plaintext size, on the revision not the entry
                    "claimedDigests": {"sha1": "abc123"},
                },
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
        client.list_with_backoff("/x", retries=5, base_delay=10, cap=15, sleep=slept.append)

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


# --- #69: throttle-resilient upload/download (parity with #33's list) ------------------


def test_upload_retries_on_throttle_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    calls = {"n": 0}
    ok_result = TransferResult(transferred_items=1, skipped_items=0, failed_items=0, failures=[])

    def flaky(args, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise DriveError("429: too many requests")
        return ok_result

    monkeypatch.setattr(client, "_run_transfer", flaky)
    slept: list[float] = []
    result = client.upload([Path("x")], "/my-files/test", base_delay=1, sleep=slept.append)

    assert calls["n"] == 3
    assert result is ok_result
    assert slept == [1, 2]  # exponential backoff between attempts


def test_download_does_not_retry_a_non_throttle_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    calls = {"n": 0}

    def permanent_failure(args, timeout=None):
        calls["n"] += 1
        raise DriveAuthError("unauthenticated")

    monkeypatch.setattr(client, "_run_transfer", permanent_failure)
    with pytest.raises(DriveAuthError):
        client.download(["/my-files/test/x"], Path("."), sleep=lambda _: None)

    assert calls["n"] == 1  # raised immediately, never retried


def test_upload_raises_throttle_error_after_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    def always_throttled(args, timeout=None):
        raise DriveError("rate limit exceeded")

    monkeypatch.setattr(client, "_run_transfer", always_throttled)
    with pytest.raises(DriveThrottleError):
        client.upload(
            [Path("x")], "/my-files/test", retries=2, base_delay=0.01, sleep=lambda _: None
        )


def test_upload_retries_on_timeout_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    calls = {"n": 0}
    ok_result = TransferResult(transferred_items=1, skipped_items=0, failed_items=0, failures=[])

    def flaky(args, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise subprocess.TimeoutExpired(cmd="proton-drive", timeout=timeout)
        return ok_result

    monkeypatch.setattr(client, "_run_transfer", flaky)
    result = client.upload([Path("x")], "/my-files/test", base_delay=0.01, sleep=lambda _: None)

    assert calls["n"] == 2
    assert result is ok_result


def test_transfer_timeout_scales_with_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # #184: a batch carrying several GiB gets time to move them at the floor rate.
    monkeypatch.setattr(drive_mod, "TRANSFER_MIN_RATE", 1024.0)

    assert drive_mod.transfer_timeout(300, None) == 300
    assert drive_mod.transfer_timeout(300, 1024 * 10) == 300  # small: the configured floor
    assert drive_mod.transfer_timeout(300, 1024 * 1000) == 1000


def test_upload_timeout_covers_the_files_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(monkeypatch)
    monkeypatch.setattr(drive_mod, "TRANSFER_MIN_RATE", 100.0)
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "inner.bin").write_bytes(b"x" * 40_000)
    (tmp_path / "top.bin").write_bytes(b"x" * 60_000)
    seen: list[float] = []

    def record(args, timeout=None):
        seen.append(timeout)
        return TransferResult(1, 0, 0, [])

    monkeypatch.setattr(client, "_run_transfer", record)
    client.upload([tmp_path / "dir", tmp_path / "top.bin"], "/my-files/test", timeout=10)

    assert seen == [1000.0]  # 100,000 bytes at 100 B/s, over the 10s floor


def test_transfer_timeout_is_retried_with_a_doubled_timeout_and_no_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    timeouts: list[float] = []
    ok_result = TransferResult(transferred_items=1, skipped_items=0, failed_items=0, failures=[])

    def slow(args, timeout=None):
        timeouts.append(timeout)
        if len(timeouts) < 3:
            raise subprocess.TimeoutExpired(cmd="proton-drive", timeout=timeout)
        return ok_result

    monkeypatch.setattr(client, "_run_transfer", slow)
    slept: list[float] = []
    result = client.download(["/my-files/test/x"], Path("."), timeout=100, sleep=slept.append)

    assert result is ok_result
    assert timeouts == [100, 200, 400]
    assert slept == []  # nothing was throttled, so nothing to back off from


def test_transfer_that_always_times_out_is_not_reported_as_throttling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #184: before, this raised DriveThrottleError("remote is throttling ...").
    client = _client(monkeypatch)

    def always_slow(args, timeout=None):
        raise subprocess.TimeoutExpired(cmd="proton-drive", timeout=timeout)

    monkeypatch.setattr(client, "_run_transfer", always_slow)
    with pytest.raises(DriveTransferTimeoutError) as info:
        client.download(
            ["/my-files/test/x"], Path("."), timeout=10, retries=2, payload_bytes=3 * 1024**3
        )

    assert not isinstance(info.value, DriveThrottleError)
    message = str(info.value)
    assert "timed out after" in message and "3.0 GiB" in message
    assert "PROTONFS_TRANSFER_TIMEOUT" in message and "PROTONFS_BATCH_SIZE" in message
    assert "throttl" not in message


def test_list_timeout_is_still_treated_as_throttling(monkeypatch: pytest.MonkeyPatch) -> None:
    # #184 only changes transfers: a listing that hangs is the throttle signature (#33).
    client = _client(monkeypatch)

    def always_timeout(path, timeout=None):
        raise subprocess.TimeoutExpired(cmd="proton-drive", timeout=timeout)

    monkeypatch.setattr(client, "list", always_timeout)
    with pytest.raises(DriveThrottleError):
        client.list_with_backoff("/x", retries=1, base_delay=0, sleep=lambda _: None)


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
    run = _stub_run('{"uid": "share~p1", "name": {"ok": true, "value": "run1"}}')
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


# --- backend passthrough logging (5h) -----------------------------------------------


def test_invoke_logs_argv_at_debug(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run('{"ok": true}'))

    with caplog.at_level(logging.DEBUG, logger="protonfs.drive"):
        client._invoke(["filesystem", "list", "/"])

    assert any("invoke" in r.getMessage() for r in caplog.records)


def test_invoke_logs_stderr_when_backend_passthrough_enabled(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run("", stderr="some backend noise"))
    monkeypatch.setattr("protonfs.drive.backend_passthrough_enabled", lambda: True)

    with caplog.at_level(logging.DEBUG, logger="protonfs.drive"):
        client._invoke(["filesystem", "list", "/"])

    assert any("some backend noise" in r.getMessage() for r in caplog.records)


def test_invoke_does_not_log_stderr_when_backend_passthrough_disabled(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run("", stderr="some backend noise"))
    monkeypatch.setattr("protonfs.drive.backend_passthrough_enabled", lambda: False)

    with caplog.at_level(logging.DEBUG, logger="protonfs.drive"):
        client._invoke(["filesystem", "list", "/"])

    assert not any("some backend noise" in r.getMessage() for r in caplog.records)


# --- command summarization: keep large-transfer logs readable --------------------------


def test_summarize_command_passes_short_commands_through() -> None:
    from protonfs.drive import _summarize_command

    assert _summarize_command(["filesystem", "list", "/my-files/x"]) == (
        "filesystem list /my-files/x"
    )


def test_summarize_command_collapses_long_file_lists() -> None:
    from protonfs.drive import _summarize_command

    args = ["filesystem", "upload", *[f"/repo/sim/f_{i:05d}" for i in range(197)], "/my-files/sim"]
    out = _summarize_command(args)

    # The hundreds of paths collapse to a count; the verb and destination survive.
    assert "filesystem upload" in out
    assert "/my-files/sim" in out
    assert "197 files" in out or "197 paths" in out
    assert "f_00042" not in out  # no interior path spelled out
    assert len(out) < 120  # bounded, not thousands of chars


# --- credentials store: _drive_env routes through credstore.drive_env ------------------


def test_driveclient_env_uses_credstore(monkeypatch: pytest.MonkeyPatch) -> None:
    from protonfs import credstore

    monkeypatch.setattr(credstore, "drive_env", lambda e=None: {"FROM_CREDSTORE": "1"})
    client = DriveClient(binary="/x")
    assert client._drive_env() == {"FROM_CREDSTORE": "1"}


# --- #136: a stale proton-drive process holding the SQLite cache lock -------------------


def test_sqlite_busy_is_classified_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#136: proton-drive prints a non-JSON SQLiteError blob when another proton-drive
    process holds its cache lock. That currently surfaces as the generic
    "unparseable output from proton-drive: ...", which gives the operator no path from
    the symptom to the cause (a stale process to kill)."""
    from protonfs.drive import DriveLockError

    blob = (
        'SQLiteError: database is locked\n      errno: 5,\n       code: "SQLITE_BUSY"\n'
        "      at setEntity (src/cache/sqliteCache.ts:21:15)\n"
    )
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run(blob, returncode=1))

    with pytest.raises(DriveLockError) as excinfo:
        client.list("/my-files/test")

    message = str(excinfo.value)
    assert "proton-drive" in message
    # must point at the actual remedy rather than at "unparseable output"
    assert "pgrep" in message


def test_sqlite_busy_on_a_transfer_is_classified_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock blocks transfers as well as listings, and _run_transfer takes a different
    path out of the parser, so it needs the same classification."""
    from protonfs.drive import DriveLockError

    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(
        subprocess, "run", _stub_run("SQLiteError: database is locked", returncode=1)
    )

    with pytest.raises(DriveLockError):
        client.upload([Path("/tmp/x")], "/my-files/test")


def test_lock_error_is_not_retried_as_a_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A held lock is not transient in the way throttling is -- retrying with backoff just
    burns the retry budget while the stale process keeps holding it. It must surface at
    once so the operator can clear it."""
    from protonfs.drive import DriveLockError

    calls = []

    def counting_run(args, capture_output, text, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 1, stdout="SQLiteError: database is locked", stderr=""
        )

    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", counting_run)

    with pytest.raises(DriveLockError):
        client.list_with_backoff("/my-files/test", retries=3, sleep=lambda _: None)

    assert len(calls) == 1, "a lock error must not be retried with backoff"


# --- #137: verification silently degrading to name-presence-only ------------------------


def _listing_json(names_with_sizes, *, claimed: bool) -> str:
    import json

    entries = []
    for name, size in names_with_sizes:
        entry = {
            "name": {"ok": True, "value": name},
            "type": "file",
            "totalStorageSize": size + 651,
        }
        if claimed:
            entry["activeRevision"] = {
                "claimedSize": size,
                "claimedDigests": {"sha1": "abc"},
            }
        entries.append(entry)
    return json.dumps(entries)


def test_remote_identities_warns_when_binary_supplies_no_claimed_sizes(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """#137: proton-drive 0.5.0 omits claimedSize/claimedDigests entirely, so
    `_verify_remote`'s size and digest guards both no-op and verification degrades to
    "a file of this name exists". That is a much weaker claim than the docs promise and
    it currently happens silently."""
    import logging

    import protonfs.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_CLAIMED_METADATA_WARNED", False, raising=False)
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(
        subprocess, "run", _stub_run(_listing_json([("a", 10), ("b", 20)], claimed=False))
    )
    # The warning names the binary's version, read through Popen, not the run stub.
    monkeypatch.setattr(client, "version", lambda: None)

    with caplog.at_level(logging.WARNING, logger="protonfs.drive"):
        idents = client.remote_identities("/my-files/test")

    assert len(idents) == 2
    assert all(i.claimed_size is None for i in idents.values())
    assert any(
        "presence" in r.message.lower() or "claimedsize" in r.message.lower()
        for r in caplog.records
    ), caplog.text


def test_remote_identities_does_not_warn_when_claimed_sizes_are_present(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    import logging

    import protonfs.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_CLAIMED_METADATA_WARNED", False, raising=False)
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run(_listing_json([("a", 10)], claimed=True)))

    with caplog.at_level(logging.WARNING, logger="protonfs.drive"):
        idents = client.remote_identities("/my-files/test")

    assert idents["a"].claimed_size == 10
    assert not caplog.records, caplog.text


def test_remote_identities_does_not_warn_on_an_empty_listing(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """An empty directory says nothing about the binary's capabilities -- warning there
    would fire on every first push into a new folder."""
    import logging

    import protonfs.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_CLAIMED_METADATA_WARNED", False, raising=False)
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run("[]"))

    with caplog.at_level(logging.WARNING, logger="protonfs.drive"):
        assert client.remote_identities("/my-files/test") == {}

    assert not caplog.records, caplog.text


def test_remote_identities_does_not_shell_out_for_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#137 regression: the degraded-verification warning must not read the proton-drive
    version eagerly. Reading it spawns the binary (seconds of SDK startup), and this runs
    after EVERY verify listing -- once per parent directory on a push. It also broke any
    caller that stubs `list` without stubbing the binary, which is how CI caught it.
    """
    import protonfs.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_CLAIMED_METADATA_WARNED", False, raising=False)
    client = DriveClient(binary="proton-drive")
    calls = []

    def exploding_version():
        calls.append(1)
        raise AssertionError("drive_version() must not be called when nothing is warned")

    monkeypatch.setattr(client, "drive_version", exploding_version)
    # a listing WITH claimed sizes -> no warning -> version must never be consulted
    monkeypatch.setattr(
        client,
        "list_with_backoff",
        lambda *a, **k: [
            {
                "name": {"ok": True, "value": "a"},
                "type": "file",
                "activeRevision": {"claimedSize": 10},
            }
        ],
    )

    idents = client.remote_identities("/my-files/test")

    assert idents["a"].claimed_size == 10
    assert calls == []


# --- #179: cli-drive@0.8.0 renamed the conflict strategies -----------------------------


def _recording(seen: list[list[str]]):
    """A stand-in transfer that records its argv and reports nothing moved."""
    return lambda args, **kw: seen.append(args) or TransferResult(0, 0, 0, [])


def _transfer_args(monkeypatch: pytest.MonkeyPatch, version: str | None, call) -> list[str]:
    client = DriveClient(binary="/x")
    monkeypatch.setattr(client, "drive_version", lambda: version)
    seen: list[list[str]] = []
    monkeypatch.setattr(client, "_run_transfer_with_backoff", _recording(seen))
    call(client)
    return seen[0]


def _opt(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        ("merge", "create-new-revision"),
        ("keep-both", "rename"),
        ("replace", "replace"),
        ("skip", "skip"),
    ],
)
def test_upload_file_strategy_uses_0_8_names(monkeypatch, strategy, expected) -> None:
    args = _transfer_args(
        monkeypatch, "0.8.0", lambda c: c.upload([Path("a")], "/p", file_strategy=strategy)
    )
    assert _opt(args, "-f") == expected


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [("keep-both", "rename"), ("replace", "remove"), ("skip", "skip")],
)
def test_download_file_strategy_uses_0_8_names(monkeypatch, strategy, expected) -> None:
    args = _transfer_args(
        monkeypatch, "0.8.0", lambda c: c.download(["/p/a"], Path("d"), file_strategy=strategy)
    )
    assert _opt(args, "-f") == expected


def test_folder_strategies_use_0_8_names(monkeypatch) -> None:
    up = _transfer_args(
        monkeypatch, "0.8.0", lambda c: c.upload([Path("a")], "/p", folder_strategy="keep-both")
    )
    down = _transfer_args(
        monkeypatch, "0.8.0", lambda c: c.download(["/p/a"], Path("d"), folder_strategy="replace")
    )
    merged = _transfer_args(
        monkeypatch, "0.8.0", lambda c: c.upload([Path("a")], "/p", folder_strategy="merge")
    )
    assert (_opt(up, "-d"), _opt(down, "-d"), _opt(merged, "-d")) == ("rename", "remove", "merge")


def test_later_versions_also_use_0_8_names(monkeypatch) -> None:
    args = _transfer_args(
        monkeypatch, "0.9.0", lambda c: c.upload([Path("a")], "/p", file_strategy="merge")
    )
    assert _opt(args, "-f") == "create-new-revision"


@pytest.mark.parametrize("version", ["0.7.0", "0.5.0", "0.4.6", None])
def test_older_or_unknown_versions_keep_the_original_names(monkeypatch, version) -> None:
    up = _transfer_args(
        monkeypatch, version, lambda c: c.upload([Path("a")], "/p", file_strategy="merge")
    )
    down = _transfer_args(
        monkeypatch, version, lambda c: c.download(["/p/a"], Path("d"), file_strategy="replace")
    )
    assert (_opt(up, "-f"), _opt(down, "-f")) == ("merge", "replace")


def test_drive_version_is_read_once_per_client(monkeypatch) -> None:
    client = DriveClient(binary="/x")
    calls: list[int] = []
    monkeypatch.setattr(client, "drive_version", lambda: calls.append(1) or "0.8.0")
    monkeypatch.setattr(client, "_run_transfer_with_backoff", _recording([]))
    client.upload([Path("a")], "/p", file_strategy="merge")
    client.download(["/p/a"], Path("d"), file_strategy="replace")
    assert len(calls) == 1


def test_unknown_version_is_not_cached(monkeypatch) -> None:
    client = DriveClient(binary="/x")
    versions = iter([None, "0.8.0"])
    monkeypatch.setattr(client, "drive_version", lambda: next(versions))
    seen: list[list[str]] = []
    monkeypatch.setattr(client, "_run_transfer_with_backoff", _recording(seen))
    client.upload([Path("a")], "/p", file_strategy="merge")
    client.upload([Path("a")], "/p", file_strategy="merge")
    assert (_opt(seen[0], "-f"), _opt(seen[1], "-f")) == ("merge", "create-new-revision")


# --- #183: -t / --skip-thumbnails --------------------------------------------------------


@pytest.mark.parametrize("version", ["0.4.6", "0.8.0"])
def test_upload_skip_thumbnails_passes_t_before_the_paths(monkeypatch, version) -> None:
    args = _transfer_args(
        monkeypatch, version, lambda c: c.upload([Path("a.tif")], "/p", skip_thumbnails=True)
    )
    assert args[:2] == ["filesystem", "upload"]
    assert args.index("-t") < args.index("a.tif")


def test_upload_keeps_thumbnails_by_default(monkeypatch) -> None:
    args = _transfer_args(monkeypatch, "0.8.0", lambda c: c.upload([Path("a.tif")], "/p"))
    assert "-t" not in args


def test_is_thumbnail_failure_matches_proton_drive_message() -> None:
    from protonfs.drive import is_thumbnail_failure

    assert is_thumbnail_failure(
        {"name": "x.tif", "error": "ValidationError: Failed to generate thumbnails (use "
         "--skip-thumbnails to upload without thumbnails): Image: format not supported"}
    )
    assert not is_thumbnail_failure({"name": "x", "error": "Name conflict ... already exists"})


# --- #197: move and rename ---------------------------------------------------------------


def test_move_passes_every_source_then_the_target_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run = _stub_run_seq([('[{"ok": true}, {"ok": false, "error": "exists"}]', 0)])
    monkeypatch.setattr(subprocess, "run", run)

    results = client.move(["/my-files/r/a/x", "/my-files/r/a/y"], "/my-files/r/b")

    assert run.calls == [[
        "proton-drive", "filesystem", "move", "/my-files/r/a/x", "/my-files/r/a/y",
        "/my-files/r/b", "--json",
    ]]
    # per-node failures are returned, not raised: the caller checks the listing
    assert results == [{"ok": True}, {"ok": False, "error": "exists"}]


def test_move_raises_a_drive_error_when_the_command_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", _stub_run("", returncode=1, stderr="no such node"))

    with pytest.raises(DriveError, match="no such node"):
        client.move(["/my-files/r/a/x"], "/my-files/r/b")


def test_rename_passes_the_path_and_the_new_name(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run = _stub_run_seq([('{"ok": true}', 0)])
    monkeypatch.setattr(subprocess, "run", run)

    client.rename("/my-files/r/a/x", "y")

    assert run.calls == [["proton-drive", "filesystem", "rename", "/my-files/r/a/x", "y", "--json"]]


def test_rename_raises_when_proton_drive_reports_the_node_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(
        subprocess, "run", _stub_run('[{"ok": false, "error": "name already exists"}]')
    )

    with pytest.raises(DriveError, match="already exists"):
        client.rename("/my-files/r/a/x", "y")


# --- #218: proton-drive glob-expands local paths --------------------------------------
# proton-drive 0.9.0 (cli/src/commands/fileSystem/localPath.ts) passes any local path
# containing * ? [ { to Node's fs.glob, and a pattern matching nothing aborts the whole
# command. Measured against the real binary, 2026-10-23.


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/d/plain name.mp4", "/d/plain name.mp4"),
        ("/d/no-trigger(1)!@+}].txt", "/d/no-trigger(1)!@+}].txt"),
        ("/d/b_[-1].txt", "/d/b_[[]-1].txt"),
        ("/d/profiles_mload00[6e,7a2].mp4", "/d/profiles_mload00[[]6e,7a2].mp4"),
        ("/d/s*ar.txt", "/d/s[*]ar.txt"),
        ("/d/q?.txt", "/d/q[?].txt"),
        ("/d/brn{x}[1].txt", "/d/brn[{]x}[[]1].txt"),
        ("/d/seq{1..3}.txt", "/d/seq[{]1..3}.txt"),
        ("/d/ext+(1)[2].txt", "/d/ext+[(]1)[[]2].txt"),
        ("/run [7]/a.txt", "/run [[]7]/a.txt"),
    ],
)
def test_glob_literal_escapes_what_proton_drive_would_expand(path: str, expected: str) -> None:
    assert drive_mod._glob_literal(path) == expected


@pytest.mark.parametrize("path", ["/d/br{x,y}.txt", "/d/back\\sl[1].txt"])
def test_glob_literal_is_none_for_a_path_no_pattern_can_name(path: str) -> None:
    assert drive_mod._glob_literal(path) is None


def _capture_run(stdout: str, returncode: int = 0, stderr: str = ""):
    calls: list[list[str]] = []

    def _run(args, capture_output, text, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    return _run, calls


_ONE_DONE = '{"transferredItems":1,"skippedItems":0,"failedItems":0,"failures":[]}'


def test_upload_escapes_glob_metacharacters(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run, calls = _capture_run(_ONE_DONE)
    monkeypatch.setattr(subprocess, "run", run)
    client.upload([Path("/d/plain.txt"), Path("/d/b_[-1].txt")], "/my-files/test")
    assert calls[0][-4:-1] == ["/d/plain.txt", "/d/b_[[]-1].txt", "/my-files/test"]


def test_upload_fails_a_path_no_pattern_can_name_without_sending_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run, calls = _capture_run(_ONE_DONE)
    monkeypatch.setattr(subprocess, "run", run)
    result = client.upload([Path("/d/ok.txt"), Path("/d/br{x,y}.txt")], "/my-files/test")
    assert "/d/br{x,y}.txt" not in calls[0]
    assert (result.transferred_items, result.failed_items) == (1, 1)
    assert result.failures[0]["name"] == "br{x,y}.txt"
    assert "rename" in result.failures[0]["error"]


def test_upload_of_only_unnameable_paths_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run, calls = _capture_run(_ONE_DONE)
    monkeypatch.setattr(subprocess, "run", run)
    result = client.upload([Path("/d/back\\sl[1].txt")], "/my-files/test")
    assert calls == []
    assert (result.transferred_items, result.failed_items) == (0, 1)


def test_download_escapes_the_local_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run, calls = _capture_run(_ONE_DONE)
    monkeypatch.setattr(subprocess, "run", run)
    client.download(["/my-files/r [1]/a.txt"], Path("/store [1]/.protonfs/tmp"))
    # remote paths are not globbed by proton-drive; only the local folder is
    assert calls[0][-3:-1] == ["/my-files/r [1]/a.txt", "/store [[]1]/.protonfs/tmp"]


def test_download_into_an_unnameable_folder_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    run, calls = _capture_run(_ONE_DONE)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(DriveError, match="glob"):
        client.download(["/my-files/a.txt"], Path("/s{a,b}/tmp"))
    assert calls == []


_ZERO = '{"transferredItems":0,"transferredBytes":0,"skippedItems":0,"failedItems":0,"failures":[]}'


def test_transfer_that_failed_as_a_whole_raises_despite_a_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """proton-drive prints its (all-zero) summary even when the command aborted before
    transferring anything, then exits 1 with the reason on stderr."""
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    stderr = "No paths matched: /d/b_[-1].txt\n"
    monkeypatch.setattr(subprocess, "run", _stub_run(_ZERO, returncode=1, stderr=stderr))
    with pytest.raises(DriveError, match="No paths matched: /d/b_"):
        client.upload([Path("/d/a.txt")], "/my-files/test")


def test_whole_transfer_failure_names_the_error_not_the_stack_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected crash (here a file gone since the scan) puts the summary AND the
    stack trace on stdout and only a banner on stderr (measured, cli 0.9.0)."""
    client = DriveClient(binary="proton-drive")
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    stdout = (
        _ZERO + "\n"
        "156 |     private async enqueueLocalPath(absolutePath: string): Promise<void> {\n"
        "157 |         const stats = await lstat(absolutePath);\n"
        "                                  ^\n"
        "ENOENT: no such file or directory, lstat '/d/gone.txt'\n"
        '    path: "/d/gone.txt",\n'
        "\n"
        "      at async enqueueLocalPath (src/commands/fileSystem/transferQueue.ts:157:29)\n"
        "Error details:\n"
        "{\n"
        "  code: 'ENOENT',\n"
        "}\n"
    )
    stderr = "===============================================\n"
    monkeypatch.setattr(subprocess, "run", _stub_run(stdout, returncode=1, stderr=stderr))
    with pytest.raises(DriveError) as info:
        client.upload([Path("/d/gone.txt")], "/my-files/test")
    assert str(info.value) == "ENOENT: no such file or directory, lstat '/d/gone.txt'"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("plain [1] {x,y}.dat", "plain [1] {x,y}.dat"),
        ("q?.dat", "q_.dat"),
        ("t 12:00.dat", "t 12_00.dat"),
        ('a<b>"c|d*e\\f.dat', "a_b__c_d_e_f.dat"),
        ("bell\x07\x7f.dat", "bell__.dat"),
        (".", "_"),
        ("..", "__"),
    ],
)
def test_downloaded_name_mirrors_proton_drives_local_renaming(name: str, expected: str) -> None:
    assert drive_mod.downloaded_name(name) == expected


# --- clock skew: Proton rejects signatures dated ahead of its own clock -------------

_PROTON_DATE = "Fri, 30 Oct 2026 14:21:08 GMT"
_PROTON_TS = 1793370068.0  # _PROTON_DATE as a Unix time


class _Response:
    def __init__(self, date: str | None) -> None:
        self.headers = {"Date": date} if date else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_proton_clock_offset_is_local_minus_proton_time() -> None:
    offset = REAL_CLOCK_OFFSET(
        opener=lambda request, timeout: _Response(_PROTON_DATE),
        clock=lambda: _PROTON_TS + 124.5,
    )
    # The header truncates to whole seconds, so Proton's time is taken mid-second.
    assert offset == pytest.approx(124.0)


def test_proton_clock_offset_reads_the_date_of_an_http_error() -> None:
    import urllib.error

    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {"Date": _PROTON_DATE}, None
        )

    offset = REAL_CLOCK_OFFSET(opener=opener, clock=lambda: _PROTON_TS - 106.5)
    assert offset == pytest.approx(-107.0)


@pytest.mark.parametrize("failure", ["unreachable", "no-date"])
def test_proton_clock_offset_is_none_when_it_cannot_measure(failure: str) -> None:
    import urllib.error

    def opener(request, timeout):
        if failure == "unreachable":
            raise urllib.error.URLError("no route to host")
        return _Response(None)

    assert REAL_CLOCK_OFFSET(opener=opener, clock=lambda: _PROTON_TS) is None


def _upload_client(
    monkeypatch: pytest.MonkeyPatch, offset: float | None
) -> tuple[DriveClient, dict]:
    calls = {"probe": 0, "run": 0}

    def probe() -> float | None:
        calls["probe"] += 1
        return offset

    def run(*args, **kwargs):
        calls["run"] += 1
        return _stub_run('{"transferredItems":1,"skippedItems":0,"failedItems":0,"failures":[]}')(
            *args, **kwargs
        )

    monkeypatch.setattr("protonfs.drive.proton_clock_offset", probe)
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", run)
    return DriveClient(binary="proton-drive"), calls


def test_upload_refuses_before_sending_when_the_clock_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _upload_client(monkeypatch, 124.0)
    with pytest.raises(DriveClockSkewError, match="124 s ahead"):
        client.upload([Path("x")], "/my-files/test")
    assert calls["run"] == 0


@pytest.mark.parametrize("offset", [None, -107.0, 0.0, 29.0])
def test_upload_goes_ahead_when_the_clock_is_within_the_limit_or_unmeasured(
    monkeypatch: pytest.MonkeyPatch, offset: float | None
) -> None:
    client, calls = _upload_client(monkeypatch, offset)
    assert client.upload([Path("x")], "/my-files/test").transferred_items == 1
    assert calls["run"] == 1


def test_clock_ahead_limit_comes_from_the_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("protonfs.drive.CLOCK_AHEAD_LIMIT_SECONDS", 200.0)
    client, calls = _upload_client(monkeypatch, 124.0)
    client.upload([Path("x")], "/my-files/test")
    assert calls["run"] == 1


def test_clock_is_probed_once_per_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _upload_client(monkeypatch, 0.0)
    client.upload([Path("x")], "/my-files/test")
    client.upload([Path("y")], "/my-files/test")
    assert calls == {"probe": 1, "run": 2}


@pytest.mark.parametrize(
    "error",
    [
        "APICodeError: Could not verify the nodeKey was used for encrypting , contentKeyPacket",
        "Error: Error generating session key: Could not find valid self-signature in key "
        "c11158dcd351e604: Signature creation time is in the future",
    ],
)
def test_upload_whose_every_item_failed_on_signature_time_raises_clock_skew(
    monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    client, _ = _upload_client(monkeypatch, None)
    payload = (
        '{"transferredItems":0,"skippedItems":0,"failedItems":2,"failures":['
        f'{{"name":"a","error":"{error}"}},{{"name":"b","error":"{error}"}}]}}'
    )
    monkeypatch.setattr(subprocess, "run", _stub_run(payload, returncode=1))
    with pytest.raises(DriveClockSkewError, match="signed in the future"):
        client.upload([Path("a"), Path("b")], "/my-files/test")


def test_upload_with_one_signature_failure_among_others_is_a_normal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _upload_client(monkeypatch, None)
    payload = (
        '{"transferredItems":1,"skippedItems":0,"failedItems":1,"failures":['
        '{"name":"a","error":"APICodeError: Could not verify the nodeKey was used '
        'for encrypting"}]}'
    )
    monkeypatch.setattr(subprocess, "run", _stub_run(payload, returncode=1))
    assert client.upload([Path("a"), Path("b")], "/my-files/test").failed_items == 1


# --- clock_skew: run proton-drive on a corrected clock ----------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("off", "off"), ("auto", "auto"), (" AUTO ", "auto"),
        ("124", 124.0), ("-3.5", -3.5), (60, 60.0),
    ],
)
def test_parse_clock_skew_accepts_off_auto_or_seconds(value, expected) -> None:
    assert drive_mod.parse_clock_skew(value) == expected


@pytest.mark.parametrize("value", ["sometimes", "", None, "12s"])
def test_parse_clock_skew_rejects_anything_else(value) -> None:
    with pytest.raises(ValueError, match="clock_skew"):
        drive_mod.parse_clock_skew(value)


def _skew_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock_skew, offset: float | None
) -> tuple[DriveClient, list[dict]]:
    envs: list[dict] = []

    def run(args, capture_output, text, **kwargs):
        envs.append(kwargs["env"])
        return subprocess.CompletedProcess(
            args, 0, stdout='{"transferredItems":1,"skippedItems":0,"failedItems":0,"failures":[]}',
            stderr="",
        )

    monkeypatch.setattr("protonfs.drive.proton_clock_offset", lambda: offset)
    monkeypatch.setattr("protonfs.drive.state_dir", lambda: tmp_path)
    monkeypatch.setattr("protonfs.drive.shutil.which", lambda _: "/usr/bin/proton-drive")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv("BUN_OPTIONS", raising=False)
    return DriveClient(binary="proton-drive", clock_skew=clock_skew), envs


def test_auto_clock_skew_runs_proton_drive_on_the_measured_correction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, envs = _skew_client(monkeypatch, tmp_path, "auto", 124.4)
    client.upload([Path("x")], "/my-files/test")
    env = envs[0]
    shim = tmp_path / "clock_skew.js"
    assert env["BUN_OPTIONS"] == f"--preload {shim}"
    assert env["PROTONFS_CLOCK_SKEW_MS"] == "-124400"
    assert "PROTONFS_CLOCK_SKEW_MS" in shim.read_text()


def test_fixed_clock_skew_is_applied_and_the_rest_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, envs = _skew_client(monkeypatch, tmp_path, "120", 124.0)
    client.upload([Path("x")], "/my-files/test")
    assert envs[0]["PROTONFS_CLOCK_SKEW_MS"] == "-120000"


def test_fixed_clock_skew_that_leaves_the_clock_ahead_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, envs = _skew_client(monkeypatch, tmp_path, "20", 124.0)
    with pytest.raises(DriveClockSkewError, match="104 s ahead"):
        client.upload([Path("x")], "/my-files/test")
    assert envs == []


def test_fixed_clock_skew_applies_when_the_offset_cannot_be_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, envs = _skew_client(monkeypatch, tmp_path, "124", None)
    client.upload([Path("x")], "/my-files/test")
    assert envs[0]["PROTONFS_CLOCK_SKEW_MS"] == "-124000"


@pytest.mark.parametrize(("clock_skew", "offset"), [("auto", None), ("off", 10.0), ("0", 0.0)])
def test_no_preload_when_there_is_nothing_to_correct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock_skew, offset
) -> None:
    client, envs = _skew_client(monkeypatch, tmp_path, clock_skew, offset)
    client.upload([Path("x")], "/my-files/test")
    assert "BUN_OPTIONS" not in envs[0] and "PROTONFS_CLOCK_SKEW_MS" not in envs[0]


def test_clock_skew_keeps_existing_bun_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, envs = _skew_client(monkeypatch, tmp_path, "auto", 124.0)
    monkeypatch.setenv("BUN_OPTIONS", "--smol")
    client.upload([Path("x")], "/my-files/test")
    assert envs[0]["BUN_OPTIONS"] == f"--smol --preload {tmp_path / 'clock_skew.js'}"


def test_correction_also_covers_listings_before_any_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, envs = _skew_client(monkeypatch, tmp_path, "auto", 124.0)
    monkeypatch.setattr(
        subprocess, "run",
        lambda args, capture_output, text, **kw: envs.append(kw["env"])
        or subprocess.CompletedProcess(args, 0, stdout="[]", stderr=""),
    )
    client.list("/my-files/test")
    assert envs[0]["PROTONFS_CLOCK_SKEW_MS"] == "-124000"
