from __future__ import annotations

from pathlib import Path

import pytest

from protonfs.config import Config, save_config
from protonfs.usage.paths import (
    Target,
    TargetError,
    child_path,
    escape_name,
    is_remote,
    is_within,
    join,
    node_segment,
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


def test_split_drops_empty_segments() -> None:
    assert split("/a//b") == ["a", "b"]
    assert split("/a/b/") == ["a", "b"]
    assert split("/") == []


def test_resolve_target_normalises_remote() -> None:
    assert resolve_target("/my-files//x/", Path("/")).remote == "/my-files/x"


def test_relative_doubled_slash() -> None:
    assert relative("/my-files/a//b", "/my-files/a") == "b"


def test_resolve_target_symlink_out_of_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / "link").symlink_to(outside)
    with pytest.raises(TargetError, match="link"):
        resolve_target(str(store / "link" / "x"), cwd=tmp_path)


def test_node_segment_and_child_path() -> None:
    assert node_segment("a/b", "u1", True) == "a/b"
    assert node_segment("garbled", "u1", False) == "u1"
    assert node_segment("garbled", None, False) == "garbled"
    assert child_path("/my-files/F", "a/b", "u1", True) == "/my-files/F/a\\/b"
    assert child_path("/devices", "x", "u2", False) == "/devices/u2"
    assert child_path("/", "my-files", None, True) == "/my-files"


def _lister(tree):
    """A ``children(path)`` over ``{parent: [(name, kind)]}`` that records the paths listed."""
    from types import SimpleNamespace

    calls = []

    def children(path):
        calls.append(path)
        return [SimpleNamespace(name=n, kind=k, uid=f"u-{n}", decrypted=True)
                for n, k in tree.get(path, [])]

    return children, calls


def test_expand_pattern_lists_only_the_literal_parent():
    from protonfs.usage.paths import expand_pattern

    children, calls = _lister({"/my-files/sim": [
        ("mload005a", "folder"), ("mload005b", "folder"), ("other", "folder"),
        ("mload005.log", "file")]})
    assert expand_pattern("/my-files/sim/mload005*", children) == [
        "/my-files/sim/mload005.log", "/my-files/sim/mload005a", "/my-files/sim/mload005b"]
    assert calls == ["/my-files/sim"]


def test_expand_pattern_in_a_middle_segment_lists_level_by_level():
    from protonfs.usage.paths import expand_pattern

    children, calls = _lister({
        "/my-files": [("a1", "folder"), ("a2", "folder"), ("b", "folder"), ("a3", "file")],
        "/my-files/a1": [("run", "folder")], "/my-files/a2": [("x", "folder")]})
    assert expand_pattern("/my-files/a*/run", children) == ["/my-files/a1/run"]
    assert calls == ["/my-files", "/my-files/a1", "/my-files/a2"]


def test_expand_pattern_reads_an_existing_name_literally_first():
    from protonfs.usage.paths import expand_pattern

    children, _ = _lister({"/my-files": [("run[7]", "folder"), ("run7", "folder")]})
    assert expand_pattern("/my-files/run[7]", children) == ["/my-files/run[7]"]
    children, _ = _lister({"/my-files": [("run7", "folder")]})
    assert expand_pattern("/my-files/run[7]", children) == ["/my-files/run7"]


def test_expand_pattern_escapes_matched_names_and_finds_nothing():
    from protonfs.usage.paths import expand_pattern

    children, _ = _lister({"/my-files": [("a/b", "folder")]})
    assert expand_pattern("/my-files/a*", children) == ["/my-files/a\\/b"]
    assert expand_pattern("/my-files/z*", children) == []
