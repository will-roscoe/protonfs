from __future__ import annotations

from pathlib import Path

from protonfs.commands.status import compute_status
from protonfs.config import init_config
from protonfs.context import load_context


def test_compute_status_counts_local_only_and_synced(tmp_path: Path) -> None:
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "new_dump").write_bytes(b"data")
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)

    counts = compute_status(ctx, None)

    assert counts["local-only"] == 1
    assert counts.get("synced", 0) == 0
