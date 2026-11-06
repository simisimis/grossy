"""Recurring positive-add durable boundary, recovery and unsafe-input matrix."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_weekly_recipes import proposal as empty_recipe_options
from typer.testing import CliRunner

from grossy.browser.mutation import AdditiveCartAdapter, apply_recurring
from grossy.browser.observation import CartObservation, validate_cart_observation
from grossy.cli import app
from grossy.db.connection import open_connection
from grossy.db.lock import LockHeldError, WriterLock
from grossy.history.maintenance import run_family_link, run_product_register, run_set_primary
from grossy.init import do_init
from grossy.paths import GrossyPaths, resolve_paths
from grossy.recipes.proposal import proposal_checksum
from grossy.weekly.mutation import (
    run_recurring_intent,
    run_recurring_observe,
    run_recurring_preflight,
    run_resume_prepare,
)
from grossy.weekly.plan import WeeklyPlanError, run_baseline_apply, run_plan_prepare
from grossy.weekly.recipes import run_recipes_apply
from grossy.weekly.selection import run_recipe_intent, run_recipe_observe, run_recipe_preflight


def now() -> str:
    return datetime.now(UTC).isoformat()


def url(pid: str) -> str:
    return f"https://store.example/en/eshop/food/p/{pid}"


def observation(quantities: dict[str, int], *, facts: bool = True) -> dict[str, Any]:
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
            for pid, qty in quantities.items()
            if qty
        ],
        "recurring_product_facts": [
            {
                "product_id": pid,
                "canonical_url": url(pid),
                "displayed_price_eur": "2.00",
                "availability": "available",
            }
            for pid in ["111", "222", "333"]
        ]
        if facts
        else [],
    }


@pytest.fixture
def home(tmp_path: Path) -> GrossyPaths:
    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    config = yaml.safe_load(paths.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "first", "product_id": "111", "product_url": url("111"), "quantity": 3},
        {"key": "second", "product_id": "222", "product_url": url("222"), "quantity": 1},
    ]
    paths.config_file.write_text(yaml.safe_dump(config))
    return paths


def prepare(paths: GrossyPaths, quantities: dict[str, int] | None = None) -> str:
    run_id = str(run_plan_prepare(paths, skip_history_sync=True)["run_id"])
    run_baseline_apply(paths, run_id, observation(quantities or {"999": 8}))
    return run_id


def rows(paths: GrossyPaths, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
    conn = open_connection(paths.database_file)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def change(paths: GrossyPaths, sql: str) -> None:
    conn = open_connection(paths.database_file)
    try:
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def seed_yaml_family(paths: GrossyPaths) -> None:
    """Two approved members, without learned recurrence, for YAML resolution."""
    conn = open_connection(paths.database_file)
    try:
        for pid in ("111", "333"):
            conn.execute(
                "INSERT INTO products(retailer_product_id,canonical_url,name,created_at,"
                "updated_at) VALUES (?,?,?,?,?)",
                (pid, url(pid), f"Product {pid}", now(), now()),
            )
        family_id = conn.execute(
            "INSERT INTO product_families(family_id,name,created_at,updated_at) "
            "VALUES ('yaml-family','YAML family',?,?)",
            (now(), now()),
        ).lastrowid
        conn.execute(
            "INSERT INTO family_members(family_id,product_id,role,approval_state,created_at) "
            "SELECT ?,id,'alternative','approved',? FROM products "
            "WHERE retailer_product_id IN ('111','333')",
            (family_id, now()),
        )
        conn.commit()
    finally:
        conn.close()


def prepare_due_family(paths: GrossyPaths, *, yaml_preferred: bool = False) -> str:
    seed_yaml_family(paths)
    run_set_primary(paths, "yaml-family", "111")
    change(
        paths,
        "UPDATE product_families SET recurrence_weeks=1, recurrence_source='observed', "
        "recurrence_confidence='high', last_purchase_date='2020-01-01'",
    )
    config = yaml.safe_load(paths.config_file.read_text())
    config["staples"]["breakfast"] = (
        [{"key": "explicit", "product_id": "111", "quantity": 1}] if yaml_preferred else []
    )
    paths.config_file.write_text(yaml.safe_dump(config))
    run_id = prepare(paths, {"999": 8})
    assert rows(
        paths,
        "SELECT p.retailer_product_id,i.source_type,i.status FROM run_recurring_items i "
        "JOIN products p ON p.id=i.product_id",
    ) == [("111", "both" if yaml_preferred else "learned", "pending")]
    return run_id


@pytest.mark.parametrize("correction_phase", ["before-preflight", "after-preflight"])
def test_supported_primary_correction_rejects_stale_learned_intent_without_writes(
    home: GrossyPaths, correction_phase: str
) -> None:
    run_id = prepare_due_family(home)
    accepted = (
        run_recurring_preflight(home, run_id, observation({"999": 8}))
        if correction_phase == "after-preflight"
        else None
    )
    assert run_set_primary(home, "yaml-family", "333")["status"] == "success"
    assert rows(
        home,
        "SELECT p.retailer_product_id FROM family_members m JOIN products p "
        "ON p.id=m.product_id WHERE m.role='primary'",
    ) == [("333",)]
    if accepted is not None:
        with pytest.raises(WeeklyPlanError, match="stale family policy"):
            run_recurring_intent(home, run_id, accepted["manifest"])
    with pytest.raises(WeeklyPlanError, match="stale family policy"):
        run_recurring_preflight(home, run_id, observation({"999": 8}))
    fake = FakeAdditiveCart(home, {"999": 8})
    confirmations: list[dict[str, Any]] = []

    def confirm(manifest: dict[str, Any]) -> bool:
        confirmations.append(manifest)
        return True

    with pytest.raises(WeeklyPlanError, match="stale family policy"):
        apply_recurring(home, run_id, fake, confirm)
    assert not confirmations and not fake.writes
    assert not rows(home, "SELECT id FROM run_mutation_manifests")
    assert len(rows(home, "SELECT id FROM run_events WHERE event_type='recurring-preflight'")) == (
        1 if accepted is not None else 0
    )
    assert rows(home, "SELECT lifecycle FROM planner_runs") == [("planning",)]


def test_current_learned_preference_preserves_approved_live_fallback(home: GrossyPaths) -> None:
    run_id = prepare_due_family(home)
    data = observation({"999": 8})
    data["recurring_product_facts"][0]["availability"] = "unavailable"
    accepted = run_recurring_preflight(home, run_id, data)
    assert accepted["manifest"]["additions"][0]["product_id"] == "333"
    assert run_recurring_intent(home, run_id, accepted["manifest"])["status"] == "persisted"


def test_yaml_preference_wins_over_supported_learned_primary_correction(home: GrossyPaths) -> None:
    old_run = prepare_due_family(home, yaml_preferred=True)
    run_set_primary(home, "yaml-family", "333")
    with pytest.raises(WeeklyPlanError, match="stale family policy"):
        run_recurring_preflight(home, old_run, observation({"999": 8}))
    # Explicit synthetic lifecycle decision, never an automatic maintenance effect.
    from grossy.weekly.closure import run_close

    run_close(home, old_run, abandon=True)
    run_id = prepare(home, {"999": 8})
    accepted = run_recurring_preflight(home, run_id, observation({"999": 8}))
    assert accepted["manifest"]["additions"] == [
        {"product_id": "111", "canonical_url": url("111"), "quantity": 1, "price_eur": "2.00"}
    ]
    assert run_recurring_intent(home, run_id, accepted["manifest"])["status"] == "persisted"


def test_changed_yaml_preferred_family_member_requires_replanning(home: GrossyPaths) -> None:
    seed_yaml_family(home)
    run_id = prepare(home)
    config = yaml.safe_load(home.config_file.read_text())
    first = config["staples"]["breakfast"][0]
    first.update(product_id="333", product_url=url("333"), allow_substitution=False)
    home.config_file.write_text(yaml.safe_dump(config))
    fake = FakeAdditiveCart(home, {"999": 8})
    with pytest.raises(WeeklyPlanError, match="stale recurring YAML target"):
        apply_recurring(home, run_id, fake, lambda manifest: True)
    assert not fake.writes
    assert not rows(home, "SELECT id FROM run_mutation_manifests")
    assert not rows(home, "SELECT id FROM run_events WHERE event_type='recurring-preflight'")


@pytest.mark.parametrize("reference", ["repeated-product", "same-family"])
@pytest.mark.parametrize("later_limit", [None, "1.00"], ids=["passing", "later-hard-limit"])
def test_duplicate_yaml_uses_baseline_first_reference_and_all_limits(
    home: GrossyPaths, reference: str, later_limit: str | None
) -> None:
    if reference == "same-family":
        seed_yaml_family(home)
    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"] = [{"key": "first", "product_id": "111", "quantity": 1}]
    config["staples"]["lunch"] = [
        {
            "key": "later",
            "product_id": "333" if reference == "same-family" else "111",
            "quantity": 3,
            "max_product_price_eur": later_limit,
        }
    ]
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = prepare(home, {"999": 8})
    assert rows(home, "SELECT intended_quantity FROM run_recurring_items") == [(1,)]
    report = json.loads((home.run_dir(run_id) / "report.json").read_text())
    assert set(report["recurring_items"][0]["category_refs"]) >= {"breakfast", "lunch"}
    accepted = run_recurring_preflight(home, run_id, observation({"999": 8}))
    if later_limit is None:
        assert accepted["manifest"]["additions"] == [
            {"product_id": "111", "canonical_url": url("111"), "quantity": 1, "price_eur": "2.00"}
        ]
    else:
        assert accepted["manifest"]["additions"] == []
        assert accepted["decisions"] == [{"product_id": "111", "status": "blocked-hard-limit"}]


class FakeAdditiveCart(AdditiveCartAdapter):
    """Scripted cart with assertions at the actual browser-write seam."""

    def __init__(self, paths: GrossyPaths, quantities: dict[str, int], failure: str = "") -> None:
        self.paths = paths
        self.quantities = dict(quantities)
        self.failure = failure
        self.writes: list[tuple[str, int]] = []
        self.fail_readback = False

    def read_cart(self) -> CartObservation:
        if self.fail_readback:
            raise RuntimeError("fake readback unavailable")
        return validate_cart_observation(observation(self.quantities))

    def add_positive(self, product_id: str, canonical_url: str, quantity: int) -> None:
        assert quantity > 0 and canonical_url == url(product_id)
        assert rows(self.paths, "SELECT lifecycle FROM planner_runs") == [("recurring-apply",)]
        manifests = rows(self.paths, "SELECT id,manifest_json FROM run_mutation_manifests")
        assert manifests and rows(
            self.paths, "SELECT event_type FROM run_events WHERE event_type='recurring-preflight'"
        )
        persisted = json.loads(manifests[-1][1])
        assert persisted["additions"][0]["product_id"] == product_id
        assert persisted["additions"][0]["quantity"] == quantity
        assert rows(
            self.paths,
            "SELECT failure_class FROM run_mutation_observations WHERE manifest_id=?",
            (manifests[-1][0],),
        ) == [("total",)]
        self.writes.append((product_id, quantity))
        if self.failure == "before":
            raise RuntimeError("fake add failed")
        self.quantities[product_id] = self.quantities.get(product_id, 0) + quantity
        if self.failure == "mismatch":
            self.quantities[product_id] += 1
        if self.failure == "readback":
            self.fail_readback = True
        if self.failure == "after":
            raise RuntimeError("fake result lost after addition")


def test_positive_delta_success_retry_and_reports(home: GrossyPaths) -> None:
    run_id = prepare(home)
    fake = FakeAdditiveCart(home, {"999": 8})
    assert apply_recurring(home, run_id, fake, lambda manifest: True)["status"] == "success"
    assert fake.writes == [("111", 3), ("222", 1)]
    assert fake.quantities == {"111": 3, "222": 1, "999": 8}
    assert apply_recurring(home, run_id, fake, lambda manifest: True)["status"] == "success"
    assert fake.writes == [("111", 3), ("222", 1)]
    report = json.loads((home.run_dir(run_id) / "report.json").read_text())
    assert report["budget"]["verified_recurring_eur"] == "8.00"
    assert report["budget"]["resulting_total_eur"] == "24.00"
    assert report["baseline"][0]["quantity"] == 8
    assert report["recurring_mutation_evidence"]
    assert "Recurring mutation evidence" in (home.run_dir(run_id) / "report.md").read_text()
    assert rows(home, "SELECT purchase_order_count FROM products") == [(0,), (0,), (0,)]


@pytest.mark.parametrize("credit", ["exact", "approved-alternative"])
def test_positive_credit_marks_cart_covered_without_top_up(home: GrossyPaths, credit: str) -> None:
    """Basket quantity 1 against target 2 is covered (missing 0); zero credit keeps the
    full intended quantity missing."""
    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"][0].update(quantity=2)
    if credit == "approved-alternative":
        seed_yaml_family(home)
        config["staples"]["breakfast"][0].update(
            allow_substitution=True, approved_alternative_product_ids=["333"]
        )
    home.config_file.write_text(yaml.safe_dump(config))
    cart = {"111" if credit == "exact" else "333": 1, "999": 8}
    run_id = prepare(home, cart)
    assert rows(
        home,
        "SELECT p.retailer_product_id,i.intended_quantity,i.credited_quantity,i.missing_delta,"
        "i.status FROM run_recurring_items i JOIN products p ON p.id=i.product_id "
        "ORDER BY p.retailer_product_id",
    ) == [("111", 2, 1, 0, "skipped"), ("222", 1, 0, 1, "pending")]
    accepted = run_recurring_preflight(home, run_id, observation(cart))
    assert {"product_id": "111", "status": "cart-covered"} in accepted["decisions"]
    assert accepted["manifest"]["additions"] == [
        {"product_id": "222", "canonical_url": url("222"), "quantity": 1, "price_eur": "2.00"}
    ]
    fake = FakeAdditiveCart(home, cart)
    assert apply_recurring(home, run_id, fake, lambda manifest: True)["status"] == "success"
    assert fake.writes == [("222", 1)]
    assert fake.quantities == {**cart, "222": 1}


@pytest.mark.parametrize("failure", ["before", "after", "mismatch", "readback"])
def test_failure_stops_without_compensation_and_resume_credits_live_cart(
    home: GrossyPaths, failure: str
) -> None:
    run_id = prepare(home)
    fake = FakeAdditiveCart(home, {"999": 8}, failure)
    assert apply_recurring(home, run_id, fake, lambda manifest: True)["status"] == "error"
    assert fake.writes == [("111", 3)]
    assert rows(home, "SELECT lifecycle FROM planner_runs") == [("partial-failure",)]
    assert len(rows(home, "SELECT observed_json FROM run_mutation_observations")) == 2
    fake.failure, fake.fail_readback = "", False
    assert (
        apply_recurring(home, run_id, fake, lambda manifest: True, resume=True)["status"]
        == "success"
    )
    expected = (
        [("111", 3), ("111", 3), ("222", 1)] if failure == "before" else [("111", 3), ("222", 1)]
    )
    assert fake.writes == expected
    assert fake.quantities["999"] == 8
    assert fake.quantities["111"] == (4 if failure == "mismatch" else 3)


def test_owner_denial_and_unexpected_cart_stop_before_write(home: GrossyPaths) -> None:
    run_id = prepare(home)
    fake = FakeAdditiveCart(home, {"999": 8})
    assert apply_recurring(home, run_id, fake, lambda manifest: False)["status"] == "cancelled"
    assert not fake.writes and not rows(home, "SELECT id FROM run_mutation_manifests")
    fake.quantities["999"] = 9
    with pytest.raises(WeeklyPlanError, match="unexpected cart"):
        apply_recurring(home, run_id, fake, lambda manifest: True)
    assert not fake.writes
    assert rows(home, "SELECT lifecycle FROM planner_runs") == [("needs-attention",)]


def test_interrupted_intent_is_not_reauthorized_and_resume_adds_only_remaining(
    home: GrossyPaths,
) -> None:
    run_id = prepare(home)
    accepted = run_recurring_preflight(home, run_id, observation({"999": 8}))
    manifest = accepted["manifest"]
    run_recurring_intent(home, run_id, manifest)
    with pytest.raises(WeeklyPlanError, match="already persisted"):
        run_recurring_intent(home, run_id, manifest)
    with pytest.raises(WeeklyPlanError, match="resume"):
        run_recurring_preflight(home, run_id, observation({"111": 2, "999": 8}))
    resumed = run_resume_prepare(home, run_id, observation({"111": 2, "999": 8}))
    # The partially landed 111 is covered by its positive credit; only 222 remains.
    assert resumed["manifest"]["additions"] == [
        {"product_id": "222", "canonical_url": url("222"), "quantity": 1, "price_eur": "2.00"}
    ]
    run_recurring_intent(home, run_id, resumed["manifest"])
    assert len(rows(home, "SELECT id FROM run_mutation_manifests")) == 2


@pytest.mark.parametrize(
    "case", ["checksum", "negative", "unknown-field", "url", "wrong-run", "no-preflight"]
)
def test_manifest_rejection_has_no_authorization(home: GrossyPaths, case: str) -> None:
    run_id = prepare(home)
    manifest = run_recurring_preflight(home, run_id, observation({"999": 8}))["manifest"]
    if case == "checksum":
        manifest["checksum"] = "0" * 64
    elif case == "negative":
        manifest["additions"][0]["quantity"] = -1
    elif case == "unknown-field":
        manifest["remove"] = ["999"]
    elif case == "url":
        manifest["additions"][0]["canonical_url"] = url("999")
    elif case == "wrong-run":
        manifest["run_id"] = "other"
    else:
        change(home, "DELETE FROM run_events WHERE event_type='recurring-preflight'")
    if case != "checksum":
        manifest["checksum"] = proposal_checksum(
            {k: v for k, v in manifest.items() if k != "checksum"}
        )
    with pytest.raises(WeeklyPlanError):
        run_recurring_intent(home, run_id, manifest)
    assert not rows(home, "SELECT id FROM run_mutation_manifests")


@pytest.mark.parametrize(
    "case",
    [
        "absent",
        "unknown",
        "unavailable",
        "hard-product",
        "hard-line",
        "stale",
        "blacklist",
        "identity",
    ],
)
def test_current_fact_policy_matrix(home: GrossyPaths, case: str) -> None:
    run_id = prepare(home)
    data = observation({"999": 8})
    fact = data["recurring_product_facts"][0]
    error = False
    if case == "absent":
        data["recurring_product_facts"] = []
    elif case in ("unknown", "unavailable"):
        fact["availability"] = case
    elif case.startswith("hard"):
        config = yaml.safe_load(home.config_file.read_text())
        config["staples"]["breakfast"][0][
            "max_product_price_eur" if case == "hard-product" else "max_line_price_eur"
        ] = 1
        home.config_file.write_text(yaml.safe_dump(config))
    elif case == "stale":
        data["generated_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        error = True
    elif case == "blacklist":
        change(
            home,
            "UPDATE products SET blacklist_state='blacklisted' WHERE retailer_product_id='111'",
        )
        error = True
    else:
        fact["canonical_url"] = url("999")
        error = True
    if error:
        with pytest.raises((ValueError, WeeklyPlanError)):
            run_recurring_preflight(home, run_id, data)
    else:
        accepted = run_recurring_preflight(home, run_id, data)
        assert all(a["product_id"] != "111" for a in accepted["manifest"]["additions"])
        assert (
            next(d for d in accepted["decisions"] if d["product_id"] == "111")["status"]
            != "passing"
        )
    assert not rows(home, "SELECT id FROM run_mutation_manifests")


def test_blacklist_change_after_preflight_and_writer_lock_reject_intent(home: GrossyPaths) -> None:
    run_id = prepare(home)
    manifest = run_recurring_preflight(home, run_id, observation({"999": 8}))["manifest"]
    with WriterLock(home.state_dir), pytest.raises(LockHeldError):
        run_recurring_intent(home, run_id, manifest)
    change(
        home, "UPDATE products SET blacklist_state='blacklisted' WHERE retailer_product_id='111'"
    )
    with pytest.raises(WeeklyPlanError, match="policy changed"):
        run_recurring_intent(home, run_id, manifest)
    assert not rows(home, "SELECT id FROM run_mutation_manifests")


def test_fr129_gate_zero_completion_and_legacy_snapshot_retry(home: GrossyPaths) -> None:
    run_id = prepare(home, {"111": 5, "222": 2, "999": 8})
    proposal = empty_recipe_options(run_id)
    with pytest.raises(WeeklyPlanError, match="recurring"):
        run_recipes_apply(home, run_id, proposal)
    fake = FakeAdditiveCart(home, {"111": 5, "222": 2, "999": 8})
    assert apply_recurring(home, run_id, fake, lambda manifest: True)["status"] == "success"
    assert not fake.writes
    from grossy.weekly.mutation import run_advance

    run_advance(home, run_id, observation(fake.quantities))
    first = run_recipes_apply(home, run_id, proposal)
    assert first["lifecycle"] == "awaiting-recipe-selection"
    # Model a persisted v9 read-only snapshot: do not invent historical completion.
    change(home, "DELETE FROM run_events WHERE event_type='recurring-complete'")
    assert run_recipes_apply(home, run_id, proposal)["recipe_slots"] == first["recipe_slots"]
    with pytest.raises(WeeklyPlanError):
        run_resume_prepare(home, run_id, observation(fake.quantities))
    assert fake.quantities == {"111": 5, "222": 2, "999": 8}


def test_cli_phases_and_observation_checksum_stop(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home.config_dir.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(home.data_dir.parent))
    monkeypatch.setenv("XDG_STATE_HOME", str(home.state_dir.parent))
    run_id = prepare(home)
    file = home.state_dir / "observation.json"
    file.write_text(json.dumps(observation({"999": 8})))
    runner = CliRunner()
    preflight = runner.invoke(app, ["weekly", "recurring-preflight", run_id, str(file)])
    assert preflight.exit_code == 0, preflight.stdout
    manifest = json.loads(preflight.stdout)["manifest"]
    file.write_text(json.dumps(manifest))
    intent = runner.invoke(app, ["weekly", "recurring-intent", run_id, str(file)])
    assert intent.exit_code == 0, intent.stdout
    data = observation({"111": 3, "999": 8})
    data["mutation_result"] = {"manifest_checksum": "0" * 64, "outcome": "readback"}
    file.write_text(json.dumps(data))
    rejected = runner.invoke(app, ["weekly", "recurring-observe", run_id, str(file)])
    assert rejected.exit_code != 0
    data["mutation_result"]["manifest_checksum"] = manifest["checksum"]
    file.write_text(json.dumps(data))
    assert runner.invoke(app, ["weekly", "recurring-observe", run_id, str(file)]).exit_code == 0
    assert rows(home, "SELECT lifecycle FROM planner_runs") == [("recurring-apply",)]


def test_resume_refuses_unrelated_change_without_removal(home: GrossyPaths) -> None:
    run_id = prepare(home)
    manifest = run_recurring_preflight(home, run_id, observation({"999": 8}))["manifest"]
    run_recurring_intent(home, run_id, manifest)
    before = rows(home, "SELECT manifest_json FROM run_mutation_manifests")
    with pytest.raises(WeeklyPlanError, match="unexplained"):
        run_resume_prepare(home, run_id, observation({"111": 3, "999": 7}))
    assert rows(home, "SELECT manifest_json FROM run_mutation_manifests") == before
    assert len(rows(home, "SELECT id FROM run_mutation_observations")) == 1


@pytest.mark.parametrize("allowed", [True, False], ids=["approved-fallback", "unapproved-blocked"])
def test_unavailable_primary_substitution_is_credited_without_promotion(
    home: GrossyPaths, allowed: bool
) -> None:
    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"] = [config["staples"]["breakfast"][0]]
    config["staples"]["breakfast"][0].update(
        allow_substitution=allowed, approved_alternative_product_ids=["333"]
    )
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = prepare(home)
    data = observation({"999": 8})
    data["recurring_product_facts"][0]["availability"] = "unavailable"
    manifest = run_recurring_preflight(home, run_id, data)["manifest"]
    assert manifest["additions"] == (
        [{"product_id": "333", "canonical_url": url("333"), "quantity": 3, "price_eur": "2.00"}]
        if allowed
        else []
    )
    run_recurring_intent(home, run_id, manifest)
    if allowed:
        readback = observation({"333": 3, "999": 8})
        readback["recurring_product_facts"][0]["availability"] = "unavailable"
        readback["mutation_result"] = {
            "manifest_checksum": manifest["checksum"],
            "outcome": "readback",
        }
        assert run_recurring_observe(home, run_id, readback)["status"] == "success"
        assert run_recurring_preflight(home, run_id, readback)["manifest"]["additions"] == []
    assert (
        yaml.safe_load(home.config_file.read_text())["staples"]["breakfast"][0]["product_id"]
        == "111"
    )
    assert rows(
        home,
        "SELECT p.retailer_product_id,i.intended_quantity FROM run_recurring_items i "
        "JOIN products p ON p.id=i.product_id",
    ) == [("111", 3)]


@pytest.mark.parametrize(
    "case",
    [
        "bool-version",
        "string-version",
        "float-price",
        "nan-price",
        "duplicate-fact",
        "unsafe-url",
        "extra-fact",
        "naive-time",
    ],
)
def test_closed_observation_and_manifest_boundary(home: GrossyPaths, case: str) -> None:
    run_id = prepare(home)
    data = observation({"999": 8})
    if case in ("bool-version", "string-version"):
        manifest = run_recurring_preflight(home, run_id, data)["manifest"]
        manifest["schema_version"] = True if case == "bool-version" else "1"
        manifest["checksum"] = proposal_checksum(
            {k: v for k, v in manifest.items() if k != "checksum"}
        )
        with pytest.raises(WeeklyPlanError):
            run_recurring_intent(home, run_id, manifest)
    else:
        fact = data["recurring_product_facts"][0]
        if case == "float-price":
            fact["displayed_price_eur"] = 2.0
        elif case == "nan-price":
            fact["displayed_price_eur"] = "NaN"
        elif case == "duplicate-fact":
            data["recurring_product_facts"].append(dict(fact))
        elif case == "unsafe-url":
            fact["canonical_url"] = "https://store.example/eshop/%3Cscript%3E/p/111"
        elif case == "extra-fact":
            fact["cookie"] = "unsafe"
        else:
            data["generated_at"] = "2026-10-03T12:00:00"
        with pytest.raises(ValueError):
            run_recurring_preflight(home, run_id, data)
    assert not rows(home, "SELECT id FROM run_mutation_manifests")


def test_confirmation_reread_change_and_report_failure_never_reach_browser_write(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = prepare(home)
    fake = FakeAdditiveCart(home, {"999": 8})

    def owner_confirms_but_cart_changes(manifest: dict[str, Any]) -> bool:
        fake.quantities["999"] = 9
        return True

    with pytest.raises(WeeklyPlanError, match="unexpected cart"):
        apply_recurring(home, run_id, fake, owner_confirms_but_cart_changes)
    assert not fake.writes
    manifest = run_recurring_preflight(home, run_id, observation({"999": 8}))["manifest"]

    def broken_report(*args: Any, **kwargs: Any) -> None:
        raise OSError("fake disk failure")

    monkeypatch.setattr("grossy.weekly.mutation.atomic_write_text", broken_report)
    with pytest.raises(OSError):
        run_recurring_intent(home, run_id, manifest)
    assert len(rows(home, "SELECT id FROM run_mutation_manifests")) == 1
    assert rows(home, "SELECT failure_class FROM run_mutation_observations") == [("total",)]
    assert rows(home, "SELECT event_type FROM run_events WHERE event_type='report-failure'")
    assert not fake.writes


def test_recurring_records_are_in_existing_private_export_inventory(home: GrossyPaths) -> None:
    from grossy.db.backup import db_export

    run_id = prepare(home)
    manifest = run_recurring_preflight(home, run_id, observation({"999": 8}))["manifest"]
    run_recurring_intent(home, run_id, manifest)
    destination = home.state_dir / "export.json"
    db_export(home.database_file, destination)
    data = json.loads(destination.read_text())
    assert data["data"]["run_mutation_manifests"][0]["checksum"] == manifest["checksum"]
    assert data["data"]["run_mutation_observations"][0]["failure_class"] == "total"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_consumed_intent_cannot_replay_and_new_preflight_invalidates_completion(
    home: GrossyPaths,
) -> None:
    run_id = prepare(home, {"111": 5, "222": 2, "999": 8})
    cart = observation({"111": 5, "222": 2, "999": 8})
    manifest = run_recurring_preflight(home, run_id, cart)["manifest"]
    run_recurring_intent(home, run_id, manifest)
    with pytest.raises(WeeklyPlanError, match="consumed"):
        run_recurring_intent(home, run_id, manifest)
    proposal = empty_recipe_options(run_id)
    # A new acceptance cannot reuse the older completion marker.
    current = observation({"111": 5, "222": 2, "999": 8})
    new = run_recurring_preflight(home, run_id, current)["manifest"]
    with pytest.raises(WeeklyPlanError, match="recurring"):
        run_recipes_apply(home, run_id, proposal)
    run_recurring_intent(home, run_id, new)
    from grossy.weekly.mutation import run_advance

    run_advance(home, run_id, observation({"111": 5, "222": 2, "999": 8}))
    assert run_recipes_apply(home, run_id, proposal)["status"] == "success"


@pytest.mark.parametrize("evidence", ["recorded-incomplete", "stale-cart-only"])
def test_resume_requires_fresh_complete_evidence_without_clearing_intent(
    home: GrossyPaths, evidence: str
) -> None:
    run_id = prepare(home)
    fake = FakeAdditiveCart(home, {"999": 8}, "readback")
    assert apply_recurring(home, run_id, fake, lambda manifest: True)["status"] == "error"
    if evidence == "recorded-incomplete":
        data = json.loads(
            rows(home, "SELECT observed_json FROM run_mutation_observations ORDER BY id")[-1][0]
        )
        assert data["mutation_result"]["outcome"] == "incomplete"
    else:
        data = observation(fake.quantities, facts=False)
        data["generated_at"] = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
    before = {
        table: rows(home, f"SELECT * FROM {table}")
        for table in ["run_mutation_observations", "run_events", "planner_runs", "weekly_reports"]
    }
    with pytest.raises(WeeklyPlanError, match="complete reread|stale"):
        run_resume_prepare(home, run_id, data)
    assert {table: rows(home, f"SELECT * FROM {table}") for table in before} == before
    with pytest.raises(WeeklyPlanError, match="resume"):
        run_recurring_preflight(home, run_id, observation(fake.quantities))
    assert fake.writes == [("111", 3)]


def test_contradictory_current_price_cannot_authorize_hard_limit_bypass(home: GrossyPaths) -> None:
    run_id = prepare(home)
    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"][0]["max_product_price_eur"] = 3
    home.config_file.write_text(yaml.safe_dump(config))
    data = observation({"111": 1, "999": 8})
    data["lines"][0]["displayed_price_eur"] = "5.00"
    with pytest.raises(ValueError, match="contradictory"):
        run_recurring_preflight(home, run_id, data)
    assert not rows(home, "SELECT id FROM run_mutation_manifests")
    assert not rows(home, "SELECT id FROM run_events WHERE event_type='recurring-preflight'")


def test_intent_expires_with_source_facts_not_acceptance_clock(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = prepare(home)
    clock = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return clock

    monkeypatch.setattr("grossy.weekly.mutation.datetime", Clock)
    data = observation({"999": 8})
    data["generated_at"] = (clock - timedelta(seconds=299)).isoformat()
    manifest = run_recurring_preflight(home, run_id, data)["manifest"]
    clock += timedelta(seconds=2)
    with pytest.raises(WeeklyPlanError, match="stale"):
        run_recurring_intent(home, run_id, manifest)
    assert not rows(home, "SELECT id FROM run_mutation_manifests")


def test_recipe_preview_credits_verified_recurring_additions_and_preserves_baseline(
    home: GrossyPaths,
) -> None:
    from grossy.recipes.proposal import RecipeIngredient
    from grossy.recipes.repository import insert_recipe_with_revision

    run_id = prepare(home, {"999": 8})
    change(
        home,
        "UPDATE products SET name='rice',package_amount=100,package_unit='g',"
        "purchase_order_count=1 WHERE retailer_product_id='111'",
    )
    conn = open_connection(home.database_file)
    try:
        insert_recipe_with_revision(
            conn,
            recipe_id="rice-dinner",
            title="Rice dinner",
            reference_url=None,
            source_name="Offline user",
            source_tier="user",
            cuisine="Greek",
            protein_class="meat",
            total_time_minutes=30,
            servings=3,
            steps=["Cook rice"],
            ingredients=[RecipeIngredient(name="rice", amount=200, unit="g")],
            appliances=[],
            variants=[],
            base_weight=1,
            is_favorite=False,
            is_regular=True,
            enabled=True,
            provenance="explicitly accepted local recipe",
            now=now(),
        )
        conn.commit()
    finally:
        conn.close()
    fake = FakeAdditiveCart(home, {"999": 8})
    assert apply_recurring(home, run_id, fake, lambda manifest: True)["status"] == "success"
    assert fake.writes == [("111", 3), ("222", 1)]
    from grossy.weekly.mutation import run_advance

    run_advance(home, run_id, observation(fake.quantities))
    proposal = empty_recipe_options(run_id)
    result = run_recipes_apply(home, run_id, proposal)
    preview = result["recipe_slots"][0]["preview"]
    ingredient = preview["ingredients"][0]
    assert Decimal(ingredient["cart_credit"]) == 200
    assert Decimal(ingredient["missing_target"]) == 0
    assert Decimal(ingredient["package_quantity"]) == 0
    assert preview["estimated_incremental_cost_eur"] == "0.00"
    report = json.loads((home.run_dir(run_id) / "report.json").read_text())
    assert report["budget"]["projected_recipe_eur"] == "0.00"
    assert report["budget"]["projected_total_eur"] == "24.00"
    assert rows(
        home,
        "SELECT p.retailer_product_id,b.quantity FROM run_cart_baselines b "
        "JOIN products p ON p.id=b.product_id",
    ) == [("999", 8)]
    assert run_recipes_apply(home, run_id, proposal)["recipe_slots"] == result["recipe_slots"]


@pytest.mark.parametrize("phase", ["preflight", "intent", "resume", "resume-pending"])
def test_changed_membership_stops_stale_run_without_rewriting_evidence(home, phase):
    run_id = prepare_due_family(home)
    before = rows(home, "SELECT * FROM run_recurring_items")
    accepted = run_recurring_preflight(home, run_id, observation({"999": 8}))
    if phase == "resume-pending":
        run_recurring_intent(home, run_id, accepted["manifest"])
    manifests = rows(home, "SELECT * FROM run_mutation_manifests")
    report_bytes = {p: p.read_bytes() for p in home.run_dir(run_id).rglob("*") if p.is_file()}
    run_product_register(home, url("444"))
    run_family_link(home, "444", "yaml-family")
    with pytest.raises(WeeklyPlanError, match="stale family policy"):
        if phase == "preflight":
            run_recurring_preflight(home, run_id, observation({"999": 8}))
        elif phase == "intent":
            run_recurring_intent(home, run_id, accepted["manifest"])
        else:
            run_resume_prepare(home, run_id, observation({"999": 8}))
    assert rows(home, "SELECT * FROM run_recurring_items") == before
    assert rows(home, "SELECT * FROM run_mutation_manifests") == manifests
    assert all(p.read_bytes() == content for p, content in report_bytes.items())


def test_registered_alternative_needs_fresh_rendered_availability(home: GrossyPaths) -> None:
    seed_yaml_family(home)
    run_set_primary(home, "yaml-family", "111")
    run_product_register(home, url("444"))
    run_family_link(home, "444", "yaml-family")
    change(
        home,
        "UPDATE product_families SET recurrence_weeks=1, recurrence_source='observed', "
        "recurrence_confidence='high', last_purchase_date='2020-01-01'",
    )
    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"] = []
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = prepare(home, {"999": 8})
    data = observation({"999": 8})
    for fact in data["recurring_product_facts"]:
        fact["availability"] = "unavailable"
    assert run_recurring_preflight(home, run_id, data)["manifest"]["additions"] == []
    data["recurring_product_facts"].append(
        {
            "product_id": "444",
            "canonical_url": url("444"),
            "availability": "available",
            "displayed_price_eur": "2.00",
        }
    )
    accepted = run_recurring_preflight(home, run_id, data)
    assert accepted["manifest"]["additions"][0]["product_id"] == "444"
    assert rows(
        home,
        "SELECT p.retailer_product_id FROM product_families f JOIN products p "
        "ON p.id=f.primary_product_id",
    ) == [("111",)]


def stale_pending_readback(
    paths: GrossyPaths, stage: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if stage == "recurring":
        run_id = prepare_due_family(paths)
        cart = observation({"999": 8})
        preflight, intent = run_recurring_preflight, run_recurring_intent
    else:
        from test_weekly_selection import observation as recipe_observation
        from test_weekly_selection import plan

        config = yaml.safe_load(paths.config_file.read_text())
        config["staples"]["breakfast"] = []
        paths.config_file.write_text(yaml.safe_dump(config))
        run_id = plan(paths, second=False)
        cart = recipe_observation()
        change(
            paths,
            "INSERT INTO product_families(family_id,name,created_at,updated_at) "
            "VALUES ('yaml-family','Fixture family','2026-10-01','2026-10-01')",
        )
        preflight, intent = run_recipe_preflight, run_recipe_intent
    manifest = preflight(paths, run_id, cart)["manifest"]
    assert len(manifest["additions"]) == 1
    assert intent(paths, run_id, manifest)["status"] == "persisted"
    run_product_register(paths, url("444"))
    run_family_link(paths, "444", "yaml-family")
    addition = manifest["additions"][0]
    cart["lines"].append(
        {
            "product_id": addition["product_id"],
            "canonical_url": addition["canonical_url"],
            "quantity": addition["quantity"],
            "displayed_price_eur": addition["price_eur"],
        }
    )
    cart["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    return run_id, manifest, cart


@pytest.mark.parametrize("stage", ["recurring", "recipe"])
@pytest.mark.parametrize("outcome", ["readback", "failed", "incomplete", "quantity-mismatch"])
def test_stale_policy_pending_observe_preserves_evidence_and_execution_denial(
    home: GrossyPaths, stage: str, outcome: str
) -> None:
    run_id, manifest, cart = stale_pending_readback(home, stage)
    observe = run_recurring_observe if stage == "recurring" else run_recipe_observe
    preflight = run_recurring_preflight if stage == "recurring" else run_recipe_preflight
    intent = run_recurring_intent if stage == "recurring" else run_recipe_intent
    snapshots = {
        table: rows(home, f"SELECT * FROM {table}")
        for table in (
            "run_mutation_manifests",
            "run_recurring_items",
            "run_cart_baselines",
            "run_recipe_plans",
            "run_recipe_selections",
            "source_orders",
            "source_order_products",
        )
    }
    marker = rows(home, "SELECT * FROM run_events WHERE event_type='maintenance-policy-changed'")
    assert len(marker) == 1
    prior_observations = rows(home, "SELECT * FROM run_mutation_observations ORDER BY id")
    prior_preflights = rows(home, "SELECT * FROM run_events WHERE event_type LIKE '%-preflight'")
    if outcome == "quantity-mismatch":
        cart["lines"][-1]["quantity"] += 1
    else:
        cart["mutation_result"]["outcome"] = outcome
    result = observe(home, run_id, cart)
    success = outcome == "readback"
    assert result["status"] == ("success" if success else "error")
    assert result["lifecycle"] == (
        ("awaiting-basket-review" if stage == "recurring" else "awaiting-user-verification")
        if success
        else "partial-failure"
    )
    observations = rows(home, "SELECT * FROM run_mutation_observations ORDER BY id")
    assert observations[:-1] == prior_observations
    assert len(observations) == len(prior_observations) + 1
    assert rows(
        home, "SELECT observed_json,failure_class FROM run_mutation_observations ORDER BY id"
    )[-1] == (json.dumps(cart), "none" if success else "partial")
    assert {table: rows(home, f"SELECT * FROM {table}") for table in snapshots} == snapshots
    for operation in (
        lambda: preflight(home, run_id, cart),
        lambda: intent(home, run_id, manifest),
        lambda: run_resume_prepare(home, run_id, observation({}, facts=False), stage=stage),
    ):
        with pytest.raises(WeeklyPlanError, match="stale family policy"):
            operation()
    assert (
        rows(home, "SELECT * FROM run_events WHERE event_type='maintenance-policy-changed'")
        == marker
    )
    assert (
        rows(home, "SELECT * FROM run_events WHERE event_type LIKE '%-preflight'")
        == prior_preflights
    )
    assert rows(home, "SELECT * FROM run_mutation_manifests") == snapshots["run_mutation_manifests"]
    if success:
        with pytest.raises(WeeklyPlanError, match="pending manifest"):
            observe(home, run_id, cart)
        assert rows(home, "SELECT * FROM run_mutation_observations ORDER BY id") == observations


@pytest.mark.parametrize("stage", ["recurring", "recipe"])
@pytest.mark.parametrize("invalid", ["checksum", "missing-result", "extra-field", "identity"])
def test_stale_policy_invalid_pending_observe_rejects_atomically(
    home: GrossyPaths, stage: str, invalid: str
) -> None:
    run_id, _, cart = stale_pending_readback(home, stage)
    observe = run_recurring_observe if stage == "recurring" else run_recipe_observe
    before = {
        table: rows(home, f"SELECT * FROM {table}")
        for table in (
            "run_mutation_observations",
            "run_mutation_manifests",
            "run_events",
            "planner_runs",
            "weekly_reports",
        )
    }
    reports = {p: p.read_bytes() for p in home.run_dir(run_id).rglob("*") if p.is_file()}
    if invalid == "checksum":
        cart["mutation_result"]["manifest_checksum"] = "0" * 64
    elif invalid == "missing-result":
        del cart["mutation_result"]
    elif invalid == "extra-field":
        cart["unexpected"] = True
    else:
        cart["lines"][-1]["canonical_url"] = url("888")
    with pytest.raises((ValueError, WeeklyPlanError)):
        observe(home, run_id, cart)
    assert {table: rows(home, f"SELECT * FROM {table}") for table in before} == before
    assert all(p.read_bytes() == content for p, content in reports.items())


def unavailable_observation(quantities: dict[str, int], pid: str = "111") -> dict[str, Any]:
    data = observation(quantities)
    fact = next(f for f in data["recurring_product_facts"] if f["product_id"] == pid)
    fact.update(availability="unavailable", displayed_price_eur=None)
    return data


@pytest.mark.parametrize("fallback", [True, False])
def test_verified_item_becomes_unavailable_other_items_finish(
    home: GrossyPaths, fallback: bool
) -> None:
    if fallback:
        config = yaml.safe_load(home.config_file.read_text())
        config["staples"]["breakfast"][0].update(
            allow_substitution=True, approved_alternative_product_ids=["333"]
        )
        home.config_file.write_text(yaml.safe_dump(config))
        run_product_register(home, url("333"))
    run_id = prepare(home, {"999": 8})
    first = run_recurring_preflight(home, run_id, observation({"999": 8}))["manifest"]
    assert first["additions"][0]["product_id"] == "111"
    run_recurring_intent(home, run_id, first)
    verified = observation({"111": 3, "999": 8})
    verified["mutation_result"] = {"manifest_checksum": first["checksum"], "outcome": "readback"}
    assert run_recurring_observe(home, run_id, verified)["status"] == "success"
    previous = rows(home, "SELECT observed_json FROM run_mutation_observations")

    quantities = {"999": 8}
    added = []
    for _ in range(4):
        data = unavailable_observation(quantities)
        prepared = run_recurring_preflight(home, run_id, data)
        candidates = prepared["product_candidates"]
        if fallback:
            assert {
                "primary_product_id": "111",
                "product_id": "333",
                "canonical_url": url("333"),
            } in candidates
        manifest = prepared["manifest"]
        run_recurring_intent(home, run_id, manifest)
        if not manifest["additions"]:
            break
        addition = manifest["additions"][0]
        added.append(addition["product_id"])
        quantities[addition["product_id"]] = addition["quantity"]
        data = unavailable_observation(quantities)
        data["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
        assert run_recurring_observe(home, run_id, data)["status"] == "success"
    else:
        pytest.fail("recurring stage did not finish")
    assert set(added) == ({"333", "222"} if fallback else {"222"})
    assert (
        rows(home, "SELECT observed_json FROM run_mutation_observations")[: len(previous)]
        == previous
    )
    report = json.loads((home.run_dir(run_id) / "report.json").read_text())
    assert report["recurring_unavailable_product_ids"] == ["111"]
    if fallback:
        assert report["recurring_substitutions"] == {"111": "333"}
    else:
        assert any(
            d["product_id"] == "111" and d["status"].startswith("skipped")
            for d in report["recurring_preflight_decisions"]
        )
    assert "unavailable" in (home.run_dir(run_id) / "report.md").read_text()


@pytest.mark.parametrize("problem", ["unknown", "stale", "incomplete", "unrelated-drift"])
def test_unavailable_does_not_waive_evidence_or_other_cart_drift(
    home: GrossyPaths, problem: str
) -> None:
    # The baseline holds 111 so its absence from the reread is drift unless forgiven.
    run_id = prepare(home, {"111": 1, "999": 8})
    data = unavailable_observation({"999": 8})
    if problem == "unknown":
        data["recurring_product_facts"][0]["availability"] = "unknown"
    elif problem == "stale":
        data["generated_at"] = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    elif problem == "incomplete":
        data["mutation_result"] = {"manifest_checksum": "a" * 64, "outcome": "incomplete"}
    else:
        data["lines"][0]["quantity"] = 7
    with pytest.raises(WeeklyPlanError):
        run_recurring_preflight(home, run_id, data)
    assert not rows(home, "SELECT id FROM run_mutation_manifests")


def test_unavailable_fact_for_non_recurring_line_is_still_drift(home: GrossyPaths) -> None:
    """Only run-known recurring identities may be forgiven; an owner/baseline line is not."""
    run_id = prepare(home, {"999": 8})
    data = observation({})
    data["recurring_product_facts"].append(
        {
            "product_id": "999",
            "canonical_url": url("999"),
            "displayed_price_eur": None,
            "availability": "unavailable",
        }
    )
    with pytest.raises(WeeklyPlanError, match="needs-attention|unexpected cart change"):
        run_recurring_preflight(home, run_id, data)


def test_preflight_accepts_page_bound_line_for_run_known_recurring_identity(
    home: GrossyPaths,
) -> None:
    """A planned recurring product first reported after baseline (its checkout row bound by
    product-page evidence in the scripts) is credited as covered, not treated as drift."""
    run_id = prepare(home, {"999": 8})
    first = run_recurring_preflight(home, run_id, observation({"999": 8}))
    assert first["remaining_addition_count"] == 2  # 111 and 222 both planned
    second = run_recurring_preflight(home, run_id, observation({"999": 8, "222": 1}))
    assert second["status"] == "accepted"
    assert {"product_id": "222", "status": "cart-covered"} in second["decisions"]
    assert [a["product_id"] for a in second["manifest"]["additions"]] == ["111"]
    assert second["remaining_addition_count"] == 1
    assert rows(home, "SELECT lifecycle FROM planner_runs") != [("needs-attention",)]
    # Every other difference is still drift: an unknown new line and a changed known quantity.
    for drift in ({"999": 8, "555": 1}, {"999": 9, "222": 1}):
        with pytest.raises(WeeklyPlanError, match="unexpected cart change"):
            run_recurring_preflight(home, run_id, observation(drift))
    assert not rows(home, "SELECT id FROM run_mutation_manifests")


def test_preflight_reports_unregistered_alternatives_as_unresolved(home: GrossyPaths) -> None:
    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"][0].update(
        allow_substitution=True, approved_alternative_product_ids=["333"]
    )
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = prepare(home, {"999": 8})
    prepared = run_recurring_preflight(home, run_id, observation({"999": 8}))
    assert {"primary_product_id": "111", "product_id": "333", "reason": "unregistered-product"} in (
        prepared["unresolved_candidates"]
    )
    assert "333" not in {c["product_id"] for c in prepared["product_candidates"]}


def test_readback_allows_unrelated_unavailable_but_not_unverified_target(home: GrossyPaths) -> None:
    run_id = prepare(home, {"111": 3, "999": 8})
    manifest = run_recurring_preflight(home, run_id, observation({"111": 3, "999": 8}))["manifest"]
    assert manifest["additions"][0]["product_id"] == "222"
    run_recurring_intent(home, run_id, manifest)
    data = unavailable_observation({"222": 1, "999": 8})
    data["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    assert run_recurring_observe(home, run_id, data)["status"] == "success"


def test_unavailable_written_target_is_not_verified(home: GrossyPaths) -> None:
    run_id = prepare(home, {"999": 8})
    manifest = run_recurring_preflight(home, run_id, observation({"999": 8}))["manifest"]
    run_recurring_intent(home, run_id, manifest)
    data = unavailable_observation({"999": 8})
    data["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    assert run_recurring_observe(home, run_id, data)["status"] == "error"


def test_resume_accepts_fresh_unavailable_without_replaying_writes(home: GrossyPaths) -> None:
    run_id = prepare(home)
    prepared = run_resume_prepare(home, run_id, unavailable_observation({"999": 8}))
    assert prepared["status"] == "accepted"
    assert prepared["manifest"]["additions"][0]["product_id"] == "222"
    assert not rows(home, "SELECT id FROM run_mutation_manifests")
