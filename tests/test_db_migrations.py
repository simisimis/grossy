"""Tests for the Grossy migration system (PRD 12.2).

Covers checksums, pre-migration backup, transactional application,
rollback on failure, upgrade paths, and no-deletion behavior.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import grossy.db.migrations as migrations_mod
from grossy.db.connection import open_connection
from grossy.db.migrations import (
    MigrationChecksumError,
    discover_migrations,
    get_schema_version,
    run_migrations,
)
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


class TestMigrationDiscovery:
    def test_discovers_migration_files(self) -> None:
        migrations = discover_migrations()
        assert len(migrations) >= 1
        assert migrations[0].version == 1
        assert migrations[0].filename.startswith("001_")
        assert len(migrations[0].checksum) == 64  # SHA-256 hex

    def test_checksums_are_deterministic(self) -> None:
        m1 = discover_migrations()
        m2 = discover_migrations()
        assert len(m1) == len(m2)
        for a, b in zip(m1, m2, strict=True):
            assert a.checksum == b.checksum

    def test_sorted_by_version(self) -> None:
        migrations = discover_migrations()
        versions = [m.version for m in migrations]
        assert versions == sorted(versions)


class TestFreshInstall:
    def test_fresh_install_applies_all_migrations(
        self, grossy_home: GrossyPaths, current_schema_version: int
    ) -> None:
        do_init(grossy_home)
        assert get_schema_version(grossy_home.database_file) == current_schema_version

    def test_fresh_install_no_pre_migration_backup_on_empty(
        self, grossy_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """A brand new empty database gets its pre-migration backup during init."""
        do_init(grossy_home)
        # The init process should have created a pre-migration backup.
        backups = list(grossy_home.backups_dir.glob("pre-migration-*"))
        assert len(backups) >= 1

    def test_fresh_install_is_idempotent(self, grossy_home: GrossyPaths) -> None:
        do_init(grossy_home)
        # Running migrations again should be a no-op.
        result = run_migrations(grossy_home.database_file, grossy_home.backups_dir)
        assert result.applied_versions == []
        assert result.total_applied >= 1


class TestMigrationChecksums:
    def test_checksum_mismatch_stops_startup(
        self, grossy_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """PRD 12.2.6: a migration checksum mismatch must stop startup."""
        do_init(grossy_home)
        # Create a tampered migration directory.
        tampered_dir = tmp_path / "tampered_migrations"
        tampered_dir.mkdir()
        # Copy the real migration but with modified content.
        real_migrations = discover_migrations()
        tampered_sql = real_migrations[0].sql + "\n-- tampered\n"
        (tampered_dir / real_migrations[0].filename).write_text(tampered_sql)

        with pytest.raises(MigrationChecksumError):
            run_migrations(
                grossy_home.database_file,
                grossy_home.backups_dir,
                migrations_dir=tampered_dir,
            )


class TestPreMigrationBackup:
    def test_backup_created_before_first_pending(
        self, grossy_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """PRD 12.2.4: automatic backup before first pending migration."""
        # Start with a fresh database (no migrations applied).
        db_path = tmp_path / "test.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO test_table VALUES (42)")
        conn.execute("COMMIT")
        conn.close()

        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        result = run_migrations(db_path, backup_dir)
        assert result.backup_path is not None
        assert result.backup_path.exists()
        # Verify the backup contains the pre-migration data.
        backup_conn = sqlite3.connect(str(result.backup_path))
        cursor = backup_conn.execute("SELECT id FROM test_table")
        assert cursor.fetchone()[0] == 42
        backup_conn.close()

    def test_backup_has_0600_permissions(self, grossy_home: GrossyPaths, tmp_path: Path) -> None:
        import os
        import stat

        db_path = tmp_path / "test.sqlite3"
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.close()

        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        result = run_migrations(db_path, backup_dir)
        assert result.backup_path is not None
        mode = stat.S_IMODE(os.stat(result.backup_path).st_mode)
        assert mode == 0o600


class TestTransactionalApplication:
    def test_failed_migration_rolls_back(self, tmp_path: Path) -> None:
        """PRD 12.2.5: each migration must run transactionally.

        A migration that fails mid-way must leave the database in its
        prior state (the failing migration's changes are rolled back).
        """
        db_path = tmp_path / "test.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE existing (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO existing VALUES (1, 'original')")
        conn.execute("COMMIT")
        conn.close()

        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        # Migration 001: valid
        (migrations_dir / "001_add_table.sql").write_text(
            "CREATE TABLE new_table (id INTEGER PRIMARY KEY);"
        )
        # Migration 002: will fail (inserts into non-existent table)
        (migrations_dir / "002_fail.sql").write_text(
            "CREATE TABLE another_table (id INTEGER PRIMARY KEY);\n"
            "INSERT INTO nonexistent_table VALUES (1);\n"
        )

        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        with pytest.raises(sqlite3.OperationalError):
            run_migrations(db_path, backup_dir, migrations_dir=migrations_dir)

        # Verify migration 001 was applied (it succeeded).
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cursor.fetchall()}
        assert "new_table" in tables  # 001 applied
        assert "another_table" not in tables  # 002 rolled back
        # Original data preserved.
        cursor = conn.execute("SELECT val FROM existing WHERE id=1")
        assert cursor.fetchone()[0] == "original"
        conn.close()


class TestNoDeletionOrRecreation:
    def test_migration_failure_preserves_usable_database(self, tmp_path: Path) -> None:
        """PRD 12.2.7/12.2.8: failed migration preserves the usable database."""
        db_path = tmp_path / "test.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE persistent (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO persistent VALUES (1)")
        conn.execute("COMMIT")
        conn.close()

        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_fail.sql").write_text("INVALID SQL;\n")

        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        with pytest.raises(sqlite3.OperationalError):
            run_migrations(db_path, backup_dir, migrations_dir=migrations_dir)

        assert db_path.exists()
        conn = open_connection(db_path)
        try:
            assert conn.execute("SELECT id FROM persistent").fetchone()[0] == 1
        finally:
            conn.close()


class TestPreMigrationBackupVerification:
    """Review finding 4: the pre-migration backup is verified before applying."""

    def test_failed_pre_migration_backup_verification_aborts_before_changing_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A database with existing data and a pending migration.
        db_path = tmp_path / "app.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE persistent (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO persistent VALUES (1, 'keep-me')")
        conn.execute("COMMIT")
        conn.close()

        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_add.sql").write_text("CREATE TABLE added (id INTEGER);\n")

        # The pre-migration backup is created, but its verification fails, so no
        # migration may be applied and the prior database must remain usable.
        def bad_verify(path: Path) -> tuple[str, int]:
            raise SnapshotError("simulated corrupt pre-migration backup")

        monkeypatch.setattr(migrations_mod, "verify_sqlite_snapshot", bad_verify)
        with pytest.raises(SnapshotError):
            run_migrations(db_path, tmp_path / "backups", migrations_dir=migrations_dir)

        conn = sqlite3.connect(str(db_path))
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "added" not in tables  # migration was NOT applied
        # schema_migrations is auto-created (empty); no migration was recorded.
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
        assert conn.execute("SELECT v FROM persistent WHERE id=1").fetchone()[0] == "keep-me"
        conn.close()


class TestPrimaryFieldConsistency:
    """Review finding P2: primary_product_id is derived from family_members.

    Covers upgrade reconciliation and direct-update prevention.
    """

    def _create_v1_db_with_mismatch(self, tmp_path: Path) -> Path:
        """Create a v1 database with a mismatched primary_product_id."""
        db = tmp_path / "v1.sqlite3"
        migs_dir = tmp_path / "migs001"
        migs_dir.mkdir()
        real_migs = discover_migrations()
        mig001 = next(m for m in real_migs if m.version == 1)
        (migs_dir / mig001.filename).write_text(mig001.sql)
        run_migrations(db, tmp_path / "bk", migrations_dir=migs_dir)
        assert get_schema_version(db) == 1

        conn = open_connection(db)
        now = "2026-09-26T00:00:00.000000Z"
        prod_cols = "retailer, retailer_product_id, canonical_url, name, created_at, updated_at"
        conn.execute(
            f"INSERT INTO products ({prod_cols}) VALUES ('ab', 'p1', 'https://x/1', 'Prod1', ?, ?)",
            (now, now),
        )
        conn.execute(
            f"INSERT INTO products ({prod_cols}) VALUES ('ab', 'p2', 'https://x/2', 'Prod2', ?, ?)",
            (now, now),
        )
        fam_cols = "family_id, name, primary_product_id, created_at, updated_at"
        conn.execute(
            f"INSERT INTO product_families ({fam_cols}) VALUES ('fam1', 'Family1', 2, ?, ?)",
            (now, now),  # primary points to product 2 (mismatch)
        )
        # The actual primary member is product 1 (mismatch!)
        conn.execute(
            "INSERT INTO family_members (family_id, product_id, role, created_at) "
            "VALUES (1, 1, 'primary', ?)",
            (now,),
        )
        conn.execute("COMMIT")
        conn.close()
        return db

    def test_upgrade_reconciles_mismatched_primary(
        self, tmp_path: Path, current_schema_version: int
    ) -> None:
        """Upgrading from v1 to v2 fixes a mismatched primary_product_id."""
        db = self._create_v1_db_with_mismatch(tmp_path)
        # Verify the mismatch exists before upgrade.
        conn = open_connection(db)
        row = conn.execute("SELECT primary_product_id FROM product_families WHERE id=1").fetchone()
        assert row[0] == 2  # wrong: points to product 2
        conn.close()

        # Upgrade (real migrations dir includes 001 through 004).
        run_migrations(db, tmp_path / "bk2")
        assert get_schema_version(db) == current_schema_version

        # After upgrade, primary_product_id must match the actual primary member.
        conn = open_connection(db)
        row = conn.execute("SELECT primary_product_id FROM product_families WHERE id=1").fetchone()
        assert row[0] == 1  # corrected to match family_members
        conn.close()

    def test_direct_update_of_primary_field_is_rejected(
        self, tmp_path: Path, current_schema_version: int
    ) -> None:
        """A direct UPDATE of primary_product_id disagreeing with the member is rejected."""
        db = self._create_v1_db_with_mismatch(tmp_path)
        run_migrations(db, tmp_path / "bk2")
        assert get_schema_version(db) == current_schema_version

        conn = open_connection(db)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="derived from family_members"):
                conn.execute("UPDATE product_families SET primary_product_id = 999 WHERE id = 1")
        finally:
            conn.close()

    def test_family_members_trigger_still_works_after_upgrade(self, tmp_path: Path) -> None:
        """Adding a primary member still updates primary_product_id after v2."""
        db = self._create_v1_db_with_mismatch(tmp_path)
        run_migrations(db, tmp_path / "bk2")

        conn = open_connection(db)
        now = "2026-09-26T00:00:00.000000Z"
        # Add another product and make it the new primary by changing the member role.
        conn.execute("UPDATE family_members SET role = 'alternative' WHERE id = 1")
        conn.execute(
            "INSERT INTO family_members (family_id, product_id, role, created_at) "
            "VALUES (1, 2, 'primary', ?)",
            (now,),
        )
        conn.execute("COMMIT")
        row = conn.execute("SELECT primary_product_id FROM product_families WHERE id=1").fetchone()
        assert row[0] == 2  # updated to the new primary member
        conn.close()
