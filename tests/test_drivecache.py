# tests/test_drivecache.py -- #178: proton-drive's cache location and its filesystem
from __future__ import annotations

import os
from pathlib import Path

import pytest

from protonfs import drivecache
from protonfs.drivecache import apply_cache_home, cache_dir, filesystem_type


def test_apply_cache_home_sets_xdg_cache_home() -> None:
    env = {"PROTONFS_DRIVE_CACHE_HOME": "/var/tmp/u", "XDG_CACHE_HOME": "/home/u/.c"}
    assert apply_cache_home(env)["XDG_CACHE_HOME"] == "/var/tmp/u"
    assert env["XDG_CACHE_HOME"] == "/home/u/.c"  # input untouched


def test_apply_cache_home_unset_or_empty_leaves_env_alone() -> None:
    env = {"XDG_CACHE_HOME": "/home/u/.c"}
    assert apply_cache_home(env) is env
    assert apply_cache_home({**env, "PROTONFS_DRIVE_CACHE_HOME": ""}) == {
        **env,
        "PROTONFS_DRIVE_CACHE_HOME": "",
    }


def test_cache_dir_follows_proton_drive_rules() -> None:
    home = {"HOME": "/home/u"}
    assert cache_dir(home, "Linux") == Path("/home/u/.cache/proton-drive-cli")
    assert cache_dir({**home, "XDG_CACHE_HOME": ""}, "Linux") == Path(
        "/home/u/.cache/proton-drive-cli"
    )
    assert cache_dir({**home, "XDG_CACHE_HOME": "/x"}, "Linux") == Path("/x/proton-drive-cli")
    assert cache_dir(home, "Darwin") == Path("/home/u/Library/Caches/proton-drive-cli")
    # PROTON_DRIVE_CACHE_DIR names the directory itself and wins everywhere
    override = {**home, "XDG_CACHE_HOME": "/x", "PROTON_DRIVE_CACHE_DIR": "/y"}
    assert cache_dir(override, "Linux") == Path("/y")
    assert cache_dir(override, "Darwin") == Path("/y")


def test_cache_dir_sees_the_protonfs_override() -> None:
    env = apply_cache_home({"HOME": "/home/u", "PROTONFS_DRIVE_CACHE_HOME": "/var/tmp/u"})
    assert cache_dir(env, "Linux") == Path("/var/tmp/u/proton-drive-cli")


def test_filesystem_type_picks_the_deepest_mount(tmp_path: Path) -> None:
    net = tmp_path / "net"
    local = net / "local"
    local.mkdir(parents=True)
    root = str(tmp_path.resolve())
    mounts = (
        "/dev/sda1 / ext4 rw 0 0\n"
        f"gluster:/v {root}/net fuse.glusterfs rw 0 0\n"
        f"/dev/sdb1 {root}/net/local xfs rw 0 0\n"
    )
    assert filesystem_type(net, mounts) == "fuse.glusterfs"
    assert filesystem_type(local / "not-yet" / "proton-drive-cli", mounts) == "xfs"
    assert filesystem_type(net / "missing" / "proton-drive-cli", mounts) == "fuse.glusterfs"
    # a symlink is judged by where it points
    link = net / "link"
    link.symlink_to(local)
    assert filesystem_type(link / "proton-drive-cli", mounts) == "xfs"


def test_filesystem_type_unescapes_mount_points(tmp_path: Path) -> None:
    spaced = tmp_path / "with space"
    spaced.mkdir()
    escaped = str(spaced.resolve()).replace(" ", "\\040")  # as /proc/mounts writes it
    mounts = f"/dev/sda1 / ext4 rw 0 0\nsrv:/x {escaped} nfs4 rw 0 0\n"
    assert filesystem_type(spaced, mounts) == "nfs4"


def test_filesystem_type_without_proc_mounts(monkeypatch) -> None:
    def no_proc(self, *a, **k):
        raise OSError("no /proc")

    monkeypatch.setattr(drivecache.Path, "read_text", no_proc)
    assert filesystem_type(Path("/")) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can search any directory")
def test_filesystem_type_beneath_an_unsearchable_directory(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        mounts = f"/dev/sda1 / ext4 rw 0 0\nsrv:/x {tmp_path.resolve()} nfs4 rw 0 0\n"
        assert filesystem_type(locked / ".cache" / "proton-drive-cli", mounts) == "nfs4"
    finally:
        locked.chmod(0o700)
