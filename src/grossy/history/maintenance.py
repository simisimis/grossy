"""Manual corrections, categories, and blacklists (PRD FR-35..FR-38, FR-63..FR-69).

Transactional maintenance commands for explicit user corrections to learned
state. Every correction is:

- explicit (user-initiated, never inferred);
- auditable (recorded in ``manual_overrides`` or ``blacklist_events`` with
  rationale and timestamps);
- transactional (all state changes are atomic; a failure leaves state unchanged);
- persistent across later synchronization (inference and sync-apply respect
  active manual overrides).

The mandatory precedence (PRD 7.1) is:

1. explicit instruction for the current run;
2. blacklist check;
3. explicit YAML policy;
4. manual SQLite override;
5. learned SQLite state;
6. live store availability and price.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import unquote

from grossy.config.models import product_id_from_url
from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import run_migrations
from grossy.paths import GrossyPaths
from grossy.security import ProhibitedContentError, sanitize_human_text, scan_prohibited_content
from grossy.store import StoreConfigError

# Shared whitespace-collapsing normalizer (same as sync.py _WHITESPACE_RE).
_WS_RE = re.compile(r"\s+")


def _normalize_category_name(name: str) -> str:
    """Normalize a category name: collapse whitespace, strip, lowercase.

    Uses the same normalization as sync-apply's ``_category_name_from_heading``
    so that manually assigned categories are consistent with scanned categories.
    """
    return _WS_RE.sub(" ", name).strip().lower()


# Closed set of override field names (one spelling per concept, PRD 7.1).
OVERRIDE_PRIMARY = "primary_product_id"
OVERRIDE_RECURRENCE = "recurrence_weeks"
OVERRIDE_QUANTITY = "typical_quantity"
OVERRIDE_ENABLED = "enabled"
OVERRIDE_CATEGORY = "category_id"


class MaintenanceError(Exception):
    """Raised when a manual correction command fails validation or application."""


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- override and blacklist event recording ---------------------------------


def _supersede_override(
    conn: sqlite3.Connection,
    target_type: str,
    target_id: str,
    field_name: str,
    now: str,
) -> None:
    """Mark any active override for the same target and field as superseded."""
    conn.execute(
        "UPDATE manual_overrides SET superseded_at = ? "
        "WHERE target_type = ? AND target_id = ? AND field_name = ? AND superseded_at IS NULL",
        (now, target_type, target_id, field_name),
    )


def _record_override(
    conn: sqlite3.Connection,
    target_type: str,
    target_id: str,
    field_name: str,
    field_value: str,
    rationale: str | None,
    now: str,
) -> int:
    """Supersede prior and insert a new manual override; return the row id."""
    _supersede_override(conn, target_type, target_id, field_name, now)
    cur = conn.execute(
        "INSERT INTO manual_overrides (target_type, target_id, field_name, field_value, "
        "rationale, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (target_type, target_id, field_name, field_value, rationale, now),
    )
    return cast(int, cur.lastrowid)


def _record_blacklist_event(
    conn: sqlite3.Connection,
    target_type: str,
    target_id: str,
    action: str,
    rationale: str | None,
    now: str,
) -> int:
    """Insert a blacklist or unblacklist event; return the row id."""
    cur = conn.execute(
        "INSERT INTO blacklist_events (target_type, target_id, action, rationale, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (target_type, target_id, action, rationale, now),
    )
    return cast(int, cur.lastrowid)


# --- lookup helpers ----------------------------------------------------------


def _get_product_row_id(conn: sqlite3.Connection, retailer_product_id: str) -> int:
    """Return the products row id for a stable retailer product ID."""
    row = conn.execute(
        "SELECT id FROM products WHERE retailer = 'ab' AND retailer_product_id = ?",
        (retailer_product_id,),
    ).fetchone()
    if row is None:
        raise MaintenanceError(f"unknown product {retailer_product_id!r}")
    return cast(int, row[0])


def _get_family_row_id(conn: sqlite3.Connection, family_id: str) -> int:
    """Return the product_families row id for a stable family ID."""
    row = conn.execute(
        "SELECT id FROM product_families WHERE family_id = ?", (family_id,)
    ).fetchone()
    if row is None:
        raise MaintenanceError(f"unknown family {family_id!r}")
    return cast(int, row[0])


def _get_category_id(conn: sqlite3.Connection, name: str) -> int:
    """Return the category row id for a normalized name, creating it if needed.

    The no-``misc`` rule (PRD 7.3 / FR-37) applies to manual assignment as well
    as inference: a ``misc`` category is never created, so assigning one is
    rejected rather than silently accepted.
    """
    normalized = _normalize_category_name(name)
    if not normalized:
        raise MaintenanceError("category name must not be empty")
    if normalized == "misc":
        raise MaintenanceError(
            "the 'misc' category is forbidden (no-misc rule, PRD 7.3 / FR-37); "
            "assign a normalized, reusable category name instead"
        )
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (normalized,)).fetchone()
    if row is not None:
        return cast(int, row[0])
    cur = conn.execute(
        "INSERT INTO categories (name, display_name, created_at) VALUES (?, ?, ?)",
        (normalized, _WS_RE.sub(" ", name).strip(), _now_utc()),
    )
    return cast(int, cur.lastrowid)


def _has_active_override(
    conn: sqlite3.Connection, target_type: str, target_id: str, field_name: str
) -> bool:
    """Whether an active (non-superseded) manual override exists for the target/field."""
    row = conn.execute(
        "SELECT 1 FROM manual_overrides "
        "WHERE target_type = ? AND target_id = ? AND field_name = ? AND superseded_at IS NULL "
        "LIMIT 1",
        (target_type, target_id, field_name),
    ).fetchone()
    return row is not None


def _resolve_target(conn: sqlite3.Connection, target_id: str) -> tuple[str, str]:
    """Resolve a product-or-family ID to (target_type, stable_id).

    Returns ``("product", retailer_product_id)`` or ``("family", family_id)``.
    Raises :class:`MaintenanceError` when the ID matches neither.
    """
    row = conn.execute(
        "SELECT 1 FROM products WHERE retailer = 'ab' AND retailer_product_id = ?",
        (target_id,),
    ).fetchone()
    if row is not None:
        return ("product", target_id)
    row = conn.execute(
        "SELECT 1 FROM product_families WHERE family_id = ?", (target_id,)
    ).fetchone()
    if row is not None:
        return ("family", target_id)
    raise MaintenanceError(f"unknown product or family: {target_id!r}")


def _family_primary_product_id(conn: sqlite3.Connection, family_row_id: int) -> str:
    """Return the primary product's retailer_product_id for a family."""
    row = conn.execute(
        "SELECT p.retailer_product_id FROM products p "
        "JOIN family_members fm ON fm.product_id = p.id "
        "WHERE fm.family_id = ? AND fm.role = 'primary' LIMIT 1",
        (family_row_id,),
    ).fetchone()
    if row is None:
        raise MaintenanceError("family has no primary product")
    return cast(str, row[0])


def _active_value(conn: sqlite3.Connection, kind: str, target: str, field: str) -> str | None:
    row = conn.execute(
        "SELECT field_value FROM manual_overrides WHERE target_type=? AND target_id=? "
        "AND field_name=? AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
        (kind, target, field),
    ).fetchone()
    return cast(str, row[0]) if row else None


def _require_not_retired(conn: sqlite3.Connection, family_id: str) -> None:
    if _active_value(conn, "family", family_id, "retired") is not None:
        raise MaintenanceError("archival family cannot be re-enabled or receive members")


def retire_empty_family(conn: sqlite3.Connection, row_id: int, now: str) -> None:
    """Preserve referenced family identity; never change historical run rows."""
    if conn.execute("SELECT 1 FROM family_members WHERE family_id=?", (row_id,)).fetchone():
        return
    stable_id = conn.execute(
        "SELECT family_id FROM product_families WHERE id=?", (row_id,)
    ).fetchone()[0]
    referenced = conn.execute(
        "SELECT 1 FROM run_recurring_items WHERE family_id=? LIMIT 1", (row_id,)
    ).fetchone()
    if referenced:
        conn.execute(
            "UPDATE product_families SET enabled=0, updated_at=? WHERE id=?", (now, row_id)
        )
    else:
        conn.execute("DELETE FROM product_families WHERE id=?", (row_id,))
    if _active_value(conn, "family", stable_id, "retired") is None:
        _record_override(
            conn,
            "family",
            stable_id,
            "retired",
            "1",
            "empty family retired; historical references preserved",
            now,
        )


def _family_policy(conn: sqlite3.Connection) -> tuple[list[Any], list[Any]]:
    return (
        conn.execute(
            "SELECT id,primary_product_id,enabled,blacklist_state FROM product_families ORDER BY id"
        ).fetchall(),
        conn.execute(
            "SELECT family_id,product_id,role,approval_state "
            "FROM family_members ORDER BY product_id"
        ).fetchall(),
    )


def run_product_register(paths: GrossyPaths, canonical_url: str) -> dict[str, object]:
    """Register identity only: no fetched metadata, purchase or availability claim."""
    try:
        scan_prohibited_content(unquote(canonical_url))
    except ProhibitedContentError as exc:
        raise MaintenanceError("prohibited content in product URL") from exc
    try:
        product_id = product_id_from_url(canonical_url)
    except StoreConfigError as exc:
        raise MaintenanceError(str(exc)) from exc
    if (
        product_id is None
        or len(canonical_url) > 4096
        or "?" in canonical_url
        or "#" in canonical_url
    ):
        raise MaintenanceError("a canonical product URL of the store is required")
    if sanitize_human_text(unquote(canonical_url)) != unquote(canonical_url):
        raise MaintenanceError("unsafe product URL content")

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        existing = conn.execute(
            "SELECT canonical_url FROM products WHERE retailer='ab' AND retailer_product_id=?",
            (product_id,),
        ).fetchone()
        if existing:
            if product_id_from_url(existing[0]) != product_id:
                raise MaintenanceError("existing canonical product identity conflicts")
            return {"product_id": product_id, "changed": False}
        conn.execute(
            "INSERT INTO products (retailer_product_id,canonical_url,name,created_at,updated_at) "
            "VALUES (?,?,'Unknown product',?,?)",
            (product_id, canonical_url, now, now),
        )
        _record_override(
            conn,
            "product",
            product_id,
            "catalogue_registration",
            canonical_url,
            "explicit non-purchase catalogue registration",
            now,
        )
        return {"product_id": product_id, "changed": True}

    return _run_maintenance(paths, "product-register", _do)


# --- transactional command runner -------------------------------------------


def _run_maintenance(
    paths: GrossyPaths,
    operation: str,
    fn: Any,
) -> dict[str, object]:
    """Acquire the writer lock, run ``fn(conn, now)`` in a transaction, commit.

    ``fn`` receives the open connection and the current UTC timestamp. It must
    raise :class:`MaintenanceError` on validation failure (the transaction
    rolls back and state is unchanged). On success it returns a dict payload
    that is merged into the versioned JSON status emitted on stdout.
    """
    if not paths.database_file.exists():
        raise MaintenanceError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )

    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        conn = open_connection(paths.database_file)
        try:
            now = _now_utc()
            before = _family_policy(conn)
            payload = fn(conn, now)
            if before != _family_policy(conn):
                # Conservative invalidation, not snapshot repair or lifecycle closure.
                conn.execute(
                    "INSERT INTO run_events (run_id,event_type,detail,created_at) "
                    "SELECT id,'maintenance-policy-changed',?,? FROM planner_runs "
                    "WHERE lifecycle NOT IN ('completed','abandoned') AND NOT EXISTS "
                    "(SELECT 1 FROM run_events e WHERE e.run_id=planner_runs.id "
                    "AND e.event_type='maintenance-policy-changed')",
                    (
                        json.dumps(
                            {
                                "reason": (
                                    "family policy changed; coordinator review/reprepare required"
                                )
                            }
                        ),
                        now,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return {
        "schema_name": "grossy.history.maintenance",
        "schema_version": 1,
        "status": "success",
        "operation": operation,
        **payload,
    }


# --- set-primary --------------------------------------------------------------


def run_set_primary(
    paths: GrossyPaths, family_id: str, product_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Explicitly set the primary product of an established family (FR-45)."""
    effective_rationale = rationale or f"manual correction: set-primary {family_id} → {product_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        family_row_id = _get_family_row_id(conn, family_id)
        product_row_id = _get_product_row_id(conn, product_id)

        # The product must be a member of the family.
        member = conn.execute(
            "SELECT role FROM family_members WHERE family_id = ? AND product_id = ?",
            (family_row_id, product_row_id),
        ).fetchone()
        if member is None:
            raise MaintenanceError(
                f"product {product_id!r} is not a member of family {family_id!r}; "
                f"link it first with 'grossy history family link'"
            )
        current_role = cast(str, member[0])
        if current_role == "primary":
            recorded = _active_value(conn, "family", family_id, OVERRIDE_PRIMARY) == product_id
            if not recorded:
                _record_override(
                    conn,
                    "family",
                    family_id,
                    OVERRIDE_PRIMARY,
                    product_id,
                    effective_rationale,
                    now,
                )
            return {
                "family_id": family_id,
                "new_primary_product_id": product_id,
                "changed": False,
                "preference_recorded": not recorded,
            }

        # Demote the current primary to alternative, promote the target to primary.
        conn.execute(
            "UPDATE family_members SET role = 'alternative' "
            "WHERE family_id = ? AND role = 'primary'",
            (family_row_id,),
        )
        conn.execute(
            "UPDATE family_members SET role = 'primary' WHERE family_id = ? AND product_id = ?",
            (family_row_id, product_row_id),
        )

        _record_override(
            conn, "family", family_id, OVERRIDE_PRIMARY, product_id, effective_rationale, now
        )
        return {
            "family_id": family_id,
            "new_primary_product_id": product_id,
            "changed": True,
            "preference_recorded": True,
        }

    return _run_maintenance(paths, f"set-primary {family_id} {product_id}", _do)


# --- set-recurrence -----------------------------------------------------------


def run_set_recurrence(
    paths: GrossyPaths, family_id: str, weeks: int, rationale: str | None = None
) -> dict[str, object]:
    """Explicitly set a family recurrence interval in whole weeks (FR-45)."""
    if weeks <= 0:
        raise MaintenanceError(f"--weeks must be a positive integer, got {weeks}")
    effective_rationale = (
        rationale or f"manual correction: set-recurrence {family_id} to {weeks} weeks"
    )

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        family_row_id = _get_family_row_id(conn, family_id)
        conn.execute(
            "UPDATE product_families SET recurrence_weeks = ?, recurrence_source = 'manual', "
            "recurrence_confidence = 'high', updated_at = ? WHERE id = ?",
            (weeks, now, family_row_id),
        )
        _record_override(
            conn, "family", family_id, OVERRIDE_RECURRENCE, str(weeks), effective_rationale, now
        )
        return {"family_id": family_id, "recurrence_weeks": weeks}

    return _run_maintenance(paths, f"set-recurrence {family_id} --weeks {weeks}", _do)


# --- set-quantity -------------------------------------------------------------


def run_set_quantity(
    paths: GrossyPaths, target_id: str, quantity: int, rationale: str | None = None
) -> dict[str, object]:
    """Explicitly set a typical quantity for a product or family (FR-35, FR-69)."""
    if quantity <= 0:
        raise MaintenanceError(f"quantity must be a positive integer, got {quantity}")

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        target_type, stable_id = _resolve_target(conn, target_id)
        if target_type == "product":
            product_row_id = _get_product_row_id(conn, stable_id)
            product_id = stable_id
        else:
            family_row_id = _get_family_row_id(conn, stable_id)
            product_id = _family_primary_product_id(conn, family_row_id)
            product_row_id = _get_product_row_id(conn, product_id)

        conn.execute(
            "UPDATE products SET typical_quantity = ?, updated_at = ? WHERE id = ?",
            (quantity, now, product_row_id),
        )
        effective_rationale = (
            rationale or f"manual correction: set-quantity {target_id} to {quantity}"
        )
        _record_override(
            conn, "product", product_id, OVERRIDE_QUANTITY, str(quantity), effective_rationale, now
        )
        return {"target_id": target_id, "resolved_product_id": product_id, "quantity": quantity}

    return _run_maintenance(paths, f"set-quantity {target_id} {quantity}", _do)


# --- enable / disable ---------------------------------------------------------


def run_disable(
    paths: GrossyPaths, family_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Disable a family, suppressing history-driven recurring prefill (FR-63)."""
    effective_rationale = rationale or f"manual correction: disable family {family_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        family_row_id = _get_family_row_id(conn, family_id)
        conn.execute(
            "UPDATE product_families SET enabled = 0, updated_at = ? WHERE id = ?",
            (now, family_row_id),
        )
        _record_override(conn, "family", family_id, OVERRIDE_ENABLED, "0", effective_rationale, now)
        return {"family_id": family_id, "enabled": False}

    return _run_maintenance(paths, f"disable {family_id}", _do)


def run_enable(
    paths: GrossyPaths, family_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Re-enable a disabled family (FR-63, FR-64)."""
    effective_rationale = rationale or f"manual correction: enable family {family_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        family_row_id = _get_family_row_id(conn, family_id)
        _require_not_retired(conn, family_id)
        conn.execute(
            "UPDATE product_families SET enabled = 1, updated_at = ? WHERE id = ?",
            (now, family_row_id),
        )
        _record_override(conn, "family", family_id, OVERRIDE_ENABLED, "1", effective_rationale, now)
        return {"family_id": family_id, "enabled": True}

    return _run_maintenance(paths, f"enable {family_id}", _do)


# --- blacklist / unblacklist ---------------------------------------------------


def run_blacklist_product(
    paths: GrossyPaths, product_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Block an exact product from all automatic selection (FR-65)."""
    effective_rationale = rationale or f"manual correction: blacklist product {product_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        product_row_id = _get_product_row_id(conn, product_id)
        conn.execute(
            "UPDATE products SET blacklist_state = 'blacklisted', updated_at = ? WHERE id = ?",
            (now, product_row_id),
        )
        event_id = _record_blacklist_event(
            conn, "product", product_id, "blacklist", effective_rationale, now
        )
        return {"product_id": product_id, "blacklisted": True, "event_id": event_id}

    return _run_maintenance(paths, f"blacklist product {product_id}", _do)


def run_blacklist_family(
    paths: GrossyPaths, family_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Block every member of a family everywhere (FR-66)."""
    effective_rationale = rationale or f"manual correction: blacklist family {family_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        family_row_id = _get_family_row_id(conn, family_id)
        conn.execute(
            "UPDATE product_families SET blacklist_state = 'blacklisted', updated_at = ? "
            "WHERE id = ?",
            (now, family_row_id),
        )
        event_id = _record_blacklist_event(
            conn, "family", family_id, "blacklist", effective_rationale, now
        )
        return {"family_id": family_id, "blacklisted": True, "event_id": event_id}

    return _run_maintenance(paths, f"blacklist family {family_id}", _do)


def run_unblacklist_product(
    paths: GrossyPaths, product_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Remove an explicit product blacklist (FR-65)."""
    effective_rationale = rationale or f"manual correction: unblacklist product {product_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        product_row_id = _get_product_row_id(conn, product_id)
        conn.execute(
            "UPDATE products SET blacklist_state = 'active', updated_at = ? WHERE id = ?",
            (now, product_row_id),
        )
        event_id = _record_blacklist_event(
            conn, "product", product_id, "unblacklist", effective_rationale, now
        )
        return {"product_id": product_id, "blacklisted": False, "event_id": event_id}

    return _run_maintenance(paths, f"unblacklist product {product_id}", _do)


def run_unblacklist_family(
    paths: GrossyPaths, family_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Remove an explicit family blacklist (FR-66)."""
    effective_rationale = rationale or f"manual correction: unblacklist family {family_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        family_row_id = _get_family_row_id(conn, family_id)
        conn.execute(
            "UPDATE product_families SET blacklist_state = 'active', updated_at = ? WHERE id = ?",
            (now, family_row_id),
        )
        event_id = _record_blacklist_event(
            conn, "family", family_id, "unblacklist", effective_rationale, now
        )
        return {"family_id": family_id, "blacklisted": False, "event_id": event_id}

    return _run_maintenance(paths, f"unblacklist family {family_id}", _do)


# --- family link / unlink / merge ---------------------------------------------


def run_family_link(
    paths: GrossyPaths, product_id: str, family_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Link an exact product into a family as an approved alternative (FR-69)."""
    effective_rationale = rationale or f"manual correction: link {product_id} to {family_id}"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        product_row_id = _get_product_row_id(conn, product_id)
        family_row_id = _get_family_row_id(conn, family_id)

        _require_not_retired(conn, family_id)
        # An approved alternative repeated by the operator is a no-op.
        existing = conn.execute(
            "SELECT role, approval_state FROM family_members "
            "WHERE family_id = ? AND product_id = ?",
            (family_row_id, product_row_id),
        ).fetchone()
        if existing is not None:
            role = cast(str, existing[0])
            if role == "primary":
                raise MaintenanceError(
                    f"product {product_id!r} is already the primary of family {family_id!r}"
                )
            if existing[1] == "approved":
                return {"product_id": product_id, "family_id": family_id, "changed": False}
            raise MaintenanceError("existing member is not an approved alternative")

        # Check if the product is in a blacklisted family (prevents silently
        # bypassing a family blacklist by moving the product out).
        current_member = conn.execute(
            "SELECT fm.family_id, f.blacklist_state FROM family_members fm "
            "JOIN product_families f ON f.id = fm.family_id "
            "WHERE fm.product_id = ?",
            (product_row_id,),
        ).fetchone()
        if current_member is not None and cast(str, current_member[1]) == "blacklisted":
            source_fam_id = conn.execute(
                "SELECT family_id FROM product_families WHERE id = ?",
                (cast(int, current_member[0]),),
            ).fetchone()
            raise MaintenanceError(
                f"cannot link: product {product_id!r} is in blacklisted family "
                f"{cast(str, source_fam_id[0]) if source_fam_id else '?'}; "
                f"unblacklist the family first"
            )

        # Move out of any current family (one-family-per-product, PRD 12.1.6).
        old_family_id: int | None = None
        if current_member is not None:
            old_family_id = cast(int, current_member[0])
            conn.execute(
                "DELETE FROM family_members WHERE family_id = ? AND product_id = ?",
                (old_family_id, product_row_id),
            )
            # Retire the vacated family if it is now empty.
            remaining = conn.execute(
                "SELECT COUNT(*) FROM family_members WHERE family_id = ?",
                (old_family_id,),
            ).fetchone()
            if remaining is not None and cast(int, remaining[0]) == 0:
                retire_empty_family(conn, old_family_id, now)

        conn.execute(
            "INSERT INTO family_members (family_id, product_id, role, approval_state, "
            "provenance, confidence, created_at) VALUES (?, ?, 'alternative', 'approved', "
            "'manual-link', 'high', ?)",
            (family_row_id, product_row_id, now),
        )
        _record_override(
            conn, "product", product_id, "family_id", family_id, effective_rationale, now
        )
        return {"product_id": product_id, "family_id": family_id, "changed": True}

    return _run_maintenance(paths, f"family link {product_id} {family_id}", _do)


def run_family_create(
    paths: GrossyPaths,
    family_id: str,
    primary_product_id: str,
    name: str,
    rationale: str | None = None,
) -> dict[str, object]:
    """Create an explicit, non-recurring family with an approved primary (FR-69).

    Families are otherwise learned from purchase history; this lets the owner
    group never-purchased catalogue products (e.g. recipe ingredients) under an
    ingredient name. Alternatives are added afterwards with ``family link``.
    """
    effective_rationale = rationale or f"manual correction: create {family_id}"
    if not re.fullmatch(r"fam-[a-z0-9][a-z0-9-]{0,62}", family_id):
        raise MaintenanceError("family id must look like fam-<lowercase-letters-digits-dashes>")
    clean = " ".join(name.split())
    if not clean or len(clean) > 100 or any(not c.isprintable() for c in name.strip()):
        raise MaintenanceError("family name must be 1-100 printable characters on one line")

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        if conn.execute(
            "SELECT 1 FROM product_families WHERE family_id = ?", (family_id,)
        ).fetchone():
            raise MaintenanceError(f"family {family_id!r} already exists")
        # Retired/merged identities stay archival; never revive them under a new primary.
        if (
            _active_value(conn, "family", family_id, "retired") is not None
            or _active_value(conn, "family", family_id, "merged_into") is not None
        ):
            raise MaintenanceError(
                f"family id {family_id!r} is archival (retired or merged); choose a new id"
            )
        product_row_id = _get_product_row_id(conn, primary_product_id)
        state = conn.execute(
            "SELECT blacklist_state, observation_state FROM products WHERE id = ?",
            (product_row_id,),
        ).fetchone()
        if state[0] == "blacklisted" or state[1] == "retired":
            raise MaintenanceError(f"product {primary_product_id!r} is blacklisted or retired")
        if conn.execute(
            "SELECT 1 FROM family_members WHERE product_id = ?", (product_row_id,)
        ).fetchone():
            raise MaintenanceError(
                f"product {primary_product_id!r} already belongs to a family; unlink it first"
            )
        cur = conn.execute(
            "INSERT INTO product_families (family_id, name, enabled, blacklist_state, "
            "confidence, rationale, created_at, updated_at) "
            "VALUES (?, ?, 1, 'active', 'high', ?, ?, ?)",
            (family_id, clean, effective_rationale, now, now),
        )
        conn.execute(
            "INSERT INTO family_members (family_id, product_id, role, approval_state, "
            "provenance, confidence, created_at) VALUES (?, ?, 'primary', 'approved', "
            "'manual-create', 'high', ?)",
            (cast(int, cur.lastrowid), product_row_id, now),
        )
        _record_override(
            conn, "family", family_id, "created", primary_product_id, effective_rationale, now
        )
        return {"family_id": family_id, "primary_product_id": primary_product_id, "changed": True}

    return _run_maintenance(paths, f"family create {family_id}", _do)


def run_family_unlink(
    paths: GrossyPaths, product_id: str, rationale: str | None = None
) -> dict[str, object]:
    """Remove a product from its family (FR-69).

    A sole-member family retires: referenced shells remain disabled, otherwise
    they are deleted. If it is the primary but other members exist,
    the primary field falls back to NULL (the user must set a new primary).
    """
    effective_rationale = rationale or f"manual correction: unlink {product_id} from family"

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        product_row_id = _get_product_row_id(conn, product_id)
        member = conn.execute(
            "SELECT family_id, role FROM family_members WHERE product_id = ?",
            (product_row_id,),
        ).fetchone()
        if member is None:
            if _active_value(conn, "product", product_id, "family_id") == "":
                return {"product_id": product_id, "removed_from_family": True, "changed": False}
            raise MaintenanceError(f"product {product_id!r} is not a member of any family")
        family_row_id = cast(int, member[0])

        # FR-66: reject unlink from a blacklisted family to prevent silently
        # erasing the family blacklist state (the family row stores the
        # blacklist state; deleting it would lose the active block).
        family_row = conn.execute(
            "SELECT family_id, blacklist_state FROM product_families WHERE id = ?",
            (family_row_id,),
        ).fetchone()
        if family_row is not None and cast(str, family_row[1]) == "blacklisted":
            raise MaintenanceError(
                f"cannot unlink: product {product_id!r} is in blacklisted family "
                f"{cast(str, family_row[0])!r}; unblacklist the family first"
            )

        # If the product is the sole member, delete the empty family shell.
        member_count = conn.execute(
            "SELECT COUNT(*) FROM family_members WHERE family_id = ?", (family_row_id,)
        ).fetchone()
        count = cast(int, member_count[0]) if member_count is not None else 0

        conn.execute(
            "DELETE FROM family_members WHERE family_id = ? AND product_id = ?",
            (family_row_id, product_row_id),
        )

        if count <= 1:
            # Sole member: delete the now-empty family.
            family_id = conn.execute(
                "SELECT family_id FROM product_families WHERE id = ?", (family_row_id,)
            ).fetchone()
            if family_id is not None:
                retire_empty_family(conn, family_row_id, now)

        _record_override(conn, "product", product_id, "family_id", "", effective_rationale, now)
        return {"product_id": product_id, "removed_from_family": True}

    return _run_maintenance(paths, f"family unlink {product_id}", _do)


def run_family_merge(
    paths: GrossyPaths,
    source_family_id: str,
    target_family_id: str,
    rationale: str | None = None,
) -> dict[str, object]:
    """Merge one family into another (FR-69).

    All members of the source family are moved into the target family. The
    source primary becomes an alternative in the target (the target's
    established primary is preserved, FR-44). The empty source family retires.

    Rejects the merge when either the source or target family is blacklisted
    (to prevent silently bypassing a family blacklist, FR-66).
    """
    effective_rationale = (
        rationale or f"manual correction: merge {source_family_id} into {target_family_id}"
    )

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        if source_family_id == target_family_id:
            raise MaintenanceError("source and target family must differ")
        target_row_id = _get_family_row_id(conn, target_family_id)
        _require_not_retired(conn, target_family_id)
        if _active_value(conn, "family", source_family_id, "merged_into") == target_family_id:
            return {
                "source_family_id": source_family_id,
                "target_family_id": target_family_id,
                "members_moved": 0,
                "changed": False,
            }
        source_row_id = _get_family_row_id(conn, source_family_id)

        # Reject if either family is blacklisted (prevents silently bypassing
        # a family blacklist, FR-66).
        source_bl = conn.execute(
            "SELECT blacklist_state FROM product_families WHERE id = ?",
            (source_row_id,),
        ).fetchone()
        if source_bl is not None and cast(str, source_bl[0]) == "blacklisted":
            raise MaintenanceError(
                f"cannot merge: source family {source_family_id!r} is blacklisted; "
                f"unblacklist it first"
            )
        target_bl = conn.execute(
            "SELECT blacklist_state FROM product_families WHERE id = ?",
            (target_row_id,),
        ).fetchone()
        if target_bl is not None and cast(str, target_bl[0]) == "blacklisted":
            raise MaintenanceError(
                f"cannot merge: target family {target_family_id!r} is blacklisted; "
                f"unblacklist it first"
            )

        # Collect source members with their roles.
        members = conn.execute(
            "SELECT product_id, role FROM family_members WHERE family_id = ? ORDER BY product_id",
            (source_row_id,),
        ).fetchall()
        if not members:
            raise MaintenanceError(f"source family {source_family_id!r} has no members")

        moved = 0
        for product_row_id, _role in members:
            # Check if the product is already in the target family.
            in_target = conn.execute(
                "SELECT 1 FROM family_members WHERE family_id = ? AND product_id = ?",
                (target_row_id, cast(int, product_row_id)),
            ).fetchone()
            if in_target is not None:
                # Product is already in target; just remove from source.
                conn.execute(
                    "DELETE FROM family_members WHERE family_id = ? AND product_id = ?",
                    (source_row_id, cast(int, product_row_id)),
                )
                continue

            # The source primary becomes an alternative in the target
            # (the target's primary is preserved, FR-44).
            new_role = "alternative"
            # Remove from source family.
            conn.execute(
                "DELETE FROM family_members WHERE family_id = ? AND product_id = ?",
                (source_row_id, cast(int, product_row_id)),
            )
            # Add to target family.
            conn.execute(
                "INSERT INTO family_members (family_id, product_id, role, approval_state, "
                "provenance, confidence, created_at) VALUES (?, ?, ?, 'approved', "
                "'manual-merge', 'high', ?)",
                (target_row_id, cast(int, product_row_id), new_role, now),
            )
            moved += 1

        # Delete the now-empty source family.
        remaining = conn.execute(
            "SELECT COUNT(*) FROM family_members WHERE family_id = ?", (source_row_id,)
        ).fetchone()
        if remaining is not None and cast(int, remaining[0]) == 0:
            retire_empty_family(conn, source_row_id, now)

        _record_override(
            conn,
            "family",
            source_family_id,
            "merged_into",
            target_family_id,
            effective_rationale,
            now,
        )
        return {
            "source_family_id": source_family_id,
            "target_family_id": target_family_id,
            "members_moved": moved,
        }

    return _run_maintenance(paths, f"family merge {source_family_id} {target_family_id}", _do)


# --- set-category -------------------------------------------------------------


def run_set_category(
    paths: GrossyPaths, target_id: str, category_name: str, rationale: str | None = None
) -> dict[str, object]:
    """Explicitly assign a normalized category to a product or family (FR-38, FR-69)."""
    effective_rationale = (
        rationale or f"manual correction: set-category {target_id} to {category_name!r}"
    )

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        target_type, stable_id = _resolve_target(conn, target_id)
        cat_id = _get_category_id(conn, category_name)

        if target_type == "product":
            product_row_id = _get_product_row_id(conn, stable_id)
            conn.execute(
                "UPDATE products SET category_id = ?, updated_at = ? WHERE id = ?",
                (cat_id, now, product_row_id),
            )
        else:
            family_row_id = _get_family_row_id(conn, stable_id)
            conn.execute(
                "UPDATE product_families SET category_id = ?, updated_at = ? WHERE id = ?",
                (cat_id, now, family_row_id),
            )

        normalized = _normalize_category_name(category_name)
        _record_override(
            conn,
            target_type,
            stable_id,
            OVERRIDE_CATEGORY,
            normalized,
            effective_rationale,
            now,
        )
        return {"target_id": target_id, "category": normalized}

    return _run_maintenance(paths, f"set-category {target_id} {category_name}", _do)


# --- category rename ----------------------------------------------------------


def run_category_rename(
    paths: GrossyPaths, old_name: str, new_name: str, rationale: str | None = None
) -> dict[str, object]:
    """Rename a normalized category transactionally (FR-38, FR-69).

    All products and families referencing the old category are updated.
    The old name is replaced by the new normalized name.
    """
    effective_rationale = (
        rationale or f"manual correction: rename category {old_name!r} to {new_name!r}"
    )

    def _do(conn: sqlite3.Connection, now: str) -> dict[str, object]:
        old_normalized = _normalize_category_name(old_name)
        new_normalized = _normalize_category_name(new_name)
        if not old_normalized:
            raise MaintenanceError("old category name must not be empty")
        if not new_normalized:
            raise MaintenanceError("new category name must not be empty")
        if new_normalized == "misc":
            raise MaintenanceError(
                "the 'misc' category is forbidden (no-misc rule, PRD 7.3 / FR-37); "
                "rename to a normalized, reusable category name instead"
            )
        if old_normalized == new_normalized:
            raise MaintenanceError("old and new category names must differ")

        # The old category must exist.
        old_row = conn.execute(
            "SELECT id, display_name FROM categories WHERE name = ?", (old_normalized,)
        ).fetchone()
        if old_row is None:
            raise MaintenanceError(f"unknown category {old_name!r}")
        old_id = cast(int, old_row[0])

        # The new name must not already exist (would be an implicit merge).
        new_exists = conn.execute(
            "SELECT 1 FROM categories WHERE name = ? AND id != ?",
            (new_normalized, old_id),
        ).fetchone()
        if new_exists is not None:
            raise MaintenanceError(f"category {new_name!r} already exists; rename is not a merge")

        # Rename the category row.
        conn.execute(
            "UPDATE categories SET name = ?, display_name = ? WHERE id = ?",
            (new_normalized, _WS_RE.sub(" ", new_name).strip(), old_id),
        )

        # Update all referencing products and families.
        # (The FK reference is by row id, so the category_id column values
        # already point to the correct row; only the category row itself
        # needs the name change. No product/family update is needed.)

        _record_override(
            conn, "category", old_normalized, "name", new_normalized, effective_rationale, now
        )
        return {"old_name": old_normalized, "new_name": new_normalized}

    return _run_maintenance(paths, f"category rename {old_name} {new_name}", _do)


# --- public helpers for sync/inference to check overrides ---------------------


def product_has_category_override(conn: sqlite3.Connection, retailer_product_id: str) -> bool:
    """Whether the product's category has been manually overridden.

    Used by sync-apply to avoid overwriting a manual category assignment
    with a scanned heading (FR-38, FR-68).
    """
    return _has_active_override(conn, "product", retailer_product_id, OVERRIDE_CATEGORY)


def family_has_recurrence_override(conn: sqlite3.Connection, family_id: str) -> bool:
    """Whether the family's recurrence has been manually overridden.

    Used by inference to skip recurrence re-inference for manually-set
    cadences (FR-68).
    """
    return _has_active_override(conn, "family", family_id, OVERRIDE_RECURRENCE)


def family_has_enabled_override(conn: sqlite3.Connection, family_id: str) -> bool:
    """Whether the family's enabled state has been manually overridden."""
    return _has_active_override(conn, "family", family_id, OVERRIDE_ENABLED)


def product_has_explicit_unlink_override(
    conn: sqlite3.Connection, retailer_product_id: str
) -> bool:
    """Whether the product has been explicitly unlinked from its family.

    An explicit unlink records an override with ``field_name='family_id'`` and
    ``field_value=''`` (empty). Inference must not re-create a family for such a
    product (FR-68: synchronization must not reverse a manual correction).
    """
    row = conn.execute(
        "SELECT 1 FROM manual_overrides "
        "WHERE target_type = 'product' AND target_id = ? AND field_name = 'family_id' "
        "AND field_value = '' AND superseded_at IS NULL LIMIT 1",
        (retailer_product_id,),
    ).fetchone()
    return row is not None


def category_rename_target(conn: sqlite3.Connection, normalized_name: str) -> str | None:
    """If an active category-rename override maps this name to another, return the new name.

    Used by sync-apply's ``_get_or_create_category`` to follow a rename chain
    so that a later scan with the old heading does not re-create the old
    category (FR-38, FR-68).
    """
    row = conn.execute(
        "SELECT field_value FROM manual_overrides "
        "WHERE target_type = 'category' AND target_id = ? AND field_name = 'name' "
        "AND superseded_at IS NULL LIMIT 1",
        (normalized_name,),
    ).fetchone()
    if row is not None:
        return cast(str, row[0])
    return None


# --- YAML/blacklist conflict detection ----------------------------------------


class BlacklistConflict:
    """A blocking conflict between an explicit reference and an active blacklist.

    Per PRD 7.1, a direct YAML reference to a blacklisted product or family is
    a blocking conflict that must be reported. The planner (task 09+) calls
    :func:`check_yaml_blacklist_conflict` before applying YAML policy.
    """

    def __init__(
        self,
        target_type: str,
        target_id: str,
        blacklist_scope: str,
        message: str,
    ) -> None:
        self.target_type = target_type  # "product" | "family"
        self.target_id = target_id
        self.blacklist_scope = blacklist_scope  # "product" | "family"
        self.message = message

    def to_dict(self) -> dict[str, object]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "blacklist_scope": self.blacklist_scope,
            "message": self.message,
        }


def check_yaml_blacklist_conflict(
    conn: sqlite3.Connection,
    product_id: str,
) -> BlacklistConflict | None:
    """Check whether a YAML product reference conflicts with an active blacklist.

    Returns a :class:`BlacklistConflict` when the product is blacklisted
    (directly, FR-65) or its family is blacklisted (FR-66), and ``None``
    otherwise. This is the deterministic policy boundary that the planner
    must call before applying any YAML staple or recipe reference
    (PRD 7.1: precedence level 2 blocks level 3).
    """
    # Check product-level blacklist first (FR-65).
    row = conn.execute(
        "SELECT blacklist_state FROM products WHERE retailer = 'ab' AND retailer_product_id = ?",
        (product_id,),
    ).fetchone()
    if row is not None and cast(str, row[0]) == "blacklisted":
        return BlacklistConflict(
            target_type="product",
            target_id=product_id,
            blacklist_scope="product",
            message=(
                f"product {product_id!r} is blacklisted; "
                f"YAML reference is a blocking conflict (FR-65)"
            ),
        )

    # Check family-level blacklist (FR-66).
    row = conn.execute(
        "SELECT f.blacklist_state, f.family_id "
        "FROM product_families f "
        "JOIN family_members m ON m.family_id = f.id "
        "JOIN products p ON p.id = m.product_id "
        "WHERE p.retailer = 'ab' AND p.retailer_product_id = ?",
        (product_id,),
    ).fetchone()
    if row is not None and cast(str, row[0]) == "blacklisted":
        family_id = cast(str, row[1])
        return BlacklistConflict(
            target_type="family",
            target_id=family_id,
            blacklist_scope="family",
            message=(
                f"product {product_id!r} is in blacklisted family {family_id!r}; "
                f"YAML reference is a blocking conflict (FR-66)"
            ),
        )

    return None
