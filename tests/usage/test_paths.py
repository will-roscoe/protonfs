from __future__ import annotations

from pathlib import Path

import pytest

from protonfs.config import Config, save_config
from protonfs.usage.paths import (
    Target,
    TargetError,
    escape_name,
    is_remote,
    is_within,
    join,
    parent_of,
    relative,
    resolve_target,
    split,
)


def _store(tmp_path: Path) -> Path:
    store = tmp_path / "sph-dev" / "sim"
    (store / ".protonfs").mkdir(parents=True)
    save_config(store, Config(remote_root="/my-files/sph-dev/sim", device_id=""))
    return store


def test_is_remote() -> None:
    assert is_remote("/") and is_remote("/my-files") and is_remote("/my-files/a/b")
    assert is_remote("/devices/Polaris")
    assert not is_remote("/win/e/Repositories/x") and not is_remote("sim/mload005")


def test_escape_and_split_roundtrip() -> None:
    p = join(join("/my-files", "a/b"), "c\\d")
    assert p == "/my-files/a\\/b/c\\\\d"
    assert split(p) == ["my-files", "a/b", "c\\d"]
    assert parent_of(p) == "/my-files/a\\/b" and parent_of("/") is None
    assert parent_of("/my-files") == "/"
    assert is_within("/my-files/a", "/my-files") and not is_within("/my-filesX", "/my-files")
    assert is_within("/my-files", "/my-files") and is_within("/x", "/")
    assert escape_name("a/b\\") == "a\\/b\\\\"
    assert join("/", "x") == "/x"


def test_relative_unescaped() -> None:
    base = "/my-files/a"
    assert relative(base, base) == ""
    assert relative(join(join(base, "x/y"), "z"), base) == "x/y/z"
    assert relative("/my-files/a", "/") == "my-files/a"


def test_resolve_target_remote() -> None:
    assert resolve_target("/my-files/a/", Path("/")) == Target("/my-files/a", None)
    assert resolve_target("/", Path("/")) == Target("/", None)
    assert resolve_target("/devices", Path("/")).remote == "/devices"


def test_resolve_target_local_path_outside_cwd(tmp_path: Path) -> None:
    store = _store(tmp_path)
    t = resolve_target(str(store / "mload005"), cwd=tmp_path)
    assert t == Target("/my-files/sph-dev/sim/mload005", store.resolve())


def test_resolve_target_store_root_itself(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert resolve_target(None, cwd=store) == Target("/my-files/sph-dev/sim", store.resolve())


def test_resolve_target_no_arg_in_store_subdir(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cwd = store / "a" / "b"
    cwd.mkdir(parents=True)
    t = resolve_target(None, cwd=cwd)
    assert t == Target("/my-files/sph-dev/sim/a/b", store.resolve())


def test_resolve_target_relative_arg(tmp_path: Path) -> None:
    store = _store(tmp_path)
    t = resolve_target("x/y", cwd=store)
    assert t.remote == "/my-files/sph-dev/sim/x/y"


def test_resolve_target_no_arg_outside_store(tmp_path: Path) -> None:
    with pytest.raises(TargetError, match=str(tmp_path)):
        resolve_target(None, cwd=tmp_path)


def test_resolve_target_local_not_in_store(tmp_path: Path) -> None:
    with pytest.raises(TargetError, match="nowhere"):
        resolve_target(str(tmp_path / "nowhere"), cwd=tmp_path)


def test_resolve_target_unsupported_section() -> None:
    with pytest.raises(TargetError, match="du supports /, /my-files, /devices"):
        resolve_target("/photos", Path("/"))
    with pytest.raises(TargetError, match="/trash/x"):
        resolve_target("/trash/x", Path("/"))
