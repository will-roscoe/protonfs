from __future__ import annotations

from pathlib import Path

import click
import pytest

from protonfs.config import init_config
from protonfs.context import load_context


def test_load_context_raises_when_no_config(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException):
        load_context(tmp_path)


def test_load_context_on_a_clone_not_set_up_here_reports_it_cleanly(tmp_path: Path) -> None:
    # #151: a clone has the committed config.json but not the gitignored config.local.json,
    # so no device_id resolves. That used to escape as a raw ValueError traceback from every
    # command; it must arrive as an actionable message naming the real remedy.
    init_config(tmp_path, "/my-files/test")
    (tmp_path / ".protonfs" / "config.local.json").unlink()

    with pytest.raises(click.ClickException) as caught:
        load_context(tmp_path)

    message = caught.value.format_message()
    assert "not on this machine yet" in message
    assert "protonfs setup" in message


def test_load_context_returns_populated_context(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    assert ctx.root == tmp_path.resolve()
    assert ctx.config.remote_root == "/my-files/test"
    assert ctx.index is not None
    assert ctx.drive is not None
