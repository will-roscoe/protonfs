from __future__ import annotations

from pathlib import Path

import click
import pytest

from protonfs.config import init_config
from protonfs.context import load_context


def test_load_context_raises_when_no_config(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException):
        load_context(tmp_path)


def test_load_context_returns_populated_context(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    assert ctx.root == tmp_path.resolve()
    assert ctx.config.remote_root == "/my-files/test"
    assert ctx.index is not None
    assert ctx.drive is not None
