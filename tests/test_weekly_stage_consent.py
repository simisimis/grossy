"""Task 16: Autonomous Weekly Basket and Dinner Workflow — stage consent, barrier, limits."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_weekly_mutation import FakeAdditiveCart
from test_weekly_recipes import add_recipe, proposal
from typer.testing import CliRunner

import grossy.browser.mutation as browser_mutation
from grossy.browser.mutation import apply_recurring
from grossy.cli import app
from grossy.db.connection import open_connection
from grossy.init import do_init
from grossy.paths import GrossyPaths, resolve_paths
from grossy.weekly.mutation import (
    run_action_check,
    run_advance,
    run_consent_revoke,
    run_recurring_intent,
    run_recurring_observe,
    run_recurring_preflight,
)
from grossy.weekly.plan import WeeklyPlanError, run_baseline_apply, run_plan_prepare
from grossy.weekly.recipes import run_recipes_apply
from grossy.weekly.selection import run_recipe_intent, run_recipe_preflight, run_selection_apply


def now() -> str:
    return datetime.now(UTC).isoformat()


def url(pid: str) -> str:
    return f"https://www.ab.gr/en/eshop/rice/p/{pid}"


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


COMPLETE_CART = {"111": 3, "222": 1, "999": 8}


@pytest.fixture
def home(tmp_path: Path) -> GrossyPaths:
    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    configure_staples(paths, {"111": 3, "222": 1})
    return paths


def configure_staples(paths: GrossyPaths, targets: dict[str, int]) -> None:
    config = yaml.safe_load(paths.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": f"staple-{pid}", "product_id": pid, "product_url": url(pid), "quantity": qty}
        for pid, qty in targets.items()
    ]
    paths.config_file.write_text(yaml.safe_dump(config))


def prepare(paths: GrossyPaths) -> str:
    run_id = str(run_plan_prepare(paths, skip_history_sync=True)["run_id"])
    # Baseline already matches the recurring target for zero-delta completion.
    run_baseline_apply(paths, run_id, observation(COMPLETE_CART))
    return run_id


def rows(paths: GrossyPaths, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
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


def run_row_id(paths: GrossyPaths, run_id: str) -> int:
    return rows(paths, "SELECT id FROM planner_runs WHERE run_id=?", (run_id,))[0][0]


def lifecycle(paths: GrossyPaths, run_id: str) -> str:
    return rows(paths, "SELECT lifecycle FROM planner_runs WHERE run_id=?", (run_id,))[0][0]


def events(paths: GrossyPaths, run_id: str) -> list[str]:
    return [
        r[0]
        for r in rows(
            paths, "SELECT event_type FROM run_events WHERE run_id=?", (run_row_id(paths, run_id),)
        )
    ]


def complete_recurring(paths: GrossyPaths, run_id: str) -> dict[str, Any]:
    """Complete the recurring stage (zero additions) and return the observation."""
    obs = observation(COMPLETE_CART)
    accepted = run_recurring_preflight(paths, run_id, obs)
    assert not accepted["manifest"]["additions"]
    assert run_recurring_intent(paths, run_id, accepted["manifest"])["addition"] is None
    return obs


def advanced_run(paths: GrossyPaths) -> str:
    """A run past the basket-review barrier, ready for recipe planning."""
    run_id = prepare(paths)
    complete_recurring(paths, run_id)
    run_advance(paths, run_id, observation(COMPLETE_CART))
    return run_id


def nonempty_slots(paths: GrossyPaths, run_id: str) -> list[str]:
    return [
        r[0]
        for r in rows(
            paths,
            "SELECT slot_name FROM run_recipe_slots WHERE run_id=? "
            "AND recipe_revision_id IS NOT NULL ORDER BY slot_name",
            (run_row_id(paths, run_id),),
        )
    ]


def recipe_stage_run(paths: GrossyPaths) -> str:
    """A run in recipe-apply with one real selection and granted recipe consent."""
    run_id = advanced_run(paths)
    add_recipe(paths, "r1", "meat", names=["rice"])
    run_recipes_apply(paths, run_id, proposal(run_id))
    assert nonempty_slots(paths, run_id) == ["recipe1"]
    assert run_selection_apply(paths, run_id, "recipe1")["status"] == "persisted"
    assert rows(
        paths,
        "SELECT status FROM run_stage_consent WHERE run_id=? AND stage='recipe'",
        (run_row_id(paths, run_id),),
    ) == [("granted",)]
    return run_id


def positive_run(paths: GrossyPaths) -> str:
    """A planned run whose baseline (111: 1) sits below the recurring target (111: 5)."""
    configure_staples(paths, {"111": 5})
    run_id = str(run_plan_prepare(paths, skip_history_sync=True)["run_id"])
    run_baseline_apply(paths, run_id, observation({"111": 1, "999": 8}))
    return run_id


def positive_intent(paths: GrossyPaths) -> tuple[str, dict[str, Any]]:
    """Persist one recurring addition intent (111: 1 -> 5) and return its manifest."""
    run_id = positive_run(paths)
    manifest = run_recurring_preflight(paths, run_id, observation({"111": 1, "999": 8}))["manifest"]
    assert run_recurring_intent(paths, run_id, manifest)["status"] == "persisted"
    return run_id, manifest


def readback(
    manifest: dict[str, Any], quantities: dict[str, int], outcome: str = "readback"
) -> dict[str, Any]:
    data = observation(quantities)
    data["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": outcome}
    return data


def checked_events(paths: GrossyPaths) -> list[Any]:
    return rows(paths, "SELECT detail FROM run_events WHERE event_type='action-consent-checked'")


# --- Stage consent ---


def test_plan_prepare_grants_recurring_consent(home: GrossyPaths) -> None:
    """Weekly invocation grants durable run-scoped recurring stage consent."""
    run_id = prepare(home)
    consent = rows(
        home,
        "SELECT stage, status FROM run_stage_consent WHERE run_id=?",
        (run_row_id(home, run_id),),
    )
    assert consent == [("recurring", "granted")]
    assert "consent-granted" in events(home, run_id)


@pytest.mark.parametrize("stage", ["recurring", "recipe"])
@pytest.mark.parametrize("case", ["revoked-preflight", "absent-preflight", "revoked-intent"])
def test_consent_revoked_or_absent_blocks_preflight_and_intent(
    home: GrossyPaths, stage: str, case: str
) -> None:
    """Revoked consent blocks new preflight/intent; an absent row (a run created
    before Task 16) fails closed with its own diagnostic."""
    if stage == "recurring":
        run_id = prepare(home)
        obs = observation(COMPLETE_CART)
        preflight: Callable[..., Any] = run_recurring_preflight
        intent: Callable[..., Any] = run_recurring_intent
    else:
        run_id = recipe_stage_run(home)
        obs = observation(COMPLETE_CART)
        obs["recipe_product_facts"] = []
        preflight, intent = run_recipe_preflight, run_recipe_intent
    rid = run_row_id(home, run_id)
    if case == "absent-preflight":
        execute(home, "DELETE FROM run_stage_consent WHERE run_id=? AND stage=?", (rid, stage))
        with pytest.raises(WeeklyPlanError, match="absent"):
            preflight(home, run_id, obs)
        return
    accepted = preflight(home, run_id, obs) if case == "revoked-intent" else None
    assert run_consent_revoke(home, run_id, stage, "user changed mind")["status"] == "success"
    assert rows(
        home,
        "SELECT status, revoked_at IS NOT NULL, rationale FROM run_stage_consent "
        "WHERE run_id=? AND stage=?",
        (rid, stage),
    ) == [("revoked", 1, "user changed mind")]
    assert "consent-revoked" in events(home, run_id)
    with pytest.raises(WeeklyPlanError, match="revoked"):
        if accepted is None:
            preflight(home, run_id, obs)
        else:
            intent(home, run_id, accepted["manifest"])


@pytest.mark.parametrize(
    "case,match",
    [
        ("already-revoked", "already revoked"),
        ("unknown-stage", "unknown stage"),
        ("unknown-run", "unknown run"),
    ],
)
def test_consent_revoke_rejects_invalid_requests(home: GrossyPaths, case: str, match: str) -> None:
    run_id = prepare(home)
    if case == "already-revoked":
        run_consent_revoke(home, run_id, "recurring")
    target_run = "nonexistent-run" if case == "unknown-run" else run_id
    stage = "invalid" if case == "unknown-stage" else "recurring"
    with pytest.raises(WeeklyPlanError, match=match):
        run_consent_revoke(home, target_run, stage)


# --- Basket-review barrier ---


def test_zero_addition_enters_basket_review_and_advance_moves_to_recurring_apply(
    home: GrossyPaths,
) -> None:
    run_id = prepare(home)
    obs = complete_recurring(home, run_id)
    assert lifecycle(home, run_id) == "awaiting-basket-review"
    # Recurring preflight still works at the barrier (idempotent zero delta).
    assert not run_recurring_preflight(home, run_id, observation(COMPLETE_CART))["manifest"][
        "additions"
    ]
    result = run_advance(home, run_id, obs)
    assert result["status"] == "success"
    assert result["lifecycle"] == "recurring-apply"
    assert lifecycle(home, run_id) == "recurring-apply"
    assert "basket-review-advance" in events(home, run_id)


@pytest.mark.parametrize(
    "case,match",
    [
        ("not-at-barrier", "awaiting-basket-review"),
        ("missing-completion-event", "recurring completion"),
        ("stale-observation", "stale"),
        ("failed-evidence", "failed/incomplete"),
        ("incomplete-evidence", "failed/incomplete"),
    ],
)
def test_advance_rejects_missing_or_stale_evidence_without_state_change(
    home: GrossyPaths, case: str, match: str
) -> None:
    run_id = prepare(home)
    obs = observation(COMPLETE_CART)
    if case != "not-at-barrier":
        obs = complete_recurring(home, run_id)
    rid = run_row_id(home, run_id)
    if case == "missing-completion-event":
        execute(
            home,
            "DELETE FROM run_events WHERE run_id=? AND event_type='recurring-complete'",
            (rid,),
        )
    elif case == "stale-observation":
        obs["generated_at"] = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
    elif case.endswith("-evidence"):
        obs["mutation_result"] = {"manifest_checksum": "a" * 64, "outcome": case.split("-")[0]}
    before = (
        rows(home, "SELECT * FROM run_events WHERE run_id=?", (rid,)),
        rows(home, "SELECT report_json FROM weekly_reports WHERE run_id=?", (rid,)),
        (home.run_dir(run_id) / "report.json").read_text(),
        lifecycle(home, run_id),
    )
    with pytest.raises(WeeklyPlanError, match=match):
        run_advance(home, run_id, obs)
    assert (
        rows(home, "SELECT * FROM run_events WHERE run_id=?", (rid,)),
        rows(home, "SELECT report_json FROM weekly_reports WHERE run_id=?", (rid,)),
        (home.run_dir(run_id) / "report.json").read_text(),
        lifecycle(home, run_id),
    ) == before


def test_nonzero_completion_enters_barrier_and_edited_cart_advance_recalculates_budget(
    home: GrossyPaths,
) -> None:
    """Every successful recurring completion enters awaiting-basket-review; the
    advance observation (possibly manually edited) drives the resulting budget."""
    run_id, manifest = positive_intent(home)
    assert (
        run_recurring_observe(home, run_id, readback(manifest, {"111": 5, "999": 8}))["status"]
        == "success"
    )
    assert lifecycle(home, run_id) == "awaiting-basket-review"
    with pytest.raises(WeeklyPlanError, match="recurring-apply|recurring completion"):
        run_recipes_apply(home, run_id, proposal(run_id))
    # The user removed 999 (16.00) before advancing: 111:5 * 2.00 = 10.00 remains.
    run_advance(home, run_id, observation({"111": 5}))
    report = json.loads((home.run_dir(run_id) / "report.json").read_text())
    assert report["budget"]["resulting_total_eur"] == "10.00"
    assert run_recipes_apply(home, run_id, proposal(run_id))["status"] == "success"


# --- Selection limits ---


@pytest.mark.parametrize("selection", ["recipe1,recipe2,recipe3,recipe4", "a,b,c,d", "all"])
def test_selection_with_empty_slots_or_more_than_three_fails(
    home: GrossyPaths, selection: str
) -> None:
    run_id = advanced_run(home)
    run_recipes_apply(home, run_id, proposal(run_id))
    assert nonempty_slots(home, run_id) == []
    with pytest.raises(WeeklyPlanError, match="one to three|nonempty"):
        run_selection_apply(home, run_id, selection)


def test_four_nonempty_slots_limit_selection_and_grant_recipe_consent(home: GrossyPaths) -> None:
    run_id = advanced_run(home)
    for name, protein in [("r1", "meat"), ("r2", "fish"), ("r3", "poultry"), ("r4", "vegetarian")]:
        add_recipe(home, name, protein, names=["rice"])
    assert run_recipes_apply(home, run_id, proposal(run_id))["status"] == "success"
    assert len(nonempty_slots(home, run_id)) == 4
    for selection in ("all", "recipe1,recipe2,recipe3,recipe4"):
        with pytest.raises(WeeklyPlanError, match="at most three|one to three"):
            run_selection_apply(home, run_id, selection)
    assert run_selection_apply(home, run_id, "recipe1,recipe2,recipe3")["status"] == "persisted"
    assert rows(
        home,
        "SELECT stage, status FROM run_stage_consent WHERE run_id=? AND stage='recipe'",
        (run_row_id(home, run_id),),
    ) == [("recipe", "granted")]


# --- Action consent check before browser action ---


def test_action_check_binds_pending_manifest_and_is_single_use(home: GrossyPaths) -> None:
    """The check authorizes only the exact pending intent, once, with its consent."""
    run_id, manifest = positive_intent(home)
    for changed in (
        {**manifest, "run_id": "other"},
        {**manifest, "checksum": "0" * 64},
        {**manifest, "additions": [{**manifest["additions"][0], "quantity": 9}]},
    ):
        with pytest.raises(WeeklyPlanError):
            run_action_check(home, run_id, changed)
    assert checked_events(home) == []
    result = run_action_check(home, run_id, manifest)
    rid = run_row_id(home, run_id)
    consent = rows(
        home, "SELECT id FROM run_stage_consent WHERE run_id=? AND stage='recurring'", (rid,)
    )[0][0]
    assert result["status"] == "authorized"
    assert result["addition"] == manifest["additions"][0]
    assert result["consent_row_id"] == consent
    stored = json.loads(
        rows(home, "SELECT manifest_json FROM run_mutation_manifests WHERE run_id=?", (rid,))[0][0]
    )
    assert stored["consent_row_id"] == consent
    assert len(checked_events(home)) == 1
    with pytest.raises(WeeklyPlanError, match="never replay"):
        run_action_check(home, run_id, manifest)


def test_action_check_after_attempted_readback_is_rejected(home: GrossyPaths) -> None:
    """A partial readback leaves the intent pending but never re-authorizes its write."""
    run_id, manifest = positive_intent(home)
    failed = readback(manifest, {"111": 1, "999": 8}, outcome="failed")
    assert run_recurring_observe(home, run_id, failed)["status"] == "error"
    with pytest.raises(WeeklyPlanError):
        run_action_check(home, run_id, manifest)


@pytest.mark.parametrize(
    "cart,status,failure_class",
    [({"111": 1, "999": 8}, "error", "partial"), ({"111": 5, "999": 8}, "success", "none")],
    ids=["unchanged-cart", "completed-cart"],
)
def test_revocation_after_intent_blocks_action_check_but_not_readback(
    home: GrossyPaths, cart: dict[str, int], status: str, failure_class: str
) -> None:
    """Revocation between persisted intent and action-check blocks the write only;
    readback of the already-durable pending manifest is still recorded."""
    run_id, manifest = positive_intent(home)
    run_consent_revoke(home, run_id, "recurring")
    with pytest.raises(WeeklyPlanError, match="revoked"):
        run_action_check(home, run_id, manifest)
    assert checked_events(home) == []
    assert run_recurring_observe(home, run_id, readback(manifest, cart))["status"] == status
    observed = rows(
        home,
        "SELECT o.failure_class FROM run_mutation_observations o "
        "JOIN run_mutation_manifests m ON m.id=o.manifest_id ORDER BY o.id",
    )
    assert observed == [("total",), (failure_class,)]


@pytest.mark.parametrize("case", ["policy", "expired"])
def test_action_check_rejects_policy_change_or_expired_preflight(
    home: GrossyPaths, case: str
) -> None:
    run_id, manifest = positive_intent(home)
    if case == "policy":
        config = yaml.safe_load(home.config_file.read_text())
        config["weekly_budget"] = 1
        home.config_file.write_text(yaml.safe_dump(config))
    else:
        execute(
            home,
            "UPDATE run_events SET detail=json_set(detail,'$.accepted_at',?) "
            "WHERE event_type='recurring-preflight'",
            ((datetime.now(UTC) - timedelta(seconds=301)).isoformat(),),
        )
    with pytest.raises(WeeklyPlanError, match="policy changed" if case == "policy" else "expired"):
        run_action_check(home, run_id, manifest)
    assert checked_events(home) == []


@pytest.mark.parametrize("stage", ["recurring", "recipe"])
@pytest.mark.parametrize("source_age,authorized", [(290, False), (270, True)])
def test_action_check_revalidates_source_observation_age(
    home: GrossyPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    source_age: int,
    authorized: bool,
) -> None:
    """Facts accepted near expiry go stale after intent while acceptance stays fresh."""
    import test_weekly_selection as selection_fixtures

    import grossy.weekly.mutation as weekly_mutation
    import grossy.weekly.selection as selection
    from grossy.browser.observation import CartObservation

    start = datetime.now(UTC)

    class Clock(datetime):
        offset = timedelta(0)

        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return start + cls.offset

    monkeypatch.setattr(weekly_mutation, "datetime", Clock)
    observed_at = (start - timedelta(seconds=source_age)).isoformat()

    def backdated(read: CartObservation) -> CartObservation:
        return read.model_copy(update={"generated_at": observed_at})

    if stage == "recurring":
        paths = home
        run_id = positive_run(paths)

        class StaleRecurringCart(FakeAdditiveCart):
            def read_cart(self) -> CartObservation:
                return backdated(super().read_cart())

        fake: Any = StaleRecurringCart(paths, {"111": 1, "999": 8})
        module: Any = browser_mutation
        apply: Any = browser_mutation.apply_recurring
        intent_name = "run_recurring_intent"
    else:
        paths = resolve_paths(
            config_home=tmp_path / "r" / "config",
            data_home=tmp_path / "r" / "data",
            state_home=tmp_path / "r" / "state",
        )
        do_init(paths)
        run_id = selection_fixtures.plan(paths, shared=False)

        class StaleRecipeCart(selection_fixtures.FakeCart):
            def read_cart(self) -> CartObservation:
                return backdated(super().read_cart())

        fake = StaleRecipeCart(paths)
        module = selection
        apply = browser_mutation.apply_recipes
        intent_name = "run_recipe_intent"
    real_intent = getattr(module, intent_name)
    pending: list[str] = []

    def intent_then_wait(p: GrossyPaths, rid: str, data: dict[str, Any]) -> Any:
        result = real_intent(p, rid, data)
        pending.append(result["manifest_checksum"])
        Clock.offset = timedelta(seconds=20)
        return result

    monkeypatch.setattr(module, intent_name, intent_then_wait)
    writes = fake.writes if stage == "recurring" else fake.actions
    if authorized:
        assert apply(paths, run_id, fake)["status"] == "success"
        assert len(checked_events(paths)) == len(writes) >= 1
        return
    with pytest.raises(WeeklyPlanError, match="stale"):
        apply(paths, run_id, fake)
    assert len(pending) == 1
    assert writes == []
    assert checked_events(paths) == []
    assert lifecycle(paths, run_id) == f"{stage}-apply"
    # Pending intent and its incomplete evidence remain for readback, never replay.
    evidence = rows(
        paths,
        "SELECT m.checksum, o.failure_class FROM run_mutation_manifests m "
        "JOIN run_mutation_observations o ON o.manifest_id=m.id WHERE m.stage=?",
        (stage,),
    )
    assert evidence == [(pending[0], "total")]


def test_action_check_cli_contract(home: GrossyPaths, tmp_path: Path) -> None:
    """CLI action-check emits versioned authorization, then fails closed on reuse."""
    run_id, manifest = positive_intent(home)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    runner = CliRunner()
    first = runner.invoke(app, ["weekly", "action-check", run_id, str(path)], env=env)
    assert first.exit_code == 0, first.output
    body = json.loads(first.stdout)
    assert body["schema_name"] == "grossy.weekly.action-check.v1"
    assert body["status"] == "authorized"
    assert body["manifest_checksum"] == manifest["checksum"]
    again = runner.invoke(app, ["weekly", "action-check", run_id, str(path)], env=env)
    assert again.exit_code == 1
    assert json.loads(again.stdout)["status"] == "error"


# --- Autonomous mode (no confirm callback) with fake adapter ---


def test_apply_recurring_autonomous_performs_every_action_and_repeats_idempotently(
    home: GrossyPaths,
) -> None:
    """Without a confirm callback, stage consent alone drives every recurring
    action in one invocation; re-invocation finds zero remaining delta."""
    run_id = str(run_plan_prepare(home, skip_history_sync=True)["run_id"])
    run_baseline_apply(home, run_id, observation({"999": 8}))
    fake = FakeAdditiveCart(home, {"999": 8})
    assert apply_recurring(home, run_id, fake)["status"] == "success"
    assert fake.writes == [("111", 3), ("222", 1)]
    assert fake.quantities == {"111": 3, "222": 1, "999": 8}
    assert apply_recurring(home, run_id, fake)["status"] == "success"
    assert fake.writes == [("111", 3), ("222", 1)]


def test_dispatch_revocation_after_intent_blocks_action(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revocation injected right after the active dispatch's intent prevents its write."""
    run_id = positive_run(home)
    persisted: list[str] = []

    def intent_then_revoke(paths: GrossyPaths, rid: str, data: dict[str, Any]) -> Any:
        result = run_recurring_intent(paths, rid, data)
        persisted.append(result["manifest_checksum"])
        run_consent_revoke(paths, rid, "recurring", "revoked mid-dispatch")
        return result

    monkeypatch.setattr(browser_mutation, "run_recurring_intent", intent_then_revoke)
    fake = FakeAdditiveCart(home, {"111": 1, "999": 8})
    with pytest.raises(WeeklyPlanError, match="revoked"):
        apply_recurring(home, run_id, fake)
    assert len(persisted) == 1 and not fake.writes
    assert checked_events(home) == []
    assert lifecycle(home, run_id) == "recurring-apply"
    # The pending intent is readable without renewed authorization; no new write is.
    run_recurring_observe(home, run_id, readback({"checksum": persisted[0]}, {"111": 1, "999": 8}))
    with pytest.raises(WeeklyPlanError, match="revoked"):
        apply_recurring(home, run_id, fake, resume=True)
    assert not fake.writes
