"""Explicit single-action removal; append-only evidence, never rollback/replay."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, StrictInt

from grossy.browser.observation import validate_cart_observation
from grossy.paths import GrossyPaths
from grossy.recipes.proposal import proposal_checksum
from grossy.weekly.closure import persist_report
from grossy.weekly.mutation import (
    Addition,
    MutationManifest,
    _baseline,
    _cart_lines,
    _event,
    _latest,
    _locked,
    _pending,
    _require_fresh_observation,
)
from grossy.weekly.plan import WeeklyPlanError, _now_utc


class RemovalManifest(MutationManifest):
    stage: Literal["removal"]  # type: ignore[assignment]
    additions: list[Addition] = Field(max_length=0)
    removals: list[Addition] = Field(min_length=1, max_length=1)
    run_attributed_quantity: StrictInt = Field(ge=0)
    preexisting_quantity: StrictInt = Field(ge=0)
    include_preexisting: bool


def removal_run(conn: sqlite3.Connection, run_id: str) -> tuple[int, str]:
    row = conn.execute("SELECT id,lifecycle FROM planner_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row or row[1] not in (
        "awaiting-recipe-selection",
        "awaiting-user-verification",
        "partial-failure",
        "needs-attention",
    ):
        raise WeeklyPlanError("removal requires a prepared active run")
    rid = int(row[0])
    if not conn.execute("SELECT 1 FROM weekly_reports WHERE run_id=?", (rid,)).fetchone():
        raise WeeklyPlanError("accepted baseline required")
    return rid, str(row[1])


def outstanding(conn: sqlite3.Connection, rid: int) -> bool:
    return any(_pending(conn, rid, stage) for stage in ("recurring", "recipe", "removal"))


def state_checksum(conn: sqlite3.Connection, rid: int) -> str:
    return proposal_checksum(
        {
            "manifests": [
                list(row)
                for row in conn.execute(
                    "SELECT id,checksum FROM run_mutation_manifests WHERE run_id=? ORDER BY id",
                    (rid,),
                )
            ],
            "observations": [
                list(row)
                for row in conn.execute(
                    "SELECT o.id,o.failure_class FROM run_mutation_observations o "
                    "JOIN run_mutation_manifests m ON m.id=o.manifest_id "
                    "WHERE m.run_id=? ORDER BY o.id",
                    (rid,),
                )
            ],
        }
    )


def attributed(conn: sqlite3.Connection, rid: int, pid: str, current: int) -> int:
    verified = 0
    # Only independently verified actual additions count, once per manifest.
    for mid, raw in conn.execute(
        "SELECT id,manifest_json FROM run_mutation_manifests WHERE run_id=? "
        "AND stage IN ('recurring','recipe')",
        (rid,),
    ):
        row = conn.execute(
            "SELECT discrepancies_json FROM run_mutation_observations "
            "WHERE manifest_id=? AND failure_class='none' ORDER BY id DESC LIMIT 1",
            (mid,),
        ).fetchone()
        if row:
            detail = json.loads(row[0] or "{}")
            for addition in json.loads(raw)["additions"]:
                if addition["product_id"] == pid:
                    verified += min(
                        addition["quantity"], detail.get("verified_quantity", addition["quantity"])
                    )
    for raw in conn.execute(
        "SELECT detail FROM run_events WHERE run_id=? AND event_type='removal-verified'", (rid,)
    ):
        data = json.loads(raw[0])
        if data["product_id"] == pid:
            verified -= data["run_attributed_quantity"]
    return max(0, min(verified, current - _baseline(conn, rid).get(pid, 0)))


def run_removal_preflight(
    paths: GrossyPaths,
    run_id: str,
    data: dict[str, Any],
    *,
    product: str,
    quantity: int | None = None,
    include_preexisting: bool = False,
) -> dict[str, Any]:
    obs = validate_cart_observation(data)
    _require_fresh_observation(obs)
    if obs.mutation_result and obs.mutation_result.outcome != "readback":
        raise WeeklyPlanError("complete cart reread required")
    with _locked(paths) as (conn, _):
        rid, lifecycle = removal_run(conn, run_id)
        if outstanding(conn, rid):
            raise WeeklyPlanError("outstanding intent requires reconciliation; no reauthorization")
        lines = _cart_lines(obs)
        line = lines.get(product)
        if not line:
            raise WeeklyPlanError("exact product must be present in current cart")
        anchor_row = conn.execute(
            "SELECT detail FROM run_events WHERE run_id=? AND event_type IN "
            "('removal-cart','removal-preflight','recipe-cart','recurring-cart') "
            "AND json_type(detail,'$.cart')='object' ORDER BY id DESC LIMIT 1",
            (rid,),
        ).fetchone()
        anchor = json.loads(anchor_row[0]) if anchor_row else None
        if anchor:
            expected_lines = _cart_lines(validate_cart_observation(anchor["cart"]))
        else:
            last = conn.execute(
                "SELECT o.observed_json FROM run_mutation_observations o "
                "JOIN run_mutation_manifests m ON m.id=o.manifest_id "
                "WHERE m.run_id=? AND o.failure_class='none' ORDER BY o.id DESC LIMIT 1",
                (rid,),
            ).fetchone()
            expected_lines = (
                _cart_lines(validate_cart_observation(json.loads(last[0])))
                if last
                else {
                    pid: {
                        "product_id": pid,
                        "canonical_url": url,
                        "quantity": qty,
                        "displayed_price_eur": f"{Decimal(cents or 0) / 100:.2f}",
                    }
                    for pid, url, qty, cents in conn.execute(
                        "SELECT p.retailer_product_id,p.canonical_url,b.quantity,b.price_cents "
                        "FROM run_cart_baselines b JOIN products p ON p.id=b.product_id "
                        "WHERE b.run_id=?",
                        (rid,),
                    )
                }
            )
        if expected_lines != lines:
            conn.execute("UPDATE planner_runs SET lifecycle='needs-attention' WHERE id=?", (rid,))
            _event(conn, rid, "removal-drift", {"cart": data})
            persist_report(
                conn,
                paths,
                rid,
                run_id,
                "needs-attention",
                {"status": "removal-drift", "cart": data},
            )
            raise WeeklyPlanError("unexpected cart drift; removal stopped")
        available = attributed(conn, rid, product, line["quantity"])
        requested = available if quantity is None else quantity
        if type(requested) is not int or requested <= 0 or requested > line["quantity"]:
            raise WeeklyPlanError("removal quantity must be positive and bounded by current cart")
        run_qty = min(available, requested)
        preexisting = requested - run_qty
        if preexisting and not include_preexisting:
            raise WeeklyPlanError(
                "quantity exceeds current-run attribution; --include-preexisting required"
            )
        removal = {
            "product_id": product,
            "canonical_url": line["canonical_url"],
            "quantity": requested,
            "price_eur": line["displayed_price_eur"],
        }
        accepted = {
            "cart": data,
            "removals": [removal],
            "run_attributed_quantity": run_qty,
            "preexisting_quantity": preexisting,
            "include_preexisting": include_preexisting,
            "lifecycle": lifecycle,
            "state_checksum": state_checksum(conn, rid),
        }
        checksum = proposal_checksum(accepted)
        _event(conn, rid, "removal-preflight", {**accepted, "checksum": checksum})
        body = {
            "schema_name": "grossy.cart.mutation-manifest.v1",
            "schema_version": 1,
            "generated_at": _now_utc(),
            "run_id": run_id,
            "stage": "removal",
            "preflight_checksum": checksum,
            "additions": [],
            "removals": [removal],
            "run_attributed_quantity": run_qty,
            "preexisting_quantity": preexisting,
            "include_preexisting": include_preexisting,
        }
        return {
            "schema_name": "grossy.weekly.removal-preflight.v1",
            "schema_version": 1,
            "status": "accepted",
            "manifest": {**body, "checksum": proposal_checksum(body)},
        }


def run_removal_intent(
    paths: GrossyPaths,
    run_id: str,
    data: dict[str, Any],
    *,
    confirmed: bool,
) -> dict[str, Any]:
    manifest = RemovalManifest.model_validate(data)
    if not confirmed:
        raise WeeklyPlanError("explicit interactive removal confirmation required")
    if (
        manifest.run_id != run_id
        or proposal_checksum({k: v for k, v in data.items() if k != "checksum"})
        != manifest.checksum
    ):
        raise WeeklyPlanError("manifest checksum or run mismatch")
    with _locked(paths) as (conn, _):
        rid, lifecycle = removal_run(conn, run_id)
        accepted = _latest(conn, rid, "removal-preflight")
        if (
            outstanding(conn, rid)
            or not accepted
            or accepted["checksum"] != manifest.preflight_checksum
        ):
            raise WeeklyPlanError("fresh accepted preflight without outstanding intent required")
        _require_fresh_observation(validate_cart_observation(accepted["cart"]))
        if state_checksum(conn, rid) != accepted["state_checksum"]:
            raise WeeklyPlanError("run evidence changed since preflight")
        age = (
            datetime.now(UTC) - datetime.fromisoformat(manifest.generated_at.replace("Z", "+00:00"))
        ).total_seconds()
        if age < -30 or age > 300:
            raise WeeklyPlanError("manifest expired")
        if lifecycle != accepted["lifecycle"]:
            raise WeeklyPlanError("lifecycle changed since preflight")
        for key in (
            "removals",
            "run_attributed_quantity",
            "preexisting_quantity",
            "include_preexisting",
        ):
            if data[key] != accepted[key]:
                raise WeeklyPlanError("manifest differs from accepted removal split")
        if conn.execute(
            "SELECT 1 FROM run_mutation_manifests WHERE run_id=? AND stage='removal' "
            "AND json_extract(manifest_json,'$.preflight_checksum')=?",
            (rid, manifest.preflight_checksum),
        ).fetchone():
            raise WeeklyPlanError("consumed removal cannot be reauthorized")
        removal = data["removals"][0]
        pid = conn.execute(
            "SELECT id,canonical_url FROM products WHERE retailer_product_id=?",
            (removal["product_id"],),
        ).fetchone()
        if not pid or pid[1] != removal["canonical_url"]:
            raise WeeklyPlanError("persisted product identity mismatch")
        conn.execute(
            "INSERT INTO run_removals(run_id,product_id,authorized_quantity,"
            "run_attributed_quantity,preexisting_quantity,include_preexisting,"
            "confirmed,created_at) VALUES (?,?,?,?,?,?,1,?)",
            (
                rid,
                pid[0],
                removal["quantity"],
                manifest.run_attributed_quantity,
                manifest.preexisting_quantity,
                manifest.include_preexisting,
                _now_utc(),
            ),
        )
        mid = conn.execute(
            "INSERT INTO run_mutation_manifests(run_id,stage,manifest_json,checksum,"
            "created_at) VALUES (?,'removal',?,?,?)",
            (rid, json.dumps(data), manifest.checksum, _now_utc()),
        ).lastrowid
        conn.execute(
            "INSERT INTO run_mutation_observations(manifest_id,observed_json,"
            "discrepancies_json,failure_class,created_at) VALUES (?,?,?,'total',?)",
            (
                mid,
                json.dumps(accepted["cart"]),
                json.dumps({"status": "intent-persisted-readback-incomplete"}),
                _now_utc(),
            ),
        )
        _event(
            conn,
            rid,
            "removal-intent",
            {"manifest": data, "before": accepted["cart"], "confirmed": True},
        )
        persist_report(
            conn,
            paths,
            rid,
            run_id,
            lifecycle,
            {
                "status": "removal-intent",
                "manifest": data,
                "before": accepted["cart"],
                "confirmed": True,
            },
        )
        return {
            "schema_name": "grossy.weekly.removal-authorization.v1",
            "schema_version": 1,
            "status": "persisted",
            "manifest_checksum": manifest.checksum,
            "removal": removal,
        }


def run_removal_observe(paths: GrossyPaths, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    obs = validate_cart_observation(data)
    _require_fresh_observation(obs)
    with _locked(paths) as (conn, _):
        rid, lifecycle = removal_run(conn, run_id)
        pending = _pending(conn, rid, "removal")
        if (
            not pending
            or not obs.mutation_result
            or obs.mutation_result.manifest_checksum != pending[2]
        ):
            raise WeeklyPlanError("readback must bind outstanding removal manifest")
        manifest = json.loads(pending[1])
        accepted = _latest(conn, rid, "removal-preflight")
        assert accepted is not None
        intent_created_at = conn.execute(
            "SELECT created_at FROM run_mutation_manifests WHERE id=?", (pending[0],)
        ).fetchone()[0]
        if obs.mutation_result.outcome == "readback" and datetime.fromisoformat(
            obs.generated_at.replace("Z", "+00:00")
        ) < datetime.fromisoformat(intent_created_at.replace("Z", "+00:00")):
            raise WeeklyPlanError("readback predates removal intent")
        before_lines = _cart_lines(validate_cart_observation(accepted["cart"]))
        expected = _cart_lines(validate_cart_observation(accepted["cart"]))
        removal = manifest["removals"][0]
        pid = removal["product_id"]
        expected[pid]["quantity"] -= removal["quantity"]
        if not expected[pid]["quantity"]:
            del expected[pid]
        verified = obs.mutation_result.outcome == "readback" and expected == _cart_lines(obs)
        no_effect = obs.mutation_result.outcome == "readback" and before_lines == _cart_lines(obs)
        success = verified or no_effect
        evidence = {
            "expected": expected,
            "observation": data,
            "status": "verified" if verified else "not-applied" if no_effect else "partial-failure",
            "manifest": manifest,
        }
        conn.execute(
            "INSERT INTO run_mutation_observations(manifest_id,observed_json,"
            "discrepancies_json,failure_class,created_at) VALUES (?,?,?,?,?)",
            (
                pending[0],
                json.dumps(data),
                json.dumps(evidence),
                "none" if success else "partial",
                _now_utc(),
            ),
        )
        if success:
            intent_id = conn.execute(
                "SELECT MAX(id) FROM run_events WHERE run_id=? AND event_type='removal-intent'",
                (rid,),
            ).fetchone()[0]
            independent = conn.execute(
                "SELECT 1 FROM run_events WHERE run_id=? AND id>? "
                "AND event_type NOT IN ('report-failure','removal-observe') LIMIT 1",
                (rid, intent_id),
            ).fetchone()
            competing = any(_pending(conn, rid, stage) for stage in ("recipe", "recurring"))
            if not independent and not competing:
                lifecycle = accepted["lifecycle"]
                conn.execute("UPDATE planner_runs SET lifecycle=? WHERE id=?", (lifecycle, rid))
            if verified:
                _event(
                    conn,
                    rid,
                    "removal-verified",
                    {
                        "product_id": pid,
                        "run_attributed_quantity": manifest["run_attributed_quantity"],
                        "preexisting_quantity": manifest["preexisting_quantity"],
                    },
                )
            _event(conn, rid, "removal-cart", {"cart": data})
        else:
            lifecycle = "partial-failure"
            conn.execute("UPDATE planner_runs SET lifecycle=? WHERE id=?", (lifecycle, rid))
        _event(conn, rid, "removal-observe", evidence)
        persist_report(conn, paths, rid, run_id, lifecycle, evidence)
        return {
            "schema_name": "grossy.weekly.removal-observe.v1",
            "schema_version": 1,
            "status": "success" if success else "error",
            "lifecycle": lifecycle,
            "result": evidence["status"],
        }
