"""YAML policy resolution against the local database (PRD 7.1, 7.3, FR-63..FR-66).

The strict YAML configuration references staple products: a preferred product
(``product_id`` or the ID carried by ``product_url``) and any approved
alternatives. The precedence order (PRD 7.1) is::

    current-run instruction -> blacklist -> YAML -> manual SQLite override ->
    learned -> live

This module is the production boundary that resolves the YAML policy against
the local database and reports the conflicts the precedence rules produce. It
consumes the strict :class:`~grossy.config.models.GrossyConfigModel` (already
validated by the loader, PRD 7.3) and the caller's open, read-only connection.
It is the single source of truth for the blacklist/disabled precedence so the
manual policy and the automatic selection boundary cannot drift:

- :func:`product_gate` resolves one product's effective policy state: blocked
  by a product blacklist (FR-65), blocked by a family blacklist (FR-66),
  suppressed by a disabled family (FR-63), active, or unknown. The automatic
  selection boundary :func:`grossy.history.learn.select_family_product` calls
  this for every candidate member, so a blacklisted product is blocked in
  *both* the automatic and the manual (YAML) paths.
- :func:`resolve_yaml_policy` walks every YAML staple reference and returns the
  **blocking conflicts** (a blacklisted product is a conflict: the blacklist is
  precedence level 2 and beats the YAML reference at level 3, so the product is
  not added — the user is told because they configured a staple their own
  blacklist blocks) and the **disabled-but-not-blacklisted references** (a
  disabled family suppresses history-driven prefill but does *not* block an
  explicit YAML reference, FR-63).

The module is read-only: it never mutates the database and the caller owns the
connection.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import cast

from grossy.config.models import GrossyConfigModel, StapleModel, product_id_from_url

# Precedence gate statuses (PRD 7.1, FR-63..FR-66). ``blocked`` means the
# blacklist wins over the lower-precedence reference and the product is not
# added; ``disabled`` means the family is suppressed from *automatic* prefill
# but an explicit YAML reference still applies.
GATE_OK = "ok"
GATE_BLACKLISTED = "blacklisted"
GATE_FAMILY_BLACKLISTED = "family-blacklisted"
GATE_DISABLED = "disabled-family"
GATE_UNKNOWN = "unknown"

_BLOCKED_STATUSES = frozenset({GATE_BLACKLISTED, GATE_FAMILY_BLACKLISTED})


@dataclass(frozen=True)
class ProductGate:
    """The effective policy gate for one product (PRD 7.1 precedence).

    ``status`` is one of :data:`GATE_OK`, :data:`GATE_BLACKLISTED`,
    :data:`GATE_FAMILY_BLACKLISTED`, :data:`GATE_DISABLED`, or
    :data:`GATE_UNKNOWN`. A blocked gate (product or family blacklist) means the
    product must not be added from any lower-precedence source; a disabled gate
    means the family is suppressed from automatic prefill only.
    """

    product_id: str
    status: str
    family_id: str | None
    detail: str

    @property
    def is_blocked(self) -> bool:
        """Whether the blacklist blocks this product (PRD 7.1 level 2)."""
        return self.status in _BLOCKED_STATUSES

    @property
    def is_disabled(self) -> bool:
        """Whether the family is disabled (suppresses prefill, not YAML refs)."""
        return self.status == GATE_DISABLED


def product_gate(conn: sqlite3.Connection, product_id: str) -> ProductGate:
    """Resolve one product's effective policy gate (PRD 7.1, FR-63..FR-66).

    Precedence within the gate: a product blacklist (FR-65) beats a family
    blacklist (FR-66), which beats a disabled family (FR-63). A product that is
    not in the local database is ``unknown`` (the reference cannot be resolved
    against local state; the caller reports it rather than guessing).
    """
    row = conn.execute(
        "SELECT p.blacklist_state, f.family_id, f.enabled, f.blacklist_state "
        "FROM products p "
        "LEFT JOIN family_members fm ON fm.product_id = p.id "
        "LEFT JOIN product_families f ON f.id = fm.family_id "
        "WHERE p.retailer = 'ab' AND p.retailer_product_id = ?",
        (product_id,),
    ).fetchone()
    if row is None:
        return ProductGate(
            product_id=product_id,
            status=GATE_UNKNOWN,
            family_id=None,
            detail=f"product {product_id} is not in the local database",
        )
    product_blacklist = cast(str, row[0])
    family_id = cast("str | None", row[1])
    family_enabled = cast("int | None", row[2])
    family_blacklist = cast("str | None", row[3])

    if product_blacklist == "blacklisted":
        return ProductGate(
            product_id=product_id,
            status=GATE_BLACKLISTED,
            family_id=family_id,
            detail=(
                f"product {product_id} is blacklisted; the product blacklist "
                "(PRD 7.1 level 2, FR-65) blocks every lower-precedence reference"
            ),
        )
    if family_blacklist == "blacklisted":
        return ProductGate(
            product_id=product_id,
            status=GATE_FAMILY_BLACKLISTED,
            family_id=family_id,
            detail=(
                f"product {product_id} belongs to blacklisted family {family_id}; "
                "the family blacklist (FR-66) blocks every member"
            ),
        )
    if family_enabled == 0:
        return ProductGate(
            product_id=product_id,
            status=GATE_DISABLED,
            family_id=family_id,
            detail=(
                f"product {product_id} belongs to disabled family {family_id}; "
                "the disabled state (FR-63) suppresses automatic prefill but an "
                "explicit YAML reference still applies"
            ),
        )
    return ProductGate(
        product_id=product_id,
        status=GATE_OK,
        family_id=family_id,
        detail="no conflict; the reference is active",
    )


@dataclass(frozen=True)
class YamlStapleResolution:
    """The policy resolution of one YAML-referenced product within a staple."""

    staple_key: str
    category: str  # "breakfast" | "lunch"
    product_id: str
    role: str  # "preferred" | "alternative"
    status: str  # a GATE_* status
    family_id: str | None
    detail: str

    @property
    def is_blocked(self) -> bool:
        return self.status in _BLOCKED_STATUSES


@dataclass(frozen=True)
class YamlPolicyResult:
    """The result of resolving the YAML policy against the database.

    ``resolutions`` covers every referenced product (preferred and approved
    alternatives) across all staples. ``blocking_conflicts`` are the references
    the blacklist blocks (a blacklisted staple is a real conflict the user must
    resolve); ``disabled_references`` are the references whose family is
    disabled but not blacklisted (allowed via the explicit YAML reference, FR-63).
    """

    resolutions: tuple[YamlStapleResolution, ...]
    blocking_conflicts: tuple[YamlStapleResolution, ...]
    disabled_references: tuple[YamlStapleResolution, ...]

    @property
    def has_conflicts(self) -> bool:
        return len(self.blocking_conflicts) > 0


def _staple_referenced_ids(staple: StapleModel) -> tuple[tuple[str, str], ...]:
    """The ``(product_id, role)`` pairs a staple references, in precedence order.

    The preferred product (``product_id`` or the ID carried by ``product_url``)
    comes first, then the approved alternatives. A staple may reference no local
    product if only a ``product_url`` is present and it fails to parse, but the
    strict config model guarantees a parseable ID when a URL is present.
    """
    refs: list[tuple[str, str]] = []
    if staple.product_id is not None:
        refs.append((staple.product_id, "preferred"))
    elif staple.product_url is not None:
        url_id = product_id_from_url(staple.product_url)
        if url_id is not None:
            refs.append((url_id, "preferred"))
    for alternative in staple.approved_alternative_product_ids:
        refs.append((alternative, "alternative"))
    return tuple(refs)


def resolve_yaml_policy(config: GrossyConfigModel, conn: sqlite3.Connection) -> YamlPolicyResult:
    """Resolve the strict YAML policy against the database (PRD 7.1).

    Walks every breakfast and lunch staple reference (preferred product and
    approved alternatives) and resolves each through :func:`product_gate`.
    Returns the full resolutions plus the derived blocking conflicts (blacklisted
    products, which the blacklist beats) and disabled-but-not-blacklisted
    references (disabled families, which do not block an explicit YAML
    reference, FR-63). Read-only: the caller owns the connection.
    """
    resolutions: list[YamlStapleResolution] = []
    for category in ("breakfast", "lunch"):
        for staple in getattr(config.staples, category):
            for product_id, role in _staple_referenced_ids(staple):
                gate = product_gate(conn, product_id)
                resolutions.append(
                    YamlStapleResolution(
                        staple_key=staple.key,
                        category=category,
                        product_id=product_id,
                        role=role,
                        status=gate.status,
                        family_id=gate.family_id,
                        detail=gate.detail,
                    )
                )
    blocking = tuple(r for r in resolutions if r.is_blocked)
    disabled = tuple(r for r in resolutions if r.status == GATE_DISABLED)
    return YamlPolicyResult(
        resolutions=tuple(resolutions),
        blocking_conflicts=blocking,
        disabled_references=disabled,
    )
