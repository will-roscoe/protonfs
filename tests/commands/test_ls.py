# tests/commands/test_ls.py
from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from protonfs.commands.ls import render_ls
from protonfs.config import init_config
from protonfs.context import load_context
from protonfs.drive import RemoteEntry
from protonfs.index import IndexEntry


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


def test_render_ls_remote_subpath_ignores_out_of_scope_index_entries(
    tmp_path: Path, make_fake_drive
) -> None:
    # Regression: a scoped `ls <subpath> --remote` must not list index entries outside
    # the subpath -- and in particular must never label a perfectly-synced out-of-scope
    # file "remote-deleted" just because the scoped walk didn't visit it.
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set(
        "run1/dump_0001",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="h",
            sha1="",
            remote_path="/my-files/test/run1/dump_0001",
            origin_device="d1",
            local_state="present",
            last_synced="2026-07-08T00:00:00+00:00",
        ),
    )
    ctx.drive = make_fake_drive(
        walk_by_root={"/my-files/test/run5": [RemoteEntry("dump_0002", is_dir=False, size=7)]}
    )

    buf = io.StringIO()
    render_ls(ctx, "run5", remote=True, trash=False, console=Console(file=buf, width=120))

    out = buf.getvalue()
    assert "run1/dump_0001" not in out  # out-of-scope entry not shown at all
    assert "remote-deleted" not in out  # and certainly not mislabelled deleted
    assert "run5/dump_0002" in out


# --- #97/#94: --dirs aggregation, --state filter, --format ------------------------------


def _entry(rel: str, size: int = 10, state: str = "metadata-only") -> IndexEntry:
    return IndexEntry(
        size=size,
        mtime=1.0,
        sha256="h",
        sha1="",
        remote_path=f"/my-files/test/{rel}",
        origin_device="d1",
        local_state=state,
        last_synced="2026-07-08T00:00:00+00:00",
    )


def _mixed_repo(tmp_path: Path):
    """Two dirs: run1 has one local file (5 bytes) + one offloaded (indexed 100);
    run2 has one offloaded (indexed 40). Plus a root-level local-only file."""
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "here").write_bytes(b"12345")
    ctx.index.set("run1/offloaded", _entry("run1/offloaded", size=100))
    ctx.index.set("run2/offloaded", _entry("run2/offloaded", size=40))
    (tmp_path / "rootfile").write_bytes(b"xy")
    return ctx


def test_collect_entries_state_filter(tmp_path: Path) -> None:
    from protonfs.commands.ls import collect_entries

    ctx = _mixed_repo(tmp_path)

    all_entries = collect_entries(ctx, None, remote=False)
    only_meta = collect_entries(ctx, None, remote=False, states=("metadata-only",))

    assert {e.state.value for e in all_entries} == {"local-only", "metadata-only"}
    assert [e.rel_path for e in only_meta] == ["run1/offloaded", "run2/offloaded"]


def test_summarize_dirs_counts_and_sizes(tmp_path: Path) -> None:
    from protonfs.commands.ls import collect_entries, summarize_dirs

    ctx = _mixed_repo(tmp_path)
    summaries = {s.path: s for s in summarize_dirs(ctx, collect_entries(ctx, None, False), None)}

    assert summaries["run1"].files == 2
    assert summaries["run1"].local_bytes == 5  # only the materialised file
    assert summaries["run1"].indexed_bytes == 100  # only the indexed (offloaded) one
    # apparent = per-file max(local, indexed): 5 (local-only "here") + 100 (offloaded) = 105.
    assert summaries["run1"].apparent_bytes == 105
    assert summaries["run1"].states == {"local-only": 1, "metadata-only": 1}
    assert summaries["run2"].indexed_bytes == 40
    assert summaries["run2"].apparent_bytes == 40  # offloaded-only: falls back to indexed
    assert summaries["."].files == 1  # rootfile groups under "."
    assert summaries["."].apparent_bytes == 2  # local-only rootfile: falls back to local


def test_summarize_dirs_relative_to_subpath(tmp_path: Path) -> None:
    from protonfs.commands.ls import collect_entries, summarize_dirs

    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("sim/a/f1", _entry("sim/a/f1"))
    ctx.index.set("sim/b/f2", _entry("sim/b/f2"))

    entries = collect_entries(ctx, "sim", remote=False)
    summaries = [s.path for s in summarize_dirs(ctx, entries, "sim")]

    assert summaries == ["a", "b"]  # children of sim, not "sim" itself


def test_human_size_units() -> None:
    from protonfs.commands.ls import human_size

    assert human_size(0) == "0 B"
    assert human_size(1536) == "1.5 KiB"
    assert human_size(3 * 1024**3) == "3.0 GiB"


def test_render_ls_dirs_json_format(tmp_path: Path) -> None:
    import json

    ctx = _mixed_repo(tmp_path)
    lines: list[str] = []
    render_ls(
        ctx, None, remote=False, trash=False,
        console=Console(file=io.StringIO(), width=120),
        dirs=True, fmt="json", echo=lines.append,
    )

    payload = json.loads(lines[0])
    run1 = next(d for d in payload if d["path"] == "run1")
    assert run1["files"] == 2 and run1["indexed_bytes"] == 100


def test_render_ls_plain_format_is_tab_separated(tmp_path: Path) -> None:
    ctx = _mixed_repo(tmp_path)
    lines: list[str] = []
    render_ls(
        ctx, None, remote=False, trash=False,
        console=Console(file=io.StringIO(), width=120),
        states=("local-only",), fmt="plain", echo=lines.append,
    )

    assert lines == ["rootfile\tlocal-only", "run1/here\tlocal-only"]


def test_cli_state_choices_match_syncstate() -> None:
    """Freshness guard: cli._STATE_CHOICES is spelled out literally (startup cost);
    it must track diff.SyncState exactly."""
    from protonfs.cli import _STATE_CHOICES
    from protonfs.diff import SyncState

    assert _STATE_CHOICES == tuple(s.value for s in SyncState)
