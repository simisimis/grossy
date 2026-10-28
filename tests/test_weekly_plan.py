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
from grossy.history.learn import link_alternative
from grossy.init import do_init
from grossy.paths import GrossyPaths, resolve_paths
from grossy.weekly import WeeklyPlanError, run_baseline_apply, run_plan_prepare

RUNNER = CliRunner()
ATHENS_TZ = ZoneInfo("Europe/Athens")
OLD_DATE = (datetime.now(ATHENS_TZ) - timedelta(weeks=10)).strftime("%Y-%m-%d")
YAML_STAPLE = '  breakfast:\n    - key: milk\n      product_id: "1111111"\n      quantity: 2'


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
{YAML_STAPLE}
  lunch: []
"""


def _write_config(paths: GrossyPaths, text: str | None = None) -> None:
    """Write a sample config to the paths, overwriting any existing config."""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text or _sample_config_text(), encoding="utf-8")


def _initialized(tmp_path: Path, config: str | None = None, orders: int = 6) -> GrossyPaths:
    """An initialized home with the sample (or given) config and imported orders."""
    paths = _make_paths(tmp_path)
    do_init(paths)
    _write_config(paths, config)
    _seed_source_orders(paths, orders)
    return paths


def _plan(paths: GrossyPaths) -> str:
    return str(run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))["run_id"])


def _apply(
    paths: GrossyPaths, run_id: str, product_ids: list[str], quantities: list[int] | None = None
) -> dict[str, Any]:
    result = run_baseline_apply(paths, run_id, _cart_observation(product_ids, quantities))
    assert result["status"] == "success"
    return result


def _items(result: dict[str, Any], **filters: Any) -> list[dict[str, Any]]:
    """Recurring items whose listed fields equal the given values."""
    return [
        item
        for item in result["recurring_items"]
        if all(item.get(key) == value for key, value in filters.items())
    ]


def _report_texts(result: dict[str, Any]) -> list[str]:
    texts = []
    for key in ("report_md_path", "report_json_path"):
        assert result[key] is not None
        texts.append(Path(result[key]).read_text(encoding="utf-8"))
    return texts


def _query(paths: GrossyPaths, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
    conn = open_connection(paths.database_file)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _execute(paths: GrossyPaths, sql: str, args: tuple[Any, ...] = ()) -> None:
    conn = open_connection(paths.database_file)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _assert_no_run_state(paths: GrossyPaths) -> None:
    for table in ("planner_runs", "run_cart_baselines", "run_mutation_manifests"):
        assert _query(paths, f"SELECT COUNT(*) FROM {table}") == [(0,)], table


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
    recurrence_weeks: int = 1,
    last_purchase_date: str = OLD_DATE,
    recurrence_source: str = "observed",
    recurrence_confidence: str = "high",
    category_name: str = "dairy",
) -> None:
    """Seed a product family (primary member included) with recurrence for testing."""
    conn = open_connection(paths.database_file)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, display_name, created_at) VALUES (?, ?, ?)",
            (category_name, category_name.title(), now),
        )
        cat_id = conn.execute(
            "SELECT id FROM categories WHERE name = ?", (category_name,)
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO products "
            "(retailer, retailer_product_id, canonical_url, name, purchase_order_count, "
            "typical_quantity, blacklist_state, observation_state, "
            "category_id, created_at, updated_at) "
            "VALUES ('ab', ?, ?, ?, 1, 1, 'active', 'active', ?, ?, ?)",
            (product_id, _product_url(product_id), f"Product {product_id}", cat_id, now, now),
        )
        prod_id = conn.execute(
            "SELECT id FROM products WHERE retailer_product_id = ?", (product_id,)
        ).fetchone()[0]
        # Create family with NULL primary (trigger 003 requires this order).
        conn.execute(
            "INSERT INTO product_families "
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
        fam_id = conn.execute(
            "SELECT id FROM product_families WHERE family_id = ?", (family_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO family_members "
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
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _execute(
        paths,
        "INSERT OR IGNORE INTO products "
        "(retailer, retailer_product_id, canonical_url, name, purchase_order_count, "
        "typical_quantity, blacklist_state, observation_state, created_at, updated_at) "
        "VALUES ('ab', ?, ?, ?, 1, 1, ?, 'active', ?, ?)",
        (
            product_id,
            _product_url(product_id),
            name or f"Product {product_id}",
            blacklist_state,
            now,
            now,
        ),
    )


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


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GrossyPaths:
    paths = _initialized(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return paths


# ---------------------------------------------------------------------------
# plan-prepare tests
# ---------------------------------------------------------------------------


class TestPlanPrepare:
    """Tests for the weekly plan-prepare command."""

    def test_creates_run_in_planning_state_with_run_dir(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path)
        result = run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
        assert result["status"] == "success"
        assert result["lifecycle"] == "planning"
        assert result["history_sync_status"] == "success"
        assert result["run_id"].startswith("run-")
        assert Path(result["run_dir"]).is_dir()
        assert _query(
            paths,
            "SELECT lifecycle, history_sync_status FROM planner_runs WHERE run_id = ?",
            (result["run_id"],),
        ) == [("planning", "success")]

    def test_skip_history_sync(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path, orders=0)
        result = run_plan_prepare(paths, skip_history_sync=True)
        assert result["status"] == "success"
        assert result["history_sync_status"] == "skipped"

    def test_history_check_fails_when_no_orders(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path, orders=0)
        with pytest.raises(WeeklyPlanError, match="requires --listed-orders"):
            run_plan_prepare(paths)

    def test_second_run_rejected(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path)
        _plan(paths)
        with pytest.raises(WeeklyPlanError, match="already active"):
            _plan(paths)

    def test_listed_orders_with_unknown_fails_until_imported(self, tmp_path: Path) -> None:
        """A listed order that is not imported fails history sync closed (FR-31)
        without creating run state; importing it lets the next plan succeed."""
        paths = _initialized(tmp_path, orders=5)
        listed_path = _write_listed_orders(paths)
        with pytest.raises(WeeklyPlanError, match="history sync incomplete"):
            run_plan_prepare(paths, skip_history_sync=False, listed_orders_path=listed_path)
        _assert_no_run_state(paths)
        # Simulate successful existing history phases importing the remaining ID.
        _seed_source_orders(paths, count=6)
        assert run_plan_prepare(paths, listed_orders_path=listed_path)["history_sync_status"] == (
            "success"
        )

    def test_invalid_config_fails_before_any_state_change(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path, config="schema_version: 999\n")
        with pytest.raises(WeeklyPlanError, match="configuration error"):
            _plan(paths)
        _assert_no_run_state(paths)


# ---------------------------------------------------------------------------
# baseline-apply tests
# ---------------------------------------------------------------------------


class TestBaselineApply:
    """Tests for the weekly baseline-apply command."""

    def test_persists_full_cart_baseline_and_refuses_retry(self, tmp_path: Path) -> None:
        """The baseline is an immutable acceptance (review P1)."""
        paths = _initialized(tmp_path)
        run_id = _plan(paths)
        result = _apply(paths, run_id, ["2222222", "3333333"], [2, 1])
        assert result["baseline_line_count"] == 2
        assert sorted(
            _query(
                paths,
                "SELECT p.retailer_product_id, rcb.quantity FROM run_cart_baselines rcb "
                "JOIN products p ON p.id = rcb.product_id "
                "JOIN planner_runs pr ON pr.id = rcb.run_id WHERE pr.run_id = ?",
                (run_id,),
            )
        ) == [("2222222", 2), ("3333333", 1)]
        with pytest.raises(WeeklyPlanError, match="already has a persisted cart baseline"):
            run_baseline_apply(paths, run_id, _cart_observation(["2222222"]))

    def test_empty_baseline_is_an_acceptance_too(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path)
        run_id = _plan(paths)
        result = _apply(paths, run_id, [])
        assert result["baseline_line_count"] == 0
        with pytest.raises(WeeklyPlanError, match="already has a persisted cart baseline"):
            run_baseline_apply(paths, run_id, _cart_observation(["2222222"]))

    @pytest.mark.parametrize(
        "case,match",
        [
            ("wrong-schema", "validation failed"),
            ("duplicate-product-id", "duplicate product_id"),
            ("unknown-run", "unknown run"),
            ("wrong-lifecycle", "planning"),
        ],
    )
    def test_invalid_observation_or_run_fails_closed(
        self, tmp_path: Path, case: str, match: str
    ) -> None:
        paths = _initialized(tmp_path)
        run_id = _plan(paths)
        obs = _cart_observation(["2222222"])
        if case == "wrong-schema":
            obs["schema_name"] = "wrong.schema"
        elif case == "duplicate-product-id":
            obs["lines"].append({**obs["lines"][0], "quantity": 2, "displayed_price_eur": "3.00"})
        elif case == "unknown-run":
            run_id = "nonexistent-run"
        else:
            _execute(
                paths,
                "UPDATE planner_runs SET lifecycle = 'recurring-apply' WHERE run_id = ?",
                (run_id,),
            )
        with pytest.raises(WeeklyPlanError, match=match):
            run_baseline_apply(paths, run_id, obs)
        assert _query(paths, "SELECT COUNT(*) FROM run_cart_baselines") == [(0,)]

    @pytest.mark.parametrize("history_status", [None, "pending", "failed"])
    def test_unverified_history_status_blocks(
        self, tmp_path: Path, history_status: str | None
    ) -> None:
        """Nullable predecessor, pending, and failed statuses cannot accept baselines."""
        paths = _initialized(tmp_path)
        run_id = _plan(paths)
        _execute(
            paths,
            "UPDATE planner_runs SET history_sync_status = ? WHERE run_id = ?",
            (history_status, run_id),
        )
        with pytest.raises(WeeklyPlanError, match="history_sync_status"):
            run_baseline_apply(paths, run_id, _cart_observation(["2222222"]))
        for table in ("run_cart_baselines", "run_recurring_items", "run_mutation_manifests"):
            assert _query(paths, f"SELECT COUNT(*) FROM {table}") == [(0,)], table

    @pytest.mark.parametrize("phase", ["plan-prepare", "baseline-apply"])
    def test_missing_database_fails_closed_before_migrations(
        self, tmp_path: Path, phase: str
    ) -> None:
        """Missing database fails closed before migrations (review P1, PRD 18)."""
        paths = _make_paths(tmp_path)
        _write_config(paths)
        with pytest.raises(WeeklyPlanError, match="database not found"):
            if phase == "plan-prepare":
                run_plan_prepare(paths, listed_orders_path=_write_listed_orders(paths))
            else:
                run_baseline_apply(paths, "any-run", _cart_observation(["2222222"]))
        assert not paths.database_file.exists()


# ---------------------------------------------------------------------------
# Recurring item resolution tests
# ---------------------------------------------------------------------------


class TestRecurringResolution:
    """Tests for YAML/learned recurring item resolution."""

    def test_yaml_staple_positive_delta(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path)
        result = _apply(paths, _plan(paths), ["1111111"], [1])
        (item,) = _items(result, product_id="1111111", category="breakfast")
        assert item["source_type"] == "yaml"
        assert item["intended_quantity"] == 2
        assert item["credited_quantity"] == 1
        assert item["missing_delta"] == 1
        assert item["status"] == "pending"

    def test_excess_is_preserved_with_no_negative_delta(self, tmp_path: Path) -> None:
        """Preview deltas are never negative (FR-121, FR-122)."""
        paths = _initialized(tmp_path)
        run_id = _plan(paths)
        result = _apply(paths, run_id, ["1111111"], [5])
        (item,) = _items(result, product_id="1111111", category="breakfast")
        assert item["missing_delta"] == 0
        assert item["status"] == "skipped"
        assert _query(
            paths,
            "SELECT rcb.quantity FROM run_cart_baselines rcb "
            "JOIN products p ON p.id = rcb.product_id "
            "JOIN planner_runs pr ON pr.id = rcb.run_id "
            "WHERE pr.run_id = ? AND p.retailer_product_id = ?",
            (run_id, "1111111"),
        ) == [(5,)]

    @pytest.mark.parametrize("source", ["observed", "manual"])
    def test_due_family_is_eligible(self, tmp_path: Path, source: str) -> None:
        """Observed and manually corrected recurrence (FR-45) both drive prefill."""
        paths = _initialized(tmp_path)
        _seed_family(paths, "fam-due", "4444444", recurrence_source=source)
        result = _apply(paths, _plan(paths), ["5555555"], [1])
        (item,) = _items(result, family_id="fam-due")
        assert item["source_type"] == "learned"
        assert item["missing_delta"] >= 0
        assert item["status"] in ("pending", "skipped")

    @pytest.mark.parametrize("source", ["observed", "manual"])
    @pytest.mark.parametrize("day,due", [(30, False), (31, True)])
    def test_shopping_week_boundary_controls_learned_preview(
        self, tmp_path: Path, source: str, day: int, due: bool
    ) -> None:
        from grossy.weekly.plan import _resolve_learned_families

        paths = _initialized(tmp_path)
        _seed_family(
            paths,
            "fam-bananas",
            "7063312",
            recurrence_source=source,
            last_purchase_date="2026-10-28",
        )
        conn = open_connection(paths.database_file)
        try:
            items = _resolve_learned_families(
                conn, set(), {}, set(), today=datetime(2026, 10, day, tzinfo=ATHENS_TZ)
            )
        finally:
            conn.close()
        bananas = [item for item in items if item.family_id == "fam-bananas"]
        assert bool(bananas) is due
        if due:
            assert bananas[0].retailer_product_id == "7063312"
            assert bananas[0].status == "pending"
            assert bananas[0].missing_delta == 1

    @pytest.mark.parametrize("case", ["not-due", "disabled"])
    def test_not_due_or_disabled_family_is_absent(self, tmp_path: Path, case: str) -> None:
        """A not-yet-due or disabled (FR-63) family not covered by YAML is not previewed."""
        paths = _initialized(tmp_path)
        if case == "not-due":
            recent = (datetime.now(ATHENS_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
            _seed_family(paths, "fam-x", "6666666", recurrence_weeks=4, last_purchase_date=recent)
        else:
            _seed_family(paths, "fam-x", "1010101")
            _execute(paths, "UPDATE product_families SET enabled = 0 WHERE family_id = 'fam-x'")
        result = _apply(paths, _plan(paths), ["7777777"], [1])
        assert _items(result, family_id="fam-x") == []

    def test_low_confidence_single_occurrence_skipped_without_fictitious_product(
        self, tmp_path: Path
    ) -> None:
        """Medium/low single-occurrence estimates are not auto-added (FR-54) and no
        placeholder product row is created for them (review P1)."""
        paths = _initialized(tmp_path)
        _seed_family(
            paths,
            "fam-low-conf",
            "8888888",
            recurrence_source="category-estimate",
            recurrence_confidence="low",
        )
        result = _apply(paths, _plan(paths), ["9999999"], [1])
        (item,) = _items(result, family_id="fam-low-conf")
        assert item["status"] == "skipped"
        assert item["missing_delta"] == 0
        assert not item["product_id"]
        assert (
            _query(paths, "SELECT 1 FROM products WHERE retailer_product_id LIKE '_skipped_%'")
            == []
        )

    def test_blacklisted_product_blocked(self, tmp_path: Path) -> None:
        """A blacklisted product is blocked from YAML staples (FR-65)."""
        paths = _initialized(tmp_path)
        _seed_product(paths, "1111111", name="Milk", blacklist_state="blacklisted")
        result = _apply(paths, _plan(paths), ["1111111"], [1])
        (item,) = _items(result, product_id="1111111")
        assert item["source_type"] == "yaml"
        assert item["status"] == "blocked"
        assert item["missing_delta"] == 0

    @pytest.mark.parametrize("approval", ["approved", "pending"])
    def test_only_approved_family_members_are_credited(self, tmp_path: Path, approval: str) -> None:
        """Compatible-family credit aggregates only approved member quantities (FR-120)."""
        paths = _initialized(tmp_path)
        _seed_product(paths, "1212121", name="Milk Alt")
        _seed_family(paths, "fam-credit", "1111111")
        conn = open_connection(paths.database_file)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            link_alternative(conn, "fam-credit", "1212121", now)
            if approval == "pending":
                conn.execute(
                    "UPDATE family_members SET approval_state = 'pending' WHERE product_id = "
                    "(SELECT id FROM products WHERE retailer_product_id = '1212121')"
                )
            conn.commit()
        finally:
            conn.close()
        result = _apply(paths, _plan(paths), ["1212121"], [1])
        (item,) = _items(result, family_id="fam-credit")
        credited = 1 if approval == "approved" else 0
        assert item["credited_quantity"] == credited
        assert item["missing_delta"] == 2 - credited

    def test_yaml_learned_same_family_consolidates(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path)
        _seed_family(paths, "fam-consol", "1111111", category_name="dairy-consol")
        result = _apply(paths, _plan(paths), ["1111111"], [1])
        (item,) = _items(result, source_type="both")
        assert {"breakfast", "dairy-consol"} <= set(item["category_refs"])
        assert item["product_id"] == "1111111"
        assert item["intended_quantity"] == 2

    def test_consolidation_preserves_blocked_later_entry(self, tmp_path: Path) -> None:
        """When two YAML staples reference the same family and the later one is
        blocked, the consolidated item reflects the block (review P1)."""
        config = _sample_config_text().replace(
            f"{YAML_STAPLE}\n  lunch: []",
            '  breakfast:\n    - key: cheese1\n      product_id: "2020202"\n      quantity: 1\n'
            '  lunch:\n    - key: cheese2\n      product_id: "2222222"\n      quantity: 1',
        )
        paths = _initialized(tmp_path, config=config)
        _seed_product(paths, "2020202", name="Cheese")
        _seed_product(paths, "2222222", name="Cheese Blocked")
        _seed_family(paths, "fam-cheese", "2020202", last_purchase_date="2026-01-01")
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
        result = _apply(paths, _plan(paths), ["3030303"], [1])
        (item,) = result["recurring_items"]
        assert item["family_id"] == "fam-cheese"
        assert item["product_id"] == "2020202"
        assert item["status"] == "blocked"
        assert {"breakfast", "lunch"} <= set(item["category_refs"])
        assert "blocked by later YAML reference in same family" in item["rationale"]

    def test_yaml_covered_disabled_family_keeps_learned_category_ref(self, tmp_path: Path) -> None:
        """Learned category refs survive for YAML-covered families even when the
        family is disabled (review P2, PRD 15.5, 7.3.18)."""
        paths = _initialized(tmp_path)
        _seed_family(paths, "fam-yaml-disabled", "1111111", category_name="dairy-learned")
        _execute(
            paths, "UPDATE product_families SET enabled = 0 WHERE family_id = 'fam-yaml-disabled'"
        )
        result = _apply(paths, _plan(paths), ["1111111"], [1])
        items = _items(result, product_id="1111111")
        assert items
        assert "dairy-learned" in {ref for item in items for ref in item.get("category_refs", [])}

    def test_substitution_proposal_claims_no_availability(self, tmp_path: Path) -> None:
        """Substitution proposals report approval without claiming availability (review P1)."""
        config = _sample_config_text().replace(
            YAML_STAPLE,
            f"{YAML_STAPLE}\n      allow_substitution: true\n"
            '      approved_alternative_product_ids:\n        - "1313131"',
        )
        paths = _initialized(tmp_path, config=config)
        _seed_product(paths, "1313131", name="Milk Sub")
        result = _apply(paths, _plan(paths), ["2222222"], [1])
        (item,) = _items(result, product_id="1111111")
        (proposal,) = item["substitution_proposals"]
        assert "available for substitution" not in proposal
        assert "availability not verified" in proposal

    def test_repeated_standalone_staple_retains_later_hard_limit(self, tmp_path: Path) -> None:
        config = _sample_config_text().replace(
            "  lunch: []",
            '  lunch:\n    - key: milk-limited\n      product_id: "1111111"\n      quantity: 2\n'
            "      max_product_price_eur: 1",
        )
        paths = _initialized(tmp_path, config=config, orders=0)
        run_id = str(run_plan_prepare(paths, skip_history_sync=True)["run_id"])
        result = _apply(paths, run_id, ["1111111"], [1])
        (item,) = result["recurring_items"]
        assert item["status"] == "blocked"
        assert "max_product_price_eur" in item["blocked_reason"]
        assert item["category_refs"] == ["breakfast", "lunch"]
        assert result["budget"]["projected_recurring_eur"] == "0.00"
        assert _query(paths, "SELECT status FROM run_recurring_items") == [("blocked",)]


# ---------------------------------------------------------------------------
# Budget and report tests
# ---------------------------------------------------------------------------


class TestBudget:
    """Tests for budget calculation (FR-171..FR-176)."""

    def test_budget_components_within_budget(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path, config=_sample_config_text(budget=50.0))
        result = _apply(paths, _plan(paths), ["2222222", "3333333"], [2, 1])
        budget = result["budget"]
        assert budget["baseline_subtotal_eur"] == "7.50"  # 2*2.50 + 1*2.50
        assert budget["weekly_budget_eur"] == "50.00"
        assert float(budget["variance_eur"]) <= 0

    def test_over_budget_variance_positive(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path, config=_sample_config_text(budget=5.0))
        result = _apply(paths, _plan(paths), ["2222222"], [10])  # 25.00 > 5.00
        assert float(result["budget"]["variance_eur"]) > 0


class TestReport:
    """Tests for sanitized report generation (PRD section 15)."""

    def test_report_files_created_and_versioned(self, tmp_path: Path) -> None:
        paths = _initialized(tmp_path)
        run_id = _plan(paths)
        result = _apply(paths, run_id, ["2222222"], [1])
        assert result["report_failed"] is False
        _, json_text = _report_texts(result)
        json_data = json.loads(json_text)
        assert json_data["schema_name"] == "grossy.weekly.report"
        assert json_data["schema_version"] == 1
        assert json_data["run_id"] == run_id

    def test_category_and_product_name_ordering(self, tmp_path: Path) -> None:
        """Recurring items are ordered breakfast, lunch, then dynamic categories
        alphabetically, and products within a category by display name (PRD 15.4)."""
        paths = _initialized(tmp_path)
        _seed_family(paths, "fam-z", "5050505", category_name="zcategory")
        _seed_family(paths, "fam-a", "6060606", category_name="acategory")
        _seed_family(paths, "fam-zebra", "1000001", category_name="fruit")
        _seed_family(paths, "fam-apple", "2000002", category_name="fruit")
        _execute(paths, "UPDATE products SET name = 'Zebra' WHERE retailer_product_id = '1000001'")
        _execute(paths, "UPDATE products SET name = 'Apple' WHERE retailer_product_id = '2000002'")
        result = _apply(paths, _plan(paths), ["1111111"], [1])
        categories = [i["category"] for i in result["recurring_items"]]
        assert categories[0] == "breakfast"
        dynamic = [c for c in categories if c not in ("breakfast", "lunch")]
        assert dynamic == sorted(dynamic) == ["acategory", "fruit", "fruit", "zcategory"]
        assert [i["display_name"] for i in _items(result, category="fruit")] == ["Apple", "Zebra"]

    def test_source_order_ids_absent_from_report(self, tmp_path: Path) -> None:
        """Numeric source order IDs never appear in human reports, even when a
        staple key embeds one (PRD 15, 17.14)."""
        paths = _initialized(
            tmp_path, config=_sample_config_text().replace("key: milk", "key: 100001 milk")
        )
        result = _apply(paths, _plan(paths), ["2222222"], [1])
        md_text, _ = _report_texts(result)
        for i in range(6):
            assert str(100001 + i) not in md_text

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
        paths = _initialized(
            tmp_path, config=_sample_config_text().replace("key: milk", f"key: Bearer {token}")
        )
        result = _apply(paths, _plan(paths), ["1111111"], [1])
        for text in _report_texts(result):
            assert token not in text
            assert forbidden_suffix not in text
            assert "[redacted-token]" in text

    @pytest.mark.parametrize(
        "key, forbidden, marker",
        [
            ("Cookie: session=first; refresh=second", "refresh=second", "[redacted"),
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz", "[redacted"),
            (
                'Authorization: Digest username="private-user", response="private-response"',
                "private-response",
                "[redacted",
            ),
            ("Authorization: Custom first-secret second-secret", "second-secret", "[redacted"),
            ("/Users/alice/Library/Application-Support/grossy/ab-profile", "alice", "[redacted"),
            ("/home/alice/.local/share/grossy/agent-browser/ab-profile", "alice", "[redacted"),
            (r"C:\Users\alice\AppData\Local\grossy\ab-profile", "alice", "[redacted"),
            ("~/.local/share/grossy/agent-browser/ab-profile", "ab-profile", "[redacted"),
            ("/home/user/ab-profile/secret", "ab-profile/secret", "[redacted-path]"),
        ],
    )
    def test_sensitive_staple_keys_redacted_in_both_reports(
        self, tmp_path: Path, key: str, forbidden: str, marker: str
    ) -> None:
        paths = _initialized(
            tmp_path,
            config=_sample_config_text().replace("key: milk", f"key: {json.dumps(key)}"),
            orders=0,
        )
        run_id = str(run_plan_prepare(paths, skip_history_sync=True)["run_id"])
        result = _apply(paths, run_id, ["1111111"])
        for text in _report_texts(result):
            assert forbidden not in text
            assert marker in text
            assert "session=first" not in text

    @pytest.mark.parametrize(
        "category, forbidden",
        [
            ("milk 100001", "100001"),
            ("bearer secret-token", "secret-token"),
        ],
    )
    def test_yaml_family_category_references_redacted(
        self, tmp_path: Path, category: str, forbidden: str
    ) -> None:
        paths = _initialized(tmp_path)
        _seed_family(
            paths, "fam-milk", "1111111", last_purchase_date="2026-01-01", category_name=category
        )
        result = _apply(paths, _plan(paths), ["1111111"])
        for text in _report_texts(result):
            assert forbidden not in text
            assert "[redacted" in text


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCliIntegration:
    """Integration tests for the CLI commands against an isolated home."""

    def test_plan_prepare_then_baseline_apply(self, cli_env: GrossyPaths, tmp_path: Path) -> None:
        result1 = RUNNER.invoke(
            app, ["weekly", "plan-prepare", "--listed-orders", str(_write_listed_orders(cli_env))]
        )
        assert result1.exit_code == 0
        planned = json.loads(result1.stdout)
        assert planned["status"] == "success"
        assert planned["lifecycle"] == "planning"
        obs_file = tmp_path / "obs.json"
        obs_file.write_text(json.dumps(_cart_observation(["2222222"], [1])), encoding="utf-8")
        result2 = RUNNER.invoke(app, ["weekly", "baseline-apply", planned["run_id"], str(obs_file)])
        assert result2.exit_code == 0
        data = json.loads(result2.stdout)
        assert data["status"] == "success"
        assert data["baseline_line_count"] == 1

    def test_baseline_apply_report_failure_exit_code(
        self, cli_env: GrossyPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI returns non-zero exit on report I/O failure (review P1, PRD 10.3.1)."""
        import grossy.weekly.plan as plan_mod

        run_id = _plan(cli_env)
        obs_file = tmp_path / "obs.json"
        obs_file.write_text(json.dumps(_cart_observation(["2222222"], [1])), encoding="utf-8")

        def _failing_write(path: Path, content: str) -> None:
            raise OSError("simulated disk failure")

        monkeypatch.setattr(plan_mod, "atomic_write_text", _failing_write)
        result = RUNNER.invoke(app, ["weekly", "baseline-apply", run_id, str(obs_file)])
        assert result.exit_code == 1

    @pytest.mark.parametrize("imported_count", [0, 1, 6])
    def test_history_evidence_required_even_with_local_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, imported_count: int
    ) -> None:
        paths = _initialized(tmp_path, orders=imported_count)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        result = RUNNER.invoke(app, ["weekly", "plan-prepare"])
        assert result.exit_code == 1
        assert "requires --listed-orders" in json.loads(result.stdout)["detail"]
        _assert_no_run_state(paths)
