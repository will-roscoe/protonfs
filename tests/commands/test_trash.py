# tests/commands/test_trash.py
from __future__ import annotations

import io
from pathlib import Path

import click
import pytest
from rich.console import Console

from protonfs.commands.trash import empty_trash, list_trash
from protonfs.config import init_config
from protonfs.context import load_context


def test_list_trash_shows_names(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[{"name": {"ok": True, "value": "dump_0001"}, "type": "file"}]
    )

    buf = io.StringIO()
    list_trash(ctx, Console(file=buf, width=120))

    assert "dump_0001" in buf.getvalue()


def test_list_trash_skips_undecryptable_names(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(trash_listing=[{"name": {"ok": False}, "type": "file"}])

    buf = io.StringIO()
    list_trash(ctx, Console(file=buf, width=120))

    # No crash, and no stray row for the undecryptable entry.
    assert "None" not in buf.getvalue()


def test_list_trash_reports_duplicate_count(tmp_path: Path, make_fake_drive) -> None:
    # #56: same-named trash entries are exactly the ambiguity `restore` refuses to
    # resolve on its own -- `trash list` must surface it.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[
            {
                "name": {"ok": True, "value": "dup.bin"},
                "type": "file",
                "parentUid": "share~p1",
            },
            {
                "name": {"ok": True, "value": "dup.bin"},
                "type": "file",
                "parentUid": "share~p2",
            },
            {
                "name": {"ok": True, "value": "unique.bin"},
                "type": "file",
                "parentUid": "share~p1",
            },
        ]
    )

    buf = io.StringIO()
    list_trash(ctx, Console(file=buf, width=120))
    out = buf.getvalue()

    # Each dup.bin row reports one OTHER duplicate; unique.bin reports none (blank).
    assert out.count("dup.bin") == 2
    assert "1" in out  # duplicate count column for dup.bin rows


def test_list_trash_resolves_original_parent_when_available(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[
            {
                "name": {"ok": True, "value": "a.bin"},
                "type": "file",
                "parentUid": "share~p1",
            }
        ],
        parent_names={"share~p1": "run1"},
    )

    buf = io.StringIO()
    list_trash(ctx, Console(file=buf, width=120))

    assert "run1" in buf.getvalue()


def test_list_trash_shows_unresolved_parent_as_placeholder(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[
            {
                "name": {"ok": True, "value": "a.bin"},
                "type": "file",
                "parentUid": "share~unknown",
            }
        ],
        parent_names={},
    )

    buf = io.StringIO()
    list_trash(ctx, Console(file=buf, width=120))

    assert "?" in buf.getvalue()


def test_list_trash_caches_parent_lookups_per_uid(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[
            {"name": {"ok": True, "value": "a.bin"}, "type": "file", "parentUid": "share~p1"},
            {"name": {"ok": True, "value": "b.bin"}, "type": "file", "parentUid": "share~p1"},
        ],
        parent_names={"share~p1": "run1"},
    )

    list_trash(ctx, Console(file=io.StringIO(), width=120))

    assert ctx.drive.parent_name_calls == ["share~p1"]


def test_empty_trash_with_yes_skips_prompt(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    empty_trash(ctx, confirmed=True)

    assert ctx.drive.emptied_trash_calls == 1


def test_empty_trash_requires_typed_confirmation(
    tmp_path: Path, make_fake_drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    monkeypatch.setattr(click, "prompt", lambda *a, **k: "empty trash")

    empty_trash(ctx, confirmed=False)

    assert ctx.drive.emptied_trash_calls == 1


def test_empty_trash_aborts_on_wrong_confirmation_text(
    tmp_path: Path, make_fake_drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    monkeypatch.setattr(click, "prompt", lambda *a, **k: "nope")

    with pytest.raises(click.ClickException):
        empty_trash(ctx, confirmed=False)

    assert ctx.drive.emptied_trash_calls == 0
