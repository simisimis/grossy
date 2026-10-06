"""Task10 offline eligibility, deterministic slots/rerolls and preview boundary."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from grossy.cli import app
from grossy.db.connection import open_connection
from grossy.db.lock import LockHeldError, WriterLock
from grossy.fsutil import atomic_write_text
from grossy.init import do_init
from grossy.paths import GrossyPaths, resolve_paths
from grossy.recipes.proposal import (
    ApplianceRequirement,
    RecipeIngredient,
    RecipeVariantLink,
    proposal_checksum,
)
from grossy.recipes.repository import insert_recipe_with_revision
from grossy.weekly.mutation import run_recurring_intent, run_recurring_preflight
from grossy.weekly.plan import WeeklyPlanError, run_baseline_apply, run_plan_prepare
from grossy.weekly.proposal import validate_recipe_options
from grossy.weekly.recipes import (
    derive_seed,
    effective_weight,
    run_recipes_apply,
    run_reroll_apply,
    weighted_choice,
)

NOW = "2026-10-01T12:00:00Z"


@pytest.fixture
def home(tmp_path: Path) -> GrossyPaths:
    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    return paths


def configure(paths: GrossyPaths, **changes: Any) -> None:
    data = yaml.safe_load(paths.config_file.read_text())
    for key, value in changes.items():
        data[key] = value
    paths.config_file.write_text(yaml.safe_dump(data))


def add_recipe(
    paths: GrossyPaths,
    rid: str,
    protein: str = "meat",
    *,
    names: list[str] | None = None,
    time: int = 30,
    appliances: list[dict[str, Any]] | None = None,
    variants: list[dict[str, Any]] | None = None,
    weight: float = 1,
    tier: str = "user",
) -> int:
    conn = open_connection(paths.database_file)
    try:
        _, revision = insert_recipe_with_revision(
            conn,
            recipe_id=rid,
            title=f"Dinner {rid}",
            reference_url=None,
            source_name="Offline user",
            source_tier=tier,
            cuisine="Greek",
            protein_class=protein,
            total_time_minutes=time,
            servings=2,
            steps=["Secret preparation step never shown before selection"],
            ingredients=[RecipeIngredient(name=n, amount=100, unit="g") for n in names or ["rice"]],
            appliances=[ApplianceRequirement.model_validate(v) for v in appliances or []],
            variants=[RecipeVariantLink.model_validate(v) for v in variants or []],
            base_weight=weight,
            is_favorite=False,
            is_regular=False,
            enabled=True,
            provenance="explicitly accepted local recipe",
            now=NOW,
        )
        conn.commit()
        return revision
    finally:
        conn.close()


def proposal(run_id: str, matches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data = {
        "schema_name": "grossy.weekly.recipe-options.v1",
        "schema_version": 1,
        "generated_at": NOW,
        "run_id": run_id,
        "matches": matches or [],
    }
    return {**data, "checksum": proposal_checksum(data)}


def prepare(paths: GrossyPaths, pids: list[str] | None = None) -> str:
    run_id = run_plan_prepare(paths, skip_history_sync=True)["run_id"]
    result = run_baseline_apply(
        paths,
        run_id,
        {
            "schema_name": "grossy.cart.observation.v1",
            "schema_version": 1,
            "generated_at": NOW,
            "lines": [
                {
                    "product_id": pid,
                    "canonical_url": f"https://www.ab.gr/en/eshop/rice/p/{pid}",
                    "quantity": 1,
                    "displayed_price_eur": "2.50",
                }
                for pid in pids or []
            ],
        },
    )
    assert result["status"] == "success"
    complete_recurring(paths, str(run_id), pids)
    return str(run_id)


def complete_recurring(paths: GrossyPaths, run_id: str, pids: list[str] | None = None) -> None:
    observation = {
        "schema_name": "grossy.cart.observation.v1",
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "lines": [
            {
                "product_id": pid,
                "canonical_url": f"https://www.ab.gr/en/eshop/rice/p/{pid}",
                "quantity": 1,
                "displayed_price_eur": "2.50",
            }
            for pid in pids or []
        ],
    }
    accepted = run_recurring_preflight(paths, run_id, observation)
    assert not accepted["manifest"]["additions"]
    assert run_recurring_intent(paths, run_id, accepted["manifest"])["addition"] is None
    # Task 16: advance from basket-review barrier to permit recipe planning.
    from grossy.weekly.mutation import run_advance

    run_advance(paths, run_id, observation)


def stored(paths: GrossyPaths, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
    conn = open_connection(paths.database_file)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def execute(paths: GrossyPaths, sql: str, args: tuple[Any, ...] = ()) -> None:
    conn = open_connection(paths.database_file)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def plan_snapshot(paths: GrossyPaths) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    row = stored(paths, "SELECT candidates_json, exclusions_json FROM run_recipe_plans")[0]
    return json.loads(row[0]), json.loads(row[1])


@pytest.mark.parametrize(
    "case,expected",
    [
        ("accepted-neither-flag", True),
        ("time-60", True),
        ("time-61", False),
        ("disabled", False),
        ("no-revision", False),
        ("wrong-owner", False),
        ("unaccepted", False),
        ("ingredient-15", True),
        ("ingredient-16", False),
        ("pantry-reduces-count", True),
        ("dislike", False),
        ("family-blacklist", False),
        ("steam-oven-equivalence", True),
        ("model-missing", False),
        ("capacity-missing", False),
    ],
    ids=lambda v: str(v),
)
def test_complete_eligibility_matrix(home: GrossyPaths, case: str, expected: bool) -> None:
    names = (
        [
            f"ingredient {i}"
            for i in range(16 if case in ("ingredient-16", "pantry-reduces-count") else 15)
        ]
        if "ingredient-" in case or case == "pantry-reduces-count"
        else ["rice"]
    )
    appliances = (
        [{"type": "conventional_oven", "capability": "baking"}]
        if case == "steam-oven-equivalence"
        else [{"type": "thermomix", "capability": "mixing", "model_constraint": "TM6"}]
        if case == "model-missing"
        else [
            {
                "type": "air_fryer",
                "capability": "air_frying",
                "capacity_constraint": 4,
                "capacity_unit": "l",
            }
        ]
        if case == "capacity-missing"
        else []
    )
    revision = add_recipe(
        home,
        "test",
        names=names,
        time=60 if case == "time-60" else 61 if case == "time-61" else 30,
        appliances=appliances,
    )
    if case == "disabled":
        execute(home, "UPDATE recipes SET enabled=0")
    elif case == "no-revision":
        execute(home, "UPDATE recipes SET current_revision_id=NULL")
    elif case == "wrong-owner":
        other = add_recipe(home, "other")
        execute(home, "UPDATE recipes SET current_revision_id=? WHERE recipe_id='test'", (other,))
    elif case == "unaccepted":
        # accepted_at is required by schema: empty legacy value must fail closed.
        execute(home, "UPDATE recipe_revisions SET accepted_at='' WHERE id=?", (revision,))
    elif case == "dislike":
        configure(home, disliked_ingredients=[" RICE "])
    elif case == "pantry-reduces-count":
        configure(home, pantry_assumptions=[names[0]])
    elif case == "family-blacklist":
        execute(
            home,
            "INSERT INTO product_families "
            "(family_id,name,blacklist_state,created_at,updated_at) VALUES "
            "('rice','rice','blacklisted',?,?)",
            (NOW, NOW),
        )
    run_id = prepare(home)
    result = run_recipes_apply(home, run_id, proposal(run_id))
    assert result["status"] == "success"
    candidates, exclusions = plan_snapshot(home)
    assert ("test" in {c["recipe_id"] for c in candidates}) is expected
    if not expected:
        assert exclusions["test"]
    slots = result["recipe_slots"]
    assert [s["slot"] for s in slots] == ["recipe1", "recipe2", "recipe3", "recipe4"]
    for slot in slots:
        if not slot["preview"]:
            assert "explicit recipes discover" in slot["empty_reason"]


@pytest.mark.parametrize(
    "prefer,variant_case,chosen",
    [
        (True, "eligible", "air"),
        (False, "eligible", "base"),
        (True, "disabled", "base"),
        (True, "disliked", "base"),
        (True, "time", "base"),
        (True, "capacity", "base"),
    ],
    ids=[
        "prefer-before-weight",
        "policy-disabled",
        "disabled-no-displacement",
        "dislike-no-displacement",
        "time-no-displacement",
        "capacity-no-displacement",
    ],
)
def test_full_eligibility_before_variant_weight(
    home: GrossyPaths, prefer: bool, variant_case: str, chosen: str
) -> None:
    add_recipe(
        home,
        "air",
        names=["sesame oil"],
        time=61 if variant_case == "time" else 20,
        appliances=[
            {
                "type": "air_fryer",
                "capability": "air_frying",
                "capacity_constraint": 9,
                "capacity_unit": "l",
            }
        ]
        if variant_case == "capacity"
        else [],
        weight=0.001,
    )
    add_recipe(
        home,
        "base",
        variants=[{"linked_recipe_id": "air", "variant_type": "air_fryer"}],
        weight=999999,
    )
    configure(
        home,
        appliance_preferences={
            "prefer_air_fryer_over_pan_frying": prefer,
            "prefer_thermomix_native_over_manual": True,
        },
    )
    if variant_case == "disabled":
        execute(home, "UPDATE recipes SET enabled=0 WHERE recipe_id='air'")
    if variant_case == "disliked":
        configure(home, disliked_ingredients=["sesame oil"])
    run_id = prepare(home)
    result = run_recipes_apply(home, run_id, proposal(run_id))
    assert result["recipe_slots"][0]["preview"]["recipe_id"] == chosen


@pytest.mark.parametrize(
    "selected,nonselected,cuisine,expected",
    [
        (False, 0, None, "1"),
        (True, 3, "Greek", "0.3125"),
        (False, 1, "Greek", "1.25"),
        (False, 2, "Greek", "0.625"),
        (False, 2, "Other", "0.5"),
    ],
    ids=["base", "recent-precedence", "one-nonselection", "repeated-nonselection", "no-cuisine"],
)
def test_effective_weight_nonstacking(
    selected: bool, nonselected: int, cuisine: str | None, expected: str
) -> None:
    assert effective_weight(Decimal(1), selected, nonselected, cuisine) == Decimal(expected)


def test_four_previous_runs_count_distinct_suggestions_and_ignore_source_tier(
    home: GrossyPaths,
) -> None:
    add_recipe(home, "regular", tier="akis_petretzikis")
    add_recipe(home, "manual", tier="user")
    rid = stored(home, "SELECT id FROM recipes WHERE recipe_id='regular'")[0][0]
    for n in range(5):
        execute(
            home,
            "INSERT INTO planner_runs "
            "(run_id,seed,lifecycle,created_at,updated_at) VALUES (?,?,'completed',?,?)",
            (f"old-{n}", "old", NOW, NOW),
        )
    for event, run in [
        ("selected", "old-0"),
        ("suggested", "old-1"),
        ("suggested", "old-1"),
        ("suggested", "old-3"),
        ("selected", "old-2"),
    ]:
        execute(
            home,
            "INSERT INTO recipe_events (recipe_id,event_type,run_id,created_at) VALUES (?,?,?,?)",
            (rid, event, run, NOW),
        )
    run_id = prepare(home)
    run_recipes_apply(home, run_id, proposal(run_id))
    candidates, _ = plan_snapshot(home)
    weights = {c["recipe_id"]: c["weight"] for c in candidates}
    assert Decimal(weights["regular"]) == Decimal("0.3125")
    assert Decimal(weights["manual"]) == Decimal("1.25")
    assert (
        next(c for c in candidates if c["recipe_id"] == "regular")["history"]["nonselected_runs"]
        == 2
    )


def test_determinism_seen_exclusion_cross_class_scope_and_no_candidate(home: GrossyPaths) -> None:
    for rid, protein in [
        ("meat-a", "meat"),
        ("meat-b", "meat"),
        ("fish-a", "fish"),
        ("fish-b", "fish"),
        ("veg-a", "vegetarian"),
    ]:
        add_recipe(home, rid, protein)
    run_id = prepare(home)
    original_baseline = stored(home, "SELECT * FROM run_cart_baselines")
    original_recurring = stored(home, "SELECT * FROM run_recurring_items")
    original_manifests = stored(home, "SELECT * FROM run_mutation_manifests")
    original_observations = stored(home, "SELECT * FROM run_mutation_observations")
    first = run_recipes_apply(home, run_id, proposal(run_id))
    candidates, _ = plan_snapshot(home)
    assert weighted_choice(list(reversed(candidates)), derive_seed(run_id)) == weighted_choice(
        candidates, derive_seed(run_id)
    )
    assert (
        first["recipe_slots"] == run_recipes_apply(home, run_id, proposal(run_id))["recipe_slots"]
    )
    seen = {s["preview"]["recipe_id"] for s in first["recipe_slots"] if s["preview"]}
    second = run_reroll_apply(home, run_id, "recipe1", "fish")
    fish = second["recipe_slots"][0]["preview"]["recipe_id"]
    assert fish not in seen
    assert second["recipe_slots"][0]["original_class"] == "meat"
    assert second["recipe_slots"][0]["protein_class"] == "fish"
    assert second["recipe_slots"][0]["seed"] == derive_seed(run_id, "recipe1", 1)
    third = run_reroll_apply(home, run_id, "recipe1", "fish")
    assert third["recipe_slots"] == second["recipe_slots"]
    assert third["reroll_history"][-1]["result"] == "no_candidate"
    fourth = run_reroll_apply(home, run_id, "recipe1", "all")
    assert fourth["recipe_slots"][0]["preview"]["recipe_id"] not in seen | {fish}
    assert len(stored(home, "SELECT * FROM run_recipe_seen")) == 5
    assert stored(home, "SELECT * FROM run_cart_baselines") == original_baseline
    assert stored(home, "SELECT * FROM run_recurring_items") == original_recurring
    assert stored(home, "SELECT * FROM run_mutation_manifests") == original_manifests
    assert stored(home, "SELECT * FROM run_mutation_observations") == original_observations
    for table in (
        "run_recipe_selections",
        "run_ingredient_targets",
    ):
        assert stored(home, f"SELECT * FROM {table}") == []
    md = Path(fourth["report_md_path"]).read_text()
    report = json.loads(Path(fourth["report_json_path"]).read_text())
    assert "Secret preparation" not in md + json.dumps(report)
    assert len(report["recipe_slots"]) == 4 and len(report["reroll_history"]) == 3
    assert set(report["rough_coverage"]) == {"protein", "carbohydrate", "fibre", "vegetables"}
    assert report["budget"]["projected_recipe_eur"] is None
    assert report["budget"]["verified_recipe_eur"] == "0.00"
    for path in (Path(fourth["report_md_path"]), Path(fourth["report_json_path"])):
        assert path.stat().st_mode & 0o777 == 0o600
    assert home.run_dir(run_id).stat().st_mode & 0o777 == 0o700


def add_product(
    paths: GrossyPaths,
    pid: str,
    name: str,
    size: float = 100,
    unit: str = "g",
    approved: bool = True,
) -> None:
    execute(
        paths,
        "INSERT INTO products "
        "(retailer_product_id,canonical_url,name,package_amount,package_unit,pu"
        "rchase_order_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            pid,
            f"https://www.ab.gr/en/eshop/item/p/{pid}",
            name,
            size,
            unit,
            int(approved),
            NOW,
            NOW,
        ),
    )


def match(rid: str, revision: int, pid: str, name: str = "rice", **changes: Any) -> dict[str, Any]:
    return {
        "recipe_id": rid,
        "revision_id": str(revision),
        "ingredient_name": name,
        "product_id": pid,
        "fallback_product_ids": [],
        "availability": "available",
        "price_eur": "2.00",
        "evidence": "exact_product_identity",
        "rationale": "exact stored normalized product identity",
        **changes,
    }


def test_preview_scaling_pantry_cart_credit_packages_cost_and_fallback(home: GrossyPaths) -> None:
    revision = add_recipe(home, "test", names=["rice", "olive oil", "sesame oil"])
    execute(
        home, "UPDATE recipe_ingredients SET amount=201, indivisible=1 WHERE ingredient_name='rice'"
    )
    add_product(home, "111", "rice", size=100)
    add_product(home, "222", "rice", size=50)
    execute(
        home,
        "INSERT INTO product_families "
        "(family_id,name,enabled,created_at,updated_at) VALUES ('rice','rice',0,?,?)",
        (NOW, NOW),
    )
    family = stored(home, "SELECT id FROM product_families")[0][0]
    for pid in ("111", "222"):
        prod = stored(home, "SELECT id FROM products WHERE retailer_product_id=?", (pid,))[0][0]
        execute(
            home,
            "INSERT INTO family_members "
            "(family_id,product_id,role,approval_state,created_at) VALUES "
            "(?,?,'alternative','approved',?)",
            (family, prod, NOW),
        )
    run_id = prepare(home, ["222"])
    result = run_recipes_apply(
        home,
        run_id,
        proposal(run_id, [match("test", revision, "111", fallback_product_ids=["222"])]),
    )
    preview = result["recipe_slots"][0]["preview"]
    rice, olive, sesame = preview["ingredients"]
    assert rice["linear_target"] == "301.5" and rice["target"] == "302"
    assert Decimal(rice["cart_credit"]) == 50 and Decimal(rice["missing_target"]) == 252
    assert rice["package_quantity"] == "3" and rice["incremental_cost_eur"] == "6.00"
    assert rice["approved_family_fallbacks"][0]["product_id"] == "222"
    assert olive["pantry_covered"] and olive["missing_target"] == "0"
    assert not sesame["pantry_covered"] and sesame["status"] == "unavailable"
    assert preview["estimated_incremental_cost_eur"] is None
    assert preview["known_incremental_cost_eur"] == "6.00"
    report = Path(result["report_md_path"]).read_text()
    assert "Baseline availability and prices come from the cart observation" in report
    assert "unverified offline recipe-options observations" in report
    assert "Neither guarantees current live store availability or prices" in report
    assert "from the cart observation only" not in report


@pytest.mark.parametrize(
    "case,status",
    [
        ("ambiguous", "ambiguous"),
        ("unapproved", "ambiguous"),
        ("incompatible", "ambiguous"),
        ("unavailable", "unavailable"),
        ("blacklisted", "blacklisted"),
        ("unit-unknown", "matched"),
    ],
    ids=lambda v: str(v),
)
def test_match_safety_never_guesses(home: GrossyPaths, case: str, status: str) -> None:
    revision = add_recipe(home, "test")
    add_product(
        home,
        "111",
        "other" if case == "incompatible" else "rice",
        unit="tbsp" if case == "unit-unknown" else "g",
        approved=case != "unapproved",
    )
    if case == "ambiguous":
        add_product(home, "222", "rice")
    if case == "blacklisted":
        execute(home, "UPDATE products SET blacklist_state='blacklisted'")
    run_id = prepare(home)
    matches = (
        []
        if case == "ambiguous"
        else [
            match(
                "test",
                revision,
                "111",
                availability="unavailable" if case == "unavailable" else "available",
            )
        ]
    )
    result = run_recipes_apply(home, run_id, proposal(run_id, matches))
    ingredient = result["recipe_slots"][0]["preview"]["ingredients"][0]
    assert ingredient["status"] == status
    assert ingredient["incremental_cost_eur"] is None


@pytest.mark.parametrize(
    "case",
    [
        "unknown-field",
        "wrong-version",
        "bool-version",
        "checksum",
        "run-mismatch",
        "naive-time",
        "unknown-revision",
        "stale-revision",
        "unknown-ingredient",
        "unknown-product",
        "duplicate",
        "float-price",
        "fallback-self",
        "script",
    ],
)
def test_proposal_rejection_is_atomic(home: GrossyPaths, case: str) -> None:
    revision = add_recipe(home, "test")
    add_product(home, "111", "rice")
    run_id = prepare(home)
    data = proposal(run_id, [match("test", revision, "111")])
    if case == "unknown-field":
        data["cookies"] = "not permitted"
    elif case == "wrong-version":
        data["schema_version"] = 2
    elif case == "bool-version":
        data["schema_version"] = True
    elif case == "checksum":
        data["checksum"] = "0" * 64
    elif case == "run-mismatch":
        data["run_id"] = "other-run"
    elif case == "naive-time":
        data["generated_at"] = "2026-10-01"
    elif case == "unknown-revision":
        data["matches"][0]["revision_id"] = "999"
    elif case == "stale-revision":
        execute(home, "UPDATE recipes SET current_revision_id=NULL")
    elif case == "unknown-ingredient":
        data["matches"][0]["ingredient_name"] = "salt"
    elif case == "unknown-product":
        data["matches"][0]["product_id"] = "999"
    elif case == "duplicate":
        data["matches"].append(copy.deepcopy(data["matches"][0]))
    elif case == "float-price":
        data["matches"][0]["price_eur"] = 2.0
    elif case == "fallback-self":
        data["matches"][0]["fallback_product_ids"] = ["111"]
    elif case == "script":
        data["matches"][0]["rationale"] = "<script>bad</script>"
    if case != "checksum":
        data["checksum"] = proposal_checksum({k: v for k, v in data.items() if k != "checksum"})
    before = stored(home, "SELECT * FROM planner_runs")
    with pytest.raises(WeeklyPlanError):
        run_recipes_apply(home, run_id, data)
    assert stored(home, "SELECT * FROM planner_runs") == before
    for table in ("run_recipe_plans", "run_recipe_slots", "run_recipe_seen", "run_rerolls"):
        assert stored(home, f"SELECT * FROM {table}") == []


def test_lifecycle_lock_policy_and_immutable_snapshots(home: GrossyPaths) -> None:
    revision = add_recipe(home, "test")
    run_id = run_plan_prepare(home, skip_history_sync=True)["run_id"]
    with pytest.raises(WeeklyPlanError, match="baseline"):
        run_recipes_apply(home, run_id, proposal(run_id))
    run_baseline_apply(
        home,
        run_id,
        {
            "schema_name": "grossy.cart.observation.v1",
            "schema_version": 1,
            "generated_at": NOW,
            "lines": [],
        },
    )
    with WriterLock(home.state_dir), pytest.raises(LockHeldError):
        run_recipes_apply(home, run_id, proposal(run_id))
    complete_recurring(home, run_id)
    run_recipes_apply(home, run_id, proposal(run_id))
    before = stored(home, "SELECT candidates_json FROM run_recipe_plans")
    execute(home, "UPDATE recipes SET base_weight=900, current_revision_id=NULL")
    result = run_reroll_apply(home, run_id, "recipe1", "all")
    assert result["recipe_slots"][0]["preview"]["revision_id"] == str(revision)
    assert stored(home, "SELECT candidates_json FROM run_recipe_plans") == before
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        execute(home, "UPDATE run_recipe_plans SET candidates_json='[]'")
    configure(home, disliked_ingredients=["rice"])
    with pytest.raises(WeeklyPlanError, match="policy changed"):
        run_reroll_apply(home, run_id, "recipe1", "all")
    execute(home, "UPDATE planner_runs SET lifecycle='completed'")
    with pytest.raises(WeeklyPlanError, match="requires"):
        run_reroll_apply(home, run_id, "recipe1", "all")


def test_report_failure_recovery_exact_retry(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grossy.weekly.recipes as module

    add_recipe(home, "test")
    run_id = prepare(home)
    original = atomic_write_text

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("offline injected failure")

    monkeypatch.setattr(module, "atomic_write_text", fail)
    result = run_recipes_apply(home, run_id, proposal(run_id))
    assert result["status"] == "error"
    assert stored(home, "SELECT report_written FROM weekly_reports") == [(0,)]
    truth = stored(home, "SELECT * FROM run_recipe_slots")
    monkeypatch.setattr(module, "atomic_write_text", original)
    recovered = run_recipes_apply(home, run_id, proposal(run_id))
    assert recovered["status"] == "success" and stored(
        home, "SELECT report_written FROM weekly_reports"
    ) == [(1,)]
    assert stored(home, "SELECT * FROM run_recipe_slots") == truth
    assert len(stored(home, "SELECT * FROM recipe_events WHERE event_type='suggested'")) == 1


def test_recipe_reports_redact_untrusted_snapshot_fields(home: GrossyPaths) -> None:
    add_recipe(home, "test")
    execute(
        home, "UPDATE recipe_revisions SET title='Bearer abc/+== /home/user/ab-profile 123456' "
    )
    execute(
        home,
        "INSERT INTO source_orders "
        "(source_order_id,displayed_label,order_date,import_timestamp,created_a"
        "t) VALUES ('123456','01/01/2026','2026-01-01',?,?)",
        (NOW, NOW),
    )
    run_id = prepare(home)
    result = run_recipes_apply(home, run_id, proposal(run_id))
    for path in (result["report_md_path"], result["report_json_path"]):
        text = Path(path).read_text()
        assert "abc/+==" not in text and "/home/user" not in text and "123456" not in text
        assert "[redacted" in text


def test_cli_read_only_phase_routing(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import grossy.cli as cli

    monkeypatch.setattr(cli, "resolve_paths", lambda: home)
    add_recipe(home, "test")
    run_id = prepare(home)
    file = tmp_path / "proposal.json"
    file.write_text(json.dumps(proposal(run_id)))
    runner = CliRunner()
    applied = runner.invoke(app, ["weekly", "recipes-apply", run_id, str(file)])
    assert applied.exit_code == 0, applied.stdout
    assert json.loads(applied.stdout)["status"] == "success"
    rerolled = runner.invoke(app, ["weekly", "reroll-apply", run_id, "recipe1", "--from", "all"])
    assert rerolled.exit_code == 0
    assert json.loads(rerolled.stdout)["reroll_history"][-1]["result"] == "no_candidate"
    invalid = runner.invoke(app, ["weekly", "reroll-apply", run_id, "recipe5", "--from", "all"])
    assert invalid.exit_code == 1 and json.loads(invalid.stdout)["status"] == "error"


def test_proposal_closed_nested_fields() -> None:
    data = proposal("run-1", [match("test", 1, "111", executable="bad")])
    with pytest.raises(WeeklyPlanError):
        validate_recipe_options(data)


def test_same_run_candidates_and_reroll_sequence_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import grossy.weekly.plan as plan_module

    monkeypatch.setattr(plan_module, "_generate_run_id", lambda: "run-fixed")
    outcomes = []
    for directory in (tmp_path / "a", tmp_path / "b"):
        paths = resolve_paths(
            config_home=directory / "config",
            data_home=directory / "data",
            state_home=directory / "state",
        )
        do_init(paths)
        for index in range(5):
            add_recipe(paths, f"meat-{index}", weight=index + 1)
        run_id = prepare(paths)
        results = [run_recipes_apply(paths, run_id, proposal(run_id))]
        if directory.name == "b":
            # Later library changes must not admit new candidates/change weights.
            add_recipe(paths, "new", weight=999999)
            execute(paths, "UPDATE recipes SET base_weight=100 WHERE recipe_id='meat-0'")
        results.extend(run_reroll_apply(paths, run_id, "recipe1", "all") for _ in range(5))
        outcomes.append([(r["recipe_slots"], r["reroll_history"]) for r in results])
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][-1][1][-1]["number"] == 5
    assert outcomes[0][-1][1][-1]["result"] == "no_candidate"


@pytest.mark.parametrize("scope", ["", "meat,meat", "all,fish", "fish,unknown", "Fish", "meat,"])
def test_reroll_invalid_scope_preserves_state(home: GrossyPaths, scope: str) -> None:
    add_recipe(home, "test")
    run_id = prepare(home)
    run_recipes_apply(home, run_id, proposal(run_id))
    before = stored(home, "SELECT * FROM run_recipe_slots")
    with pytest.raises(WeeklyPlanError, match="--from"):
        run_reroll_apply(home, run_id, "recipe1", scope)
    assert stored(home, "SELECT * FROM run_recipe_slots") == before
    assert stored(home, "SELECT * FROM run_rerolls") == []


def test_linked_dishes_not_repeated_across_class_slots_or_rerolls(home: GrossyPaths) -> None:
    add_recipe(home, "fish", "fish")
    add_recipe(
        home, "meat", "meat", variants=[{"linked_recipe_id": "fish", "variant_type": "other"}]
    )
    run_id = prepare(home)
    result = run_recipes_apply(home, run_id, proposal(run_id))
    assert result["recipe_slots"][0]["preview"]["recipe_id"] == "meat"
    assert result["recipe_slots"][1]["preview"] is None
    assert "linked-dish" in result["recipe_slots"][1]["empty_reason"]
    reroll = run_reroll_apply(home, run_id, "recipe1", "fish")
    assert reroll["recipe_slots"] == result["recipe_slots"]
    assert reroll["reroll_history"][-1]["result"] == "no_candidate"


@pytest.mark.parametrize(
    "text",
    [
        "Cookie: private=secret",
        "Authorization: Basic abc==",
        "Bearer abc/+==",
        "/home/user/ab-profile",
        "%3Cscript%3Ebad%3C/script%3E",
        "address: private residence",
    ],
    ids=["cookie", "authorization", "bearer", "profile", "encoded-markup", "address"],
)
def test_recipe_options_private_content_is_rejected(text: str) -> None:
    data = proposal("run-1", [match("test", 1, "111", rationale=text)])
    with pytest.raises(WeeklyPlanError):
        validate_recipe_options(data)


def test_family_identity_proves_need_but_membership_alone_does_not(home: GrossyPaths) -> None:
    revision = add_recipe(home, "test")
    add_product(home, "111", "Brand long grain 100g")
    execute(
        home,
        "INSERT INTO product_families (family_id,name,created_at,updated_at) "
        "VALUES ('rice','rice',?,?)",
        (NOW, NOW),
    )
    execute(
        home,
        "INSERT INTO family_members (family_id,product_id,role,created_at) "
        "VALUES (1,1,'alternative',?)",
        (NOW,),
    )
    run_id = prepare(home)
    result = run_recipes_apply(
        home,
        run_id,
        proposal(run_id, [match("test", revision, "111", evidence="exact_family_identity")]),
    )
    ingredient = result["recipe_slots"][0]["preview"]["ingredients"][0]
    assert ingredient["status"] == "matched"
    assert ingredient["exact_match"]["product_id"] == "111"
    assert ingredient["evidence"] == "exact_family_identity"


def test_recipe_preview_applies_explicit_yaml_hard_limits(home: GrossyPaths) -> None:
    revision = add_recipe(home, "test")
    add_product(home, "111", "rice", size=100)
    configure(
        home,
        staples={
            "breakfast": [
                {"key": "rice", "product_id": "111", "quantity": 1, "max_line_price_eur": "3.00"}
            ],
            "lunch": [],
        },
    )
    run_id = prepare(home)
    result = run_recipes_apply(home, run_id, proposal(run_id, [match("test", revision, "111")]))
    ingredient = result["recipe_slots"][0]["preview"]["ingredients"][0]
    assert ingredient["package_quantity"] == "2"
    assert ingredient["status"] == "blocked"
    assert "hard price limit" in ingredient["reason"]
    assert ingredient["incremental_cost_eur"] is None


def test_thermomix_preference_precedes_weight_and_other_appliances_add_no_preference(
    home: GrossyPaths,
) -> None:
    add_recipe(home, "thermomix", weight=0.001)
    add_recipe(home, "oven", weight=999999)
    add_recipe(
        home,
        "manual",
        variants=[
            {"linked_recipe_id": "thermomix", "variant_type": "thermomix_native"},
            {"linked_recipe_id": "oven", "variant_type": "other"},
        ],
        weight=999999,
    )
    run_id = prepare(home)
    result = run_recipes_apply(home, run_id, proposal(run_id))
    assert result["recipe_slots"][0]["preview"]["recipe_id"] == "thermomix"
    candidates, excluded = plan_snapshot(home)
    assert [c["recipe_id"] for c in candidates] == ["thermomix"]
    assert "preferred before weighting" in excluded["oven"][0]


def test_reroll_current_blacklist_blocks_unseen_snapshot(home: GrossyPaths) -> None:
    add_recipe(home, "test", names=["rice"])
    add_recipe(home, "other", names=["beans"])
    run_id = prepare(home)
    first = run_recipes_apply(home, run_id, proposal(run_id))
    candidates, _ = plan_snapshot(home)
    shown = first["recipe_slots"][0]["preview"]["recipe_id"]
    unseen = next(c for c in candidates if c["recipe_id"] != shown)
    name = unseen["ingredients"][0]["name"]
    execute(
        home,
        "INSERT INTO product_families (family_id,name,blacklist_state,created_at,"
        "updated_at) VALUES ('blocked',?,'blacklisted',?,?)",
        (name, NOW, NOW),
    )
    result = run_reroll_apply(home, run_id, "recipe1", "all")
    assert result["recipe_slots"] == first["recipe_slots"]
    assert result["reroll_history"][-1]["result"] == "no_candidate"
    assert result["reroll_history"][-1]["seed"] == derive_seed(run_id, "recipe1", 1)
    assert (
        "seed="
        in stored(home, "SELECT detail FROM run_events WHERE event_type='recipe-reroll-preview'")[
            0
        ][0]
    )


@pytest.mark.parametrize(
    "rationale",
    [
        "Approved by alice@example.com",
        "Approved by alice%40example.com",
        "Approved by alice%2540example.com",
        "Approved by alice&#64;example.com",
    ],
    ids=["plain-email", "percent-email", "double-encoded-email", "entity-email"],
)
def test_recipe_options_account_identity_rejection_preserves_state(
    home: GrossyPaths, rationale: str
) -> None:
    revision = add_recipe(home, "test")
    add_product(home, "111", "rice")
    run_id = prepare(home)
    tables = (
        "planner_runs",
        "weekly_reports",
        "run_recipe_plans",
        "run_recipe_slots",
        "run_recipe_seen",
        "run_rerolls",
        "recipe_events",
        "run_events",
    )
    before = {table: stored(home, f"SELECT * FROM {table}") for table in tables}
    data = proposal(run_id, [match("test", revision, "111", rationale=rationale)])
    with pytest.raises(WeeklyPlanError, match="prohibited"):
        run_recipes_apply(home, run_id, data)
    assert {table: stored(home, f"SELECT * FROM {table}") for table in tables} == before


def test_recipe_plan_export_retains_snapshots_and_seen_history(home: GrossyPaths) -> None:
    from grossy.db.backup import db_export

    revision = add_recipe(home, "first")
    add_recipe(home, "second")
    add_product(home, "111", "rice")
    run_id = prepare(home)
    run_recipes_apply(home, run_id, proposal(run_id, [match("first", revision, "111")]))
    run_reroll_apply(home, run_id, "recipe1", "all")
    dest = home.run_dir(run_id) / "export.json"
    db_export(home.database_file, dest)
    data = json.loads(dest.read_text())["data"]
    conn = open_connection(home.database_file)
    try:
        for table in ("run_recipe_plans", "run_recipe_seen", "run_recipe_slots", "run_rerolls"):
            cursor = conn.execute(f"SELECT * FROM {table}")
            columns = [column[0] for column in cursor.description]
            expected = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
            assert expected and data[table] == expected
    finally:
        conn.close()
    snapshots = json.loads(data["run_recipe_plans"][0]["candidates_json"])
    assert len(snapshots) == 2
    assert len(data["run_recipe_seen"]) == 2
