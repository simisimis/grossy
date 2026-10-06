"""Offline Stage2 selection, current-facts, passing-only deltas and recovery."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_weekly_recipes import (
    add_product,
    add_recipe,
    configure,
    execute,
    match,
    prepare,
    proposal,
    stored,
)
from typer.testing import CliRunner

from grossy.browser.mutation import AdditiveCartAdapter, apply_recipes
from grossy.browser.observation import (
    CartObservation,
    CartObservationError,
    validate_cart_observation,
)
from grossy.cli import app
from grossy.db.lock import LockHeldError, WriterLock
from grossy.init import do_init
from grossy.paths import GrossyPaths, resolve_paths
from grossy.weekly.plan import WeeklyPlanError
from grossy.weekly.recipes import run_recipes_apply
from grossy.weekly.selection import (
    run_recipe_intent,
    run_recipe_preflight,
    run_selection_apply,
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def url(pid: str) -> str:
    return f"https://www.ab.gr/en/eshop/item/p/{pid}"


def fact(pid: str = "111", **changes: Any) -> dict[str, Any]:
    return {
        "product_id": pid,
        "canonical_url": url(pid),
        "displayed_price_eur": "2.00",
        "availability": "available",
        "package_amount": "100",
        "package_unit": "g",
        "package_count": 1,
        "sale_kind": "packaged",
        **changes,
    }


def observation(
    cart: dict[str, int] | None = None, facts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "schema_name": "grossy.cart.observation.v1",
        "schema_version": 1,
        "generated_at": now(),
        "lines": [
            {
                "product_id": pid,
                "canonical_url": url(pid),
                "quantity": qty,
                "displayed_price_eur": "2.00",
            }
            for pid, qty in (cart or {}).items()
            if qty
        ],
        "recipe_product_facts": facts if facts is not None else [fact(), fact("222")],
    }


@pytest.fixture
def home(tmp_path: Path) -> GrossyPaths:
    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    return paths


def plan(
    paths: GrossyPaths, *, shared: bool = True, second: bool = True, select: str | None = "all"
) -> str:
    add_product(paths, "111", "rice")
    add_product(paths, "222", "beans")
    matches = []
    for rid, protein, ingredient, pid in [
        ("one", "meat", "rice", "111"),
        *(
            [("two", "fish", "rice" if shared else "beans", "111" if shared else "222")]
            if second
            else []
        ),
    ]:
        revision = add_recipe(paths, rid, protein, names=[ingredient])
        matches.append(match(rid, revision, pid, ingredient))
    run_id = prepare(paths)
    run_recipes_apply(paths, run_id, proposal(run_id, matches))
    if select is not None:
        run_selection_apply(paths, run_id, select)
    return run_id


def report(paths: GrossyPaths, run_id: str) -> dict[str, Any]:
    return json.loads((paths.run_dir(run_id) / "report.json").read_text())


@pytest.mark.parametrize(
    "selection,expected",
    [
        ("recipe2,recipe1", ["recipe2", "recipe1"]),
        ("all", ["recipe1", "recipe2"]),
        ("recipe2", ["recipe2"]),
        ("recipe1,recipe1", None),
        ("recipe3", None),
        ("", None),
        ("all,recipe1", None),
    ],
)
def test_argument_order_selection_matrix(
    home: GrossyPaths, selection: str, expected: list[str] | None
) -> None:
    rid = plan(home, select=None)
    if expected is None:
        with pytest.raises(WeeklyPlanError):
            run_selection_apply(home, rid, selection)
        assert stored(home, "SELECT * FROM run_recipe_selections") == []
        return
    first = run_selection_apply(home, rid, selection)
    assert first["mapping"] == [
        {"recipe": s, "dinner": f"dinner{n}"} for n, s in enumerate(expected, 1)
    ]
    events = stored(home, "SELECT * FROM recipe_events")
    assert run_selection_apply(home, rid, selection) == first
    assert stored(home, "SELECT * FROM recipe_events") == events
    md = (home.run_dir(rid) / "report.md").read_text()
    data = report(home, rid)
    assert data["recipe_selection_mappings"] == first["mapping"]
    assert len(data["selected_dinners"]) == len(expected)
    assert "Secret preparation step" in md


@pytest.mark.parametrize(
    "case,quantity,status",
    [
        ("shared", 3, "passing"),
        ("credit-once", 2, "passing"),
        ("multipack", 2, "passing"),
        ("kg-conversion", 2, "passing"),
        ("weighed", 3, "passing"),
        ("missing-facts", 0, "blocked"),
        ("unknown-stock", 0, "blocked"),
        ("incompatible-unit", 0, "blocked"),
        ("blacklisted", 0, "blocked"),
        ("family-blacklisted", 0, "blocked"),
        ("stale-name", 0, "blocked"),
        ("pantry", 0, "passing"),
        ("hard-product", 0, "blocked"),
        ("hard-shared-line", 0, "blocked"),
    ],
)
def test_current_conversion_and_policy_matrix(
    home: GrossyPaths, case: str, quantity: int, status: str
) -> None:
    rid = plan(home)
    current = observation({"111": 1} if case == "credit-once" else {})
    f = current["recipe_product_facts"][0]
    if case == "multipack":
        f["package_count"] = 2
    if case == "kg-conversion":
        f.update(package_amount="0.2", package_unit="kg")
    if case == "weighed":
        f.update(package_amount="120", sale_kind="weighed")
    if case == "missing-facts":
        current["recipe_product_facts"] = []
    if case == "unknown-stock":
        f["availability"] = "unknown"
    if case == "incompatible-unit":
        f["package_unit"] = "ml"
    if case == "blacklisted":
        execute(
            home,
            "UPDATE products SET blacklist_state='blacklisted' WHERE retailer_product_id='111'",
        )
    if case == "family-blacklisted":
        execute(
            home,
            "INSERT INTO product_families(family_id,name,blacklist_state,created_at,updated_at) "
            "VALUES ('rice','rice','blacklisted',?,?)",
            (now(), now()),
        )
    if case == "stale-name":
        execute(home, "UPDATE products SET name='sesame' WHERE retailer_product_id='111'")
    if case == "pantry":
        configure(home, pantry_assumptions=["rice"])
    if case.startswith("hard-"):
        configure(
            home,
            staples={
                "breakfast": [
                    {
                        "key": "rice",
                        "quantity": 1,
                        "product_id": "111",
                        "max_product_price_eur": "1.00" if case == "hard-product" else None,
                        "max_line_price_eur": "4.00" if case == "hard-shared-line" else None,
                    }
                ],
                "lunch": [],
            },
        )
    accepted = run_recipe_preflight(home, rid, current)
    assert [d["status"] for d in accepted["decisions"]] == [status, status]
    assert accepted["manifest"]["additions"] == (
        [
            {
                "product_id": "111",
                "canonical_url": url("111"),
                "quantity": quantity,
                "price_eur": "2.00",
            }
        ]
        if quantity
        else []
    )
    data = report(home, rid)
    assert data["recipe_preflight_decisions"] == accepted["decisions"]
    assert "Current recipe preflight" in (home.run_dir(rid) / "report.md").read_text()
    if status == "passing" and case != "pantry":
        for d in accepted["decisions"]:
            i = d["ingredients"][0]
            assert i["linear_target"] == "150" and i["target"] == "150"
            assert i["retail_quantity"] == quantity and i["assumptions"]


class FakeCart(AdditiveCartAdapter):
    def __init__(self, paths: GrossyPaths, fail: str | None = None) -> None:
        self.paths, self.fail = paths, fail
        self.cart: dict[str, int] = {}
        self.actions: list[tuple[str, int]] = []
        self.reads = 0

    def read_cart(self) -> CartObservation:
        self.reads += 1
        if self.fail == "readback" and self.actions:
            self.fail = None
            raise RuntimeError("fake readback unavailable")
        return validate_cart_observation(observation(self.cart))

    def add_positive(self, product_id: str, canonical_url: str, quantity: int) -> None:
        assert quantity > 0 and canonical_url == url(product_id)
        assert stored(self.paths, "SELECT lifecycle FROM planner_runs")[0][0] == "recipe-apply"
        assert (
            stored(self.paths, "SELECT stage FROM run_mutation_manifests ORDER BY id DESC LIMIT 1")[
                0
            ][0]
            == "recipe"
        )
        assert (
            stored(
                self.paths,
                "SELECT failure_class FROM run_mutation_observations ORDER BY id DESC LIMIT 1",
            )[0][0]
            == "total"
        )
        self.actions.append((product_id, quantity))
        self.cart[product_id] = self.cart.get(product_id, 0) + quantity
        if self.fail == "write":
            self.fail = None
            raise RuntimeError("fake failure after addition")


@pytest.mark.parametrize("failure", [None, "write", "readback", "interrupt"])
def test_intent_readback_repeat_and_resume(home: GrossyPaths, failure: str | None) -> None:
    rid = plan(home, shared=False)
    execute(
        home,
        "INSERT INTO product_families(family_id,name,last_purchase_date,created_at,updated_at) "
        "VALUES ('historical','historical','2026-09-01',?,?)",
        (now(), now()),
    )
    execute(
        home, "UPDATE products SET first_observed_date='2026-08-01',last_observed_date='2026-09-01'"
    )
    history = stored(home, "SELECT last_purchase_date FROM product_families")
    product_history = stored(
        home, "SELECT first_observed_date,last_observed_date,purchase_order_count FROM products"
    )
    adapter = FakeCart(home, failure if failure != "interrupt" else None)
    if failure == "interrupt":
        prepared = run_recipe_preflight(home, rid, observation())
        run_recipe_intent(home, rid, prepared["manifest"])
        adapter.cart = {"111": 1}
        with pytest.raises(WeeklyPlanError):
            run_recipe_preflight(home, rid, observation())
        result = apply_recipes(home, rid, adapter, lambda _: True, resume=True)
        assert adapter.actions == [("111", 1), ("222", 2)]
    else:
        result = apply_recipes(home, rid, adapter, lambda _: True)
        if failure:
            assert result["status"] == "error" and len(adapter.actions) == 1
            assert stored(home, "SELECT lifecycle FROM planner_runs")[0][0] == "partial-failure"
            result = apply_recipes(home, rid, adapter, lambda _: True, resume=True)
    assert result["status"] == "success"
    assert adapter.cart == {"111": 2, "222": 2}
    assert adapter.reads >= 6
    assert stored(home, "SELECT lifecycle FROM planner_runs")[0][0] == "awaiting-user-verification"
    assert stored(home, "SELECT last_purchase_date FROM product_families") == history
    assert (
        stored(
            home, "SELECT first_observed_date,last_observed_date,purchase_order_count FROM products"
        )
        == product_history
    )
    actions = adapter.actions[:]
    assert run_selection_apply(home, rid, "all")["status"] == "persisted"
    assert apply_recipes(home, rid, adapter, lambda _: True)["status"] == "success"
    assert adapter.actions == actions
    data = report(home, rid)
    assert data["budget"]["verified_recipe_eur"] == "8.00"
    assert data["recipe_mutation_evidence"] and data["recurring_mutation_evidence"]
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in home.run_dir(rid).glob("report.*"))


def test_autonomous_multi_recipe_prefill_credits_edited_cart_and_repeats(
    home: GrossyPaths,
) -> None:
    """No callback: selection consent alone drives every recipe action; repeat is idempotent."""
    from grossy.weekly.mutation import (
        run_advance,
        run_recurring_intent,
        run_recurring_preflight,
    )
    from grossy.weekly.plan import run_baseline_apply, run_plan_prepare

    add_product(home, "111", "rice")
    add_product(home, "222", "beans")
    one = add_recipe(home, "one", "meat", names=["rice"])
    two = add_recipe(home, "two", "fish", names=["beans"])
    rid = str(run_plan_prepare(home, skip_history_sync=True)["run_id"])
    run_baseline_apply(home, rid, observation({"999": 1}, []))
    accepted = run_recurring_preflight(home, rid, observation({"999": 1}, []))
    assert run_recurring_intent(home, rid, accepted["manifest"])["addition"] is None
    # Review edit: the user removed 999 and added one rice pack before advancing.
    edited = {"111": 1, "333": 1}
    run_advance(home, rid, observation(edited, []))
    run_recipes_apply(
        home, rid, proposal(rid, [match("one", one, "111"), match("two", two, "222", "beans")])
    )
    run_selection_apply(home, rid, "all")
    adapter = FakeCart(home)
    adapter.cart = dict(edited)
    assert apply_recipes(home, rid, adapter)["status"] == "success"
    assert sorted(adapter.actions) == [("111", 1), ("222", 2)]
    assert adapter.cart == {"111": 2, "222": 2, "333": 1}
    assert stored(home, "SELECT lifecycle FROM planner_runs")[0][0] == "awaiting-user-verification"
    checks = [
        json.loads(r[0])
        for r in stored(
            home, "SELECT detail FROM run_events WHERE event_type='action-consent-checked'"
        )
    ]
    consent = stored(home, "SELECT id FROM run_stage_consent WHERE stage='recipe'")[0][0]
    recipe_manifests = stored(
        home,
        "SELECT checksum FROM run_mutation_manifests WHERE stage='recipe' "
        "AND json_array_length(manifest_json,'$.additions')=1 ORDER BY id",
    )
    assert [(c["stage"], c["consent_row_id"]) for c in checks] == [("recipe", consent)] * 2
    assert [c["manifest_checksum"] for c in checks] == [r[0] for r in recipe_manifests]
    assert apply_recipes(home, rid, adapter)["status"] == "success"
    assert sorted(adapter.actions) == [("111", 1), ("222", 2)]
    assert "999" not in adapter.cart


def test_recipe_dispatch_revocation_after_intent_blocks_action(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grossy.weekly.selection as selection
    from grossy.weekly.mutation import run_consent_revoke

    rid = plan(home, shared=False)
    real_intent = selection.run_recipe_intent

    def intent_then_revoke(paths: GrossyPaths, run_id: str, data: dict[str, Any]) -> Any:
        result = real_intent(paths, run_id, data)
        run_consent_revoke(paths, run_id, "recipe")
        return result

    monkeypatch.setattr(selection, "run_recipe_intent", intent_then_revoke)
    adapter = FakeCart(home)
    with pytest.raises(WeeklyPlanError, match="revoked"):
        apply_recipes(home, rid, adapter)
    assert adapter.actions == []
    assert stored(home, "SELECT lifecycle FROM planner_runs")[0][0] == "recipe-apply"
    assert stored(home, "SELECT 1 FROM run_events WHERE event_type='action-consent-checked'") == []
    pending = stored(home, "SELECT checksum FROM run_mutation_manifests WHERE stage='recipe'")
    readback = observation()
    readback["mutation_result"] = {"manifest_checksum": pending[-1][0], "outcome": "readback"}
    assert selection.run_recipe_observe(home, rid, readback)["status"] == "error"


def test_passing_only_consolidation_and_removed_recurring_never_restored(home: GrossyPaths) -> None:
    add_product(home, "111", "rice")
    first = add_recipe(home, "one", "meat", names=["rice"])
    second = add_recipe(home, "two", "fish", names=["rice", "unresolved"])
    # Baseline contains an unrelated recurring/cart line which the owner later removes.
    rid = prepare(home, ["999"])
    run_recipes_apply(
        home, rid, proposal(rid, [match("one", first, "111"), match("two", second, "111")])
    )
    run_selection_apply(home, rid, "all")
    accepted = run_recipe_preflight(home, rid, observation())
    assert [d["status"] for d in accepted["decisions"]] == ["passing", "blocked"]
    assert [(a["product_id"], a["quantity"]) for a in accepted["manifest"]["additions"]] == [
        ("111", 2)
    ]
    adapter = FakeCart(home)
    assert apply_recipes(home, rid, adapter, lambda _: True)["status"] == "success"
    assert adapter.actions == [("111", 2)] and "999" not in adapter.cart


@pytest.mark.parametrize(
    "case", ["checksum", "policy", "identity", "stale", "selection-change", "lock"]
)
def test_authorization_fail_closed_matrix(home: GrossyPaths, case: str) -> None:
    rid = plan(home)
    obs = observation()
    prepared = run_recipe_preflight(home, rid, obs)
    manifest = copy.deepcopy(prepared["manifest"])
    if case == "checksum":
        manifest["additions"][0]["quantity"] += 1
    if case == "policy":
        configure(home, pantry_assumptions=["rice"])
    if case == "identity":
        execute(home, "UPDATE products SET name='different' WHERE retailer_product_id='111'")
    if case == "stale":
        execute(
            home,
            "UPDATE run_events SET detail=json_set(detail,'$.cart.generated_at',?) "
            "WHERE event_type='recipe-preflight'",
            ((datetime.now(UTC) - timedelta(minutes=6)).isoformat(),),
        )
    manifests = stored(home, "SELECT * FROM run_mutation_manifests")
    if case == "lock":
        with WriterLock(home.state_dir), pytest.raises(LockHeldError):
            run_recipe_intent(home, rid, manifest)
    elif case == "selection-change":
        with pytest.raises(WeeklyPlanError):
            run_selection_apply(home, rid, "recipe1")
    else:
        with pytest.raises(WeeklyPlanError):
            run_recipe_intent(home, rid, manifest)
    assert stored(home, "SELECT * FROM run_mutation_manifests") == manifests


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "zero-amount",
        "float-amount",
        "count-bool",
        "weighed-count",
        "price-conflict",
        "identity-conflict",
        "duplicate",
        "unit",
    ],
)
def test_closed_recipe_facts_matrix(case: str) -> None:
    obs = observation({"111": 1})
    f = obs["recipe_product_facts"][0]
    if case == "unknown":
        f["raw_dom"] = "unsafe"
    if case == "zero-amount":
        f["package_amount"] = "0"
    if case == "float-amount":
        f["package_amount"] = 1.5
    if case == "count-bool":
        f["package_count"] = True
    if case == "weighed-count":
        f.update(sale_kind="weighed", package_count=2)
    if case == "price-conflict":
        f["displayed_price_eur"] = "3.00"
    if case == "identity-conflict":
        f["canonical_url"] = "https://www.ab.gr/en/eshop/other/p/111"
    if case == "duplicate":
        obs["recipe_product_facts"].append(f)
    if case == "unit":
        f["package_unit"] = "cups"
    with pytest.raises(CartObservationError):
        validate_cart_observation(obs)


def test_selection_cli_uses_only_isolated_state(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = plan(home, select=None)
    monkeypatch.setattr("grossy.cli.resolve_paths", lambda: home)
    runner = CliRunner()
    selected = runner.invoke(app, ["weekly", "selection-apply", rid, "recipe2,recipe1"])
    assert (
        selected.exit_code == 0 and json.loads(selected.stdout)["mapping"][0]["recipe"] == "recipe2"
    )
    path = home.state_dir / "observation.json"
    path.write_text(json.dumps(observation()))
    preflight = runner.invoke(app, ["weekly", "recipe-preflight", rid, str(path)])
    assert preflight.exit_code == 0
    manifest = json.loads(preflight.stdout)["manifest"]
    path.write_text(json.dumps(manifest))
    intent = runner.invoke(app, ["weekly", "recipe-intent", rid, str(path)])
    assert intent.exit_code == 0 and json.loads(intent.stdout)["status"] == "persisted"
    observed = observation({"111": 3})
    observed["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    path.write_text(json.dumps(observed))
    result = runner.invoke(app, ["weekly", "recipe-observe", rid, str(path)])
    assert (
        result.exit_code == 0
        and json.loads(result.stdout)["lifecycle"] == "awaiting-user-verification"
    )


@pytest.mark.parametrize(
    "case,status,pid",
    [
        ("available-primary", "passing", "111"),
        ("unavailable-primary", "passing", "222"),
        ("blacklisted-primary", "passing", "222"),
        ("unknown-primary", "blocked", None),
        ("unapproved-fallback", "blocked", None),
        ("wrong-family", "blocked", None),
        ("yaml-disallows", "blocked", None),
        ("disabled-family", "passing", "222"),
        ("compatible-cart-credit", "passing", None),
    ],
)
def test_approved_same_family_substitution_matrix(
    home: GrossyPaths, case: str, status: str, pid: str | None
) -> None:
    add_product(home, "111", "rice")
    add_product(home, "222", "rice", size=200)
    execute(
        home,
        "INSERT INTO product_families(family_id,name,created_at,updated_at) "
        "VALUES ('rice','rice',?,?)",
        (now(), now()),
    )
    execute(
        home,
        "INSERT INTO family_members(family_id,product_id,role,approval_state,created_at) "
        "SELECT f.id,p.id,'alternative','approved',? "
        "FROM product_families f CROSS JOIN products p",
        (now(),),
    )
    revision = add_recipe(home, "one")
    rid = prepare(home)
    run_recipes_apply(
        home, rid, proposal(rid, [match("one", revision, "111", fallback_product_ids=["222"])])
    )
    run_selection_apply(home, rid, "all")
    if case == "disabled-family":
        execute(home, "UPDATE product_families SET enabled=0")
    if case == "blacklisted-primary":
        execute(
            home,
            "UPDATE products SET blacklist_state='blacklisted' WHERE retailer_product_id='111'",
        )
    if case == "unapproved-fallback":
        execute(home, "UPDATE products SET purchase_order_count=0 WHERE retailer_product_id='222'")
        execute(
            home,
            "UPDATE family_members SET approval_state='pending' WHERE product_id="
            "(SELECT id FROM products WHERE retailer_product_id='222')",
        )
    if case == "wrong-family":
        execute(
            home,
            "DELETE FROM family_members WHERE product_id="
            "(SELECT id FROM products WHERE retailer_product_id='222')",
        )
    if case == "yaml-disallows":
        configure(
            home,
            staples={
                "breakfast": [
                    {"key": "rice", "product_id": "111", "quantity": 1, "allow_substitution": False}
                ],
                "lunch": [],
            },
        )
    obs = observation(
        facts=[
            fact(
                availability="available"
                if case in ("available-primary", "compatible-cart-credit")
                else "unknown"
                if case == "unknown-primary"
                else "unavailable"
            ),
            fact("222", package_amount="200"),
        ]
    )
    if case == "compatible-cart-credit":
        obs["lines"] = observation({"222": 1})["lines"]
    accepted = run_recipe_preflight(home, rid, obs)
    assert accepted["decisions"][0]["status"] == status
    assert [a["product_id"] for a in accepted["manifest"]["additions"]] == ([pid] if pid else [])
    if pid == "222":
        assert accepted["manifest"]["additions"][0]["quantity"] == 1
        assert accepted["decisions"][0]["ingredients"][0]["status"] == "substituted"


@pytest.mark.parametrize("drift", ["price", "package", "availability", "cart", "deny"])
def test_immediate_reread_requires_renewed_confirmation(home: GrossyPaths, drift: str) -> None:
    rid = plan(home)
    adapter = FakeCart(home)
    original = adapter.read_cart

    def changed_read() -> CartObservation:
        obs = original().model_dump(mode="json")
        if adapter.reads == 2:
            if drift == "price":
                obs["recipe_product_facts"][0]["displayed_price_eur"] = "3.00"
            if drift == "package":
                obs["recipe_product_facts"][0]["package_amount"] = "200"
            if drift == "availability":
                obs["recipe_product_facts"][0]["availability"] = "unknown"
            if drift == "cart":
                obs = observation({"111": 1})
        return validate_cart_observation(obs)

    adapter.read_cart = changed_read  # type: ignore[method-assign]
    if drift == "deny":
        assert apply_recipes(home, rid, adapter, lambda _: False)["status"] == "cancelled"
    else:
        with pytest.raises(WeeklyPlanError):
            apply_recipes(home, rid, adapter, lambda _: True)
    assert not adapter.actions
    assert stored(home, "SELECT * FROM run_mutation_manifests WHERE stage='recipe'") == []


@pytest.mark.parametrize(
    "case", ["indivisible", "pantry-identity", "budget", "excess", "freshness", "reroll"]
)
def test_selected_scaling_budget_and_safety_matrix(home: GrossyPaths, case: str) -> None:
    from grossy.weekly.recipes import run_reroll_apply

    add_product(home, "111", "rice", unit="piece" if case == "indivisible" else "g")
    revision = add_recipe(home, "one")
    if case == "indivisible":
        # Immutable child rows are fixture inputs before planning; no revision rewrite.
        execute(
            home,
            "UPDATE recipe_ingredients SET amount=1,unit='piece',indivisible=1 WHERE revision_id=?",
            (revision,),
        )
    if case == "pantry-identity":
        configure(home, pantry_assumptions=["rice flour"])
    if case == "budget":
        configure(home, weekly_budget="1.00")
    rid = prepare(home)
    run_recipes_apply(home, rid, proposal(rid, [match("one", revision, "111")]))
    run_selection_apply(home, rid, "recipe1")
    obs = observation(
        {"111": 5} if case == "excess" else {},
        [fact(package_amount="1", package_unit="piece")] if case == "indivisible" else None,
    )
    if case == "freshness":
        obs["generated_at"] = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
        with pytest.raises(WeeklyPlanError):
            run_recipe_preflight(home, rid, obs)
        return
    if case == "reroll":
        with pytest.raises(WeeklyPlanError):
            run_reroll_apply(home, rid, "recipe1", "all")
        return
    accepted = run_recipe_preflight(home, rid, obs)
    ingredients = accepted["decisions"][0]["ingredients"]
    if case == "indivisible":
        assert ingredients[0]["linear_target"] == "1.5" and ingredients[0]["target"] == "2"
        assert accepted["manifest"]["additions"][0]["quantity"] == 2
    if case == "pantry-identity":
        assert ingredients[0]["status"] == "exact"
    if case == "excess":
        assert accepted["manifest"]["additions"] == []
    if case == "budget":
        assert "Over budget" in (home.run_dir(rid) / "report.md").read_text()
        assert run_recipe_intent(home, rid, accepted["manifest"])["addition"]["quantity"] == 2


@pytest.mark.parametrize(
    "case",
    ["mismatch", "resume-unrelated", "resume-incomplete", "excess-resume", "report-recovery"],
)
def test_partial_evidence_and_report_recovery_matrix(
    home: GrossyPaths, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = plan(home, shared=False)
    prepared = run_recipe_preflight(home, rid, observation())
    manifest = prepared["manifest"]
    if case == "report-recovery":
        with monkeypatch.context() as patch:

            def fail_write(*_: Any, **__: Any) -> None:
                raise OSError("fake report failure")

            patch.setattr("grossy.weekly.mutation.atomic_write_text", fail_write)
            with pytest.raises(OSError):
                run_recipe_intent(home, rid, manifest)
        assert stored(home, "SELECT report_written FROM weekly_reports")[0][0] == 0
        assert stored(home, "SELECT lifecycle FROM planner_runs")[0][0] == "recipe-apply"
        assert (
            stored(home, "SELECT COUNT(*) FROM run_mutation_manifests WHERE stage='recipe'")[0][0]
            == 1
        )
        run_selection_apply(home, rid, "all")
        assert stored(home, "SELECT report_written FROM weekly_reports")[0][0] == 1
    else:
        run_recipe_intent(home, rid, manifest)
    if case == "mismatch":
        from grossy.weekly.selection import run_recipe_observe

        observed = observation({"111": 1})
        observed["mutation_result"] = {
            "manifest_checksum": manifest["checksum"],
            "outcome": "readback",
        }
        result = run_recipe_observe(home, rid, observed)
        assert result["lifecycle"] == "partial-failure"
        assert report(home, rid)["recipe_mutation_evidence"][-1]["observation"] == observed
        with pytest.raises(WeeklyPlanError):
            run_recipe_preflight(home, rid, observed)
    from grossy.weekly.mutation import run_resume_prepare

    if case in ("resume-unrelated", "resume-incomplete"):
        obs = observation({"999": 1} if case == "resume-unrelated" else {"111": 1})
        if case == "resume-incomplete":
            obs["mutation_result"] = {
                "manifest_checksum": manifest["checksum"],
                "outcome": "incomplete",
            }
        before = stored(home, "SELECT * FROM run_mutation_observations")
        with pytest.raises(WeeklyPlanError):
            run_resume_prepare(home, rid, obs)
        assert stored(home, "SELECT * FROM run_mutation_observations") == before
    if case == "excess-resume":
        accepted = run_resume_prepare(home, rid, observation({"111": 5}))
        assert accepted["manifest"]["stage"] == "recipe"
        assert [a["product_id"] for a in accepted["manifest"]["additions"]] == ["222"]
        assert accepted["decisions"][0]["ingredients"][0]["retail_quantity"] == 0


def test_selected_steps_sanitized_and_historical_evidence_retained(home: GrossyPaths) -> None:
    add_product(home, "111", "rice")
    first = add_recipe(home, "one", "meat")
    second = add_recipe(home, "two", "fish")
    # Deliberately hostile local fixture content exercises the human-report boundary.
    execute(
        home,
        "UPDATE recipe_revisions SET steps_json=? WHERE id=?",
        (json.dumps(["Prepare rice", "Bearer abc+/~==", "account: private fixture"]), first),
    )
    execute(
        home,
        "UPDATE recipe_revisions SET steps_json=? WHERE id=?",
        (json.dumps(["Unselected private preparation"]), second),
    )
    rid = prepare(home)
    run_recipes_apply(
        home, rid, proposal(rid, [match("one", first, "111"), match("two", second, "111")])
    )
    before = report(home, rid)["recurring_mutation_evidence"]
    run_selection_apply(home, rid, "recipe1")
    accepted = run_recipe_preflight(home, rid, observation())
    assert accepted["decisions"][0]["status"] == "passing"
    structured = report(home, rid)
    md = (home.run_dir(rid) / "report.md").read_text()
    for output in (json.dumps(structured), md):
        assert "Prepare rice" in output and "redacted" in output
        assert "abc+/~==" not in output and "private fixture" not in output
        assert "Unselected private preparation" not in output
    assert structured["recurring_mutation_evidence"] == before


@pytest.mark.parametrize("phase", ["fake-adapter", "cli-phases"])
@pytest.mark.parametrize("drift", ["addition", "removal", "quantity", "price", "identity"])
def test_unrelated_cart_drift_before_first_recipe_intent(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch, phase: str, drift: str
) -> None:
    rid = plan(home)
    before = observation({"999": 1})
    after = copy.deepcopy(before)
    if drift == "addition":
        after = observation({"999": 1, "888": 1})
    elif drift == "removal":
        after = observation()
    elif drift == "quantity":
        after = observation({"999": 2})
    elif drift == "price":
        after["lines"][0]["displayed_price_eur"] = "3.00"
    else:
        after["lines"][0]["canonical_url"] = "https://www.ab.gr/en/eshop/changed/p/999"
    manifests = stored(home, "SELECT * FROM run_mutation_manifests")
    if phase == "fake-adapter":
        adapter = FakeCart(home)

        def read() -> CartObservation:
            adapter.reads += 1
            return validate_cart_observation(before if adapter.reads == 1 else after)

        adapter.read_cart = read  # type: ignore[method-assign]
        with pytest.raises(WeeklyPlanError, match="unexpected cart change"):
            apply_recipes(home, rid, adapter, lambda _: True)
        assert adapter.reads == 2 and adapter.actions == []
    else:
        monkeypatch.setattr("grossy.cli.resolve_paths", lambda: home)
        runner = CliRunner()
        path = home.state_dir / "cart.json"
        path.write_text(json.dumps(before))
        accepted = runner.invoke(app, ["weekly", "recipe-preflight", rid, str(path)])
        assert accepted.exit_code == 0
        manifest = json.loads(accepted.stdout)["manifest"]
        path.write_text(json.dumps(after))
        rejected = runner.invoke(app, ["weekly", "recipe-preflight", rid, str(path)])
        assert rejected.exit_code != 0 and "unexpected cart change" in rejected.stderr
        # A previously accepted manifest must not bypass the drift rejection.
        path.write_text(json.dumps(manifest))
        rejected_intent = runner.invoke(app, ["weekly", "recipe-intent", rid, str(path)])
        assert rejected_intent.exit_code != 0
        assert "reconciliation" in rejected_intent.stderr
    assert stored(home, "SELECT * FROM run_mutation_manifests") == manifests
    assert stored(home, "SELECT lifecycle FROM planner_runs")[0][0] == "needs-attention"
    data = report(home, rid)
    assert data["lifecycle"] == "needs-attention"
    assert "unexpected-cart" in json.dumps(data)
    assert "needs-attention" in (home.run_dir(rid) / "report.md").read_text()


@pytest.mark.parametrize("phase", ["cli-phases", "fake-adapter"])
@pytest.mark.parametrize("timing", ["readback", "after-readback"])
@pytest.mark.parametrize("drift", ["price", "identity"])
def test_whole_cart_anchor_after_recipe_action(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch, phase: str, timing: str, drift: str
) -> None:
    rid = plan(home, shared=False)
    monkeypatch.setattr("grossy.cli.resolve_paths", lambda: home)
    runner = CliRunner()
    path = home.state_dir / "recipe-phase.json"

    def cli(command: str, payload: dict[str, Any]) -> Any:
        path.write_text(json.dumps(payload))
        return runner.invoke(app, ["weekly", command, rid, str(path)])

    def changed(payload: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(payload)
        line = next(line for line in payload["lines"] if line["product_id"] == "999")
        if drift == "price":
            line["displayed_price_eur"] = "3.00"
        else:
            line["canonical_url"] = "https://www.ab.gr/en/eshop/changed/p/999"
        return payload

    first_manifest: dict[str, Any] = {}
    if phase == "cli-phases":
        accepted = cli("recipe-preflight", observation({"999": 1}))
        assert accepted.exit_code == 0
        first_manifest = json.loads(accepted.stdout)["manifest"]
        assert cli("recipe-intent", first_manifest).exit_code == 0
        addition = first_manifest["additions"][0]
        after = observation({"999": 1, addition["product_id"]: addition["quantity"]})
        readback = changed(after) if timing == "readback" else copy.deepcopy(after)
        readback["mutation_result"] = {
            "manifest_checksum": first_manifest["checksum"],
            "outcome": "readback",
        }
        reconciled = cli("recipe-observe", readback)
        if timing == "readback":
            assert reconciled.exit_code != 0
        else:
            assert reconciled.exit_code == 0
            # Accept the next action, then drift: old authorization must be invalidated.
            accepted = cli("recipe-preflight", after)
            assert accepted.exit_code == 0
            stale = json.loads(accepted.stdout)["manifest"]
            rejected = cli("recipe-preflight", changed(after))
            assert rejected.exit_code != 0 and "unexpected cart change" in rejected.stderr
            rejected = cli("recipe-intent", stale)
            assert rejected.exit_code != 0 and "reconciliation" in rejected.stderr
    else:
        adapter = FakeCart(home)
        adapter.cart = {"999": 1}

        def read() -> CartObservation:
            adapter.reads += 1
            payload = observation(adapter.cart)
            if adapter.actions and (timing == "readback" or adapter.reads >= 4):
                payload = changed(payload)
            return validate_cart_observation(payload)

        adapter.read_cart = read  # type: ignore[method-assign]
        if timing == "readback":
            assert apply_recipes(home, rid, adapter, lambda _: True)["status"] == "error"
        else:
            with pytest.raises(WeeklyPlanError, match="unexpected cart change"):
                apply_recipes(home, rid, adapter, lambda _: True)
        assert len(adapter.actions) == 1
        first_manifest = json.loads(
            stored(home, "SELECT manifest_json FROM run_mutation_manifests WHERE stage='recipe'")[
                0
            ][0]
        )

    assert len(stored(home, "SELECT * FROM run_mutation_manifests WHERE stage='recipe'")) == 1
    lifecycle = "partial-failure" if timing == "readback" else "needs-attention"
    assert stored(home, "SELECT lifecycle FROM planner_runs")[0][0] == lifecycle
    assert cli("recipe-intent", first_manifest).exit_code != 0
    before_resume = stored(home, "SELECT * FROM run_mutation_observations")
    addition = first_manifest["additions"][0]
    drifted = changed(observation({"999": 1, addition["product_id"]: addition["quantity"]}))
    resumed = cli("resume-prepare", drifted)
    assert resumed.exit_code != 0 and "unexplained cart" in resumed.stderr
    assert stored(home, "SELECT * FROM run_mutation_observations") == before_resume
    data = report(home, rid)
    assert data["lifecycle"] == lifecycle
    assert lifecycle in (home.run_dir(rid) / "report.md").read_text()
    if timing == "readback":
        discrepancy = json.loads(
            stored(
                home,
                "SELECT discrepancies_json FROM run_mutation_observations ORDER BY id DESC LIMIT 1",
            )[0][0]
        )
        assert discrepancy["expected_lines"]["999"] != discrepancy["observed_lines"]["999"]
        assert (
            stored(
                home, "SELECT failure_class FROM run_mutation_observations ORDER BY id DESC LIMIT 1"
            )[0][0]
            == "partial"
        )
    else:
        assert "unexpected-cart" in json.dumps(data)
