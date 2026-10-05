"""Deterministic weekly planning phases (PRD FR-113..FR-123, FR-163..FR-176, 15, 18).

``run_plan_prepare`` creates one weekly run in the ``planning`` lifecycle state
with one-active-run enforcement (PRD FR-113, FR-114, 12.1.8) and a lightweight
history-sync check (FR-30, FR-31). ``run_baseline_apply`` validates a
``grossy.cart.observation.v1`` envelope, persists the whole visible cart
baseline by stable product identity (FR-116, FR-117), resolves YAML staples
and due learned families into a normalized intended set with positive-delta
preview (FR-118..FR-123), computes the full-cart budget (FR-171..FR-176),
and writes sanitized versioned Markdown and JSON reports (section 15).

No cart mutation occurs. The run stays in ``planning`` state.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from grossy.browser.observation import (
    CartObservationError,
    validate_cart_observation,
)
from grossy.config.loader import GrossyConfigError, load_config_file
from grossy.config.models import GrossyConfigModel, StapleModel, product_id_from_url
from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import run_migrations
from grossy.fsutil import atomic_write_text, ensure_private_dir
from grossy.history.learn import (
    ATHENS_TZ,
    is_family_due,
    select_family_product,
)
from grossy.history.policy import (
    GATE_BLACKLISTED,
    GATE_DISABLED,
    GATE_FAMILY_BLACKLISTED,
    GATE_UNKNOWN,
    product_gate,
)
from grossy.history.proposal import (
    HistoryProposalError,
    validate_listed_orders,
)
from grossy.paths import GrossyPaths
from grossy.security import sanitize_human_text
from grossy.weekly.report import (
    REPORT_SCHEMA_NAME,
    REPORT_SCHEMA_VERSION,
    build_json_report,
    build_markdown_report,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WeeklyPlanError(Exception):
    """Raised when a weekly planning phase fails."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineLine:
    """One persisted cart baseline line (stable identity, PRD FR-117)."""

    retailer_product_id: str
    canonical_url: str
    quantity: int
    price_cents: int | None  # None when the observation has no valid price


@dataclass(frozen=True)
class RecurringItem:
    """One resolved recurring item for the preview (FR-118..FR-123)."""

    retailer_product_id: str
    family_id: str | None
    category: str  # "breakfast" | "lunch" | dynamic category name
    source_type: str  # "yaml" | "learned" | "both"
    intended_quantity: int
    credited_quantity: int
    missing_delta: int
    status: str  # "pending" | "skipped" | "blocked"
    rationale: str
    category_refs: tuple[str, ...] = field(default=())
    price_cents: int | None = None
    blocked_reason: str | None = None
    substitution_proposals: tuple[str, ...] = field(default=())
    display_name: str = ""  # sanitized product name for ordering (PRD 15.4)
    canonical_url: str = ""  # stable identity URL for persistence


@dataclass(frozen=True)
class BudgetComponents:
    """Exact-money budget components (FR-171..FR-176, PRD 11 currency rule)."""

    baseline_subtotal_cents: int
    projected_recurring_cents: int
    projected_total_cents: int
    weekly_budget_cents: int
    variance_cents: int  # projected_total - budget (positive = over budget)
    unknown_price_products: tuple[str, ...] = field(default=())


# ---------------------------------------------------------------------------
# Sanitization (PRD 15, 17)
# ---------------------------------------------------------------------------


def _sanitize_report_field(value: str, order_ids: set[str]) -> str:
    """Minimize untrusted human report text (PRD 15, 17)."""
    return sanitize_human_text(value, order_ids)


def _get_source_order_ids(conn: sqlite3.Connection) -> set[str]:
    """All known source order IDs for redaction (PRD 15, 17.14)."""
    rows = conn.execute("SELECT source_order_id FROM source_orders").fetchall()
    return {cast(str, r[0]) for r in rows}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _generate_run_id() -> str:
    """A stable, filesystem-safe run identifier."""
    return f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"


def _price_to_cents(price_str: str) -> int | None:
    """Convert a decimal EUR string to integer minor units (cents)."""
    try:
        d = Decimal(price_str)
    except Exception:
        return None
    if not d.is_finite() or d <= 0:
        return None
    return int((d * 100).to_integral_value())


def _cents_to_eur(cents: int) -> str:
    """Format integer minor units as a EUR decimal string."""
    return f"{cents / 100:.2f}"


def _ensure_product(
    conn: sqlite3.Connection,
    retailer_product_id: str,
    canonical_url: str,
    now: str,
) -> int:
    """Look up or create a minimal product record; return the row id."""
    row = conn.execute(
        "SELECT id FROM products WHERE retailer = 'ab' AND retailer_product_id = ?",
        (retailer_product_id,),
    ).fetchone()
    if row is not None:
        return cast(int, row[0])
    cur = conn.execute(
        "INSERT INTO products (retailer, retailer_product_id, canonical_url, name, "
        "purchase_order_count, typical_quantity, blacklist_state, observation_state, "
        "created_at, updated_at) "
        "VALUES ('ab', ?, ?, ?, 0, 1, 'active', 'active', ?, ?)",
        (retailer_product_id, canonical_url, f"unlearned-{retailer_product_id}", now, now),
    )
    return cast(int, cur.lastrowid)


def _product_display_name(conn: sqlite3.Connection, retailer_product_id: str) -> str:
    """Look up the stored product name for display ordering (PRD 15.4)."""
    row = conn.execute(
        "SELECT name FROM products WHERE retailer = 'ab' AND retailer_product_id = ?",
        (retailer_product_id,),
    ).fetchone()
    if row is not None:
        return cast(str, row[0])
    return retailer_product_id


def _check_database_exists(paths: GrossyPaths) -> None:
    """Fail closed when the database does not exist (PRD 18, FR-190).

    Prevents ``run_migrations`` from recreating a deleted user database.
    """
    if not paths.database_file.exists():
        raise WeeklyPlanError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )


def _load_config_strict(paths: GrossyPaths) -> GrossyConfigModel:
    """Load the strict YAML configuration; fail closed on error."""
    try:
        return load_config_file(paths.config_file)
    except GrossyConfigError as exc:
        raise WeeklyPlanError(f"configuration error: {exc}") from exc


def _budget_cents(config: GrossyConfigModel) -> int:
    """Convert the configured weekly budget to integer minor units."""
    return int((config.weekly_budget * 100).to_integral_value())


def _product_price_cents(
    conn: sqlite3.Connection,
    retailer_product_id: str,
    baseline: dict[str, BaselineLine],
) -> int | None:
    """The price in cents for a product, from the cart baseline if present."""
    line = baseline.get(retailer_product_id)
    if line is not None and line.price_cents is not None:
        return line.price_cents
    return None


def _category_sort_key(item: RecurringItem) -> tuple[int, str, str]:
    """Sort order: breakfast=0, lunch=1, dynamic=2 (alphabetic), then product name.

    PRD 15.4: within each category, products are sorted by display name.
    """
    if item.category == "breakfast":
        priority = 0
    elif item.category == "lunch":
        priority = 1
    else:
        priority = 2
    # Sort by display name (alphabetic), falling back to product_id for stability.
    name_key = item.display_name.lower() if item.display_name else item.retailer_product_id
    return (priority, item.category, name_key)


def _approved_family_member_product_ids(conn: sqlite3.Connection, family_id: str) -> frozenset[str]:
    """Approved, non-blacklisted product IDs in a family (for compatible credit).

    Only members with ``approval_state = 'approved'`` are considered (the
    schema supports pending/rejected states, 001_initial.sql:94). Blacklisted
    members are excluded by the policy gate (FR-65, FR-66).
    """
    rows = conn.execute(
        "SELECT p.retailer_product_id "
        "FROM family_members m "
        "JOIN products p ON p.id = m.product_id "
        "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = ?) "
        "AND m.approval_state = 'approved'",
        (family_id,),
    ).fetchall()
    ids: set[str] = set()
    for r in rows:
        pid = cast(str, r[0])
        # Exclude blacklisted members (product or family blacklist).
        if not product_gate(conn, pid).is_blocked:
            ids.add(pid)
    return frozenset(ids)


def _compute_compatible_credit(
    conn: sqlite3.Connection,
    product_id: str,
    family_id: str | None,
    intended_qty: int,
    baseline: dict[str, BaselineLine],
) -> int:
    """Compute cart credit from the selected product and its compatible family members.

    The selected product's quantity is credited first (exact-product credit).
    If the product belongs to a family, remaining approved, non-blacklisted
    family members also contribute their baseline quantity (compatible-family
    credit, FR-120). Total credit is capped at ``intended_qty`` to prevent
    double-crediting.
    """
    credit = 0
    # Exact-product credit.
    line = baseline.get(product_id)
    if line is not None:
        credit += line.quantity
    # Compatible family credit (only for remaining need, approved members only).
    if family_id is not None and credit < intended_qty:
        member_ids = _approved_family_member_product_ids(conn, family_id)
        for member_id in sorted(member_ids):
            if member_id == product_id:
                continue
            member_line = baseline.get(member_id)
            if member_line is not None:
                credit += member_line.quantity
                if credit >= intended_qty:
                    break
    return min(credit, intended_qty)


def _resolve_substitution_proposals(
    conn: sqlite3.Connection,
    staple: StapleModel,
    category: str,
    preferred_id: str,
    order_ids: set[str],
) -> tuple[str, ...]:
    """Resolve approved substitution proposals for a YAML staple (FR-113, FR-123).

    Returns a tuple of sanitized rationale strings for each approved
    alternative that is policy-compliant (not blacklisted). Only called when
    ``staple.allow_substitution`` is True. Does not execute a substitution;
    reports the proposals only. Availability is NOT verified in the preview:
    the proposal states the alternative is approved for substitution without
    claiming it is available at the store (PRD 15: report unknown facts rather
    than inventing live facts).
    """
    proposals: list[str] = []
    for alt_id in staple.approved_alternative_product_ids:
        if alt_id == preferred_id:
            continue
        gate = product_gate(conn, alt_id)
        if gate.is_blocked:
            # Report the blocked alternative with sanitized detail.
            safe_detail = _sanitize_report_field(gate.detail, order_ids)
            proposals.append(
                _sanitize_report_field(
                    f"alternative {alt_id} blocked by policy: {safe_detail}", order_ids
                )
            )
        elif gate.status == GATE_UNKNOWN:
            proposals.append(
                _sanitize_report_field(
                    f"alternative {alt_id} is approved for substitution "
                    "(not in local database; availability not verified in preview)",
                    order_ids,
                )
            )
        else:
            # GATE_OK or GATE_DISABLED: approved and not blocked.
            detail = (
                f"alternative {alt_id} is approved for substitution "
                "(availability not verified in preview)"
            )
            if gate.status == GATE_DISABLED:
                detail += " (family disabled; explicit YAML reference applies)"
            proposals.append(_sanitize_report_field(detail, order_ids))
    return tuple(proposals)


def _check_baseline_already_applied(conn: sqlite3.Connection, run_row_id: int) -> bool:
    """Whether a cart baseline has already been accepted for this run.

    Uses the ``weekly_reports`` table as the persisted acceptance marker:
    it is always written during ``run_baseline_apply`` (even for an empty
    cart), so it correctly guards against re-acceptance regardless of
    baseline line count (review P1: empty-to-empty and empty-to-nonempty).
    """
    row = conn.execute(
        "SELECT 1 FROM weekly_reports WHERE run_id = ? LIMIT 1", (run_row_id,)
    ).fetchone()
    return row is not None


def _family_category_name(conn: sqlite3.Connection, family_id: str) -> str | None:
    """Look up the learned category name for a family (PRD 15.5, 7.3.18).

    Category provenance is resolved independently of recurrence eligibility:
    a YAML-referenced family's learned category is visible even when the
    family is not due or disabled.
    """
    row = conn.execute(
        "SELECT c.name FROM product_families f "
        "LEFT JOIN categories c ON c.id = f.category_id "
        "WHERE f.family_id = ?",
        (family_id,),
    ).fetchone()
    if row is not None and row[0] is not None:
        return cast(str, row[0])
    return None


# ---------------------------------------------------------------------------
# History sync coordination (FR-30, FR-31, FR-9)
# ---------------------------------------------------------------------------


def _check_history_sync(
    conn: sqlite3.Connection,
    listed_orders_path: Path | None,
) -> str:
    """Perform the lightweight listed-ID comparison for history sync (FR-30).

    When a listed-orders proposal path is provided, validates it and computes
    the set difference (listed minus imported, FR-9). If the unknown set is
    empty, history sync is "success" (all listed orders are already imported).
    If the unknown set is non-empty, history sync is "failed" (there are
    un-imported listed orders that must be synchronized before planning).

    Without listed-order evidence, planning fails closed even when local
    purchase history exists. Only an explicit skip bypasses this check.

    Returns the history_sync_status string. Raises WeeklyPlanError on
    validation failure (fail closed, FR-31).
    """
    if listed_orders_path is not None:
        # Load and validate the listed-orders proposal.
        try:
            data = json.loads(listed_orders_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WeeklyPlanError(
                f"cannot read listed-orders proposal {listed_orders_path}: {exc}"
            ) from exc
        try:
            proposal = validate_listed_orders(data)
        except HistoryProposalError as exc:
            raise WeeklyPlanError(f"listed-orders proposal rejected: {exc}") from exc

        # Compute the set difference: listed minus imported (FR-9).
        imported = {
            cast(str, r[0])
            for r in conn.execute(
                "SELECT source_order_id FROM source_orders WHERE retailer = 'ab'"
            ).fetchall()
        }
        listed_ids = {order.source_order_id for order in proposal.orders}
        unknown = listed_ids - imported

        if not unknown:
            return "success"
        # Fail closed: there are un-imported listed orders.
        raise WeeklyPlanError(
            f"history sync incomplete: {len(unknown)} listed order(s) not yet "
            "imported; run 'grossy history sync-apply' first, or use "
            "--skip-history-sync to explicitly skip the check"
        )

    raise WeeklyPlanError(
        "history comparison requires --listed-orders evidence; synchronize the "
        "currently listed orders first, or use --skip-history-sync explicitly"
    )


# ---------------------------------------------------------------------------
# YAML staple resolution
# ---------------------------------------------------------------------------


def _resolve_yaml_staple(
    conn: sqlite3.Connection,
    config: GrossyConfigModel,
    staple: StapleModel,
    category: str,
    baseline: dict[str, BaselineLine],
    order_ids: set[str],
) -> list[RecurringItem]:
    """Resolve one YAML staple into recurring items (FR-118, FR-119).

    Returns a list with one item for the preferred product and (if
    ``allow_substitution`` is True) substitution proposals for approved
    alternatives. Blocked items are included with status ``"blocked"``.
    """
    # Determine the preferred product ID.
    product_id: str | None = staple.product_id
    product_url: str | None = staple.product_url
    if product_id is None and staple.product_url is not None:
        product_id = product_id_from_url(staple.product_url)
    if product_id is None:
        return []

    # Resolve the policy gate.
    gate = product_gate(conn, product_id)

    # Look up the product's family for consolidation tracking.
    family_row = conn.execute(
        "SELECT f.family_id "
        "FROM family_members fm "
        "JOIN product_families f ON f.id = fm.family_id "
        "WHERE fm.product_id = (SELECT id FROM products WHERE retailer_product_id = ?)",
        (product_id,),
    ).fetchone()
    family_id: str | None = cast("str | None", family_row[0]) if family_row else None

    # Build category refs: the YAML category plus the learned family category
    # (PRD 15.5, 7.3.18: contributing references remain visible independently
    # of recurrence eligibility).
    category_refs: list[str] = [category]
    if family_id is not None:
        learned_cat = _family_category_name(conn, family_id)
        if learned_cat is not None and learned_cat != category:
            category_refs.append(_sanitize_report_field(learned_cat, order_ids))

    # Look up the display name for ordering (PRD 15.4).
    display_name = _product_display_name(conn, product_id)
    safe_display = _sanitize_report_field(display_name, order_ids)

    # Check hard limits.
    price_cents = _product_price_cents(conn, product_id, baseline)
    blocked_reason: str | None = None
    if staple.max_product_price_eur is not None:
        limit_cents = int((staple.max_product_price_eur * 100).to_integral_value())
        if price_cents is not None and price_cents > limit_cents:
            blocked_reason = (
                f"product price {_cents_to_eur(price_cents)} EUR exceeds "
                f"max_product_price_eur {_cents_to_eur(limit_cents)} EUR"
            )
    if blocked_reason is None and staple.max_line_price_eur is not None and price_cents is not None:
        line_limit_cents = int((staple.max_line_price_eur * 100).to_integral_value())
        line_cents = price_cents * staple.quantity
        if line_cents > line_limit_cents:
            blocked_reason = (
                f"line price {_cents_to_eur(line_cents)} EUR exceeds "
                f"max_line_price_eur {_cents_to_eur(line_limit_cents)} EUR"
            )

    # Blacklist block (FR-65, FR-66).
    if gate.status in (GATE_BLACKLISTED, GATE_FAMILY_BLACKLISTED):
        blocked_reason = f"blocked by blacklist: {gate.detail}"

    if blocked_reason is not None:
        return [
            RecurringItem(
                retailer_product_id=product_id,
                family_id=family_id,
                category=category,
                source_type="yaml",
                intended_quantity=staple.quantity,
                credited_quantity=0,
                missing_delta=0,
                status="blocked",
                rationale=_sanitize_report_field(
                    f"YAML staple {staple.key!r} ({category}): {blocked_reason}", order_ids
                ),
                category_refs=tuple(category_refs),
                price_cents=price_cents,
                blocked_reason=_sanitize_report_field(blocked_reason, order_ids),
                display_name=safe_display,
                canonical_url=product_url or "",
            )
        ]

    # Cart credit (FR-120, FR-121): exact + compatible-family credit.
    credited = _compute_compatible_credit(conn, product_id, family_id, staple.quantity, baseline)
    missing = max(0, staple.quantity - credited)

    status = "pending" if missing > 0 else "skipped"
    rationale_parts = [f"YAML staple {staple.key!r} ({category})"]
    if gate.status == GATE_DISABLED:
        rationale_parts.append("family disabled but explicit YAML reference applies (FR-63)")
    if credited > 0:
        rationale_parts.append(f"credited {credited} from cart")
    if missing > 0:
        rationale_parts.append(f"missing delta {missing}")
    else:
        rationale_parts.append("satisfied; retained excess preserved")

    # Substitution proposals (FR-113, FR-123): proposals only, no execution.
    substitution_proposals: tuple[str, ...] = ()
    if staple.allow_substitution and staple.approved_alternative_product_ids:
        substitution_proposals = _resolve_substitution_proposals(
            conn, staple, category, product_id, order_ids
        )

    return [
        RecurringItem(
            retailer_product_id=product_id,
            family_id=family_id,
            category=category,
            source_type="yaml",
            intended_quantity=staple.quantity,
            credited_quantity=credited,
            missing_delta=missing,
            status=status,
            rationale=_sanitize_report_field("; ".join(rationale_parts), order_ids),
            category_refs=tuple(category_refs),
            price_cents=price_cents,
            substitution_proposals=substitution_proposals,
            display_name=safe_display,
            canonical_url=product_url or "",
        )
    ]


# ---------------------------------------------------------------------------
# Learned family resolution
# ---------------------------------------------------------------------------

# Recurrence sources that are eligible for auto-add (FR-52..FR-54, FR-45).
_ELIGIBLE_SOURCES = frozenset({"observed", "manual"})


def _resolve_learned_families(
    conn: sqlite3.Connection,
    yaml_family_ids: set[str],
    baseline: dict[str, BaselineLine],
    order_ids: set[str],
    today: datetime | None = None,
) -> list[RecurringItem]:
    """Resolve due learned families into recurring items (FR-49..FR-54, FR-60..FR-62).

    Families that are covered by a YAML staple are still resolved here so
    ``_consolidate`` can merge them (FR-119). Confidence gating (FR-52..FR-54)
    prevents single-occurrence low/medium-confidence estimates from being
    auto-added. Manual recurrence corrections (FR-45) are always eligible.

    Category refs are resolved independently of recurrence eligibility: even
    a not-due or disabled family contributes its category ref when covered by
    a YAML staple (PRD 15.5, 7.3.18).
    """
    items: list[RecurringItem] = []
    if today is None:
        today = datetime.now(ATHENS_TZ)

    # Query all families (including disabled and not-due) so category refs
    # are resolved independently of eligibility (PRD 15.5, 7.3.18).
    rows = conn.execute(
        "SELECT f.family_id, f.last_purchase_date, f.recurrence_weeks, "
        "f.recurrence_source, f.recurrence_confidence, f.category_id, f.enabled, "
        "f.blacklist_state, c.name as category_name "
        "FROM product_families f "
        "LEFT JOIN categories c ON c.id = f.category_id "
        "WHERE f.last_purchase_date IS NOT NULL AND f.recurrence_weeks IS NOT NULL"
    ).fetchall()

    for (
        family_id_text,
        last_purchase,
        recurrence_weeks,
        recurrence_source,
        recurrence_conf,
        _category_row_id,
        enabled,
        _blacklist,
        category_name,
    ) in rows:
        family_id_str = cast(str, family_id_text)
        category = cast(str, category_name) or "(uncategorized)"
        cat_ref = _sanitize_report_field(category, order_ids)
        is_enabled = cast(int, enabled) == 1

        # For YAML-covered families that are not eligible (disabled, not-due,
        # low-confidence), emit a lightweight entry with the category ref so
        # consolidation can merge it (PRD 15.5, 7.3.18).
        is_yaml_covered = family_id_str in yaml_family_ids
        is_due = is_family_due(cast(str, last_purchase), cast(int, recurrence_weeks), today.date())

        if not is_enabled:
            if is_yaml_covered:
                # Emit a skipped entry with category ref for consolidation.
                items.append(
                    RecurringItem(
                        retailer_product_id="",
                        family_id=family_id_str,
                        category=cat_ref,
                        source_type="learned",
                        intended_quantity=0,
                        credited_quantity=0,
                        missing_delta=0,
                        status="skipped",
                        rationale=_sanitize_report_field(
                            f"family {family_id_str}: disabled (FR-63); "
                            "category ref preserved for YAML consolidation",
                            order_ids,
                        ),
                        category_refs=(cat_ref,),
                    )
                )
            continue

        if not is_due:
            if is_yaml_covered:
                # Emit a skipped entry with category ref for consolidation.
                items.append(
                    RecurringItem(
                        retailer_product_id="",
                        family_id=family_id_str,
                        category=cat_ref,
                        source_type="learned",
                        intended_quantity=0,
                        credited_quantity=0,
                        missing_delta=0,
                        status="skipped",
                        rationale=_sanitize_report_field(
                            f"family {family_id_str}: not due; "
                            "category ref preserved for YAML consolidation",
                            order_ids,
                        ),
                        category_refs=(cat_ref,),
                    )
                )
            continue

        # Confidence gate (FR-52..FR-54, FR-45).
        source = cast(str | None, recurrence_source)
        conf = cast(str | None, recurrence_conf)
        eligible = False
        if source in _ELIGIBLE_SOURCES:
            eligible = True
            if source == "manual":
                conf_rationale = f"manual recurrence correction ({cast(int, recurrence_weeks)}w)"
            else:
                conf_rationale = f"observed recurrence ({cast(int, recurrence_weeks)}w)"
        elif source == "category-estimate":
            eligible = conf == "high"
            conf_rationale = (
                f"category-estimate ({cast(int, recurrence_weeks)}w, {conf} confidence)"
            )
        else:
            # No recurrence data or unknown source: report but don't add.
            items.append(
                RecurringItem(
                    retailer_product_id="",
                    family_id=family_id_str,
                    category=cat_ref,
                    source_type="learned",
                    intended_quantity=0,
                    credited_quantity=0,
                    missing_delta=0,
                    status="skipped",
                    rationale=_sanitize_report_field(
                        f"family {family_id_str}: no eligible recurrence data", order_ids
                    ),
                    category_refs=(cat_ref,),
                )
            )
            continue

        if not eligible:
            items.append(
                RecurringItem(
                    retailer_product_id="",
                    family_id=family_id_str,
                    category=cat_ref,
                    source_type="learned",
                    intended_quantity=0,
                    credited_quantity=0,
                    missing_delta=0,
                    status="skipped",
                    rationale=_sanitize_report_field(
                        f"family {family_id_str}: {conf_rationale}; not auto-add eligible (FR-54)",
                        order_ids,
                    ),
                    category_refs=(cat_ref,),
                )
            )
            continue

        # Select the product (primary first, FR-60).
        # For the preview, the "available" set is the approved family members:
        # this selects the intended product for the preview. It does NOT assert
        # store availability (the cart observation only shows current cart
        # contents, not store inventory). The report states "intended product"
        # rather than "available product" (PRD 15: report unknown facts).
        member_ids = _approved_family_member_product_ids(conn, family_id_str)
        if not member_ids:
            items.append(
                RecurringItem(
                    retailer_product_id="",
                    family_id=family_id_str,
                    category=cat_ref,
                    source_type="learned",
                    intended_quantity=0,
                    credited_quantity=0,
                    missing_delta=0,
                    status="skipped",
                    rationale=_sanitize_report_field(
                        f"family {family_id_str}: no approved member eligible for selection",
                        order_ids,
                    ),
                    category_refs=(cat_ref,),
                )
            )
            continue

        selection = select_family_product(conn, family_id_str, member_ids)
        if selection is None:
            items.append(
                RecurringItem(
                    retailer_product_id="",
                    family_id=family_id_str,
                    category=cat_ref,
                    source_type="learned",
                    intended_quantity=0,
                    credited_quantity=0,
                    missing_delta=0,
                    status="skipped",
                    rationale=_sanitize_report_field(
                        f"family {family_id_str}: no available approved member", order_ids
                    ),
                    category_refs=(cat_ref,),
                )
            )
            continue

        product_id = selection.retailer_product_id

        # Get the product's typical quantity.
        prod_row = conn.execute(
            "SELECT typical_quantity FROM products WHERE retailer_product_id = ?",
            (product_id,),
        ).fetchone()
        typical_qty = cast(int, prod_row[0]) if prod_row else 1

        # Compatible cart credit (FR-120): exact + family member credit.
        credited = _compute_compatible_credit(
            conn, product_id, family_id_str, typical_qty, baseline
        )
        missing = max(0, typical_qty - credited)
        price_cents = _product_price_cents(conn, product_id, baseline)

        # Display name for ordering.
        display_name = _product_display_name(conn, product_id)
        safe_display = _sanitize_report_field(display_name, order_ids)

        status = "pending" if missing > 0 else "skipped"
        rationale_parts = [f"learned family {family_id_str}; {conf_rationale}"]
        if selection.role == "alternative":
            rationale_parts.append(
                "alternative selected (primary not eligible for automatic selection)"
            )
        if credited > 0:
            rationale_parts.append(f"credited {credited} from cart")
        if missing > 0:
            rationale_parts.append(f"missing delta {missing}")
        else:
            rationale_parts.append("satisfied; retained excess preserved")

        items.append(
            RecurringItem(
                retailer_product_id=product_id,
                family_id=family_id_str,
                category=cat_ref,
                source_type="learned",
                intended_quantity=typical_qty,
                credited_quantity=credited,
                missing_delta=missing,
                status=status,
                rationale=_sanitize_report_field("; ".join(rationale_parts), order_ids),
                category_refs=(cat_ref,),
                price_cents=price_cents,
                display_name=safe_display,
            )
        )

    return items


# ---------------------------------------------------------------------------
# Consolidation (FR-119, FR-17)
# ---------------------------------------------------------------------------


def _consolidate(
    yaml_items: list[RecurringItem],
    learned_items: list[RecurringItem],
) -> list[RecurringItem]:
    """Merge YAML and learned items for the same family (FR-119, FR-17).

    When a YAML staple and a learned family refer to the same family, the YAML
    product and quantity win (FR-119). All category references from both
    sources are retained (FR-17). The consolidated item has
    ``source_type = "both"``.

    Multiple YAML entries in the same family are normalized: the first entry
    wins for product/quantity, but if ANY entry in the family is blocked, the
    consolidated item is blocked (review P1: later explicit policy conflicts
    are preserved, not silently dropped).
    """
    # Index YAML items by family_id (first entry wins for product/quantity).
    yaml_by_family: dict[str, RecurringItem] = {}
    yaml_family_order: list[str] = []
    yaml_family_blocked: dict[str, str | None] = {}
    standalone_yaml: dict[str, RecurringItem] = {}
    standalone_yaml_order: list[str] = []

    for item in yaml_items:
        if item.family_id is not None:
            fid = item.family_id
            if fid not in yaml_by_family:
                yaml_by_family[fid] = item
                yaml_family_order.append(fid)
                yaml_family_blocked[fid] = item.blocked_reason
            else:
                # Merge category refs from the second (and later) YAML entry.
                existing = yaml_by_family[fid]
                merged_refs = tuple(dict.fromkeys(existing.category_refs + item.category_refs))
                yaml_by_family[fid] = RecurringItem(
                    retailer_product_id=existing.retailer_product_id,
                    family_id=existing.family_id,
                    category=existing.category,
                    source_type=existing.source_type,
                    intended_quantity=existing.intended_quantity,
                    credited_quantity=existing.credited_quantity,
                    missing_delta=existing.missing_delta,
                    status=existing.status,
                    rationale=existing.rationale,
                    category_refs=merged_refs,
                    price_cents=existing.price_cents,
                    blocked_reason=existing.blocked_reason,
                    substitution_proposals=existing.substitution_proposals,
                    display_name=existing.display_name,
                    canonical_url=existing.canonical_url,
                )
                # Preserve blocked status from later entries (review P1).
                if item.blocked_reason is not None:
                    yaml_family_blocked[fid] = item.blocked_reason
        else:
            key = item.retailer_product_id
            if key not in standalone_yaml:
                standalone_yaml[key] = item
                standalone_yaml_order.append(key)
            else:
                # Merge category refs from the repeated YAML entry.
                existing = standalone_yaml[key]
                merged_refs = tuple(dict.fromkeys(existing.category_refs + item.category_refs))
                standalone_yaml[key] = RecurringItem(
                    retailer_product_id=existing.retailer_product_id,
                    family_id=None,
                    category=existing.category,
                    source_type=existing.source_type,
                    intended_quantity=existing.intended_quantity,
                    credited_quantity=existing.credited_quantity,
                    missing_delta=existing.missing_delta,
                    status="blocked" if item.blocked_reason else existing.status,
                    rationale=existing.rationale
                    + (
                        " (blocked by later YAML reference to same product)"
                        if item.blocked_reason
                        else ""
                    ),
                    category_refs=merged_refs,
                    price_cents=existing.price_cents,
                    blocked_reason=existing.blocked_reason or item.blocked_reason,
                    substitution_proposals=existing.substitution_proposals,
                    display_name=existing.display_name,
                    canonical_url=existing.canonical_url,
                )

    result: list[RecurringItem] = []
    consumed_learned: set[str] = set()

    # Process learned items: consolidate with YAML if the family matches.
    for learned in learned_items:
        if learned.family_id is None:
            result.append(learned)
            continue
        if learned.family_id in yaml_by_family:
            # Consolidate: YAML wins (FR-119), merge category refs (FR-17).
            yaml_item = yaml_by_family[learned.family_id]
            merged_refs = tuple(dict.fromkeys(yaml_item.category_refs + learned.category_refs))
            # Check if any YAML entry in this family was blocked (review P1).
            blocked_reason = yaml_family_blocked.get(learned.family_id)
            if blocked_reason is not None:
                status = "blocked"
                rationale = (
                    yaml_item.rationale + " (blocked by later YAML reference in same family)"
                )
            else:
                status = yaml_item.status
                rationale = yaml_item.rationale + " (consolidated with learned family)"
            result.append(
                RecurringItem(
                    retailer_product_id=yaml_item.retailer_product_id,
                    family_id=yaml_item.family_id,
                    category=yaml_item.category,
                    source_type="both",
                    intended_quantity=yaml_item.intended_quantity,
                    credited_quantity=yaml_item.credited_quantity,
                    missing_delta=yaml_item.missing_delta,
                    status=status,
                    rationale=rationale,
                    category_refs=merged_refs,
                    price_cents=yaml_item.price_cents,
                    blocked_reason=blocked_reason,
                    substitution_proposals=yaml_item.substitution_proposals,
                    display_name=yaml_item.display_name,
                    canonical_url=yaml_item.canonical_url,
                )
            )
            consumed_learned.add(learned.family_id)
        else:
            result.append(learned)

    # Add consolidated YAML family items (already in result via learned merge).
    # Add YAML family items whose family was NOT in the learned set.
    for fid in yaml_family_order:
        if fid not in consumed_learned:
            item = yaml_by_family[fid]
            # Apply blocked status from later entries (review P1).
            blocked_reason = yaml_family_blocked.get(fid)
            if blocked_reason is not None and item.status != "blocked":
                item = RecurringItem(
                    retailer_product_id=item.retailer_product_id,
                    family_id=item.family_id,
                    category=item.category,
                    source_type=item.source_type,
                    intended_quantity=item.intended_quantity,
                    credited_quantity=item.credited_quantity,
                    missing_delta=item.missing_delta,
                    status="blocked",
                    rationale=item.rationale + " (blocked by later YAML reference in same family)",
                    category_refs=item.category_refs,
                    price_cents=item.price_cents,
                    blocked_reason=blocked_reason,
                    substitution_proposals=item.substitution_proposals,
                    display_name=item.display_name,
                    canonical_url=item.canonical_url,
                )
            result.append(item)

    # Add standalone YAML items (deduplicated by product ID).
    for key in standalone_yaml_order:
        result.append(standalone_yaml[key])

    return result


# ---------------------------------------------------------------------------
# Budget calculation (FR-171..FR-176)
# ---------------------------------------------------------------------------


def _compute_budget(
    baseline_lines: list[BaselineLine],
    recurring_items: list[RecurringItem],
    weekly_budget_cents: int,
) -> BudgetComponents:
    """Compute exact-money budget components (FR-171..FR-176)."""
    baseline_subtotal = sum(line.quantity * (line.price_cents or 0) for line in baseline_lines)

    projected_recurring = 0
    unknown_prices: list[str] = []
    for item in recurring_items:
        if item.missing_delta > 0 and item.status == "pending":
            if item.price_cents is not None:
                projected_recurring += item.missing_delta * item.price_cents
            else:
                unknown_prices.append(item.retailer_product_id)

    projected_total = baseline_subtotal + projected_recurring
    variance = projected_total - weekly_budget_cents

    return BudgetComponents(
        baseline_subtotal_cents=baseline_subtotal,
        projected_recurring_cents=projected_recurring,
        projected_total_cents=projected_total,
        weekly_budget_cents=weekly_budget_cents,
        variance_cents=variance,
        unknown_price_products=tuple(unknown_prices),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_baseline(
    conn: sqlite3.Connection,
    run_row_id: int,
    baseline_lines: list[BaselineLine],
    now: str,
) -> None:
    """Persist the cart baseline lines (FR-116)."""
    for line in baseline_lines:
        product_row_id = _ensure_product(conn, line.retailer_product_id, line.canonical_url, now)
        conn.execute(
            "INSERT OR REPLACE INTO run_cart_baselines "
            "(run_id, product_id, quantity, price_cents, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_row_id, product_row_id, line.quantity, line.price_cents, now),
        )


def _persist_recurring_items(
    conn: sqlite3.Connection,
    run_row_id: int,
    items: list[RecurringItem],
    now: str,
) -> None:
    """Persist the recurring items for the run (FR-118..FR-123).

    Items without a valid product ID (e.g., skipped families with no selected
    product) are NOT persisted to ``run_recurring_items``: the schema requires
    a non-NULL product reference and a positive intended_quantity, and
    manufacturing a placeholder product row would create a fictitious retailer
    catalogue identity (review P1). These items are still visible in the
    sanitized report (JSON/Markdown) where they carry their family_id and
    rationale.
    """
    for item in items:
        if not item.retailer_product_id:
            # No valid product: skip persistence (report-only item).
            continue
        if item.intended_quantity <= 0:
            # Zero-quantity items are report-only (skipped, not pending).
            continue
        product_row_id = _ensure_product(
            conn,
            item.retailer_product_id,
            item.canonical_url,  # preserve supplied identity (review P2)
            now,
        )
        family_row_id = None
        if item.family_id:
            fam_row = conn.execute(
                "SELECT id FROM product_families WHERE family_id = ?",
                (item.family_id,),
            ).fetchone()
            family_row_id = cast("int | None", fam_row[0]) if fam_row else None
        conn.execute(
            "INSERT INTO run_recurring_items "
            "(run_id, product_id, family_id, category, source_type, "
            "intended_quantity, credited_quantity, missing_delta, status, "
            "rationale, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_row_id,
                product_row_id,
                family_row_id,
                item.category,
                item.source_type,
                item.intended_quantity,
                item.credited_quantity,
                item.missing_delta,
                item.status,
                item.rationale,
                now,
            ),
        )


def _persist_report_recovery(
    conn: sqlite3.Connection,
    run_row_id: int,
    report_md: str,
    report_json: str,
    report_md_path: str,
    report_json_path: str,
    observation_generated_at: str | None,
    now: str,
) -> None:
    """Persist the rendered report texts in the DB for recovery (PRD 18)."""
    conn.execute(
        "INSERT INTO weekly_reports "
        "(run_id, report_md, report_json, report_md_path, report_json_path, "
        "report_written, observation_generated_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET "
        "report_md = excluded.report_md, "
        "report_json = excluded.report_json, "
        "report_md_path = excluded.report_md_path, "
        "report_json_path = excluded.report_json_path, "
        "report_written = 0, "
        "observation_generated_at = excluded.observation_generated_at, "
        "updated_at = excluded.updated_at",
        (
            run_row_id,
            report_md,
            report_json,
            report_md_path,
            report_json_path,
            observation_generated_at,
            now,
            now,
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_plan_prepare(
    paths: GrossyPaths,
    skip_history_sync: bool = False,
    listed_orders_path: Path | None = None,
) -> dict[str, Any]:
    """Create a weekly run in the ``planning`` lifecycle state.

    Enforces one-active-run (PRD FR-113, 12.1.8). Performs the lightweight
    history-sync check unless ``skip_history_sync`` is True (FR-30): when
    ``listed_orders_path`` is provided, validates the proposal and computes
    the listed-minus-imported set difference (FR-9); a non-empty unknown set
    fails closed (FR-31: sync failure stops before cart mutation). When no
    listed-orders path is provided, fails closed; imported history alone
    cannot prove a current listed-ID comparison.

    Returns a versioned JSON-serializable dict on success.
    Raises :class:`WeeklyPlanError` on failure.
    """
    # Fail closed before any state-changing operation (PRD 18, FR-190).
    _check_database_exists(paths)

    # Load and validate config before migrations so invalid YAML cannot
    # change migration state before rejection.
    config = _load_config_strict(paths)
    budget = _budget_cents(config)

    ensure_private_dir(paths.data_dir)
    ensure_private_dir(paths.state_dir)

    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            # Lightweight history check (FR-30, FR-31).
            if skip_history_sync:
                history_status = "skipped"
            else:
                history_status = _check_history_sync(conn, listed_orders_path)

            run_id = _generate_run_id()
            now = _now_utc()

            try:
                cur = conn.execute(
                    "INSERT INTO planner_runs "
                    "(run_id, seed, lifecycle, history_sync_status, budget_eur_cents, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, 'planning', ?, ?, ?, ?)",
                    (run_id, run_id, history_status, budget, now, now),
                )
            except sqlite3.IntegrityError as exc:
                if "one_active_run_violation" in str(exc):
                    raise WeeklyPlanError(
                        "a weekly run is already active; complete or abandon it first "
                        "(PRD FR-113: only one active run is permitted)"
                    ) from exc
                raise
            run_row_id = cast(int, cur.lastrowid)

            conn.execute(
                "INSERT INTO run_events (run_id, event_type, detail, created_at) "
                "VALUES (?, 'run-created', ?, ?)",
                (
                    run_row_id,
                    (
                        "run created; lifecycle=planning; "
                        f"history_sync_status={history_status}; "
                        f"budget={_cents_to_eur(budget)} EUR"
                    ),
                    now,
                ),
            )
            conn.commit()

            # Create the run directory.
            run_dir = ensure_private_dir(paths.run_dir(run_id))

            return {
                "schema_name": "grossy.weekly.plan-prepare",
                "schema_version": 1,
                "status": "success",
                "run_id": run_id,
                "lifecycle": "planning",
                "history_sync_status": history_status,
                "budget_eur": _cents_to_eur(budget),
                "run_dir": str(run_dir),
            }
        finally:
            conn.close()


def run_baseline_apply(
    paths: GrossyPaths,
    run_id: str,
    observation_data: dict[str, Any],
) -> dict[str, Any]:
    """Validate and persist the cart baseline, compute the recurring preview.

    Steps (PRD FR-116..FR-123, FR-163..FR-176, section 15):
    1. Verify the database exists and the run is in ``planning`` state.
    2. Gate on history sync status (reject 'failed' and 'pending').
    3. Validate the ``grossy.cart.observation.v1`` envelope.
    4. Check for idempotent retry (baseline already applied, using the
       persisted acceptance marker in ``weekly_reports``).
    5. Persist the whole visible cart baseline by stable product identity.
    6. Resolve YAML staples and due learned families with precedence.
    7. Compute positive-delta preview, budget, and hard limits.
    8. Persist report recovery data, commit, then write report files.

    No cart mutation occurs. The run stays in ``planning`` state.
    Raises :class:`WeeklyPlanError` on failure.
    Returns a dict with ``status="error"`` when report I/O fails (PRD 10.3.1).
    """
    # Fail closed before any state-changing operation (PRD 18, FR-190).
    _check_database_exists(paths)
    config = _load_config_strict(paths)

    ensure_private_dir(paths.data_dir)
    ensure_private_dir(paths.state_dir)

    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            # 1. Verify the run.
            run_row = conn.execute(
                "SELECT id, lifecycle, history_sync_status, budget_eur_cents, "
                "seed, created_at "
                "FROM planner_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise WeeklyPlanError(f"unknown run ID: {run_id!r}")
            run_row_id = cast(int, run_row[0])
            lifecycle = cast(str, run_row[1])
            history_status = cast("str | None", run_row[2])
            budget_cents = cast(int, run_row[3])
            run_seed = cast(str, run_row[4])
            run_created_at = cast(str, run_row[5])

            if lifecycle != "planning":
                raise WeeklyPlanError(
                    f"run {run_id!r} is in lifecycle {lifecycle!r}; "
                    "baseline-apply requires the 'planning' state"
                )

            # 2. Gate on history sync status (FR-30, FR-31).
            # Allow only verified success or an explicit skip, including for
            # nullable predecessor rows.
            if history_status not in ("success", "skipped"):
                raise WeeklyPlanError(
                    f"run {run_id!r} has history_sync_status={history_status!r}; "
                    "a required synchronization failure or unverified state "
                    "stops planning (FR-31). Only 'success' or 'skipped' "
                    "are acceptable."
                )

            # 3. Validate the cart observation.
            try:
                observation = validate_cart_observation(observation_data)
            except CartObservationError as exc:
                raise WeeklyPlanError(f"cart observation validation failed: {exc}") from exc

            now = _now_utc()

            # 4. Idempotent retry check: reject if baseline already applied
            # with different content (immutable baseline acceptance).
            # Uses the weekly_reports table as the acceptance marker (review P1:
            # works even for empty baselines that create no run_cart_baselines rows).
            if _check_baseline_already_applied(conn, run_row_id):
                raise WeeklyPlanError(
                    f"run {run_id!r} already has a persisted cart baseline; "
                    "baseline acceptance is immutable. Use a new run for a "
                    "different cart observation."
                )

            # 5. Persist the baseline (FR-116, FR-117).
            baseline_lines: list[BaselineLine] = []
            for line in observation.lines:
                price_cents = _price_to_cents(line.displayed_price_eur)
                baseline_lines.append(
                    BaselineLine(
                        retailer_product_id=line.product_id,
                        canonical_url=line.canonical_url,
                        quantity=line.quantity,
                        price_cents=price_cents,
                    )
                )
            _persist_baseline(conn, run_row_id, baseline_lines, now)
            baseline_by_pid: dict[str, BaselineLine] = {
                line.retailer_product_id: line for line in baseline_lines
            }

            # 6. Resolve recurring items.
            order_ids = _get_source_order_ids(conn)

            # YAML staples.
            yaml_items: list[RecurringItem] = []
            yaml_family_ids: set[str] = set()
            for category in ("breakfast", "lunch"):
                for staple in getattr(config.staples, category):
                    items = _resolve_yaml_staple(
                        conn, config, staple, category, baseline_by_pid, order_ids
                    )
                    yaml_items.extend(items)
                    for item in items:
                        if item.family_id is not None:
                            yaml_family_ids.add(item.family_id)

            # Learned families (includes YAML-covered families for consolidation).
            learned_items = _resolve_learned_families(
                conn, yaml_family_ids, baseline_by_pid, order_ids
            )

            # Consolidate (FR-119, FR-17).
            recurring_items = _consolidate(yaml_items, learned_items)

            # Sort by category order (breakfast, lunch, alphabetic dynamic, product name).
            recurring_items.sort(key=_category_sort_key)

            # 7. Compute budget.
            budget = _compute_budget(baseline_lines, recurring_items, budget_cents)

            # Persist recurring items (only those with valid product references).
            _persist_recurring_items(conn, run_row_id, recurring_items, now)

            # 8. Generate reports (section 15).
            report_md_path = paths.run_dir(run_id) / "report.md"
            report_json_path = paths.run_dir(run_id) / "report.json"

            # Sanitize family_id in report data (review P1: raw family_id
            # could contain sensitive content if untrusted).
            report_data = {
                "schema_name": REPORT_SCHEMA_NAME,
                "schema_version": REPORT_SCHEMA_VERSION,
                "run_id": run_id,
                "seed": run_seed,
                "lifecycle": "planning",
                "history_sync_status": history_status,
                "created_at": run_created_at,
                "applied_at": now,
                "observation_generated_at": observation.generated_at,
                "baseline": [
                    {
                        "product_id": line.retailer_product_id,
                        "quantity": line.quantity,
                        "price_eur": _cents_to_eur(line.price_cents)
                        if line.price_cents is not None
                        else None,
                    }
                    for line in baseline_lines
                ],
                "recurring_items": [
                    {
                        "product_id": item.retailer_product_id or None,
                        "family_id": _sanitize_report_field(item.family_id, order_ids)
                        if item.family_id
                        else None,
                        "category": item.category,
                        "source_type": item.source_type,
                        "intended_quantity": item.intended_quantity,
                        "credited_quantity": item.credited_quantity,
                        "missing_delta": item.missing_delta,
                        "status": item.status,
                        "rationale": item.rationale,
                        "category_refs": list(item.category_refs),
                        "blocked_reason": item.blocked_reason,
                        "substitution_proposals": list(item.substitution_proposals),
                        "display_name": item.display_name or None,
                    }
                    for item in recurring_items
                ],
                "budget": {
                    "baseline_subtotal_eur": _cents_to_eur(budget.baseline_subtotal_cents),
                    "projected_recurring_eur": _cents_to_eur(budget.projected_recurring_cents),
                    "projected_total_eur": _cents_to_eur(budget.projected_total_cents),
                    "weekly_budget_eur": _cents_to_eur(budget.weekly_budget_cents),
                    "variance_eur": _cents_to_eur(budget.variance_cents),
                    "unknown_price_products": list(budget.unknown_price_products),
                },
                "errors": [],
            }

            md_text = build_markdown_report(report_data)
            json_text = build_json_report(report_data)

            # 9. Persist report recovery data in the same transaction (PRD 18).
            _persist_report_recovery(
                conn,
                run_row_id,
                md_text,
                json_text,
                str(report_md_path),
                str(report_json_path),
                observation.generated_at,
                now,
            )

            conn.execute(
                "INSERT INTO run_events (run_id, event_type, detail, created_at) "
                "VALUES (?, 'baseline-applied', ?, ?)",
                (
                    run_row_id,
                    (
                        f"baseline lines={len(baseline_lines)}; "
                        f"recurring items={len(recurring_items)}; "
                        f"projected total={_cents_to_eur(budget.projected_total_cents)} EUR"
                    ),
                    now,
                ),
            )

            # Commit truth first (PRD 18).
            conn.commit()

            # 10. Write report files after commit (PRD 18).
            report_failed = False
            report_errors: list[str] = []
            try:
                atomic_write_text(report_md_path, md_text)
                atomic_write_text(report_json_path, json_text)
            except OSError as exc:
                report_failed = True
                report_errors.append(
                    f"report write failed: {exc}; SQLite state is authoritative; "
                    "reports can be regenerated from the persisted recovery data"
                )
                # Mark report recovery required (the DB row already has
                # report_written=0 from the INSERT above).
                _reconnect = open_connection(paths.database_file)
                try:
                    _reconnect.execute(
                        "INSERT INTO run_events (run_id, event_type, detail, created_at) "
                        "VALUES (?, 'report-failure', ?, ?)",
                        (
                            run_row_id,
                            f"report write failed: {exc}; report_written=0; "
                            "regenerate from weekly_reports table",
                            _now_utc(),
                        ),
                    )
                    _reconnect.commit()
                finally:
                    _reconnect.close()

            # Update planner_runs with report paths.
            conn2 = open_connection(paths.database_file)
            try:
                conn2.execute(
                    "UPDATE planner_runs SET report_md_path = ?, report_json_path = ?, "
                    "updated_at = ? WHERE id = ?",
                    (
                        str(report_md_path),
                        str(report_json_path),
                        _now_utc(),
                        run_row_id,
                    ),
                )
                if not report_failed:
                    conn2.execute(
                        "UPDATE weekly_reports SET report_written = 1, updated_at = ? "
                        "WHERE run_id = ?",
                        (_now_utc(), run_row_id),
                    )
                conn2.commit()
            finally:
                conn2.close()

            status = "error" if report_failed else "success"

            return {
                "schema_name": "grossy.weekly.baseline-result",
                "schema_version": 1,
                "status": status,
                "run_id": run_id,
                "lifecycle": "planning",
                "history_sync_status": history_status,
                "baseline_line_count": len(baseline_lines),
                "recurring_items": report_data["recurring_items"],
                "budget": report_data["budget"],
                "report_failed": report_failed,
                "report_md_path": str(report_md_path) if not report_failed else None,
                "report_json_path": str(report_json_path) if not report_failed else None,
                "errors": report_errors,
            }
        finally:
            conn.close()
