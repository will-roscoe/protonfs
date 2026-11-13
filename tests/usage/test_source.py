from __future__ import annotations

import threading
import time

import pytest

from protonfs.drive import DriveError, DriveLockError, SizeResult
from protonfs.usage.source import CliDriveSource, list_timeout, normalise_entry


class StubClient:
    def __init__(self, fail=0, delay=0.0):
        self.fail = fail
        self.delay = delay
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0
        self._lock = threading.Lock()

    def _enter(self):
        with self._lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        time.sleep(self.delay)
        with self._lock:
            self.inflight -= 1

    def _maybe_fail(self):
        if self.fail:
            self.fail -= 1
            raise DriveLockError("locked")

    def list_with_backoff(self, path, *, timeout):
        self.calls.append(("list_with_backoff", path, timeout))
        self._enter()
        self._maybe_fail()
        return [{"type": "file", "uid": "u", "name": {"ok": True, "value": "f"},
                 "totalStorageSize": 7}]

    def list(self, path, timeout=None):
        self.calls.append(("list", path, timeout))
        self._enter()
        self._maybe_fail()
        return []

    def size(self, path, **kw):
        self.calls.append(("size", path))
        self._enter()
        self._maybe_fail()
        return SizeResult(1, 2)


def test_list_timeout_scales():
    assert list_timeout(None) == 45
    assert list_timeout(22470) == pytest.approx(1378.2)
    assert list_timeout(10**9) == 3600


def test_device_entry_is_folder():
    e = normalise_entry({"type": "Windows", "rootFolderUid": "r",
                         "name": {"ok": True, "value": "Polaris"}, "uid": "u"})
    assert e.kind == "folder" and e.name == "Polaris" and e.size is None


def test_file_and_folder_entries():
    f = normalise_entry({"type": "file", "uid": "u", "parentUid": "p", "treeEventScopeId": "s",
                         "totalStorageSize": 9, "modificationTime": "t", "trashTime": "x",
                         "name": {"ok": True, "value": "f"}})
    assert (f.kind, f.size, f.parent_uid, f.scope, f.mtime, f.trash_time, f.decrypted) == (
        "file", 9, "p", "s", "t", "x", True)
    d = normalise_entry({"type": "folder", "uid": "u", "name": {"ok": True, "value": "d"}})
    assert d.kind == "folder" and d.size is None and d.trash_time is None


def test_undecryptable_name_uses_uid():
    e = normalise_entry({"type": "file", "uid": "uid1", "name": {"ok": False}})
    assert e.name == "uid1" and e.decrypted is False


def test_lock_error_retried_then_raised():
    sleeps = []
    c = StubClient(fail=7)
    src = CliDriveSource(c, sleep=sleeps.append)
    with pytest.raises(DriveLockError):
        src.size("/my-files")
    assert len(c.calls) == 7
    assert sleeps == [0.5, 1, 2, 4, 4, 4]


def test_lock_error_then_ok():
    sleeps = []
    c = StubClient(fail=3)
    src = CliDriveSource(c, sleep=sleeps.append)
    assert src.size("/my-files") == SizeResult(1, 2)
    assert sleeps == [0.5, 1, 2]


def test_non_lock_error_not_retried():
    class C(StubClient):
        def size(self, path, **kw):
            raise DriveError("nope")

    sleeps = []
    with pytest.raises(DriveError):
        CliDriveSource(C(), sleep=sleeps.append).size("/x")
    assert sleeps == []


def test_list_dir_and_trash_use_client():
    c = StubClient()
    src = CliDriveSource(c)
    (e,) = src.list_dir("/my-files", expected_entries=100)
    assert e.name == "f" and e.size == 7
    assert c.calls[0] == ("list_with_backoff", "/my-files", list_timeout(100))
    assert src.list_trash(timeout=12.5) == []
    assert c.calls[1] == ("list", "/trash", 12.5)


def test_semaphore_bounds_concurrency():
    c = StubClient(delay=0.05)
    src = CliDriveSource(c, concurrency=4)
    threads = [threading.Thread(target=src.size, args=("/x",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 2 <= c.max_inflight <= 4  # bounded by the semaphore; overlap is timing-dependent
    assert src.concurrency == 4
