"""Explicit bounded removal and state-oriented recovery, only synthetic carts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_weekly_mutation import home as base_home
from test_weekly_mutation import observation, prepare, rows
from typer.testing import CliRunner

from grossy.browser.observation import CartObservation, validate_cart_observation
from grossy.browser.removal import RemovalCartAdapter, apply_removal
from grossy.cli import app
from grossy.paths import GrossyPaths
from grossy.weekly.closure import run_close, run_report_regenerate
from grossy.weekly.mutation import (
    run_resume_prepare,
)
from grossy.weekly.plan import WeeklyPlanError
from grossy.weekly.removal import run_removal_intent, run_removal_observe, run_removal_preflight


@pytest.fixture
def home(tmp_path: Path) -> GrossyPaths:
    return base_home.__wrapped__(tmp_path)


def added(paths: GrossyPaths) -> str:
    run_id = prepare(paths)
    from test_weekly_mutation import FakeAdditiveCart

    from grossy.browser.mutation import apply_recurring

    fake = FakeAdditiveCart(paths, {"111": 1, "999": 8})
    assert apply_recurring(paths, run_id, fake, lambda _: True)["status"] == "success"
    # Complete Stage1 with an empty established-recipe proposal through public phase.
    from grossy.recipes.proposal import proposal_checksum
    from grossy.weekly.recipes import run_recipes_apply

    body = {
        "schema_name": "grossy.weekly.recipe-options.v1",
        "schema_version": 1,
        "generated_at": observation({})["generated_at"],
        "run_id": run_id,
        "matches": [],
    }
    body["checksum"] = proposal_checksum(body)
    run_recipes_apply(paths, run_id, body)
    return run_id


class FakeRemoval(RemovalCartAdapter):
    def __init__(self, quantities: dict[str, int], *, drift: bool = False, fail: bool = False):
        self.quantities = quantities
        self.reads = 0
        self.writes: list[tuple[str, str, int]] = []
        self.drift = drift
        self.fail = fail

    def read_cart(self) -> CartObservation:
        self.reads += 1
        if self.drift and self.reads == 2:
            self.quantities["999"] = 7
        return validate_cart_observation(observation(self.quantities))

    def remove_bounded(self, product_id: str, canonical_url: str, quantity: int) -> None:
        self.writes.append((product_id, canonical_url, quantity))
        if self.fail:
            raise RuntimeError("synthetic browser failure")
        self.quantities[product_id] -= quantity


def test_default_bounds_audit_and_no_restoration(home: GrossyPaths) -> None:
    run_id = added(home)
    fake = FakeRemoval({"111": 3, "222": 1, "999": 8})
    with pytest.raises(WeeklyPlanError, match="attribution"):
        run_removal_preflight(home, run_id, observation(fake.quantities), product="111", quantity=3)
    assert apply_removal(home, run_id, fake, lambda _: True, product="111")["status"] == "success"
    assert fake.quantities == {"111": 1, "222": 1, "999": 8} and len(fake.writes) == 1
    assert rows(
        home,
        "SELECT authorized_quantity,run_attributed_quantity,preexisting_quantity,"
        "confirmed FROM run_removals",
    ) == [(2, 2, 0, 1)]
    with pytest.raises(WeeklyPlanError, match="positive"):
        run_removal_preflight(home, run_id, observation(fake.quantities), product="111")
    with pytest.raises(WeeklyPlanError, match="restored"):
        run_resume_prepare(home, run_id, observation(fake.quantities))
    result = run_close(home, run_id, no_recipes=True)
    assert result["lifecycle"] == "completed" and not result["order_placed"]
    assert not rows(home, "SELECT id FROM source_orders")
    assert rows(home, "SELECT detail FROM run_events WHERE event_type='selection-empty'")


@pytest.mark.parametrize("include,approve", [(False, True), (True, False), (True, True)])
def test_preexisting_confirmation(home: GrossyPaths, include: bool, approve: bool) -> None:
    run_id = added(home)
    fake = FakeRemoval({"111": 3, "222": 1, "999": 8})
    if not include:
        with pytest.raises(WeeklyPlanError, match="include-preexisting"):
            apply_removal(home, run_id, fake, lambda _: approve, product="999", quantity=1)
    else:
        result = apply_removal(
            home,
            run_id,
            fake,
            lambda _: approve,
            product="999",
            quantity=1,
            include_preexisting=True,
        )
        assert result["status"] == ("success" if approve else "cancelled")
    assert len(fake.writes) == int(include and approve)
    assert len(rows(home, "SELECT id FROM run_removals")) == int(include and approve)


@pytest.mark.parametrize("failure", ["drift", "failed", "mismatch", "pending"])
def test_failure_stops_preserves_evidence(home: GrossyPaths, failure: str) -> None:
    run_id = added(home)
    fake = FakeRemoval(
        {"111": 3, "222": 1, "999": 8}, drift=failure == "drift", fail=failure == "failed"
    )
    if failure == "drift":
        with pytest.raises(WeeklyPlanError, match="drift"):
            apply_removal(home, run_id, fake, lambda _: True, product="111")
        assert not fake.writes
    elif failure == "failed":
        assert apply_removal(home, run_id, fake, lambda _: True, product="111")["status"] == "error"
        assert len(fake.writes) == 1
    else:
        manifest = run_removal_preflight(home, run_id, observation(fake.quantities), product="111")[
            "manifest"
        ]
        run_removal_intent(home, run_id, manifest, confirmed=True)
        if failure == "mismatch":
            obs = observation({"111": 2, "222": 1, "999": 8})
            obs["mutation_result"] = {
                "manifest_checksum": manifest["checksum"],
                "outcome": "readback",
            }
            assert run_removal_observe(home, run_id, obs)["status"] == "error"
        with pytest.raises(WeeklyPlanError):
            run_removal_intent(home, run_id, manifest, confirmed=True)
    with pytest.raises(WeeklyPlanError):
        run_resume_prepare(home, run_id, observation(fake.quantities))
    assert run_close(home, run_id, abandon=True)["lifecycle"] == "abandoned"
    assert fake.quantities["999"] >= 7


def test_report_failure_truth_and_regeneration(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = added(home)
    import grossy.weekly.closure as closure

    original = closure.atomic_write_text

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("synthetic disk full")

    monkeypatch.setattr(closure, "atomic_write_text", fail)
    with pytest.raises(OSError):
        run_close(home, run_id, no_recipes=True)
    assert rows(home, "SELECT lifecycle FROM planner_runs") == [("completed",)]
    assert rows(home, "SELECT report_written FROM weekly_reports") == [(0,)]
    evidence = rows(home, "SELECT manifest_json FROM run_mutation_manifests")
    monkeypatch.setattr(closure, "atomic_write_text", original)
    run_report_regenerate(home, run_id)
    first = (home.runs_dir / run_id / "report.json").read_bytes()
    run_report_regenerate(home, run_id)
    assert first == (home.runs_dir / run_id / "report.json").read_bytes()
    assert evidence == rows(home, "SELECT manifest_json FROM run_mutation_manifests")


def test_cli_confirmation_denial_and_eof(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = added(home)
    monkeypatch.setattr("grossy.cli.resolve_paths", lambda: home)
    manifest = run_removal_preflight(
        home,
        run_id,
        observation({"111": 3, "222": 1, "999": 8}),
        product="999",
        quantity=1,
        include_preexisting=True,
    )["manifest"]
    proposal = tmp_path / "manifest.json"
    proposal.write_text(json.dumps(manifest))
    for answer in ("n\n", ""):
        result = CliRunner(mix_stderr=False).invoke(
            app, ["weekly", "removal-intent", run_id, str(proposal)], input=answer
        )
        assert result.exit_code != 0
        assert not rows(home, "SELECT id FROM run_removals")
    result = CliRunner(mix_stderr=False).invoke(
        app, ["weekly", "removal-intent", run_id, str(proposal)], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "persisted"
    assert "1 pre-existing" in result.stderr


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("run_id", "other"),
        ("preexisting_quantity", 1),
        ("run_attributed_quantity", 999),
        ("unknown", "field"),
        ("generated_at", "2000-01-01T00:00:00Z"),
    ],
)
def test_manifest_binding_rejection(home: GrossyPaths, field: str, value: Any) -> None:
    from grossy.recipes.proposal import proposal_checksum

    run_id = added(home)
    manifest = run_removal_preflight(
        home, run_id, observation({"111": 3, "222": 1, "999": 8}), product="111"
    )["manifest"]
    manifest[field] = value
    manifest["checksum"] = proposal_checksum({k: v for k, v in manifest.items() if k != "checksum"})
    with pytest.raises((ValueError, WeeklyPlanError)):
        run_removal_intent(home, run_id, manifest, confirmed=True)
    assert not rows(home, "SELECT id FROM run_removals")


def test_stale_cart_deny_revocation_and_replayed_intent(home: GrossyPaths) -> None:
    from grossy.recipes.proposal import proposal_checksum

    run_id = added(home)
    stale = observation({"111": 3, "222": 1, "999": 8})
    stale["generated_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(WeeklyPlanError, match="stale"):
        run_removal_preflight(home, run_id, stale, product="111")
    fake = FakeRemoval({"111": 3, "222": 1, "999": 8})
    approvals = iter([True, False])
    assert (
        apply_removal(home, run_id, fake, lambda _: next(approvals), product="111")["status"]
        == "cancelled"
    )
    assert not fake.writes and not rows(home, "SELECT id FROM run_removals")
    manifest = run_removal_preflight(home, run_id, observation(fake.quantities), product="111")[
        "manifest"
    ]
    run_removal_intent(home, run_id, manifest, confirmed=True)
    after = observation({"111": 1, "222": 1, "999": 8})
    after["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    assert run_removal_observe(home, run_id, after)["status"] == "success"
    manifest["generated_at"] = observation({})["generated_at"]
    manifest["checksum"] = proposal_checksum({k: v for k, v in manifest.items() if k != "checksum"})
    with pytest.raises(WeeklyPlanError):
        run_removal_intent(home, run_id, manifest, confirmed=True)
    assert len(rows(home, "SELECT id FROM run_removals")) == 1


@pytest.mark.parametrize("applied", [True, False])
def test_removal_does_not_block_unrelated_recipe_and_history_unchanged(
    home: GrossyPaths, applied: bool
) -> None:
    # The fixture's YAML targets are not relevant to this recipe-only run.
    from test_weekly_recipes import configure
    from test_weekly_selection import FakeCart, plan
    from test_weekly_selection import observation as recipe_obs

    from grossy.browser.mutation import apply_recipes
    from grossy.weekly.selection import run_recipe_preflight

    configure(home, staples={"breakfast": [], "lunch": []})
    run_id = plan(home, shared=False)
    cart = FakeCart(home)
    assert apply_recipes(home, run_id, cart, lambda _: True)["status"] == "success"
    history = {
        table: rows(home, f"SELECT * FROM {table}")
        for table in ("source_orders", "source_order_products", "product_families")
    }
    before = recipe_obs(cart.cart)
    manifest = run_removal_preflight(home, run_id, before, product="111", quantity=1)["manifest"]
    run_removal_intent(home, run_id, manifest, confirmed=True)
    if applied:
        cart.cart["111"] -= 1
    after = recipe_obs(cart.cart)
    after["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    result = run_removal_observe(home, run_id, after)
    assert result["result"] == ("verified" if applied else "not-applied")
    resumed = run_resume_prepare(home, run_id, recipe_obs(cart.cart))
    prepared = run_recipe_preflight(home, run_id, recipe_obs(cart.cart))
    for preflight in (resumed, prepared):
        assert preflight["manifest"]["additions"] == []
        assert [d["status"] for d in preflight["decisions"]] == [
            "blocked" if applied else "passing",
            "passing",
        ]
        if applied:
            assert "removal" in " ".join(preflight["decisions"][0]["reasons"])
    assert history == {table: rows(home, f"SELECT * FROM {table}") for table in history}


@pytest.mark.parametrize("interrupted_readback", [False, True])
def test_stage1_removal_allows_unrelated_stage2_additions(
    home: GrossyPaths, interrupted_readback: bool
) -> None:
    from test_weekly_recipes import add_product, add_recipe, configure, match, proposal
    from test_weekly_selection import FakeCart
    from test_weekly_selection import observation as recipe_obs

    from grossy.browser.mutation import AdditiveCartAdapter, apply_recipes, apply_recurring
    from grossy.weekly.plan import run_baseline_apply, run_plan_prepare
    from grossy.weekly.recipes import run_recipes_apply
    from grossy.weekly.selection import run_selection_apply

    configure(
        home,
        staples={"breakfast": [{"key": "rice", "product_id": "111", "quantity": 1}], "lunch": []},
    )
    add_product(home, "111", "rice")
    add_product(home, "222", "beans")
    first = add_recipe(home, "one", "meat", names=["rice"])
    second = add_recipe(home, "two", "fish", names=["beans"])
    run_id = run_plan_prepare(home, skip_history_sync=True)["run_id"]
    run_baseline_apply(home, run_id, recipe_obs({}))

    class Recurring(AdditiveCartAdapter):
        cart: dict[str, int] = {}

        def read_cart(self) -> CartObservation:
            data = recipe_obs(self.cart)
            data["recurring_product_facts"] = [
                {
                    "product_id": "111",
                    "canonical_url": "https://www.ab.gr/en/eshop/item/p/111",
                    "displayed_price_eur": "2.00",
                    "availability": "available",
                }
            ]
            return validate_cart_observation(data)

        def add_positive(self, product_id: str, canonical_url: str, quantity: int) -> None:
            self.cart[product_id] = self.cart.get(product_id, 0) + quantity

    recurring = Recurring()
    assert apply_recurring(home, run_id, recurring, lambda _: True)["status"] == "success"
    run_recipes_apply(
        home,
        run_id,
        proposal(
            run_id, [match("one", first, "111", "rice"), match("two", second, "222", "beans")]
        ),
    )
    manifest = run_removal_preflight(home, run_id, recipe_obs(recurring.cart), product="111")[
        "manifest"
    ]
    run_removal_intent(home, run_id, manifest, confirmed=True)
    after = recipe_obs({})
    after["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    assert run_removal_observe(home, run_id, after)["status"] == "success"
    run_selection_apply(home, run_id, "all")
    cart = FakeCart(home, fail="readback" if interrupted_readback else None)
    result = apply_recipes(home, run_id, cart, lambda _: True)
    assert result["status"] == ("error" if interrupted_readback else "success")
    assert cart.actions and all(pid == "222" for pid, _ in cart.actions)
    actions = list(cart.actions)
    if interrupted_readback:
        resumed = run_resume_prepare(home, run_id, recipe_obs(cart.cart))
        assert resumed["manifest"]["additions"] == []
        # Reconciliation must supersede the older removal-cart even before completion.
        assert (
            run_removal_preflight(home, run_id, recipe_obs(cart.cart), product="222", quantity=1)[
                "status"
            ]
            == "accepted"
        )
        assert apply_recipes(home, run_id, cart, lambda _: True)["status"] == "success"
        assert cart.actions == actions
    # Zero-add completion must also retain the authoritative whole-cart anchor.
    accepted = run_removal_preflight(home, run_id, recipe_obs(cart.cart), product="222", quantity=1)
    assert accepted["manifest"]["run_attributed_quantity"] == 1
    assert accepted["manifest"]["preexisting_quantity"] == 0
    assert rows(home, "SELECT lifecycle FROM planner_runs") == [("awaiting-user-verification",)]
    assert not rows(home, "SELECT id FROM run_events WHERE event_type='removal-drift'")
    assert run_close(home, run_id)["lifecycle"] == "completed"


def test_unattempted_report_failure_safe_no_effect_reconciliation(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grossy.weekly.closure as closure

    run_id = added(home)
    before = observation({"111": 3, "222": 1, "999": 8})
    manifest = run_removal_preflight(home, run_id, before, product="111")["manifest"]
    original = closure.atomic_write_text

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("synthetic disk full before browser action")

    monkeypatch.setattr(closure, "atomic_write_text", fail)
    with pytest.raises(OSError):
        run_removal_intent(home, run_id, manifest, confirmed=True)
    assert rows(home, "SELECT id FROM run_removals")
    assert rows(
        home, "SELECT failure_class FROM run_mutation_observations ORDER BY id DESC LIMIT 1"
    ) == [("total",)]
    monkeypatch.setattr(closure, "atomic_write_text", original)
    for outcome in ("failed", "incomplete"):
        reread = observation({"111": 3, "222": 1, "999": 8})
        reread["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": outcome}
        assert run_removal_observe(home, run_id, reread)["status"] == "error"
        with pytest.raises(WeeklyPlanError):
            run_removal_intent(home, run_id, manifest, confirmed=True)
    reread = observation({"111": 3, "222": 1, "999": 8})
    reread["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    result = run_removal_observe(home, run_id, reread)
    assert result["result"] == "not-applied" and result["lifecycle"] == "awaiting-recipe-selection"
    assert not rows(home, "SELECT id FROM run_events WHERE event_type='removal-verified'")
    assert rows(home, "SELECT id FROM run_events WHERE event_type='report-failure'")
    assert (
        len(
            rows(
                home,
                "SELECT id FROM run_mutation_observations WHERE manifest_id="
                "(SELECT id FROM run_mutation_manifests WHERE stage='removal')",
            )
        )
        == 4
    )
    with pytest.raises(WeeklyPlanError):
        run_removal_intent(home, run_id, manifest, confirmed=True)
    renewed = run_removal_preflight(home, run_id, reread, product="111")["manifest"]
    assert renewed["checksum"] != manifest["checksum"]
    assert renewed["run_attributed_quantity"] == 2


def test_abandon_and_report_recovery_work_with_broken_policy(home: GrossyPaths) -> None:
    from grossy.weekly.plan import run_plan_prepare

    run_id = added(home)
    manifest_count = len(rows(home, "SELECT id FROM run_mutation_manifests"))
    home.config_file.write_text("invalid: [yaml")
    assert run_close(home, run_id, abandon=True)["lifecycle"] == "abandoned"
    assert run_report_regenerate(home, run_id)["status"] == "success"
    assert len(rows(home, "SELECT id FROM run_mutation_manifests")) == manifest_count
    with pytest.raises(WeeklyPlanError):
        run_close(home, run_id)
    with pytest.raises(WeeklyPlanError):
        run_plan_prepare(home, skip_history_sync=True)


def test_shared_recipe_credit_cannot_undo_removal(home: GrossyPaths) -> None:
    from test_weekly_recipes import configure
    from test_weekly_selection import FakeCart, plan
    from test_weekly_selection import observation as recipe_obs

    from grossy.browser.mutation import apply_recipes
    from grossy.weekly.selection import run_recipe_preflight

    configure(home, staples={"breakfast": [], "lunch": []})
    run_id = plan(home, shared=True)
    cart = FakeCart(home)
    assert apply_recipes(home, run_id, cart, lambda _: True)["status"] == "success"
    manifest = run_removal_preflight(
        home, run_id, recipe_obs(cart.cart), product="111", quantity=1
    )["manifest"]
    run_removal_intent(home, run_id, manifest, confirmed=True)
    cart.cart["111"] -= 1
    after = recipe_obs(cart.cart)
    after["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    run_removal_observe(home, run_id, after)
    prepared = run_recipe_preflight(home, run_id, recipe_obs(cart.cart))
    assert not prepared["manifest"]["additions"]
    assert all(d["status"] == "blocked" for d in prepared["decisions"])


@pytest.mark.parametrize(
    "quantities", [{"111": 3, "222": 1, "999": 8}, {"111": 1, "222": 1, "999": 8}]
)
def test_pre_intent_readback_cannot_clear_authorization(
    home: GrossyPaths, quantities: dict[str, int]
) -> None:
    run_id = added(home)
    manifest = run_removal_preflight(
        home, run_id, observation({"111": 3, "222": 1, "999": 8}), product="111"
    )["manifest"]
    early = observation(quantities)
    early["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    run_removal_intent(home, run_id, manifest, confirmed=True)
    intent_time = rows(home, "SELECT created_at FROM run_mutation_manifests WHERE stage='removal'")[
        0
    ][0]
    assert manifest["generated_at"] < early["generated_at"] < intent_time
    evidence = rows(home, "SELECT * FROM run_mutation_observations")
    with pytest.raises(WeeklyPlanError, match="predates removal intent"):
        run_removal_observe(home, run_id, early)
    assert rows(home, "SELECT * FROM run_mutation_observations") == evidence
    with pytest.raises(WeeklyPlanError):
        run_removal_preflight(home, run_id, observation(quantities), product="111")
    with pytest.raises(WeeklyPlanError):
        run_removal_intent(home, run_id, manifest, confirmed=True)
    assert not rows(home, "SELECT id FROM run_events WHERE event_type='removal-cart'")
