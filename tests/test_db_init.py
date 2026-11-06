"""Tests for grossy init (PRD FR-1, FR-2, sections 12, 13).

Covers fresh installation, non-overwrite, permissions, schema metadata,
and process-lock location.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from grossy.db.connection import open_connection
from grossy.init import InitError, do_init
from grossy.paths import GrossyPaths


def _paths_in(tmp_path: Path) -> GrossyPaths:
    return GrossyPaths(
        config_dir=tmp_path / ".config" / "grossy",
        data_dir=tmp_path / ".local" / "share" / "grossy",
        state_dir=tmp_path / ".local" / "state" / "grossy",
    )


@pytest.fixture
def grossy_home(tmp_path: Path) -> GrossyPaths:
    """Provide isolated XDG paths under tmp_path."""
    return _paths_in(tmp_path)


class TestFreshInstall:
    @pytest.fixture(scope="class")
    @classmethod
    def grossy_home(cls, tmp_path_factory: pytest.TempPathFactory) -> GrossyPaths:
        """Initialize one read-only fresh install shared by this contract class."""
        paths = _paths_in(tmp_path_factory.mktemp("fresh-install"))
        do_init(paths)
        return paths

    @pytest.mark.parametrize(
        "directory",
        ["config_dir", "data_dir", "backups_dir", "state_dir", "browser_profile_dir"],
    )
    def test_creates_private_directories_with_0700(
        self, grossy_home: GrossyPaths, directory: str
    ) -> None:
        path = getattr(grossy_home, directory)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700

    @pytest.mark.parametrize("file", ["config_file", "database_file"])
    def test_writes_private_files_with_0600(self, grossy_home: GrossyPaths, file: str) -> None:
        path = getattr(grossy_home, file)
        assert path.exists()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_sample_config_is_schema_version_1_for_ab(self, grossy_home: GrossyPaths) -> None:
        content = grossy_home.config_file.read_text()
        assert "schema_version: 1" in content
        assert "retailer: ab" in content

    def test_database_records_migrations_with_wal_and_foreign_keys(
        self, grossy_home: GrossyPaths
    ) -> None:
        conn = open_connection(grossy_home.database_file)
        try:
            rows = conn.execute(
                "SELECT version, filename, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            assert len(rows) >= 1
            assert rows[0][0] == 1
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            conn.close()

    def test_creates_writer_lock_location(self, grossy_home: GrossyPaths) -> None:
        lock_path = grossy_home.state_dir / "writer.lock"
        assert lock_path.exists()

    def test_required_tables_created(self, grossy_home: GrossyPaths) -> None:
        conn = open_connection(grossy_home.database_file)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = {row[0] for row in cursor.fetchall()}
            expected = {
                "schema_migrations",
                "source_orders",
                "source_order_products",
                "products",
                "categories",
                "product_families",
                "family_members",
                "manual_overrides",
                "blacklist_events",
                "recipes",
                "recipe_revisions",
                "recipe_ingredients",
                "recipe_appliance_requirements",
                "recipe_variants",
                "recipe_events",
                "planner_runs",
                "run_cart_baselines",
                "run_recurring_items",
                "run_recipe_slots",
                "run_rerolls",
                "run_recipe_selections",
                "run_ingredient_targets",
                "run_mutation_manifests",
                "run_mutation_observations",
                "run_removals",
                "run_events",
                "exports",
            }
            assert expected.issubset(tables)
        finally:
            conn.close()

    def test_result_is_versioned_json_structure(self, tmp_path: Path) -> None:
        result = do_init(_paths_in(tmp_path))
        assert result["schema_name"] == "grossy.init.result"
        assert result["schema_version"] == 1
        assert result["status"] == "success"


class TestOnePrimaryMemberPerFamily:
    """Review finding 5: one primary member per family, with a consistent field."""

    NOW = "2026-09-26T00:00:00Z"

    @staticmethod
    def _setup_family_and_products(conn: sqlite3.Connection) -> tuple[int, int, int]:
        """Create three products and one family; return (family_id, product_a, product_b)."""
        for pid, name in ((1, "A"), (2, "B"), (3, "C")):
            conn.execute(
                "INSERT INTO products (retailer, retailer_product_id, canonical_url, name, "
                "created_at, updated_at) VALUES ('ab', ?, ?, ?, ?, ?)",
                (
                    f"p{pid}",
                    f"u{pid}",
                    name,
                    TestOnePrimaryMemberPerFamily.NOW,
                    TestOnePrimaryMemberPerFamily.NOW,
                ),
            )
        conn.execute(
            "INSERT INTO product_families (family_id, name, created_at, updated_at) "
            "VALUES ('fam1', 'Fam', ?, ?)",
            (TestOnePrimaryMemberPerFamily.NOW, TestOnePrimaryMemberPerFamily.NOW),
        )
        family_id = conn.execute("SELECT id FROM product_families").fetchone()[0]
        conn.execute("COMMIT")
        return family_id, 1, 2

    def test_two_primary_members_in_same_family_rejected(self, grossy_home: GrossyPaths) -> None:
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            family_id, a, b = self._setup_family_and_products(conn)
            conn.execute(
                "INSERT INTO family_members (family_id, product_id, role, created_at) "
                "VALUES (?, ?, 'primary', ?)",
                (family_id, a, self.NOW),
            )
            conn.execute("COMMIT")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO family_members (family_id, product_id, role, created_at) "
                    "VALUES (?, ?, 'primary', ?)",
                    (family_id, b, self.NOW),
                )
                conn.execute("COMMIT")
        finally:
            conn.close()

    def test_primary_field_consistent_on_demote_and_delete(self, grossy_home: GrossyPaths) -> None:
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            family_id, a, b = self._setup_family_and_products(conn)
            conn.execute(
                "INSERT INTO family_members (family_id, product_id, role, created_at) "
                "VALUES (?, ?, 'primary', ?)",
                (family_id, a, self.NOW),
            )
            conn.execute("COMMIT")
            assert (
                conn.execute("SELECT primary_product_id FROM product_families").fetchone()[0] == a
            )

            # Demote the (only) primary member -> field becomes NULL.
            conn.execute(
                "UPDATE family_members SET role = 'alternative' WHERE product_id = ?", (a,)
            )
            conn.execute("COMMIT")
            assert (
                conn.execute("SELECT primary_product_id FROM product_families").fetchone()[0]
                is None
            )

            # Promote another member -> field tracks the new primary.
            conn.execute(
                "INSERT INTO family_members (family_id, product_id, role, created_at) "
                "VALUES (?, ?, 'primary', ?)",
                (family_id, b, self.NOW),
            )
            conn.execute("COMMIT")
            assert (
                conn.execute("SELECT primary_product_id FROM product_families").fetchone()[0] == b
            )

            # Delete the (last) primary member -> field becomes NULL again.
            conn.execute("DELETE FROM family_members WHERE product_id = ?", (b,))
            conn.execute("COMMIT")
            assert (
                conn.execute("SELECT primary_product_id FROM product_families").fetchone()[0]
                is None
            )
        finally:
            conn.close()


class TestNonOverwrite:
    def test_refuses_existing_config(self, grossy_home: GrossyPaths) -> None:
        do_init(grossy_home)
        with pytest.raises(InitError, match="configuration already exists"):
            do_init(grossy_home)

    def test_refuses_existing_database(self, grossy_home: GrossyPaths) -> None:
        do_init(grossy_home)
        # Remove config so it's the database that triggers the error.
        grossy_home.config_file.unlink()
        with pytest.raises(InitError, match="database already exists"):
            do_init(grossy_home)

    def test_does_not_modify_existing_config_on_failure(self, grossy_home: GrossyPaths) -> None:
        do_init(grossy_home)
        original = grossy_home.config_file.read_text()
        with pytest.raises(InitError):
            do_init(grossy_home)
        assert grossy_home.config_file.read_text() == original

    def test_does_not_delete_existing_database_on_failure(self, grossy_home: GrossyPaths) -> None:
        do_init(grossy_home)
        db_bytes = grossy_home.database_file.read_bytes()
        assert len(db_bytes) > 0
        grossy_home.config_file.unlink()
        with pytest.raises(InitError):
            do_init(grossy_home)
        assert grossy_home.database_file.read_bytes() == db_bytes


class TestSchemaConstraints:
    def test_one_active_run_enforced(self, grossy_home: GrossyPaths) -> None:
        """PRD 12.1.8: at most one active weekly run."""
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            now = "2026-09-26T00:00:00.000000Z"
            conn.execute(
                "INSERT INTO planner_runs "
                "(run_id, seed, lifecycle, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("run-1", "seed-1", "planning", now, now),
            )
            conn.execute("COMMIT")
            with pytest.raises(sqlite3.IntegrityError, match="one_active_run"):
                conn.execute(
                    "INSERT INTO planner_runs "
                    "(run_id, seed, lifecycle, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("run-2", "seed-2", "planning", now, now),
                )
        finally:
            conn.close()

    def test_one_primary_per_family_enforced(self, grossy_home: GrossyPaths) -> None:
        """PRD 12.1.7: a product can be the primary of at most one family.

        Uses the correct flow: create family with NULL primary, then add the
        primary member (the trigger sets primary_product_id).
        """
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            now = "2026-09-26T00:00:00.000000Z"
            url = "https://store.example/en/eshop/x/y/p/123"
            conn.execute(
                "INSERT INTO products "
                "(retailer, retailer_product_id, canonical_url, name, "
                "created_at, updated_at) VALUES ('ab', '123', ?, 'Test', ?, ?)",
                (url, now, now),
            )
            # Create family with NULL primary (the correct flow).
            conn.execute(
                "INSERT INTO product_families "
                "(family_id, name, created_at, updated_at) "
                "VALUES ('fam-1', 'F1', ?, ?)",
                (now, now),
            )
            # Add the primary member; the trigger sets primary_product_id.
            conn.execute(
                "INSERT INTO family_members (family_id, product_id, role, created_at) "
                "VALUES (1, 1, 'primary', ?)",
                (now,),
            )
            conn.execute("COMMIT")
            # A second family cannot claim the same product as primary:
            # idx_one_family_per_product blocks the member insert.
            conn.execute(
                "INSERT INTO product_families "
                "(family_id, name, created_at, updated_at) "
                "VALUES ('fam-2', 'F2', ?, ?)",
                (now, now),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO family_members (family_id, product_id, role, created_at) "
                    "VALUES (2, 1, 'primary', ?)",
                    (now,),
                )
        finally:
            conn.close()

    def test_product_families_rejects_direct_primary_insert(self, grossy_home: GrossyPaths) -> None:
        """PRD 12.1.7: primary_product_id is derived from family_members; a
        direct INSERT with non-NULL primary_product_id is rejected."""
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            now = "2026-09-26T00:00:00.000000Z"
            url = "https://store.example/en/eshop/x/y/p/123"
            conn.execute(
                "INSERT INTO products "
                "(retailer, retailer_product_id, canonical_url, name, "
                "created_at, updated_at) VALUES ('ab', '123', ?, 'Test', ?, ?)",
                (url, now, now),
            )
            conn.execute("COMMIT")
            with pytest.raises(sqlite3.IntegrityError, match="derived from family_members"):
                conn.execute(
                    "INSERT INTO product_families "
                    "(family_id, name, primary_product_id, created_at, updated_at) "
                    "VALUES ('fam-1', 'F1', 1, ?, ?)",
                    (now, now),
                )
        finally:
            conn.close()

    def test_product_unique_by_retailer_and_id(self, grossy_home: GrossyPaths) -> None:
        """PRD 12.1.5: product identity unique by retailer and product ID."""
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            now = "2026-09-26T00:00:00.000000Z"
            url = "https://store.example/en/eshop/x/y/p/123"
            conn.execute(
                "INSERT INTO products "
                "(retailer, retailer_product_id, canonical_url, name, "
                "created_at, updated_at) VALUES ('ab', '123', ?, 'T', ?, ?)",
                (url, now, now),
            )
            conn.execute("COMMIT")
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                conn.execute(
                    "INSERT INTO products "
                    "(retailer, retailer_product_id, canonical_url, name, "
                    "created_at, updated_at) VALUES ('ab', '123', ?, 'D', ?, ?)",
                    (url, now, now),
                )
        finally:
            conn.close()

    def test_source_order_unique_by_retailer_and_id(self, grossy_home: GrossyPaths) -> None:
        """PRD 12.1.3: source order unique by retailer and source order ID."""
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            now = "2026-09-26T00:00:00.000000Z"
            conn.execute(
                "INSERT INTO source_orders "
                "(retailer, source_order_id, displayed_label, "
                "order_date, import_timestamp, created_at) "
                "VALUES ('ab', '42', '26/09/2026', '2026-09-26', ?, ?)",
                (now, now),
            )
            conn.execute("COMMIT")
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                conn.execute(
                    "INSERT INTO source_orders "
                    "(retailer, source_order_id, displayed_label, "
                    "order_date, import_timestamp, created_at) "
                    "VALUES ('ab', '42', '26/09/2026', '2026-09-26', ?, ?)",
                    (now, now),
                )
        finally:
            conn.close()

    def test_budget_cents_integer_value_round_trips(self, grossy_home: GrossyPaths) -> None:
        """An integer minor-unit budget value survives database persistence."""
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            now = "2026-09-26T00:00:00.000000Z"
            conn.execute(
                "INSERT INTO planner_runs "
                "(run_id, seed, lifecycle, budget_eur_cents, created_at, updated_at) "
                "VALUES (?, ?, ?, 10000, ?, ?)",
                ("run-1", "seed", "planning", now, now),
            )
            conn.execute("COMMIT")
            cursor = conn.execute("SELECT budget_eur_cents FROM planner_runs WHERE run_id='run-1'")
            assert cursor.fetchone()[0] == 10000
        finally:
            conn.close()

    def test_quantities_must_be_positive(self, grossy_home: GrossyPaths) -> None:
        """PRD 12.1.9: quantities must be positive."""
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            now = "2026-09-26T00:00:00.000000Z"
            url = "https://store.example/en/eshop/x/y/p/1"
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                conn.execute(
                    "INSERT INTO products "
                    "(retailer, retailer_product_id, canonical_url, name, "
                    "typical_quantity, created_at, updated_at) "
                    "VALUES ('ab', '1', ?, 'Neg', 0, ?, ?)",
                    (url, now, now),
                )
        finally:
            conn.close()

    def test_foreign_key_violation_detected(self, grossy_home: GrossyPaths) -> None:
        """PRD 12.1.1: foreign keys enabled, violations detected."""
        do_init(grossy_home)
        conn = open_connection(grossy_home.database_file)
        try:
            now = "2026-09-26T00:00:00.000000Z"
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                conn.execute(
                    "INSERT INTO family_members "
                    "(family_id, product_id, role, created_at) "
                    "VALUES (1, 999, 'primary', ?)",
                    (now,),
                )
        finally:
            conn.close()
