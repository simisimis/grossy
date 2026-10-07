"""Tests for backup, export, and restore (PRD section 14).

Covers SQLite-safe backup, manifest/checksum, versioned export,
validated restore, confirmation requirement, atomicity, and
failure preservation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import grossy.db.backup as backup_mod
from grossy.db.backup import (
    BACKUP_MANIFEST_SCHEMA,
    EXPORT_SCHEMA,
    ExportError,
    RestoreError,
    db_backup,
    db_export,
    db_restore,
)
from grossy.db.connection import open_connection
from grossy.db.lock import LockHeldError, WriterLock
from grossy.db.migrations import discover_migrations, get_schema_version, run_migrations
from grossy.db.snapshot import SnapshotError
from grossy.init import do_init
from grossy.paths import GrossyPaths


@pytest.fixture
def grossy_home(tmp_path: Path) -> GrossyPaths:
    return GrossyPaths(
        config_dir=tmp_path / ".config" / "grossy",
        data_dir=tmp_path / ".local" / "share" / "grossy",
        state_dir=tmp_path / ".local" / "state" / "grossy",
    )


@pytest.fixture
def populated_db(grossy_home: GrossyPaths) -> Path:
    """Initialize and populate the database with sample data."""
    do_init(grossy_home)
    conn = open_connection(grossy_home.database_file)
    now = "2026-09-26T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO categories (name, display_name, created_at) VALUES (?, ?, ?)",
        ("breakfast", "Breakfast", now),
    )
    conn.execute(
        "INSERT INTO products (retailer, retailer_product_id, canonical_url, name, "
        "category_id, created_at, updated_at) VALUES ('ab', '123', "
        "'https://www.ab.gr/en/eshop/Dairy/Yogurt/p/123', 'Greek Yogurt', 1, ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO source_orders (retailer, source_order_id, displayed_label, order_date, "
        "import_timestamp, created_at) VALUES ('ab', '999', '25/09/2026', '2026-09-25', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO source_order_products (source_order_id, product_id, created_at) "
        "VALUES (1, 1, ?)",
        (now,),
    )
    conn.execute("UPDATE products SET purchase_order_count = 1 WHERE id = 1")
    conn.execute("COMMIT")
    conn.close()
    return grossy_home.database_file


class TestBackup:
    def test_backup_success_contract(
        self, populated_db: Path, tmp_path: Path, current_schema_version: int
    ) -> None:
        """One SQLite snapshot carries a private, checksummed manifest and data."""
        dest = tmp_path / "backup.sqlite3"
        result = db_backup(populated_db, dest)
        assert result == dest
        assert dest.exists()
        assert dest.stat().st_size > 0
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600

        manifest_path = dest.with_suffix(dest.suffix + ".manifest.json")
        assert manifest_path.exists()
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
        manifest = json.loads(manifest_path.read_text())
        assert manifest["schema_name"] == BACKUP_MANIFEST_SCHEMA
        assert manifest["schema_version_manifest"] == 1
        assert manifest["database_schema_version"] == current_schema_version
        assert manifest["checksum_sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()
        assert manifest["backup_at"]

        conn = sqlite3.connect(str(dest))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT name FROM products").fetchone()[0] == "Greek Yogurt"
            assert conn.execute("SELECT source_order_id FROM source_orders").fetchone()[0] == "999"
            assert (
                conn.execute("SELECT purchase_order_count FROM products WHERE id=1").fetchone()[0]
                == 1
            )
        finally:
            conn.close()

    def test_backup_creates_parent_directories(self, populated_db: Path, tmp_path: Path) -> None:
        dest = tmp_path / "deep" / "nested" / "backup.sqlite3"
        db_backup(populated_db, dest)
        assert dest.exists()


class TestExport:
    def test_export_success_contract(
        self, populated_db: Path, tmp_path: Path, current_schema_version: int
    ) -> None:
        """One private export contains its schema metadata and required data tables."""
        dest = tmp_path / "export.json"
        result = db_export(populated_db, dest)
        assert result == dest
        assert result.exists()
        assert stat.S_IMODE(result.stat().st_mode) == 0o600

        data = json.loads(result.read_text())
        assert data["schema_name"] == EXPORT_SCHEMA
        assert data["schema_version"] == 1
        assert data["database_schema_version"] == current_schema_version
        exported = data["data"]
        assert {
            "source_orders",
            "products",
            "product_families",
            "recipes",
            "planner_runs",
            "manual_overrides",
            "blacklist_events",
        }.issubset(exported)
        assert exported["source_orders"][0]["source_order_id"] == "999"
        assert exported["products"][0]["name"] == "Greek Yogurt"


class TestRestore:
    def test_restore_requires_confirmation(
        self,
        populated_db: Path,
        tmp_path: Path,
        grossy_home: GrossyPaths,
        current_schema_version: int,
    ) -> None:
        """PRD 14.3.7: restore requires explicit confirmation."""
        backup_path = tmp_path / "backup.sqlite3"
        db_backup(populated_db, backup_path)
        # Give the destination different contents so an unauthorized restore
        # cannot pass unnoticed by replacing it with identical backup data.
        conn = open_connection(populated_db)
        try:
            conn.execute("UPDATE products SET name='Changed Name' WHERE id=1")
            conn.commit()
        finally:
            conn.close()
        assert _product_name(populated_db) == "Changed Name"

        # Without --confirm, return preview information and preserve the
        # destination rather than restoring the backup's "Greek Yogurt" value.
        info = db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=False)
        assert info is not None
        assert info.source_schema_version == current_schema_version
        assert info.target_schema_version == current_schema_version
        assert _product_name(populated_db) == "Changed Name"

    def test_confirmed_restore_replaces_drift_with_verified_pre_restore_backup(
        self,
        populated_db: Path,
        tmp_path: Path,
        grossy_home: GrossyPaths,
        current_schema_version: int,
    ) -> None:
        """PRD 14.3.5: a confirmed restore first snapshots the live database, then
        restores the backup; repeating the same restore succeeds again."""
        backup_path = tmp_path / "backup.sqlite3"
        db_backup(populated_db, backup_path)
        conn = open_connection(populated_db)
        conn.execute("UPDATE products SET name='Changed Name' WHERE id=1")
        conn.execute("COMMIT")
        conn.close()
        pre_restore_before = set(populated_db.parent.glob("pre-restore-*"))

        info = db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=True)
        assert info is not None
        assert info.source_schema_version == current_schema_version
        assert _product_name(populated_db) == "Greek Yogurt"
        assert len(set(populated_db.parent.glob("pre-restore-*"))) > len(pre_restore_before)

        db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=True)
        assert _product_name(populated_db) == "Greek Yogurt"

    def test_restore_refuses_when_active_run_exists(
        self, populated_db: Path, tmp_path: Path, grossy_home: GrossyPaths
    ) -> None:
        """PRD 14.3.1: restore requires no active weekly run."""
        conn = open_connection(populated_db)
        now = "2026-09-26T00:00:00.000000Z"
        conn.execute(
            "INSERT INTO planner_runs (run_id, seed, lifecycle, created_at, updated_at) "
            "VALUES ('run-1', 'seed', 'planning', ?, ?)",
            (now, now),
        )
        conn.execute("COMMIT")
        conn.close()

        backup_path = tmp_path / "backup.sqlite3"
        db_backup(populated_db, backup_path)

        with pytest.raises(RestoreError, match="active weekly run"):
            db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=True)

    def test_restore_validates_checksum(
        self, populated_db: Path, tmp_path: Path, grossy_home: GrossyPaths
    ) -> None:
        """PRD 14.3.3: restore validates manifest checksum."""
        backup_path = tmp_path / "backup.sqlite3"
        db_backup(populated_db, backup_path)
        # Tamper with the backup.
        data = bytearray(backup_path.read_bytes())
        data[-1] ^= 0xFF
        backup_path.write_bytes(bytes(data))

        with pytest.raises(RestoreError, match="checksum mismatch"):
            db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=True)

    def test_restore_preserves_prior_on_failure(
        self, populated_db: Path, tmp_path: Path, grossy_home: GrossyPaths
    ) -> None:
        """PRD 14.3.10: preserve prior database if any step fails."""
        backup_path = tmp_path / "backup.sqlite3"
        db_backup(populated_db, backup_path)
        # Corrupt the backup so integrity check fails.
        backup_path.write_bytes(b"corrupted data that is not sqlite")

        with pytest.raises(RestoreError):
            db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=True)

        # Original database must still be intact.
        conn = open_connection(populated_db)
        cursor = conn.execute("SELECT name FROM products WHERE id=1")
        assert cursor.fetchone()[0] == "Greek Yogurt"
        conn.close()

    def test_restore_fails_on_missing_backup(
        self, populated_db: Path, tmp_path: Path, grossy_home: GrossyPaths
    ) -> None:
        missing = tmp_path / "nonexistent.sqlite3"
        with pytest.raises(RestoreError, match="not found"):
            db_restore(populated_db, missing, grossy_home.state_dir, confirm=True)


def _product_name(db: Path) -> str | None:
    conn = open_connection(db)
    try:
        row = conn.execute("SELECT name FROM products WHERE id=1").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class TestRestoreManifestValidation:
    """Review finding 1: restore requires a complete manifest with an exact checksum."""

    @pytest.mark.parametrize(
        "damage,match",
        [
            pytest.param(lambda m: None, "manifest not found", id="missing-manifest"),
            pytest.param(lambda m: {**m, "checksum_sha256": ""}, "checksum", id="empty-checksum"),
            pytest.param(
                lambda m: {k: v for k, v in m.items() if k != "checksum_sha256"},
                "checksum",
                id="missing-checksum-key",
            ),
            pytest.param(lambda m: "{ this is not valid json", "not valid JSON", id="malformed"),
            pytest.param(
                lambda m: {**m, "schema_name": "something.else"}, "schema_name", id="wrong-schema"
            ),
        ],
    )
    def test_restore_rejects_damaged_manifest(
        self,
        populated_db: Path,
        tmp_path: Path,
        grossy_home: GrossyPaths,
        damage: Callable[[dict[str, Any]], Any],
        match: str,
    ) -> None:
        backup_path = tmp_path / "backup.sqlite3"
        db_backup(populated_db, backup_path)
        manifest_path = backup_path.with_suffix(backup_path.suffix + ".manifest.json")
        damaged = damage(json.loads(manifest_path.read_text()))
        if damaged is None:
            manifest_path.unlink()
        else:
            manifest_path.write_text(damaged if isinstance(damaged, str) else json.dumps(damaged))
        with pytest.raises(RestoreError, match=match):
            db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=True)
        assert _product_name(populated_db) == "Greek Yogurt"


class TestRestoreLockOrdering:
    """Review finding 2: the writer lock is acquired before the no-active-run check."""

    def _seed_active_run(self, db: Path) -> None:
        conn = open_connection(db)
        now = "2026-09-26T00:00:00.000000Z"
        conn.execute(
            "INSERT INTO planner_runs (run_id, seed, lifecycle, created_at, updated_at) "
            "VALUES ('run-1', 'seed', 'planning', ?, ?)",
            (now, now),
        )
        conn.execute("COMMIT")
        conn.close()

    def test_restore_acquires_lock_before_active_run_check(
        self, populated_db: Path, tmp_path: Path, grossy_home: GrossyPaths
    ) -> None:
        self._seed_active_run(populated_db)
        backup_path = tmp_path / "backup.sqlite3"
        db_backup(populated_db, backup_path)
        # Hold the writer lock externally (a concurrent writer). db_restore must
        # try to acquire the lock FIRST and fail with LockHeldError -- proving
        # the no-active-run check happens under the lock, not before it. If the
        # old check-first ordering were used, this would raise RestoreError
        # ("active weekly run") without ever touching the lock.
        with WriterLock(grossy_home.state_dir), pytest.raises(LockHeldError):
            db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=True)


class TestExportFailureSafety:
    """Review finding 3: a query/I-O error aborts the export and preserves output."""

    def test_export_query_failure_fails_and_preserves_destination(
        self, populated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "export.json"
        dest.write_text('{"stale": "keep-me"}\n')
        stale = dest.read_bytes()
        real_rows = backup_mod._table_rows

        def failing(conn: sqlite3.Connection, name: str) -> list[dict[str, object]]:
            if name == "source_orders":
                raise sqlite3.OperationalError("simulated disk I/O error mid-export")
            return real_rows(conn, name)

        monkeypatch.setattr(backup_mod, "_table_rows", failing)
        with pytest.raises(sqlite3.OperationalError):
            db_export(populated_db, dest)
        # Existing destination must be preserved, not replaced with partial data.
        assert dest.read_bytes() == stale

    def test_export_io_error_is_not_masked(
        self, populated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "export.json"
        dest.write_text('{"stale": "keep-me"}\n')
        stale = dest.read_bytes()

        def boom(db_path: Path, dest_path: Path) -> Path:
            raise OSError("simulated write failure")

        monkeypatch.setattr(backup_mod, "atomic_write", boom)
        with pytest.raises(OSError):
            db_export(populated_db, dest)
        assert dest.read_bytes() == stale

    def test_export_rejects_schema_version_zero(self, populated_db: Path, tmp_path: Path) -> None:
        """Schema version 0 (missing/empty migration metadata) must be rejected
        and the existing destination preserved (PRD 14.2, 18)."""
        dest = tmp_path / "export.json"
        db_export(populated_db, dest)
        stale = dest.read_bytes()
        # Damage: remove schema_migrations to simulate a damaged database.
        conn = sqlite3.connect(str(populated_db), isolation_level=None)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE IF EXISTS schema_migrations")
        conn.close()
        with pytest.raises(ExportError, match="not a valid initialized"):
            db_export(populated_db, dest)
        # The valid export must be preserved.
        assert dest.read_bytes() == stale


class TestRestorePreRestoreBackup:
    """Review finding 4: the pre-restore backup is verified before activation."""

    def test_restore_aborts_if_pre_restore_backup_fails_verification(
        self,
        populated_db: Path,
        tmp_path: Path,
        grossy_home: GrossyPaths,
        monkeypatch: pytest.MonkeyPatch,
        current_schema_version: int,
    ) -> None:
        backup_path = tmp_path / "backup.sqlite3"
        db_backup(populated_db, backup_path)
        real_verify = backup_mod.verify_sqlite_snapshot

        def flaky_verify(path: Path) -> tuple[str, int]:
            # Only fail for the newly created pre-restore backup, so the source
            # backup validation succeeds and the failure is specifically in the
            # pre-restore snapshot verification.
            if path.name.startswith("pre-restore-"):
                raise SnapshotError("simulated corrupt pre-restore backup")
            return real_verify(path)

        monkeypatch.setattr(backup_mod, "verify_sqlite_snapshot", flaky_verify)
        # The SnapshotError is translated to a RestoreError before activation.
        with pytest.raises(RestoreError, match="pre-restore"):
            db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=True)
        # The active database must be unchanged.
        assert _product_name(populated_db) == "Greek Yogurt"
        assert get_schema_version(populated_db) == current_schema_version


class TestRestoreSchemaVersionComparison:
    """Review finding 6: restore compares against the installed max, not the DB."""

    @staticmethod
    def _max_supported() -> int:
        return max(m.version for m in discover_migrations())

    def test_restore_rejects_backup_newer_than_installed_max(
        self, populated_db: Path, tmp_path: Path, grossy_home: GrossyPaths
    ) -> None:
        # Craft a backup whose schema is newer than the code's max supported
        # version (e.g. a database produced by a future grossy build).
        future = self._max_supported() + 1
        backup_path = tmp_path / "future.sqlite3"
        src = open_connection(populated_db)
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        dst.close()
        src.close()
        conn = sqlite3.connect(str(backup_path))
        conn.execute(
            "INSERT INTO schema_migrations (version, filename, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (future, "999_future.sql", "0" * 64, "2031-01-01T00:00:00Z"),
        )
        conn.execute("COMMIT")
        conn.close()
        manifest = {
            "schema_name": BACKUP_MANIFEST_SCHEMA,
            "schema_version_manifest": 1,
            "database_schema_version": future,
            "checksum_sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
            "backup_at": "2031-01-01T00:00:00.000000Z",
            "source_database": str(populated_db),
        }
        backup_path.with_suffix(backup_path.suffix + ".manifest.json").write_text(
            json.dumps(manifest)
        )
        with pytest.raises(RestoreError, match="newer than"):
            db_restore(populated_db, backup_path, grossy_home.state_dir, confirm=True)

    def test_restore_accepts_backup_at_or_below_installed_max_even_if_db_is_older(
        self, tmp_path: Path, grossy_home: GrossyPaths, current_schema_version: int
    ) -> None:
        # The review's key regression: a backup at the installed max must be
        # accepted even when the current database is at an older version.
        real_migrations = discover_migrations()
        mig001 = next(m for m in real_migrations if m.version == 1)
        # Current DB: only the real migration 001 applied -> schema version 1,
        # but with the full 001 schema (planner_runs exists for the run check).
        migs_dir = tmp_path / "migs001"
        migs_dir.mkdir()
        (migs_dir / "001_initial.sql").write_text(mig001.sql)
        current_db = tmp_path / "old.sqlite3"
        run_migrations(current_db, tmp_path / "bk001", migrations_dir=migs_dir)
        assert get_schema_version(current_db) == 1  # current DB is older than the max
        # Backup source: the real full migrations -> schema version = installed max.
        max_supported = self._max_supported()
        assert max_supported == current_schema_version
        backup_source = tmp_path / "new.sqlite3"
        run_migrations(backup_source, tmp_path / "bk002")  # real migrations dir
        assert get_schema_version(backup_source) == max_supported
        backup_path = tmp_path / "backup.sqlite3"
        db_backup(backup_source, backup_path)
        # Restore the version-2 backup onto the version-1 database: allowed.
        info = db_restore(current_db, backup_path, grossy_home.state_dir, confirm=True)
        assert info is not None
        assert info.source_schema_version == max_supported
        assert get_schema_version(current_db) == max_supported


@pytest.mark.parametrize(
    "table", ["source_orders", "run_recipe_plans", "run_recipe_seen", "run_recipe_mappings"]
)
def test_export_missing_required_table_fails_and_preserves_destination(
    populated_db: Path, tmp_path: Path, table: str
) -> None:
    """A supported schema version missing a required table is damaged: the export
    fails rather than silently omitting data, creates no new file and never
    replaces an existing export."""
    dest = tmp_path / "export.json"
    db_export(populated_db, dest)
    before = dest.read_bytes()
    conn = sqlite3.connect(str(populated_db), isolation_level=None)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(f"DROP TABLE {table}")
    conn.close()
    fresh = tmp_path / "fresh-export.json"
    with pytest.raises(ExportError, match=f"required table '{table}'"):
        db_export(populated_db, fresh)
    assert not fresh.exists()
    with pytest.raises(ExportError, match=f"required table '{table}'"):
        db_export(populated_db, dest)
    assert dest.read_bytes() == before


@pytest.mark.parametrize("version", range(1, 11), ids=lambda version: f"schema{version}")
def test_export_prior_schema_retains_empty_optional_planning_evidence(
    tmp_path: Path, version: int
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    for migration in discover_migrations():
        if migration.version <= version:
            (migrations_dir / migration.filename).write_text(migration.sql)
    db = tmp_path / "prior.sqlite3"
    run_migrations(db, tmp_path / "backups", migrations_dir=migrations_dir)
    dest = tmp_path / "export.json"
    db_export(db, dest)
    data = json.loads(dest.read_text())
    assert data["database_schema_version"] == version
    assert data["data"]["run_recipe_plans"] == []
    assert data["data"]["run_recipe_seen"] == []
    assert data["data"]["run_recipe_mappings"] == []
    assert "run_recipe_slots" in data["data"]


def test_backup_private_at_sqlite_open_and_on_failure(
    populated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permissive umask cannot expose snapshots before the final chmod."""
    import os

    import grossy.db.snapshot as snapshot_mod

    dest = tmp_path / "private" / "snapshot.sqlite3"
    real_connect = sqlite3.connect
    opened: list[Path] = []

    def connect(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        path = Path(database)
        if path.parent == dest.parent and not database.startswith("file:"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            opened.append(path)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshot_mod.sqlite3, "connect", connect)
    old_umask = os.umask(0)
    try:
        db_backup(populated_db, dest)
    finally:
        os.umask(old_umask)
    assert opened and stat.S_IMODE(dest.parent.stat().st_mode) == 0o700
    before = dest.read_bytes()

    def fail(path: Path) -> tuple[str, int]:
        raise SnapshotError("synthetic snapshot verification failure")

    monkeypatch.setattr(snapshot_mod, "verify_sqlite_snapshot", fail)
    with pytest.raises(SnapshotError):
        db_backup(populated_db, dest)
    assert dest.read_bytes() == before
    assert not list(dest.parent.glob(".snapshot.sqlite3.*"))


@pytest.mark.parametrize("operation", [db_backup, db_export], ids=["backup", "export"])
def test_private_output_cannot_overwrite_active_database(
    populated_db: Path, tmp_path: Path, operation: object
) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(populated_db)
    with pytest.raises(ExportError, match="alias"):
        operation(populated_db, alias)
    assert _product_name(populated_db) == "Greek Yogurt"


def test_restore_rechecks_copied_checksum_before_activation(
    populated_db: Path, tmp_path: Path, grossy_home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.sqlite3"
    db_backup(populated_db, source)
    real_verify = backup_mod._verify_snapshot
    source_checks = 0

    def changed_after_check(path: Path) -> tuple[str, int]:
        nonlocal source_checks
        result = real_verify(path)
        if path == source:
            source_checks += 1
            if source_checks == 2:
                conn = sqlite3.connect(str(source))
                conn.execute("UPDATE products SET name='Tampered fixture' WHERE id=1")
                conn.commit()
                conn.close()
        return result

    monkeypatch.setattr(backup_mod, "_verify_snapshot", changed_after_check)
    with pytest.raises(RestoreError, match="changed during restore"):
        db_restore(populated_db, source, grossy_home.state_dir, confirm=True)
    assert _product_name(populated_db) == "Greek Yogurt"
    assert not populated_db.with_suffix(".restore-tmp").exists()


def test_isolated_backup_export_restore_report_regeneration(tmp_path: Path) -> None:
    """A fixture-completed run survives recovery with reports and private state."""
    from test_weekly_mutation import home as fixture_home
    from test_weekly_removal import added

    from grossy.weekly.closure import run_close, run_report_regenerate

    paths = fixture_home.__wrapped__(tmp_path / "isolated")
    run_id = added(paths)  # synthetic additive adapter only
    run_close(paths, run_id, no_recipes=True)
    directory = paths.run_dir(run_id)
    before = {name: (directory / name).read_bytes() for name in ("report.md", "report.json")}
    backup = tmp_path / "private-recovery" / "snapshot.sqlite3"
    export = backup.parent / "export.json"
    db_backup(paths.database_file, backup)
    db_export(paths.database_file, export)
    data = json.loads(export.read_text())["data"]
    assert data["planner_runs"][0]["lifecycle"] == "completed"
    assert data["weekly_reports"] and data["run_mutation_manifests"]
    assert "history_sync_reports" in data and "review_items" in data
    assert "cookies" not in data and "browser_state" not in data
    conn = open_connection(paths.database_file)
    conn.execute("UPDATE weekly_reports SET report_md='damaged fixture'")
    conn.commit()
    conn.close()
    db_restore(paths.database_file, backup, paths.state_dir, confirm=True)
    for name in before:
        (directory / name).unlink()
    run_report_regenerate(paths, run_id)
    for name, content in before.items():
        assert (directory / name).read_bytes() == content
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    for output in (backup, export, backup.with_suffix(".sqlite3.manifest.json")):
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
