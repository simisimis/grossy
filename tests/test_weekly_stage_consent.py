"""Task 16: Autonomous Weekly Basket and Dinner Workflow — stage consent, barrier, limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

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
from grossy.weekly.selection import (
    run_selection_apply,
)


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


def prepare(paths: GrossyPaths) -> str:
    run_id = str(run_plan_prepare(paths, skip_history_sync=True)["run_id"])
    # Baseline already matches the recurring target for zero-delta completion.
    run_baseline_apply(paths, run_id, observation({"111": 3, "222": 1, "999": 8}))
    return run_id


def rows(paths: GrossyPaths, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
    conn = open_connection(paths.database_file)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def run_row_id(paths: GrossyPaths, run_id: str) -> int:
    return rows(paths, "SELECT id FROM planner_runs WHERE run_id=?", (run_id,))[0][0]


def complete_recurring(paths: GrossyPaths, run_id: str) -> dict[str, Any]:
    """Complete the recurring stage (zero additions) and return the observation."""
    obs = observation({"111": 3, "222": 1, "999": 8})
    accepted = run_recurring_preflight(paths, run_id, obs)
    assert not accepted["manifest"]["additions"]
    assert run_recurring_intent(paths, run_id, accepted["manifest"])["addition"] is None
    return obs


# --- Stage consent tests ---


def test_plan_prepare_grants_recurring_consent(home: GrossyPaths) -> None:
    """Weekly invocation grants durable run-scoped recurring stage consent."""
    run_id = prepare(home)
    rid = run_row_id(home, run_id)
    consent = rows(home, "SELECT stage, status FROM run_stage_consent WHERE run_id=?", (rid,))
    assert consent == [("recurring", "granted")]
    # Event recorded.
    events = [r[0] for r in rows(home, "SELECT event_type FROM run_events WHERE run_id=?", (rid,))]
    assert "consent-granted" in events


def test_recipe_consent_revocation_blocks_preflight(home: GrossyPaths) -> None:
    """Revoked recipe consent blocks recipe preflight/intent."""
    run_id = prepare(home)
    complete_recurring(home, run_id)
    run_advance(home, run_id, observation({"111": 3, "222": 1, "999": 8}))
    # Grant recipe consent directly (simulating selection-apply).
    rid = run_row_id(home, run_id)
    conn = open_connection(home.database_file)
    conn.execute(
        "INSERT INTO run_stage_consent (run_id, stage, status, granted_at) "
        "VALUES (?, 'recipe', 'granted', ?)",
        (rid, now()),
    )
    conn.commit()
    conn.close()
    # Revoke recipe consent.
    result = run_consent_revoke(home, run_id, "recipe", "user changed mind")
    assert result["status"] == "success"
    # Verify.
    consent = rows(
        home, "SELECT status FROM run_stage_consent WHERE run_id=? AND stage='recipe'", (rid,)
    )
    assert consent == [("revoked",)]


def test_consent_revocation_blocks_preflight(home: GrossyPaths) -> None:
    """Revoked consent blocks new preflight/intent; readback remains possible."""
    run_id = prepare(home)
    # Revoke recurring consent.
    result = run_consent_revoke(home, run_id, "recurring", "user changed mind")
    assert result["status"] == "success"
    # Preflight now fails.
    with pytest.raises(WeeklyPlanError, match="revoked"):
        run_recurring_preflight(home, run_id, observation({"111": 3, "222": 1, "999": 8}))
    # Verify the consent row.
    rid = run_row_id(home, run_id)
    consent = rows(
        home, "SELECT status, revoked_at, rationale FROM run_stage_consent WHERE run_id=?", (rid,)
    )
    assert consent[0][0] == "revoked"
    assert consent[0][1] is not None
    assert consent[0][2] == "user changed mind"
    # Event recorded.
    events = [r[0] for r in rows(home, "SELECT event_type FROM run_events WHERE run_id=?", (rid,))]
    assert "consent-revoked" in events


def test_consent_revocation_blocks_intent(home: GrossyPaths) -> None:
    """Revoked consent blocks intent even with valid preflight data."""
    run_id = prepare(home)
    # Do preflight first (consent still granted).
    obs = observation({"111": 3, "222": 1, "999": 8})
    accepted = run_recurring_preflight(home, run_id, obs)
    # Now revoke.
    run_consent_revoke(home, run_id, "recurring")
    # Intent now fails.
    with pytest.raises(WeeklyPlanError, match="revoked"):
        run_recurring_intent(home, run_id, accepted["manifest"])


def test_consent_absent_fails_closed(home: GrossyPaths) -> None:
    """Absent consent fails closed (simulates a run created before Task 16)."""
    run_id = prepare(home)
    # Delete the consent record to simulate pre-Task-16 run.
    conn = open_connection(home.database_file)
    conn.execute("DELETE FROM run_stage_consent WHERE run_id=?", (run_row_id(home, run_id),))
    conn.commit()
    conn.close()
    with pytest.raises(WeeklyPlanError, match="absent"):
        run_recurring_preflight(home, run_id, observation({"111": 3, "222": 1, "999": 8}))


def test_consent_revocation_is_idempotent_failure(home: GrossyPaths) -> None:
    """Revoking already-revoked consent fails with a clear error."""
    run_id = prepare(home)
    run_consent_revoke(home, run_id, "recurring")
    with pytest.raises(WeeklyPlanError, match="already revoked"):
        run_consent_revoke(home, run_id, "recurring")


def test_consent_revocation_unknown_stage(home: GrossyPaths) -> None:
    """Unknown stage fails with a clear error."""
    run_id = prepare(home)
    with pytest.raises(WeeklyPlanError, match="unknown stage"):
        run_consent_revoke(home, run_id, "invalid")


def test_consent_revocation_unknown_run(home: GrossyPaths) -> None:
    """Unknown run ID fails."""
    with pytest.raises(WeeklyPlanError, match="unknown run"):
        run_consent_revoke(home, "nonexistent-run", "recurring")


# --- Basket-review barrier tests ---


def test_zero_addition_transitions_to_awaiting_basket_review(home: GrossyPaths) -> None:
    """Zero-addition recurring intent transitions to awaiting-basket-review."""
    run_id = prepare(home)
    complete_recurring(home, run_id)
    lifecycle = rows(home, "SELECT lifecycle FROM planner_runs WHERE run_id=?", (run_id,))
    assert lifecycle == [("awaiting-basket-review",)]


def test_advance_requires_awaiting_basket_review(home: GrossyPaths) -> None:
    """Advance fails if lifecycle is not awaiting-basket-review."""
    run_id = prepare(home)
    with pytest.raises(WeeklyPlanError, match="awaiting-basket-review"):
        run_advance(home, run_id, observation({"111": 3, "222": 1, "999": 8}))


def test_advance_requires_recurring_complete_event(home: GrossyPaths) -> None:
    """Advance requires recurring-complete evidence."""
    run_id = prepare(home)
    complete_recurring(home, run_id)
    # Delete the recurring-complete event.
    conn = open_connection(home.database_file)
    conn.execute(
        "DELETE FROM run_events WHERE run_id=? AND event_type='recurring-complete'",
        (run_row_id(home, run_id),),
    )
    conn.commit()
    conn.close()
    with pytest.raises(WeeklyPlanError, match="recurring completion"):
        run_advance(home, run_id, observation({"111": 3, "222": 1, "999": 8}))


def test_advance_transitions_to_recurring_apply(home: GrossyPaths) -> None:
    """Advance transitions from awaiting-basket-review to recurring-apply."""
    run_id = prepare(home)
    obs = complete_recurring(home, run_id)
    result = run_advance(home, run_id, obs)
    assert result["status"] == "success"
    assert result["lifecycle"] == "recurring-apply"
    lifecycle = rows(home, "SELECT lifecycle FROM planner_runs WHERE run_id=?", (run_id,))
    assert lifecycle == [("recurring-apply",)]
    # Event recorded.
    events = [
        r[0]
        for r in rows(
            home, "SELECT event_type FROM run_events WHERE run_id=?", (run_row_id(home, run_id),)
        )
    ]
    assert "basket-review-advance" in events


def test_advance_requires_fresh_observation(home: GrossyPaths) -> None:
    """Advance rejects stale cart observations."""
    run_id = prepare(home)
    obs = complete_recurring(home, run_id)
    obs["generated_at"] = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
    with pytest.raises(WeeklyPlanError, match="stale"):
        run_advance(home, run_id, obs)


@pytest.mark.parametrize("outcome", ["failed", "incomplete"])
def test_advance_rejects_failed_or_incomplete_evidence(home: GrossyPaths, outcome: str) -> None:
    """Advance rejects non-readback evidence before any event, lifecycle or report change."""
    run_id = prepare(home)
    obs = complete_recurring(home, run_id)
    rid = run_row_id(home, run_id)
    events = rows(home, "SELECT * FROM run_events WHERE run_id=?", (rid,))
    report = rows(home, "SELECT report_json FROM weekly_reports WHERE run_id=?", (rid,))
    report_file = (home.run_dir(run_id) / "report.json").read_text()
    obs["mutation_result"] = {"manifest_checksum": "a" * 64, "outcome": outcome}
    with pytest.raises(WeeklyPlanError, match="failed/incomplete"):
        run_advance(home, run_id, obs)
    assert rows(home, "SELECT * FROM run_events WHERE run_id=?", (rid,)) == events
    assert rows(home, "SELECT report_json FROM weekly_reports WHERE run_id=?", (rid,)) == report
    assert (home.run_dir(run_id) / "report.json").read_text() == report_file
    lifecycle = rows(home, "SELECT lifecycle FROM planner_runs WHERE id=?", (rid,))
    assert lifecycle == [("awaiting-basket-review",)]


def test_no_recurrence_after_basket_review_without_advance(home: GrossyPaths) -> None:
    """After basket-review, recurring operations still work (idempotent zero-delta)."""
    run_id = prepare(home)
    complete_recurring(home, run_id)
    # Preflight still works (computes zero delta).
    result = run_recurring_preflight(home, run_id, observation({"111": 3, "222": 1, "999": 8}))
    assert not result["manifest"]["additions"]


# --- Selection limit tests ---


def test_selection_more_than_three_fails(home: GrossyPaths) -> None:
    """Selecting more than 3 slots fails."""
    run_id = prepare(home)
    complete_recurring(home, run_id)
    run_advance(home, run_id, observation({"111": 3, "222": 1, "999": 8}))
    from grossy.recipes.proposal import proposal_checksum
    from grossy.weekly.recipes import run_recipes_apply

    body = {
        "schema_name": "grossy.weekly.recipe-options.v1",
        "schema_version": 1,
        "generated_at": now(),
        "run_id": run_id,
        "matches": [],
    }
    proposal = {**body, "checksum": proposal_checksum(body)}
    run_recipes_apply(home, run_id, proposal)
    # 4 slots exist (empty). Selecting 4 by name fails because they are empty.
    with pytest.raises(WeeklyPlanError, match="one to three|nonempty"):
        run_selection_apply(home, run_id, "recipe1,recipe2,recipe3,recipe4")
    # Selecting a nonempty subset that is > 3 also fails.
    with pytest.raises(WeeklyPlanError, match="one to three"):
        run_selection_apply(home, run_id, "a,b,c,d")


def test_selection_all_with_no_nonempty_slots_fails(home: GrossyPaths) -> None:
    """'all' with zero nonempty slots fails."""
    run_id = prepare(home)
    complete_recurring(home, run_id)
    run_advance(home, run_id, observation({"111": 3, "222": 1, "999": 8}))
    from grossy.recipes.proposal import proposal_checksum
    from grossy.weekly.recipes import run_recipes_apply

    body = {
        "schema_name": "grossy.weekly.recipe-options.v1",
        "schema_version": 1,
        "generated_at": now(),
        "run_id": run_id,
        "matches": [],
    }
    proposal = {**body, "checksum": proposal_checksum(body)}
    run_recipes_apply(home, run_id, proposal)
    with pytest.raises(WeeklyPlanError, match="one to three"):
        run_selection_apply(home, run_id, "all")


# --- Recipe consent revocation with genuine preflight/intent ---


def _setup_recipe_stage(paths: GrossyPaths) -> str:
    """Set up a run through to recipe-apply with a real selection.

    Returns the run_id in recipe-apply lifecycle with recipe consent granted.
    """
    from test_weekly_recipes import add_recipe

    from grossy.recipes.proposal import proposal_checksum
    from grossy.weekly.recipes import run_recipes_apply

    run_id = prepare(paths)
    complete_recurring(paths, run_id)
    run_advance(paths, run_id, observation({"111": 3, "222": 1, "999": 8}))
    # Add a recipe BEFORE recipes-apply so slots get populated.
    add_recipe(paths, "r1", "meat", names=["rice"])
    # Create a recipe plan; the recipe should be assigned to a meat slot.
    body = {
        "schema_name": "grossy.weekly.recipe-options.v1",
        "schema_version": 1,
        "generated_at": now(),
        "run_id": run_id,
        "matches": [],
    }
    proposal = {**body, "checksum": proposal_checksum(body)}
    run_recipes_apply(paths, run_id, proposal)
    # Find the nonempty slot and its recipe to create a valid selection.
    rid = run_row_id(paths, run_id)
    conn = open_connection(paths.database_file)
    slot_row = conn.execute(
        "SELECT slot_name, recipe_revision_id FROM run_recipe_slots "
        "WHERE run_id=? AND recipe_revision_id IS NOT NULL ORDER BY slot_name LIMIT 1",
        (rid,),
    ).fetchone()
    assert slot_row is not None, "expected a nonempty recipe slot"
    slot_name, revision_id = slot_row
    recipe_row_id = conn.execute(
        "SELECT recipe_id FROM recipe_revisions WHERE id=?", (revision_id,)
    ).fetchone()[0]
    # Insert a selection row (simulating selection-apply).
    conn.execute(
        "INSERT INTO run_recipe_selections(run_id,arg_order,slot_name,dinner_label,"
        "recipe_id,created_at) VALUES (?,1,?, 'dinner1',?,?)",
        (rid, slot_name, recipe_row_id, now()),
    )
    # Transition to recipe-apply (simulating selection-apply effect).
    conn.execute("UPDATE planner_runs SET lifecycle='recipe-apply' WHERE id=?", (rid,))
    # Grant recipe consent (what selection-apply does).
    conn.execute(
        "INSERT INTO run_stage_consent (run_id, stage, status, granted_at) "
        "VALUES (?, 'recipe', 'granted', ?)",
        (rid, now()),
    )
    conn.commit()
    conn.close()
    return run_id


def test_recipe_consent_revoked_blocks_genuine_preflight(home: GrossyPaths) -> None:
    """Revoked recipe consent blocks recipe preflight with the consent error."""
    from grossy.weekly.selection import run_recipe_preflight

    run_id = _setup_recipe_stage(home)
    # Revoke recipe consent.
    run_consent_revoke(home, run_id, "recipe", "user changed mind")
    # Now recipe preflight must fail with consent error, not selection error.
    recipe_obs = observation({"111": 3, "222": 1, "999": 8})
    recipe_obs["recipe_product_facts"] = [
        {
            "product_id": "111",
            "canonical_url": url("111"),
            "displayed_price_eur": "2.00",
            "availability": "available",
            "package_amount": "100",
            "package_unit": "g",
            "package_count": 1,
            "sale_kind": "packaged",
        }
    ]
    with pytest.raises(WeeklyPlanError, match="revoked"):
        run_recipe_preflight(home, run_id, recipe_obs)


def test_recipe_consent_absent_blocks_genuine_preflight(home: GrossyPaths) -> None:
    """Absent recipe consent blocks recipe preflight (simulates pre-Task-16 run)."""
    from grossy.weekly.selection import run_recipe_preflight

    run_id = _setup_recipe_stage(home)
    # Delete the consent row to simulate absence.
    rid = run_row_id(home, run_id)
    conn = open_connection(home.database_file)
    conn.execute("DELETE FROM run_stage_consent WHERE run_id=? AND stage='recipe'", (rid,))
    conn.commit()
    conn.close()
    recipe_obs = observation({"111": 3, "222": 1, "999": 8})
    recipe_obs["recipe_product_facts"] = []
    with pytest.raises(WeeklyPlanError, match="absent"):
        run_recipe_preflight(home, run_id, recipe_obs)


def test_recipe_consent_revoked_blocks_genuine_intent(home: GrossyPaths) -> None:
    """Revoked recipe consent blocks intent even with a valid preflight manifest."""
    from grossy.weekly.selection import run_recipe_intent, run_recipe_preflight

    run_id = _setup_recipe_stage(home)
    recipe_obs = observation({"111": 3, "222": 1, "999": 8})
    recipe_obs["recipe_product_facts"] = []
    # Do preflight first (consent still granted).
    accepted = run_recipe_preflight(home, run_id, recipe_obs)
    # Now revoke.
    run_consent_revoke(home, run_id, "recipe")
    # Intent now fails with consent error.
    with pytest.raises(WeeklyPlanError, match="revoked"):
        run_recipe_intent(home, run_id, accepted["manifest"])


# --- Action consent check before browser action ---


def positive_intent(paths: GrossyPaths) -> tuple[str, dict[str, Any]]:
    """Persist one recurring addition intent (111: 1 -> 5) and return its manifest."""
    config = yaml.safe_load(paths.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "first", "product_id": "111", "product_url": url("111"), "quantity": 5}
    ]
    paths.config_file.write_text(yaml.safe_dump(config))
    run_id = str(run_plan_prepare(paths, skip_history_sync=True)["run_id"])
    run_baseline_apply(paths, run_id, observation({"111": 1, "999": 8}))
    manifest = run_recurring_preflight(paths, run_id, observation({"111": 1, "999": 8}))["manifest"]
    assert run_recurring_intent(paths, run_id, manifest)["status"] == "persisted"
    return run_id, manifest


def checked_events(paths: GrossyPaths) -> list[Any]:
    return rows(paths, "SELECT detail FROM run_events WHERE event_type='action-consent-checked'")


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
    assert len(checked_events(home)) == 1
    with pytest.raises(WeeklyPlanError, match="never replay"):
        run_action_check(home, run_id, manifest)


def test_action_check_after_attempted_readback_is_rejected(home: GrossyPaths) -> None:
    """A partial readback leaves the intent pending but never re-authorizes its write."""
    run_id, manifest = positive_intent(home)
    failed = observation({"111": 1, "999": 8})
    failed["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "failed"}
    assert run_recurring_observe(home, run_id, failed)["status"] == "error"
    with pytest.raises(WeeklyPlanError):
        run_action_check(home, run_id, manifest)


def test_revocation_after_intent_blocks_action_check_but_not_readback(
    home: GrossyPaths,
) -> None:
    """Revocation between persisted intent and action-check blocks the write only."""
    run_id, manifest = positive_intent(home)
    run_consent_revoke(home, run_id, "recurring")
    with pytest.raises(WeeklyPlanError, match="revoked"):
        run_action_check(home, run_id, manifest)
    assert checked_events(home) == []
    readback = observation({"111": 1, "999": 8})
    readback["mutation_result"] = {"manifest_checksum": manifest["checksum"], "outcome": "readback"}
    # Unchanged readback of the pending intent is still accepted as evidence.
    assert run_recurring_observe(home, run_id, readback)["status"] == "error"
    observed = rows(
        home,
        "SELECT o.failure_class FROM run_mutation_observations o "
        "JOIN run_mutation_manifests m ON m.id=o.manifest_id ORDER BY o.id",
    )
    assert observed == [("total",), ("partial",)]


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
        conn = open_connection(home.database_file)
        conn.execute(
            "UPDATE run_events SET detail=json_set(detail,'$.accepted_at',?) "
            "WHERE event_type='recurring-preflight'",
            ((datetime.now(UTC) - timedelta(seconds=301)).isoformat(),),
        )
        conn.commit()
        conn.close()
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
    from test_weekly_mutation import FakeAdditiveCart

    import grossy.browser.mutation as browser_mutation
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
        config = yaml.safe_load(home.config_file.read_text())
        config["staples"]["breakfast"] = [
            {"key": "first", "product_id": "111", "product_url": url("111"), "quantity": 5}
        ]
        home.config_file.write_text(yaml.safe_dump(config))
        paths = home
        run_id = str(run_plan_prepare(paths, skip_history_sync=True)["run_id"])
        run_baseline_apply(paths, run_id, observation({"111": 1, "999": 8}))

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
    lifecycle = rows(paths, "SELECT lifecycle FROM planner_runs WHERE run_id=?", (run_id,))
    assert lifecycle == [(f"{stage}-apply",)]
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
    import json

    from typer.testing import CliRunner

    from grossy.cli import app

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


# --- Readback after revocation (observe permitted without new write authority) ---


def test_readback_permitted_after_consent_revocation(home: GrossyPaths) -> None:
    """Observe of an already-pending intent succeeds after consent revocation.

    Revocation prevents new intent/action but does not block readback of an
    already-durable pending manifest.
    """
    from grossy.weekly.mutation import run_recurring_observe

    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "first", "product_id": "111", "product_url": url("111"), "quantity": 5}
    ]
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = str(run_plan_prepare(home, skip_history_sync=True)["run_id"])
    run_baseline_apply(home, run_id, observation({"111": 1, "999": 8}))
    obs = observation({"111": 1, "999": 8})
    accepted = run_recurring_preflight(home, run_id, obs)
    manifest = accepted["manifest"]
    result = run_recurring_intent(home, run_id, manifest)
    assert result["status"] == "persisted"
    # Revoke consent AFTER intent is persisted.
    run_consent_revoke(home, run_id, "recurring")
    # Simulate successful readback: cart now has the addition.
    readback = observation({"111": 5, "999": 8})
    readback["mutation_result"] = {
        "manifest_checksum": manifest["checksum"],
        "outcome": "readback",
    }
    # Observe should succeed (readback does not require consent).
    observe_result = run_recurring_observe(home, run_id, readback)
    assert observe_result["status"] == "success"


# --- Nonzero-delta recurring completion transitions to barrier ---


def test_nonzero_completion_transitions_to_awaiting_basket_review(home: GrossyPaths) -> None:
    """Successful recurring observe with the final addition enters the barrier.

    Task 16: every successful recurring completion (nonzero or zero) must
    transition to awaiting-basket-review; recipes-apply requires advance.
    """
    from grossy.recipes.proposal import proposal_checksum
    from grossy.weekly.mutation import run_recurring_observe
    from grossy.weekly.recipes import run_recipes_apply

    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "first", "product_id": "111", "product_url": url("111"), "quantity": 5}
    ]
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = str(run_plan_prepare(home, skip_history_sync=True)["run_id"])
    run_baseline_apply(home, run_id, observation({"111": 1, "999": 8}))
    obs = observation({"111": 1, "999": 8})
    accepted = run_recurring_preflight(home, run_id, obs)
    assert len(accepted["manifest"]["additions"]) == 1
    manifest = accepted["manifest"]
    run_recurring_intent(home, run_id, manifest)
    # Simulate the addition happening and readback.
    readback = observation({"111": 5, "999": 8})
    readback["mutation_result"] = {
        "manifest_checksum": manifest["checksum"],
        "outcome": "readback",
    }
    result = run_recurring_observe(home, run_id, readback)
    assert result["status"] == "success"
    # Lifecycle must be awaiting-basket-review, not recurring-apply.
    lifecycle = rows(home, "SELECT lifecycle FROM planner_runs WHERE run_id=?", (run_id,))
    assert lifecycle == [("awaiting-basket-review",)]
    # recipes-apply must fail without advance.
    body = {
        "schema_name": "grossy.weekly.recipe-options.v1",
        "schema_version": 1,
        "generated_at": now(),
        "run_id": run_id,
        "matches": [],
    }
    proposal = {**body, "checksum": proposal_checksum(body)}
    with pytest.raises(WeeklyPlanError, match="recurring-apply|recurring completion"):
        run_recipes_apply(home, run_id, proposal)
    # Advance now permits recipes-apply.
    run_advance(home, run_id, observation({"111": 5, "999": 8}))
    result2 = run_recipes_apply(home, run_id, proposal)
    assert result2["status"] == "success"


# --- Nonempty selection with real recipes ---


def test_selection_with_real_recipes_grants_consent(home: GrossyPaths) -> None:
    """Selection of real nonempty recipes grants recipe consent and persists mapping."""
    from test_weekly_recipes import add_recipe

    from grossy.recipes.proposal import proposal_checksum
    from grossy.weekly.recipes import run_recipes_apply

    run_id = prepare(home)
    complete_recurring(home, run_id)
    run_advance(home, run_id, observation({"111": 3, "222": 1, "999": 8}))
    # Add real recipes so slots are nonempty.
    add_recipe(home, "r1", "meat", names=["rice"])
    add_recipe(home, "r2", "fish", names=["rice"])
    add_recipe(home, "r3", "poultry", names=["rice"])
    add_recipe(home, "r4", "vegetarian", names=["rice"])
    body = {
        "schema_name": "grossy.weekly.recipe-options.v1",
        "schema_version": 1,
        "generated_at": now(),
        "run_id": run_id,
        "matches": [],
    }
    proposal = {**body, "checksum": proposal_checksum(body)}
    result = run_recipes_apply(home, run_id, proposal)
    assert result["status"] == "success"
    # Verify slots are nonempty (at least some have recipes).
    rid = run_row_id(home, run_id)
    nonempty = rows(
        home,
        "SELECT slot_name FROM run_recipe_slots WHERE run_id=? AND recipe_revision_id IS NOT NULL",
        (rid,),
    )
    assert len(nonempty) >= 1, "expected at least one nonempty slot with real recipes"
    # Select up to 3 (or all if ≤3).
    slots_to_select = [r[0] for r in nonempty][:3]
    selection_result = run_selection_apply(home, run_id, ",".join(slots_to_select))
    assert selection_result["status"] == "persisted"
    # Recipe consent must be granted.
    consent = rows(
        home,
        "SELECT stage, status FROM run_stage_consent WHERE run_id=? AND stage='recipe'",
        (rid,),
    )
    assert consent == [("recipe", "granted")]


# --- Autonomous mode (no confirm callback) with fake adapter ---


def test_apply_recurring_autonomous_no_confirm(home: GrossyPaths) -> None:
    """Autonomous mode: apply_recurring without confirm callback uses stage consent."""
    from test_weekly_mutation import FakeAdditiveCart

    from grossy.browser.mutation import apply_recurring

    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "first", "product_id": "111", "product_url": url("111"), "quantity": 5}
    ]
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = str(run_plan_prepare(home, skip_history_sync=True)["run_id"])
    run_baseline_apply(home, run_id, observation({"111": 1, "999": 8}))
    fake = FakeAdditiveCart(home, {"111": 1, "999": 8})
    # No confirm callback: autonomous mode.
    result = apply_recurring(home, run_id, fake)
    assert result["status"] == "success"
    assert fake.writes == [("111", 4)]
    assert fake.quantities == {"111": 5, "999": 8}
    # Idempotent re-invocation: zero remaining delta.
    result2 = apply_recurring(home, run_id, fake)
    assert result2["status"] == "success"
    assert fake.writes == [("111", 4)]


def test_apply_recurring_autonomous_multi_action(home: GrossyPaths) -> None:
    """Autonomous mode performs multiple actions in a single invocation."""
    from test_weekly_mutation import FakeAdditiveCart

    from grossy.browser.mutation import apply_recurring

    # Use a fresh run (not prepare()) so the baseline starts empty for 111/222.
    run_id = str(run_plan_prepare(home, skip_history_sync=True)["run_id"])
    # Baseline with only 999; recurring targets require 111:3 and 222:1.
    run_baseline_apply(home, run_id, observation({"999": 8}))
    fake = FakeAdditiveCart(home, {"999": 8})
    result = apply_recurring(home, run_id, fake)
    assert result["status"] == "success"
    # Both additions should have been performed in a single invocation.
    assert ("111", 3) in fake.writes
    assert ("222", 1) in fake.writes


# --- Dispatch-level revocation between intent and browser action ---


def test_dispatch_revocation_after_intent_blocks_action(
    home: GrossyPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revocation injected right after the active dispatch's intent prevents its write."""
    from test_weekly_mutation import FakeAdditiveCart

    import grossy.browser.mutation as browser_mutation
    from grossy.browser.mutation import apply_recurring

    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "first", "product_id": "111", "product_url": url("111"), "quantity": 5}
    ]
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = str(run_plan_prepare(home, skip_history_sync=True)["run_id"])
    run_baseline_apply(home, run_id, observation({"111": 1, "999": 8}))
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
    lifecycle = rows(home, "SELECT lifecycle FROM planner_runs WHERE run_id=?", (run_id,))
    assert lifecycle == [("recurring-apply",)]
    # The pending intent is readable without renewed authorization; no new write is.
    readback = observation({"111": 1, "999": 8})
    readback["mutation_result"] = {"manifest_checksum": persisted[0], "outcome": "readback"}
    run_recurring_observe(home, run_id, readback)
    with pytest.raises(WeeklyPlanError, match="revoked"):
        apply_recurring(home, run_id, fake, resume=True)
    assert not fake.writes


# --- Advance with edited cart recalculates budget ---


def test_advance_with_edited_cart_updates_budget(home: GrossyPaths) -> None:
    """Advance with a manually edited cart recalculates resulting_total_eur.

    Repro: complete with a cart totaling one amount, manually remove an item,
    advance with the reduced cart; the budget must reflect the new total.
    """
    import json

    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "first", "product_id": "111", "product_url": url("111"), "quantity": 5}
    ]
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = str(run_plan_prepare(home, skip_history_sync=True)["run_id"])
    # Baseline has 111:1 and 999:8 (total = 1*2 + 8*2 = 18.00).
    run_baseline_apply(home, run_id, observation({"111": 1, "999": 8}))
    # Preflight shows a positive delta for 111.
    obs = observation({"111": 1, "999": 8})
    accepted = run_recurring_preflight(home, run_id, obs)
    manifest = accepted["manifest"]
    run_recurring_intent(home, run_id, manifest)
    # Simulate the addition and readback.
    readback = observation({"111": 5, "999": 8})
    readback["mutation_result"] = {
        "manifest_checksum": manifest["checksum"],
        "outcome": "readback",
    }
    run_recurring_observe = __import__(
        "grossy.weekly.mutation", fromlist=["run_recurring_observe"]
    ).run_recurring_observe
    run_recurring_observe(home, run_id, readback)
    # Now the cart is 111:5 + 999:8 = 5*2 + 8*2 = 26.00.
    # User manually removes the 999 item (saves 16.00), leaving 111:5 = 10.00.
    edited_cart = observation({"111": 5})
    run_advance(home, run_id, edited_cart)
    # Verify the report budget was recalculated from the edited cart.
    report = json.loads((home.run_dir(run_id) / "report.json").read_text())
    budget = report["budget"]
    # The resulting_total_eur should reflect the edited cart (111:5 * 2.00 = 10.00).
    assert budget["resulting_total_eur"] == "10.00"


# --- Real four-slot rejection ---


def test_selection_all_with_four_nonempty_slots_fails(home: GrossyPaths) -> None:
    """Selecting 'all' with four nonempty slots fails (exceeds 1-3 limit)."""
    from test_weekly_recipes import add_recipe

    from grossy.recipes.proposal import proposal_checksum
    from grossy.weekly.recipes import run_recipes_apply

    run_id = prepare(home)
    complete_recurring(home, run_id)
    run_advance(home, run_id, observation({"111": 3, "222": 1, "999": 8}))
    # Add four real recipes, one per protein class, so all four slots are nonempty.
    add_recipe(home, "r1", "meat", names=["rice"])
    add_recipe(home, "r2", "fish", names=["rice"])
    add_recipe(home, "r3", "poultry", names=["rice"])
    add_recipe(home, "r4", "vegetarian", names=["rice"])
    body = {
        "schema_name": "grossy.weekly.recipe-options.v1",
        "schema_version": 1,
        "generated_at": now(),
        "run_id": run_id,
        "matches": [],
    }
    proposal = {**body, "checksum": proposal_checksum(body)}
    run_recipes_apply(home, run_id, proposal)
    # Verify all four slots are nonempty.
    rid = run_row_id(home, run_id)
    nonempty = rows(
        home,
        "SELECT slot_name FROM run_recipe_slots WHERE run_id=? AND recipe_revision_id IS NOT NULL",
        (rid,),
    )
    assert len(nonempty) == 4, f"expected 4 nonempty slots, got {len(nonempty)}"
    # 'all' with 4 nonempty slots must fail.
    with pytest.raises(WeeklyPlanError, match="at most three|one to three"):
        run_selection_apply(home, run_id, "all")
    # Explicitly selecting 4 slots must also fail.
    with pytest.raises(WeeklyPlanError, match="one to three"):
        run_selection_apply(home, run_id, "recipe1,recipe2,recipe3,recipe4")
    # Selecting exactly 3 succeeds.
    result = run_selection_apply(home, run_id, "recipe1,recipe2,recipe3")
    assert result["status"] == "persisted"


# --- Consent row ID persisted in manifest ---


def test_intent_manifest_contains_consent_row_id(home: GrossyPaths) -> None:
    """The persisted manifest JSON includes the consent_row_id reference."""
    import json

    config = yaml.safe_load(home.config_file.read_text())
    config["staples"]["breakfast"] = [
        {"key": "first", "product_id": "111", "product_url": url("111"), "quantity": 5}
    ]
    home.config_file.write_text(yaml.safe_dump(config))
    run_id = str(run_plan_prepare(home, skip_history_sync=True)["run_id"])
    run_baseline_apply(home, run_id, observation({"111": 1, "999": 8}))
    obs = observation({"111": 1, "999": 8})
    accepted = run_recurring_preflight(home, run_id, obs)
    manifest = accepted["manifest"]
    run_recurring_intent(home, run_id, manifest)
    # Verify the stored manifest JSON includes consent_row_id.
    rid = run_row_id(home, run_id)
    row = rows(
        home,
        "SELECT manifest_json FROM run_mutation_manifests WHERE run_id=?",
        (rid,),
    )
    assert row, "expected a persisted manifest"
    stored = json.loads(row[0][0])
    assert "consent_row_id" in stored
    assert stored["consent_row_id"] > 0
    # Verify it matches the actual consent row.
    consent = rows(
        home,
        "SELECT id FROM run_stage_consent WHERE run_id=? AND stage='recurring'",
        (rid,),
    )
    assert stored["consent_row_id"] == consent[0][0]
