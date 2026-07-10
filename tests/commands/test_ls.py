# tests/commands/test_ls.py
from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from protonfs.commands.ls import render_ls
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import RemoteEntry


def test_render_ls_lists_local_only_file(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)

    buf = io.StringIO()
    render_ls(ctx, None, remote=False, trash=False, console=Console(file=buf, width=120))

    assert "run1/dump_0001" in buf.getvalue()
    assert "local-only" in buf.getvalue()


def test_render_ls_trash_lists_trashed_items(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[{"name": {"ok": True, "value": "trashed_item"}, "type": "file"}]
    )

    buf = io.StringIO()
    render_ls(ctx, None, remote=False, trash=True, console=Console(file=buf, width=120))

    assert "trashed_item" in buf.getvalue()


def test_render_ls_remote_includes_remote_only_files(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        walk_entries=[RemoteEntry("nested/remote_only.bin", is_dir=False, size=3)]
    )

    buf = io.StringIO()
    render_ls(ctx, None, remote=True, trash=False, console=Console(file=buf, width=120))

    assert "remote_only.bin" in buf.getvalue()
    assert "remote-only" in buf.getvalue()


def test_render_ls_remote_includes_nested_remote_only_files(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        walk_entries=[RemoteEntry("nested/remote_only.bin", is_dir=False, size=3)]
    )

    buf = io.StringIO()
    render_ls(ctx, None, remote=True, trash=False, console=Console(file=buf, width=120))

    out = buf.getvalue()
    assert "nested/remote_only.bin" in out
    assert "remote-only" in out


def test_render_ls_remote_scopes_walk_to_subpath(tmp_path: Path, make_fake_drive) -> None:
    # ls <subpath> --remote must scope the walk to remote_root/<subpath> and re-prefix
    # results (same convention as refresh), not list the entire remote_root.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = make_fake_drive(
        walk_by_root={
            "/my-files/test/run5": [RemoteEntry("dump_0002", is_dir=False, size=7)]
        }
    )
    ctx.drive = fake

    buf = io.StringIO()
    render_ls(ctx, "run5", remote=True, trash=False, console=Console(file=buf, width=120))

    out = buf.getvalue()
    assert fake.walk_roots == ["/my-files/test/run5"]  # scoped, not the full remote_root
    assert "run5/dump_0002" in out  # re-prefixed to a repo-root-relative path
    assert "remote-only" in out
