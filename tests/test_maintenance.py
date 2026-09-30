"""Tests for manual corrections, categories, and blacklists (task 06).

Covers the PRD unit-test requirements of section 19.1 and the task-06
acceptance criteria:

- every manual correction is explicit, auditable, transactional, and
  survives synchronization;
- product blacklists block exact products; family blacklists block all
  members everywhere;
- disabling suppresses learned recurring prefill without blocking explicit
  YAML or selected recipes unless a blacklist applies;
- invalid link, merge, category, or primary operations leave state unchanged;
- no synchronization clears a correction or blacklist.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from grossy.config import (
    AppliancePreferencesModel,
    AppliancesModel,
    GrossyConfigModel,
    HouseholdModel,
    StapleModel,
    StaplesModel,
)
from grossy.config.models import (
    AirFryerModel,
    DutchOvenModel,
    MicrowaveModel,
    PressureCookerModel,
    SimpleApplianceModel,
    ThermomixModel,
)
from grossy.db.connection import open_connection
from grossy.history import (
    run_sync_apply,
    select_family_product,
)
from grossy.history.maintenance import (
    MaintenanceError,
    run_blacklist_family,
    run_blacklist_product,
    run_category_rename,
    run_disable,
    run_enable,
    run_family_link,
    run_family_merge,
    run_family_unlink,
    run_set_category,
    run_set_primary,
    run_set_quantity,
    run_set_recurrence,
    run_unblacklist_family,
    run_unblacklist_product,
)
from grossy.init import do_init
from grossy.paths import GrossyPaths

GENERATED_AT = "2026-09-26T12:00:00+00:00"
NOW = "2026-09-26T12:00:00.000000Z"


# --- helpers -----------------------------------------------------------------


def _label(iso_date: str) -> str:
    year, month, day = iso_date.split("-")
    return f"{day}/{month}/{year}"


def _product_url(product_id: str) -> str:
    return f"https://www.ab.gr/en/eshop/Food/item-{product_id}/p/{product_id}"


def _scanned_product(
    product_id: str, *, title: str | None = None, category: str | None = "Dairy"
) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "canonical_url": _product_url(product_id),
        "title": title or f"Product {product_id}",
        "brand": None,
        "category_heading": category,
    }


def _scanned_order(
    source_order_id: str, order_date: str, product_ids: list[str], category: str | None = "Dairy"
) -> dict[str, Any]:
    return {
        "source_order_id": source_order_id,
        "displayed_label": _label(order_date),
        "order_date": order_date,
        "navigation_url": f"https://www.ab.gr/orders/{source_order_id}",
        "displayed_product_count": len(product_ids),
        "products": [_scanned_product(pid, category=category) for pid in product_ids],
    }


def _scan_payload(orders: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_name": "grossy.history.order-scan.v1",
        "schema_version": 1,
        "generated_at": GENERATED_AT,
        "orders": orders,
    }


def _paths_at(config_root: Path, data_root: Path, state_root: Path) -> GrossyPaths:
    return GrossyPaths(
        config_dir=config_root / "grossy",
        data_dir=data_root / "grossy",
        state_dir=state_root / "grossy",
    )


@pytest.fixture()
def tmp_env(tmp_path: Path) -> tuple[Path, GrossyPaths]:
    """Create an initialized environment with two orders and two products."""
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    paths = _paths_at(config_root, data_root, state_root)
    do_init(paths)

    # Import two orders with products "100" and "200".
    orders = [
        _scanned_order("10001", "2026-09-01", ["100", "200"]),
        _scanned_order("10002", "2026-09-08", ["100", "200"]),
    ]
    payload = _scan_payload(orders)
    proposal_path = state_root / "proposal.json"
    state_root.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")
    run_sync_apply(proposal_path, paths)

    return tmp_path, paths


def _apply_sync_order(
    paths: GrossyPaths,
    order_id: str,
    date: str,
    product_ids: list[str],
    category: str | None = "Dairy",
) -> None:
    """Apply a single-order sync proposal to the initialized environment."""
    orders = [_scanned_order(order_id, date, product_ids, category=category)]
    payload = _scan_payload(orders)
    proposal_path = paths.state_dir / "proposal2.json"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")
    run_sync_apply(proposal_path, paths)


# --- set-primary ---------------------------------------------------------------


class TestSetPrimary:
    def test_set_primary_success(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        # After sync, product "100" has family "fam-100" and is its own primary.
        # Link product "200" into family "fam-100", then set "200" as primary.
        run_family_link(paths, "200", "fam-100")
        result = run_set_primary(paths, "fam-100", "200")
        assert result["status"] == "success"
        assert result["new_primary_product_id"] == "200"

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT m.role FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "AND p.retailer_product_id = '200'"
            ).fetchone()
            assert row is not None
            assert row[0] == "primary"
        finally:
            c.close()

    def test_set_primary_already_primary_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="already the primary"):
            run_set_primary(paths, "fam-100", "100")

    def test_set_primary_not_a_member_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        # Product "200" is not a member of "fam-100" yet.
        with pytest.raises(MaintenanceError, match="not a member"):
            run_set_primary(paths, "fam-100", "200")

    def test_set_primary_unknown_family_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown family"):
            run_set_primary(paths, "nonexistent", "100")

    def test_set_primary_unknown_product_rejected(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Early validation: an unknown product is rejected before any write."""
        _, paths = tmp_env
        c = open_connection(paths.database_file)
        try:
            before = c.execute(
                "SELECT product_id, role FROM family_members "
                "WHERE family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "ORDER BY product_id"
            ).fetchall()
            with pytest.raises(MaintenanceError, match="unknown product"):
                run_set_primary(paths, "fam-100", "999")  # unknown product
            after = c.execute(
                "SELECT product_id, role FROM family_members "
                "WHERE family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "ORDER BY product_id"
            ).fetchall()
            assert before == after
        finally:
            c.close()

    def test_set_primary_rolls_back_after_state_update_when_audit_fails(
        self, tmp_env: tuple[Path, GrossyPaths], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Late-failure rollback: the demote/promote succeeds, then the audit
        (override) write fails, so the whole transaction rolls back. The domain
        state and the override audit history must be unchanged."""
        import grossy.history.maintenance as maint

        _, paths = tmp_env
        run_family_link(paths, "200", "fam-100")  # fam-100: 100 (primary) + 200 (alt)

        def _boom(*args: object, **kwargs: object) -> int:
            raise sqlite3.OperationalError("simulated audit write failure")

        monkeypatch.setattr(maint, "_record_override", _boom)

        c = open_connection(paths.database_file)
        try:
            with pytest.raises(sqlite3.OperationalError, match="simulated audit write failure"):
                run_set_primary(paths, "fam-100", "200")
            # The demote/promote must have rolled back: 100 is still the primary.
            row = c.execute(
                "SELECT p.retailer_product_id FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "AND m.role = 'primary'"
            ).fetchone()
            assert row is not None
            assert row[0] == "100"
            # 200 is still an alternative, not promoted.
            role = c.execute(
                "SELECT m.role FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "AND p.retailer_product_id = '200'"
            ).fetchone()
            assert role is not None
            assert role[0] == "alternative"
            # And no override was recorded for the primary change.
            override = c.execute(
                "SELECT 1 FROM manual_overrides "
                "WHERE target_id = 'fam-100' AND field_name = 'primary_product_id' LIMIT 1"
            ).fetchone()
            assert override is None
        finally:
            c.close()


# --- set-recurrence -----------------------------------------------------------


class TestSetRecurrence:
    def test_set_recurrence_success(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        result = run_set_recurrence(paths, "fam-100", 3)
        assert result["status"] == "success"
        assert result["recurrence_weeks"] == 3

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT recurrence_weeks, recurrence_source, recurrence_confidence "
                "FROM product_families WHERE family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == 3
            assert row[1] == "manual"
            assert row[2] == "high"
        finally:
            c.close()

    def test_set_recurrence_invalid_weeks_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="positive integer"):
            run_set_recurrence(paths, "fam-100", 0)

    def test_set_recurrence_unknown_family_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown family"):
            run_set_recurrence(paths, "nonexistent", 2)


# --- set-quantity --------------------------------------------------------------


class TestSetQuantity:
    def test_set_quantity_product(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        result = run_set_quantity(paths, "100", 5)
        assert result["status"] == "success"
        assert result["quantity"] == 5

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT typical_quantity FROM products WHERE retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == 5
        finally:
            c.close()

    def test_set_quantity_family_resolves_to_primary(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """Setting quantity on a family with an alternative applies only to
        the primary; the alternative is unaffected."""
        _, paths = tmp_env
        # Create a nontrivial family: link 200 as an alternative.
        run_family_link(paths, "200", "fam-100")
        result = run_set_quantity(paths, "fam-100", 7)
        assert result["status"] == "success"
        assert result["resolved_product_id"] == "100"

        c = open_connection(paths.database_file)
        try:
            # The primary's quantity was changed.
            row = c.execute(
                "SELECT typical_quantity FROM products WHERE retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == 7
            # The alternative's quantity was NOT changed (stays at default 1).
            row = c.execute(
                "SELECT typical_quantity FROM products WHERE retailer_product_id = '200'"
            ).fetchone()
            assert row is not None
            assert row[0] == 1
        finally:
            c.close()

    def test_set_quantity_invalid_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="positive integer"):
            run_set_quantity(paths, "100", 0)

    def test_set_quantity_unknown_target_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown product or family"):
            run_set_quantity(paths, "nonexistent", 3)


# --- enable / disable ----------------------------------------------------------


class TestEnableDisable:
    def test_disable_family(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        result = run_disable(paths, "fam-100")
        assert result["status"] == "success"
        assert result["enabled"] is False

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT enabled FROM product_families WHERE family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == 0
        finally:
            c.close()

    def test_enable_family(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        run_disable(paths, "fam-100")
        result = run_enable(paths, "fam-100")
        assert result["status"] == "success"
        assert result["enabled"] is True

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT enabled FROM product_families WHERE family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == 1
        finally:
            c.close()

    def test_disable_unknown_family_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown family"):
            run_disable(paths, "nonexistent")


# --- blacklist / unblacklist ---------------------------------------------------


class TestBlacklist:
    def test_blacklist_product(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        result = run_blacklist_product(paths, "100")
        assert result["status"] == "success"
        assert result["blacklisted"] is True

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT blacklist_state FROM products WHERE retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "blacklisted"

            # Event is recorded.
            event = c.execute(
                "SELECT target_type, target_id, action FROM blacklist_events "
                "WHERE target_id = '100' AND action = 'blacklist'"
            ).fetchone()
            assert event is not None
            assert event[0] == "product"
        finally:
            c.close()

    def test_blacklist_family(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        result = run_blacklist_family(paths, "fam-100")
        assert result["status"] == "success"
        assert result["blacklisted"] is True

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT blacklist_state FROM product_families WHERE family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "blacklisted"
        finally:
            c.close()

    def test_unblacklist_product(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        run_blacklist_product(paths, "100")
        result = run_unblacklist_product(paths, "100")
        assert result["status"] == "success"
        assert result["blacklisted"] is False

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT blacklist_state FROM products WHERE retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "active"

            # Two events: blacklist then unblacklist.
            events = c.execute(
                "SELECT action FROM blacklist_events WHERE target_id = '100' ORDER BY id"
            ).fetchall()
            assert len(events) == 2
            assert events[0][0] == "blacklist"
            assert events[1][0] == "unblacklist"
        finally:
            c.close()

    def test_unblacklist_family(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        run_blacklist_family(paths, "fam-100")
        result = run_unblacklist_family(paths, "fam-100")
        assert result["status"] == "success"
        assert result["blacklisted"] is False

        c = open_connection(paths.database_file)
        try:
            # Durable family state is active again.
            row = c.execute(
                "SELECT blacklist_state FROM product_families WHERE family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "active"

            # Ordered family blacklist/unblacklist events are recorded.
            events = c.execute(
                "SELECT action FROM blacklist_events "
                "WHERE target_type = 'family' AND target_id = 'fam-100' ORDER BY id"
            ).fetchall()
            assert len(events) == 2
            assert events[0][0] == "blacklist"
            assert events[1][0] == "unblacklist"
        finally:
            c.close()

    def test_blacklist_unknown_product_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown product"):
            run_blacklist_product(paths, "nonexistent")

    def test_blacklist_unknown_family_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown family"):
            run_blacklist_family(paths, "nonexistent")

    def test_product_blacklist_blocks_selection(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A blacklisted product is not selected for a due family (FR-65)."""
        _, paths = tmp_env
        run_blacklist_product(paths, "100")

        c = open_connection(paths.database_file)
        try:
            # select_family_product should return None: the only member ("100")
            # is blacklisted, so no product is selectable.
            result = select_family_product(c, "fam-100", frozenset({"100"}))
            assert result is None
        finally:
            c.close()

    def test_family_blacklist_blocks_selection(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A family blacklist blocks every member of the family (FR-66)."""
        _, paths = tmp_env
        run_family_link(paths, "200", "fam-100")
        run_blacklist_family(paths, "fam-100")

        c = open_connection(paths.database_file)
        try:
            # Both members are blocked by the family-level blacklist.
            result = select_family_product(c, "fam-100", frozenset({"100", "200"}))
            assert result is None
        finally:
            c.close()


# --- family link / unlink / merge -----------------------------------------------


class TestFamilyLinkUnlinkMerge:
    def test_family_link_success(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Linking a sole member into another family also retires the vacated
        source family (no orphan family shells)."""
        _, paths = tmp_env
        result = run_family_link(paths, "200", "fam-100")
        assert result["status"] == "success"

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT m.role FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "AND p.retailer_product_id = '200'"
            ).fetchone()
            assert row is not None
            assert row[0] == "alternative"
            # The vacated family (sole member "200" was moved) is deleted.
            row = c.execute("SELECT 1 FROM product_families WHERE family_id = 'fam-200'").fetchone()
            assert row is None
        finally:
            c.close()

    def test_family_link_already_member_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="already"):
            run_family_link(paths, "100", "fam-100")

    def test_family_link_unknown_product_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown product"):
            run_family_link(paths, "nonexistent", "fam-100")

    def test_family_link_unknown_family_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown family"):
            run_family_link(paths, "100", "nonexistent")

    def test_family_unlink_success(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        # Link "200" into "fam-100" first.
        run_family_link(paths, "200", "fam-100")
        result = run_family_unlink(paths, "200")
        assert result["status"] == "success"

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT 1 FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "AND p.retailer_product_id = '200'"
            ).fetchone()
            assert row is None
        finally:
            c.close()

    def test_family_unlink_unknown_product_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown product"):
            run_family_unlink(paths, "999")

    def test_family_unlink_sole_member_deletes_family(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        _, paths = tmp_env
        result = run_family_unlink(paths, "100")
        assert result["status"] == "success"

        c = open_connection(paths.database_file)
        try:
            # The family should be deleted (sole member removed).
            row = c.execute("SELECT 1 FROM product_families WHERE family_id = 'fam-100'").fetchone()
            assert row is None
        finally:
            c.close()

    def test_family_merge_success(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        result = run_family_merge(paths, "fam-200", "fam-100")
        assert result["status"] == "success"
        assert result["members_moved"] == 1

        c = open_connection(paths.database_file)
        try:
            # Source family is deleted.
            row = c.execute("SELECT 1 FROM product_families WHERE family_id = 'fam-200'").fetchone()
            assert row is None

            # Product "200" is now in fam-100 as an alternative.
            row = c.execute(
                "SELECT m.role FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "AND p.retailer_product_id = '200'"
            ).fetchone()
            assert row is not None
            assert row[0] == "alternative"
        finally:
            c.close()

    def test_family_merge_same_family_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="must differ"):
            run_family_merge(paths, "fam-100", "fam-100")

    def test_family_merge_unknown_source_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """An invalid merge leaves state unchanged."""
        _, paths = tmp_env
        c = open_connection(paths.database_file)
        try:
            families_before = c.execute(
                "SELECT family_id FROM product_families ORDER BY family_id"
            ).fetchall()
            members_before = c.execute(
                "SELECT family_id, product_id, role FROM family_members ORDER BY product_id"
            ).fetchall()
            overrides_before = c.execute("SELECT * FROM manual_overrides ORDER BY id").fetchall()
            events_before = c.execute("SELECT * FROM blacklist_events ORDER BY id").fetchall()
            with pytest.raises(MaintenanceError, match="unknown family"):
                run_family_merge(paths, "nonexistent", "fam-100")
            families_after = c.execute(
                "SELECT family_id FROM product_families ORDER BY family_id"
            ).fetchall()
            members_after = c.execute(
                "SELECT family_id, product_id, role FROM family_members ORDER BY product_id"
            ).fetchall()
            assert families_before == families_after
            assert members_before == members_after
            assert (
                c.execute("SELECT * FROM manual_overrides ORDER BY id").fetchall()
                == overrides_before
            )
            assert (
                c.execute("SELECT * FROM blacklist_events ORDER BY id").fetchall() == events_before
            )
        finally:
            c.close()

    def test_family_merge_unknown_target_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown family"):
            run_family_merge(paths, "fam-100", "nonexistent")

    def test_family_merge_rolls_back_after_moves_when_audit_fails(
        self, tmp_env: tuple[Path, GrossyPaths], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Late-failure rollback: the member moves and the source-family deletion
        succeed, then the audit (override) write fails, so the whole transaction
        rolls back. The domain state and the override audit history must be
        unchanged."""
        import grossy.history.maintenance as maint

        _, paths = tmp_env

        def _boom(*args: object, **kwargs: object) -> int:
            raise sqlite3.OperationalError("simulated audit write failure")

        monkeypatch.setattr(maint, "_record_override", _boom)

        c = open_connection(paths.database_file)
        try:
            with pytest.raises(sqlite3.OperationalError, match="simulated audit write failure"):
                run_family_merge(paths, "fam-200", "fam-100")
            # The source family must still exist with its primary member.
            fam200 = c.execute(
                "SELECT 1 FROM product_families WHERE family_id = 'fam-200'"
            ).fetchone()
            assert fam200 is not None
            member = c.execute(
                "SELECT m.role FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = 'fam-200') "
                "AND p.retailer_product_id = '200'"
            ).fetchone()
            assert member is not None
            assert member[0] == "primary"
            # 200 was not moved into fam-100.
            moved = c.execute(
                "SELECT 1 FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "AND p.retailer_product_id = '200'"
            ).fetchone()
            assert moved is None
            # And no override was recorded for the merge.
            override = c.execute(
                "SELECT 1 FROM manual_overrides "
                "WHERE target_id = 'fam-200' AND field_name = 'merged_into' LIMIT 1"
            ).fetchone()
            assert override is None
        finally:
            c.close()


# --- set-category ---------------------------------------------------------------


class TestSetCategory:
    def test_set_category_product(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        result = run_set_category(paths, "100", "Breakfast")
        assert result["status"] == "success"
        assert result["category"] == "breakfast"

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT c.name FROM products p "
                "JOIN categories c ON c.id = p.category_id "
                "WHERE p.retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "breakfast"
        finally:
            c.close()

    def test_set_category_family(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        result = run_set_category(paths, "fam-100", "Snacks")
        assert result["status"] == "success"

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT c.name FROM product_families f "
                "JOIN categories c ON c.id = f.category_id "
                "WHERE f.family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "snacks"
        finally:
            c.close()

    def test_set_category_unknown_target_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown product or family"):
            run_set_category(paths, "nonexistent", "Breakfast")

    def test_set_category_empty_name_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="must not be empty"):
            run_set_category(paths, "100", "  ")

    def test_set_category_misc_rejected(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """No-misc rule (PRD 7.3 / FR-37): a `misc` category is never created,
        even by manual assignment. The product keeps its existing category
        unchanged and no `misc` category row appears."""
        _, paths = tmp_env
        c = open_connection(paths.database_file)
        try:
            # Capture the product's existing category before the rejected call.
            before = c.execute(
                "SELECT c.name FROM products p "
                "LEFT JOIN categories c ON c.id = p.category_id "
                "WHERE p.retailer_product_id = '100'"
            ).fetchone()
            with pytest.raises(MaintenanceError, match="misc"):
                run_set_category(paths, "100", "Misc")
            # No `misc` category was created.
            row = c.execute("SELECT 1 FROM categories WHERE name = 'misc'").fetchone()
            assert row is None
            # The product's category is unchanged (same as before).
            after = c.execute(
                "SELECT c.name FROM products p "
                "LEFT JOIN categories c ON c.id = p.category_id "
                "WHERE p.retailer_product_id = '100'"
            ).fetchone()
            assert before is not None
            assert after == before
            # No category override was recorded for the rejected assignment.
            row = c.execute(
                "SELECT 1 FROM manual_overrides "
                "WHERE target_type = 'product' AND target_id = '100' "
                "AND field_name = 'category_id' AND superseded_at IS NULL"
            ).fetchone()
            assert row is None
        finally:
            c.close()


# --- category rename -------------------------------------------------------------


class TestCategoryRename:
    def test_category_rename_success(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        result = run_category_rename(paths, "dairy", "Dairy Products")
        assert result["status"] == "success"
        assert result["new_name"] == "dairy products"

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT name, display_name FROM categories WHERE name = 'dairy products'"
            ).fetchone()
            assert row is not None
            assert row[0] == "dairy products"
            assert row[1] == "Dairy Products"

            # Old name is gone.
            row = c.execute("SELECT 1 FROM categories WHERE name = 'dairy'").fetchone()
            assert row is None
        finally:
            c.close()

    def test_category_rename_to_misc_rejected(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """No-misc rule (PRD 7.3 / FR-37): a rename to `misc` is rejected before
        any state change; the source category is preserved."""
        _, paths = tmp_env
        # Create a category to rename so the source exists.
        run_set_category(paths, "100", "Dairy")
        with pytest.raises(MaintenanceError, match="misc"):
            run_category_rename(paths, "dairy", "Misc")

        c = open_connection(paths.database_file)
        try:
            # The source category is unchanged and no `misc` category exists.
            row = c.execute("SELECT 1 FROM categories WHERE name = 'dairy'").fetchone()
            assert row is not None
            row = c.execute("SELECT 1 FROM categories WHERE name = 'misc'").fetchone()
            assert row is None
        finally:
            c.close()

    def test_category_rename_unknown_old_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="unknown category"):
            run_category_rename(paths, "nonexistent", "New")

    def test_category_rename_conflict_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        # Create a second category first.
        run_set_category(paths, "200", "Bakery")
        with pytest.raises(MaintenanceError, match="already exists"):
            run_category_rename(paths, "dairy", "bakery")

    def test_category_rename_same_name_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="must differ"):
            run_category_rename(paths, "dairy", "dairy")

    def test_category_rename_empty_fails(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        _, paths = tmp_env
        with pytest.raises(MaintenanceError, match="must not be empty"):
            run_category_rename(paths, "  ", "New")


# --- override auditability --------------------------------------------------------


class TestOverrideAuditability:
    def test_override_recorded_with_supersession(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Each override supersedes the prior one for the same target/field."""
        _, paths = tmp_env
        run_set_quantity(paths, "100", 3)
        run_set_quantity(paths, "100", 5)

        c = open_connection(paths.database_file)
        try:
            rows = c.execute(
                "SELECT field_value, superseded_at FROM manual_overrides "
                "WHERE target_type = 'product' AND target_id = '100' "
                "AND field_name = 'typical_quantity' ORDER BY id"
            ).fetchall()
            assert len(rows) == 2
            # First is superseded, second is active.
            assert rows[0][0] == "3"
            assert rows[0][1] is not None
            assert rows[1][0] == "5"
            assert rows[1][1] is None
        finally:
            c.close()

    def test_blacklist_events_are_append_only(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Blacklist events are never modified or deleted: after each call,
        all prior rows remain byte-for-value unchanged (IDs, rationale,
        timestamps) as new rows are appended."""
        _, paths = tmp_env
        c = open_connection(paths.database_file)
        try:
            run_blacklist_product(paths, "100")
            first_rows = c.execute(
                "SELECT id, target_type, target_id, action, rationale, created_at "
                "FROM blacklist_events WHERE target_id = '100' ORDER BY id"
            ).fetchall()
            assert len(first_rows) == 1
            assert first_rows[0][3] == "blacklist"

            run_unblacklist_product(paths, "100")
            second_rows = c.execute(
                "SELECT id, target_type, target_id, action, rationale, created_at "
                "FROM blacklist_events WHERE target_id = '100' ORDER BY id"
            ).fetchall()
            assert len(second_rows) == 2
            # The first row is unchanged.
            assert second_rows[0] == first_rows[0]
            assert second_rows[1][3] == "unblacklist"

            run_blacklist_product(paths, "100")
            third_rows = c.execute(
                "SELECT id, target_type, target_id, action, rationale, created_at "
                "FROM blacklist_events WHERE target_id = '100' ORDER BY id"
            ).fetchall()
            assert len(third_rows) == 3
            # Both prior rows are unchanged.
            assert third_rows[0] == first_rows[0]
            assert third_rows[1] == second_rows[1]
            assert third_rows[2][3] == "blacklist"
        finally:
            c.close()


# --- sync preservation ------------------------------------------------------------


class TestSyncPreservation:
    def test_sync_does_not_clear_product_blacklist(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A later sync does not clear a product blacklist (FR-68) and continues
        recording historical appearances of blacklisted products (FR-67)."""
        _, paths = tmp_env
        run_blacklist_product(paths, "100")
        _apply_sync_order(paths, "10003", "2026-09-15", ["100"])

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT blacklist_state FROM products WHERE retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "blacklisted"
            # The product's purchase_order_count should have increased.
            row = c.execute(
                "SELECT purchase_order_count FROM products WHERE retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == 3  # was 2, now 3 after the new order.
        finally:
            c.close()

    def test_sync_does_not_clear_family_blacklist(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A later sync does not clear a family blacklist (FR-68)."""
        _, paths = tmp_env
        run_blacklist_family(paths, "fam-100")
        _apply_sync_order(paths, "10003", "2026-09-15", ["100"])

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT blacklist_state FROM product_families WHERE family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "blacklisted"
        finally:
            c.close()

    def test_sync_does_not_overwrite_manual_recurrence(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """A later sync does not overwrite a manually set recurrence (FR-68)."""
        _, paths = tmp_env
        run_set_recurrence(paths, "fam-100", 4)
        _apply_sync_order(paths, "10003", "2026-09-22", ["100"])

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT recurrence_weeks, recurrence_source FROM product_families "
                "WHERE family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == 4
            assert row[1] == "manual"
        finally:
            c.close()

    def test_sync_does_not_overwrite_manual_category(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """A later sync does not overwrite a manually set category (FR-38, FR-68)."""
        _, paths = tmp_env
        run_set_category(paths, "100", "Custom Category")
        _apply_sync_order(paths, "10003", "2026-09-15", ["100"], category="Bakery")

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT c.name FROM products p "
                "JOIN categories c ON c.id = p.category_id "
                "WHERE p.retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "custom category"
        finally:
            c.close()

    def test_sync_does_not_overwrite_manual_quantity(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """A later sync does not overwrite a manually set quantity (FR-35, FR-68)."""
        _, paths = tmp_env
        run_set_quantity(paths, "100", 9)
        _apply_sync_order(paths, "10003", "2026-09-15", ["100"])

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT typical_quantity FROM products WHERE retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == 9
        finally:
            c.close()

    def test_sync_does_not_overwrite_manual_primary(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """A later sync does not recalculate or promote the primary (FR-44, FR-68)."""
        _, paths = tmp_env
        run_family_link(paths, "200", "fam-100")
        run_set_primary(paths, "fam-100", "200")  # primary is now "200", not "100"
        _apply_sync_order(paths, "10003", "2026-09-15", ["100"])

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT p.retailer_product_id FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "AND m.role = 'primary'"
            ).fetchone()
            assert row is not None
            assert row[0] == "200"
        finally:
            c.close()

    def test_sync_does_not_overwrite_manual_family_category(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """A later sync does not overwrite a manually set family category (FR-40, FR-68)."""
        _, paths = tmp_env
        run_set_category(paths, "fam-100", "Household")
        _apply_sync_order(paths, "10003", "2026-09-15", ["100"], category="Bakery")

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT c.name FROM product_families f "
                "JOIN categories c ON c.id = f.category_id "
                "WHERE f.family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "household"
        finally:
            c.close()

    def test_sync_does_not_clear_disabled_state(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A later sync does not re-enable a disabled family (FR-68)."""
        _, paths = tmp_env
        run_disable(paths, "fam-100")
        _apply_sync_order(paths, "10003", "2026-09-15", ["100"])

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT enabled FROM product_families WHERE family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == 0
        finally:
            c.close()


# --- disable semantics -------------------------------------------------------------


class TestDisableSemantics:
    def test_disabled_family_suppresses_prefill(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """FR-63: A disabled family suppresses history-driven recurring prefill.

        select_family_product returns None for a disabled family, preventing
        it from being auto-selected. The PRD distinguishes this from blacklist:
        disabling does not block explicit YAML or selected recipes (that is the
        planner's responsibility at a higher level).
        """
        _, paths = tmp_env
        run_disable(paths, "fam-100")

        c = open_connection(paths.database_file)
        try:
            result = select_family_product(c, "fam-100", frozenset({"100"}))
            assert result is None
        finally:
            c.close()

    def test_reenabled_family_selectable_again(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """FR-64: Re-enabling restores history-driven prefill."""
        _, paths = tmp_env
        run_disable(paths, "fam-100")
        run_enable(paths, "fam-100")

        c = open_connection(paths.database_file)
        try:
            result = select_family_product(c, "fam-100", frozenset({"100"}))
            assert result is not None
            assert result.retailer_product_id == "100"
        finally:
            c.close()


# --- blacklist bypass prevention (family operations) --------------------------------


class TestBlacklistBypassPrevention:
    def test_link_from_blacklisted_family_rejected(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Linking a product out of a blacklisted family is rejected (FR-66)."""
        _, paths = tmp_env
        # Create a second family for product 200 by linking, then blacklist fam-200.
        # Product 200 is in fam-200 (auto-created by sync).
        run_blacklist_family(paths, "fam-200")
        with pytest.raises(MaintenanceError, match="blacklisted"):
            run_family_link(paths, "200", "fam-100")

    def test_merge_blacklisted_source_rejected(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Merging a blacklisted source family is rejected (FR-66)."""
        _, paths = tmp_env
        run_blacklist_family(paths, "fam-200")
        with pytest.raises(MaintenanceError, match="blacklisted"):
            run_family_merge(paths, "fam-200", "fam-100")

    def test_merge_blacklisted_target_rejected(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Merging into a blacklisted target family is rejected (FR-66)."""
        _, paths = tmp_env
        run_blacklist_family(paths, "fam-100")
        with pytest.raises(MaintenanceError, match="blacklisted"):
            run_family_merge(paths, "fam-200", "fam-100")


# --- rationale auditability ----------------------------------------------------------


class TestRationaleAuditability:
    def test_set_primary_records_generated_rationale(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """When no user rationale is provided, a command-generated one is stored."""
        _, paths = tmp_env
        run_family_link(paths, "200", "fam-100")
        run_set_primary(paths, "fam-100", "200")

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT rationale FROM manual_overrides "
                "WHERE field_name = 'primary_product_id' AND superseded_at IS NULL"
            ).fetchone()
            assert row is not None
            rationale = str(row[0])
            assert "set-primary" in rationale
        finally:
            c.close()

    def test_blacklist_records_generated_rationale(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Blacklist events include a generated rationale when none is provided."""
        _, paths = tmp_env
        run_blacklist_product(paths, "100")

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT rationale FROM blacklist_events WHERE action = 'blacklist'"
            ).fetchone()
            assert row is not None
            rationale = str(row[0])
            assert "blacklist" in rationale
            assert "100" in rationale
        finally:
            c.close()

    def test_user_rationale_stored(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A user-provided rationale is stored verbatim."""
        _, paths = tmp_env
        run_blacklist_product(paths, "100", rationale="allergic to this product")

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT rationale FROM blacklist_events WHERE action = 'blacklist'"
            ).fetchone()
            assert row is not None
            assert str(row[0]) == "allergic to this product"
        finally:
            c.close()


# --- category normalization ----------------------------------------------------------


class TestCategoryNormalization:
    def test_whitespace_collapse_in_set_category(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Multiple internal spaces are collapsed to one (consistent with sync)."""
        _, paths = tmp_env
        run_set_category(paths, "100", "Dairy   Products")

        c = open_connection(paths.database_file)
        try:
            row = c.execute("SELECT name FROM categories WHERE name = 'dairy products'").fetchone()
            assert row is not None
        finally:
            c.close()

    def test_whitespace_collapse_in_rename(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Rename uses the same whitespace-collapsing normalizer."""
        _, paths = tmp_env
        # First create a category with multiple spaces.
        run_set_category(paths, "100", "Bread   Products")
        # Now rename it (the normalizer will collapse spaces).
        result = run_category_rename(paths, "Bread   Products", "Grain Items")
        assert result["status"] == "success"
        assert result["old_name"] == "bread products"
        assert result["new_name"] == "grain items"

    def test_tab_and_newline_collapsed(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """Tabs and newlines are treated as whitespace and collapsed."""
        _, paths = tmp_env
        run_set_category(paths, "100", "Snacks\tAnd\nDrinks")

        c = open_connection(paths.database_file)
        try:
            row = c.execute(
                "SELECT name FROM categories WHERE name = 'snacks and drinks'"
            ).fetchone()
            assert row is not None
        finally:
            c.close()


# --- category rename persistence through sync ----------------------------------


class TestCategoryRenamePersistence:
    def test_rename_preserved_by_later_sync(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """FR-38/FR-68: a later sync with the old heading does not re-create the
        old category or move products back to it."""
        _, paths = tmp_env
        # Rename 'dairy' to 'dairy products'.
        run_category_rename(paths, "dairy", "Dairy Products")

        # Import a new order with the same heading "Dairy" (normalized to "dairy").
        _apply_sync_order(paths, "10003", "2026-09-15", ["100", "200"], category="Dairy")

        c = open_connection(paths.database_file)
        try:
            # The old category name 'dairy' must NOT have been re-created.
            row = c.execute("SELECT 1 FROM categories WHERE name = 'dairy'").fetchone()
            assert row is None, "sync re-created the old category name 'dairy'"

            # The product should still reference 'dairy products'.
            row = c.execute(
                "SELECT c.name FROM products p "
                "JOIN categories c ON c.id = p.category_id "
                "WHERE p.retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "dairy products"
        finally:
            c.close()

    def test_rename_chain_preserved_by_later_sync(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A multi-step rename chain is followed by sync (dairy → dairy products → fresh)."""
        _, paths = tmp_env
        run_category_rename(paths, "dairy", "Dairy Products")
        run_category_rename(paths, "Dairy Products", "Fresh")

        # Import a new order with the original heading.
        _apply_sync_order(paths, "10003", "2026-09-15", ["100"], category="Dairy")

        c = open_connection(paths.database_file)
        try:
            # Neither intermediate name should have been re-created.
            row = c.execute("SELECT 1 FROM categories WHERE name = 'dairy'").fetchone()
            assert row is None
            row = c.execute("SELECT 1 FROM categories WHERE name = 'dairy products'").fetchone()
            assert row is None

            # The product should reference the final rename target 'fresh'.
            row = c.execute(
                "SELECT c.name FROM products p "
                "JOIN categories c ON c.id = p.category_id "
                "WHERE p.retailer_product_id = '100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "fresh"
        finally:
            c.close()


# --- unlink from blacklisted family -------------------------------------------


class TestUnlinkBlacklistedFamily:
    def test_unlink_from_blacklisted_family_rejected(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """FR-66: unlinking the sole member of a blacklisted family is rejected
        to prevent silently erasing the family blacklist state; the family and
        blacklist remain intact."""
        _, paths = tmp_env
        run_blacklist_family(paths, "fam-100")
        c = open_connection(paths.database_file)
        try:
            overrides_before = c.execute("SELECT * FROM manual_overrides ORDER BY id").fetchall()
            events_before = c.execute("SELECT * FROM blacklist_events ORDER BY id").fetchall()
            with pytest.raises(MaintenanceError, match="blacklisted"):
                run_family_unlink(paths, "100")
            assert (
                c.execute("SELECT * FROM manual_overrides ORDER BY id").fetchall()
                == overrides_before
            )
            assert (
                c.execute("SELECT * FROM blacklist_events ORDER BY id").fetchall() == events_before
            )
            # Family still exists and is still blacklisted.
            row = c.execute(
                "SELECT blacklist_state FROM product_families WHERE family_id = 'fam-100'"
            ).fetchone()
            assert row is not None
            assert row[0] == "blacklisted"

            # Product is still a member.
            row = c.execute(
                "SELECT 1 FROM family_members "
                "WHERE family_id = (SELECT id FROM product_families WHERE family_id = 'fam-100') "
                "AND product_id = (SELECT id FROM products WHERE retailer_product_id = '100')"
            ).fetchone()
            assert row is not None
        finally:
            c.close()


# --- unlink persistence through inference ---------------------------------------


class TestUnlinkPersistence:
    def test_unlink_not_reversed_by_later_sync(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """FR-68: a manually unlinked product is not re-linked by inference on a
        later sync. The explicit unlink override prevents `_ensure_family` from
        creating a new family for the product."""
        _, paths = tmp_env
        # Unlink product "100" from its family (sole member → family deleted).
        result = run_family_unlink(paths, "100")
        assert result["status"] == "success"

        # Import a new order containing product "100".
        _apply_sync_order(paths, "10003", "2026-09-15", ["100"])

        c = open_connection(paths.database_file)
        try:
            # Product "100" must have NO family after the sync.
            row = c.execute(
                "SELECT 1 FROM family_members "
                "WHERE product_id = (SELECT id FROM products WHERE retailer_product_id = '100')"
            ).fetchone()
            assert row is None, "inference re-created a family for a manually unlinked product"

            # The explicit unlink override is still active.
            row = c.execute(
                "SELECT 1 FROM manual_overrides "
                "WHERE target_type = 'product' AND target_id = '100' "
                "AND field_name = 'family_id' AND field_value = '' AND superseded_at IS NULL"
            ).fetchone()
            assert row is not None
        finally:
            c.close()


# --- YAML/blacklist conflict reporting (deterministic policy boundary) -----------


class TestYamlBlacklistConflictDeterministic:
    """Tests for the deterministic YAML-reference blacklist conflict check
    (PRD 7.1: precedence level 2 blocks level 3)."""

    def test_product_blacklist_conflict(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A YAML reference to a product-blacklisted product returns a blocking
        conflict with scope 'product'."""
        from grossy.history.maintenance import check_yaml_blacklist_conflict

        _, paths = tmp_env
        run_blacklist_product(paths, "100")

        c = open_connection(paths.database_file)
        try:
            conflict = check_yaml_blacklist_conflict(c, "100")
            assert conflict is not None
            assert conflict.target_type == "product"
            assert conflict.target_id == "100"
            assert conflict.blacklist_scope == "product"
            assert "100" in conflict.message
        finally:
            c.close()

    def test_family_blacklist_conflict(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A YAML reference to a product in a family-blacklisted family returns
        a blocking conflict with scope 'family'."""
        from grossy.history.maintenance import check_yaml_blacklist_conflict

        _, paths = tmp_env
        run_family_link(paths, "200", "fam-100")
        run_blacklist_family(paths, "fam-100")

        c = open_connection(paths.database_file)
        try:
            # Both members conflict via the family blacklist.
            conflict = check_yaml_blacklist_conflict(c, "100")
            assert conflict is not None
            assert conflict.blacklist_scope == "family"
            assert conflict.target_id == "fam-100"

            conflict = check_yaml_blacklist_conflict(c, "200")
            assert conflict is not None
            assert conflict.blacklist_scope == "family"
            assert conflict.target_id == "fam-100"
        finally:
            c.close()

    def test_no_conflict_when_not_blacklisted(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """No conflict when neither the product nor its family is blacklisted."""
        from grossy.history.maintenance import check_yaml_blacklist_conflict

        _, paths = tmp_env

        c = open_connection(paths.database_file)
        try:
            conflict = check_yaml_blacklist_conflict(c, "100")
            assert conflict is None
        finally:
            c.close()

    def test_product_blacklist_takes_precedence_over_family(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """When both product and family are blacklisted, the product scope is
        reported (it is the more specific constraint)."""
        from grossy.history.maintenance import check_yaml_blacklist_conflict

        _, paths = tmp_env
        run_family_link(paths, "200", "fam-100")
        run_blacklist_family(paths, "fam-100")
        run_blacklist_product(paths, "100")

        c = open_connection(paths.database_file)
        try:
            conflict = check_yaml_blacklist_conflict(c, "100")
            assert conflict is not None
            # Product blacklist is checked first and reported.
            assert conflict.blacklist_scope == "product"
        finally:
            c.close()


# --- production policy boundary: strict YAML config against DB state --------------


def _make_config(staples: list[tuple[str, str]]) -> GrossyConfigModel:
    """Build a minimal strict ``GrossyConfigModel`` with the given staple
    ``(key, product_id)`` references. This is the actual config input the
    production policy boundary consumes."""
    return GrossyConfigModel(
        schema_version=1,
        retailer="ab",
        timezone="Europe/Athens",
        currency="EUR",
        household=HouseholdModel(adults=2, children=1, recipe_servings=4),
        appliances=AppliancesModel(
            thermomix=ThermomixModel(available=False),
            dough_kneading_machine=SimpleApplianceModel(available=False),
            steam_oven=SimpleApplianceModel(available=False),
            air_fryer=AirFryerModel(available=False),
            induction_cooktop=SimpleApplianceModel(available=False),
            microwave=MicrowaveModel(available=False),
            pressure_cooker=PressureCookerModel(available=False),
            dutch_oven=DutchOvenModel(available=False),
        ),
        appliance_preferences=AppliancePreferencesModel(
            prefer_air_fryer_over_pan_frying=False,
            prefer_thermomix_native_over_manual=False,
        ),
        staples=StaplesModel(
            breakfast=[
                StapleModel(key=key, product_id=product_id, quantity=1)
                for key, product_id in staples
            ]
        ),
    )


class TestYamlPolicyBoundary:
    """Tests for the production YAML policy boundary: a strict
    ``GrossyConfigModel`` is resolved against live database state, producing
    per-reference verdicts (FR-71, FR-72). This is the callable task-06
    boundary the reviewer required, exercised with actual config input."""

    def test_product_blacklist_conflict(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A YAML staple referencing a product-blacklisted product is blocked."""
        from grossy.history.policy import GATE_BLACKLISTED, resolve_yaml_policy

        _, paths = tmp_env
        run_blacklist_product(paths, "100")
        config = _make_config([("coffee", "100")])

        c = open_connection(paths.database_file)
        try:
            result = resolve_yaml_policy(config, c)
            assert len(result.resolutions) == 1
            assert len(result.blocking_conflicts) == 1
            assert len(result.disabled_references) == 0
            assert result.has_conflicts
            r = result.resolutions[0]
            assert r.staple_key == "coffee"
            assert r.status == GATE_BLACKLISTED
            assert r.is_blocked
        finally:
            c.close()

    def test_family_blacklist_conflict(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A YAML staple referencing a member of a family-blacklisted family is
        blocked with family scope."""
        from grossy.history.policy import GATE_FAMILY_BLACKLISTED, resolve_yaml_policy

        _, paths = tmp_env
        run_family_link(paths, "200", "fam-100")
        run_blacklist_family(paths, "fam-100")
        config = _make_config([("coffee", "100")])

        c = open_connection(paths.database_file)
        try:
            result = resolve_yaml_policy(config, c)
            assert len(result.blocking_conflicts) == 1
            r = result.resolutions[0]
            assert r.status == GATE_FAMILY_BLACKLISTED
            assert r.is_blocked
            assert r.family_id == "fam-100"
        finally:
            c.close()

    def test_disabled_but_not_blacklisted_is_not_a_conflict(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """A disabled family is NOT a blocking conflict for an explicit YAML
        reference (PRD: disabling suppresses learned prefill, it does not block
        YAML). The reference resolves with a disabled-family status, not a
        blocking conflict."""
        from grossy.history.policy import GATE_DISABLED, resolve_yaml_policy

        _, paths = tmp_env
        run_disable(paths, "fam-100")
        config = _make_config([("coffee", "100")])

        c = open_connection(paths.database_file)
        try:
            result = resolve_yaml_policy(config, c)
            assert len(result.resolutions) == 1
            assert len(result.blocking_conflicts) == 0
            assert len(result.disabled_references) == 1
            assert not result.has_conflicts
            r = result.resolutions[0]
            assert r.status == GATE_DISABLED
            assert not r.is_blocked
        finally:
            c.close()

    def test_product_blacklist_takes_precedence_over_disabled(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """When a product is blacklisted AND its family is disabled, the
        blacklist conflict (level 2) is reported, not a disabled status."""
        from grossy.history.policy import GATE_BLACKLISTED, resolve_yaml_policy

        _, paths = tmp_env
        run_disable(paths, "fam-100")
        run_blacklist_product(paths, "100")
        config = _make_config([("coffee", "100")])

        c = open_connection(paths.database_file)
        try:
            result = resolve_yaml_policy(config, c)
            assert len(result.blocking_conflicts) == 1
            assert len(result.disabled_references) == 0
            r = result.resolutions[0]
            assert r.status == GATE_BLACKLISTED
            assert r.is_blocked
        finally:
            c.close()

    def test_unknown_yaml_product_is_unknown(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A YAML staple referencing a product with no observed history
        resolves as unknown (no conflict, not ok)."""
        from grossy.history.policy import GATE_UNKNOWN, resolve_yaml_policy

        _, paths = tmp_env
        config = _make_config([("espresso", "999999")])

        c = open_connection(paths.database_file)
        try:
            result = resolve_yaml_policy(config, c)
            assert len(result.resolutions) == 1
            assert len(result.blocking_conflicts) == 0
            assert not result.has_conflicts
            r = result.resolutions[0]
            assert r.status == GATE_UNKNOWN
            assert not r.is_blocked
        finally:
            c.close()

    def test_multiple_staples_mixed_outcomes(self, tmp_env: tuple[Path, GrossyPaths]) -> None:
        """A config with multiple staples resolves each independently: one ok,
        one blocked, one unknown."""
        from grossy.history.policy import (
            GATE_BLACKLISTED,
            GATE_OK,
            GATE_UNKNOWN,
            resolve_yaml_policy,
        )

        _, paths = tmp_env
        run_blacklist_product(paths, "200")
        config = _make_config(
            [("ok_staple", "100"), ("blocked_staple", "200"), ("unknown_staple", "888888")]
        )

        c = open_connection(paths.database_file)
        try:
            result = resolve_yaml_policy(config, c)
            assert len(result.resolutions) == 3
            assert len(result.blocking_conflicts) == 1
            by_key = {r.staple_key: r for r in result.resolutions}
            assert by_key["ok_staple"].status == GATE_OK
            assert by_key["blocked_staple"].status == GATE_BLACKLISTED
            assert by_key["unknown_staple"].status == GATE_UNKNOWN
        finally:
            c.close()

    def test_lunch_url_only_reference_with_alternatives(
        self, tmp_env: tuple[Path, GrossyPaths]
    ) -> None:
        """A lunch staple with a URL-only preferred reference and approved
        alternatives exercises the full policy traversal: URL extraction,
        category assignment, role tracking, and conflict detection."""
        from grossy.history.policy import (
            GATE_BLACKLISTED,
            GATE_OK,
            GATE_UNKNOWN,
            resolve_yaml_policy,
        )

        _, paths = tmp_env
        # Blacklist product "200" so the alternative is a blocking conflict.
        run_blacklist_product(paths, "200")

        # Build a config with a breakfast staple (direct ID) and a lunch staple
        # (URL-only preferred + alternatives).
        config = GrossyConfigModel(
            schema_version=1,
            retailer="ab",
            timezone="Europe/Athens",
            currency="EUR",
            household=HouseholdModel(adults=2, children=1, recipe_servings=4),
            appliances=AppliancesModel(
                thermomix=ThermomixModel(available=False),
                dough_kneading_machine=SimpleApplianceModel(available=False),
                steam_oven=SimpleApplianceModel(available=False),
                air_fryer=AirFryerModel(available=False),
                induction_cooktop=SimpleApplianceModel(available=False),
                microwave=MicrowaveModel(available=False),
                pressure_cooker=PressureCookerModel(available=False),
                dutch_oven=DutchOvenModel(available=False),
            ),
            appliance_preferences=AppliancePreferencesModel(
                prefer_air_fryer_over_pan_frying=False,
                prefer_thermomix_native_over_manual=False,
            ),
            staples=StaplesModel(
                breakfast=[StapleModel(key="milk", product_id="100", quantity=2)],
                lunch=[
                    StapleModel(
                        key="yogurt",
                        product_url="https://www.ab.gr/en/eshop/Food/Dairy/Yogurt/p/100",
                        quantity=1,
                        approved_alternative_product_ids=["200", "999999"],
                    )
                ],
            ),
        )

        c = open_connection(paths.database_file)
        try:
            result = resolve_yaml_policy(config, c)
            assert [
                (r.staple_key, r.category, r.product_id, r.role, r.status)
                for r in result.resolutions
            ] == [
                ("milk", "breakfast", "100", "preferred", GATE_OK),
                ("yogurt", "lunch", "100", "preferred", GATE_OK),
                ("yogurt", "lunch", "200", "alternative", GATE_BLACKLISTED),
                ("yogurt", "lunch", "999999", "alternative", GATE_UNKNOWN),
            ]
            assert result.has_conflicts
            assert result.blocking_conflicts == (result.resolutions[2],)
            assert result.disabled_references == ()
        finally:
            c.close()
