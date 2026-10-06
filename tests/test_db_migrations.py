"""Tests for the Grossy migration system (PRD 12.2).

Covers checksums, pre-migration backup, transactional application,
rollback on failure, upgrade paths, and no-deletion behavior.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import grossy.db.migrations as migrations_mod
from grossy.db.backup import db_export
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


class TestRevisionBoundVariantUpgrade:
    """Upgrade from committed Task 06 history (schema 5) to revision-bound
    ``recipe_variants`` (schema 6; PRD 12.2.6, 12.2.10).

    A Task 06 database recorded the SHA-256 of the committed 001, whose
    ``recipe_variants`` was keyed by ``recipe_id``. Rewriting 001 in place
    made the runner reject every such database with
    ``MigrationChecksumError`` before any pending migration ran; the
    revision-bound conversion is therefore delivered as forward migration
    006, and 001 is pinned byte-for-byte to the committed file.
    """

    # SHA-256 of src/grossy/migrations/001_initial.sql as committed in the
    # Task 06 history (commit d42e99c). 001 must never be rewritten: applied
    # databases recorded this checksum and the runner verifies it first.
    COMMITTED_001_SHA256 = "38381d7ce04d1aa70f9a9ade8e78f1da7298c3221b5938aeb7222884f8ae809b"

    def test_migration_001_is_byte_identical_to_committed_task06_history(self) -> None:
        """Rewriting 001 would brick every Task 06 database (PRD 12.2.6)."""
        mig001 = next(m for m in discover_migrations() if m.version == 1)
        assert mig001.checksum == self.COMMITTED_001_SHA256

    def _build_task06_v5_database(self, tmp_path: Path) -> Path:
        """Create a database holding exactly the committed Task 06 history (001-005)."""
        db = tmp_path / "task06.sqlite3"
        migs_dir = tmp_path / "task06_migrations"
        migs_dir.mkdir()
        for mig in discover_migrations():
            if mig.version <= 5:
                (migs_dir / mig.filename).write_text(mig.sql)
        run_migrations(db, tmp_path / "bk", migrations_dir=migs_dir)
        assert get_schema_version(db) == 5
        return db

    def _seed_old_schema_variant_rows(self, db: Path) -> None:
        """Seed the pre-006 table exactly as the committed 001 schema allows."""
        conn = open_connection(db)
        try:
            now = "2026-10-01T00:00:00.000000Z"
            for row_id, recipe_id in ((1, "a"), (2, "b"), (3, "c")):
                conn.execute(
                    "INSERT INTO recipes (id, recipe_id, title, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row_id, recipe_id, f"Recipe {recipe_id}", now, now),
                )
            # Recipe a has a current revision (id 1); recipe c has a revision
            # (id 2) but no current_revision_id (legacy drift -> fallback).
            conn.execute(
                "INSERT INTO recipe_revisions (id, recipe_id, revision_number, servings, "
                "steps_json, accepted_at, created_at) VALUES (1, 1, 1, 3.0, '[]', ?, ?)",
                (now, now),
            )
            conn.execute("UPDATE recipes SET current_revision_id = 1 WHERE id = 1")
            conn.execute(
                "INSERT INTO recipe_revisions (id, recipe_id, revision_number, servings, "
                "steps_json, accepted_at, created_at) VALUES (2, 3, 1, 3.0, '[]', ?, ?)",
                (now, now),
            )
            # Old-schema rows keyed by the recipe's integer row id.
            conn.execute(
                "INSERT INTO recipe_variants (recipe_id, linked_recipe_id, variant_type, "
                "created_at) VALUES (1, 2, 'air_fryer', ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO recipe_variants (recipe_id, linked_recipe_id, variant_type, "
                "created_at) VALUES (3, 2, 'other', ?)",
                (now,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_upgrade_from_task06_history_maps_variant_rows_to_declaring_revisions(
        self, tmp_path: Path, current_schema_version: int
    ) -> None:
        """A committed-history v5 database upgrades to v6 with rows re-keyed.

        This also proves the checksum gate passes: if the working-tree 001
        diverged from the committed file, ``run_migrations`` would raise
        ``MigrationChecksumError`` before applying 006.
        """
        db = self._build_task06_v5_database(tmp_path)
        self._seed_old_schema_variant_rows(db)

        result = run_migrations(db, tmp_path / "bk2")
        assert result.applied_versions == [6, 7, 8, 9, 10]
        assert get_schema_version(db) == current_schema_version

        conn = open_connection(db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(recipe_variants)")}
            assert "revision_id" in columns
            assert "recipe_id" not in columns
            rows = conn.execute(
                "SELECT id, revision_id, linked_recipe_id, variant_type FROM recipe_variants "
                "ORDER BY id"
            ).fetchall()
            # Row 1 -> recipe a's current revision (id 1); row 2 -> recipe c's
            # latest revision (id 2) via the revision_number fallback.
            assert rows == [(1, 1, 2, "air_fryer"), (2, 2, 2, "other")]
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "recipe_variants_old" not in tables
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()

    def test_upgraded_database_accepts_revision_bound_variant_inserts(self, tmp_path: Path) -> None:
        """The task 07 write path works against an upgraded (not fresh) database."""
        from grossy.recipes.proposal import RecipeVariantLink
        from grossy.recipes.repository import insert_recipe_with_revision

        db = self._build_task06_v5_database(tmp_path)
        self._seed_old_schema_variant_rows(db)
        run_migrations(db, tmp_path / "bk2")

        conn = open_connection(db)
        try:
            now = "2026-10-02T00:00:00.000000Z"
            _, revision_row_id = insert_recipe_with_revision(
                conn,
                recipe_id="d",
                title="Recipe d",
                reference_url=None,
                source_name="user",
                source_tier="user",
                cuisine=None,
                protein_class="vegetarian",
                total_time_minutes=None,
                servings=3.0,
                steps=["step one"],
                ingredients=[],
                appliances=[],
                variants=[RecipeVariantLink(linked_recipe_id="a", variant_type="air_fryer")],
                base_weight=1.0,
                is_favorite=False,
                is_regular=True,
                enabled=True,
                provenance="upgrade-test",
                now=now,
            )
            conn.commit()
            row = conn.execute(
                "SELECT v.revision_id, l.recipe_id FROM recipe_variants v "
                "JOIN recipes l ON l.id = v.linked_recipe_id "
                "WHERE v.revision_id = ?",
                (revision_row_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == revision_row_id
            assert row[1] == "a"
        finally:
            conn.close()

    def test_upgrade_fails_when_variant_recipe_has_no_revision(self, tmp_path: Path) -> None:
        """Migration 006 must fail transactionally if a variant row references a
        recipe with no resolvable revision (PRD 12.2.9: no unauthorized
        destructive migrations).

        A schema-valid v5 database can contain a variant for a recipe that has
        neither current_revision_id nor any rows in recipe_revisions. The new
        schema requires revision_id NOT NULL, so such a row cannot be mapped.
        The migration must abort before destroying the old table, preserving
        all prior state.
        """
        db = self._build_task06_v5_database(tmp_path)

        # Seed a recipe with NO revision (no current_revision_id, no rows in
        # recipe_revisions) and a variant row referencing it. This is valid
        # under the 001 schema (recipe_variants.recipe_id REFERENCES recipes(id),
        # which does not require a revision to exist).
        conn = open_connection(db)
        try:
            now = "2026-10-01T00:00:00.000000Z"
            conn.execute(
                "INSERT INTO recipes (id, recipe_id, title, created_at, updated_at) "
                "VALUES (1, 'orphan', 'Orphan Recipe', ?, ?)",
                (now, now),
            )
            # A linked recipe (target of the variant).
            conn.execute(
                "INSERT INTO recipes (id, recipe_id, title, created_at, updated_at) "
                "VALUES (2, 'linked', 'Linked Recipe', ?, ?)",
                (now, now),
            )
            # Variant row: recipe 1 (no revision) -> recipe 2.
            conn.execute(
                "INSERT INTO recipe_variants (recipe_id, linked_recipe_id, variant_type, "
                "created_at) VALUES (1, 2, 'air_fryer', ?)",
                (now,),
            )
            conn.commit()
        finally:
            conn.close()

        # Migration 006 must fail.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            run_migrations(db, tmp_path / "bk2")

        # Verify prior state is fully preserved.
        conn = open_connection(db)
        try:
            # The old table still exists (not renamed, not dropped).
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "recipe_variants" in tables
            assert "recipe_variants_old" not in tables

            # The row values are intact.
            row = conn.execute(
                "SELECT id, recipe_id, linked_recipe_id, variant_type FROM recipe_variants"
            ).fetchone()
            assert row == (1, 1, 2, "air_fryer")

            # Foreign key state is clean.
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

            # schema_migrations has versions 1-5 but NOT 6.
            versions = sorted(
                row[0] for row in conn.execute("SELECT version FROM schema_migrations")
            )
            assert versions == [1, 2, 3, 4, 5]
        finally:
            conn.close()

    def test_upgrade_fails_when_current_revision_id_points_to_other_recipe_revision(
        self, tmp_path: Path
    ) -> None:
        """Migration 006 must reject a schema-valid v5 database where recipe A's
        current_revision_id points to a revision owned by recipe B (P1 review
        finding).

        In 001, recipes.current_revision_id is a bare INTEGER with no foreign
        key or ownership constraint. A corrupt or drifted row can reference
        another recipe's revision. Migration 006 must validate ownership and
        fall back to the declaring recipe's own revisions. If the declaring
        recipe has no revision of its own, the migration must abort
        transactionally, preserving all prior state.
        """
        db = self._build_task06_v5_database(tmp_path)

        conn = open_connection(db)
        try:
            now = "2026-10-01T00:00:00.000000Z"
            # Recipe A (id 1) — declaring recipe for the variant.
            conn.execute(
                "INSERT INTO recipes (id, recipe_id, title, created_at, updated_at) "
                "VALUES (1, 'a', 'Recipe A', ?, ?)",
                (now, now),
            )
            # Recipe B (id 2) — has a revision, is the linked target.
            conn.execute(
                "INSERT INTO recipes (id, recipe_id, title, created_at, updated_at) "
                "VALUES (2, 'b', 'Recipe B', ?, ?)",
                (now, now),
            )
            # Recipe B's revision (id 1, belongs to recipe B: recipe_id = 2).
            conn.execute(
                "INSERT INTO recipe_revisions (id, recipe_id, revision_number, servings, "
                "steps_json, accepted_at, created_at) VALUES (1, 2, 1, 3.0, '[]', ?, ?)",
                (now, now),
            )
            # Recipe A's current_revision_id incorrectly points to recipe B's
            # revision. This is schema-valid under 001 (bare INTEGER, no FK).
            conn.execute("UPDATE recipes SET current_revision_id = 1 WHERE id = 1")
            # Recipe A has NO revision of its own.
            # Variant row: recipe A (id 1) -> recipe B (id 2).
            conn.execute(
                "INSERT INTO recipe_variants (recipe_id, linked_recipe_id, variant_type, "
                "created_at) VALUES (1, 2, 'air_fryer', ?)",
                (now,),
            )
            conn.commit()
        finally:
            conn.close()

        # Migration 006 must fail: recipe A's current_revision_id points to
        # recipe B's revision (ownership mismatch), and recipe A has no own
        # revision for fallback.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            run_migrations(db, tmp_path / "bk2")

        # Verify prior state is fully preserved (atomic rollback).
        conn = open_connection(db)
        try:
            # The old table still exists with its original schema.
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "recipe_variants" in tables
            assert "recipe_variants_old" not in tables

            # The original column is still recipe_id (not revision_id).
            columns = {row[1] for row in conn.execute("PRAGMA table_info(recipe_variants)")}
            assert "recipe_id" in columns
            assert "revision_id" not in columns

            # The row values are intact.
            row = conn.execute(
                "SELECT id, recipe_id, linked_recipe_id, variant_type FROM recipe_variants"
            ).fetchone()
            assert row == (1, 1, 2, "air_fryer")

            # Recipe A's current_revision_id is unchanged.
            crid = conn.execute("SELECT current_revision_id FROM recipes WHERE id = 1").fetchone()
            assert crid[0] == 1

            # Foreign key state is clean.
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

            # schema_migrations has versions 1-5 but NOT 6.
            versions = sorted(
                row[0] for row in conn.execute("SELECT version FROM schema_migrations")
            )
            assert versions == [1, 2, 3, 4, 5]
        finally:
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


def test_populated_v7_upgrade_preserves_history_and_run_with_verified_backup(
    tmp_path: Path,
) -> None:
    """Task09 migration008 preserves predecessor data and adds report recovery."""
    from grossy.db.snapshot import verify_sqlite_snapshot

    migrations_dir = tmp_path / "v7-migrations"
    migrations_dir.mkdir()
    for migration in discover_migrations():
        if migration.version <= 7:
            (migrations_dir / migration.filename).write_text(migration.sql, encoding="utf-8")
    db = tmp_path / "v7.sqlite3"
    run_migrations(db, tmp_path / "initial-backups", migrations_dir=migrations_dir)
    assert get_schema_version(db) == 7
    now = "2026-10-01T00:00:00Z"
    conn = open_connection(db)
    try:
        conn.execute(
            "INSERT INTO products (id, retailer_product_id, canonical_url, name, "
            "purchase_order_count, created_at, updated_at) VALUES "
            "(1, '1111111', 'https://www.ab.gr/en/eshop/milk/p/1111111', 'Milk', 1, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO source_orders (id, source_order_id, displayed_label, order_date, "
            "import_timestamp, created_at) VALUES (1, '100001', '01/01/2026', '2026-01-01', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO source_order_products (source_order_id, product_id, created_at) "
            "VALUES (1, 1, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO planner_runs (id, run_id, seed, lifecycle, history_sync_status, "
            "budget_eur_cents, created_at, updated_at) "
            "VALUES (1, 'run-predecessor', 'seed', 'planning', NULL, 10000, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO run_cart_baselines "
            "(run_id, product_id, quantity, price_cents, created_at) "
            "VALUES (1, 1, 2, 250, ?)",
            (now,),
        )
        conn.commit()
        preserved_tables = (
            "products",
            "source_orders",
            "source_order_products",
            "planner_runs",
            "run_cart_baselines",
        )
        predecessor = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in preserved_tables
        }
    finally:
        conn.close()

    result = run_migrations(db, tmp_path / "upgrade-backups")
    assert result.applied_versions == [8, 9, 10]
    assert result.backup_path is not None
    assert result.backup_path.stat().st_mode & 0o777 == 0o600
    assert verify_sqlite_snapshot(result.backup_path)[1] == 7
    assert verify_sqlite_snapshot(db)[1] == 10
    for path in (db, result.backup_path):
        conn = open_connection(path)
        try:
            for table, expected in predecessor.items():
                assert conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == expected
            has_reports = conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'weekly_reports'"
            ).fetchone()
            assert (has_reports is not None) == (path == db)
        finally:
            conn.close()
    conn = open_connection(db)
    try:
        conn.execute(
            "INSERT INTO weekly_reports (run_id, report_md, report_json, created_at, updated_at) "
            "VALUES (1, 'preview', '{}', ?, ?)",
            (now, now),
        )
        conn.commit()
        assert conn.execute("SELECT report_written FROM weekly_reports").fetchone() == (0,)
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


@pytest.mark.parametrize("prior_version", range(1, 10), ids=lambda v: f"v{v}-to-v10")
def test_recipe_planning_upgrade_preserves_supported_schema(
    tmp_path: Path,
    prior_version: int,
) -> None:
    """Forward migrations preserve existing runs/slots/rerolls across supported upgrades."""
    from grossy.db.snapshot import verify_sqlite_snapshot

    prior = tmp_path / "prior-migrations"
    prior.mkdir()
    for migration in discover_migrations():
        if migration.version <= prior_version:
            (prior / migration.filename).write_text(migration.sql)
    db = tmp_path / "prior.sqlite3"
    run_migrations(db, tmp_path / "before", migrations_dir=prior)
    conn = open_connection(db)
    try:
        conn.execute(
            "INSERT INTO planner_runs (id,run_id,seed,lifecycle,created_at,updated_at) "
            "VALUES (1,'historical-run','historical-seed','completed','old','old')"
        )
        conn.execute(
            "INSERT INTO run_recipe_slots (run_id,slot_name,protein_class,eligibility_result,"
            "empty_reason,created_at) VALUES (1,'recipe1','meat','empty','old reason','old')"
        )
        conn.execute(
            "INSERT INTO run_rerolls (run_id,slot_name,reroll_number,candidate_scope,result,"
            "reason,created_at) VALUES (1,'recipe1',1,'meat','no_candidate','old reason','old')"
        )
        tables = ["planner_runs", "run_recipe_slots", "run_rerolls"]
        if prior_version >= 9:
            # Populated v9 planning snapshots must survive the v10 planner_runs rebuild.
            conn.execute(
                "INSERT INTO recipes (id,recipe_id,title,protein_class,created_at,updated_at) "
                "VALUES (1,'historical-recipe','Historical dinner','meat','old','old')"
            )
            conn.execute(
                "INSERT INTO run_recipe_plans (run_id,seed,proposal_checksum,"
                "proposal_generated_at,policy_json,candidates_json,exclusions_json,created_at) "
                "VALUES (1,'historical-seed',?,'old','{\"p\":1}','[1]','[]','old')",
                ("a" * 64,),
            )
            conn.execute(
                "INSERT INTO run_recipe_seen (run_id,recipe_id,first_shown_at) VALUES (1,1,'old')"
            )
            tables += ["run_recipe_plans", "run_recipe_seen"]
        conn.commit()
        expected = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    finally:
        conn.close()
    result = run_migrations(db, tmp_path / "after")
    assert result.applied_versions == list(range(prior_version + 1, 11))
    assert result.backup_path is not None
    assert verify_sqlite_snapshot(result.backup_path)[1] == prior_version
    assert verify_sqlite_snapshot(db)[1] == 10
    conn = open_connection(db)
    try:
        for table, rows in expected.items():
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
        if prior_version < 9:
            assert conn.execute("SELECT * FROM run_recipe_plans").fetchall() == []
            assert conn.execute("SELECT * FROM run_recipe_seen").fetchall() == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    if prior_version >= 9:
        dest = tmp_path / "export.json"
        db_export(db, dest)
        exported = json.loads(dest.read_text())["data"]
        assert [r["seed"] for r in exported["run_recipe_plans"]] == ["historical-seed"]
        assert [(r["run_id"], r["recipe_id"]) for r in exported["run_recipe_seen"]] == [(1, 1)]
        assert [r["lifecycle"] for r in exported["planner_runs"]] == ["completed"]
