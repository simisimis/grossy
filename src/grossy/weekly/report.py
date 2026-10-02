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
        lines.append(f"- **Projected total:** EUR {budget.get('projected_total_eur', '0.00')}")
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

    # Errors section.
    errors = data.get("errors", [])
    if errors:
        lines.append("## Errors")
        for err in errors:
            lines.append(f"- {err}")
        lines.append("")

    lines.append("---")
    lines.append(
        "_This is a read-only preview. No cart mutation has occurred. "
        "Availability and prices are from the cart observation only and "
        "do not guarantee live store availability._"
    )
    lines.append("")

    return "\n".join(lines)
