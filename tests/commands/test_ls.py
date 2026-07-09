# tests/commands/test_ls.py
from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from protonfs.commands.ls import render_ls
from protonfs.config import init_config
from protonfs.context import load_context


class _FakeDrive:
    def list(self, remote_path: str) -> list[dict]:
        if remote_path == "/trash":
            return [{"name": {"ok": True, "value": "trashed_item"}, "type": "file"}]
        return [
            {
                "name": {"ok": True, "value": "remote_only.bin"},
                "type": "file",
                "totalStorageSize": 3,
            }
        ]


def test_render_ls_lists_local_only_file(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)

    buf = io.StringIO()
    render_ls(ctx, None, remote=False, trash=False, console=Console(file=buf, width=120))

    assert "run1/dump_0001" in buf.getvalue()
    assert "local-only" in buf.getvalue()


def test_render_ls_trash_lists_trashed_items(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = _FakeDrive()

    buf = io.StringIO()
    render_ls(ctx, None, remote=False, trash=True, console=Console(file=buf, width=120))

    assert "trashed_item" in buf.getvalue()


def test_render_ls_remote_includes_remote_only_files(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = _FakeDrive()

    buf = io.StringIO()
    render_ls(ctx, None, remote=True, trash=False, console=Console(file=buf, width=120))

    assert "remote_only.bin" in buf.getvalue()
    assert "remote-only" in buf.getvalue()
