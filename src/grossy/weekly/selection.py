"""Explicit dinner selection and current, passing-only culinary consolidation."""

from __future__ import annotations

import json
import sqlite3
from decimal import ROUND_CEILING, Decimal
from typing import Any

from grossy.browser.observation import CartObservation
from grossy.config.models import GrossyConfigModel, product_id_from_url
from grossy.history.policy import product_gate, resolve_yaml_policy
from grossy.paths import GrossyPaths
from grossy.weekly.mutation import _event, _locked, _reports
from grossy.weekly.plan import WeeklyPlanError, _now_utc
from grossy.weekly.preview import (
    _UNITS,
    ingredient_family_blacklisted,
    local_products,
    pantry_covers,
    product_identity_valid,
    retail_size,
    uses_estimate,
)


def selected_candidates(conn: sqlite3.Connection, rid: int) -> list[dict[str, Any]]:
    row = conn.execute(
        "SELECT candidates_json FROM run_recipe_plans WHERE run_id=?", (rid,)
    ).fetchone()
    if row is None:
        raise WeeklyPlanError("persisted recipe plan required")
    from grossy.weekly.matching import mapped_candidates

    candidates = {c["revision_id"]: c for c in mapped_candidates(conn, rid, json.loads(row[0]))}
    result = []
    for slot, dinner, revision in conn.execute(
        "SELECT s.slot_name,s.dinner_label,o.recipe_revision_id FROM run_recipe_selections s "
        "JOIN run_recipe_slots o ON o.run_id=s.run_id AND o.slot_name=s.slot_name "
        "WHERE s.run_id=? ORDER BY s.arg_order",
        (rid,),
    ):
        result.append({**candidates[revision], "slot": slot, "dinner": dinner})
    return result


def run_selection_apply(paths: GrossyPaths, run_id: str, value: str) -> dict[str, Any]:
    with _locked(paths) as (conn, config):
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
            raise WeeklyPlanError("selection requires an existing Stage2 run")
        rid = row[0]
        slots = dict(
            conn.execute(
                "SELECT slot_name,recipe_revision_id FROM run_recipe_slots "
                "WHERE run_id=? ORDER BY slot_name",
                (rid,),
            )
        )
        nonempty_slots = [name for name, revision in slots.items() if revision]
        names = nonempty_slots if value == "all" else value.split(",")
        # Task 16: `all` resolves only when ≤ 3 nonempty slots.
        if value == "all" and len(nonempty_slots) > 3:
            raise WeeklyPlanError(
                f"'all' resolves to {len(nonempty_slots)} slots; select at most three explicitly"
            )
        if (
            not names
            or len(names) > 3
            or len(set(names)) != len(names)
            or any(not slots.get(n) for n in names)
        ):
            raise WeeklyPlanError("select one to three unique nonempty recipe slots")
        previous = [
            r[0]
            for r in conn.execute(
                "SELECT slot_name FROM run_recipe_selections WHERE run_id=? ORDER BY arg_order",
                (rid,),
            )
        ]
        if previous and previous != names:
            raise WeeklyPlanError("persisted dinner selection cannot change during recovery")
        if not previous:
            if row[1] != "awaiting-recipe-selection":
                raise WeeklyPlanError("new selection requires awaiting-recipe-selection")
            for order, name in enumerate(names, 1):
                recipe = conn.execute(
                    "SELECT recipe_id FROM recipe_revisions WHERE id=?", (slots[name],)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO run_recipe_selections(run_id,arg_order,slot_name,dinner_label,"
                    "recipe_id,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (rid, order, name, f"dinner{order}", recipe, _now_utc()),
                )
                conn.execute(
                    "INSERT INTO recipe_events(recipe_id,event_type,run_id,detail,created_at) "
                    "VALUES (?,'selected',?,?,?)",
                    (recipe, run_id, name, _now_utc()),
                )
            _event(conn, rid, "recipe-selection", {"slots": names})
            # Task 16: selection grants durable run-scoped recipe stage consent.
            conn.execute(
                "INSERT INTO run_stage_consent (run_id, stage, status, granted_at) "
                "VALUES (?, 'recipe', 'granted', ?)",
                (rid, _now_utc()),
            )
            _event(
                conn,
                rid,
                "consent-granted",
                {"stage": "recipe", "granted_by": "selection-apply"},
            )
        dinners = selected_candidates(conn, rid)
        report = json.loads(
            conn.execute(
                "SELECT report_json FROM weekly_reports WHERE run_id=?", (rid,)
            ).fetchone()[0]
        )
        report["recipe_selection_mappings"] = [
            {"recipe": c["slot"], "dinner": c["dinner"]} for c in dinners
        ]
        report["selected_dinners"] = [
            {
                "slot": c["slot"],
                "dinner": c["dinner"],
                "title": c["title"],
                "revision_id": str(c["revision_id"]),
                "preparation_steps": c["steps"],
            }
            for c in dinners
        ]
        conn.execute(
            "UPDATE weekly_reports SET report_json=? WHERE run_id=?", (json.dumps(report), rid)
        )
        _reports(conn, paths, rid, run_id, row[1], {"status": "selected"}, stage="recipe")
        return {
            "schema_name": "grossy.weekly.selection.v1",
            "schema_version": 1,
            "status": "persisted",
            "mapping": report["recipe_selection_mappings"],
        }


def _limits(config: GrossyConfigModel, pid: str) -> list[Any]:
    return [
        s
        for s in [*config.staples.breakfast, *config.staples.lunch]
        if pid
        in [
            s.product_id or product_id_from_url(s.product_url or ""),
            *s.approved_alternative_product_ids,
        ]
    ]


def recipe_plan(
    conn: sqlite3.Connection, rid: int, config: GrossyConfigModel, obs: CartObservation
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Classify all recipes before consolidating; credit each cart unit only once."""
    if resolve_yaml_policy(config, conn).has_conflicts:
        raise WeeklyPlanError("YAML blacklist conflict blocks recipe additions")
    facts = {f.product_id: f for f in obs.recipe_product_facts}
    removed = {
        json.loads(row[0])["product_id"]
        for row in conn.execute(
            "SELECT detail FROM run_events WHERE run_id=? AND event_type='removal-verified'", (rid,)
        )
    }
    removed_equivalents = set(removed)
    for removed_pid in removed:
        removed_equivalents.update(
            row[0]
            for row in conn.execute(
                "SELECT p.retailer_product_id FROM family_members m "
                "JOIN products p ON p.id=m.product_id "
                "WHERE m.family_id=(SELECT fm.family_id FROM family_members fm "
                "JOIN products rp ON rp.id=fm.product_id WHERE rp.retailer_product_id=?)",
                (removed_pid,),
            )
        )
    decisions: list[dict[str, Any]] = []
    from grossy.weekly.recipes import _eligibility

    for candidate in selected_candidates(conn, rid):
        decision: dict[str, Any] = {
            "slot": candidate["slot"],
            "dinner": candidate["dinner"],
            "status": "passing",
            "ingredients": [],
            "reasons": _eligibility(conn, candidate, config),
        }
        for preview in candidate["preview"]["ingredients"]:
            name, unit = preview["name"], preview["unit"]
            item = {
                "name": name,
                "unit": unit,
                "linear_target": preview["linear_target"],
                "target": preview["target"],
                "assumptions": preview["assumptions"][:2],
            }
            decision["ingredients"].append(item)
            if ingredient_family_blacklisted(conn, name):
                decision["reasons"].append(f"{name}: family blacklisted")
                continue
            if pantry_covers(name, config):
                item.update(status="pantry", pantry_credit=preview["target"])
                continue
            exact = preview["exact_match"]
            coverage = preview.get("retail_unit_coverage", {})
            # Culinary units (tsp, tbsp, sprig, bunch) only pass with a declared estimate.
            if exact is None or (unit.casefold() not in _UNITS and not coverage):
                decision["reasons"].append(f"{name}: unresolved preview match or unit")
                continue
            semantic_ids = preview.get("semantic_compatible_product_ids")
            products = {p["product_id"]: p for p in local_products(conn, name, semantic_ids)}
            preferred = products.get(exact["product_id"])
            if preferred is None or preferred["canonical_url"] != exact["canonical_url"]:
                decision["reasons"].append(f"{name}: stale preview identity")
                continue
            ids = [
                exact["product_id"],
                *[p["product_id"] for p in preview["approved_family_fallbacks"]],
            ]
            allowed = []
            for pid in ids:
                product = products.get(pid)
                if (
                    product is None
                    or not product["approved"]
                    or product["retired"]
                    or not product_identity_valid(product)
                    or product_gate(conn, pid).is_blocked
                ):
                    continue
                if (
                    pid != exact["product_id"]
                    and not semantic_ids
                    and (
                        not preferred["family_row_id"]
                        or product["family_row_id"] != preferred["family_row_id"]
                    )
                ):
                    continue
                # YAML explicit substitution restrictions also constrain a previewed fallback.
                if pid != exact["product_id"] and any(
                    not s.allow_substitution or pid not in s.approved_alternative_product_ids
                    for s in _limits(config, exact["product_id"])
                ):
                    continue
                fact = facts.get(pid)
                if fact is None or fact.canonical_url != product["canonical_url"]:
                    continue
                if any(
                    v is None
                    for v in (
                        fact.package_amount,
                        fact.package_unit,
                        fact.package_count,
                        fact.sale_kind,
                    )
                ):
                    continue
                size = retail_size(fact.model_dump(), unit, coverage)
                if size is None:
                    continue
                allowed.append((pid, size))
            preferred_fact = facts.get(exact["product_id"])
            # Unknown stock is not permission to substitute; unavailable/blacklisted is.
            can_fallback = product_gate(conn, exact["product_id"]).is_blocked or (
                preferred_fact is not None and preferred_fact.availability == "unavailable"
            )
            selected = next(
                (
                    (pid, size)
                    for pid, size in allowed
                    if facts[pid].availability == "available"
                    and (pid == exact["product_id"] or can_fallback)
                ),
                None,
            )
            if selected is None:
                decision["reasons"].append(
                    f"{name}: missing current compatible identity/package/stock facts"
                )
                continue
            pid, size = selected
            observed_price = facts[pid].displayed_price_eur
            assert observed_price is not None  # Available facts require a real price.
            price = Decimal(observed_price)
            if any(
                s.max_product_price_eur is not None and price > s.max_product_price_eur
                for s in [*_limits(config, pid), *_limits(config, exact["product_id"])]
            ):
                decision["reasons"].append(f"{name}: hard product price limit")
                continue
            basis, factor = _UNITS.get(
                unit.casefold(), (f"recipe-unit:{unit.casefold()}", Decimal(1))
            )
            item.update(
                removal_protected=bool(set(ids) & removed_equivalents),
                status="exact" if pid == exact["product_id"] else "substituted",
                product_id=pid,
                preferred_product_id=exact["product_id"],
                canonical_url=facts[pid].canonical_url,
                price_eur=facts[pid].displayed_price_eur,
                basis=basis,
                base_target=str(Decimal(preview["target"]) * factor),
                base_size=str(size * factor),
                compatible={p: str(q * factor) for p, q in allowed},
            )
            estimated = uses_estimate(facts[pid].model_dump(), unit, coverage)
            item["assumptions"].append(
                f"one adapter unit supplies {size} {unit}; "
                + (
                    "explicit agent estimate, not an observed package fact"
                    if estimated
                    else "rendered weighed increment"
                    if facts[pid].sale_kind == "weighed"
                    else "whole package amount times count"
                )
                + "; rounded upward, never knowingly undersupplied"
            )
            if estimated:
                item["estimated_coverage"] = True
            item["estimated_credit_product_ids"] = sorted(
                p for p, _ in allowed if uses_estimate(facts[p].model_dump(), unit, coverage)
            )
        if decision["reasons"]:
            decision["status"] = "blocked"
        decisions.append(decision)

    # Consolidate only identical compatible groups; overlapping differing groups
    # are ambiguous, not permission to spend the same cart credit twice.
    while True:
        groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for d in decisions:
            if d["status"] == "passing":
                for i in d["ingredients"]:
                    if i.get("product_id"):
                        groups.setdefault(i["product_id"], []).append((d, i))
        blocked: set[str] = set()
        additions = []
        used: set[str] = set()
        for pid, contributions in groups.items():
            sample = contributions[0][1]
            compatible = sample["compatible"]
            if used.intersection(compatible) or any(
                i["compatible"] != compatible
                or i["basis"] != sample["basis"]
                or i["base_size"] != sample["base_size"]
                for _, i in contributions
            ):
                blocked.update(d["slot"] for d, _ in contributions)
                # Block prior overlapping group as well.
                blocked.update(
                    d["slot"]
                    for g in groups.values()
                    for d, i in g
                    if set(i["compatible"]).intersection(compatible)
                )
                continue
            used.update(compatible)
            target = sum((Decimal(i["base_target"]) for _, i in contributions), Decimal(0))
            credit = sum(
                (
                    Decimal(line.quantity) * Decimal(compatible[line.product_id])
                    for line in obs.lines
                    if line.product_id in compatible
                ),
                Decimal(0),
            )
            est_pids = set(sample.get("estimated_credit_product_ids", []))
            if est_pids and any(
                line.product_id in est_pids for line in obs.lines if line.product_id in compatible
            ):
                for _, i in contributions:
                    i["assumptions"].append(
                        "cart credit includes explicit agent estimates for non-comparable "
                        "package units; not an observed package fact"
                    )
            # Any positive cart credit satisfies the ingredient; never top up.
            missing = Decimal(0) if credit > 0 else target
            quantity = int(
                (missing / Decimal(sample["base_size"])).to_integral_value(rounding=ROUND_CEILING)
            )
            if quantity and any(i["removal_protected"] for _, i in contributions):
                blocked.update(d["slot"] for d, _ in contributions)
                for d, _ in contributions:
                    d["reasons"].append("explicit removal would be undone; renewed review required")
                continue
            intended = int(
                (target / Decimal(sample["base_size"])).to_integral_value(rounding=ROUND_CEILING)
            )
            price = Decimal(sample["price_eur"])
            limits = [
                s
                for _, i in contributions
                for p in (pid, i["preferred_product_id"])
                for s in _limits(config, p)
            ]
            if any(
                s.max_line_price_eur is not None and price * intended > s.max_line_price_eur
                for s in limits
            ):
                blocked.update(d["slot"] for d, _ in contributions)
                continue
            for _, i in contributions:
                i.update(
                    consolidated_target=str(target),
                    consolidated_cart_credit=str(min(target, credit)),
                    retail_quantity=quantity,
                    retail_unit="adapter units",
                    consolidation_product_id=pid,
                )
            if quantity:
                additions.append(
                    {
                        "product_id": pid,
                        "canonical_url": sample["canonical_url"],
                        "quantity": quantity,
                        "price_eur": sample["price_eur"],
                    }
                )
        if not blocked:
            return additions, decisions
        for d in decisions:
            if d["slot"] in blocked:
                d["status"] = "blocked"
                d["reasons"].append(
                    "consolidated hard line limit or ambiguous overlapping compatible groups"
                )


def run_recipe_preflight(paths: GrossyPaths, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    from grossy.weekly.mutation import run_recurring_preflight

    return run_recurring_preflight(paths, run_id, data, stage="recipe")


def run_recipe_intent(paths: GrossyPaths, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    from grossy.weekly.mutation import run_recurring_intent

    return run_recurring_intent(paths, run_id, data, stage="recipe")


def run_recipe_observe(paths: GrossyPaths, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    from grossy.weekly.mutation import run_recurring_observe

    return run_recurring_observe(paths, run_id, data, stage="recipe")
