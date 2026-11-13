from __future__ import annotations

import pytest
from tests.usage.treedrive import TreeDrive

from protonfs.drive import DriveError, SizeResult


def _probe_tree() -> TreeDrive:
    d = TreeDrive()
    d.add_folder("/my-files/p")
    d.add_file("/my-files/p/a", 1048723)
    d.add_file("/my-files/p/c", 524403)
    d.add_folder("/my-files/p/sub")
    d.add_file("/my-files/p/sub/b", 2097364)
    return d


def test_trash_semantics_match_probe():
    d = _probe_tree()
    assert d.size("/my-files/p") == SizeResult(3670490, 4)
    d.trash("/my-files/p/a")
    d.trash("/my-files/p/sub")
    assert d.size("/my-files/p") == SizeResult(3670490, 4)
    assert [e.name for e in d.list_dir("/my-files/p")] == ["c"]
    trashed = {e.name: e for e in d.list_trash(timeout=1)}
    assert set(trashed) == {"a", "sub"}
    assert trashed["a"].size == 1048723 and trashed["sub"].size is None
    assert trashed["a"].trash_time is not None
    with pytest.raises(DriveError, match="Trashed node not found"):
        d.size("/trash/sub")
    with pytest.raises(DriveError, match="Trashed node not found"):
        d.list_dir("/trash/sub")
    d.purge("/my-files/p/a")
    d.purge("/my-files/p/sub")
    assert d.size("/my-files/p") == SizeResult(524403, 1)
    assert d.list_trash(timeout=1) == []


def test_missing_path_raises():
    d = _probe_tree()
    with pytest.raises(DriveError):
        d.size("/my-files/nope")
    with pytest.raises(DriveError):
        d.list_dir("/my-files/nope")


def test_entries_carry_scope_uid_parent_and_mtime():
    d = TreeDrive(scope="vol1")
    pu = d.add_folder("/my-files/p")
    fu = d.add_file("/my-files/p/a", 5)
    (e,) = d.list_dir("/my-files/p")
    assert (e.uid, e.parent_uid, e.scope, e.size, e.kind) == (fu, pu, "vol1", 5, "file")
    assert e.decrypted and e.mtime
    (f,) = d.list_dir("/my-files")
    assert f.kind == "folder" and f.size is None and f.uid == pu


def test_escaped_names_and_devices():
    d = TreeDrive()
    d.add_device("Polaris")
    d.add_file("/devices/Polaris/a\\/b", 3)
    (e,) = d.list_dir("/devices/Polaris")
    assert e.name == "a/b"
    assert [x.name for x in d.list_dir("/devices")] == ["Polaris"]


def test_fail_next_and_calls():
    d = _probe_tree()
    d.fail_next("size", "/my-files/p", RuntimeError("boom"), times=2)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            d.size("/my-files/p")
    assert d.size("/my-files/p").descendants == 4
    d.list_dir("/my-files/p")
    d.list_trash(timeout=1)
    assert ("size", "/my-files/p") in d.calls
    assert ("list", "/my-files/p") in d.calls
    assert ("list_trash", "/trash") in d.calls


def test_before_hook_runs_once_before_matching_call():
    d = _probe_tree()
    d.before("list", "/my-files/p", lambda: d.add_file("/my-files/p/new", 1))
    names = [e.name for e in d.list_dir("/my-files/p")]
    assert "new" in names
    d.before("size", "/my-files/p", lambda: d.purge("/my-files/p/c"))
    assert d.size("/my-files/p").descendants == 4  # a, sub, b, new
    assert d.size("/my-files/p").descendants == 4
