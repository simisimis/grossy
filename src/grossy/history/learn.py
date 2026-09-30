"""Deterministic learned-state inference (PRD FR-32..FR-62, sections 7.3, 12, 19.1).

Task 05 turns the imported, dated order-history observations into learned state:
normalized categories, single-member product families with a fixed primary,
recurrence intervals, confidence/rationale, and ``Europe/Athens`` due dates.

The inference is deterministic and idempotent: it runs inside the same atomic
transaction as a history ``sync-apply`` (PRD FR-25, FR-26) and may be re-run on
every synchronization. It never invents historical quantities (a presence is a
presence, PRD FR-19/FR-33), never creates a ``misc`` category (FR-37), never
auto-promotes or recalculates an established primary (FR-43/FR-44), and leaves
uncertain assignments reviewable rather than silently committed (FR-37, FR-56).

Only pure functions and explicit, transaction-scoped helpers are exposed. The
caller owns the open :class:`sqlite3.Connection` and the surrounding
BEGIN/commit/rollback, so a failure in inference rolls back the whole batch.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from grossy.history.proposal import (
    FAMILY_LINKS_SCHEMA_NAME,
    FAMILY_LINKS_SCHEMA_VERSION,
    FamilyLinksProposal,
    HistoryProposalError,
    validate_family_links,
)

# The configured business timezone (PRD 3, 7.3.3). Stored order/purchase dates
# are calendar dates; due-date evaluation happens in this timezone.
ATHENS_TZ = ZoneInfo("Europe/Athens")

# A single-occurrence product receives this guarded, low-confidence, category-
# based recurrence estimate (FR-51). It is never auto-added on its own (FR-54).
DEFAULT_ESTIMATE_WEEKS = 1

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# Whitespace normalization for base-name extraction (FR-55/FR-56).
_WHITESPACE_RE = re.compile(r"\s+")


class InferenceError(Exception):
    """Raised when deterministic learned-state inference fails."""


# --- pure recurrence / due-date logic --------------------------------------


@dataclass(frozen=True)
class RecurrenceInference:
    """The result of inferring a recurrence from dated occurrences.

    ``weeks`` is ``None`` only when there are no dated occurrences at all (a
    product that has not been imported). ``source`` is ``"observed"`` for two
    or more dated occurrences (inferred from dated intervals, FR-50) or
    ``"category-estimate"`` for a single guarded estimate (FR-51).
    """

    weeks: int | None
    source: str | None  # "observed" | "category-estimate"
    confidence: str | None  # "low" | "medium" | "high"
    rationale: str

    @property
    def is_observed(self) -> bool:
        return self.source == "observed"

    def is_auto_add_eligible(self) -> bool:
        """Whether this recurrence may drive automatic recurring addition.

        Observed recurrence from two or more dated occurrences is always
        eligible (FR-52). A one-occurrence category estimate is eligible only at
        ``high`` confidence (FR-53); a medium/low single-occurrence estimate is
        reported but never auto-added (FR-54).
        """
        if self.source == "observed":
            return True
        if self.source == "category-estimate":
            return self.confidence == "high"
        return False


def _round_weeks(days: int) -> int:
    """Round a day interval to whole weeks, never below one (PRD 40, 12.1.9)."""
    return max(1, round(days / 7))


def _intervals_consistent(intervals: Sequence[int]) -> bool:
    """Whether dated intervals support a stable weekly cadence.

    Consistent when every interval rounds to the same whole-week count, which
    keeps the inferred cadence high-confidence. Divergent cadences (for example
    one weekly and one biweekly purchase) are medium-confidence.
    """
    weeks = {_round_weeks(days) for days in intervals}
    return len(weeks) == 1


def infer_recurrence(order_dates: Sequence[str]) -> RecurrenceInference:
    """Infer a recurrence from a product's dated occurrences (FR-50, FR-51).

    ``order_dates`` are ISO ``YYYY-MM-DD`` strings of the completed orders that
    contain the product (presence only; no quantities, FR-19). Two or more dated
    occurrences yield an ``observed`` interval; a single occurrence yields the
    guarded, low-confidence ``category-estimate``; none yields an empty result.
    """
    unique_dates = sorted(set(order_dates))
    if not unique_dates:
        return RecurrenceInference(None, None, None, "no dated occurrences")

    if len(unique_dates) == 1:
        return RecurrenceInference(
            DEFAULT_ESTIMATE_WEEKS,
            "category-estimate",
            "low",
            "single dated occurrence; guarded category-based weekly estimate "
            "(reported, not auto-added)",
        )

    parsed = [date.fromisoformat(d) for d in unique_dates]
    intervals = [(parsed[i + 1] - parsed[i]).days for i in range(len(parsed) - 1)]
    average_days = sum(intervals) / len(intervals)
    weeks = _round_weeks(round(average_days))
    confidence = "high" if _intervals_consistent(intervals) else "medium"
    rationale = f"{len(unique_dates)} dated occurrences; observed cadence ~{weeks} week(s)"
    return RecurrenceInference(weeks, "observed", confidence, rationale)


def family_due_date(last_purchase_date: str, recurrence_weeks: int) -> date:
    """The date a family becomes due (FR-49): last purchase + recurrence weeks."""
    return date.fromisoformat(last_purchase_date) + timedelta(weeks=recurrence_weeks)


def is_family_due(
    last_purchase_date: str,
    recurrence_weeks: int,
    today: date | None = None,
) -> bool:
    """Whether a family is due now (FR-49).

    ``today`` defaults to the current ``Europe/Athens`` calendar date so the
    due comparison is timezone-correct regardless of the host clock.
    """
    if today is None:
        today = datetime.now(ATHENS_TZ).date()
    return today >= family_due_date(last_purchase_date, recurrence_weeks)


# --- inference results ------------------------------------------------------


@dataclass(frozen=True)
class ReviewItem:
    """A reviewable, tentative learned-state assignment (FR-37, FR-56)."""

    kind: str  # "category" | "recurrence" | "family_link"
    target_type: str  # "product" | "family"
    target_id: str
    reason: str


@dataclass(frozen=True)
class InferenceSummary:
    """Facts about an inference pass (no source order IDs; FR-29).

    ``recurrence`` and ``family_links`` entries carry untrusted product names;
    the report builder is responsible for sanitizing them (FR-29). This summary
    is data only and does not render report text itself.
    """

    families_created: int = 0
    families_updated: int = 0
    recurrence: tuple[dict[str, object], ...] = field(default=())
    family_links: tuple[dict[str, object], ...] = field(default=())
    review_items: tuple[ReviewItem, ...] = field(default=())


# --- connection-scoped inference -------------------------------------------


def _product_occurrences(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """Map each product row id to the dated orders that contain it.

    One row per (product, completed order) presence; a product that was imported
    always appears in at least one dated order (FR-19).
    """
    rows = conn.execute(
        "SELECT sop.product_id, so.order_date "
        "FROM source_order_products sop "
        "JOIN source_orders so ON so.id = sop.source_order_id "
        "ORDER BY sop.product_id, so.order_date"
    ).fetchall()
    occurrences: dict[int, list[str]] = {}
    for product_row_id, order_date in rows:
        occurrences.setdefault(cast(int, product_row_id), []).append(cast(str, order_date))
    return occurrences


def _ensure_family(
    conn: sqlite3.Connection,
    product_row_id: int,
    family_id_text: str,
    family_name: str,
    now: str,
) -> tuple[int, bool]:
    """Return the family for a product, creating a single-member family if needed.

    Returns ``(family_row_id, created)``. Creation follows the flow enforced by
    migration 003: insert the family with a NULL primary, then insert the
    primary member (the AFTER INSERT trigger populates the derived primary). If
    the product already belongs to a family, that family is returned untouched
    so inference never replaces an established primary (FR-44).
    """
    existing = conn.execute(
        "SELECT family_id FROM family_members WHERE product_id = ?", (product_row_id,)
    ).fetchone()
    if existing is not None:
        return cast(int, existing[0]), False

    cur = conn.execute(
        "INSERT INTO product_families (family_id, name, primary_product_id, enabled, "
        "blacklist_state, created_at, updated_at) VALUES (?, ?, NULL, 1, 'active', ?, ?)",
        (family_id_text, family_name, now, now),
    )
    family_row_id = cast(int, cur.lastrowid)
    conn.execute(
        "INSERT INTO family_members (family_id, product_id, role, approval_state, "
        "provenance, confidence, created_at) VALUES (?, ?, 'primary', 'approved', "
        "'inferred', 'high', ?)",
        (family_row_id, product_row_id, now),
    )
    return family_row_id, True


def _upsert_review_item(conn: sqlite3.Connection, item: ReviewItem, now: str) -> None:
    conn.execute(
        "INSERT INTO review_items (kind, target_type, target_id, reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, target_type, target_id) DO UPDATE SET "
        "reason = excluded.reason, updated_at = excluded.updated_at",
        (item.kind, item.target_type, item.target_id, item.reason, now, now),
    )


def _clear_review_items_for(
    conn: sqlite3.Connection, kind: str, target_type: str, target_ids: Sequence[str]
) -> None:
    """Delete review items of one kind whose target is no longer uncertain.

    The kind is scoped so clearing a resolved category item never removes a
    pending item of a different kind for the same target.
    """
    if not target_ids:
        return
    placeholders = ",".join("?" for _ in target_ids)
    conn.execute(
        f"DELETE FROM review_items WHERE kind = ? AND target_type = ? "
        f"AND target_id IN ({placeholders})",
        (kind, target_type, *target_ids),
    )


def _clear_resolved_family_link_reviews(conn: sqlite3.Connection) -> None:
    """Clear only family_link review items whose specific pair is now resolved.

    A review item is resolved when the target product and the "other" product
    (named in the reason) are now in the same family. This is more precise
    than clearing by multi-membership: a product may be in a multi-member
    family (linked to one product) while still having an unresolved review
    item about a different product. Only the specific candidate relation
    being in the same family clears its review item.
    """
    existing = conn.execute(
        "SELECT target_id, reason FROM review_items "
        "WHERE kind = 'family_link' AND target_type = 'product'"
    ).fetchall()
    for target_id, reason in existing:
        # The other product ID appears in the reason as:
        # "...link with '<other_pid>' ..."
        marker = "link with '"
        idx = reason.find(marker)
        if idx == -1:
            continue
        rest = reason[idx + len(marker) :]
        end_idx = rest.find("'")
        if end_idx == -1:
            continue
        other_pid = rest[:end_idx]
        same_family = conn.execute(
            "SELECT 1 FROM family_members m1 "
            "JOIN family_members m2 ON m1.family_id = m2.family_id "
            "JOIN products p1 ON p1.id = m1.product_id AND p1.retailer_product_id = ? "
            "JOIN products p2 ON p2.id = m2.product_id AND p2.retailer_product_id = ? "
            "LIMIT 1",
            (target_id, other_pid),
        ).fetchone()
        if same_family is not None:
            conn.execute(
                "DELETE FROM review_items WHERE kind = 'family_link' "
                "AND target_type = 'product' AND target_id = ?",
                (target_id,),
            )


def _record_review_item(
    conn: sqlite3.Connection, item: ReviewItem, now: str, resolved_targets: dict[str, list[str]]
) -> None:
    """Record a review item unless its target is known to be resolved this pass.

    Recording happens only while the condition still holds (checked just before),
    and targets whose condition cleared this pass are removed, so the table is
    idempotent across re-runs.
    """
    bucket = resolved_targets.setdefault(item.target_type, [])
    if item.target_id in bucket:
        return
    _upsert_review_item(conn, item, now)


@dataclass
class _FamilyGroup:
    row_id: int
    family_id: str
    primary_product_row_id: int | None
    member_row_ids: list[int] = field(default_factory=list)


def _group_families(conn: sqlite3.Connection) -> list[_FamilyGroup]:
    """Group family members by family, keyed by the family row id."""
    groups: dict[int, _FamilyGroup] = {}
    rows = conn.execute(
        "SELECT f.id, f.family_id, f.primary_product_id, m.product_id "
        "FROM product_families f "
        "JOIN family_members m ON m.family_id = f.id"
    ).fetchall()
    for family_row_id, family_id_text, primary_row_id, member_row_id in rows:
        group = groups.setdefault(
            cast(int, family_row_id),
            _FamilyGroup(
                cast(int, family_row_id),
                cast(str, family_id_text),
                cast("int | None", primary_row_id),
            ),
        )
        group.member_row_ids.append(cast(int, member_row_id))
    return list(groups.values())


def _product_name_category(
    conn: sqlite3.Connection, product_row_id: int | None
) -> tuple[str, int | None]:
    if product_row_id is None:
        return ("(none)", None)
    row = conn.execute(
        "SELECT name, category_id FROM products WHERE id = ?", (product_row_id,)
    ).fetchone()
    if row is None:
        return ("(none)", None)
    return cast(str, row[0]), cast("int | None", row[1])


def run_inference(conn: sqlite3.Connection, now: str) -> InferenceSummary:
    """Learn categories, families, recurrence, and review items from the DB.

    Idempotent and deterministic. Every imported product is ensured to belong
    to exactly one family (a single-member family is created on first
    inference, and the primary is fixed then and never changed by later
    synchronization, FR-43/FR-44). High-confidence family links (FR-55) are
    clustered, emitted as a schema-bound proposal, validated fail-closed, and
    only then applied; uncertain links stay reviewable with the products
    separate (FR-56). Recurrence is family-owned (FR-40) and is inferred from
    the union of dated occurrences across all members of the final family
    membership, so an alternative purchase advances the family cadence (FR-47)
    without promoting the primary (FR-62). The family's category is derived
    from the primary product's normalized category when unset (FR-40). Review
    items are recorded for uncertain category and single-occurrence recurrence
    assignments (FR-37, FR-51, FR-54, FR-56) and are cleared once the
    uncertainty is resolved by later evidence, so the persisted review state
    always matches the current observations. Runs in the caller's open
    transaction.
    """
    occurrences = _product_occurrences(conn)

    product_rows = conn.execute(
        "SELECT id, retailer_product_id, name, category_id FROM products ORDER BY id"
    ).fetchall()

    # Step 1: ensure every product has a family (single-member for orphans).
    # Track which products had their families created in this pass: only those
    # may be auto-linked (FR-43: the initial primary is inferred when a family
    # is created; FR-44: subsequent synchronization must not recalculate or
    # promote the primary automatically).
    families_created = 0
    newly_created: set[str] = set()
    for product_row_id, retailer_id, name, _category_id in product_rows:
        _family_row_id, created = _ensure_family(
            conn, cast(int, product_row_id), f"fam-{retailer_id}", cast(str, name), now
        )
        if created:
            families_created += 1
            newly_created.add(cast(str, retailer_id))

    # Step 2: family-link inference through the validated proposal boundary
    # (FR-55, FR-56). Runs before recurrence inference so the cadence is
    # computed over the final membership and a vacated source family never
    # leaves behind a primary-less family (FR-42, FR-44).
    family_link_review, applied_links = _infer_family_links(conn, product_rows, now, newly_created)

    # Step 3: infer recurrence per family from the union of members' dates.
    summary_recurrence, recurrence_review, resolved_families = _infer_family_recurrence(
        conn, occurrences, now
    )
    families_updated = len(summary_recurrence)

    # Step 4: review items for products with no normalized category (FR-37).
    category_review: list[ReviewItem] = []
    resolved_products: list[str] = []
    for _product_row_id, retailer_id, _name, category_id in product_rows:
        if category_id is None:
            category_review.append(
                ReviewItem(
                    "category",
                    "product",
                    cast(str, retailer_id),
                    "no normalized category could be derived from the observed "
                    "heading; left unassigned for review (never `misc`)",
                )
            )
        else:
            resolved_products.append(cast(str, retailer_id))

    # Record category and recurrence review items through the resolved-target
    # mechanism (a product that now has a category no longer needs a category
    # review item; a family with observed recurrence no longer needs a
    # recurrence review item).
    resolved = {"product": resolved_products, "family": resolved_families}
    for item in category_review + recurrence_review:
        _record_review_item(conn, item, now, resolved)
    _clear_review_items_for(conn, "category", "product", resolved_products)
    _clear_review_items_for(conn, "recurrence", "family", resolved_families)

    # Record family_link review items directly (their resolution is driven by
    # the specific candidate pair entering the same family, not by the
    # product's general multi-membership).
    for item in family_link_review:
        _upsert_review_item(conn, item, now)
    # Clear only family_link review items whose specific candidate pair is now
    # in the same family (the uncertain link was resolved by auto-apply or by
    # another path). A product may be in a multi-member family (linked to one
    # product) while still having an unresolved review item about a DIFFERENT
    # product; the broad multi-membership clear would incorrectly delete that
    # still-pending item.
    _clear_resolved_family_link_reviews(conn)

    review_items = tuple(category_review + recurrence_review + family_link_review)

    return InferenceSummary(
        families_created=families_created,
        families_updated=families_updated,
        recurrence=tuple(summary_recurrence),
        family_links=tuple(applied_links),
        review_items=review_items,
    )


def _infer_family_recurrence(
    conn: sqlite3.Connection,
    occurrences: dict[int, list[str]],
    now: str,
) -> tuple[list[dict[str, object]], list[ReviewItem], list[str]]:
    """Infer family-owned recurrence over the final membership (FR-40, FR-47).

    Each family's cadence is inferred from the union of its members' dated
    occurrences, so an alternative purchase advances the family's last-purchase
    date (FR-47) without promoting the primary (FR-62). Returns
    ``(summary_entries, recurrence_review_items, resolved_family_ids)`` where a
    resolved family is one whose recurrence rests on two or more dated
    occurrences (the single-occurrence uncertainty is gone, FR-50).
    """
    summary_recurrence: list[dict[str, object]] = []
    recurrence_review: list[ReviewItem] = []
    resolved_families: list[str] = []
    for group in _group_families(conn):
        combined = sorted(
            {d for member in group.member_row_ids for d in occurrences.get(member, [])}
        )
        inference = infer_recurrence(combined)
        if inference.weeks is None:
            continue
        primary_name, primary_category_id = _product_name_category(
            conn, group.primary_product_row_id
        )
        last_purchase = max(combined)
        rationale = (
            f"category: {_category_display(conn, primary_category_id)}; {inference.rationale}"
        )
        # The family owns its category (FR-40): derive it from the primary
        # product's normalized category, but only fill it when unset so a
        # later manual assignment is never overwritten by inference.
        conn.execute(
            "UPDATE product_families SET recurrence_weeks = ?, recurrence_source = ?, "
            "recurrence_confidence = ?, last_purchase_date = ?, confidence = ?, "
            "rationale = ?, category_id = COALESCE(category_id, ?), updated_at = ? "
            "WHERE id = ?",
            (
                inference.weeks,
                inference.source,
                inference.confidence,
                last_purchase,
                inference.confidence,
                rationale,
                primary_category_id,
                now,
                group.row_id,
            ),
        )
        summary_recurrence.append(
            {
                "family_id": group.family_id,
                "primary_product_id": _primary_retailer_id(conn, group.primary_product_row_id),
                "name": primary_name,
                "weeks": inference.weeks,
                "source": inference.source,
                "confidence": inference.confidence,
                "auto_add_eligible": inference.is_auto_add_eligible(),
            }
        )
        if inference.source == "category-estimate":
            recurrence_review.append(
                ReviewItem(
                    "recurrence",
                    "family",
                    group.family_id,
                    "recurrence rests on a single dated occurrence (guarded "
                    "category estimate); confirm the cadence before auto-add",
                )
            )
        else:
            # Observed recurrence (two or more dated occurrences) resolved the
            # single-occurrence uncertainty; the stale review item is cleared
            # by the caller so the review state always reflects the current
            # evidence.
            resolved_families.append(group.family_id)
    return summary_recurrence, recurrence_review, resolved_families


def _base_name(name: str) -> str:
    """Extract the base product name by stripping trailing package details.

    The base name is everything before the first digit in the normalized name.
    This handles common grocery patterns like "Olympus Milk 1L" → "olympus
    milk", "Lactalis Kefir 100ml x4" → "lactalis kefir", "Greek Yogurt 500g"
    → "greek yogurt". Products whose names contain no digits retain their full
    normalized name as the base (e.g., "olives kalamata").
    """
    normalized = _WHITESPACE_RE.sub(" ", name).strip().lower()
    for i, ch in enumerate(normalized):
        if ch.isdigit():
            return normalized[:i].rstrip()
    return normalized


def _common_prefix_words(a: str, b: str) -> int:
    """Count the number of leading words shared by two normalized strings."""
    words_a = a.split()
    words_b = b.split()
    count = 0
    for wa, wb in zip(words_a, words_b, strict=False):
        if wa == wb:
            count += 1
        else:
            break
    return count


def _infer_family_links(
    conn: sqlite3.Connection,
    product_rows: list[tuple[int, str, str, int | None]],
    now: str,
    newly_created: set[str],
) -> tuple[list[ReviewItem], list[dict[str, object]]]:
    """Cluster link candidates and apply them through the validated boundary.

    High-confidence candidates (FR-55) are products that share a normalized
    category and an identical base name of at least two words. Each candidate
    cluster is reduced to one schema-bound family-links proposal before any
    membership changes: the most established product in the cluster (highest
    ``purchase_order_count``, then earliest creation, then stable ID) is the
    anchor, and every other product not already in the anchor's family is
    linked into that family exactly once. Clustering before mutation keeps the
    link targets stable for the whole pass (the anchor family never moves) and
    guarantees that no family is left without a primary (FR-42, FR-44). Source
    families that lose their last member to an applied link are retired,
    together with any review items that still target them.

    The proposal is schema-bound and validated fail-closed (FR-55, PRD 11):
    a validation or apply failure propagates so the caller's transaction rolls
    back the whole synchronization (FR-26).

    FR-44 constraint: a product whose family already existed before this pass
    has an established primary that must not be demoted by inference. Only
    products whose families were created in this pass (``newly_created``) may
    be auto-linked; established primaries are left untouched and a review item
    is generated so the user can manually merge if desired.

    Uncertain candidates (FR-56) -- same category, base names sharing a common
    prefix of at least two words but not identical -- produce ``family_link``
    review items and the products remain in separate families.

    Returns ``(review_items, applied_links)`` where each applied link carries
    ``product_id``, ``target_family_id``, ``base_name``, and ``category``.
    """
    facts = _product_link_facts(conn, product_rows)

    clusters: dict[tuple[int, str], list[str]] = {}
    for _product_row_id, retailer_id, _name, category_id in product_rows:
        if category_id is None:
            continue
        info = facts.get(retailer_id)
        if info is None:
            continue
        clusters.setdefault((category_id, cast(str, info["base_name"])), []).append(retailer_id)

    links: list[dict[str, str]] = []
    blocked_high_confidence: list[ReviewItem] = []
    for (category_id, _base_name), members in sorted(clusters.items()):
        if len(members) < 2:
            continue
        anchor = min(
            members,
            key=lambda pid: (
                -cast(int, facts[pid]["purchase_order_count"]),
                cast(str, facts[pid]["created_at"]),
                pid,
            ),
        )
        anchor_family = cast("str | None", facts[anchor]["family_id"])
        if anchor_family is None:
            continue
        for pid in sorted(members):
            if pid == anchor:
                continue
            info = facts[pid]
            if cast("str | None", info["family_id"]) == anchor_family:
                continue  # already a member of the anchor family
            # FR-44: a product whose family already existed before this pass
            # has an established primary that inference must not demote.
            if pid not in newly_created:
                count_a = cast(int, facts[anchor]["purchase_order_count"])
                count_b = cast(int, facts[pid]["purchase_order_count"])
                target = pid if count_a > count_b else anchor
                other = anchor if target == pid else pid
                blocked_high_confidence.append(
                    ReviewItem(
                        "family_link",
                        "product",
                        target,
                        f"high-confidence family link with {other!r} (identical "
                        f"base name, same category) cannot be auto-applied because "
                        f"the target is the established primary of its family; "
                        f"manual merge required (FR-44)",
                    )
                )
                continue
            links.append(
                {
                    "product_id": pid,
                    "target_family_id": anchor_family,
                    "base_name": cast(str, info["base_name"]),
                    "category": _category_display(conn, category_id),
                    "confidence": "high",
                    "provenance": "inferred",
                }
            )

    proposal: dict[str, Any] = {
        "schema_name": FAMILY_LINKS_SCHEMA_NAME,
        "schema_version": FAMILY_LINKS_SCHEMA_VERSION,
        "generated_at": now,
        "links": links,
    }
    validated = _validate_family_links_or_fail(proposal)

    # Apply the validated links. Each product appears at most once (enforced
    # by the proposal validator) and its target is the stable anchor family,
    # so a failure here is an internal inconsistency: fail closed and let the
    # caller roll back the whole synchronization (FR-26).
    source_families: list[str] = []
    applied: list[dict[str, object]] = []
    for link in validated.links:
        source_family = _current_family_id(conn, link.product_id)
        if source_family is not None and source_family != link.target_family_id:
            source_families.append(source_family)
        link_alternative(conn, link.target_family_id, link.product_id, now)
        applied.append(
            {
                "product_id": link.product_id,
                "target_family_id": link.target_family_id,
                "base_name": link.base_name,
                "category": link.category,
            }
        )

    _retire_vacated_families(conn, source_families)

    review_items = _family_link_review_items(conn, product_rows, facts)
    review_items.extend(blocked_high_confidence)
    return review_items, applied


def _validate_family_links_or_fail(proposal: dict[str, Any]) -> FamilyLinksProposal:
    """Validate the family-links proposal; fail closed (rolls back the sync)."""
    try:
        return validate_family_links(proposal)
    except HistoryProposalError as exc:
        raise InferenceError(f"family-links proposal rejected: {exc}") from exc


def _product_link_facts(
    conn: sqlite3.Connection, product_rows: list[tuple[int, str, str, int | None]]
) -> dict[str, dict[str, object]]:
    """Per-product facts for family-link clustering, keyed by stable ID.

    Products without a normalized category, or with a base name of fewer than
    two words (too generic to match, e.g. "product" from "Product 71"), are
    excluded: they never produce an automatic link or an uncertain review item.
    The family is read live per product (never from a stale snapshot) so link
    targets stay correct when several links are applied in one pass.
    """
    facts: dict[str, dict[str, object]] = {}
    for _product_row_id, retailer_id, name, category_id in product_rows:
        if category_id is None:
            continue
        base = _base_name(name)
        if len(base.split()) < 2:
            continue
        row = conn.execute(
            "SELECT p.purchase_order_count, p.created_at, f.family_id "
            "FROM products p "
            "LEFT JOIN family_members fm ON fm.product_id = p.id "
            "LEFT JOIN product_families f ON f.id = fm.family_id "
            "WHERE p.retailer = 'ab' AND p.retailer_product_id = ?",
            (retailer_id,),
        ).fetchone()
        if row is None:
            continue
        facts[retailer_id] = {
            "purchase_order_count": cast(int, row[0]),
            "created_at": cast(str, row[1]),
            "family_id": cast("str | None", row[2]),
            "base_name": base,
        }
    return facts


def _current_family_id(conn: sqlite3.Connection, retailer_product_id: str) -> str | None:
    """The product's current family_id, re-read live (never a stale snapshot)."""
    row = conn.execute(
        "SELECT f.family_id "
        "FROM family_members fm "
        "JOIN product_families f ON f.id = fm.family_id "
        "JOIN products p ON p.id = fm.product_id "
        "WHERE p.retailer = 'ab' AND p.retailer_product_id = ?",
        (retailer_product_id,),
    ).fetchone()
    return cast(str, row[0]) if row is not None else None


def _retire_vacated_families(conn: sqlite3.Connection, family_ids: Sequence[str]) -> None:
    """Retire source families that lost their last member to this pass's links.

    Such a family is an inference artifact with no remaining members; leaving
    it would persist a primary-less family shell (FR-42) and stale review
    items. Families that still have members are left untouched, as are
    families that were already empty before this pass (e.g. from manual
    operations in a later task).
    """
    for family_id in sorted(set(family_ids)):
        remaining = conn.execute(
            "SELECT 1 FROM family_members WHERE family_id = "
            "(SELECT id FROM product_families WHERE family_id = ?) LIMIT 1",
            (family_id,),
        ).fetchone()
        if remaining is not None:
            continue
        conn.execute("DELETE FROM product_families WHERE family_id = ?", (family_id,))
        # A vacated family's pending recurrence review item is stale: the
        # merged family re-infers its own recurrence this pass.
        _clear_review_items_for(conn, "recurrence", "family", [family_id])


def _family_link_review_items(
    conn: sqlite3.Connection,
    product_rows: list[tuple[int, str, str, int | None]],
    facts: dict[str, dict[str, object]],
) -> list[ReviewItem]:
    """Review items for uncertain family-link candidates (FR-56).

    Two products in the same category whose base names share a common prefix
    of at least two words but are not identical are possible family members;
    the link is recorded for review and the products remain separate.
    """
    review_items: list[ReviewItem] = []
    by_category: dict[int, list[str]] = {}
    for _product_row_id, retailer_id, _name, category_id in product_rows:
        if category_id is None:
            continue
        if retailer_id not in facts:
            continue
        by_category.setdefault(category_id, []).append(retailer_id)
    for _category_id, pids in by_category.items():
        pids = sorted(pids)
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                info_a = facts[pids[i]]
                info_b = facts[pids[j]]
                base_a = cast(str, info_a["base_name"])
                base_b = cast(str, info_b["base_name"])
                if base_a == base_b:
                    continue  # high-confidence candidate; handled by the proposal
                prefix = _common_prefix_words(base_a, base_b)
                if prefix < 2:
                    continue
                count_a = cast(int, info_a["purchase_order_count"])
                count_b = cast(int, info_b["purchase_order_count"])
                target = pids[i] if count_a <= count_b else pids[j]
                other = pids[j] if target == pids[i] else pids[i]
                review_items.append(
                    ReviewItem(
                        "family_link",
                        "product",
                        target,
                        f"possible family link with {other!r} (same category, "
                        f"common base-name prefix of {prefix} words); products "
                        f"remain separate pending review (FR-56)",
                    )
                )
    return review_items


def _primary_retailer_id(conn: sqlite3.Connection, product_row_id: int | None) -> str:
    if product_row_id is None:
        return "(none)"
    row = conn.execute(
        "SELECT retailer_product_id FROM products WHERE id = ?", (product_row_id,)
    ).fetchone()
    return cast(str, row[0]) if row is not None else "(none)"


def _category_display(conn: sqlite3.Connection, category_id: int | None) -> str:
    if category_id is None:
        return "(unassigned)"
    row = conn.execute(
        "SELECT display_name FROM categories WHERE id = ?", (category_id,)
    ).fetchone()
    if row is None:
        return "(unassigned)"
    return cast(str, row[0])


# --- alternative selection and cadence advancement (FR-46..FR-62) ----------


@dataclass(frozen=True)
class SelectionResult:
    """The product selected for a due family (never a promotion, FR-62)."""

    retailer_product_id: str
    role: str  # "primary" | "alternative"
    family_id: str
    promoted: bool  # always False
    rationale: str


def select_family_product(
    conn: sqlite3.Connection, family_id: str, available_product_ids: frozenset[str]
) -> SelectionResult | None:
    """Select the product to add for a due family (FR-60..FR-62).

    The established primary is tried first; if it is not available, an approved
    policy-compliant alternative may be tried (FR-61). Selecting an alternative
    never promotes it and never changes the stored primary (FR-62), so the next
    due run tries the primary again. Returns ``None`` when no available, approved
    member exists (the caller skips and reports rather than guessing, FR-170).
    """
    rows = conn.execute(
        "SELECT m.role, m.approval_state, p.retailer_product_id "
        "FROM family_members m "
        "JOIN products p ON p.id = m.product_id "
        "WHERE m.family_id = (SELECT id FROM product_families WHERE family_id = ?) "
        "  AND m.role IN ('primary', 'alternative') "
        "ORDER BY (m.role = 'alternative'), m.product_id",
        (family_id,),
    ).fetchall()
    for role, approval_state, retailer_id in rows:
        if approval_state != "approved":
            continue
        if cast(str, retailer_id) not in available_product_ids:
            continue
        role_str = cast(str, role)
        if role_str == "primary":
            rationale = "established primary is available"
        else:
            rationale = (
                "primary unavailable; approved alternative selected (not promoted; "
                "the primary is tried again next due run)"
            )
        return SelectionResult(
            retailer_product_id=cast(str, retailer_id),
            role=role_str,
            family_id=family_id,
            promoted=False,
            rationale=rationale,
        )
    return None


def advance_family_cadence(conn: sqlite3.Connection, family_id: str, order_date: str) -> bool:
    """Advance a family's last purchase date to a completed order date (FR-47, FR-62).

    Only a synchronized completed order that actually contains a family member
    may advance the cadence (FR-47). This never changes the primary (an
    alternative purchase does not promote it, FR-62) and never affects
    planner/cart activity (FR-48). Returns ``True`` when the date actually
    advanced.

    Raises :class:`InferenceError` when no synchronized completed order with
    the given date contains a member of the family (guards against advancing
    cadence from arbitrary or planner-sourced dates).
    """
    row = conn.execute(
        "SELECT last_purchase_date FROM product_families WHERE family_id = ?", (family_id,)
    ).fetchone()
    if row is None:
        raise InferenceError(f"unknown family {family_id!r}")
    current = row[0]
    if current is not None and current >= order_date:
        return False

    # Validate that a synchronized completed order with this date exists and
    # contains at least one member of the family (FR-47: only synchronized
    # completed order appearances advance cadence).
    evidence = conn.execute(
        "SELECT 1 FROM source_order_products sop "
        "JOIN source_orders so ON so.id = sop.source_order_id "
        "JOIN family_members m ON m.product_id = sop.product_id "
        "WHERE so.order_date = ? "
        "  AND m.family_id = (SELECT id FROM product_families WHERE family_id = ?) "
        "LIMIT 1",
        (order_date, family_id),
    ).fetchone()
    if evidence is None:
        raise InferenceError(
            f"no synchronized completed order on {order_date!r} contains a "
            f"member of family {family_id!r}; cadence may only advance from "
            f"actual order evidence (FR-47)"
        )

    conn.execute(
        "UPDATE product_families SET last_purchase_date = ? WHERE family_id = ?",
        (order_date, family_id),
    )
    return True


def link_alternative(
    conn: sqlite3.Connection, family_id: str, retailer_product_id: str, now: str
) -> int:
    """Link an existing product into a family as an approved alternative.

    The product is moved out of any family it already belongs to (a product may
    belong to at most one active family, PRD 12.1.6) and added with role
    ``alternative`` and ``approved`` state. This is the deterministic,
    transaction-scoped form of an explicit family link; it does not change the
    family's established primary: the AFTER INSERT trigger recomputes the
    derived primary from the family's primary members, and the established
    primary member stays in place (FR-44, FR-62).

    Raises :class:`InferenceError` when:
    - the product is already a member of the target family (including as its
      primary); self-linking a primary would delete the sole primary and break
      the fixed-primary invariant (FR-42, FR-44).
    """
    product_row = conn.execute(
        "SELECT id FROM products WHERE retailer = 'ab' AND retailer_product_id = ?",
        (retailer_product_id,),
    ).fetchone()
    if product_row is None:
        raise InferenceError(f"unknown product {retailer_product_id!r}")
    product_row_id = cast(int, product_row[0])

    family_row = conn.execute(
        "SELECT id FROM product_families WHERE family_id = ?", (family_id,)
    ).fetchone()
    if family_row is None:
        raise InferenceError(f"unknown family {family_id!r}")
    family_row_id = cast(int, family_row[0])

    # Reject self-linking: the product must not already be a member of the
    # target family (especially as its primary, which would be destroyed by
    # the DELETE below, FR-42/FR-44).
    existing = conn.execute(
        "SELECT role FROM family_members WHERE family_id = ? AND product_id = ?",
        (family_row_id, product_row_id),
    ).fetchone()
    if existing is not None:
        role = cast(str, existing[0])
        if role == "primary":
            raise InferenceError(
                f"product {retailer_product_id!r} is the primary of family "
                f"{family_id!r}; linking it as an alternative would destroy "
                f"the established primary (FR-42, FR-44)"
            )
        raise InferenceError(
            f"product {retailer_product_id!r} is already a member of family {family_id!r}"
        )

    # A product belongs to at most one active family (PRD 12.1.6).
    conn.execute("DELETE FROM family_members WHERE product_id = ?", (product_row_id,))
    conn.execute(
        "INSERT INTO family_members (family_id, product_id, role, approval_state, "
        "provenance, confidence, created_at) VALUES (?, ?, 'alternative', 'approved', "
        "'linked', 'high', ?)",
        (family_row_id, product_row_id, now),
    )
    return family_row_id
