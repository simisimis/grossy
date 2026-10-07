"""Sanitized versioned report generation (PRD section 15, 17.13-14).

Builds the Markdown and JSON reports for a weekly planning run. Human-facing
reports omit source order IDs, profile paths, addresses, account identity,
credentials, cookies, tokens, raw DOM, screenshots, and headers (PRD 15, 17).
"""

from __future__ import annotations

import json
from typing import Any

REPORT_SCHEMA_NAME = "grossy.weekly.report"
REPORT_SCHEMA_VERSION = 1


def build_json_report(data: dict[str, Any]) -> str:
    """Serialize the report data as versioned JSON."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def build_markdown_report(data: dict[str, Any]) -> str:
    """Render the sanitized Markdown report (PRD section 15).

    The report covers:
    1. Run identity, seed, and lifecycle.
    2. History sync status.
    3. Cart baseline.
    4. Recurring items ordered by category (breakfast, lunch, alphabetic dynamic),
       sorted by product name within each category.
    5. Budget components with variance explanation.
    6. Per-item rationale (all items, not just skipped/blocked).
    7. Substitution proposals.
    8. Blocked items and errors.

    No source order IDs, profile paths, addresses, or sensitive data appear
    in the output (PRD 15, 17.13-14).
    """
    lines: list[str] = []
    lines.append("# Grossy Weekly Run Report")
    lines.append("")

    # Run section.
    lines.append("## Run")
    lines.append(f"- **Run ID:** `{data['run_id']}`")
    if data.get("seed"):
        lines.append(f"- **Seed:** `{data['seed']}`")
    lines.append(f"- **Lifecycle:** {data['lifecycle']}")
    lines.append(f"- **Created:** {data.get('created_at', 'unknown')}")
    if data.get("applied_at"):
        lines.append(f"- **Baseline applied:** {data['applied_at']}")
    if data.get("observation_generated_at"):
        lines.append(f"- **Observation timestamp:** {data['observation_generated_at']}")
    lines.append(f"- **History sync:** {data['history_sync_status']}")
    lines.append("")

    # Baseline section.
    lines.append("## Cart Baseline")
    baseline = data.get("baseline", [])
    if baseline:
        lines.append("| Product ID | Quantity | Price (EUR) |")
        lines.append("|---|---|---|")
        for line in baseline:
            pid = line["product_id"]
            qty = line["quantity"]
            price = line.get("price_eur")
            price_str = price if price is not None else "unknown"
            lines.append(f"| {pid} | {qty} | {price_str} |")
        total_qty = sum(line["quantity"] for line in baseline)
        lines.append(f"\n_Total: {len(baseline)} lines, {total_qty} units._")
    else:
        lines.append("_No baseline lines recorded._")
    lines.append("")

    # Recurring items section.
    lines.append("## Recurring Items (Preview)")
    items = data.get("recurring_items", [])
    if items:
        # Group by category for display.
        by_category: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            cat = item["category"]
            by_category.setdefault(cat, []).append(item)

        # Order: breakfast, lunch, then alphabetic.
        def _cat_order(cat: str) -> tuple[int, str]:
            if cat == "breakfast":
                return (0, cat)
            if cat == "lunch":
                return (1, cat)
            return (2, cat)

        for cat in sorted(by_category, key=_cat_order):
            lines.append(f"### {cat}")
            lines.append("")
            # Sort by display name (alphabetic) within category (PRD 15.4).
            cat_items = sorted(
                by_category[cat],
                key=lambda i: (
                    i.get("display_name") or i.get("product_id") or i.get("family_id") or ""
                ).lower(),
            )
            lines.append("| Product | Source | Intended | Credited | Missing | Status |")
            lines.append("|---|---|---|---|---|---|")
            for item in cat_items:
                pid = item.get("product_id") or "(none)"
                src = item["source_type"]
                intended = item["intended_quantity"]
                credited = item["credited_quantity"]
                missing = item["missing_delta"]
                status = item["status"]
                lines.append(f"| {pid} | {src} | {intended} | {credited} | {missing} | {status} |")

            # Per-item rationale (all items, not just skipped/blocked).
            for item in cat_items:
                pid = item.get("product_id") or "(none)"
                rationale = item.get("rationale", "")
                blocked = item.get("blocked_reason")
                if blocked:
                    lines.append(f"- **{pid}** ({item['status']}): {blocked}")
                elif rationale:
                    lines.append(f"- **{pid}** ({item['status']}): {rationale}")

            # Substitution proposals.
            for item in cat_items:
                proposals = item.get("substitution_proposals", [])
                if proposals:
                    pid = item.get("product_id") or "(none)"
                    lines.append(f"- **{pid}** substitution proposals:")
                    for prop in proposals:
                        lines.append(f"  - {prop}")
            lines.append("")
    else:
        lines.append("_No recurring items resolved._")
        lines.append("")

    # Category references.
    items = data.get("recurring_items", [])
    all_refs: set[str] = set()
    for item in items:
        for ref in item.get("category_refs", []):
            all_refs.add(ref)
    if all_refs:
        lines.append("## Category References")
        for ref in sorted(all_refs):
            lines.append(f"- {ref}")
        lines.append("")

    # Budget section.
    lines.append("## Budget")
    budget = data.get("budget", {})
    if budget:
        lines.append(f"- **Baseline subtotal:** EUR {budget.get('baseline_subtotal_eur', '0.00')}")
        lines.append(
            "- **Projected recurring additions:** EUR "
            f"{budget.get('projected_recurring_eur', '0.00')}"
        )
        if "verified_recurring_eur" in budget and "projected_recipe_eur" not in budget:
            lines.append(
                f"- **Verified recurring additions:** EUR {budget['verified_recurring_eur']}"
            )
        if "resulting_total_eur" in budget:
            lines.append(f"- **Resulting visible cart:** EUR {budget['resulting_total_eur']}")
        if "projected_recipe_eur" in budget:
            lines.append(
                f"- **Projected recipe additions:** EUR "
                f"{budget['projected_recipe_eur'] or 'unknown'}"
            )
            lines.append(
                f"- **Verified recurring additions:** EUR {budget['verified_recurring_eur']}"
            )
            lines.append(f"- **Verified recipe additions:** EUR {budget['verified_recipe_eur']}")
            lines.extend(f"- {assumption}" for assumption in budget["assumptions"])
        lines.append(
            f"- **Projected total (known costs):** EUR {budget.get('projected_total_eur', '0.00')}"
        )
        lines.append(f"- **Weekly budget:** EUR {budget.get('weekly_budget_eur', '0.00')}")
        variance = budget.get("variance_eur", "0.00")
        try:
            var_val = float(variance)
            if var_val > 0:
                lines.append(f"- **⚠️ Over budget by:** EUR {variance}")
            elif var_val < 0:
                lines.append(f"- **Within budget by:** EUR {abs(var_val):.2f}")
            else:
                lines.append("- **Exactly at budget.**")
        except (ValueError, TypeError):
            lines.append(f"- **Variance:** EUR {variance}")
        unknown = budget.get("unknown_price_products", [])
        if unknown:
            lines.append(
                f"- **Unknown prices (excluded from projected total):** {', '.join(unknown)}"
            )
    else:
        lines.append("_No budget data._")
    lines.append("")

    # Read-only recipe options; never expose unselected preparation steps.
    if "recipe_slots" in data:
        lines.append("## Recipe Options (configured full servings; read-only)")
        lines.append(f"- Recipe plan persisted: {data['recipe_planned_at']}")
        lines.append(f"- Offline proposal timestamp: {data['recipe_observation_generated_at']}")
        for slot in data["recipe_slots"]:
            lines.append(f"### {slot['slot']} — {slot['protein_class']}")
            preview = slot.get("preview")
            if preview is None:
                lines.append(slot["empty_reason"])
                continue
            lines.append(f"**{preview['title']}** — {preview['summary']}")
            lines.append(
                f"- Accepted revision: {preview['accepted_revision']} (ID {preview['revision_id']})"
            )
            lines.append(f"- Source: {json.dumps(preview['source'], ensure_ascii=False)}")
            lines.append(
                f"- Scaling: source {preview['source_servings']} → "
                f"{preview['target_servings']} full servings"
            )
            lines.append(f"- Appliances: {json.dumps(preview['appliances'], ensure_ascii=False)}")
            lines.append(
                "- Configured appliance metadata: "
                + json.dumps(preview["appliance_configuration"], ensure_ascii=False)
            )
            for ingredient in preview["ingredients"]:
                lines.append(
                    f"- {ingredient['name']}: target {ingredient['target']} {ingredient['unit']}; "
                    f"pantry={ingredient['pantry_covered']}; cart "
                    f"credit={ingredient['cart_credit']}; "
                    f"missing={ingredient['missing_target']}; status={ingredient['status']}; "
                    f"exact match={json.dumps(ingredient['exact_match'], ensure_ascii=False)}; "
                    "approved fallback="
                    f"{json.dumps(ingredient['approved_family_fallbacks'], ensure_ascii=False)}; "
                    f"packages={ingredient['package_quantity']}; incremental EUR "
                    f"{ingredient['incremental_cost_eur'] or 'unknown'}; {ingredient['reason']}"
                )
                lines.append(
                    f"  - Offline availability: {ingredient['availability']}; "
                    f"price EUR {ingredient.get('price_eur') or 'unknown'}; "
                    "evidence: "
                    f"{ingredient.get('evidence', 'local exact identity or unresolved')}; "
                    f"rationale: {ingredient.get('rationale', ingredient['reason'])}"
                )
                lines.append(
                    "  - Recipe package annotation: "
                    + json.dumps(ingredient["recipe_package_annotation"], ensure_ascii=False)
                )
                for assumption in ingredient["assumptions"]:
                    lines.append(f"  - {assumption}")
            lines.append(
                f"- Estimated incremental cost: EUR "
                f"{preview['estimated_incremental_cost_eur'] or 'unknown'}"
            )
            lines.extend(f"- {assumption}" for assumption in preview["assumptions"])
            lines.append("")
        lines.append("## Recipe Eligibility Decisions")
        for recipe_id, reasons in data["recipe_exclusions"].items():
            lines.append(f"- {recipe_id}: {'; '.join(reasons)}")
        lines.append("## Reroll History")
        lines.extend(f"- {json.dumps(r, ensure_ascii=False)}" for r in data["reroll_history"])
        if not data["reroll_history"]:
            lines.append("_No rerolls._")
        lines.append("## Pantry Assumptions")
        lines.extend(f"- {name}" for name in data["pantry_assumptions"])
        lines.append("## Rough Coverage")
        lines.append(json.dumps(data["rough_coverage"], ensure_ascii=False))
        lines.append(data["coverage_assumptions"])
        lines.append("")

    if data.get("selected_dinners"):
        lines.append("## Selected dinners and recipe → dinner mapping")
        for dinner in data["selected_dinners"]:
            lines.append(f"### {dinner['slot']} → {dinner['dinner']} — {dinner['title']}")
            lines.append(f"- Accepted revision: {dinner['revision_id']}")
            lines.extend(f"{n}. {step}" for n, step in enumerate(dinner["preparation_steps"], 1))
        lines.append("## Current recipe preflight decisions and quantities")
        lines.append(
            "- Current pantry policy: " + json.dumps(data.get("recipe_pantry_assumptions", []))
        )
        lines.extend(
            json.dumps(d, ensure_ascii=False, sort_keys=True)
            for d in data.get("recipe_preflight_decisions", [])
        )
        lines.append("## Recipe mutation evidence")
        lines.extend(
            json.dumps(e, ensure_ascii=False, sort_keys=True)
            for e in data.get("recipe_mutation_evidence", [])
        )
        lines.append("")

    # Errors section.
    errors = data.get("errors", [])
    if errors:
        lines.append("## Errors")
        for err in errors:
            lines.append(f"- {err}")
        lines.append("")

    if data.get("recurring_mutation_evidence"):
        lines.append("## Recurring mutation evidence")
        for evidence in data["recurring_mutation_evidence"]:
            lines.append(f"- {json.dumps(evidence, sort_keys=True)}")
        lines.append("")
    if data.get("removal_closure_evidence"):
        lines.append("## Explicit removals and cart-review closure")
        lines.extend(json.dumps(e, sort_keys=True) for e in data["removal_closure_evidence"])
        lines.append("Cart review is not checkout or a completed order. Abandon removes nothing.")
        lines.append("")
    lines.append("---")
    lines.append(
        (
            "_Cart intent/readback evidence is recorded above. "
            if any(
                data.get(key)
                for key in (
                    "recurring_mutation_evidence",
                    "recipe_mutation_evidence",
                    "removal_closure_evidence",
                )
            )
            else "_This is a read-only preview. No cart mutation has occurred. "
        )
        + "Baseline availability and prices come from the cart observation; recipe-match "
        "availability and prices come from unverified offline recipe-options observations. "
        "Neither guarantees current live store availability or prices._"
    )
    lines.append("")

    return "\n".join(lines)
