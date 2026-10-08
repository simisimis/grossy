"""Agent-reviewed semantic choices, local ranking and pre-selection-only refresh."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from test_weekly_recipes import add_product, add_recipe, execute, match, prepare, proposal, stored
from test_weekly_selection import fact, observation
from test_weekly_selection import home as home
from typer.testing import CliRunner

from grossy.browser.observation import CartObservationError, validate_cart_observation
from grossy.cli import app
from grossy.paths import GrossyPaths
from grossy.recipes.proposal import proposal_checksum
from grossy.weekly.matching import run_matches_apply, run_matching_prepare
from grossy.weekly.plan import WeeklyPlanError
from grossy.weekly.recipes import run_recipes_apply
from grossy.weekly.selection import run_recipe_preflight, run_selection_apply


def semantic(revision: int, pid: str = "111", **changes: Any) -> dict[str, Any]:
    return match(
        "dinner",
        revision,
        pid,
        "unsalted butter",
        evidence="agent_semantic_review",
        compatible_product_ids=["111", "222"],
        rationale="Reviewed actual dairy butter, not nut spreads; unsalted qualifier flexible.",
        **changes,
    )


def setup(paths: GrossyPaths) -> tuple[str, int]:
    revision = add_recipe(paths, "dinner", names=["unsalted butter"])
    add_product(paths, "111", "Cow Butter Unsalted 250g")
    add_product(paths, "222", "Regular Butter 200g")
    execute(paths, "UPDATE products SET purchase_order_count=6 WHERE retailer_product_id='111'")
    run_id = prepare(paths)
    run_recipes_apply(paths, run_id, proposal(run_id))
    return run_id, revision


def test_semantic_refresh_preserves_frozen_state_and_enables_recipe_preflight(
    home: GrossyPaths,
) -> None:
    run_id, revision = setup(home)
    tables = [
        "run_recipe_plans",
        "run_recipe_slots",
        "run_recipe_seen",
        "run_recipe_selections",
        "run_cart_baselines",
        "run_stage_consent",
        "recipe_revisions",
        "family_members",
    ]
    before = {t: stored(home, f"SELECT * FROM {t}") for t in tables}
    run_sql = (
        "SELECT id,run_id,seed,lifecycle,history_sync_status,budget_eur_cents,"
        "created_at FROM planner_runs"
    )
    before_run = stored(home, run_sql)
    preparation = run_matching_prepare(home, run_id)
    assert preparation["status"] == "ready"
    assert preparation["local_products"][0]["product_id"] == "111"
    assert preparation["local_products"][0]["purchase_order_count"] == 6
    result = run_matches_apply(home, run_id, proposal(run_id, [semantic(revision)]))
    for t in tables:
        assert stored(home, f"SELECT * FROM {t}") == before[t]
    assert stored(home, run_sql) == before_run
    item = result["recipe_slots"][0]["preview"]["ingredients"][0]
    assert item["exact_match"]["product_id"] == "111"
    assert item["evidence"] == "agent_semantic_review"
    assert item["name"] == "unsalted butter"  # Original recipe fact preserved.
    assert item["semantic_compatible_product_ids"] == ["111", "222"]
    # P1: semantic match must not claim exact normalized local identity.
    assert "exact normalized local identity" not in item["reason"]
    assert "agent-reviewed semantic identity" in item["reason"]
    run_selection_apply(home, run_id, "recipe1")
    accepted = run_recipe_preflight(home, run_id, observation(facts=[fact("111")]))
    assert accepted["decisions"][0]["status"] == "passing"
    assert accepted["manifest"]["additions"][0]["product_id"] == "111"
    with pytest.raises(WeeklyPlanError, match="selected or attempted recipes"):
        run_matches_apply(home, run_id, proposal(run_id, [semantic(revision)]))


@pytest.mark.parametrize(
    "case,error",
    [
        ("less-purchased", "most frequently purchased"),
        ("unreviewed-chosen", "explicitly cover"),
        ("unreviewed-fallback", "explicitly cover"),
        ("unknown-reviewed", "unknown or inconsistent"),
        ("exact-assertion", "compatible IDs require"),
        ("wrong-revision", "frozen recipe revision"),
        ("wrong-ingredient", "frozen recipe revision"),
        ("coverage-unrelated-product", "retail unit coverage"),
        ("coverage-zero", "retail_unit_coverage"),
    ],
    ids=lambda x: x,
)
def test_mapping_rejections_leave_plan_and_catalogue_unchanged(
    home: GrossyPaths, case: str, error: str
) -> None:
    run_id, revision = setup(home)
    m = semantic(revision)
    if case == "less-purchased":
        m["product_id"] = "222"
    if case == "unreviewed-chosen":
        m["compatible_product_ids"] = ["222"]
    if case == "unreviewed-fallback":
        m["fallback_product_ids"] = ["333"]
    if case == "unknown-reviewed":
        m["compatible_product_ids"].append("333")
    if case == "exact-assertion":
        m["evidence"] = "exact_product_identity"
    if case == "wrong-revision":
        m["revision_id"] = str(revision + 1)
    if case == "wrong-ingredient":
        m["ingredient_name"] = "peanut butter"
    if case == "coverage-unrelated-product":
        m["retail_unit_coverage"] = {"222": "2"}
    if case == "coverage-zero":
        m["retail_unit_coverage"] = {"111": "0"}
    before = {
        t: stored(home, f"SELECT * FROM {t}")
        for t in ["products", "run_recipe_plans", "run_recipe_slots", "run_events"]
    }
    with pytest.raises(WeeklyPlanError, match=error):
        run_matches_apply(home, run_id, proposal(run_id, [m]))
    assert {t: stored(home, f"SELECT * FROM {t}") for t in before} == before


def test_fresh_catalogue_identity_has_no_fabricated_purchases_and_can_be_fallback(
    home: GrossyPaths,
) -> None:
    run_id, revision = setup(home)
    m = semantic(revision, fallback_product_ids=["333"])
    m["compatible_product_ids"].append("333")
    data = proposal(run_id, [m])
    data["catalogue_products"] = [
        {
            "product_id": "333",
            "canonical_url": "https://www.ab.gr/en/eshop/item/p/333",
            "title": "Fresh dairy butter 200g",
            "brand": "Test",
            "observed_at": datetime.now(UTC).isoformat(),
        }
    ]
    data["checksum"] = proposal_checksum({k: v for k, v in data.items() if k != "checksum"})
    result = run_matches_apply(home, run_id, data)
    assert stored(
        home, "SELECT purchase_order_count FROM products WHERE retailer_product_id='333'"
    ) == [(0,)]
    assert (
        result["recipe_slots"][0]["preview"]["ingredients"][0]["approved_family_fallbacks"][0][
            "product_id"
        ]
        == "333"
    )
    run_selection_apply(home, run_id, "recipe1")
    unavailable = fact(
        "111",
        availability="unavailable",
        displayed_price_eur=None,
        package_amount=None,
        package_unit=None,
        package_count=None,
        sale_kind=None,
    )
    accepted = run_recipe_preflight(home, run_id, observation(facts=[unavailable, fact("333")]))
    assert accepted["decisions"][0]["status"] == "passing"
    assert accepted["manifest"]["additions"][0]["product_id"] == "333"
    assert accepted["manifest"]["additions"][0]["price_eur"] == "2.00"


@pytest.mark.parametrize(
    "case",
    ["stale", "wrong-id", "unknown-field", "unknown-stock", "incompatible-unit"],
    ids=lambda x: x,
)
def test_catalogue_and_live_facts_fail_closed(home: GrossyPaths, case: str) -> None:
    run_id, revision = setup(home)
    data = proposal(run_id, [semantic(revision)])
    if case in ("stale", "wrong-id", "unknown-field"):
        data["matches"][0]["compatible_product_ids"].append("333")
        p = {
            "product_id": "333",
            "canonical_url": "https://www.ab.gr/en/eshop/item/p/333",
            "title": "Dairy butter",
            "observed_at": datetime.now(UTC).isoformat(),
        }
        if case == "stale":
            p["observed_at"] = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
        if case == "wrong-id":
            p["canonical_url"] = "https://www.ab.gr/en/eshop/item/p/444"
        if case == "unknown-field":
            p["unchecked"] = True
        data["catalogue_products"] = [p]
        data["checksum"] = proposal_checksum({k: v for k, v in data.items() if k != "checksum"})
        before = stored(home, "SELECT * FROM products")
        with pytest.raises(WeeklyPlanError):
            run_matches_apply(home, run_id, data)
        assert stored(home, "SELECT * FROM products") == before
    else:
        run_matches_apply(home, run_id, data)
        run_selection_apply(home, run_id, "recipe1")
        f = (
            fact("111", availability="unknown")
            if case == "unknown-stock"
            else fact("111", package_unit="ml")
        )
        result = run_recipe_preflight(home, run_id, observation(facts=[f]))
        assert result["decisions"][0]["status"] == "blocked"
        assert result["manifest"]["additions"] == []


@pytest.mark.parametrize("availability", ["available", "unavailable", "unknown"])
def test_absent_price_never_authorizes_purchase(availability: str) -> None:
    o = observation(facts=[fact(availability=availability, displayed_price_eur=None)])
    if availability == "available":
        with pytest.raises(CartObservationError, match="observed price"):
            validate_cart_observation(o)
    else:
        assert validate_cart_observation(o).recipe_product_facts[0].displayed_price_eur is None


def test_mapping_cli_uses_versioned_json_and_same_preselection_gate(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = setup(home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home.config_dir.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(home.data_dir.parent))
    monkeypatch.setenv("XDG_STATE_HOME", str(home.state_dir.parent))
    result = CliRunner().invoke(app, ["weekly", "matching-prepare", run_id])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema_name"] == "grossy.weekly.matching-prepare.v1"
    run_selection_apply(home, run_id, "recipe1")
    refused = CliRunner().invoke(app, ["weekly", "matching-prepare", run_id])
    assert refused.exit_code != 0 and "Traceback" not in refused.stdout


@pytest.mark.parametrize("excluded", ["blacklisted", "retired"])
def test_history_preference_never_overrides_excluded_products(
    home: GrossyPaths, excluded: str
) -> None:
    run_id, revision = setup(home)
    field = "blacklist_state" if excluded == "blacklisted" else "observation_state"
    execute(home, f"UPDATE products SET {field}=? WHERE retailer_product_id='111'", (excluded,))
    # Test fixture edits precede acceptance; never mutate real active family policy.
    result = run_matches_apply(home, run_id, proposal(run_id, [semantic(revision, "222")]))
    assert (
        result["recipe_slots"][0]["preview"]["ingredients"][0]["exact_match"]["product_id"] == "222"
    )


def test_matching_prepare_on_schema10_is_read_only(home: GrossyPaths) -> None:
    run_id, _ = setup(home)
    # Migration11 only adds this table/triggers; reconstruct valid prior schema.
    execute(home, "DROP TABLE run_recipe_mappings")
    execute(home, "DELETE FROM schema_migrations WHERE version=11")
    assert run_matching_prepare(home, run_id)["status"] == "ready"
    assert stored(home, "SELECT MAX(version) FROM schema_migrations") == [(10,)]
    assert stored(home, "SELECT name FROM sqlite_master WHERE name='run_recipe_mappings'") == []


def test_append_only_mapping_is_exported_and_restored_with_original_plan(
    home: GrossyPaths, tmp_path: Any
) -> None:
    from grossy.db.backup import db_backup, db_export, db_restore
    from grossy.weekly.closure import run_close

    run_id, revision = setup(home)
    original = stored(home, "SELECT * FROM run_recipe_plans")
    run_matches_apply(home, run_id, proposal(run_id, [semantic(revision)]))
    mappings = stored(home, "SELECT * FROM run_recipe_mappings")
    assert len(mappings) == 1
    import sqlite3

    for sql in [
        "UPDATE run_recipe_mappings SET preview_json='{}'",
        "DELETE FROM run_recipe_mappings",
    ]:
        with pytest.raises(sqlite3.IntegrityError, match="mappings are immutable"):
            execute(home, sql)
    export = tmp_path / "private-export" / "snapshot.json"
    db_export(home.database_file, export)
    payload = json.loads(export.read_text())
    assert len(payload["data"]["run_recipe_mappings"]) == 1
    assert payload["data"]["run_recipe_mappings"][0]["proposal_checksum"] == mappings[0][3]
    # Restore requires no active run; close only the private test fixture.
    run_close(home, run_id, abandon=True)
    backup = tmp_path / "backup" / "snapshot.sqlite3"
    db_backup(home.database_file, backup)
    db_restore(home.database_file, backup, confirm=True, state_dir=home.state_dir)
    assert stored(home, "SELECT * FROM run_recipe_plans") == original
    assert stored(home, "SELECT * FROM run_recipe_mappings") == mappings


def test_refreshed_preview_uses_refresh_timestamp_not_original(home: GrossyPaths) -> None:
    """P2: refreshed preview must carry the refresh proposal timestamp."""
    from test_weekly_recipes import NOW as ORIGINAL_TS

    run_id, revision = setup(home)
    refresh_ts = "2026-10-02T08:00:00Z"  # Distinct from original NOW timestamp.
    data = proposal(run_id, [semantic(revision)])
    data["generated_at"] = refresh_ts
    data["checksum"] = proposal_checksum({k: v for k, v in data.items() if k != "checksum"})
    result = run_matches_apply(home, run_id, data)
    preview = result["recipe_slots"][0]["preview"]
    assert preview["offline_observation_generated_at"] == refresh_ts
    # The original frozen plan must retain its original timestamp.
    plan_ts = stored(
        home,
        "SELECT r.proposal_generated_at FROM run_recipe_plans r "
        "JOIN planner_runs p ON p.id=r.run_id WHERE p.run_id=?",
        (run_id,),
    )[0][0]
    assert plan_ts == ORIGINAL_TS


def counted_onion(
    paths: GrossyPaths, coverage: dict[str, str] | None
) -> tuple[str, dict[str, Any]]:
    """Two onions per two-serving recipe (three-serving target 3), mass-sold pack."""
    revision = add_recipe(paths, "dinner", names=["onion"], unit="pc", amount=2)
    add_product(paths, "111", "Dry Onions Packaged 1kg", size=1, unit="kg")
    run_id = prepare(paths)
    run_recipes_apply(paths, run_id, proposal(run_id))
    m = match(
        "dinner",
        revision,
        "111",
        "onion",
        evidence="agent_semantic_review",
        compatible_product_ids=["111"],
        rationale="Dry onions; one packed bag holds several whole onions.",
    )
    if coverage is not None:
        m["retail_unit_coverage"] = coverage
    result = run_matches_apply(paths, run_id, proposal(run_id, [m]))
    return run_id, result["recipe_slots"][0]["preview"]["ingredients"][0]


@pytest.mark.parametrize(
    "case,coverage,facts,quantity,estimated",
    [
        (
            "estimate-for-mass-pack",
            {"111": "5"},
            {"package_unit": "kg", "package_amount": "1"},
            1,
            True,
        ),
        ("no-estimate-blocks", None, {"package_unit": "kg", "package_amount": "1"}, None, False),
        (
            "declared-estimate-wins-for-counts",
            {"111": "5"},
            {"package_unit": "pieces", "package_amount": "2"},
            1,
            True,
        ),
        (
            "native-count-without-estimate",
            None,
            {"package_unit": "pieces", "package_amount": "2"},
            2,
            False,
        ),
    ],
    ids=lambda x: x if isinstance(x, str) else None,
)
def test_counted_ingredients_use_declared_coverage_or_native_counts(
    home: GrossyPaths,
    case: str,
    coverage: dict[str, str] | None,
    facts: dict[str, str],
    quantity: int | None,
    estimated: bool,
) -> None:
    run_id, item = counted_onion(home, coverage)
    assert item["target"] == "3"
    if coverage:
        assert item["retail_unit_coverage"] == coverage
        assert item["package_quantity"] == "1"  # ceil(3 / 5) from the stated estimate.
        assert any("explicit agent estimate" in a for a in item["assumptions"])
    else:
        assert item["package_quantity"] is None  # No count-to-mass guess.
    run_selection_apply(home, run_id, "recipe1")
    result = run_recipe_preflight(home, run_id, observation(facts=[fact("111", **facts)]))
    decision = result["decisions"][0]
    if quantity is None:
        assert decision["status"] == "blocked"
        assert result["manifest"]["additions"] == []
        return
    assert decision["status"] == "passing"
    assert result["manifest"]["additions"][0]["quantity"] == quantity
    ingredient = decision["ingredients"][0]
    assert ingredient.get("estimated_coverage", False) is estimated
    assert any(
        ("explicit agent estimate" in a) is estimated and "supplies" in a
        for a in ingredient["assumptions"]
    )


def test_targets_scale_to_configured_household_servings(home: GrossyPaths) -> None:
    config = home.config_file
    text = config.read_text()
    assert "recipe_servings: 3" in text
    config.write_text(text.replace("recipe_servings: 3", "recipe_servings: 4"))
    run_id, _ = setup(home)
    preview = run_matching_prepare(home, run_id)["recipes"][0]
    item = preview["ingredients"][0]
    assert item["target"] == "200"  # 100 g for two source servings scaled to four.
    assert "linear scaling to 4 full servings" in item["assumptions"]


def test_mixed_native_primary_estimated_fallback_cart_credit_labels_estimate(
    home: GrossyPaths,
) -> None:
    """P1: estimated fallback cart credit is explicitly labelled in preview and preflight."""
    revision = add_recipe(home, "dinner", names=["onion"], unit="pc", amount=2)
    add_product(home, "111", "Onion 2-pack", size=2, unit="pieces")
    add_product(home, "222", "Onions 1kg", size=1, unit="kg")
    execute(home, "UPDATE products SET purchase_order_count=6 WHERE retailer_product_id='111'")
    execute(home, "UPDATE products SET purchase_order_count=1 WHERE retailer_product_id='222'")
    # Baseline cart has one unit of the fallback (222).
    run_id = prepare(home, ["222"])
    run_recipes_apply(home, run_id, proposal(run_id))
    m = match(
        "dinner",
        revision,
        "111",
        "onion",
        evidence="agent_semantic_review",
        compatible_product_ids=["111", "222"],
        fallback_product_ids=["222"],
        retail_unit_coverage={"222": "5"},
        rationale="Onions; one 1kg pack holds several whole onions.",
    )
    result = run_matches_apply(home, run_id, proposal(run_id, [m]))
    item = result["recipe_slots"][0]["preview"]["ingredients"][0]
    assert item["target"] == "3"
    # Primary has native 2 pc/package; fallback 222 has estimated 5 pc from coverage.
    assert item["approved_family_fallbacks"][0]["product_id"] == "222"
    # Cart baseline has one unit of 222: credit = min(1, 3/5)*5 = 3.
    assert Decimal(item["cart_credit"]) == 3
    assert Decimal(item["missing_target"]) == 0
    assert Decimal(item["package_quantity"]) == 0
    # The estimated fallback credit is explicitly labelled in preview assumptions.
    assert any(
        "explicit agent estimate for cart credit" in a
        and "one unit of 222" in a
        and "not an observed package fact" in a
        for a in item["assumptions"]
    )
    # Preflight: cart has one fallback unit, target fully met by estimated credit.
    run_selection_apply(home, run_id, "recipe1")
    obs = observation(
        cart={"222": 1},
        facts=[
            fact("111", package_amount="2", package_unit="pieces"),
            fact("222", package_amount="1", package_unit="kg"),
        ],
    )
    accepted = run_recipe_preflight(home, run_id, obs)
    decision = accepted["decisions"][0]
    assert decision["status"] == "passing"
    # Zero additions: estimated credit (5 pc) >= target (3 pc).
    assert accepted["manifest"]["additions"] == []
    ingredient = decision["ingredients"][0]
    # The selected product (111) is native, not estimated.
    assert ingredient.get("estimated_coverage", False) is False
    # The estimated fallback credit contributor is explicitly tracked and labelled.
    assert ingredient["estimated_credit_product_ids"] == ["222"]
    assert any(
        "cart credit includes explicit agent estimates" in a and "not an observed package fact" in a
        for a in ingredient["assumptions"]
    )
