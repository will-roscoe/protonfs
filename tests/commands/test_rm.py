from __future__ import annotations

from pathlib import Path

import click
import pytest

from protonfs.commands.rm import rm
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.index import IndexEntry


class _FakeDrive:
    def __init__(self, trash_listing: list[dict] | None = None) -> None:
        self.trashed: list[str] = []
        self.deleted: list[str] = []
        # what `list("/trash")` reports (defaults to one entry per basename trashed)
        self._trash_listing = trash_listing

    def trash(self, remote_paths: list[str]) -> list[dict]:
        self.trashed.extend(remote_paths)
        return [{"ok": True} for _ in remote_paths]

    def delete(self, remote_paths: list[str]) -> list[dict]:
        self.deleted.extend(remote_paths)
        return [{"ok": True} for _ in remote_paths]

    def list(self, remote_path: str) -> list[dict]:
        if self._trash_listing is not None:
            return self._trash_listing
        # default: each trashed path appears once in /trash by its basename
        from pathlib import PurePosixPath

        return [
            {"name": {"ok": True, "value": PurePosixPath(p).name}, "type": "file"}
            for p in self.trashed
        ]


def test_rm_trashes_and_removes_from_index(tmp_path: Path) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            remote_path="/my-files/test/dump_0001",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    fake = _FakeDrive()
    ctx.drive = fake

    rm(ctx, "dump_0001", recursive=False, force=False, confirmed=True)

    assert fake.trashed == ["/my-files/test/dump_0001"]
    assert fake.deleted == []
    assert ctx.index.get("dump_0001") is None


def test_rm_force_deletes_when_exactly_one_trash_match(tmp_path: Path) -> None:
    # D2.2: rm -f trashes, then permanently deletes only when exactly one item of
    # that basename is in /trash (unambiguous).
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = _FakeDrive()  # default listing: one entry per trashed basename
    ctx.drive = fake

    rm(ctx, "dump_0001", recursive=False, force=True, confirmed=True)

    assert fake.trashed == ["/my-files/test/dump_0001"]
    assert fake.deleted == ["/trash/dump_0001"]


def test_rm_force_duplicate_basename_leaves_trashed_and_warns(tmp_path: Path, capsys) -> None:
    # D2.2: with >1 items of the same basename in trash, protonfs cannot safely pick
    # which is the user's -> do NOT delete; leave it trashed (reversible) and warn.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = _FakeDrive(
        trash_listing=[
            {"name": {"ok": True, "value": "dump_0001"}, "type": "file"},
            {"name": {"ok": True, "value": "dump_0001"}, "type": "file"},
        ]
    )
    ctx.drive = fake

    rm(ctx, "dump_0001", recursive=False, force=True, confirmed=True)

    assert fake.trashed == ["/my-files/test/dump_0001"]
    assert fake.deleted == []  # not deleted -- ambiguous
    out = capsys.readouterr().out
    assert "dump_0001" in out
    assert "trash" in out.lower()


def test_rm_force_zero_trash_matches_informs_and_keeps_trashed(
    tmp_path: Path, capsys
) -> None:
    # D2.2 + cross-cutting messaging principle: if the trashed item can't be found in
    # /trash to permanently delete (stale listing / undecryptable name), don't delete
    # and don't stay silent -- tell the user it remains trashed/reversible.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    fake = _FakeDrive(trash_listing=[])  # nothing matches the basename
    ctx.drive = fake

    rm(ctx, "dump_0001", recursive=False, force=True, confirmed=True)

    assert fake.trashed == ["/my-files/test/dump_0001"]
    assert fake.deleted == []
    out = capsys.readouterr().out
    assert "dump_0001" in out
    assert "reversible" in out.lower()


def test_rm_directory_without_recursive_raises(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.drive = _FakeDrive()

    with pytest.raises(click.ClickException):
        rm(ctx, "run1", recursive=False, force=False, confirmed=True)


def test_rm_directory_with_recursive_removes_all_index_entries_under_it(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "run1/dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            remote_path="/my-files/test/run1/dump_0001",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.drive = _FakeDrive()

    rm(ctx, "run1", recursive=True, force=False, confirmed=True)

    assert ctx.index.get("run1/dump_0001") is None


def test_rm_unconfirmed_triggers_confirmation_prompt(tmp_path: Path, monkeypatch) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "somefile",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            remote_path="/my-files/test/somefile",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.drive = _FakeDrive()

    confirm_called = False

    def mock_confirm(message: str, abort: bool) -> bool:
        nonlocal confirm_called
        confirm_called = True
        return True

    monkeypatch.setattr("click.confirm", mock_confirm)

    rm(ctx, "somefile", recursive=False, force=False, confirmed=False)

    assert confirm_called


def test_rm_confirmed_does_not_trigger_confirmation_prompt(tmp_path: Path, monkeypatch) -> None:
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "somefile",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            remote_path="/my-files/test/somefile",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.drive = _FakeDrive()

    def mock_confirm_fail(message: str, abort: bool) -> bool:
        raise AssertionError("click.confirm should not be called when confirmed=True")

    monkeypatch.setattr("click.confirm", mock_confirm_fail)

    rm(ctx, "somefile", recursive=False, force=False, confirmed=True)

    assert ctx.index.get("somefile") is None


def test_rm_recursive_does_not_remove_sibling_directories(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "dump_0001").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "run1/dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            remote_path="/my-files/test/run1/dump_0001",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.index.set(
        "run10/other_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            remote_path="/my-files/test/run10/other_0001",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.drive = _FakeDrive()

    rm(ctx, "run1", recursive=True, force=False, confirmed=True)

    assert ctx.index.get("run1/dump_0001") is None
    assert ctx.index.get("run10/other_0001") is not None
