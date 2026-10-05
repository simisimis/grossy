"""Tests for weekly baseline and recurring-item preview (Task 09).

Covers:
- plan-prepare: run creation, one-active-run enforcement, history check.
- baseline-apply: observation validation, baseline persistence, recurring
  item resolution, positive-delta preview, budget, report generation.
- YAML/learned precedence, family consolidation, confidence gating.
- No negative deltas, excess preservation, fail-closed behavior.
- Review corrections: sensitive sanitization, empty-baseline retry,
  approved-only credit, substitution without availability claims,
  consolidation with blocked entries, product-name ordering.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from grossy.cli import app
from grossy.db.connection import open_connection
from grossy.init import do_init
from grossy.paths import GrossyPaths, resolve_paths
from grossy.weekly import WeeklyPlanError, run_baseline_apply, run_plan_prepare

RUNNER = CliRunner()
ATHENS_TZ = ZoneInfo("Europe/Athens")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> GrossyPaths:
    """Create isolated Grossy paths under tmp_path."""
    return resolve_paths(
        config_home=tmp_path / "config",
        data_home=tmp_path / "data",
        state_home=tmp_path / "state",
    )


def _init_db(paths: GrossyPaths) -> None:
    """Initialize the database with migrations."""
    do_init(paths)


def _sample_config_text(budget: float = 100.0) -> str:
    return f"""
schema_version: 1
retailer: ab
timezone: Europe/Athens
currency: EUR
household:
  adults: 2
  children: 1
  recipe_servings: 3
weekly_budget: {budget}
material_unit_price_improvement_pct: 15
disliked_ingredients: []
pantry_assumptions: []
appliances:
  thermomix:
    available: true
    model: null
  dough_kneading_machine:
    available: true
  steam_oven:
    available: true
  air_fryer:
    available: true
    capacity_litres: null
  induction_cooktop:
    available: true
  microwave:
    available: true
    wattage: null
  pressure_cooker:
    available: true
    kind: null
    capacity_litres: null
  dutch_oven:
    available: true
    capacity_litres: null
appliance_preferences:
  prefer_air_fryer_over_pan_frying: true
  prefer_thermomix_native_over_manual: true
staples:
  breakfast:
    - key: milk
      product_id: "1111111"
      quantity: 2
  lunch: []
"""


def _write_config(paths: GrossyPaths, text: str | None = None) -> None:
    """Write a sample config to the paths, overwriting any existing config."""
    if text is None:
        text = _sample_config_text()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")


def _product_url(pid: str) -> str:
    return f"https://www.ab.gr/en/eshop/Food/item-{pid}/p/{pid}"


def _cart_observation(
    product_ids: list[str], quantities: list[int] | None = None
) -> dict[str, Any]:
    """Build a valid cart observation dict."""
    if quantities is None:
        quantities = [1] * len(product_ids)
    return {
        "schema_name": "grossy.cart.observation.v1",
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "lines": [
            {
                "product_id": pid,
                "canonical_url": _product_url(pid),
                "quantity": qty,
                "displayed_price_eur": "2.50",
            }
            for pid, qty in zip(product_ids, quantities, strict=True)
        ],
    }


def _seed_source_orders(paths: GrossyPaths, count: int = 6) -> None:
    """Insert imported orders matching the deterministic listed-order fixture."""
    conn = open_connection(paths.database_file)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        for i in range(count):
            order_id = f"{100001 + i}"
            conn.execute(
                "INSERT OR IGNORE INTO source_orders "
                "(retailer, source_order_id, displayed_label, order_date, "
                "import_timestamp, created_at) "
                "VALUES ('ab', ?, '01/01/2026', '2026-01-01', ?, ?)",
                (order_id, now, now),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_family(
    paths: GrossyPaths,
    family_id: str,
    product_id: str,
    recurrence_weeks: int,
    last_purchase_date: str,
    recurrence_source: str = "observed",
    recurrence_confidence: str = "high",
    category_name: str = "dairy",
) -> None:
    """Seed a product family with recurrence for testing."""
    conn = open_connection(paths.database_file)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        # Create category if needed.
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, display_name, created_at) VALUES (?, ?, ?)",
            (category_name, category_name.title(), now),
        )
        cat_row = conn.execute(
            "SELECT id FROM categories WHERE name = ?", (category_name,)
        ).fetchone()
        cat_id = cat_row[0] if cat_row else None

        # Create product.
        conn.execute(
            "INSERT OR IGNORE INTO products "
            "(retailer, retailer_product_id, canonical_url, name, purchase_order_count, "
            "typical_quantity, blacklist_state, observation_state, "
            "category_id, created_at, updated_at) "
            "VALUES ('ab', ?, ?, ?, 1, 1, 'active', 'active', ?, ?, ?)",
            (product_id, _product_url(product_id), f"Product {product_id}", cat_id, now, now),
        )
        prod_row = conn.execute(
            "SELECT id FROM products WHERE retailer_product_id = ?", (product_id,)
        ).fetchone()
        prod_id = prod_row[0] if prod_row else None

        if prod_id is None:
            return

        # Create family with NULL primary (trigger 003 requires this order).
        conn.execute(
            "INSERT OR IGNORE INTO product_families "
            "(family_id, name, primary_product_id, recurrence_weeks, recurrence_source, "
            "recurrence_confidence, last_purchase_date, enabled, blacklist_state, "
            "category_id, created_at, updated_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, 1, 'active', ?, ?, ?)",
            (
                family_id,
                f"Family {family_id}",
                recurrence_weeks,
                recurrence_source,
                recurrence_confidence,
                last_purchase_date,
                cat_id,
                now,
                now,
            ),
        )
        fam_row = conn.execute(
            "SELECT id FROM product_families WHERE family_id = ?", (family_id,)
        ).fetchone()
        fam_id = fam_row[0] if fam_row else None

        if fam_id is not None:
            conn.execute(
                "INSERT OR IGNORE INTO family_members "
                "(family_id, product_id, role, approval_state, provenance, confidence, created_at) "
                "VALUES (?, ?, 'primary', 'approved', 'inferred', 'high', ?)",
                (fam_id, prod_id, now),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_product(
    paths: GrossyPaths,
    product_id: str,
    name: str = "",
    blacklist_state: str = "active",
) -> None:
    """Insert a minimal product row."""
    if not name:
        name = f"Product {product_id}"
    conn = open_connection(paths.database_file)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO products "
            "(retailer, retailer_product_id, canonical_url, name, purchase_order_count, "
            "typical_quantity, blacklist_state, observation_state, created_at, updated_at) "
            "VALUES ('ab', ?, ?, ?, 1, 1, ?, 'active', ?, ?)",
            (product_id, _product_url(product_id), name, blacklist_state, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _listed_orders_6(base_id: int = 100001) -> dict[str, Any]:
    """Build a valid 6-order listed-orders proposal (numeric IDs)."""
    return {
        "schema_name": "grossy.history.listed-orders.v1",
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "orders": [
            {
                "source_order_id": str(base_id + i),
                "displayed_label": f"{(i + 1):02d}/01/2026",
                "order_date": f"2026-01-{(i + 1):02d}",
                "navigation_url": f"https://www.ab.gr/en/membership/orders/{base_id + i}",
            }
            for i in range(6)
        ],
    }


def _write_listed_orders(paths: GrossyPaths) -> Path:
    """Provide six currently listed IDs independently of the imported DB rows."""
    path = paths.config_dir / "listed-test.json"
    path.write_text(json.dumps(_listed_orders_6()), encoding="utf-8")
    path.chmod(0o600)
    return path


# ---------------------------------------------------------------------------
# plan-prepare tests
# ---------------------------------------------------------------------------


class TestPlanPrepare:
    """Tests for the weekly plan-prepare command."""

    def test_creates_run_in_planning_state(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        result = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        assert result["status"] == "success"
        assert result["lifecycle"] == "planning"
        assert result["history_sync_status"] == "success"
        assert result["run_id"].startswith("run-")

        # Verify the run is in the DB.
        conn = open_connection(paths.database_file)
        try:
            row = conn.execute(
                "SELECT lifecycle, history_sync_status FROM planner_runs WHERE run_id = ?",
                (result["run_id"],),
            ).fetchone()
            assert row is not None
            assert row[0] == "planning"
            assert row[1] == "success"
        finally:
            conn.close()

    def test_skip_history_sync(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        # No source orders seeded.

        result = run_plan_prepare(paths, skip_history_sync=True)
        assert result["status"] == "success"
        assert result["history_sync_status"] == "skipped"

    def test_history_check_fails_when_no_orders(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        # No source orders seeded.

        with pytest.raises(WeeklyPlanError, match="requires --listed-orders"):
            run_plan_prepare(paths)

    def test_second_run_rejected(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        result1 = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        assert result1["status"] == "success"

        with pytest.raises(WeeklyPlanError, match="already active"):
            run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))

    def test_run_dir_created(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        result = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_dir = Path(result["run_dir"])
        assert run_dir.is_dir()


# ---------------------------------------------------------------------------
# baseline-apply tests
# ---------------------------------------------------------------------------


class TestBaselineApply:
    """Tests for the weekly baseline-apply command."""

    def test_persists_full_cart_baseline(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["2222222", "3333333"], [2, 1])
        result = run_baseline_apply(paths, run_id, obs)
        assert result["status"] == "success"
        assert result["baseline_line_count"] == 2

        # Verify baseline in DB.
        conn = open_connection(paths.database_file)
        try:
            rows = conn.execute(
                "SELECT p.retailer_product_id, rcb.quantity FROM run_cart_baselines rcb "
                "JOIN products p ON p.id = rcb.product_id "
                "JOIN planner_runs pr ON pr.id = rcb.run_id WHERE pr.run_id = ?",
                (run_id,),
            ).fetchall()
            pids = {row[0] for row in rows}
            assert pids == {"2222222", "3333333"}
        finally:
            conn.close()

    def test_invalid_observation_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Invalid: wrong schema name.
        bad_obs = _cart_observation(["2222222"])
        bad_obs["schema_name"] = "wrong.schema"
        with pytest.raises(WeeklyPlanError, match="validation failed"):
            run_baseline_apply(paths, run_id, bad_obs)

    def test_unknown_run_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        obs = _cart_observation(["2222222"])
        with pytest.raises(WeeklyPlanError, match="unknown run"):
            run_baseline_apply(paths, "nonexistent-run", obs)

    def test_wrong_lifecycle_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Move the run to a different lifecycle.
        conn = open_connection(paths.database_file)
        try:
            conn.execute(
                "UPDATE planner_runs SET lifecycle = 'recurring-apply' WHERE run_id = ?",
                (run_id,),
            )
            conn.commit()
        finally:
            conn.close()

        obs = _cart_observation(["2222222"])
        with pytest.raises(WeeklyPlanError, match="planning"):
            run_baseline_apply(paths, run_id, obs)

    @pytest.mark.parametrize("history_status", [None, "pending", "failed"])
    def test_unverified_history_status_blocks(
        self, tmp_path: Path, history_status: str | None
    ) -> None:
        """Nullable predecessor, pending, and failed statuses cannot accept baselines."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Set a schema-valid unverified predecessor status.
        conn = open_connection(paths.database_file)
        try:
            conn.execute(
                "UPDATE planner_runs SET history_sync_status = ? WHERE run_id = ?",
                (history_status, run_id),
            )
            conn.commit()
        finally:
            conn.close()

        obs = _cart_observation(["2222222"])
        with pytest.raises(WeeklyPlanError, match="history_sync_status"):
            run_baseline_apply(paths, run_id, obs)
        conn = open_connection(paths.database_file)
        try:
            assert conn.execute("SELECT COUNT(*) FROM run_cart_baselines").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM run_recurring_items").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM run_mutation_manifests").fetchone()[0] == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Recurring item resolution tests
# ---------------------------------------------------------------------------


class TestRecurringResolution:
    """Tests for YAML/learned recurring item resolution."""

    def test_yaml_staple_resolved(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Cart has 1 unit of the staple product; intended is 2.
        obs = _cart_observation(["1111111"], [1])
        result = run_baseline_apply(paths, run_id, obs)
        assert result["status"] == "success"

        items = result["recurring_items"]
        # Find the YAML staple item.
        yaml_items = [
            i
            for i in items
            if i["source_type"] in ("yaml", "both") and i["category"] == "breakfast"
        ]
        assert len(yaml_items) == 1
        item = yaml_items[0]
        assert item["product_id"] == "1111111"
        assert item["intended_quantity"] == 2
        assert item["credited_quantity"] == 1
        assert item["missing_delta"] == 1
        assert item["status"] == "pending"

    def test_no_negative_delta(self, tmp_path: Path) -> None:
        """Preview deltas are never negative (FR-121, FR-122)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Cart has 5 units; intended is 2. Excess must be preserved.
        obs = _cart_observation(["1111111"], [5])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        yaml_items = [
            i
            for i in items
            if i["source_type"] in ("yaml", "both") and i["category"] == "breakfast"
        ]
        assert len(yaml_items) == 1
        item = yaml_items[0]
        assert item["missing_delta"] == 0  # No addition needed.
        assert item["status"] == "skipped"
        # Excess is preserved: the baseline still has 5 units. Verify via DB.
        conn = open_connection(paths.database_file)
        try:
            row = conn.execute(
                "SELECT rcb.quantity FROM run_cart_baselines rcb "
                "JOIN products p ON p.id = rcb.product_id "
                "JOIN planner_runs pr ON pr.id = rcb.run_id "
                "WHERE pr.run_id = ? AND p.retailer_product_id = ?",
                (run_id, "1111111"),
            ).fetchone()
            assert row is not None
            assert row[0] == 5  # Excess preserved.
        finally:
            conn.close()

    def test_learned_family_due(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Seed a due family (last purchase 10 weeks ago, recurrence 1 week).
        old_date = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
        _seed_family(
            paths,
            family_id="fam-due",
            product_id="4444444",
            recurrence_weeks=1,
            last_purchase_date=old_date,
        )

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["5555555"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        learned_items = [i for i in items if i["source_type"] in ("learned", "both")]
        assert len(learned_items) >= 1
        fam_item = [i for i in learned_items if i.get("family_id") == "fam-due"]
        assert len(fam_item) == 1
        assert fam_item[0]["missing_delta"] >= 0
        assert fam_item[0]["status"] in ("pending", "skipped")

    def test_not_due_family_skipped(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Seed a NOT due family (last purchase yesterday, recurrence 4 weeks).
        recent_date = (datetime.now(ATHENS_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        _seed_family(
            paths,
            family_id="fam-not-due",
            product_id="6666666",
            recurrence_weeks=4,
            last_purchase_date=recent_date,
        )

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["7777777"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        # Not-due family that is NOT YAML-covered: not in the preview.
        fam_items = [i for i in items if i.get("family_id") == "fam-not-due"]
        assert len(fam_items) == 0

    def test_low_confidence_single_occurrence_skipped(self, tmp_path: Path) -> None:
        """Medium/low single-occurrence estimates are not auto-added (FR-54)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        old_date = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
        _seed_family(
            paths,
            family_id="fam-low-conf",
            product_id="8888888",
            recurrence_weeks=1,
            last_purchase_date=old_date,
            recurrence_source="category-estimate",
            recurrence_confidence="low",
        )

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["9999999"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        fam_items = [i for i in items if i.get("family_id") == "fam-low-conf"]
        assert len(fam_items) == 1
        assert fam_items[0]["status"] == "skipped"
        assert fam_items[0]["missing_delta"] == 0
        # No fictitious product should be created (review P1).
        assert fam_items[0]["product_id"] is None or fam_items[0]["product_id"] == ""

    def test_disabled_family_suppresses_prefill(self, tmp_path: Path) -> None:
        """A disabled family suppresses history-driven prefill (FR-63)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        old_date = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
        _seed_family(
            paths,
            family_id="fam-disabled",
            product_id="1010101",
            recurrence_weeks=1,
            last_purchase_date=old_date,
        )

        # Disable the family.
        conn = open_connection(paths.database_file)
        try:
            conn.execute(
                "UPDATE product_families SET enabled = 0 WHERE family_id = ?",
                ("fam-disabled",),
            )
            conn.commit()
        finally:
            conn.close()

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["1111111"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        # Disabled family NOT covered by YAML: suppressed entirely.
        fam_items = [i for i in items if i.get("family_id") == "fam-disabled"]
        assert len(fam_items) == 0

    def test_blacklisted_product_blocked(self, tmp_path: Path) -> None:
        """A blacklisted product is blocked from YAML staples (FR-65)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Create the product and blacklist it.
        _seed_product(paths, "1111111", name="Milk", blacklist_state="blacklisted")

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["1111111"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        yaml_items = [
            i
            for i in items
            if i["source_type"] in ("yaml", "both") and i["product_id"] == "1111111"
        ]
        assert len(yaml_items) == 1
        assert yaml_items[0]["status"] == "blocked"
        assert yaml_items[0]["missing_delta"] == 0


# ---------------------------------------------------------------------------
# Budget tests
# ---------------------------------------------------------------------------


class TestBudget:
    """Tests for budget calculation (FR-171..FR-176)."""

    def test_budget_components(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths, _sample_config_text(budget=50.0))
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Cart: 2 items at 2.50 EUR each = 5.00 EUR baseline.
        obs = _cart_observation(["2222222", "3333333"], [2, 1])
        result = run_baseline_apply(paths, run_id, obs)

        budget = result["budget"]
        # Baseline: 2*2.50 + 1*2.50 = 7.50 EUR = 750 cents
        assert budget["baseline_subtotal_eur"] == "7.50"
        # Budget is 50 EUR = 5000 cents.
        assert budget["weekly_budget_eur"] == "50.00"
        # Projected total <= budget (small cart).
        var = float(budget["variance_eur"])
        assert var <= 0  # Within budget.

    def test_over_budget_variance_positive(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths, _sample_config_text(budget=5.0))
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Cart: 10 items at 2.50 EUR each = 25.00 EUR baseline > 5.00 budget.
        obs = _cart_observation(["2222222"], [10])
        result = run_baseline_apply(paths, run_id, obs)

        budget = result["budget"]
        var = float(budget["variance_eur"])
        assert var > 0  # Over budget.


# ---------------------------------------------------------------------------
# Report tests
# ---------------------------------------------------------------------------


class TestReport:
    """Tests for sanitized report generation (PRD section 15)."""

    def test_report_files_created(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["2222222"], [1])
        result = run_baseline_apply(paths, run_id, obs)
        assert result["report_failed"] is False
        assert result["report_md_path"] is not None
        assert result["report_json_path"] is not None

        md_path = Path(result["report_md_path"])
        json_path = Path(result["report_json_path"])
        assert md_path.exists()
        assert json_path.exists()

        # JSON report is valid and versioned.
        json_data = json.loads(json_path.read_text(encoding="utf-8"))
        assert json_data["schema_name"] == "grossy.weekly.report"
        assert json_data["schema_version"] == 1
        assert json_data["run_id"] == run_id

    def test_report_no_source_order_ids(self, tmp_path: Path) -> None:
        """Human reports must omit source order IDs (PRD 15, 17.14)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths, count=6)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["2222222"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        md_path = Path(result["report_md_path"])
        md_text = md_path.read_text(encoding="utf-8")
        # Source order IDs are numeric (100001, 100002, etc.)
        for i in range(6):
            assert str(100001 + i) not in md_text

    def test_report_includes_category_ordering(self, tmp_path: Path) -> None:
        """Recurring items are ordered: breakfast, lunch, alphabetic dynamic."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Seed two dynamic categories.
        old_date = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
        _seed_family(paths, "fam-z", "5050505", 1, old_date, category_name="zcategory")
        _seed_family(paths, "fam-a", "6060606", 1, old_date, category_name="acategory")

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["1111111"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        categories = [i["category"] for i in items]
        # breakfast comes first (from YAML), then alphabetic dynamic.
        if "breakfast" in categories:
            assert categories[0] == "breakfast"
        # Dynamic categories are alphabetically ordered.
        dynamic_cats = [c for c in categories if c not in ("breakfast", "lunch")]
        assert dynamic_cats == sorted(dynamic_cats)


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCliIntegration:
    """Integration tests for the CLI commands."""

    def test_plan_prepare_cli(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

        result = RUNNER.invoke(
            app, ["weekly", "plan-prepare", "--listed-orders", str(_write_listed_orders(paths))]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "success"
        assert data["lifecycle"] == "planning"

    def test_baseline_apply_cli(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

        # Create a run first.
        result1 = RUNNER.invoke(
            app, ["weekly", "plan-prepare", "--listed-orders", str(_write_listed_orders(paths))]
        )
        assert result1.exit_code == 0
        run_id = json.loads(result1.stdout)["run_id"]

        # Write the observation to a file.
        obs = _cart_observation(["2222222"], [1])
        obs_file = tmp_path / "obs.json"
        obs_file.write_text(json.dumps(obs), encoding="utf-8")

        result2 = RUNNER.invoke(app, ["weekly", "baseline-apply", run_id, str(obs_file)])
        assert result2.exit_code == 0
        data = json.loads(result2.stdout)
        assert data["status"] == "success"
        assert data["baseline_line_count"] == 1


# ---------------------------------------------------------------------------
# Correction tests: review findings
# ---------------------------------------------------------------------------


class TestDuplicateObservationIds:
    """Duplicate stable product IDs in the observation are rejected (review P1)."""

    def test_duplicate_ids_rejected(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Two lines with the same product ID.
        obs = {
            "schema_name": "grossy.cart.observation.v1",
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "lines": [
                {
                    "product_id": "2222222",
                    "canonical_url": _product_url("2222222"),
                    "quantity": 1,
                    "displayed_price_eur": "2.50",
                },
                {
                    "product_id": "2222222",
                    "canonical_url": _product_url("2222222"),
                    "quantity": 2,
                    "displayed_price_eur": "3.00",
                },
            ],
        }
        with pytest.raises(WeeklyPlanError, match="duplicate product_id"):
            run_baseline_apply(paths, run_id, obs)


class TestManualRecurrence:
    """Manually corrected recurrence is honored (review P1, FR-45)."""

    def test_manual_recurrence_eligible(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Seed a family with manual recurrence (as set by maintenance API).
        old_date = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
        _seed_family(
            paths,
            family_id="fam-manual",
            product_id="4444444",
            recurrence_weeks=1,
            last_purchase_date=old_date,
            recurrence_source="manual",
            recurrence_confidence="high",
        )

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["5555555"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        fam_items = [i for i in items if i.get("family_id") == "fam-manual"]
        assert len(fam_items) == 1
        # Manual recurrence is eligible: should be pending or skipped (not absent).
        assert fam_items[0]["status"] in ("pending", "skipped")
        assert fam_items[0]["missing_delta"] >= 0


class TestCompatibleCartCredit:
    """Compatible-family credit aggregates only approved member quantities (review P1, FR-120)."""

    def test_approved_member_credit(self, tmp_path: Path) -> None:
        """An approved family member's cart quantity is credited (FR-120)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Create a family with two members: primary (1111111) and approved alternative (1212121).
        _seed_product(paths, "1212121", name="Milk Alt")
        _seed_family(
            paths,
            family_id="fam-credit",
            product_id="1111111",
            recurrence_weeks=1,
            last_purchase_date=(datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d"),
        )

        # Link the alternative to the same family as approved.
        from grossy.history.learn import link_alternative

        conn = open_connection(paths.database_file)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            link_alternative(conn, "fam-credit", "1212121", now)
            conn.commit()
        finally:
            conn.close()

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Cart has 1 unit of the approved alternative; intended is 2 for the primary.
        obs = _cart_observation(["1212121"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        # The consolidated item (YAML + learned) should have credit from the approved family member.
        fam_items = [i for i in items if i.get("family_id") == "fam-credit"]
        assert len(fam_items) == 1
        # YAML intended=2, credited=1 (from approved alternative 1212121).
        assert fam_items[0]["credited_quantity"] == 1
        assert fam_items[0]["missing_delta"] == 1  # 2 intended - 1 credited = 1

    def test_pending_member_not_credited(self, tmp_path: Path) -> None:
        """A pending (not approved) family member's cart quantity is NOT credited."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Create a family with primary (1111111) and a pending alternative (1313131).
        _seed_product(paths, "1313131", name="Milk Pending")
        _seed_family(
            paths,
            family_id="fam-pending",
            product_id="1111111",
            recurrence_weeks=1,
            last_purchase_date=(datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d"),
        )

        # Link the alternative as PENDING (not approved).
        conn = open_connection(paths.database_file)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            # Manually insert a pending member (bypassing link_alternative which sets approved).
            alt_prod = conn.execute(
                "SELECT id FROM products WHERE retailer_product_id = '1313131'"
            ).fetchone()
            fam_row = conn.execute(
                "SELECT id FROM product_families WHERE family_id = 'fam-pending'"
            ).fetchone()
            if alt_prod and fam_row:
                # Remove from any existing family (unique index constraint).
                conn.execute("DELETE FROM family_members WHERE product_id = ?", (alt_prod[0],))
                conn.execute(
                    "INSERT INTO family_members "
                    "(family_id, product_id, role, approval_state, provenance, "
                    "confidence, created_at) "
                    "VALUES (?, ?, 'alternative', 'pending', 'linked', 'high', ?)",
                    (fam_row[0], alt_prod[0], now),
                )
                conn.commit()
        finally:
            conn.close()

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Cart has 1 unit of the pending alternative.
        obs = _cart_observation(["1313131"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        fam_items = [i for i in items if i.get("family_id") == "fam-pending"]
        assert len(fam_items) == 1
        # Pending member should NOT be credited. YAML intended=2, credited=0.
        assert fam_items[0]["credited_quantity"] == 0
        assert fam_items[0]["missing_delta"] == 2  # 2 intended - 0 credited = 2


class TestConsolidation:
    """YAML and learned consolidation preserves category refs and blocked status."""

    def test_yaml_learned_same_family_consolidates(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Seed a family that matches the YAML staple product (1111111).
        old_date = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
        _seed_family(
            paths,
            family_id="fam-consol",
            product_id="1111111",
            recurrence_weeks=1,
            last_purchase_date=old_date,
            category_name="dairy-consol",
        )

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["1111111"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        # The consolidated item should have source_type="both".
        both_items = [i for i in items if i.get("source_type") == "both"]
        assert len(both_items) == 1
        item = both_items[0]
        # Category refs should include both the YAML category and the learned category.
        refs = item["category_refs"]
        assert "breakfast" in refs
        assert "dairy-consol" in refs
        # Exact product and quantity from YAML.
        assert item["product_id"] == "1111111"
        assert item["intended_quantity"] == 2

    def test_consolidation_preserves_blocked_later_entry(self, tmp_path: Path) -> None:
        """When two YAML staples reference the same family and the later one is
        blocked, the consolidated item reflects the block (review P1)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _seed_source_orders(paths)

        # Create products.
        _seed_product(paths, "2020202", name="Cheese")
        _seed_product(paths, "2222222", name="Cheese Blocked")

        _seed_family(paths, "fam-cheese", "2020202", 1, "2026-01-01")
        from grossy.history.learn import link_alternative

        conn = open_connection(paths.database_file)
        try:
            link_alternative(conn, "fam-cheese", "2222222", datetime.now(UTC).isoformat())
            # Blacklist the later approved member, not the whole family.
            conn.execute(
                "UPDATE products SET blacklist_state = 'blacklisted' WHERE retailer_product_id = ?",
                ("2222222",),
            )
            conn.commit()
        finally:
            conn.close()

        # Two YAML staples: first is fine, second is blocked.
        config_text = """
schema_version: 1
retailer: ab
timezone: Europe/Athens
currency: EUR
household:
  adults: 2
  children: 1
  recipe_servings: 3
weekly_budget: 100.0
material_unit_price_improvement_pct: 15
disliked_ingredients: []
pantry_assumptions: []
appliances:
  thermomix:
    available: true
    model: null
  dough_kneading_machine:
    available: true
  steam_oven:
    available: true
  air_fryer:
    available: true
    capacity_litres: null
  induction_cooktop:
    available: true
  microwave:
    available: true
    wattage: null
  pressure_cooker:
    available: true
    kind: null
    capacity_litres: null
  dutch_oven:
    available: true
    capacity_litres: null
appliance_preferences:
  prefer_air_fryer_over_pan_frying: true
  prefer_thermomix_native_over_manual: true
staples:
  breakfast:
    - key: cheese1
      product_id: "2020202"
      quantity: 1
  lunch:
    - key: cheese2
      product_id: "2222222"
      quantity: 1
"""
        _write_config(paths, config_text)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["3030303"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        assert len(items) == 1
        item = items[0]
        assert item["family_id"] == "fam-cheese"
        assert item["product_id"] == "2020202"
        assert item["status"] == "blocked"
        assert {"breakfast", "lunch"} <= set(item["category_refs"])
        assert "blocked by later YAML reference in same family" in item["rationale"]


class TestIdempotentRetry:
    """Repeating baseline-apply on the same run is rejected (review P1)."""

    def test_second_apply_rejected(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["2222222"], [1])
        result1 = run_baseline_apply(paths, run_id, obs)
        assert result1["status"] == "success"

        # Second apply with the same observation should be rejected.
        obs2 = _cart_observation(["2222222"], [1])
        with pytest.raises(WeeklyPlanError, match="already has a persisted cart baseline"):
            run_baseline_apply(paths, run_id, obs2)

    def test_empty_baseline_retry_rejected(self, tmp_path: Path) -> None:
        """An empty cart baseline is still an acceptance; retry is rejected (review P1)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        # Apply an empty baseline (no lines).
        obs_empty = {
            "schema_name": "grossy.cart.observation.v1",
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "lines": [],
        }
        result1 = run_baseline_apply(paths, run_id, obs_empty)
        assert result1["status"] == "success"
        assert result1["baseline_line_count"] == 0

        # Retry with a non-empty baseline should be rejected (immutable acceptance).
        obs_nonempty = _cart_observation(["2222222"], [1])
        with pytest.raises(WeeklyPlanError, match="already has a persisted cart baseline"):
            run_baseline_apply(paths, run_id, obs_nonempty)


class TestReportSanitization:
    """Reports sanitize untrusted fields including sensitive data (review P1, PRD 15/17)."""

    def test_source_order_ids_not_in_report(self, tmp_path: Path) -> None:
        """Numeric source order IDs are redacted from reports."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        # Write a config with a staple key that embeds a known order ID.
        _write_config(
            paths,
            _sample_config_text().replace(
                "key: milk",
                "key: 100001 milk",
            ),
        )
        _seed_source_orders(paths, count=6)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["2222222"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        assert result["status"] == "success"
        assert result["report_md_path"] is not None
        md_text = Path(result["report_md_path"]).read_text(encoding="utf-8")
        # The embedded order ID should be redacted.
        assert "100001" not in md_text

    @pytest.mark.parametrize(
        "token, forbidden_suffix",
        [
            pytest.param("secret-token-abc123", "abc123", id="hyphen"),
            pytest.param("first/remaining-secret", "remaining-secret", id="slash"),
            pytest.param("first+remaining-secret", "remaining-secret", id="plus"),
            pytest.param("first==", "==", id="padding"),
            pytest.param(
                "AZaz09-._~+/remaining-secret==",
                "remaining-secret==",
                id="complete-standard-alphabet",
            ),
        ],
    )
    def test_standalone_bearer_token_redacted_in_both_reports(
        self, tmp_path: Path, token: str, forbidden_suffix: str
    ) -> None:
        """Entire standalone Bearer values are omitted from both reports (PRD 15/17)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _seed_source_orders(paths)

        # Use a staple key that embeds a bearer token.
        config_text = _sample_config_text().replace(
            "key: milk",
            f"key: Bearer {token}",
        )
        _write_config(paths, config_text)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["1111111"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        assert result["status"] == "success"
        assert result["report_md_path"] is not None
        assert result["report_json_path"] is not None
        for report_key in ("report_md_path", "report_json_path"):
            text = Path(result[report_key]).read_text(encoding="utf-8")
            assert token not in text
            assert forbidden_suffix not in text
            assert "[redacted-token]" in text

    def test_sensitive_token_redacted_in_json(self, tmp_path: Path) -> None:
        """Profile paths in untrusted fields are redacted from JSON (PRD 15/17)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _seed_source_orders(paths)

        # Use a staple key that embeds a profile path.
        config_text = _sample_config_text().replace(
            "key: milk",
            "key: /home/user/ab-profile/secret",
        )
        _write_config(paths, config_text)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["1111111"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        assert result["status"] == "success"
        assert result["report_json_path"] is not None
        json_text = Path(result["report_json_path"]).read_text(encoding="utf-8")
        assert "/home/user/ab-profile/secret" not in json_text
        assert "[redacted-path]" in json_text


class TestSubstitutionProposals:
    """Substitution proposals report approval without claiming availability (review P1)."""

    def test_substitution_no_availability_claim(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _seed_source_orders(paths)

        # Create the alternative product.
        _seed_product(paths, "1313131", name="Milk Sub")

        # Config with substitution enabled.
        config_text = _sample_config_text().replace(
            '  breakfast:\n    - key: milk\n      product_id: "1111111"\n      quantity: 2',
            '  breakfast:\n    - key: milk\n      product_id: "1111111"\n      quantity: 2\n'
            "      allow_substitution: true\n"
            "      approved_alternative_product_ids:\n"
            '        - "1313131"',
        )
        _write_config(paths, config_text)

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["2222222"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        yaml_items = [
            i
            for i in items
            if i["source_type"] in ("yaml", "both") and i["product_id"] == "1111111"
        ]
        assert len(yaml_items) == 1
        props = yaml_items[0].get("substitution_proposals", [])
        assert len(props) == 1
        # Must NOT claim availability; must state "availability not verified".
        assert "available for substitution" not in props[0]
        assert "availability not verified" in props[0]


class TestMissingDatabase:
    """Missing database fails closed before migrations (review P1, PRD 18)."""

    def test_missing_db_plan_prepare(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _write_config(paths)
        # Do NOT initialize the database.
        with pytest.raises(WeeklyPlanError, match="database not found"):
            run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))

    def test_missing_db_baseline_apply(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _write_config(paths)
        # Do NOT initialize the database.
        obs = _cart_observation(["2222222"], [1])
        with pytest.raises(WeeklyPlanError, match="database not found"):
            run_baseline_apply(paths, "any-run", obs)


class TestInvalidConfigBeforeMigrations:
    """Invalid YAML config fails before any state change (review P1)."""

    def test_invalid_config_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        # Write an invalid config (missing required fields).
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text("schema_version: 999\n", encoding="utf-8")
        _seed_source_orders(paths)

        with pytest.raises(WeeklyPlanError, match="configuration error"):
            run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))


class TestCliReportFailure:
    """CLI returns non-zero exit on report I/O failure (review P1, PRD 10.3.1)."""

    def test_baseline_apply_report_failure_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

        result1 = RUNNER.invoke(
            app, ["weekly", "plan-prepare", "--listed-orders", str(_write_listed_orders(paths))]
        )
        assert result1.exit_code == 0
        run_id = json.loads(result1.stdout)["run_id"]

        obs = _cart_observation(["2222222"], [1])
        obs_file = tmp_path / "obs.json"
        obs_file.write_text(json.dumps(obs), encoding="utf-8")

        # Patch atomic_write_text in the plan module to simulate I/O failure.
        import grossy.weekly.plan as plan_mod

        def _failing_write(path: Path, content: str) -> None:
            raise OSError("simulated disk failure")

        monkeypatch.setattr(plan_mod, "atomic_write_text", _failing_write)

        result2 = RUNNER.invoke(app, ["weekly", "baseline-apply", run_id, str(obs_file)])
        # Should exit non-zero due to report failure.
        assert result2.exit_code == 1


class TestProductOrdering:
    """Products within a category are sorted by display name (PRD 15.4, review P2)."""

    def test_alphabetic_product_name_ordering(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Seed two products in the same dynamic category with names that
        # reverse the product-ID ordering.
        old_date = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
        _seed_family(
            paths,
            "fam-zebra",
            "1000001",
            1,
            old_date,
            category_name="fruit",
        )
        # Override the name to "Zebra".
        conn = open_connection(paths.database_file)
        try:
            conn.execute("UPDATE products SET name = 'Zebra' WHERE retailer_product_id = '1000001'")
            conn.commit()
        finally:
            conn.close()

        _seed_family(
            paths,
            "fam-apple",
            "2000002",
            1,
            old_date,
            category_name="fruit",
        )
        conn = open_connection(paths.database_file)
        try:
            conn.execute("UPDATE products SET name = 'Apple' WHERE retailer_product_id = '2000002'")
            conn.commit()
        finally:
            conn.close()

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["3000003"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        # Filter to the "fruit" category items.
        fruit_items = [i for i in items if i["category"] == "fruit"]
        assert len(fruit_items) == 2
        # Apple should come before Zebra (alphabetic), despite Zebra having lower ID.
        names = [i.get("display_name") or "" for i in fruit_items]
        assert names[0] == "Apple"
        assert names[1] == "Zebra"


class TestSkippedFamilyNoFictitiousProduct:
    """Skipped families without a selected product do not create fictitious
    product rows (review P1)."""

    def test_no_placeholder_product_in_db(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        old_date = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
        _seed_family(
            paths,
            family_id="fam-skip",
            product_id="8888888",
            recurrence_weeks=1,
            last_purchase_date=old_date,
            recurrence_source="category-estimate",
            recurrence_confidence="low",
        )

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["9999999"], [1])
        run_baseline_apply(paths, run_id, obs)

        # No fictitious '_skipped_*' product should exist in the database.
        conn = open_connection(paths.database_file)
        try:
            rows = conn.execute(
                "SELECT retailer_product_id FROM products "
                "WHERE retailer_product_id LIKE '_skipped_%'"
            ).fetchall()
            assert rows == []
        finally:
            conn.close()


class TestHistorySyncWithListedOrders:
    """History sync with a listed-orders proposal (review P1, FR-9, FR-30)."""

    def test_listed_orders_all_imported_success(self, tmp_path: Path) -> None:
        """When all listed orders are imported, history sync succeeds."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        # Seed all 6 orders that match the listed proposal.
        _seed_source_orders(paths, count=6)

        listed_orders = _listed_orders_6()
        listed_path = tmp_path / "listed.json"
        listed_path.write_text(json.dumps(listed_orders), encoding="utf-8")

        result = run_plan_prepare(paths, skip_history_sync=False, listed_orders_path=listed_path)
        assert result["status"] == "success"
        assert result["history_sync_status"] == "success"

    def test_listed_orders_with_unknown_fails(self, tmp_path: Path) -> None:
        """When a listed order is not imported, history sync fails closed (FR-31)."""
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        # Only 5 of the 6 listed orders are imported.
        _seed_source_orders(paths, count=5)

        listed_orders = _listed_orders_6()
        listed_path = tmp_path / "listed.json"
        listed_path.write_text(json.dumps(listed_orders), encoding="utf-8")

        with pytest.raises(WeeklyPlanError, match="history sync incomplete"):
            run_plan_prepare(paths, skip_history_sync=False, listed_orders_path=listed_path)
        conn = open_connection(paths.database_file)
        try:
            assert conn.execute("SELECT COUNT(*) FROM planner_runs").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM run_cart_baselines").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM run_mutation_manifests").fetchone()[0] == 0
        finally:
            conn.close()
        # Simulate successful existing history phases importing the remaining ID.
        _seed_source_orders(paths, count=6)
        result = run_plan_prepare(paths, listed_orders_path=listed_path)
        assert result["history_sync_status"] == "success"


class TestCategoryRefsPreserved:
    """Learned category refs are preserved for YAML-covered families even when
    not due or disabled (review P2, PRD 15.5, 7.3.18)."""

    def test_yaml_covered_disabled_family_category_ref(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        _init_db(paths)
        _write_config(paths)
        _seed_source_orders(paths)

        # Seed a disabled family that matches the YAML staple product.
        old_date = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
        _seed_family(
            paths,
            family_id="fam-yaml-disabled",
            product_id="1111111",
            recurrence_weeks=1,
            last_purchase_date=old_date,
            category_name="dairy-learned",
        )

        # Disable the family.
        conn = open_connection(paths.database_file)
        try:
            conn.execute(
                "UPDATE product_families SET enabled = 0 WHERE family_id = ?",
                ("fam-yaml-disabled",),
            )
            conn.commit()
        finally:
            conn.close()

        plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        run_id = plan["run_id"]

        obs = _cart_observation(["1111111"], [1])
        result = run_baseline_apply(paths, run_id, obs)

        items = result["recurring_items"]
        # The YAML item should still have the learned category ref.
        yaml_items = [i for i in items if i["product_id"] == "1111111"]
        assert len(yaml_items) >= 1
        # The category refs should include the learned category.
        all_refs: set[str] = set()
        for item in yaml_items:
            all_refs.update(item.get("category_refs", []))
        assert "dairy-learned" in all_refs


@pytest.mark.parametrize("imported_count", [0, 1, 6])
def test_history_evidence_required_even_with_local_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, imported_count: int
) -> None:
    paths = _make_paths(tmp_path)
    _init_db(paths)
    _write_config(paths)
    _seed_source_orders(paths, count=imported_count)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    result = RUNNER.invoke(app, ["weekly", "plan-prepare"])
    assert result.exit_code == 1
    assert "requires --listed-orders" in json.loads(result.stdout)["detail"]
    conn = open_connection(paths.database_file)
    try:
        assert conn.execute("SELECT COUNT(*) FROM planner_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM run_cart_baselines").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM run_mutation_manifests").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "category, forbidden",
    [
        ("milk 100001", "100001"),
        ("bearer secret-token", "secret-token"),
    ],
)
def test_yaml_family_category_references_redacted(
    tmp_path: Path, category: str, forbidden: str
) -> None:
    paths = _make_paths(tmp_path)
    _init_db(paths)
    _write_config(paths)
    _seed_source_orders(paths)
    _seed_family(paths, "fam-milk", "1111111", 1, "2026-01-01", category_name=category)
    plan = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
    result = run_baseline_apply(paths, plan["run_id"], _cart_observation(["1111111"]))
    assert result["status"] == "success"
    for report_key in ("report_md_path", "report_json_path"):
        assert result[report_key] is not None
        text = Path(result[report_key]).read_text(encoding="utf-8")
        assert forbidden not in text
        assert "[redacted" in text


@pytest.mark.parametrize(
    "key, forbidden",
    [
        ("Cookie: session=first; refresh=second", "refresh=second"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        (
            'Authorization: Digest username="private-user", response="private-response"',
            "private-response",
        ),
        ("Authorization: Custom first-secret second-secret", "second-secret"),
        ("/Users/alice/Library/Application-Support/grossy/ab-profile", "alice"),
        ("/home/alice/.local/share/grossy/agent-browser/ab-profile", "alice"),
        (r"C:\Users\alice\AppData\Local\grossy\ab-profile", "alice"),
        ("~/.local/share/grossy/agent-browser/ab-profile", "ab-profile"),
    ],
)
def test_sensitive_staple_keys_redacted_in_both_reports(
    tmp_path: Path, key: str, forbidden: str
) -> None:
    paths = _make_paths(tmp_path)
    _init_db(paths)
    _write_config(paths, _sample_config_text().replace("key: milk", f"key: {json.dumps(key)}"))
    plan = run_plan_prepare(paths, skip_history_sync=True)
    result = run_baseline_apply(paths, plan["run_id"], _cart_observation(["1111111"]))
    assert result["status"] == "success"
    for report_key in ("report_md_path", "report_json_path"):
        assert result[report_key] is not None
        text = Path(result[report_key]).read_text(encoding="utf-8")
        assert forbidden not in text
        assert "[redacted" in text
        assert "session=first" not in text


def test_repeated_standalone_staple_retains_later_hard_limit(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _init_db(paths)
    text = _sample_config_text().replace(
        "  lunch: []",
        """
  lunch:
    - key: milk-limited
      product_id: "1111111"
      quantity: 2
      max_product_price_eur: 1
""",
    )
    _write_config(paths, text)
    plan = run_plan_prepare(paths, skip_history_sync=True)
    result = run_baseline_apply(paths, plan["run_id"], _cart_observation(["1111111"], [1]))
    assert result["status"] == "success"
    assert len(result["recurring_items"]) == 1
    item = result["recurring_items"][0]
    assert item["status"] == "blocked"
    assert "max_product_price_eur" in item["blocked_reason"]
    assert item["category_refs"] == ["breakfast", "lunch"]
    assert result["budget"]["projected_recurring_eur"] == "0.00"
    conn = open_connection(paths.database_file)
    try:
        assert conn.execute("SELECT status FROM run_recurring_items").fetchone()[0] == "blocked"
    finally:
        conn.close()
