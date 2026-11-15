"""Live checks of ``protonfs du`` against the real Drive.

Gated on PROTONFS_TEST_REMOTE (a disposable remote directory, e.g. /my-files/test), like
``test_live_integration.py``. Everything happens in a unique ``pfs-live-du-<uuid12>``
subdir. Cleanup trashes it, then permanently deletes only the trashed nodes this test
created, each addressed as ``/trash/<unique name>`` and only after its uid matches the
one recorded at creation, because ``size`` keeps counting trashed bytes until they are
deleted. ``empty-trash`` is never called.

    PROTONFS_TEST_REMOTE=/my-files/test .venv/bin/python -m pytest tests/test_live_du.py -v
"""

from __future__ import annotations

import io
import os
import time
import uuid
from pathlib import Path

import pytest

from protonfs.drive import DriveClient, decrypted_name
from protonfs.usage.cache import UsageCache
from protonfs.usage.engine import UsageEngine
from protonfs.usage.excludes import ExcludeRules
from protonfs.usage.model import GapKind
from protonfs.usage.paths import Target
from protonfs.usage.policy import RefreshPolicy
from protonfs.usage.source import CliDriveSource

REMOTE = os.environ.get("PROTONFS_TEST_REMOTE")

pytestmark = pytest.mark.skipif(
    not REMOTE,
    reason="set PROTONFS_TEST_REMOTE to a throwaway Drive dir to run live tests",
)

FRESH = RefreshPolicy.parse("0")
NO_RULES = ExcludeRules((), ())


def _by_name(entries: list[dict]) -> dict[str, dict]:
    """Return raw listing entries keyed by decrypted name."""
    return {decrypted_name(e): e for e in entries}


@pytest.fixture
def live_du(tmp_path: Path):
    """A unique Drive dir plus an engine with a private cache.

    Yields ``(client, remote_dir, engine, created)`` where ``created`` maps the name of
    each node the test trashes to its uid, for the uid-checked permanent delete.
    """
    client = DriveClient()
    root = REMOTE.rstrip("/")
    name = f"pfs-live-du-{uuid.uuid4().hex[:12]}"
    client.create_folder(root, name)
    remote_dir = f"{root}/{name}"
    created = {name: _by_name(client.list(root))[name]["uid"]}
    engine = UsageEngine(CliDriveSource(client, concurrency=2), UsageCache(tmp_path / "du.sqlite"))
    try:
        yield client, remote_dir, engine, created
    finally:
        client.trash([remote_dir])
        trashed = _by_name(client.list("/trash"))
        doomed = [
            f"/trash/{n}"
            for n, uid in created.items()
            if uid is not None and trashed.get(n, {}).get("uid") == uid
        ]
        if doomed:
            client.delete(doomed)


def _settled_breakdown(engine: UsageEngine, remote_dir: str, want_gap_kind: GapKind):
    """Return a fresh breakdown, retrying briefly while Drive's totals settle."""
    bd = None
    for _ in range(6):
        bd = engine.breakdown(remote_dir, FRESH, NO_RULES, remote_dir)
        if bd.gap_kind is want_gap_kind:
            return bd
        time.sleep(5)
    return bd


def test_live_du_trash_gap(tmp_path: Path, live_du) -> None:
    client, remote_dir, engine, created = live_du
    tag = uuid.uuid4().hex[:12]
    src = tmp_path / "src"
    sub = src / f"sub-{tag}"
    sub.mkdir(parents=True)
    a = src / f"a-{tag}.bin"
    x = src / f"x[1]-{tag}.bin"
    b = sub / f"b-{tag}.bin"
    a.write_bytes(os.urandom(1024 * 1024))
    x.write_bytes(os.urandom(4 * 1024))
    b.write_bytes(os.urandom(2 * 1024 * 1024))
    client.upload([a, x, sub], remote_dir)

    listed = _by_name(client.list(remote_dir))
    assert set(listed) == {a.name, x.name, sub.name}
    created[a.name] = listed[a.name]["uid"]
    created[sub.name] = listed[sub.name]["uid"]
    b_size = client.list(f"{remote_dir}/{sub.name}")[0]["totalStorageSize"]

    # 1. the breakdown's file rows match the listing, with nothing left over
    bd = _settled_breakdown(engine, remote_dir, GapKind.NONE)
    assert bd.gap_kind is GapKind.NONE, (bd.gap, bd.gap_kind)
    rows = {e.name: e for e in bd.entries}
    for n in (a.name, x.name):
        assert rows[n].size == listed[n]["totalStorageSize"]
    assert rows[sub.name].size == b_size

    # 2. trashed children stay in the folder's total and show up as the trash remainder
    client.trash([f"{remote_dir}/{a.name}", f"{remote_dir}/{sub.name}"])
    bd = _settled_breakdown(engine, remote_dir, GapKind.TRASH)
    assert bd.gap_kind is GapKind.TRASH, (bd.gap, bd.gap_kind)
    assert bd.gap == listed[a.name]["totalStorageSize"] + b_size
    assert {e.name for e in bd.entries} == {x.name}

    # 3. the command prints the remainder row and exits 0
    from protonfs.commands.du import build_options, run_du

    out, err = io.StringIO(), io.StringIO()
    rc = run_du(
        [Target(remote_dir, None)],
        build_options(refresh="0", n_paths=1),
        engine_factory=lambda: engine,
        out=out,
        err=err,
    )
    assert rc == 0, err.getvalue()
    assert "(in trash)" in out.getvalue()
