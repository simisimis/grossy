"""Scroll stabilization and order-list filtering (FR-7, FR-15, FR-16, FR-22).

These are pure, deterministic functions that process raw browser observations
before a versioned proposal is emitted. The Pi skill calls these when it
observes the store's rendered order-history interface. They are also unit-tested
in isolation with fake observations (PRD 19.3).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RawProductCard:
    """A single product card as rendered on an order page.

    ``product_id`` is ``None`` when the rendered card lacks a stable numeric
    ID (FR-22 requires the scan to fail closed in that case).
    """

    product_id: str | None
    title: str
    canonical_url: str | None = None
    brand: str | None = None
    category_heading: str | None = None
    package_amount: float | None = None
    package_unit: str | None = None
    package_count: int | None = None
    package_display: str | None = None
    displayed_price_eur: str | None = None


@dataclass(frozen=True)
class OrderListEntry:
    """One entry from the rendered order-history list page.

    The list contains any number of dated orders (including zero) and may
    carry a single ``All orders`` union entry (``is_union_entry=True``) which
    must be excluded (FR-7).
    """

    label: str
    source_order_id: str | None = None
    navigation_url: str | None = None
    is_union_entry: bool = False


class ScanStabilizationError(Exception):
    """Raised when the scroll-stabilization process fails (FR-16, FR-22)."""


class OrderListError(Exception):
    """Raised when the order-list observation is invalid (FR-7)."""


def stabilize_scan(bottom_passes: list[list[RawProductCard]]) -> list[RawProductCard]:
    """Stabilize a product scan from a sequence of bottom-pass observations.

    FR-15: The skill navigates to each unknown order and repeatedly scrolls to
    the bottom. FR-16: Scanning stops only after two consecutive bottom passes
    reveal no new unique product IDs. FR-22: If any card lacks a stable product
    ID, the scan fails closed.

    Each element of ``bottom_passes`` is the list of product cards visible
    after one scroll-to-bottom pass. The function validates the stabilization
    invariant and returns the deduplicated union of all products (preserving
    first-appearance order).

    Raises :class:`ScanStabilizationError` if:
    - Fewer than 2 passes were provided.
    - Any product card has ``product_id=None``.
    - The final two passes do not both reveal no new unique IDs.
    """
    if len(bottom_passes) < 2:
        raise ScanStabilizationError(
            f"at least 2 bottom passes required for scan stabilization, got {len(bottom_passes)}"
        )

    # FR-22: missing stable product IDs reject the batch.
    for pass_idx, cards in enumerate(bottom_passes, start=1):
        for card in cards:
            if card.product_id is None:
                raise ScanStabilizationError(
                    f"pass {pass_idx}: a product card lacks a stable product ID; "
                    "the synchronization must fail closed (FR-22)"
                )

    # FR-16: verify two consecutive stable passes at the end.
    # A pass is "stable" if it reveals no product IDs that are new relative
    # to the cumulative set seen across all prior passes. This handles
    # non-cumulative rendered cards where a pass may show a subset of the
    # full set (e.g., alternating {A},{B},{A},{B},{B} is stable because the
    # last two passes add no globally new IDs).
    cumulative_seen: set[str] = set()
    consecutive_stable = 0
    for cards in bottom_passes:
        current_ids: set[str] = {c.product_id for c in cards if c.product_id is not None}
        if not (current_ids - cumulative_seen):
            consecutive_stable += 1
        else:
            consecutive_stable = 0
        cumulative_seen |= current_ids

    if consecutive_stable < 2:
        raise ScanStabilizationError(
            "scan not stable: the final two passes must each reveal no new unique "
            "product IDs (FR-16)"
        )

    # Return the deduplicated union preserving first-appearance order.
    seen: dict[str, RawProductCard] = {}
    for cards in bottom_passes:
        for card in cards:
            assert card.product_id is not None
            if card.product_id not in seen:
                seen[card.product_id] = card
    return list(seen.values())


def filter_dated_orders(entries: list[OrderListEntry]) -> list[OrderListEntry]:
    """Filter the rendered order list to its dated orders, whatever their count.

    FR-7: The ``All orders`` union entry must be excluded because it is a
    de-duplicated union without per-order timing. Any number of dated
    orders, including zero, is accepted.

    Raises :class:`OrderListError` if any dated entry lacks a stable source
    order ID.
    """
    dated = [e for e in entries if not e.is_union_entry]
    for entry in dated:
        if entry.source_order_id is None:
            raise OrderListError("a dated order entry lacks a stable source order ID")
    return dated
