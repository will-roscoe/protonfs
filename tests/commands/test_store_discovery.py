"""Every store-scoped command finds its store from a subdirectory (#232), with PATH
arguments relative to the current directory, as git pathspecs are."""
from __future__ import annotations

import os
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from protonfs.cli import _implicit_scope, main
from protonfs.config import init_config
from protonfs.context import load_context


@pytest.fixture
def store(tmp_path, monkeypatch, make_fake_drive):
    """A store at ``tmp_path/root`` with ``sim/`` and ``other/`` subdirectories, a fake
    Drive wired in under the real discovery, and the cwd left at the store root."""
    root = tmp_path / "root"
    (root / "sim" / "mload005").mkdir(parents=True)
    (root / "sim" / "mload006").mkdir()
    (root / "other").mkdir()
    (root / "sim" / "mload005" / "d1").write_bytes(b"one")
    (root / "sim" / "mload006" / "d1").write_bytes(b"two")
    (root / "other" / "x").write_bytes(b"x")
    init_config(root, "/my-files/test")
    drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.DriveClient", lambda **kw: drive)
    monkeypatch.chdir(root)
    return root, drive


def _run(*args: str):
    return CliRunner().invoke(main, list(args))


def test_load_context_finds_the_store_from_a_subdirectory(store) -> None:
    root, _ = store
    assert load_context(root / "sim" / "mload005").root == root.resolve()


def test_load_context_prefers_the_innermost_nested_store(store, make_fake_drive) -> None:
    root, _ = store
    init_config(root / "sim", "/my-files/inner")
    assert load_context(root / "sim" / "mload005").root == (root / "sim").resolve()
    assert load_context(root / "other").root == root.resolve()


def test_load_context_follows_a_symlinked_directory(store, tmp_path) -> None:
    root, _ = store
    link = tmp_path / "link"
    link.symlink_to(root / "sim")
    assert load_context(link).root == root.resolve()


def test_load_context_outside_any_store_says_so(tmp_path) -> None:
    with pytest.raises(click.ClickException, match="any parent directory"):
        load_context(tmp_path)


def test_config_get_works_from_a_subdirectory(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim" / "mload005")
    result = _run("config", "get", "remote_root")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "/my-files/test"


def test_config_set_writes_the_discovered_store(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("config", "set", "remote_root", "/my-files/moved")
    assert result.exit_code == 0, result.output
    assert not (root / "sim" / ".protonfs").exists()
    assert "/my-files/moved" in (root / ".protonfs" / "config.json").read_text()


def test_push_path_is_relative_to_the_current_directory(store, monkeypatch) -> None:
    root, drive = store
    monkeypatch.chdir(root / "sim")
    result = _run("push", "mload005")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload005/d1") is not None
    assert ctx.index.get("sim/mload006/d1") is None
    assert ctx.index.get("other/x") is None


def test_push_same_as_from_the_root(store, monkeypatch) -> None:
    root, drive = store
    monkeypatch.chdir(root / "sim" / "mload005")
    result = _run("push", ".")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload005/d1") is not None
    assert ctx.index.get("sim/mload006/d1") is None


def test_push_dotdot_reaches_a_sibling(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim" / "mload005")
    result = _run("push", "../mload006")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload006/d1") is not None
    assert ctx.index.get("sim/mload005/d1") is None


def test_push_absolute_path(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "other")
    result = _run("push", str(root / "sim" / "mload006"))
    assert result.exit_code == 0, result.output
    assert load_context(root).index.get("sim/mload006/d1") is not None


def test_push_quoted_glob_expands_from_the_current_directory(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("push", "mload00*")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload005/d1") is not None
    assert ctx.index.get("sim/mload006/d1") is not None
    assert ctx.index.get("other/x") is None


def test_push_outside_the_store_is_a_usage_error_naming_the_path(
    store, monkeypatch, tmp_path
) -> None:
    root, _ = store
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "f").write_bytes(b"f")
    monkeypatch.chdir(root / "sim")
    for arg in (str(outside / "f"), "../../elsewhere/f"):
        result = _run("push", arg)
        assert result.exit_code == 2, result.output
        assert arg in result.output
        assert "outside" in result.output


def test_push_missing_path_is_named_as_typed(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("push", "nope")
    assert result.exit_code == 2
    assert "'nope'" in result.output
    assert "sim/nope" not in result.output


def test_push_unmatched_pattern_is_named_as_typed(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("push", "zzz*")
    assert result.exit_code == 0, result.output
    assert "'zzz*'" in result.output
    assert "sim/" not in result.output


def test_pull_pattern_matches_the_index_relative_to_the_current_directory(
    store, monkeypatch
) -> None:
    root, _ = store
    assert _run("push", ".").exit_code == 0
    assert _run("offload", "--yes", "--min-age=0", ".").exit_code == 0
    assert not (root / "sim" / "mload005" / "d1").exists()
    monkeypatch.chdir(root / "sim")
    result = _run("pull", "mload005*")
    assert result.exit_code == 0, result.output
    assert (root / "sim" / "mload005" / "d1").exists()
    assert not (root / "sim" / "mload006" / "d1").exists()


def test_ls_prints_paths_as_typed_from_the_current_directory(store, monkeypatch) -> None:
    root, _ = store
    assert _run("push", ".").exit_code == 0
    monkeypatch.chdir(root / "sim")
    result = _run("ls", "--format", "plain", "mload005")
    assert result.exit_code == 0, result.output
    assert result.output.split("\t")[0] == "mload005/d1"


def test_ls_from_the_root_prints_store_relative_paths_unchanged(store) -> None:
    assert _run("push", ".").exit_code == 0
    result = _run("ls", "--format", "plain", "sim/mload005")
    assert result.output.split("\t")[0] == "sim/mload005/d1"


def test_deinit_from_a_subdirectory_names_the_root_to_run_it_from(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("deinit", "--yes")
    assert result.exit_code == 2, result.output
    assert str(root.resolve()) in result.output
    assert (root / ".protonfs" / "config.json").exists()


def test_setup_still_creates_a_store_where_you_are(store, monkeypatch) -> None:
    root, _ = store
    seen: list[Path] = []
    monkeypatch.setattr(
        "protonfs.commands.setup.run_setup", lambda cwd, **kw: seen.append(cwd)
    )
    monkeypatch.chdir(root / "other")
    _run("setup", "--dry-run")
    assert seen == [(root / "other").resolve()]


def test_rm_path_is_relative_to_the_current_directory(store, monkeypatch) -> None:
    root, drive = store
    assert _run("push", ".").exit_code == 0
    monkeypatch.chdir(root / "sim")
    result = _run("rm", "-r", "--yes", "mload005")
    assert result.exit_code == 0, result.output
    ctx = load_context(root)
    assert ctx.index.get("sim/mload005/d1") is None
    assert ctx.index.get("sim/mload006/d1") is not None


def test_implicit_scope_is_the_whole_store_for_now(store, monkeypatch) -> None:
    """The single place that decides what a command with no PATH covers from a
    subdirectory (#232); ``None`` is the whole store."""
    root, _ = store
    ctx = load_context(root)
    assert _implicit_scope(ctx, root / "sim") is None
    assert _implicit_scope(ctx, root) is None


def test_status_without_a_path_from_a_subdirectory(store, monkeypatch) -> None:
    root, _ = store
    monkeypatch.chdir(root / "sim")
    result = _run("status")
    assert result.exit_code == 1, result.output  # local-only files present: drift
    assert "local-only: 3" in result.output  # whole store, not just sim/


def test_symlinked_cwd_resolves_to_the_store(store, monkeypatch, tmp_path) -> None:
    root, _ = store
    link = tmp_path / "link"
    link.symlink_to(root / "sim")
    monkeypatch.chdir(link)
    assert os.getcwd() == str((root / "sim").resolve())
    result = _run("push", "mload005")
    assert result.exit_code == 0, result.output
    assert load_context(root).index.get("sim/mload005/d1") is not None
