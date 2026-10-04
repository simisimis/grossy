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
        cursor = conn.execute(
            "INSERT INTO run_mutation_manifests(run_id,stage,manifest_json,checksum,created_at) "
            "VALUES (?,?,?,?,?)",
            (rid, stage, json.dumps(data), manifest.checksum, _now_utc()),
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
                    {"quantities": _cart(validate_cart_observation(accepted["cart"]))},
                )
            _event(conn, rid, f"{stage}-complete", {"status": "zero-additions"})
            if stage == "recipe":
                lifecycle = "awaiting-user-verification"
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


def run_recurring_observe(
    paths: GrossyPaths, run_id: str, data: dict[str, Any], *, stage: str = "recurring"
) -> dict[str, Any]:
    obs = validate_cart_observation(data)
    with _locked(paths) as (conn, config):
        rid = _run(conn, run_id, stage)
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
            _event(conn, rid, f"{stage}-cart", {"quantities": current})
        else:
            expected = _latest(conn, rid, f"{stage}-cart")
            if stage == "recipe":
                anchor = _recipe_anchor(conn, rid)
                if anchor is not None and _cart_lines(obs) != _cart_lines(anchor):
                    raise WeeklyPlanError("unexplained cart change on resume")
            if (stage == "recurring" or expected) and _cart(obs) != (
                expected["quantities"] if expected else _baseline(conn, rid)
            ):
                raise WeeklyPlanError("unexplained cart change on resume")
        _event(conn, rid, f"{stage}-resume", {"quantities": _cart(obs)})
        return _prepare(conn, config, paths, rid, run_id, obs, stage)
