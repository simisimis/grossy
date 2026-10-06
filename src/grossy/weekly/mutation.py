"""Durable positive additions shared by recurring and selected-recipe stages.

A manifest authorizes exactly one addition. Every next addition needs another
cart read, preflight and durable intent; a pending intent is never reauthorized.
Run events store accepted preflights; existing append-only manifest/observation
tables store intent and interruption evidence without changing shipped schemas.
Recipe preflight events retain exact decimal targets and recipe contributions;
legacy REAL ingredient-target columns are not used for quantity arithmetic.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from grossy.browser.observation import CartObservation, validate_cart_observation
from grossy.config.models import GrossyConfigModel, product_id_from_url
from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import run_migrations
from grossy.fsutil import atomic_write_text, ensure_private_dir
from grossy.history.learn import select_family_product
from grossy.history.policy import product_gate, resolve_yaml_policy
from grossy.paths import GrossyPaths
from grossy.recipes.proposal import proposal_checksum
from grossy.weekly.plan import (
    BaselineLine,
    WeeklyPlanError,
    _approved_family_member_product_ids,
    _check_database_exists,
    _compute_compatible_credit,
    _load_config_strict,
    _now_utc,
)
from grossy.weekly.report import build_json_report, build_markdown_report


class Addition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product_id: str = Field(pattern=r"^[0-9]+$", max_length=64)
    canonical_url: str
    quantity: StrictInt = Field(gt=0)
    price_eur: str


class MutationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_name: Literal["grossy.cart.mutation-manifest.v1"]
    schema_version: StrictInt
    generated_at: str
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=100)
    stage: Literal["recurring", "recipe"]
    preflight_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    additions: list[Addition] = Field(max_length=1)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("schema_version")
    @classmethod
    def version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported manifest version")
        return value

    @field_validator("generated_at")
    @classmethod
    def timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must be an aware instant")
        return value


@contextmanager
def _locked(paths: GrossyPaths) -> Iterator[tuple[sqlite3.Connection, GrossyConfigModel]]:
    _check_database_exists(paths)
    config = _load_config_strict(paths)
    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            yield conn, config
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _run(conn: sqlite3.Connection, run_id: str, stage: str = "recurring") -> int:
    removal_run = conn.execute("SELECT id FROM planner_runs WHERE run_id=?", (run_id,)).fetchone()
    if removal_run:
        if _pending(conn, int(removal_run[0]), "removal"):
            raise WeeklyPlanError(
                "outstanding removal requires readback; never replay through additions"
            )
        if (
            stage == "recurring"
            and conn.execute(
                "SELECT 1 FROM run_events WHERE run_id=? AND event_type='removal-verified'",
                (removal_run[0],),
            ).fetchone()
        ):
            raise WeeklyPlanError("explicit removal cannot be restored by recurring additions")
    if stage == "recipe":
        row = conn.execute(
            "SELECT id,lifecycle FROM planner_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row or row[1] not in (
            "awaiting-recipe-selection",
            "recipe-apply",
            "awaiting-user-verification",
            "partial-failure",
            "needs-attention",
        ):
            raise WeeklyPlanError("recipe phase requires a recoverable Stage2 run")
        if not conn.execute(
            "SELECT 1 FROM run_recipe_selections WHERE run_id=?", (row[0],)
        ).fetchone():
            raise WeeklyPlanError("explicit persisted recipe selection required")
        return int(row[0])
    row = conn.execute(
        "SELECT id,lifecycle,history_sync_status FROM planner_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None or row[1] not in (
        "planning",
        "recurring-apply",
        "awaiting-basket-review",
        "partial-failure",
        "needs-attention",
    ):
        raise WeeklyPlanError("recurring phase requires a recoverable Stage1 run")
    rid = int(row[0])
    if (
        row[2] not in ("success", "skipped")
        or not conn.execute("SELECT 1 FROM weekly_reports WHERE run_id=?", (rid,)).fetchone()
    ):
        raise WeeklyPlanError("verified history and accepted baseline required")
    if conn.execute("SELECT 1 FROM run_recipe_plans WHERE run_id=?", (rid,)).fetchone():
        raise WeeklyPlanError("existing recipe snapshots cannot authorize recurring additions")
    return rid


def _require_consent(conn: sqlite3.Connection, rid: int, stage: str) -> int:
    """Check that stage consent is granted; return the consent row ID.

    Task 16: absent or revoked consent fails closed before intent/action.
    Readback (observe) of an already-pending intent does not require consent.
    """
    row = conn.execute(
        "SELECT id, status FROM run_stage_consent WHERE run_id=? AND stage=?",
        (rid, stage),
    ).fetchone()
    if row is None:
        raise WeeklyPlanError(
            f"stage consent for {stage!r} is absent; "
            "consent must be granted before any new intent or action"
        )
    if cast(str, row[1]) != "granted":
        raise WeeklyPlanError(
            f"stage consent for {stage!r} has been revoked; no new intent or action is authorized"
        )
    return cast(int, row[0])


def _event(conn: sqlite3.Connection, rid: int, kind: str, data: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO run_events(run_id,event_type,detail,created_at) VALUES (?,?,?,?)",
        (rid, kind, json.dumps(data, sort_keys=True), _now_utc()),
    )


def _latest(conn: sqlite3.Connection, rid: int, kind: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT detail FROM run_events WHERE run_id=? AND event_type=? ORDER BY id DESC LIMIT 1",
        (rid, kind),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _cart(obs: CartObservation) -> dict[str, int]:
    return {line.product_id: line.quantity for line in obs.lines}


def _cart_lines(obs: CartObservation) -> dict[str, dict[str, Any]]:
    return {line.product_id: line.model_dump(mode="json") for line in obs.lines}


def _expected_recipe_lines(
    before: CartObservation, additions: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    lines = _cart_lines(before)
    for addition in additions:
        pid = addition["product_id"]
        quantity = lines.get(pid, {}).get("quantity", 0) + addition["quantity"]
        lines[pid] = {
            "product_id": pid,
            "canonical_url": addition["canonical_url"],
            "quantity": quantity,
            "displayed_price_eur": addition["price_eur"],
        }
    return lines


def _recipe_anchor(conn: sqlite3.Connection, rid: int) -> CartObservation | None:
    # Verified observations retain full identity/price even for older cart events
    # that recorded only quantities. Never downgrade their anchor to quantities.
    row = conn.execute(
        "SELECT o.observed_json FROM run_mutation_observations o "
        "JOIN run_mutation_manifests m ON m.id=o.manifest_id "
        "WHERE m.run_id=? AND m.stage='recipe' AND o.failure_class='none' "
        "ORDER BY o.id DESC LIMIT 1",
        (rid,),
    ).fetchone()
    anchor_event = conn.execute(
        "SELECT event_type,detail FROM run_events WHERE run_id=? "
        "AND event_type IN ('removal-cart','recipe-cart') ORDER BY id DESC LIMIT 1",
        (rid,),
    ).fetchone()
    if anchor_event and anchor_event[0] == "removal-cart":
        return validate_cart_observation(json.loads(anchor_event[1])["cart"])
    if row:
        return validate_cart_observation(json.loads(row[0]))
    accepted = _latest(conn, rid, "recipe-preflight")
    return validate_cart_observation(accepted["cart"]) if accepted else None


def _baseline(conn: sqlite3.Connection, rid: int) -> dict[str, int]:
    return dict(
        conn.execute(
            "SELECT p.retailer_product_id,b.quantity FROM run_cart_baselines b "
            "JOIN products p ON p.id=b.product_id WHERE b.run_id=?",
            (rid,),
        ).fetchall()
    )


def _pending(
    conn: sqlite3.Connection, rid: int, stage: str = "recurring"
) -> sqlite3.Row | tuple[Any, ...] | None:
    return cast(
        "tuple[Any, ...] | None",
        conn.execute(
            "SELECT id,manifest_json,checksum FROM run_mutation_manifests m WHERE run_id=? "
            "AND stage=? AND NOT EXISTS (SELECT 1 FROM run_mutation_observations o "
            "WHERE o.manifest_id=m.id AND o.failure_class='none') ORDER BY id DESC LIMIT 1",
            (rid, stage),
        ).fetchone(),
    )


def _require_current_family_policy(conn: sqlite3.Connection, rid: int) -> None:
    if conn.execute(
        "SELECT 1 FROM run_events WHERE run_id=? AND event_type='maintenance-policy-changed'",
        (rid,),
    ).fetchone():
        raise WeeklyPlanError(
            "stale family policy; coordinator review and explicit reprepare decision required"
        )


def _policy(conn: sqlite3.Connection, config: GrossyConfigModel) -> str:
    # Bind both YAML and mutable blacklist/family/approval state to acceptance.
    return proposal_checksum(
        {
            "config": config.model_dump(mode="json"),
            "products": [
                list(r)
                for r in conn.execute(
                    "SELECT id,blacklist_state,typical_quantity,name,canonical_url,package_amount,"
                    "package_unit,package_count,observation_state,purchase_order_count "
                    "FROM products ORDER BY id"
                )
            ],
            "families": [
                list(r)
                for r in conn.execute(
                    "SELECT id,primary_product_id,enabled,blacklist_state "
                    "FROM product_families ORDER BY id"
                )
            ],
            "members": [
                list(r)
                for r in conn.execute(
                    "SELECT family_id,product_id,approval_state "
                    "FROM family_members ORDER BY product_id"
                )
            ],
        }
    )


def _plan(
    conn: sqlite3.Connection, rid: int, config: GrossyConfigModel, obs: CartObservation
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if resolve_yaml_policy(config, conn).has_conflicts:
        raise WeeklyPlanError("YAML blacklist conflict blocks recurring preflight")
    baseline = {
        line.product_id: BaselineLine(line.product_id, line.canonical_url, line.quantity, None)
        for line in obs.lines
    }
    facts = {fact.product_id: fact for fact in obs.recurring_product_facts}
    additions: list[dict[str, Any]] = []
    decisions: list[dict[str, str]] = []
    staples = list(config.staples.breakfast) + list(config.staples.lunch)
    for pid, family, target, source, status in conn.execute(
        "SELECT p.retailer_product_id,f.family_id,i.intended_quantity,i.source_type,i.status "
        "FROM run_recurring_items i JOIN products p ON p.id=i.product_id "
        "LEFT JOIN product_families f ON f.id=i.family_id WHERE i.run_id=? "
        "ORDER BY CASE i.category WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 ELSE 2 END,"
        "i.category,p.name,p.retailer_product_id",
        (rid,),
    ):
        if source == "learned" and status != "pending":
            decisions.append({"product_id": pid, "status": "skipped-preview"})
            continue
        gate = product_gate(conn, pid)
        if gate.is_blocked or (source == "learned" and gate.is_disabled):
            decisions.append({"product_id": pid, "status": "blocked-policy"})
            continue
        if source == "learned":
            # Resolve policy preference as baseline planning does, independently
            # of live availability; approved live fallback is evaluated below.
            preferred = (
                select_family_product(
                    conn, family, _approved_family_member_product_ids(conn, family)
                )
                if family is not None
                else None
            )
            if preferred is None or preferred.retailer_product_id != pid:
                raise WeeklyPlanError("stale recurring learned target; renewed planning required")
        matches = [
            s
            for s in staples
            if (s.product_id or product_id_from_url(s.product_url or "")) == pid
            or (
                family is not None
                and product_gate(
                    conn, s.product_id or product_id_from_url(s.product_url or "") or ""
                ).family_id
                == family
            )
        ]
        # Baseline consolidation keeps the first YAML reference's exact product
        # and quantity; later references still constrain substitution/hard limits.
        if source in ("yaml", "both") and (
            not matches
            or (matches[0].product_id or product_id_from_url(matches[0].product_url or "")) != pid
            or matches[0].quantity != target
        ):
            raise WeeklyPlanError("stale recurring YAML target; renewed planning required")
        credit = _compute_compatible_credit(conn, pid, family, target, baseline)
        approved_yaml: set[str] = set()
        if matches and all(s.allow_substitution for s in matches):
            approved_yaml = set.intersection(
                *(set(s.approved_alternative_product_ids) for s in matches)
            )
        family_credit_ids = (
            set(_approved_family_member_product_ids(conn, family)) if family else set()
        )
        extra_credit_ids = approved_yaml - family_credit_ids - {pid}
        credit = min(
            target,
            credit
            + sum(
                baseline[p].quantity
                for p in extra_credit_ids
                if p in baseline and not product_gate(conn, p).is_blocked
            ),
        )
        delta = max(0, target - credit)
        if not delta:
            decisions.append({"product_id": pid, "status": "cart-covered"})
            continue
        selected = pid
        fact = facts.get(pid)
        if fact is not None and fact.availability == "unavailable":
            allowed: set[str] = set()
            if matches and all(s.allow_substitution for s in matches):
                allowed = set.intersection(
                    *(set(s.approved_alternative_product_ids) for s in matches)
                )
            elif source == "learned" and family:
                allowed = set(_approved_family_member_product_ids(conn, family)) - {pid}
            selected = next(
                (
                    p
                    for p in sorted(allowed)
                    if p in facts
                    and facts[p].availability == "available"
                    and not product_gate(conn, p).is_blocked
                ),
                "",
            )
            fact = facts.get(selected)
        if fact is None or fact.availability != "available":
            decisions.append({"product_id": pid, "status": "skipped-unknown-or-unavailable"})
            continue
        price = Decimal(fact.displayed_price_eur)
        if any(
            (s.max_product_price_eur is not None and price > s.max_product_price_eur)
            or (s.max_line_price_eur is not None and price * target > s.max_line_price_eur)
            for s in matches
        ):
            decisions.append({"product_id": pid, "status": "blocked-hard-limit"})
            continue
        # Previously blocked preview facts can be corrected by current facts,
        # but never by guessing unavailable/unknown identity or price.
        additions.append(
            {
                "product_id": selected,
                "canonical_url": fact.canonical_url,
                "quantity": delta,
                "price_eur": fact.displayed_price_eur,
            }
        )
        decisions.append(
            {"product_id": pid, "status": "substituted" if selected != pid else "passing"}
        )
    if len({a["product_id"] for a in additions}) != len(additions):
        raise WeeklyPlanError("ambiguous overlapping recurring substitutions")
    return additions, decisions


def _reports(
    conn: sqlite3.Connection,
    paths: GrossyPaths,
    rid: int,
    run_id: str,
    lifecycle: str,
    evidence: dict[str, Any],
    *,
    stage: str = "recurring",
) -> None:
    row = conn.execute("SELECT report_json FROM weekly_reports WHERE run_id=?", (rid,)).fetchone()
    data = json.loads(row[0])
    data["lifecycle"] = lifecycle
    data.setdefault(f"{stage}_mutation_evidence", []).append(evidence)
    verified = Decimal(0)
    for raw, discrepancy in conn.execute(
        "SELECT m.manifest_json,o.discrepancies_json FROM run_mutation_manifests m "
        "JOIN run_mutation_observations o ON o.manifest_id=m.id "
        "WHERE m.run_id=? AND m.stage=? AND o.failure_class='none'",
        (rid, stage),
    ):
        manifest = json.loads(raw)
        detail = json.loads(discrepancy or "{}")
        for addition in manifest["additions"]:
            quantity = detail.get("verified_quantity", addition["quantity"])
            verified += Decimal(addition["price_eur"]) * quantity
    data["budget"][f"verified_{stage}_eur"] = f"{verified:.2f}"
    observed = evidence.get("observation")
    if observed and observed.get("mutation_result", {}).get("outcome") != "incomplete":
        total = sum(
            Decimal(line["displayed_price_eur"]) * line["quantity"] for line in observed["lines"]
        )
        remaining = Decimal(data["budget"][f"projected_{stage}_eur"])
        if evidence["status"] == "verified":
            last = json.loads(
                conn.execute(
                    "SELECT manifest_json FROM run_mutation_manifests "
                    "WHERE run_id=? ORDER BY id DESC LIMIT 1",
                    (rid,),
                ).fetchone()[0]
            )
            remaining = max(
                Decimal(0),
                remaining - sum(Decimal(a["price_eur"]) * a["quantity"] for a in last["additions"]),
            )
        data["budget"].update(
            resulting_total_eur=f"{total:.2f}",
            **{f"projected_{stage}_eur": f"{remaining:.2f}"},
            projected_total_eur=f"{total + remaining:.2f}",
            variance_eur=f"{total + remaining - Decimal(data['budget']['weekly_budget_eur']):.2f}",
        )
    if (
        stage == "recipe"
        and observed
        and observed.get("mutation_result", {}).get("outcome") != "incomplete"
    ):
        data["budget"]["known_projected_recipe_eur"] = data["budget"]["projected_recipe_eur"]
    from grossy.weekly.plan import _get_source_order_ids
    from grossy.weekly.recipes import _sanitize_tree

    data = _sanitize_tree(data, _get_source_order_ids(conn))
    md = build_markdown_report(data)
    conn.execute(
        "UPDATE weekly_reports SET report_json=?,report_md=?,report_written=0 WHERE run_id=?",
        (json.dumps(data), md, rid),
    )
    conn.commit()  # SQLite truth survives report I/O failure.
    directory = paths.runs_dir / run_id
    ensure_private_dir(directory)
    try:
        atomic_write_text(directory / "report.json", build_json_report(data))
        atomic_write_text(directory / "report.md", md)
    except OSError:
        _event(conn, rid, "report-failure", {"status": f"{stage}-report-recovery-required"})
        conn.commit()
        raise
    conn.execute("UPDATE weekly_reports SET report_written=1 WHERE run_id=?", (rid,))


def _require_fresh_observation(obs: CartObservation) -> None:
    timestamp = datetime.fromisoformat(obs.generated_at.replace("Z", "+00:00"))
    age = (datetime.now(UTC) - timestamp).total_seconds()
    if age < -30 or age > 300:
        raise WeeklyPlanError("current cart/product facts are stale; reread before authorization")


def _prepare(
    conn: sqlite3.Connection,
    config: GrossyConfigModel,
    paths: GrossyPaths,
    rid: int,
    run_id: str,
    obs: CartObservation,
    stage: str = "recurring",
) -> dict[str, Any]:
    if stage == "recipe" or obs.recurring_product_facts:
        _require_fresh_observation(obs)
    if stage == "recipe":
        from grossy.weekly.selection import recipe_plan

        additions, decisions = recipe_plan(conn, rid, config, obs)
    else:
        additions, decisions = _plan(conn, rid, config, obs)
    preflight = {
        "cart": obs.model_dump(mode="json"),
        "policy_checksum": _policy(conn, config),
        "additions": additions,
        "decisions": decisions,
    }
    current_total = sum(Decimal(line.displayed_price_eur) * line.quantity for line in obs.lines)
    projected = sum(Decimal(a["price_eur"]) * a["quantity"] for a in additions)
    report = json.loads(
        conn.execute("SELECT report_json FROM weekly_reports WHERE run_id=?", (rid,)).fetchone()[0]
    )
    report["budget"].update(
        **{f"projected_{stage}_eur": f"{projected:.2f}"},
        resulting_total_eur=f"{current_total:.2f}",
        projected_total_eur=f"{current_total + projected:.2f}",
        variance_eur=f"{current_total + projected - config.weekly_budget:.2f}",
    )
    if stage == "recipe":
        report["budget"].update(
            known_projected_recipe_eur=f"{projected:.2f}", recipe_cost_complete=True
        )
        report["recipe_pantry_assumptions"] = config.pantry_assumptions
        report["recipe_preflight_decisions"] = decisions
        report["budget"]["assumptions"] = [
            "selected passing recipes only; compatible cart credit used once; "
            "positive additions only",
            "whole-cart budget warning does not override hard limits",
        ]
    conn.execute(
        "UPDATE weekly_reports SET report_json=? WHERE run_id=?", (json.dumps(report), rid)
    )
    checksum = proposal_checksum(preflight)
    _event(
        conn,
        rid,
        f"{stage}-preflight",
        {**preflight, "checksum": checksum, "accepted_at": _now_utc()},
    )
    body = {
        "schema_name": "grossy.cart.mutation-manifest.v1",
        "schema_version": 1,
        "generated_at": _now_utc(),
        "run_id": run_id,
        "stage": stage,
        "preflight_checksum": checksum,
        "additions": additions[:1],
    }
    manifest = {**body, "checksum": proposal_checksum(body)}
    lifecycle = conn.execute("SELECT lifecycle FROM planner_runs WHERE id=?", (rid,)).fetchone()[0]
    _reports(
        conn,
        paths,
        rid,
        run_id,
        lifecycle,
        {"status": "preflight", "decisions": decisions},
        stage=stage,
    )
    return {
        "schema_name": f"grossy.weekly.{stage}-preflight.v1",
        "schema_version": 1,
        "status": "accepted",
        "manifest": manifest,
        "decisions": decisions,
        "remaining_addition_count": len(additions),
    }


def run_recurring_preflight(
    paths: GrossyPaths, run_id: str, data: dict[str, Any], *, stage: str = "recurring"
) -> dict[str, Any]:
    obs = validate_cart_observation(data)
    with _locked(paths) as (conn, config):
        rid = _run(conn, run_id, stage)
        _require_consent(conn, rid, stage)
        _require_current_family_policy(conn, rid)
        if _pending(conn, rid, stage):
            raise WeeklyPlanError("unreconciled intent requires resume-prepare")
        expected = _latest(conn, rid, f"{stage}-cart")
        unexpected = (stage == "recurring" or expected) and _cart(obs) != (
            expected["quantities"] if expected else _baseline(conn, rid)
        )
        if stage == "recipe":
            anchor = _recipe_anchor(conn, rid)
            unexpected = anchor is not None and _cart_lines(obs) != _cart_lines(anchor)
        if unexpected:
            conn.execute("UPDATE planner_runs SET lifecycle='needs-attention' WHERE id=?", (rid,))
            _event(conn, rid, f"{stage}-unexpected-cart", {"quantities": _cart(obs), "cart": data})
            _reports(
                conn,
                paths,
                rid,
                run_id,
                "needs-attention",
                {"status": "unexpected-cart"},
                stage=stage,
            )
            raise WeeklyPlanError("unexpected cart change; no addition authorized")
        return _prepare(conn, config, paths, rid, run_id, obs, stage)


def run_recurring_intent(
    paths: GrossyPaths, run_id: str, data: dict[str, Any], *, stage: str = "recurring"
) -> dict[str, Any]:
    try:
        manifest = MutationManifest.model_validate(data)
    except ValueError as exc:
        raise WeeklyPlanError(f"invalid {stage} mutation manifest") from exc
    if (
        manifest.stage != stage
        or manifest.run_id != run_id
        or proposal_checksum({k: v for k, v in data.items() if k != "checksum"})
        != manifest.checksum
    ):
        raise WeeklyPlanError("manifest checksum or run mismatch")
    with _locked(paths) as (conn, config):
        rid = _run(conn, run_id, stage)
        consent_row_id = _require_consent(conn, rid, stage)
        _require_current_family_policy(conn, rid)
        if _pending(conn, rid, stage):
            raise WeeklyPlanError("intent already persisted; reread via resume-prepare")
        accepted = _latest(conn, rid, f"{stage}-preflight")
        if not accepted or accepted["checksum"] != manifest.preflight_checksum:
            raise WeeklyPlanError("accepted preflight required")
        if (
            stage == "recipe"
            and conn.execute(
                "SELECT 1 FROM run_events WHERE run_id=? AND event_type='recipe-unexpected-cart' "
                "AND id > (SELECT MAX(id) FROM run_events WHERE run_id=? "
                "AND event_type='recipe-preflight')",
                (rid, rid),
            ).fetchone()
        ):
            raise WeeklyPlanError("unexpected cart change requires reconciliation before intent")
        if conn.execute(
            "SELECT 1 FROM run_mutation_manifests WHERE run_id=? AND stage=? "
            "AND json_extract(manifest_json,'$.preflight_checksum')=?",
            (rid, stage, manifest.preflight_checksum),
        ).fetchone():
            raise WeeklyPlanError("preflight already consumed; reread before another intent")
        accepted_age = (
            datetime.now(UTC)
            - datetime.fromisoformat(accepted["accepted_at"].replace("Z", "+00:00"))
        ).total_seconds()
        if accepted_age > 300:
            raise WeeklyPlanError("accepted preflight expired; reread current cart/facts")
        _require_fresh_observation(validate_cart_observation(accepted["cart"]))
        if accepted["policy_checksum"] != _policy(conn, config):
            raise WeeklyPlanError("policy changed since preflight")
        if stage == "recipe":
            from grossy.weekly.selection import recipe_plan

            additions, decisions = recipe_plan(
                conn, rid, config, validate_cart_observation(accepted["cart"])
            )
            if additions != accepted["additions"] or decisions != accepted["decisions"]:
                raise WeeklyPlanError("current recipe matching changed since preflight")
        if data["additions"] != accepted["additions"][:1]:
            raise WeeklyPlanError("manifest must equal accepted positive delta")
        # Task 16: persist the checked consent reference with the intent evidence.
        stored_manifest = {**data, "consent_row_id": consent_row_id}
        cursor = conn.execute(
            "INSERT INTO run_mutation_manifests(run_id,stage,manifest_json,checksum,created_at) "
            "VALUES (?,?,?,?,?)",
            (rid, stage, json.dumps(stored_manifest), manifest.checksum, _now_utc()),
        )
        mid = cursor.lastrowid
        # Durable incomplete evidence exists even if process/browser dies before readback.
        conn.execute(
            "INSERT INTO run_mutation_observations("
            "manifest_id,observed_json,discrepancies_json,"
            "failure_class,created_at) VALUES (?,?,?,'total',?)",
            (
                mid,
                json.dumps(accepted["cart"]),
                json.dumps({"status": "intent-persisted-readback-incomplete"}),
                _now_utc(),
            ),
        )
        lifecycle = f"{stage}-apply"
        conn.execute("UPDATE planner_runs SET lifecycle=? WHERE id=?", (lifecycle, rid))
        if not manifest.additions:
            if stage == "recipe":
                _event(
                    conn,
                    rid,
                    "recipe-cart",
                    {
                        "quantities": _cart(validate_cart_observation(accepted["cart"])),
                        "cart": accepted["cart"],
                    },
                )
            _event(conn, rid, f"{stage}-complete", {"status": "zero-additions"})
            if stage == "recipe":
                lifecycle = "awaiting-user-verification"
                conn.execute("UPDATE planner_runs SET lifecycle=? WHERE id=?", (lifecycle, rid))
            elif stage == "recurring":
                # Task 16: recurring completion stops at the basket-review barrier.
                lifecycle = "awaiting-basket-review"
                conn.execute("UPDATE planner_runs SET lifecycle=? WHERE id=?", (lifecycle, rid))
            conn.execute(
                "INSERT INTO run_mutation_observations(manifest_id,observed_json,"
                "failure_class,created_at) VALUES (?,?,'none',?)",
                (mid, json.dumps(accepted["cart"]), _now_utc()),
            )
        conn.commit()
        _reports(
            conn,
            paths,
            rid,
            run_id,
            lifecycle,
            {
                "manifest": data,
                "status": "intent-persisted",
                "before_observation": accepted["cart"],
                "attempt_status": "authorized-readback-incomplete",
            },
            stage=stage,
        )
        return {
            "schema_name": f"grossy.weekly.{stage}-authorization.v1",
            "schema_version": 1,
            "status": "persisted",
            "manifest_checksum": manifest.checksum,
            "addition": data["additions"][0] if manifest.additions else None,
        }


def run_action_check(paths: GrossyPaths, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Single-use consent check immediately before the one browser write.

    Task 16: binds the exact pending manifest, its run/stage and the consent row
    referenced at intent. Revocation after intent blocks the action; readback of
    that pending intent stays possible through observe/resume. The check never
    reauthorizes an attempted, consumed or zero-addition intent.
    """
    try:
        manifest = MutationManifest.model_validate(data)
    except ValueError as exc:
        raise WeeklyPlanError("invalid mutation manifest for action check") from exc
    if (
        manifest.run_id != run_id
        or proposal_checksum({k: v for k, v in data.items() if k != "checksum"})
        != manifest.checksum
    ):
        raise WeeklyPlanError("manifest checksum or run mismatch")
    stage = manifest.stage
    if not manifest.additions:
        raise WeeklyPlanError("zero-addition intent authorizes no browser action")
    with _locked(paths) as (conn, config):
        rid = _run(conn, run_id, stage)
        pending = _pending(conn, rid, stage)
        if not pending or pending[2] != manifest.checksum:
            raise WeeklyPlanError("action check requires the exact pending manifest")
        stored = json.loads(pending[1])
        consent_row_id = stored.pop("consent_row_id", None)
        if stored != data:
            raise WeeklyPlanError("action check requires the exact pending manifest")
        lifecycle = conn.execute("SELECT lifecycle FROM planner_runs WHERE id=?", (rid,)).fetchone()
        if lifecycle[0] != f"{stage}-apply":
            raise WeeklyPlanError(f"action check requires lifecycle '{stage}-apply'")
        observations = conn.execute(
            "SELECT COUNT(*) FROM run_mutation_observations WHERE manifest_id=?", (pending[0],)
        ).fetchone()[0]
        if (
            observations != 1
            or conn.execute(
                "SELECT 1 FROM run_events WHERE run_id=? AND event_type='action-consent-checked' "
                "AND json_extract(detail,'$.manifest_checksum')=?",
                (rid, manifest.checksum),
            ).fetchone()
        ):
            raise WeeklyPlanError(
                "pending intent already checked or attempted; read back via observe/resume, "
                "never replay"
            )
        current_consent = _require_consent(conn, rid, stage)
        if consent_row_id != current_consent:
            raise WeeklyPlanError("pending intent does not reference the current stage consent")
        _require_current_family_policy(conn, rid)
        accepted = _latest(conn, rid, f"{stage}-preflight")
        if not accepted or accepted["checksum"] != manifest.preflight_checksum:
            raise WeeklyPlanError("pending intent no longer matches the accepted preflight")
        accepted_age = (
            datetime.now(UTC)
            - datetime.fromisoformat(accepted["accepted_at"].replace("Z", "+00:00"))
        ).total_seconds()
        if accepted_age > 300:
            raise WeeklyPlanError("accepted preflight expired; read back via resume-prepare")
        # Source facts age independently of acceptance; both must be fresh at action time.
        _require_fresh_observation(validate_cart_observation(accepted["cart"]))
        if accepted["policy_checksum"] != _policy(conn, config):
            raise WeeklyPlanError("policy changed since preflight")
        _event(
            conn,
            rid,
            "action-consent-checked",
            {
                "stage": stage,
                "consent_row_id": current_consent,
                "manifest_checksum": manifest.checksum,
            },
        )
        return {
            "schema_name": "grossy.weekly.action-check.v1",
            "schema_version": 1,
            "status": "authorized",
            "stage": stage,
            "manifest_checksum": manifest.checksum,
            "consent_row_id": current_consent,
            "addition": data["additions"][0],
        }


def run_recurring_observe(
    paths: GrossyPaths, run_id: str, data: dict[str, Any], *, stage: str = "recurring"
) -> dict[str, Any]:
    obs = validate_cart_observation(data)
    with _locked(paths) as (conn, config):
        rid = _run(conn, run_id, stage)
        # Policy drift blocks execution, not readback of an already durable intent.
        # The pending-manifest binding below remains required; no new intent is issued.
        pending = _pending(conn, rid, stage)
        if (
            not pending
            or obs.mutation_result is None
            or obs.mutation_result.manifest_checksum != pending[2]
        ):
            raise WeeklyPlanError("observation must identify the pending manifest")
        manifest = json.loads(pending[1])
        accepted = _latest(conn, rid, f"{stage}-preflight")
        assert accepted is not None
        before_observation = validate_cart_observation(accepted["cart"])
        before = _cart(before_observation)
        expected = dict(before)
        for addition in manifest["additions"]:
            pid = addition["product_id"]
            expected[pid] = expected.get(pid, 0) + addition["quantity"]
        expected_lines = _expected_recipe_lines(before_observation, manifest["additions"])
        success = (
            obs.mutation_result.outcome == "readback"
            and _cart(obs) == expected
            and (stage != "recipe" or _cart_lines(obs) == expected_lines)
        )
        conn.execute(
            "INSERT INTO run_mutation_observations("
            "manifest_id,observed_json,discrepancies_json,"
            "failure_class,created_at) VALUES (?,?,?,?,?)",
            (
                pending[0],
                json.dumps(data),
                json.dumps(
                    {
                        "expected": expected,
                        "observed": _cart(obs),
                        "outcome": obs.mutation_result.outcome,
                        **(
                            {"expected_lines": expected_lines, "observed_lines": _cart_lines(obs)}
                            if stage == "recipe"
                            else {}
                        ),
                    }
                ),
                "none" if success else "partial",
                _now_utc(),
            ),
        )
        lifecycle = f"{stage}-apply" if success else "partial-failure"
        if success and stage == "recipe" and len(accepted["additions"]) <= 1:
            lifecycle = "awaiting-user-verification"
        if success and stage == "recurring" and len(accepted["additions"]) <= 1:
            lifecycle = "awaiting-basket-review"
        conn.execute("UPDATE planner_runs SET lifecycle=? WHERE id=?", (lifecycle, rid))
        if success:
            _event(conn, rid, f"{stage}-cart", {"quantities": _cart(obs), "cart": data})
            if len(accepted["additions"]) <= 1:
                _event(conn, rid, f"{stage}-complete", {"status": "readback-verified"})
        _reports(
            conn,
            paths,
            rid,
            run_id,
            lifecycle,
            {"status": "verified" if success else "partial-failure", "observation": data},
            stage=stage,
        )
        return {
            "schema_name": f"grossy.weekly.{stage}-observe.v1",
            "schema_version": 1,
            "status": "success" if success else "error",
            "lifecycle": lifecycle,
        }


def run_resume_prepare(
    paths: GrossyPaths, run_id: str, data: dict[str, Any], *, stage: str = "recurring"
) -> dict[str, Any]:
    if stage == "recurring":
        _check_database_exists(paths)
        conn = open_connection(paths.database_file)
        try:
            selected = conn.execute(
                "SELECT 1 FROM run_recipe_selections s JOIN planner_runs r ON r.id=s.run_id "
                "WHERE r.run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
        if selected:
            stage = "recipe"
    obs = validate_cart_observation(data)
    if obs.mutation_result is not None and obs.mutation_result.outcome != "readback":
        raise WeeklyPlanError(
            "resume requires complete reread evidence, not failed/incomplete state"
        )
    _require_fresh_observation(obs)
    with _locked(paths) as (conn, config):
        rid = _run(conn, run_id, stage)
        _require_consent(conn, rid, stage)
        _require_current_family_policy(conn, rid)
        pending = _pending(conn, rid, stage)
        if pending:
            accepted = _latest(conn, rid, f"{stage}-preflight")
            assert accepted is not None
            before = _cart(validate_cart_observation(accepted["cart"]))
            manifest = json.loads(pending[1])
            allowed = {a["product_id"] for a in manifest["additions"]}
            current = _cart(obs)
            if stage == "recipe":
                before_obs = validate_cart_observation(accepted["cart"])
                lines = _expected_recipe_lines(before_obs, manifest["additions"])
                observed_lines = _cart_lines(obs)
                if any(
                    pid not in lines
                    or any(
                        line[key] != lines[pid][key]
                        for key in ("canonical_url", "displayed_price_eur")
                    )
                    for pid, line in observed_lines.items()
                ):
                    raise WeeklyPlanError("unexplained cart identity/price change on resume")
            if any(
                current.get(p, 0) != before.get(p, 0)
                for p in set(current) | set(before)
                if p not in allowed
            ) or any(current.get(p, 0) < q for p, q in before.items()):
                raise WeeklyPlanError("unexplained cart change on resume; no additions authorized")
            conn.execute(
                "INSERT INTO run_mutation_observations("
                "manifest_id,observed_json,discrepancies_json,"
                "failure_class,created_at) VALUES (?, ?, ?, 'none', ?)",
                (
                    pending[0],
                    json.dumps(data),
                    json.dumps(
                        {
                            "status": "resume-reconciled-excess-preserved",
                            "verified_quantity": min(
                                manifest["additions"][0]["quantity"],
                                max(
                                    0,
                                    current.get(manifest["additions"][0]["product_id"], 0)
                                    - before.get(manifest["additions"][0]["product_id"], 0),
                                ),
                            ),
                        }
                    ),
                    _now_utc(),
                ),
            )
            _event(conn, rid, f"{stage}-cart", {"quantities": current, "cart": data})
        else:
            expected = _latest(conn, rid, f"{stage}-cart")
            anchor = _recipe_anchor(conn, rid) if stage == "recipe" else None
            if anchor is not None:
                if _cart_lines(obs) != _cart_lines(anchor):
                    raise WeeklyPlanError("unexplained cart change on resume")
            elif (stage == "recurring" or expected) and _cart(obs) != (
                expected["quantities"] if expected else _baseline(conn, rid)
            ):
                raise WeeklyPlanError("unexplained cart change on resume")
        _event(conn, rid, f"{stage}-resume", {"quantities": _cart(obs)})
        return _prepare(conn, config, paths, rid, run_id, obs, stage)


def run_advance(paths: GrossyPaths, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Advance from the basket-review barrier to recipe planning.

    Task 16: explicit user advance accepts the freshly reread cart after
    manual edits and permits dinner suggestions. Requires lifecycle
    ``awaiting-basket-review`` and a fresh cart observation. Transitions
    to ``recurring-apply`` so ``recipes-apply`` can proceed.
    """
    _check_database_exists(paths)
    obs = validate_cart_observation(data)
    # Manual review edits are accepted; failed/incomplete evidence is not a
    # complete reread and must not become the dinner-planning basis.
    if obs.mutation_result is not None and obs.mutation_result.outcome != "readback":
        raise WeeklyPlanError(
            "advance requires complete reread evidence, not failed/incomplete state"
        )
    _require_fresh_observation(obs)
    with _locked(paths) as (conn, config):
        row = conn.execute(
            "SELECT id, lifecycle FROM planner_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise WeeklyPlanError(f"unknown run ID: {run_id!r}")
        rid = cast(int, row[0])
        lifecycle = cast(str, row[1])
        if lifecycle != "awaiting-basket-review":
            raise WeeklyPlanError(
                f"advance requires lifecycle 'awaiting-basket-review', got {lifecycle!r}"
            )
        # Verify recurring completion evidence exists.
        if not conn.execute(
            "SELECT 1 FROM run_events WHERE run_id=? AND event_type='recurring-complete'",
            (rid,),
        ).fetchone():
            raise WeeklyPlanError("recurring completion evidence required before advance")
        now = _now_utc()
        conn.execute(
            "INSERT INTO run_events (run_id, event_type, detail, created_at) "
            "VALUES (?, 'basket-review-advance', ?, ?)",
            (rid, json.dumps({"quantities": _cart(obs), "cart": data}, sort_keys=True), now),
        )
        conn.execute(
            "UPDATE planner_runs SET lifecycle='recurring-apply', updated_at=? WHERE id=?",
            (now, rid),
        )
        conn.commit()
        # Task 16: pass the advance cart as "observation" so _reports recalculates
        # the whole-cart budget basis (resulting_total_eur) from the accepted
        # post-edit cart. This ensures recipe previews use the current cart total,
        # not the stale pre-advance value.
        _reports(
            conn,
            paths,
            rid,
            run_id,
            "recurring-apply",
            {"status": "advanced", "observation": data},
            stage="recurring",
        )
        return {
            "schema_name": "grossy.weekly.advance.v1",
            "schema_version": 1,
            "status": "success",
            "run_id": run_id,
            "lifecycle": "recurring-apply",
            "detail": "basket review accepted; recipe planning now permitted",
        }


def run_consent_revoke(
    paths: GrossyPaths, run_id: str, stage: str, rationale: str | None = None
) -> dict[str, Any]:
    """Revoke stage consent for a weekly run.

    Task 16: consent is revocable. After revocation, preflight and intent
    fail closed; readback (observe) of an already-pending intent remains
    possible without renewed write authorization.
    """
    if stage not in ("recurring", "recipe"):
        raise WeeklyPlanError(f"unknown stage: {stage!r}; must be 'recurring' or 'recipe'")
    _check_database_exists(paths)
    with _locked(paths) as (conn, config):
        row = conn.execute("SELECT id FROM planner_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise WeeklyPlanError(f"unknown run ID: {run_id!r}")
        rid = cast(int, row[0])
        consent = conn.execute(
            "SELECT id, status FROM run_stage_consent WHERE run_id=? AND stage=?",
            (rid, stage),
        ).fetchone()
        if consent is None:
            raise WeeklyPlanError(f"no consent record for stage {stage!r}")
        if cast(str, consent[1]) == "revoked":
            raise WeeklyPlanError(f"consent for {stage!r} is already revoked")
        now = _now_utc()
        conn.execute(
            "UPDATE run_stage_consent SET status='revoked', revoked_at=?, rationale=? WHERE id=?",
            (now, rationale, consent[0]),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, event_type, detail, created_at) "
            "VALUES (?, 'consent-revoked', ?, ?)",
            (rid, json.dumps({"stage": stage, "rationale": rationale}, sort_keys=True), now),
        )
        conn.commit()
        return {
            "schema_name": "grossy.weekly.consent-revoke.v1",
            "schema_version": 1,
            "status": "success",
            "run_id": run_id,
            "stage": stage,
            "detail": "consent revoked; no new intent or action is authorized",
        }
