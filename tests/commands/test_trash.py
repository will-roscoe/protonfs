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


def _render(ctx) -> str:
    buf = io.StringIO()
    list_trash(ctx, Console(file=buf, width=200))
    return buf.getvalue()


def _entry(name: str, parent: str | None, **extra) -> dict:
    entry = {"name": {"ok": True, "value": name}, "type": "file", **extra}
    if parent:
        entry["parentUid"] = parent
    return entry


def test_list_trash_shows_resolved_original_parent_path(tmp_path: Path, make_fake_drive) -> None:
    # #231: the original parent is a PATH found by walking the remote_root subtree.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[_entry("a.bin", "uid~p1")],
        folder_paths={"uid~p1": "/my-files/test/run1"},
    )

    assert "/my-files/test/run1" in _render(ctx)
    assert ctx.drive.find_folder_calls == [("/my-files/test", frozenset({"uid~p1"}))]


def test_list_trash_resolves_root_parents_without_a_walk(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[_entry("a.bin", "uid~root"), _entry("b.bin", "uid~repo")],
        node_uids={"/my-files": "uid~root", "/my-files/test": "uid~repo"},
    )

    out = _render(ctx)

    assert "/my-files " in out and "/my-files/test" in out
    assert ctx.drive.find_folder_calls == []


def test_list_trash_names_a_trashed_parent_folder(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    folder = {"name": {"ok": True, "value": "oldrun"}, "type": "folder", "uid": "uid~f"}
    ctx.drive = make_fake_drive(trash_listing=[folder, _entry("a.bin", "uid~f")])

    assert "(trashed folder oldrun)" in _render(ctx)


def test_list_trash_marks_parent_not_under_remote_root_when_search_complete(
    tmp_path: Path, make_fake_drive
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(trash_listing=[_entry("a.bin", "uid~other")])

    assert "(not under /my-files/test)" in _render(ctx)


def test_list_trash_announces_search_on_stderr_only_when_it_runs(
    tmp_path: Path, make_fake_drive, capsys
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[_entry("a.bin", "uid~p1"), _entry("b.bin", "uid~p2")]
    )

    _render(ctx)
    err = capsys.readouterr().err
    assert "Looking up 2 original folder(s) under /my-files/test (up to 2 min)" in err

    ctx.drive = make_fake_drive(
        trash_listing=[_entry("a.bin", "uid~root")], node_uids={"/my-files": "uid~root"}
    )
    _render(ctx)
    assert capsys.readouterr().err == ""


def test_list_trash_marks_incomplete_search_honestly(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[_entry("a.bin", "uid~other")], folder_search_complete=False
    )

    out = _render(ctx)

    assert "not found within search limit" in out
    assert "not under" not in out


def test_list_trash_entry_without_parent_uid_is_unknown(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(trash_listing=[_entry("a.bin", None)])

    assert "(unknown)" in _render(ctx)
    assert ctx.drive.find_folder_calls == []


def test_list_trash_searches_each_parent_uid_once(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive(
        trash_listing=[_entry("a.bin", "uid~p1"), _entry("b.bin", "uid~p1")],
        folder_paths={"uid~p1": "/my-files/test/run1"},
    )

    _render(ctx)

    assert len(ctx.drive.find_folder_calls) == 1


def test_empty_trash_with_yes_skips_prompt(tmp_path: Path, make_fake_drive) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()

    empty_trash(ctx, confirmed=True)

    assert ctx.drive.emptied_trash_calls == 1


def test_empty_trash_narrates_phase(
    tmp_path: Path, make_fake_drive, recording_reporter_cls
) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = make_fake_drive()
    rep = recording_reporter_cls()

    empty_trash(ctx, confirmed=True, reporter=rep)

    assert rep.calls == [("phase", "emptying trash")]


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
