"""Conservative local identity matching and three-full-serving previews.

Only exact normalized ingredient/product or ingredient/family names establish
compatibility. No fuzzy title match, package guess, density conversion or live
lookup is performed. Proposal facts are explicitly unverified projections.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from decimal import ROUND_CEILING, Decimal
from typing import Any

from grossy.browser.observation import CartObservation, validate_cart_observation
from grossy.config.models import GrossyConfigModel, product_id_from_url
from grossy.history.policy import product_gate
from grossy.weekly.plan import WeeklyPlanError
from grossy.weekly.proposal import IngredientMatch


def ingredient_identity(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def pantry_covers(name: str, config: GrossyConfigModel) -> bool:
    return ingredient_identity(name) in {ingredient_identity(v) for v in config.pantry_assumptions}


def ingredient_family_blacklisted(conn: sqlite3.Connection, name: str) -> bool:
    identity = ingredient_identity(name)
    return any(
        ingredient_identity(row[0]) == identity
        for row in conn.execute(
            "SELECT name FROM product_families WHERE blacklist_state = 'blacklisted' "
            "UNION SELECT p.name FROM products p JOIN family_members fm ON fm.product_id=p.id "
            "JOIN product_families f ON f.id=fm.family_id WHERE f.blacklist_state='blacklisted'"
        )
    )


def local_products(
    conn: sqlite3.Connection, name: str, semantic_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    identity = ingredient_identity(name)
    reviewed = set(semantic_ids or [])
    rows = conn.execute(
        "SELECT p.id, p.retailer_product_id, p.canonical_url, p.name, p.package_amount, "
        "p.package_unit, p.package_count, p.observation_state, p.purchase_order_count, "
        "f.id, f.name, fm.approval_state, f.primary_product_id "
        "FROM products p LEFT JOIN family_members fm ON fm.product_id=p.id "
        "LEFT JOIN product_families f ON f.id=fm.family_id WHERE p.retailer='ab' ORDER BY p.id"
    )
    return [
        dict(
            row_id=r[0],
            product_id=r[1],
            canonical_url=r[2],
            name=r[3],
            package_amount=r[4],
            package_unit=r[5],
            package_count=r[6],
            retired=r[7] == "retired",
            approved=r[8] > 0 or r[11] == "approved" or r[1] in reviewed,
            purchase_order_count=r[8],
            family_row_id=r[9],
            family_name=r[10],
            primary=r[0] == r[12],
            exact_product=ingredient_identity(r[3]) == identity,
            exact_family=r[10] is not None and ingredient_identity(r[10]) == identity,
        )
        for r in rows
        if r[1] in reviewed
        or ingredient_identity(r[3]) == identity
        or (r[10] is not None and ingredient_identity(r[10]) == identity)
    ]


# Comparable measured units only; no culinary/count-to-mass conversion.
_UNITS = {
    "g": ("mass", Decimal(1)),
    "kg": ("mass", Decimal(1000)),
    "ml": ("volume", Decimal(1)),
    "l": ("volume", Decimal(1000)),
    "pc": ("count", Decimal(1)),
    "piece": ("count", Decimal(1)),
    "pieces": ("count", Decimal(1)),
    "count": ("count", Decimal(1)),
}


def product_identity_valid(product: dict[str, Any]) -> bool:
    try:
        return bool(product_id_from_url(product["canonical_url"]) == product["product_id"])
    except ValueError:
        return False


def retail_size(product: dict[str, Any], unit: str, coverage: dict[str, str]) -> Decimal | None:
    """Observed comparable package amount, else the explicit reviewed coverage estimate.

    Counted recipe units (clove, slice) need not share the retail counting unit
    (net, loaf), so a declared estimate takes precedence for count targets.
    """
    native = package_amount(product, unit)
    estimate = coverage.get(product["product_id"])
    if estimate is not None and (native is None or is_count_unit(unit)):
        return Decimal(estimate)
    return native


def is_count_unit(unit: str) -> bool:
    return _UNITS.get(unit.casefold(), ("", Decimal(0)))[0] == "count"


def uses_estimate(product: dict[str, Any], unit: str, coverage: dict[str, str]) -> bool:
    return product["product_id"] in coverage and (
        package_amount(product, unit) is None or is_count_unit(unit)
    )


def package_amount(product: dict[str, Any], unit: str) -> Decimal | None:
    target_unit = _UNITS.get(unit.casefold())
    retail_unit = _UNITS.get(str(product["package_unit"]).casefold())
    amount = product["package_amount"]
    if target_unit is None or retail_unit is None or target_unit[0] != retail_unit[0] or not amount:
        return None
    return (
        Decimal(str(amount))
        * retail_unit[1]
        / target_unit[1]
        * Decimal(product["package_count"] or 1)
    )


def _match_product(
    conn: sqlite3.Connection,
    name: str,
    observation: IngredientMatch | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str, str]:
    semantic = observation is not None and observation.evidence == "agent_semantic_review"
    products = local_products(
        conn, name, observation.compatible_product_ids if semantic and observation else None
    )
    if semantic and observation:
        from grossy.weekly.matching import require_history_preference

        require_history_preference(conn, observation)
    if observation is not None:
        preferred = next((p for p in products if p["product_id"] == observation.product_id), None)
        if preferred is None or (
            not semantic and not preferred[observation.evidence.removesuffix("_identity")]
        ):
            # Explicit observations cannot establish compatibility by assertion.
            return (
                None,
                [],
                "ambiguous",
                "proposed match lacks exact local ingredient identity evidence",
            )
        fallbacks = []
        for pid in observation.fallback_product_ids:
            fallback = next((p for p in products if p["product_id"] == pid), None)
            if (
                fallback is None
                or (
                    not semantic
                    and (
                        not preferred["family_row_id"]
                        or fallback["family_row_id"] != preferred["family_row_id"]
                    )
                )
                or not fallback["approved"]
            ):
                raise WeeklyPlanError(
                    "fallback must be an approved compatible member of the same family"
                )
            if not product_identity_valid(fallback):
                raise WeeklyPlanError("fallback canonical product identity is inconsistent")
            if product_gate(conn, pid).is_blocked:
                raise WeeklyPlanError("proposed fallback conflicts with current blacklist policy")
            if not fallback["retired"]:
                fallbacks.append(fallback)
    else:
        allowed = [
            p
            for p in products
            if p["approved"]
            and product_identity_valid(p)
            and not p["retired"]
            and not product_gate(conn, p["product_id"]).is_blocked
        ]
        primaries = [p for p in allowed if p["primary"]]
        if len(allowed) == 1:
            preferred = allowed[0]
        elif len(primaries) == 1 and all(
            p["family_row_id"] == primaries[0]["family_row_id"] for p in allowed
        ):
            preferred = primaries[0]
        else:
            status = (
                "ambiguous"
                if allowed
                else (
                    "blacklisted"
                    if products
                    and any(product_gate(conn, p["product_id"]).is_blocked for p in products)
                    else "unavailable"
                )
            )
            return (
                None,
                [],
                status,
                "no unambiguous approved local exact match; explicit review required",
            )
        fallbacks = [
            p
            for p in allowed
            if p["product_id"] != preferred["product_id"]
            and preferred["family_row_id"]
            and p["family_row_id"] == preferred["family_row_id"]
        ]
    if not product_identity_valid(preferred):
        return None, [], "ambiguous", "local canonical product identity is inconsistent"
    if not preferred["approved"]:
        return (
            None,
            [],
            "ambiguous",
            "exact product is not approved by local history or family approval",
        )
    if product_gate(conn, preferred["product_id"]).is_blocked:
        return (
            None,
            fallbacks,
            "blacklisted",
            "preferred exact product blocked; approved fallback requires review",
        )
    if preferred["retired"] or (observation and observation.availability == "unavailable"):
        return (
            preferred,
            fallbacks,
            "unavailable",
            "offline unavailable observation; fallback availability is unknown",
        )
    if semantic:
        reason = "agent-reviewed semantic identity; availability/price remain unverified"
    else:
        reason = "exact normalized local identity; availability/price remain unverified"
    return preferred, fallbacks, "matched", reason


def build_recipe_preview(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    config: GrossyConfigModel,
    matches: dict[tuple[str, str], IngredientMatch],
) -> dict[str, Any]:
    baseline = {
        r[0]: (Decimal(r[1]), Decimal(r[2]) if r[2] is not None else None)
        for r in conn.execute(
            "SELECT p.retailer_product_id, b.quantity, b.price_cents FROM run_cart_baselines b "
            "JOIN products p ON p.id=b.product_id WHERE b.run_id=?",
            (snapshot["run_row_id"],),
        )
    }
    # Task 16: the basket-review-advance cart reflects user manual edits and
    # takes precedence over the last recurring observation as the planning basis.
    advance = conn.execute(
        "SELECT detail FROM run_events WHERE run_id=? AND event_type='basket-review-advance' "
        "ORDER BY id DESC LIMIT 1",
        (snapshot["run_row_id"],),
    ).fetchone()
    cart_observation: CartObservation | None = None
    if advance:
        advance_data = json.loads(advance[0])
        cart_observation = validate_cart_observation(advance_data["cart"])
        baseline = {
            line.product_id: (Decimal(line.quantity), Decimal(line.displayed_price_eur) * 100)
            for line in cart_observation.lines
        }
    else:
        verified = conn.execute(
            "SELECT o.observed_json FROM run_mutation_observations o "
            "JOIN run_mutation_manifests m ON m.id=o.manifest_id "
            "WHERE m.run_id=? AND m.stage='recurring' AND o.failure_class='none' "
            "ORDER BY o.id DESC LIMIT 1",
            (snapshot["run_row_id"],),
        ).fetchone()
        if verified:
            cart_observation = validate_cart_observation(json.loads(verified[0]))
            baseline = {
                line.product_id: (Decimal(line.quantity), Decimal(line.displayed_price_eur) * 100)
                for line in cart_observation.lines
            }
    remaining = {pid: qty for pid, (qty, _) in baseline.items()}
    servings = config.household.recipe_servings
    ingredients = []
    total = Decimal(0)
    cost_known = True
    for ingredient in snapshot["ingredients"]:
        name, unit = ingredient["name"], ingredient["unit"]
        linear = (
            Decimal(str(ingredient["amount"]))
            * Decimal(servings)
            / Decimal(str(snapshot["servings"]))
        )
        target = (
            linear.to_integral_value(rounding=ROUND_CEILING)
            if ingredient["indivisible"]
            else linear
        )
        item: dict[str, Any] = {
            "name": name,
            "unit": unit,
            "recipe_package_annotation": {
                "size": ingredient["package_size"],
                "unit": ingredient["package_unit"],
                "notes": ingredient["notes"],
                "assumption": "recipe annotation is not proof of the store product package",
            },
            "linear_target": str(linear),
            "target": str(target),
            "pantry_covered": pantry_covers(name, config),
            "cart_credit": "0",
            "missing_target": str(target),
            "exact_match": None,
            "approved_family_fallbacks": [],
            "package_quantity": None,
            "incremental_cost_eur": None,
            "availability": "unknown",
            "assumptions": [
                f"linear scaling to {servings} full servings",
                "indivisible target rounded upward"
                if ingredient["indivisible"]
                else "divisible culinary target retained exactly",
                "cart credit uses basket-review advance cart (post manual edits)"
                if advance
                else "cart credit uses latest verified/reconciled recurring cart observation"
                if verified
                else "cart credit uses immutable baseline; no verified recurring observation",
                "offline observations are unverified; no live availability or price validation",
                "approved family fallback identity is not availability/price/hard-limit approval; "
                "fallback is previewed, not automatically selected",
            ],
        }
        if item["pantry_covered"]:
            item.update(
                status="pantry",
                missing_target="0",
                incremental_cost_eur="0.00",
                reason="exact normalized pantry identity",
            )
            ingredients.append(item)
            continue
        observation = matches.get((snapshot["recipe_id"], name))
        product, fallbacks, status, reason = _match_product(conn, name, observation)
        item.update(
            status=status,
            reason=reason,
            approved_family_fallbacks=[
                {
                    "product_id": p["product_id"],
                    "canonical_url": p["canonical_url"],
                    "availability": "unknown",
                }
                for p in fallbacks
            ],
        )
        if observation:
            item["availability"] = observation.availability
            item["evidence"] = observation.evidence
            item["rationale"] = observation.rationale
            if observation.evidence == "agent_semantic_review":
                item["semantic_compatible_product_ids"] = observation.compatible_product_ids
                item["assumptions"].append(
                    "agent-reviewed culinary compatibility; recipe qualifiers flexible; "
                    "history-ranked choice, not literal name equality or verified live facts"
                )
        if product is None:
            cost_known = False
            ingredients.append(item)
            continue
        item["exact_match"] = {
            "product_id": product["product_id"],
            "canonical_url": product["canonical_url"],
        }
        coverage = observation.retail_unit_coverage if observation else {}
        if coverage:
            item["retail_unit_coverage"] = coverage
        size = retail_size(product, unit, coverage)
        if size is not None and uses_estimate(product, unit, coverage):
            item["assumptions"].append(
                f"explicit agent estimate: one retail unit supplies {size} {unit}; "
                "not an observed package fact"
            )
        if size is None:
            item["assumptions"].append(
                "unknown or incompatible retail package unit; no conversion or package guess"
            )
            cost_known = False
            ingredients.append(item)
            continue
        item["assumptions"].append(
            f"retail package supplies {size} {unit}; whole packages rounded upward"
        )
        credit = Decimal(0)
        for compatible in [product, *fallbacks]:
            pid = compatible["product_id"]
            compatible_size = retail_size(compatible, unit, coverage)
            if compatible_size is not None and pid in remaining:
                if pid != product["product_id"] and uses_estimate(compatible, unit, coverage):
                    item["assumptions"].append(
                        f"explicit agent estimate for cart credit: one unit of {pid} "
                        f"supplies {compatible_size} {unit}; not an observed package fact"
                    )
                # Credit exact amounts; dividing first (e.g. by 900) leaves a repeating
                # remainder that would otherwise buy a whole extra package.
                take = min(max(Decimal(0), target - credit), remaining[pid] * compatible_size)
                credit += take
                remaining[pid] -= take / compatible_size
        missing = max(Decimal(0), target - credit)
        packages = (missing / size).to_integral_value(rounding=ROUND_CEILING)
        item.update(
            cart_credit=str(credit), missing_target=str(missing), package_quantity=str(packages)
        )
        price = Decimal(observation.price_eur) if observation and observation.price_eur else None
        if price is None and product["product_id"] in baseline:
            cents = baseline[product["product_id"]][1]
            price = Decimal(cents) / 100 if cents is not None else None
        item["price_eur"] = str(price) if price is not None else None
        # Apply explicit YAML hard limits to the same exact product, never weaken them.
        for category in (config.staples.breakfast, config.staples.lunch):
            for staple in category:
                pid = staple.product_id or (
                    product_id_from_url(staple.product_url) if staple.product_url else None
                )
                if (
                    product["product_id"] in [pid, *staple.approved_alternative_product_ids]
                    and price is not None
                    and (
                        (
                            staple.max_product_price_eur is not None
                            and price > staple.max_product_price_eur
                        )
                        or (
                            staple.max_line_price_eur is not None
                            and price * packages > staple.max_line_price_eur
                        )
                    )
                ):
                    item.update(status="blocked", reason="explicit YAML hard price limit exceeded")
        if missing == 0:
            item["incremental_cost_eur"] = "0.00"
        elif price is not None and item["status"] == "matched":
            cost = price * packages
            item["incremental_cost_eur"] = f"{cost:.2f}"
            total += cost
        else:
            cost_known = False
        ingredients.append(item)
    return {
        "recipe_id": snapshot["recipe_id"],
        "revision_id": str(snapshot["revision_id"]),
        "accepted_revision": snapshot["revision_number"],
        "source_servings": str(snapshot["servings"]),
        "offline_observation_generated_at": snapshot["match_observation_generated_at"],
        "target_servings": servings,
        "title": snapshot["title"],
        "protein_class": snapshot["protein_class"],
        "source": snapshot["source"],
        "summary": f"{snapshot['total_time_minutes']} minutes; {servings} full "
        "servings; accepted offline revision",
        "appliances": snapshot["appliances"],
        "appliance_configuration": config.appliances.model_dump(mode="json"),
        "ingredients": ingredients,
        "estimated_incremental_cost_eur": f"{total:.2f}" if cost_known else None,
        "known_incremental_cost_eur": f"{total:.2f}",
        "cost_complete": cost_known,
        "assumptions": [
            "each option preview is independent, not a selected/consolidated dinner",
            "required capabilities/model/capacity checked against persisted policy; "
            "steam oven may satisfy conventional oven, induction may satisfy stovetop, "
            "Thermomix may supply its declared capabilities; no recipe adaptation performed",
            "no preparation steps shown before explicit Stage 2 selection",
        ],
    }
