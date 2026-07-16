from __future__ import annotations

import json
from pathlib import Path

from protonfs import migrations as migrations_mod
from protonfs.config import init_config, load_local_config
from protonfs.ignore import ignore_path, include_path
from protonfs.index import IndexEntry, IndexStore
from protonfs.migrations import (
    CURRENT_LAYOUT_VERSION,
    MIGRATIONS,
    layout_version,
    pending_migrations,
    run_migrations,
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# --- fixture repos, one per historical layout ---------------------------------------------


def _make_0_2_0_repo(root: Path) -> None:
    """0.2.0-era: bare (unwrapped) index document, device_id embedded in the committed
    config.json, no control files backfilled at all."""
    _write_json(
        root / ".protonfs" / "config.json",
        {"remote_root": "/my-files/x", "device_id": "old-device-id"},
    )
    _write_json(
        root / ".protonfs" / "index.json",
        {
            "a/b": {
                "size": 1,
                "mtime": 1.0,
                "sha256": "abc",
                "remote_path": "/my-files/x/a/b",
                "origin_device": "old-device-id",
                "local_state": "present",
                "last_synced": "2020-01-01T00:00:00+00:00",
            }
        },
    )


def _make_mid_era_repo(root: Path) -> None:
    """Mid-era: index wrapped at schema v1 (no sha1 yet), device_id already relocated to
    config.local.json (#21), but control files still never backfilled."""
    _write_json(root / ".protonfs" / "config.json", {"remote_root": "/my-files/x"})
    _write_json(root / ".protonfs" / "config.local.json", {"device_id": "mid-device-id"})
    _write_json(
        root / ".protonfs" / "index.json",
        {
            "schema_version": 1,
            "entries": {
                "a/b": {
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "abc",
                    "remote_path": "/my-files/x/a/b",
                    "origin_device": "mid-device-id",
                    "local_state": "present",
                    "last_synced": "2021-01-01T00:00:00+00:00",
                }
            },
        },
    )


def _make_current_repo(root: Path) -> None:
    """A repo that's already fully current: created via the real `init_config` +
    `IndexStore`, so it exercises the exact on-disk shape today's code produces."""
    init_config(root, "/my-files/x")
    store = IndexStore(root)
    store.set(
        "a/b",
        IndexEntry(
            size=1,
            mtime=1.0,
            sha256="abc",
            sha1="def",
            remote_path="/my-files/x/a/b",
            origin_device="dev",
            local_state="present",
            last_synced="2026-01-01T00:00:00+00:00",
        ),
    )
    store.save()
    migrations_mod.write_git_control_files(root)
    from protonfs.ignore import init_ignore, init_include

    init_ignore(root)
    init_include(root)


# --- pending_migrations ---------------------------------------------------------------------


class TestPendingMigrations:
    def test_not_a_repo_returns_empty(self, tmp_path: Path) -> None:
        assert pending_migrations(tmp_path) == []

    def test_0_2_0_repo_has_all_migrations_pending(self, tmp_path: Path) -> None:
        _make_0_2_0_repo(tmp_path)
        pending = pending_migrations(tmp_path)
        ids = {m.id for m in pending}
        assert "index-schema-current" in ids
        assert "device-id-to-local-config" in ids
        assert "control-file-backfill" in ids
        # refresh-state-format is never pending (no legacy format exists)
        assert "refresh-state-format" not in ids

    def test_mid_era_repo_has_index_and_control_files_pending_only(self, tmp_path: Path) -> None:
        _make_mid_era_repo(tmp_path)
        pending = pending_migrations(tmp_path)
        ids = {m.id for m in pending}
        assert ids == {"index-schema-current", "control-file-backfill"}

    def test_current_repo_has_nothing_pending(self, tmp_path: Path) -> None:
        _make_current_repo(tmp_path)
        assert pending_migrations(tmp_path) == []

    def test_registry_order_is_stable_and_ascending_versions(self) -> None:
        versions = [m.version for m in MIGRATIONS]
        assert versions == sorted(versions)
        assert len(versions) == len(set(versions))


# --- run_migrations: dry-run makes no changes -----------------------------------------------


class TestRunMigrationsDryRun:
    def test_dry_run_reports_plan_without_changing_anything(self, tmp_path: Path) -> None:
        _make_0_2_0_repo(tmp_path)
        before_config = (tmp_path / ".protonfs" / "config.json").read_text()
        before_index = (tmp_path / ".protonfs" / "index.json").read_text()

        result = run_migrations(tmp_path, dry_run=True)

        assert result.dry_run is True
        assert set(result.applied) == {
            "index-schema-current",
            "device-id-to-local-config",
            "control-file-backfill",
        }
        # nothing on disk touched
        assert (tmp_path / ".protonfs" / "config.json").read_text() == before_config
        assert (tmp_path / ".protonfs" / "index.json").read_text() == before_index
        assert not (tmp_path / ".protonfs" / "config.local.json").exists()
        assert not ignore_path(tmp_path).exists()
        assert not include_path(tmp_path).exists()
        # layout version marker untouched
        assert layout_version(tmp_path) == 0

    def test_dry_run_on_current_repo_reports_nothing_pending(self, tmp_path: Path) -> None:
        _make_current_repo(tmp_path)
        result = run_migrations(tmp_path, dry_run=True)
        assert result.applied == []


# --- run_migrations: actually applies ---------------------------------------------------------


class TestRunMigrationsApply:
    def test_0_2_0_repo_migrates_fully(self, tmp_path: Path) -> None:
        _make_0_2_0_repo(tmp_path)

        result = run_migrations(tmp_path)

        assert set(result.applied) == {
            "index-schema-current",
            "device-id-to-local-config",
            "control-file-backfill",
        }
        assert result.layout_version_before == 0
        assert result.layout_version_after == CURRENT_LAYOUT_VERSION

        # device_id relocated
        shared = json.loads((tmp_path / ".protonfs" / "config.json").read_text())
        assert "device_id" not in shared
        local = load_local_config(tmp_path)
        assert local["device_id"] == "old-device-id"
        assert local["layout_version"] == CURRENT_LAYOUT_VERSION

        # index resaved at current schema
        on_disk_index = json.loads((tmp_path / ".protonfs" / "index.json").read_text())
        assert on_disk_index["schema_version"] == 2
        assert on_disk_index["entries"]["a/b"]["sha1"] == ""

        # control files backfilled
        assert ignore_path(tmp_path).exists()
        assert include_path(tmp_path).exists()
        assert (tmp_path / ".protonfs" / ".gitattributes").exists()
        assert (tmp_path / ".protonfs" / ".gitignore").exists()

        # nothing left pending
        assert pending_migrations(tmp_path) == []

    def test_mid_era_repo_migrates_remaining_steps_only(self, tmp_path: Path) -> None:
        _make_mid_era_repo(tmp_path)
        result = run_migrations(tmp_path)
        assert set(result.applied) == {"index-schema-current", "control-file-backfill"}

        local = load_local_config(tmp_path)
        assert local["device_id"] == "mid-device-id"  # untouched, already local
        assert local["layout_version"] == CURRENT_LAYOUT_VERSION

    def test_already_current_repo_applies_nothing(self, tmp_path: Path) -> None:
        _make_current_repo(tmp_path)
        result = run_migrations(tmp_path)
        assert result.applied == []
        assert result.layout_version_before == 0  # marker was never written by init helpers
        assert result.layout_version_after == CURRENT_LAYOUT_VERSION

    def test_not_a_repo_is_a_safe_noop(self, tmp_path: Path) -> None:
        result = run_migrations(tmp_path)
        assert result.applied == []
        assert not (tmp_path / ".protonfs").exists()


# --- idempotency: running twice produces the same end state -----------------------------------


class TestIdempotency:
    def test_running_twice_is_a_noop_the_second_time(self, tmp_path: Path) -> None:
        _make_0_2_0_repo(tmp_path)
        run_migrations(tmp_path)

        config_after_first = (tmp_path / ".protonfs" / "config.json").read_text()
        index_after_first = (tmp_path / ".protonfs" / "index.json").read_text()
        local_after_first = (tmp_path / ".protonfs" / "config.local.json").read_text()

        second = run_migrations(tmp_path)

        assert second.applied == []
        assert (tmp_path / ".protonfs" / "config.json").read_text() == config_after_first
        assert (tmp_path / ".protonfs" / "index.json").read_text() == index_after_first
        assert (tmp_path / ".protonfs" / "config.local.json").read_text() == local_after_first

    def test_mid_era_repo_idempotent(self, tmp_path: Path) -> None:
        _make_mid_era_repo(tmp_path)
        run_migrations(tmp_path)
        assert pending_migrations(tmp_path) == []
        second = run_migrations(tmp_path)
        assert second.applied == []

    def test_dry_run_then_apply_then_dry_run_again(self, tmp_path: Path) -> None:
        _make_0_2_0_repo(tmp_path)
        first_dry = run_migrations(tmp_path, dry_run=True)
        assert len(first_dry.applied) == 3

        run_migrations(tmp_path)

        second_dry = run_migrations(tmp_path, dry_run=True)
        assert second_dry.applied == []


# --- layout_version helper ---------------------------------------------------------------------


class TestLayoutVersion:
    def test_zero_when_never_recorded(self, tmp_path: Path) -> None:
        assert layout_version(tmp_path) == 0

    def test_reflects_marker_after_migration(self, tmp_path: Path) -> None:
        _make_0_2_0_repo(tmp_path)
        run_migrations(tmp_path)
        assert layout_version(tmp_path) == CURRENT_LAYOUT_VERSION
