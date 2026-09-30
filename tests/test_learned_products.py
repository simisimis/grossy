"""Tests for learned products, families, alternatives, and recurrence (task 05).

Covers the PRD unit-test requirements of section 19.1 and the task-05
acceptance criteria:

- binary purchase counts (presence, never a quantity);
- normalized, reusable categories with no ``misc`` fallback;
- a fixed primary that later synchronization cannot replace;
- recurrence inference from dated occurrences with confidence gates;
- guarded single-occurrence category estimates (reported, not auto-added);
- family-owned recurrence and ``Europe/Athens`` due dates;
- an approved alternative that advances the family cadence without promoting,
  with the primary retried on the next due run.
"""

from __future__ import annotations

import json
from datetime import UTC, date
from pathlib import Path
from typing import Any

import pytest

import grossy.history.learn as learn_mod
from grossy.db.connection import open_connection
from grossy.history import (
    HistoryProposalError,
    HistorySyncError,
    InferenceError,
    advance_family_cadence,
    family_due_date,
    infer_recurrence,
    is_family_due,
    link_alternative,
    run_inference,
    select_family_product,
)
from grossy.history.proposal import validate_family_links
from grossy.history.sync import run_sync_apply
from grossy.init import do_init
from grossy.paths import GrossyPaths

GENERATED_AT = "2026-09-26T12:00:00+00:00"
NOW = "2026-09-26T12:00:00.000000Z"


# --- proposal / DB helpers --------------------------------------------------


def _label(iso_date: str) -> str:
    year, month, day = iso_date.split("-")
    return f"{day}/{month}/{year}"


def _nav_url(source_order_id: str) -> str:
    return f"https://www.ab.gr/orders/{source_order_id}"


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
        "navigation_url": _nav_url(source_order_id),
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


@pytest.fixture
def grossy_home(tmp_path: Path) -> GrossyPaths:
    return _paths_at(
        tmp_path / ".config",
        tmp_path / ".local" / "share",
        tmp_path / ".local" / "state",
    )


@pytest.fixture
def initialized_home(grossy_home: GrossyPaths) -> GrossyPaths:
    do_init(grossy_home)
    return grossy_home


def _write(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _family_row(db: Path, family_id: str) -> tuple[Any, ...]:
    conn = open_connection(db)
    try:
        return conn.execute(
            "SELECT primary_product_id, recurrence_weeks, recurrence_source, "
            "recurrence_confidence, last_purchase_date, confidence, name "
            "FROM product_families WHERE family_id = ?",
            (family_id,),
        ).fetchone()
    finally:
        conn.close()


def _product_row_id(db: Path, product_id: str) -> int:
    conn = open_connection(db)
    try:
        row = conn.execute(
            "SELECT id FROM products WHERE retailer='ab' AND retailer_product_id=?",
            (product_id,),
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


# --- pure recurrence inference (FR-50, FR-51, FR-52, FR-53, FR-54) ----------


class TestInferRecurrence:
    def test_no_occurrences_is_empty(self) -> None:
        r = infer_recurrence([])
        assert r.weeks is None
        assert r.source is None
        assert r.confidence is None
        assert not r.is_auto_add_eligible()

    def test_single_occurrence_is_guarded_estimate(self) -> None:
        r = infer_recurrence(["2026-09-20"])
        assert r.source == "category-estimate"
        assert r.confidence == "low"
        assert r.weeks == 1
        # FR-54: a low single-occurrence estimate is reported, not auto-added.
        assert not r.is_auto_add_eligible()

    def test_single_occurrence_duplicate_dates_deduplicated(self) -> None:
        # The same order date repeated is still a single dated occurrence.
        r = infer_recurrence(["2026-09-20", "2026-09-20"])
        assert r.source == "category-estimate"
        assert not r.is_auto_add_eligible()

    def test_two_occurrences_weekly_is_observed_high(self) -> None:
        r = infer_recurrence(["2026-09-06", "2026-09-13"])
        assert r.source == "observed"
        assert r.weeks == 1
        assert r.confidence == "high"
        # FR-52: observed recurrence (2+) is eligible for automatic addition.
        assert r.is_auto_add_eligible()

    def test_two_occurrences_biweekly(self) -> None:
        r = infer_recurrence(["2026-09-01", "2026-09-15"])
        assert r.source == "observed"
        assert r.weeks == 2
        assert r.is_auto_add_eligible()

    def test_inconsistent_intervals_are_medium(self) -> None:
        # 7 days then 21 days -> whole weeks 1 and 3 -> not a stable cadence.
        r = infer_recurrence(["2026-09-01", "2026-09-08", "2026-09-29"])
        assert r.source == "observed"
        assert r.confidence == "medium"
        # Still observed, so still auto-add-eligible (FR-52).
        assert r.is_auto_add_eligible()

    def test_three_occurrences_weekly_stays_high(self) -> None:
        r = infer_recurrence(["2026-09-01", "2026-09-08", "2026-09-15"])
        assert r.source == "observed"
        assert r.confidence == "high"
        assert r.weeks == 1


class TestDueDateEuropeAthens:
    def test_due_date_is_last_purchase_plus_weeks(self) -> None:
        assert family_due_date("2026-09-13", 1) == date(2026, 9, 20)
        assert family_due_date("2026-09-13", 2) == date(2026, 9, 27)

    def test_not_due_before_boundary(self) -> None:
        assert is_family_due("2026-09-13", 1, today=date(2026, 9, 19)) is False

    def test_due_on_boundary_date(self) -> None:
        # FR-49: due when current date >= last purchase + recurrence weeks.
        assert is_family_due("2026-09-13", 1, today=date(2026, 9, 20)) is True

    def test_due_after_boundary(self) -> None:
        assert is_family_due("2026-09-13", 1, today=date(2026, 9, 21)) is True

    def test_default_today_uses_athens_calendar_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A fixed UTC instant whose HOST date is 2026-09-19 but whose Athens
        # date (UTC+3) is 2026-09-20. The due date is 2026-09-20, so the
        # result must be True: it proves the Europe/Athens date, not the host
        # UTC date, drives the comparison.
        from datetime import datetime as real_datetime

        fixed_instant = real_datetime(2026, 9, 19, 23, 0, tzinfo=UTC)

        class _FakeDatetime:
            @staticmethod
            def now(tz: Any = None) -> Any:
                return fixed_instant if tz is None else fixed_instant.astimezone(tz)

        monkeypatch.setattr(learn_mod, "datetime", _FakeDatetime)
        assert is_family_due("2026-09-13", 1) is True


# --- category normalization (FR-36, FR-37): no misc -------------------------


class TestCategoryNormalization:
    def test_heading_normalizes_and_reuses(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Same heading across two orders must yield one reused category.
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order("1001", "2026-09-06", ["71"], category="Dairy"),
                    _scanned_order("1002", "2026-09-13", ["71"], category="Dairy"),
                ]
            ),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            categories = conn.execute("SELECT name, display_name FROM categories").fetchall()
            dairy = [c for c in categories if c[0] == "dairy"]
            assert len(dairy) == 1
            assert dairy[0][1] == "Dairy"
            # No `misc` category may ever be created (FR-37).
            names = {c[0] for c in categories}
            assert "misc" not in names
        finally:
            conn.close()

    def test_missing_heading_is_reviewable_not_misc(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"], category=None)]),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT category_id FROM products WHERE retailer='ab' AND retailer_product_id='71'"
            ).fetchone()
            assert row[0] is None  # unassigned, not `misc`
            review = conn.execute(
                "SELECT kind, target_type, target_id FROM review_items "
                "WHERE kind='category' AND target_id='71'"
            ).fetchall()
            assert len(review) == 1
            assert review[0][1] == "product"
            names = {c[0] for c in conn.execute("SELECT name FROM categories").fetchall()}
            assert "misc" not in names
        finally:
            conn.close()

    def test_review_item_cleared_when_category_resolves(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # First sync: no heading -> review item. Second sync: heading present ->
        # category assigned and the review item is cleared.
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"], category=None)]),
        )
        run_sync_apply(path1, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            # Assign a normalized category manually (task 06 would do this via
            # `set-category`); simulate the resolved state.
            conn.execute(
                "INSERT INTO categories (name, display_name, created_at) "
                "VALUES ('dairy', 'Dairy', ?)",
                (NOW,),
            )
            conn.execute(
                "UPDATE products SET category_id = (SELECT id FROM categories WHERE name='dairy') "
                "WHERE retailer_product_id='71'"
            )
            conn.commit()
        finally:
            conn.close()

        # A later inference pass must clear the now-resolved review item.
        conn = open_connection(initialized_home.database_file)
        try:
            run_inference(conn, NOW)
            conn.commit()
        finally:
            conn.close()
        conn = open_connection(initialized_home.database_file)
        try:
            review = conn.execute(
                "SELECT 1 FROM review_items WHERE kind='category' AND target_id='71'"
            ).fetchall()
            assert review == []
        finally:
            conn.close()


# --- binary counts: presence, never quantity (FR-19, FR-33) -----------------


class TestBinaryCounts:
    def test_purchase_count_equals_dated_order_presence(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Product 71 in three distinct dated orders -> count 3 (not a sum of
        # units; there are no units in the model).
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order("1001", "2026-09-06", ["71"]),
                    _scanned_order("1002", "2026-09-13", ["71"]),
                    _scanned_order("1003", "2026-09-20", ["71"]),
                ]
            ),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT purchase_order_count, typical_quantity FROM products "
                "WHERE retailer_product_id='71'"
            ).fetchone()
            assert row[0] == 3
            assert row[1] == 1  # FR-34: typical_quantity defaults to 1
        finally:
            conn.close()


# --- family identity and fixed primary (FR-42, FR-43, FR-44) ----------------


class TestFixedPrimary:
    def test_recurring_product_becomes_single_member_family(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order("1001", "2026-09-06", ["71"]),
                    _scanned_order("1002", "2026-09-13", ["71"]),
                ]
            ),
        )
        run_sync_apply(path, initialized_home)
        primary_row = _product_row_id(initialized_home.database_file, "71")
        row = _family_row(initialized_home.database_file, "fam-71")
        assert row is not None
        assert row[0] == primary_row  # the product is the primary
        # One and only one member, role primary.
        conn = open_connection(initialized_home.database_file)
        try:
            members = conn.execute(
                "SELECT role FROM family_members WHERE family_id = "
                "(SELECT id FROM product_families WHERE family_id='fam-71')"
            ).fetchall()
        finally:
            conn.close()
        assert [m[0] for m in members] == ["primary"]

    def test_later_sync_cannot_replace_primary(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # P is bought once (single-member family). A later sync adds more
        # evidence for Q; inference must leave P as the primary of its family.
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71", "72"])]),
        )
        run_sync_apply(path1, initialized_home)
        p_row = _product_row_id(initialized_home.database_file, "71")
        assert _family_row(initialized_home.database_file, "fam-71")[0] == p_row

        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload(
                [
                    _scanned_order("1002", "2026-09-13", ["72"]),
                    _scanned_order("1003", "2026-09-20", ["72"]),
                ]
            ),
        )
        run_sync_apply(path2, initialized_home)
        # P is still the primary of fam-71; Q owns its own family as primary.
        assert _family_row(initialized_home.database_file, "fam-71")[0] == p_row
        q_row = _product_row_id(initialized_home.database_file, "72")
        assert _family_row(initialized_home.database_file, "fam-72")[0] == q_row

    def test_inference_is_idempotent(self, initialized_home: GrossyPaths, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order("1001", "2026-09-06", ["71"]),
                    _scanned_order("1002", "2026-09-13", ["71"]),
                ]
            ),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            for _ in range(3):
                run_inference(conn, NOW)
                conn.commit()
            family_count = conn.execute("SELECT COUNT(*) FROM product_families").fetchone()[0]
            member_count = conn.execute("SELECT COUNT(*) FROM family_members").fetchone()[0]
            row = conn.execute(
                "SELECT recurrence_weeks, recurrence_confidence, last_purchase_date "
                "FROM product_families WHERE family_id='fam-71'"
            ).fetchone()
        finally:
            conn.close()
        # Re-running inference many times is stable: one family, one member,
        # and the recurrence facts do not drift.
        assert family_count == 1
        assert member_count == 1
        assert row == (1, "high", "2026-09-13")


# --- recurrence, family cadence, last purchase (FR-47, FR-50) ---------------


class TestFamilyRecurrence:
    def test_recurrence_advances_from_completed_orders(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"])]),
        )
        run_sync_apply(path1, initialized_home)
        row1 = _family_row(initialized_home.database_file, "fam-71")
        # Single occurrence -> guarded category estimate, low confidence.
        assert row1[1] == 1
        assert row1[2] == "category-estimate"
        assert row1[3] == "low"
        assert row1[4] == "2026-09-06"  # last purchase date

        # A second completed order turns it into observed recurrence and
        # advances the last purchase date.
        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload([_scanned_order("1002", "2026-09-13", ["71"])]),
        )
        run_sync_apply(path2, initialized_home)
        row2 = _family_row(initialized_home.database_file, "fam-71")
        assert row2[2] == "observed"
        assert row2[3] == "high"
        assert row2[4] == "2026-09-13"

    def test_single_occurrence_family_is_reviewable(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"])]),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            review = conn.execute(
                "SELECT kind, target_type, target_id FROM review_items "
                "WHERE kind='recurrence' AND target_id='fam-71'"
            ).fetchall()
        finally:
            conn.close()
        assert len(review) == 1
        assert review[0][1] == "family"

    def test_recurrence_review_cleared_after_second_occurrence(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # A single dated occurrence leaves a reviewable recurrence item...
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"])]),
        )
        run_sync_apply(path1, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            review = conn.execute(
                "SELECT 1 FROM review_items "
                "WHERE kind='recurrence' AND target_type='family' AND target_id='fam-71'"
            ).fetchall()
        finally:
            conn.close()
        assert len(review) == 1

        # ...that a second dated occurrence resolves into observed recurrence;
        # the stale review item must be cleared so the review state always
        # matches the current evidence.
        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload([_scanned_order("1002", "2026-09-13", ["71"])]),
        )
        run_sync_apply(path2, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            review = conn.execute(
                "SELECT 1 FROM review_items "
                "WHERE kind='recurrence' AND target_type='family' AND target_id='fam-71'"
            ).fetchall()
        finally:
            conn.close()
        assert review == []

    def test_report_surfaces_recurrence_and_review(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order("1001", "2026-09-06", ["71"], category=None),
                    _scanned_order("1002", "2026-09-13", ["72"]),
                ]
            ),
        )
        result = run_sync_apply(path, initialized_home)
        assert result.report_path is not None
        text = result.report_path.read_text(encoding="utf-8")
        assert "deferred" not in text
        assert "auto-add" in text  # recurrence section rendered
        # Product 71 (no heading, single occurrence) yields review lines.
        assert "requiring review" in text


# --- persisted observation updates (FR-18, FR-36, FR-37) -------------------


class TestObservationUpdates:
    def test_absent_heading_does_not_erase_learned_category(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # First observation carries a heading; a later card without a heading
        # is absence of evidence, not evidence of absence: the learned
        # category is preserved and no review item is raised.
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"], category="Dairy")]),
        )
        run_sync_apply(path1, initialized_home)
        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload([_scanned_order("1002", "2026-09-13", ["71"], category=None)]),
        )
        run_sync_apply(path2, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT c.name FROM products p "
                "LEFT JOIN categories c ON c.id = p.category_id "
                "WHERE p.retailer = 'ab' AND p.retailer_product_id = '71'"
            ).fetchone()
            review = conn.execute(
                "SELECT 1 FROM review_items "
                "WHERE kind = 'category' AND target_type = 'product' AND target_id = '71'"
            ).fetchall()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "dairy"
        assert review == []

    def test_changed_heading_updates_category_and_reports_change(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # A present heading that normalizes to a different category updates
        # the stored category and is reported as a changed product fact.
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"], category="Dairy")]),
        )
        run_sync_apply(path1, initialized_home)
        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload([_scanned_order("1002", "2026-09-13", ["71"], category="Bakery")]),
        )
        result = run_sync_apply(path2, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT c.name FROM products p "
                "JOIN categories c ON c.id = p.category_id "
                "WHERE p.retailer = 'ab' AND p.retailer_product_id = '71'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "bakery"
        assert result.stats.changed_products == 1
        assert any(
            "category updated" in reason
            for _name, _category, reason in result.stats.changed_product_facts
        )


# --- family-owned category (FR-40) -----------------------------------------


class TestFamilyCategory:
    def test_family_category_derived_from_primary(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # The family owns a category (FR-40); inference derives it from the
        # primary product's normalized category.
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order("1001", "2026-09-06", ["71"], category="Dairy"),
                    _scanned_order("1002", "2026-09-13", ["71"], category="Dairy"),
                ]
            ),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT f.category_id, c.name FROM product_families f "
                "LEFT JOIN categories c ON c.id = f.category_id "
                "WHERE f.family_id = 'fam-71'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] is not None
        assert row[1] == "dairy"

    def test_family_category_not_overwritten_by_later_inference(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # A later (e.g. manual, task 06) family category assignment must
        # survive subsequent inference passes: inference only fills an unset
        # family category, it never reverses an existing one.
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"], category="Dairy")]),
        )
        run_sync_apply(path1, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            conn.execute(
                "INSERT INTO categories (name, display_name, created_at) "
                "VALUES ('bakery', 'Bakery', ?)",
                (NOW,),
            )
            conn.execute(
                "UPDATE product_families SET category_id = "
                "(SELECT id FROM categories WHERE name = 'bakery') "
                "WHERE family_id = 'fam-71'"
            )
            conn.commit()
        finally:
            conn.close()

        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload([_scanned_order("1002", "2026-09-13", ["71"], category="Dairy")]),
        )
        run_sync_apply(path2, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT c.name FROM product_families f "
                "JOIN categories c ON c.id = f.category_id "
                "WHERE f.family_id = 'fam-71'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "bakery"


# --- alternatives: advance cadence without promotion (FR-46..FR-62) ---------


class TestAlternatives:
    def _link_alternative(self, db: Path, family_id: str, product_id: str) -> None:
        conn = open_connection(db)
        try:
            link_alternative(conn, family_id, product_id, NOW)
            conn.commit()
        finally:
            conn.close()

    def test_primary_selected_first(self, initialized_home: GrossyPaths, tmp_path: Path) -> None:
        # Import P (primary of fam-71) and A (primary of its own fam-80).
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71", "80"])]),
        )
        run_sync_apply(path, initialized_home)
        self._link_alternative(initialized_home.database_file, "fam-71", "80")
        conn = open_connection(initialized_home.database_file)
        try:
            sel = select_family_product(conn, "fam-71", frozenset({"71", "80"}))
        finally:
            conn.close()
        assert sel is not None
        assert sel.role == "primary"
        assert sel.retailer_product_id == "71"
        assert sel.promoted is False

    def test_unavailable_primary_falls_back_to_approved_alternative(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71", "80"])]),
        )
        run_sync_apply(path, initialized_home)
        self._link_alternative(initialized_home.database_file, "fam-71", "80")
        conn = open_connection(initialized_home.database_file)
        try:
            sel = select_family_product(conn, "fam-71", frozenset({"80"}))
        finally:
            conn.close()
        assert sel is not None
        assert sel.role == "alternative"
        assert sel.retailer_product_id == "80"
        assert sel.promoted is False

    def test_no_available_member_returns_none(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71", "80"])]),
        )
        run_sync_apply(path, initialized_home)
        self._link_alternative(initialized_home.database_file, "fam-71", "80")
        conn = open_connection(initialized_home.database_file)
        try:
            sel = select_family_product(conn, "fam-71", frozenset())
        finally:
            conn.close()
        assert sel is None

    def test_alternative_purchase_advances_cadence_without_promotion(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71", "80"])]),
        )
        run_sync_apply(path1, initialized_home)
        self._link_alternative(initialized_home.database_file, "fam-71", "80")
        primary_row = _product_row_id(initialized_home.database_file, "71")

        # A completed order containing the alternative on a later date.
        # Inference advances the family cadence (FR-47) without promoting.
        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload([_scanned_order("1002", "2026-10-01", ["80"])]),
        )
        run_sync_apply(path2, initialized_home)
        row = _family_row(initialized_home.database_file, "fam-71")
        # Cadence advanced to the alternative's completed order date...
        assert row[4] == "2026-10-01"
        # ...but the established primary is unchanged (no promotion, FR-62).
        assert row[0] == primary_row

        # The next due run tries the primary again.
        conn = open_connection(initialized_home.database_file)
        try:
            sel = select_family_product(conn, "fam-71", frozenset({"71", "80"}))
        finally:
            conn.close()
        assert sel is not None
        assert sel.role == "primary"
        assert sel.retailer_product_id == "71"

    def test_advance_is_monotonic(self, initialized_home: GrossyPaths, tmp_path: Path) -> None:
        # Import two orders; inference sets last_purchase to the later date.
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"])]),
        )
        run_sync_apply(path1, initialized_home)
        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload([_scanned_order("1002", "2026-10-01", ["71"])]),
        )
        run_sync_apply(path2, initialized_home)
        # After inference, last_purchase is 2026-10-01. An earlier date is
        # rejected (monotonic), and the same date is also rejected.
        conn = open_connection(initialized_home.database_file)
        try:
            earlier = advance_family_cadence(conn, "fam-71", "2026-09-06")
            same = advance_family_cadence(conn, "fam-71", "2026-10-01")
            conn.commit()
        finally:
            conn.close()
        assert earlier is False  # an earlier date never moves the cadence back
        assert same is False  # same date does not re-advance
        assert _family_row(initialized_home.database_file, "fam-71")[4] == "2026-10-01"

    def test_advance_rejects_date_without_order_evidence(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # A date that does not correspond to any synchronized order must be
        # rejected (FR-47: only synchronized completed order appearances
        # advance cadence).
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"])]),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            with pytest.raises(InferenceError, match="no synchronized completed order"):
                advance_family_cadence(conn, "fam-71", "2026-12-25")
            conn.rollback()
        finally:
            conn.close()

    def test_advance_rejects_date_without_family_member(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # An order that exists but does not contain a member of the target
        # family must be rejected.
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order("1001", "2026-09-06", ["71"]),
                    _scanned_order("1002", "2026-09-13", ["99"]),  # different product
                ]
            ),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            with pytest.raises(InferenceError, match="no synchronized completed order"):
                advance_family_cadence(conn, "fam-71", "2026-09-13")
            conn.rollback()
        finally:
            conn.close()

    def test_alternative_advances_family_last_purchase_via_inference(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Sequence: 71 is bought (order 1), 80 first appears (order 2), 80 is
        # linked into fam-71 as an alternative, then 80 is bought again
        # (order 3). Because recurrence is family-owned over the union of
        # members, order 3 advances fam-71's last-purchase date (FR-47) without
        # promoting the primary (FR-62).
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"])]),
        )
        run_sync_apply(path1, initialized_home)
        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload([_scanned_order("1002", "2026-09-13", ["80"])]),
        )
        run_sync_apply(path2, initialized_home)
        self._link_alternative(initialized_home.database_file, "fam-71", "80")
        path3 = _write(
            tmp_path,
            "s3.json",
            _scan_payload([_scanned_order("1003", "2026-09-20", ["80"])]),
        )
        run_sync_apply(path3, initialized_home)
        row = _family_row(initialized_home.database_file, "fam-71")
        assert row[4] == "2026-09-20"  # advanced by the alternative's order
        primary_row = _product_row_id(initialized_home.database_file, "71")
        assert row[0] == primary_row  # primary still the original product


# --- invariants and error handling -----------------------------------------


class TestInvariants:
    def _link_alternative(self, db: Path, family_id: str, product_id: str) -> None:
        conn = open_connection(db)
        try:
            link_alternative(conn, family_id, product_id, NOW)
            conn.commit()
        finally:
            conn.close()

    def test_link_alternative_unknown_product_raises(self, initialized_home: GrossyPaths) -> None:
        conn = open_connection(initialized_home.database_file)
        try:
            with pytest.raises(InferenceError):
                link_alternative(conn, "fam-1", "999999", NOW)
        finally:
            conn.close()

    def test_advance_unknown_family_raises(self, initialized_home: GrossyPaths) -> None:
        conn = open_connection(initialized_home.database_file)
        try:
            with pytest.raises(InferenceError):
                advance_family_cadence(conn, "fam-none", "2026-10-01")
        finally:
            conn.close()

    def test_link_alternative_rejects_self_link_of_primary(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Linking a product that is the primary of the target family must be
        # rejected (FR-42, FR-44): it would destroy the established primary.
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"])]),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            with pytest.raises(InferenceError, match="primary of family"):
                link_alternative(conn, "fam-71", "71", NOW)
            conn.rollback()
        finally:
            conn.close()

    def test_link_alternative_rejects_already_member(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Linking a product that is already an alternative in the target family
        # must be rejected.
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71", "80"])]),
        )
        run_sync_apply(path, initialized_home)
        self._link_alternative(initialized_home.database_file, "fam-71", "80")
        conn = open_connection(initialized_home.database_file)
        try:
            with pytest.raises(InferenceError, match="already a member"):
                link_alternative(conn, "fam-71", "80", NOW)
            conn.rollback()
        finally:
            conn.close()


class TestFamilyLinkInference:
    """Tests for FR-55 (high-confidence auto-apply) and FR-56 (uncertain review)."""

    def test_high_confidence_same_base_name_auto_links(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Two products in the same category with the same base name (differing
        # only in package details) are automatically linked (FR-55) when their
        # families are created in the same inference pass (initial inference).
        # Here both products are imported with their correct names from the
        # start, so the first sync creates the families AND links them.
        orders = [
            _scanned_order("1001", "2026-09-06", ["71", "72"], category="Dairy"),
        ]
        # Override the titles to have the same base name but different packages.
        orders[0]["products"] = [
            _scanned_product("71", title="Olympus Milk 1L", category="Dairy"),
            _scanned_product("72", title="Olympus Milk 2L", category="Dairy"),
        ]
        path = _write(tmp_path, "scan.json", _scan_payload(orders))
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            # Product 72 should now be an alternative in fam-71 (auto-linked
            # in the initial inference pass).
            row = conn.execute(
                "SELECT m.role, m.approval_state, f.family_id FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "JOIN product_families f ON f.id = m.family_id "
                "WHERE p.retailer_product_id = '72'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "alternative"
        assert row[1] == "approved"

        # The primary of fam-71 is unchanged (product 71).
        primary_row = _product_row_id(initialized_home.database_file, "71")
        row = _family_row(initialized_home.database_file, "fam-71")
        assert row[0] == primary_row

    def test_different_base_names_same_category_stays_separate(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Two products in the same category with different base names that do
        # not share a 2-word prefix remain separate (no review item, no link).
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order(
                        "1001",
                        "2026-09-06",
                        ["71", "72"],
                        category="Dairy",
                    ),
                ]
            ),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            conn.execute(
                "UPDATE products SET name = 'Olympus Milk 1L' WHERE retailer_product_id = '71'"
            )
            conn.execute(
                "UPDATE products SET name = 'Foninis Cheese 200g' WHERE retailer_product_id = '72'"
            )
            conn.commit()
        finally:
            conn.close()

        conn = open_connection(initialized_home.database_file)
        try:
            run_inference(conn, NOW)
            conn.commit()
            # No family_link review items for this pair (prefix < 2 words).
            review_count = conn.execute(
                "SELECT COUNT(*) FROM review_items WHERE kind = 'family_link'"
            ).fetchone()[0]
            # Product 72 remains in its own family.
            row = conn.execute(
                "SELECT m.role FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE p.retailer_product_id = '72'"
            ).fetchone()
        finally:
            conn.close()
        assert review_count == 0
        assert row is not None
        assert row[0] == "primary"  # still primary of its own family

    def test_uncertain_common_prefix_generates_review_item(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Two products in the same category whose base names share a 2-word
        # prefix produce a family_link review item (FR-56) and remain separate.
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order(
                        "1001",
                        "2026-09-06",
                        ["71", "72"],
                        category="Dairy",
                    ),
                ]
            ),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            conn.execute(
                "UPDATE products SET name = 'Olympus Greek Yogurt 500g' "
                "WHERE retailer_product_id = '71'"
            )
            conn.execute(
                "UPDATE products SET name = 'Olympus Greek Kefir 200ml' "
                "WHERE retailer_product_id = '72'"
            )
            conn.commit()
        finally:
            conn.close()

        conn = open_connection(initialized_home.database_file)
        try:
            run_inference(conn, NOW)
            conn.commit()
            review = conn.execute(
                "SELECT kind, target_type, target_id FROM review_items WHERE kind = 'family_link'"
            ).fetchall()
        finally:
            conn.close()
        assert len(review) >= 1
        # The products remain in separate families.
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT m.role FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE p.retailer_product_id = '72'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "primary"  # still primary of its own family

    def test_a_product_belongs_to_at_most_one_family(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71", "80"])]),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            link_alternative(conn, "fam-71", "80", NOW)  # moves 80 to fam-71
            conn.commit()
            memberships = conn.execute(
                "SELECT COUNT(*) FROM family_members WHERE product_id = "
                "(SELECT id FROM products WHERE retailer_product_id='80')"
            ).fetchone()[0]
        finally:
            conn.close()
        assert memberships == 1

    def test_multi_product_cluster_links_to_most_established_anchor(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Three products sharing a category and base name form one cluster.
        # When their families were established in a prior sync, later inference
        # must NOT demote any established primary (FR-44). The products remain
        # in their separate families and review items are generated for the
        # high-confidence links that cannot be auto-applied.
        path = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order("1001", "2026-09-06", ["71", "72", "73"], category="Dairy"),
                    _scanned_order("1002", "2026-09-13", ["73"], category="Dairy"),
                    _scanned_order("1003", "2026-09-20", ["73"], category="Dairy"),
                ]
            ),
        )
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            for pid, suffix in (("71", "1L"), ("72", "2L"), ("73", "3L")):
                conn.execute(
                    "UPDATE products SET name = ? WHERE retailer_product_id = ?",
                    (f"Olympus Milk {suffix}", pid),
                )
            conn.commit()
        finally:
            conn.close()

        conn = open_connection(initialized_home.database_file)
        try:
            summary = run_inference(conn, NOW)
            conn.commit()
            roles = dict(
                conn.execute(
                    "SELECT p.retailer_product_id, m.role FROM family_members m "
                    "JOIN products p ON p.id = m.product_id"
                ).fetchall()
            )
            family_count = conn.execute("SELECT COUNT(*) FROM product_families").fetchone()[0]
            orphaned = conn.execute(
                "SELECT COUNT(*) FROM product_families f WHERE NOT EXISTS "
                "(SELECT 1 FROM family_members m WHERE m.family_id = f.id "
                "AND m.role = 'primary')"
            ).fetchone()[0]
            review_count = conn.execute(
                "SELECT COUNT(*) FROM review_items WHERE kind = 'family_link'"
            ).fetchone()[0]
        finally:
            conn.close()
        # All three products remain primaries of their own established families
        # (FR-44: established primaries are never demoted by inference).
        assert roles == {"71": "primary", "72": "primary", "73": "primary"}
        assert family_count == 3
        assert orphaned == 0
        # Review items are generated for the high-confidence links that cannot
        # be auto-applied due to the fixed-primary constraint.
        assert review_count >= 2
        # No links were applied.
        assert len(summary.family_links) == 0

    def test_family_links_proposal_rejects_invalid_payloads(self) -> None:
        # The family-links boundary is schema-bound and fail-closed (FR-55):
        # only high-confidence, non-duplicate, closed-schema links validate.

        def make(**overrides: Any) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "schema_name": "grossy.history.family-links.v1",
                "schema_version": 1,
                "generated_at": GENERATED_AT,
                "links": [
                    {
                        "product_id": "72",
                        "target_family_id": "fam-71",
                        "base_name": "Olympus Milk",
                        "category": "dairy",
                        "confidence": "high",
                        "provenance": "inferred",
                    }
                ],
            }
            payload.update(overrides)
            return payload

        validated = validate_family_links(make())
        assert len(validated.links) == 1

        dup = make()
        dup["links"].append(dict(dup["links"][0]))
        with pytest.raises(HistoryProposalError):
            validate_family_links(dup)

        low = make()
        low["links"][0]["confidence"] = "medium"
        with pytest.raises(HistoryProposalError):
            validate_family_links(low)

        extra = make()
        extra["links"][0]["note"] = "unexpected"
        with pytest.raises(HistoryProposalError):
            validate_family_links(extra)

        wrong = make(schema_name="grossy.history.family-links.v2")
        with pytest.raises(HistoryProposalError):
            validate_family_links(wrong)

    def test_family_links_validation_failure_fails_closed(
        self, initialized_home: GrossyPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A family-links proposal that fails validation makes the whole
        # inference pass fail closed: the caller's transaction rolls back and
        # no partial membership change is persisted (FR-26). This test uses
        # products whose families are created in the same pass as the link
        # (initial inference) so the auto-link path is exercised.
        orders = [_scanned_order("1001", "2026-09-06", ["71", "72", "73"], category="Dairy")]
        orders[0]["products"] = [
            _scanned_product("71", title="Olympus Milk 1L", category="Dairy"),
            _scanned_product("72", title="Olympus Milk 2L", category="Dairy"),
            _scanned_product("73", title="Olympus Milk 3L", category="Dairy"),
        ]
        path = _write(tmp_path, "scan.json", _scan_payload(orders))

        def _reject(proposal: dict[str, Any]) -> Any:
            raise HistoryProposalError("simulated validation failure")

        monkeypatch.setattr(learn_mod, "validate_family_links", _reject)
        try:
            with pytest.raises((InferenceError, HistorySyncError)):
                run_sync_apply(path, initialized_home)
        finally:
            monkeypatch.undo()
        # The failed pass persisted nothing: every product still owns its own
        # family (or no family at all if the transaction rolled back).
        conn = open_connection(initialized_home.database_file)
        try:
            roles = dict(
                conn.execute(
                    "SELECT p.retailer_product_id, m.role FROM family_members m "
                    "JOIN products p ON p.id = m.product_id"
                ).fetchall()
            )
        finally:
            conn.close()
        # Either no families were created (full rollback) or each product is
        # still the primary of its own single-member family.
        assert roles == {} or all(r == "primary" for r in roles.values())

        # A subsequent healthy pass with newly created families links correctly.
        monkeypatch.undo()
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            roles = dict(
                conn.execute(
                    "SELECT p.retailer_product_id, m.role FROM family_members m "
                    "JOIN products p ON p.id = m.product_id"
                ).fetchall()
            )
        finally:
            conn.close()
        # All counts tie and created_at ties, so the earliest stable ID (71)
        # anchors the cluster.
        assert roles == {"71": "primary", "72": "alternative", "73": "alternative"}


# --- regression: established primary not demoted (FR-44) -------------------


class TestEstablishedPrimaryNotDemoted:
    """Regression: inference must never demote or replace an established family primary."""

    def test_later_inference_does_not_demote_established_primary(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Product 71 is imported first (established primary of fam-71). A later
        # sync imports product 73 with more purchases and the same base name.
        # Inference must NOT demote 71 to an alternative (FR-44).
        path1 = _write(
            tmp_path,
            "s1.json",
            _scan_payload([_scanned_order("1001", "2026-09-06", ["71"], category="Dairy")]),
        )
        run_sync_apply(path1, initialized_home)
        # Verify 71 is the primary of fam-71.
        p_row = _product_row_id(initialized_home.database_file, "71")
        assert _family_row(initialized_home.database_file, "fam-71")[0] == p_row

        # Update names so they share a base name.
        conn = open_connection(initialized_home.database_file)
        try:
            conn.execute(
                "UPDATE products SET name = 'Olympus Milk 1L' WHERE retailer_product_id = '71'"
            )
            conn.commit()
        finally:
            conn.close()

        # A later sync imports product 73 (more purchases, same base name).
        orders = [
            _scanned_order("1002", "2026-09-13", ["73"], category="Dairy"),
            _scanned_order("1003", "2026-09-20", ["73"], category="Dairy"),
        ]
        orders[0]["products"] = [
            _scanned_product("73", title="Olympus Milk 3L", category="Dairy"),
        ]
        orders[1]["products"] = [
            _scanned_product("73", title="Olympus Milk 3L", category="Dairy"),
        ]
        path2 = _write(tmp_path, "s2.json", _scan_payload(orders))
        run_sync_apply(path2, initialized_home)

        # Product 71 must still be the primary of fam-71.
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT m.role FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE p.retailer_product_id = '71'"
            ).fetchone()
            fam_row = conn.execute(
                "SELECT primary_product_id FROM product_families WHERE family_id = 'fam-71'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "primary"  # still primary, not demoted
        assert fam_row is not None  # fam-71 still exists
        assert fam_row[0] == p_row  # primary unchanged


# --- regression: overlapping linked/uncertain review preserved ---------------


class TestOverlappingLinkedUncertainReview:
    """Regression: unresolved family-link review items must remain until the
    specific candidate relation resolves, even if the target product is in a
    multi-member family through a DIFFERENT link."""

    def test_uncertain_review_survives_when_target_is_linked_to_different_product(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Three products in the same category:
        # - A and B share an identical base name (high-confidence link)
        # - A and C share a 2-word prefix but different base names (uncertain)
        # A and B are auto-linked (first pass). The uncertain A->C review must
        # survive because A and C are still in different families.
        orders = [
            _scanned_order("1001", "2026-09-06", ["71", "72", "73"], category="Dairy"),
        ]
        orders[0]["products"] = [
            _scanned_product("71", title="Olympus Greek Yogurt 500g", category="Dairy"),
            _scanned_product("72", title="Olympus Greek Yogurt 1L", category="Dairy"),
            _scanned_product("73", title="Olympus Greek Kefir 200ml", category="Dairy"),
        ]
        path = _write(tmp_path, "scan.json", _scan_payload(orders))
        run_sync_apply(path, initialized_home)

        conn = open_connection(initialized_home.database_file)
        try:
            # 71 and 72 share base name "olympus greek yogurt" -> auto-linked.
            # 71 and 73 share prefix "olympus greek" but different base names
            # -> uncertain review item.
            # After auto-link, 71 is in a multi-member family (with 72).
            # The review item for the 71<->73 uncertain pair must survive.
            roles = dict(
                conn.execute(
                    "SELECT p.retailer_product_id, m.role FROM family_members m "
                    "JOIN products p ON p.id = m.product_id"
                ).fetchall()
            )
            reviews = conn.execute(
                "SELECT target_id, reason FROM review_items WHERE kind = 'family_link'"
            ).fetchall()
        finally:
            conn.close()
        # 71 and 72 are in the same family (auto-linked, first pass).
        # 73 is in its own family.
        assert roles["71"] == "primary" or roles["71"] == "alternative"
        assert roles["72"] == "primary" or roles["72"] == "alternative"
        assert roles["73"] == "primary"  # 73 remains separate

        # Review items for the uncertain pairs (71<->73 and 72<->73) must
        # survive despite 71 and 72 being in a multi-member family (linked to
        # each other). The specific uncertain pairs (with 73) are unresolved.
        assert len(reviews) >= 1
        # At least one review item references product 73 as the uncertain
        # counterpart (the kefir product that is still in a separate family).
        assert any("'73'" in r[1] for r in reviews), (
            f"Expected a family_link review item referencing product 73, got: {reviews}"
        )

    def test_review_cleared_only_when_specific_pair_resolves(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # A and B share an uncertain prefix. They start separate. A is then
        # manually linked to C (a different product) forming a multi-member
        # family. The A<->B review must survive because A and B are still in
        # different families.
        orders = [
            _scanned_order("1001", "2026-09-06", ["71", "72", "80"], category="Dairy"),
        ]
        orders[0]["products"] = [
            _scanned_product("71", title="Olympus Greek Yogurt 500g", category="Dairy"),
            _scanned_product("72", title="Olympus Greek Kefir 200ml", category="Dairy"),
            _scanned_product("80", title="Dairy Milk 1L", category="Dairy"),
        ]
        path = _write(tmp_path, "scan.json", _scan_payload(orders))
        run_sync_apply(path, initialized_home)

        # Generate an uncertain review for 71<->72 (same prefix "olympus greek",
        # different base names). Then manually link 80 into fam-71 so 71 is in
        # a multi-member family. The 71<->72 review must survive.
        conn = open_connection(initialized_home.database_file)
        try:
            # Run inference to generate the uncertain review item.
            run_inference(conn, NOW)
            conn.commit()
            # Manually link 80 into fam-71 (making it multi-member).
            link_alternative(conn, "fam-71", "80", NOW)
            conn.commit()
        finally:
            conn.close()

        # Run inference again: 71 is now in a multi-member family (with 80),
        # but 72 is still in its own family. The 71<->72 review must survive.
        conn = open_connection(initialized_home.database_file)
        try:
            run_inference(conn, NOW)
            conn.commit()
            reviews = conn.execute(
                "SELECT target_id, reason FROM review_items WHERE kind = 'family_link'"
            ).fetchall()
            # 72 is still primary of its own family.
            row = conn.execute(
                "SELECT m.role FROM family_members m "
                "JOIN products p ON p.id = m.product_id "
                "WHERE p.retailer_product_id = '72'"
            ).fetchone()
        finally:
            conn.close()
        # The uncertain review for 71<->72 must survive (72 is still separate).
        assert len(reviews) >= 1
        assert row is not None
        assert row[0] == "primary"  # 72 is still in its own family


# --- regression: explicit misc heading never creates misc category -----------


class TestMiscHeadingRejected:
    """Regression: an explicit 'misc' heading must not create a misc category."""

    def test_explicit_misc_heading_leaves_product_unassigned(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # A product with category_heading="misc" must not create a misc
        # category (FR-37, PRD 7.3.20). The product is left unassigned and a
        # reviewable category item is generated.
        orders = [
            _scanned_order("1001", "2026-09-06", ["71"], category="Dairy"),
        ]
        orders[0]["products"] = [
            _scanned_product("71", title="Product 71", category="misc"),
        ]
        path = _write(tmp_path, "scan.json", _scan_payload(orders))
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            # No misc category should exist.
            names = {c[0] for c in conn.execute("SELECT name FROM categories").fetchall()}
            assert "misc" not in names
            # The product is unassigned (category_id is NULL).
            row = conn.execute(
                "SELECT category_id FROM products WHERE retailer_product_id = '71'"
            ).fetchone()
            assert row is not None
            assert row[0] is None
            # A reviewable category item is generated.
            review = conn.execute(
                "SELECT 1 FROM review_items WHERE kind = 'category' AND target_id = '71'"
            ).fetchall()
            assert len(review) == 1
        finally:
            conn.close()


# --- normalized unit basis for package facts (FR-32) ------------------------


class TestUnitBasis:
    """The stored unit basis is derived where determinable and never guessed."""

    def test_unit_basis_derived_on_insert(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        order = _scanned_order("1001", "2026-09-06", ["71", "72"])
        order["products"][0]["package_unit"] = "ml"
        order["products"][1]["package_unit"] = "pcs"
        path = _write(tmp_path, "s1.json", _scan_payload([order]))
        run_sync_apply(path, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            basis = dict(
                conn.execute(
                    "SELECT retailer_product_id, unit_basis FROM products "
                    "ORDER BY retailer_product_id"
                ).fetchall()
            )
        finally:
            conn.close()
        assert basis == {"71": "ml", "72": "count"}

    def test_unit_basis_preserved_when_indeterminate(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # A later card without a determinable unit is absence of evidence: the
        # stored basis is preserved and no unit-basis change is reported.
        order1 = _scanned_order("1001", "2026-09-06", ["71"])
        order1["products"][0]["package_unit"] = "ml"
        path1 = _write(tmp_path, "s1.json", _scan_payload([order1]))
        run_sync_apply(path1, initialized_home)
        path2 = _write(
            tmp_path,
            "s2.json",
            _scan_payload([_scanned_order("1002", "2026-09-13", ["71"])]),
        )
        result = run_sync_apply(path2, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT unit_basis FROM products WHERE retailer_product_id = '71'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "ml"
        assert all(
            "unit basis" not in reason
            for _name, _category, reason in result.stats.changed_product_facts
        )

    def test_unit_basis_updated_and_reported_when_determinable(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # A later card with a different determinable unit updates the stored
        # basis and is reported as a changed product fact.
        order1 = _scanned_order("1001", "2026-09-06", ["71"])
        order1["products"][0]["package_unit"] = "ml"
        path1 = _write(tmp_path, "s1.json", _scan_payload([order1]))
        run_sync_apply(path1, initialized_home)
        order2 = _scanned_order("1002", "2026-09-13", ["71"])
        order2["products"][0]["package_unit"] = "l"
        path2 = _write(tmp_path, "s2.json", _scan_payload([order2]))
        result = run_sync_apply(path2, initialized_home)
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT unit_basis FROM products WHERE retailer_product_id = '71'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "l"
        assert any(
            "unit basis updated" in reason
            for _name, _category, reason in result.stats.changed_product_facts
        )
