"""Observable-six history synchronization (PRD FR-6..FR-31, sections 10.2, 15, 18).

Deterministic phases that never open a browser (PRD 10.3.9):

- ``discover-prepare``: read-only preparation; migrates the database and
  reports the imported state plus the discovery requirements.
- ``sync-dry-run``: count-only unknown discovery; it reads only the imported
  source order IDs and never opens an order or inspects product contents
  (FR-13).
- ``scan-prepare``: deterministic scan preparation; it computes the exact
  unknown orders (listed minus imported, FR-9) and exposes them oldest first
  so the skill scans exactly that set (FR-14, FR-15). With zero unknown
  orders it writes the sanitized no-op synchronization summary (FR-28) and
  reports ``no-op``.
- ``scan-validate``: deterministic validation of an order-scan proposal.
- ``sync-apply``: atomic one-transaction import of every accepted unknown
  order (FR-25); a failed discovery, scan, validation, or application imports
  none of the batch (FR-26), and re-applying is idempotent (FR-27).

Sanitized reports (FR-28, FR-29) are written atomically and omit source order
IDs.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from grossy.db.connection import open_connection
from grossy.db.lock import WriterLock
from grossy.db.migrations import get_schema_version, run_migrations
from grossy.fsutil import atomic_write_text, ensure_private_dir
from grossy.history.proposal import (
    OBSERVABLE_ORDER_WINDOW,
    ORDER_SCAN_SCHEMA_NAME,
    HistoryProposalError,
    ListedOrdersProposal,
    OrderScanProposal,
    ScannedOrder,
    ScannedProduct,
    validate_listed_orders,
    validate_order_scan,
)
from grossy.paths import GrossyPaths

SYNC_REPORT_SCHEMA_NAME = "grossy.history.sync-report"
SYNC_REPORT_SCHEMA_VERSION = 1

_RETAILER = "ab"
_WHITESPACE_RE = re.compile(r"\s+")


class HistorySyncError(Exception):
    """Raised when a history synchronization phase fails."""


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _stamp() -> str:
    """A filesystem-safe UTC timestamp for report file names."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise HistorySyncError(f"cannot read proposal {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise HistorySyncError(f"proposal {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HistorySyncError(f"proposal {path} must be a JSON object")
    return data


def _order_sort_key(order: Any) -> tuple[str, str]:
    """Oldest-first ordering (FR-14); source ID breaks date ties deterministically."""
    return (order.order_date, order.source_order_id)


def _reports_dir(paths: GrossyPaths) -> Path:
    return ensure_private_dir(paths.state_dir / "history")


def _imported_source_order_ids(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT source_order_id FROM source_orders WHERE retailer = ?", (_RETAILER,)
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise HistorySyncError(
            "database has no source_orders table; run 'grossy init' first"
        ) from exc
    return {row[0] for row in rows}


def _existing_product_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = conn.execute("SELECT retailer, retailer_product_id FROM products").fetchall()
    return {(row[0], row[1]) for row in rows}


def _category_name_from_heading(heading: str) -> str:
    return _WHITESPACE_RE.sub(" ", heading).strip().lower()


def _get_or_create_category(conn: sqlite3.Connection, heading: str | None, now: str) -> int | None:
    """Create or reuse a category from a scannable heading (non-inferential).

    Task 04 preserves the visible heading as a category; confidence-based
    normalization and reviewable assignment are deferred to task 05.
    """
    if heading is None:
        return None
    name = _category_name_from_heading(heading)
    if not name:
        return None
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return cast(int, row[0])
    cur = conn.execute(
        "INSERT INTO categories (name, display_name, created_at) VALUES (?, ?, ?)",
        (name, heading.strip(), now),
    )
    return cast(int, cur.lastrowid)


@dataclass(frozen=True)
class DiscoverPrepareResult:
    """Result of the observable-six discovery preparation phase."""

    database_exists: bool
    schema_version: int
    imported_order_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_name": "grossy.history.discover-prepare",
            "schema_version": 1,
            "status": "ready",
            "database_exists": self.database_exists,
            "database_schema_version": self.schema_version,
            "imported_order_count": self.imported_order_count,
            "required_order_window": OBSERVABLE_ORDER_WINDOW,
            "listed_orders_schema": "grossy.history.listed-orders.v1",
            "requirements": [
                "inspect the rendered AB.gr order-history list",
                "extract exactly the six currently listed dated orders",
                "exclude the 'All orders' union entry (no per-order timing)",
                "for each order capture a numeric stable source order ID, a "
                "DD/MM/YYYY displayed label, a normalized ISO order date, and a "
                "stable navigation URL",
                "emit a grossy.history.listed-orders.v1 proposal and run sync-dry-run",
            ],
        }


@dataclass(frozen=True)
class SyncDryRunResult:
    """Result of the count-only dry-run phase (FR-13)."""

    listed_order_count: int
    unknown_order_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_name": "grossy.history.sync-dry-run",
            "schema_version": 1,
            "status": "dry-run",
            "listed_order_count": self.listed_order_count,
            "unknown_order_count": self.unknown_order_count,
            "orders_opened": 0,
            "product_contents_inspected": False,
        }


@dataclass(frozen=True)
class SyncPrepareResult:
    """Result of the deterministic scan-preparation phase (FR-9, FR-14, FR-15).

    ``unknown_orders`` carries the exact orders the skill must scan, oldest
    first. It is machine output on stdout and therefore carries the stable
    source order IDs, dates, labels, and navigation URLs the skill needs to
    navigate; human-facing reports still omit them (FR-29). The phase is
    count-only with respect to order contents: no order is opened and no
    product content is inspected (FR-13).
    """

    status: str
    listed_order_count: int
    unknown_order_count: int
    unknown_orders: tuple[dict[str, str], ...]
    report_path: Path | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_name": "grossy.history.scan-prepare",
            "schema_version": 1,
            "status": self.status,
            "listed_order_count": self.listed_order_count,
            "unknown_order_count": self.unknown_order_count,
            "unknown_orders": [dict(order) for order in self.unknown_orders],
            "orders_opened": 0,
            "product_contents_inspected": False,
        }
        if self.report_path is not None:
            payload["report_path"] = str(self.report_path)
        return payload


@dataclass(frozen=True)
class SyncScanValidateResult:
    """Result of the deterministic order-scan validation phase (FR-24)."""

    order_count: int
    product_card_count: int
    checksum: str
    warnings: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_name": "grossy.history.scan-validate",
            "schema_version": 1,
            "status": "accepted",
            "schema": ORDER_SCAN_SCHEMA_NAME,
            "order_count": self.order_count,
            "product_card_count": self.product_card_count,
            "checksum": self.checksum,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class _ApplyStats:
    """Facts computed during an atomic apply (no source order IDs retained)."""

    orders_in_scan: int
    imported_orders: int
    already_present_orders: int
    imported_product_presence: int
    new_products: int
    changed_products: int
    displayed_count_discrepancies: int
    warnings: tuple[str, ...]
    listed_order_count: int = 0
    unknown_order_count: int = 0
    # Sanitized product facts (no source order IDs; FR-29).
    new_product_facts: tuple[tuple[str, str], ...] = ()  # (name, category or "")
    changed_product_facts: tuple[tuple[str, str, str], ...] = ()  # (name, category or "", reason)


@dataclass(frozen=True)
class SyncApplyResult:
    """Result of the atomic one-transaction apply phase (FR-25..FR-28)."""

    status: str
    checksum: str
    stats: _ApplyStats
    report_path: Path | None = None

    def to_dict(self) -> dict[str, object]:
        s = self.stats
        payload: dict[str, object] = {
            "schema_name": "grossy.history.sync-apply",
            "schema_version": 1,
            "status": self.status,
            "proposal_checksum": self.checksum,
            "orders_in_scan": s.orders_in_scan,
            "imported_orders": s.imported_orders,
            "already_present_orders": s.already_present_orders,
            "imported_product_presence": s.imported_product_presence,
            "new_products": s.new_products,
            "changed_products": s.changed_products,
            "displayed_count_discrepancies": s.displayed_count_discrepancies,
            "warnings": list(s.warnings),
        }
        if s.listed_order_count:
            payload["listed_order_count"] = s.listed_order_count
            payload["unknown_order_count"] = s.unknown_order_count
        if self.report_path is not None:
            payload["report_path"] = str(self.report_path)
        return payload


# --- phase entry points ------------------------------------------------------


def run_discover_prepare(paths: GrossyPaths) -> DiscoverPrepareResult:
    """Prepare observable-six discovery: migrate and report imported state."""
    if not paths.database_file.exists():
        raise HistorySyncError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )
    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        # Regenerate any report whose post-commit file write failed on a prior
        # apply (PRD 18). discover-prepare is the first phase of every sync, so
        # recovery is guaranteed to run before a zero-unknown no-op short-circuit.
        _recover_pending_sync_reports(paths)
        conn = open_connection(paths.database_file)
        try:
            imported = len(_imported_source_order_ids(conn))
        finally:
            conn.close()
    return DiscoverPrepareResult(
        database_exists=True,
        schema_version=get_schema_version(paths.database_file),
        imported_order_count=imported,
    )


def run_sync_dry_run(proposal_path: Path, paths: GrossyPaths) -> SyncDryRunResult:
    """Count-only dry-run: unknown = listed - imported, without a date cursor."""
    data = _load_json(proposal_path)
    proposal = _validate_listed_or_fail(data)

    if not paths.database_file.exists():
        raise HistorySyncError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )
    conn = open_connection(paths.database_file)
    try:
        imported = _imported_source_order_ids(conn)
    finally:
        conn.close()

    listed_ids = [order.source_order_id for order in proposal.orders]
    unknown = [oid for oid in listed_ids if oid not in imported]
    return SyncDryRunResult(
        listed_order_count=len(listed_ids),
        unknown_order_count=len(unknown),
    )


def run_scan_prepare(listed_orders_path: Path, paths: GrossyPaths) -> SyncPrepareResult:
    """Deterministic scan preparation: expose the exact unknown orders oldest first.

    Implements the discovery set-difference of FR-9 (listed minus
    already-imported, no date cursor per FR-10) and fixes the oldest-first
    scan order of FR-14. The result is machine output: it carries the stable
    source order IDs, dates, labels, and navigation URLs the skill needs to
    navigate each unknown order (FR-15). No order is opened and no product
    content is inspected (FR-13).

    When the unknown set is empty, the phase writes the timestamped sanitized
    no-op synchronization summary (FR-28, FR-29) and reports ``no-op`` so a
    normal retry always leaves the required report.
    """
    data = _load_json(listed_orders_path)
    listed = _validate_listed_or_fail(data)

    if not paths.database_file.exists():
        raise HistorySyncError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )
    conn = open_connection(paths.database_file)
    try:
        imported = _imported_source_order_ids(conn)
    finally:
        conn.close()

    unknown_orders = tuple(
        {
            "source_order_id": order.source_order_id,
            "displayed_label": order.displayed_label,
            "order_date": order.order_date,
            "navigation_url": order.navigation_url,
        }
        for order in sorted(
            (o for o in listed.orders if o.source_order_id not in imported),
            key=_order_sort_key,
        )
    )

    report_path: Path | None = None
    status = "ready"
    if not unknown_orders:
        status = "no-op"
        report_path = _write_noop_report(paths, listed_order_count=len(listed.orders))

    return SyncPrepareResult(
        status=status,
        listed_order_count=len(listed.orders),
        unknown_order_count=len(unknown_orders),
        unknown_orders=unknown_orders,
        report_path=report_path,
    )


def run_scan_validate(proposal_path: Path) -> SyncScanValidateResult:
    """Deterministically validate an order-scan proposal (FR-22, FR-24)."""
    data = _load_json(proposal_path)
    proposal = _validate_scan_or_fail(data)
    warnings = tuple(
        warning
        for warning in (_scan_mismatch_warning(order) for order in proposal.orders)
        if warning is not None
    )
    product_cards = sum(len(order.products) for order in proposal.orders)
    return SyncScanValidateResult(
        order_count=len(proposal.orders),
        product_card_count=product_cards,
        checksum=_sha256_file(proposal_path),
        warnings=warnings,
    )


def run_sync_apply(
    proposal_path: Path,
    paths: GrossyPaths,
    listed_orders_path: Path | None = None,
) -> SyncApplyResult:
    """Apply a validated order-scan proposal in one atomic transaction.

    When ``listed_orders_path`` is supplied (FR-10.3.6, FR-23, FR-24), the
    scan's order identities are bound to the discovered unknown set: every
    scanned order must appear in the listed-orders window with matching date
    and navigation URL, and the scan must contain exactly the unknown orders
    (listed minus already-imported). This prevents partial scans or out-of-
    window IDs from being committed (PRD FR-26, FR-10.3.6).
    """
    data = _load_json(proposal_path)
    proposal = _validate_scan_or_fail(data)
    checksum = _sha256_file(proposal_path)

    if not paths.database_file.exists():
        raise HistorySyncError(
            f"database not found at {paths.database_file}; run 'grossy init' first"
        )

    # Load and validate the listed-orders proposal for discovery binding.
    listed: ListedOrdersProposal | None = None
    listed_order_count = 0
    unknown_order_count = 0
    if listed_orders_path is not None:
        listed_data = _load_json(listed_orders_path)
        listed = _validate_listed_or_fail(listed_data)

    with WriterLock(paths.state_dir):
        run_migrations(paths.database_file, paths.backups_dir)
        # Regenerate any report whose post-commit file write failed on a prior
        # apply (PRD 18): deterministic, byte-for-byte from persisted facts.
        _recover_pending_sync_reports(paths)
        conn = open_connection(paths.database_file)
        try:
            pre_existing = _existing_product_keys(conn)
            imported = _imported_source_order_ids(conn)

            if listed is not None:
                listed_order_count = len(listed.orders)
                listed_ids = {o.source_order_id for o in listed.orders}
                expected_unknown = listed_ids - imported
                scan_ids = {o.source_order_id for o in proposal.orders}

                # FR-23: the scan must contain exactly the unknown orders.
                missing = expected_unknown - scan_ids
                out_of_window = scan_ids - listed_ids
                already_imported = scan_ids & imported
                if missing:
                    raise HistorySyncError(
                        f"order-scan proposal is missing {len(missing)} "
                        f"expected unknown order(s) from the listed-orders "
                        f"discovery; the scan must cover the full unknown set"
                    )
                if out_of_window:
                    raise HistorySyncError(
                        f"order-scan proposal contains {len(out_of_window)} order(s) "
                        f"not present in the listed-orders window; "
                        f"out-of-window or 'All orders'-derived IDs are rejected"
                    )
                if already_imported:
                    raise HistorySyncError(
                        f"order-scan proposal contains {len(already_imported)} "
                        f"already-imported order(s); a scan must cover exactly the "
                        f"unknown orders from the listed-orders discovery and must "
                        f"not re-scan known orders"
                    )

                # Verify metadata consistency between listed and scan.
                listed_by_id = {o.source_order_id: o for o in listed.orders}
                for scan_order in proposal.orders:
                    listed_order = listed_by_id[scan_order.source_order_id]
                    if scan_order.order_date != listed_order.order_date:
                        raise HistorySyncError(
                            f"order-scan order_date {scan_order.order_date!r} "
                            f"does not match listed-orders date "
                            f"{listed_order.order_date!r}"
                        )
                    if scan_order.navigation_url != listed_order.navigation_url:
                        raise HistorySyncError(
                            "order-scan navigation_url does not match "
                            "the listed-orders navigation URL"
                        )

                unknown_order_count = len(expected_unknown)

            orders = sorted(proposal.orders, key=_order_sort_key)
            stats = _apply_orders(
                conn,
                orders,
                pre_existing,
                imported,
                checksum,
                listed_order_count=listed_order_count,
                unknown_order_count=unknown_order_count,
            )
            # Build the sanitized report texts and persist the recoverable
            # facts in the SAME transaction as the import (PRD 18): if a
            # report file write fails after commit, the persisted facts carry
            # report_written=0 ("report recovery required") and the report is
            # regenerated deterministically on a later sync phase. The import
            # itself is already durable in SQLite (SQLite truth retained).
            report_dir = _reports_dir(paths)
            stamp = _stamp()
            md_path = report_dir / f"sync-apply-{stamp}.md"
            json_path = report_dir / f"sync-apply-{stamp}.json"
            # FR-29: the known source order IDs to redact from untrusted report
            # text are all imported orders (visible post-apply in this
            # transaction) plus the discovered listed-orders window.
            redaction_ids = _imported_source_order_ids(conn)
            if listed is not None:
                redaction_ids = redaction_ids | {o.source_order_id for o in listed.orders}
            json_text, md_text = _build_apply_report_texts(stats, checksum, redaction_ids)
            report_key = f"apply:{checksum}"
            _persist_sync_report(
                conn,
                report_key=report_key,
                phase="sync-apply",
                checksum=checksum,
                json_text=json_text,
                md_text=md_text,
                md_path=md_path,
                json_path=json_path,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Write the report files and mark the persisted facts written, still
        # under the writer lock so the DB update is single-writer. A file-write
        # failure propagates (I/O error) while the facts stay report_written=0
        # for deterministic regeneration (PRD 18).
        report_path = _write_apply_report_files(
            paths,
            report_key=report_key,
            md_path=md_path,
            json_path=json_path,
            json_text=json_text,
            md_text=md_text,
        )
    status = "applied" if stats.imported_orders > 0 else "no-op"
    return SyncApplyResult(status=status, checksum=checksum, stats=stats, report_path=report_path)


# --- validation wrappers -----------------------------------------------------


def _validate_listed_or_fail(data: dict[str, Any]) -> ListedOrdersProposal:
    try:
        return validate_listed_orders(data)
    except HistoryProposalError as exc:
        raise HistorySyncError(f"listed-orders proposal rejected: {exc}") from exc


def _validate_scan_or_fail(data: dict[str, Any]) -> OrderScanProposal:
    try:
        return validate_order_scan(data)
    except HistoryProposalError as exc:
        raise HistorySyncError(f"order-scan proposal rejected: {exc}") from exc


# --- atomic apply ------------------------------------------------------------


def _apply_orders(
    conn: sqlite3.Connection,
    orders: list[ScannedOrder],
    pre_existing: set[tuple[str, str]],
    imported: set[str],
    checksum: str,
    *,
    listed_order_count: int = 0,
    unknown_order_count: int = 0,
) -> _ApplyStats:
    """Import every accepted unknown order in the caller's open transaction.

    The caller owns ``BEGIN``/commit/rollback; any exception here propagates so
    the whole batch rolls back (FR-25, FR-26). Already-imported orders are
    skipped so a re-apply is idempotent (FR-27).
    """
    now = _now_utc()
    # Snapshot the products that existed before this apply; new/changed
    # accounting is based on this original set, not the working set that
    # _upsert_product grows as it creates new products.
    original_pre_existing = set(pre_existing)
    new_product_facts: dict[str, tuple[str, str]] = {}  # product_id -> (name, category)
    changed_product_facts: dict[
        str, tuple[str, str, str]
    ] = {}  # product_id -> (name, category, reason)
    imported_orders = 0
    already_present = 0
    product_presence = 0
    discrepancies = 0
    warnings: list[str] = []

    for order in orders:
        if order.source_order_id in imported:
            already_present += 1
            continue

        mismatch_warning = _scan_mismatch_warning(order)
        if mismatch_warning is not None:
            discrepancies += 1
            warnings.append(mismatch_warning)

        cur = conn.execute(
            "INSERT INTO source_orders (retailer, source_order_id, displayed_label, "
            "order_date, import_timestamp, displayed_product_count, "
            "stabilized_product_count, diagnostics_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _RETAILER,
                order.source_order_id,
                order.displayed_label,
                order.order_date,
                now,
                order.displayed_product_count,
                len(order.products),
                json.dumps({"proposal_checksum": checksum}, sort_keys=True),
                now,
            ),
        )
        source_order_row_id = cast(int, cur.lastrowid)
        imported_orders += 1

        for product in order.products:
            key = (_RETAILER, product.product_id)
            pre_existing_row = (
                _read_product_row(conn, key) if key in original_pre_existing else None
            )
            product_row_id = _upsert_product(
                conn, product, order.order_date, key, pre_existing, now
            )
            if key not in original_pre_existing:
                new_product_facts[product.product_id] = (
                    product.title,
                    product.category_heading or "",
                )
            else:
                old_row = pre_existing_row
                assert old_row is not None
                # A pre-existing product is "changed" if its descriptive facts
                # or observed-date range were extended by this scan.
                if _is_product_changed(old_row, product, order.order_date):
                    changed_product_facts[product.product_id] = (
                        product.title,
                        product.category_heading or "",
                        _change_reason(old_row, product, order.order_date),
                    )
            conn.execute(
                "INSERT INTO source_order_products (source_order_id, product_id, created_at) "
                "VALUES (?, ?, ?)",
                (source_order_row_id, product_row_id, now),
            )
            product_presence += 1

    return _ApplyStats(
        orders_in_scan=len(orders),
        imported_orders=imported_orders,
        already_present_orders=already_present,
        imported_product_presence=product_presence,
        new_products=len(new_product_facts),
        changed_products=len(changed_product_facts),
        displayed_count_discrepancies=discrepancies,
        warnings=tuple(warnings),
        listed_order_count=listed_order_count,
        unknown_order_count=unknown_order_count,
        new_product_facts=tuple(sorted(new_product_facts.values())),
        changed_product_facts=tuple(sorted(changed_product_facts.values())),
    )


def _order_count_mismatch(order: Any) -> tuple[int, int] | None:
    """Return ``(displayed, stabilized)`` if the order has a count mismatch."""
    if order.displayed_product_count is not None and order.displayed_product_count != len(
        order.products
    ):
        return (order.displayed_product_count, len(order.products))
    return None


def _scan_mismatch_warning(order: Any) -> str | None:
    """Sanitized per-order count-mismatch diagnostic (FR-20, FR-21)."""
    mismatch = _order_count_mismatch(order)
    if mismatch is None:
        return None
    return (
        f"order dated {order.order_date}: displayed count {mismatch[0]} differs "
        f"from stabilized unique product count {mismatch[1]}; the stabilized set was used"
    )


def _upsert_product(
    conn: sqlite3.Connection,
    product: ScannedProduct,
    order_date: str,
    key: tuple[str, str],
    pre_existing: set[tuple[str, str]],
    now: str,
) -> int:
    """Insert or update a product from a scanned card; return its row id.

    ``purchase_order_count`` and observed dates are updated here for every
    appearance in a newly imported order (FR-33). The observed dates use the
    owning order's normalized date.
    """
    category_id = _get_or_create_category(conn, product.category_heading, now)
    if key in pre_existing:
        row = conn.execute(
            "SELECT id, first_observed_date, last_observed_date FROM products "
            "WHERE retailer = ? AND retailer_product_id = ?",
            key,
        ).fetchone()
        product_row_id = cast(int, row[0])
        first = min(filter(None, [row[1], order_date]), default=None)
        last = max(filter(None, [row[2], order_date]), default=None)
        conn.execute(
            "UPDATE products SET name = ?, brand = ?, package_amount = ?, "
            "package_unit = ?, package_count = ?, package_display = ?, "
            "category_id = ?, first_observed_date = ?, last_observed_date = ?, "
            "purchase_order_count = purchase_order_count + 1, updated_at = ? "
            "WHERE id = ?",
            (
                product.title,
                product.brand,
                product.package_amount,
                product.package_unit,
                product.package_count,
                product.package_display,
                category_id,
                first,
                last,
                now,
                product_row_id,
            ),
        )
        return product_row_id

    cur = conn.execute(
        "INSERT INTO products (retailer, retailer_product_id, canonical_url, name, "
        "brand, package_amount, package_unit, package_count, package_display, "
        "category_id, first_observed_date, last_observed_date, purchase_order_count, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            _RETAILER,
            product.product_id,
            product.canonical_url,
            product.title,
            product.brand,
            product.package_amount,
            product.package_unit,
            product.package_count,
            product.package_display,
            category_id,
            order_date,
            order_date,
            now,
            now,
        ),
    )
    pre_existing.add(key)
    return cast(int, cur.lastrowid)


def _read_product_row(conn: sqlite3.Connection, key: tuple[str, str]) -> tuple[Any, ...]:
    """Read a product's descriptive facts and observed dates (pre-update)."""
    row = conn.execute(
        "SELECT name, brand, package_amount, package_unit, package_count, "
        "package_display, first_observed_date, last_observed_date FROM products "
        "WHERE retailer = ? AND retailer_product_id = ?",
        key,
    ).fetchone()
    assert row is not None
    return cast("tuple[Any, ...]", row)


def _is_product_changed(old: tuple[Any, ...], product: ScannedProduct, order_date: str) -> bool:
    """Whether a pre-existing product's descriptive facts or dates changed.

    ``old`` is the product row read *before* the update; comparing against the
    pre-update values is what makes the observed-date extension detectable.
    """
    (
        old_name,
        old_brand,
        old_pkg_amount,
        old_pkg_unit,
        old_pkg_count,
        old_pkg_display,
        old_first,
        old_last,
    ) = old
    first_changed = old_first is None or order_date < old_first
    last_changed = old_last is None or order_date > old_last
    facts_changed = (
        old_name != product.title
        or old_brand != product.brand
        or _num_differs(old_pkg_amount, product.package_amount)
        or old_pkg_unit != product.package_unit
        or old_pkg_count != product.package_count
        or old_pkg_display != product.package_display
    )
    return bool(first_changed or last_changed or facts_changed)


def _num_differs(old: Any, new: Any) -> bool:
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    try:
        return float(old) != float(new)
    except (TypeError, ValueError):
        return bool(old != new)


def _change_reason(old: tuple[Any, ...], product: ScannedProduct, order_date: str) -> str:
    """Sanitized reason a product was changed (no source order IDs)."""
    reasons: list[str] = []
    (
        old_name,
        old_brand,
        old_pkg_amount,
        old_pkg_unit,
        old_pkg_count,
        old_pkg_display,
        old_first,
        old_last,
    ) = old
    if old_first is None or order_date < old_first:
        reasons.append("first observed date extended earlier")
    if old_last is None or order_date > old_last:
        reasons.append("last observed date extended later")
    if old_name != product.title:
        reasons.append("name updated")
    if old_brand != product.brand:
        reasons.append("brand updated")
    if _num_differs(old_pkg_amount, product.package_amount):
        reasons.append("package amount updated")
    if old_pkg_unit != product.package_unit:
        reasons.append("package unit updated")
    if old_pkg_count != product.package_count:
        reasons.append("package count updated")
    if old_pkg_display != product.package_display:
        reasons.append("package display updated")
    return "; ".join(reasons) if reasons else "facts updated"


# --- sanitized reports (FR-28, FR-29) ---------------------------------------


def _redact_source_order_ids(text: str, order_ids: set[str]) -> str:
    """Redact known source order IDs from report text (FR-29).

    Uses digit-boundary matching so an ID is only replaced when it is not part
    of a longer numeric run, avoiding corrupting unrelated counts or the hex
    proposal checksum. Longer IDs are matched first so an ID that is a prefix
    of another is not partially redacted.
    """
    ids = sorted((oid for oid in order_ids if oid), key=len, reverse=True)
    if not ids:
        return text
    pattern = re.compile(r"(?<!\d)(" + "|".join(re.escape(oid) for oid in ids) + r")(?!\d)")
    return pattern.sub("[redacted]", text)


def _sanitize_untrusted_text(value: str, order_ids: set[str]) -> str:
    """Sanitize an untrusted report field (product title/category) (FR-29).

    Collapses all whitespace (including newlines and tabs) to single spaces so
    a multiline or control-laden title cannot inject report/markdown structure,
    then redacts known source order IDs so they never reach the report even if
    embedded in untrusted text.
    """
    collapsed = _WHITESPACE_RE.sub(" ", value).strip()
    return _redact_source_order_ids(collapsed, order_ids)


def _build_apply_report_texts(
    stats: _ApplyStats, checksum: str, redaction_ids: set[str]
) -> tuple[str, str]:
    """Build the sanitized apply-report JSON and Markdown texts.

    Source order IDs are never included (FR-29): untrusted product facts are
    sanitized and known source order IDs are redacted from all report text.
    Only counts, sanitized product facts, and diagnostics remain. The returned
    texts are the exact bytes persisted for recovery and written to files.
    """
    generated_at = _now_utc()

    new_facts = [
        {
            "name": _sanitize_untrusted_text(name, redaction_ids),
            "category": _sanitize_untrusted_text(cat, redaction_ids),
        }
        for name, cat in stats.new_product_facts
    ]
    changed_facts = [
        {
            "name": _sanitize_untrusted_text(name, redaction_ids),
            "category": _sanitize_untrusted_text(cat, redaction_ids),
            "reason": reason,
        }
        for name, cat, reason in stats.changed_product_facts
    ]

    payload = {
        "schema_name": SYNC_REPORT_SCHEMA_NAME,
        "schema_version": SYNC_REPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "phase": "sync-apply",
        "proposal_checksum": checksum,
        "discovery": {
            "orders_in_scan": stats.orders_in_scan,
            "imported_orders": stats.imported_orders,
            "already_present_orders": stats.already_present_orders,
            "listed_order_count": stats.listed_order_count,
            "unknown_order_count": stats.unknown_order_count,
        },
        "import": {
            "product_presence_records": stats.imported_product_presence,
            "new_products": stats.new_products,
            "new_product_facts": new_facts,
            "changed_products": stats.changed_products,
            "changed_product_facts": changed_facts,
        },
        "diagnostics": {
            "displayed_count_discrepancies": stats.displayed_count_discrepancies,
            "warnings": list(stats.warnings),
        },
        "category_family_recurrence": {
            "status": "deferred",
            "note": (
                "Category normalization, family inference, and recurrence are "
                "implemented in task 05."
            ),
        },
        "review": {
            "links_requiring_review": [],
            "note": "Family links and uncertain category assignments require review (task 05+).",
        },
    }
    json_text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    md_text = _render_apply_markdown(generated_at, checksum, stats, redaction_ids)
    return json_text, md_text


def _write_apply_report_files(
    paths: GrossyPaths,
    *,
    report_key: str,
    md_path: Path,
    json_path: Path,
    json_text: str,
    md_text: str,
) -> Path:
    """Write the persisted apply-report files and mark the facts written.

    Called after the import transaction commits, still under the writer lock.
    If a report for ``report_key`` is already written (e.g. an idempotent
    no-op re-apply), the original record and its files are kept. A file-write
    failure propagates as an I/O error while the persisted facts remain
    ``report_written = 0`` ("report recovery required"), so the report is
    regenerated deterministically on a later sync phase (PRD 18).
    """
    conn = open_connection(paths.database_file)
    try:
        row = conn.execute(
            "SELECT report_written, report_md_path FROM history_sync_reports WHERE report_key = ?",
            (report_key,),
        ).fetchone()
        if row is not None and row[0] == 1:
            return Path(row[1])
        atomic_write_text(json_path, json_text)
        atomic_write_text(md_path, md_text)
        conn.execute(
            "UPDATE history_sync_reports SET report_written = 1, updated_at = ? "
            "WHERE report_key = ?",
            (_now_utc(), report_key),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return md_path


def _persist_sync_report(
    conn: sqlite3.Connection,
    *,
    report_key: str,
    phase: str,
    checksum: str | None,
    json_text: str,
    md_text: str,
    md_path: Path,
    json_path: Path,
) -> None:
    """Persist recoverable report facts in the caller's open transaction.

    The fully rendered texts and intended file paths are stored with
    ``report_written = 0`` so a post-commit file-write failure leaves a
    deterministic regeneration source (PRD 18). A report already written for
    the same key is left untouched so a later no-op re-apply cannot overwrite
    the record of the successful import.
    """
    now = _now_utc()
    existing = conn.execute(
        "SELECT report_written FROM history_sync_reports WHERE report_key = ?",
        (report_key,),
    ).fetchone()
    if existing is not None and existing[0] == 1:
        return
    conn.execute(
        """INSERT INTO history_sync_reports
           (report_key, phase, proposal_checksum, payload_json, report_md,
            report_written, report_md_path, report_json_path, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
           ON CONFLICT(report_key) DO UPDATE SET
             phase = excluded.phase,
             proposal_checksum = excluded.proposal_checksum,
             payload_json = excluded.payload_json,
             report_md = excluded.report_md,
             report_written = 0,
             report_md_path = excluded.report_md_path,
             report_json_path = excluded.report_json_path,
             updated_at = excluded.updated_at""",
        (report_key, phase, checksum, json_text, md_text, str(md_path), str(json_path), now, now),
    )


def _recover_pending_sync_reports(paths: GrossyPaths) -> list[Path]:
    """Regenerate report files whose post-commit write failed (PRD 18).

    Must run after migrations (the ``history_sync_reports`` table exists).
    Reads the persisted facts for every report still marked
    ``report_written = 0`` ("report recovery required"), re-writes the files
    byte-for-byte from the stored texts, and marks them written. Deterministic
    and idempotent: the stored texts are exactly the original report content.
    Returns the recovered Markdown paths.
    """
    if not paths.database_file.exists():
        return []
    conn = open_connection(paths.database_file)
    recovered: list[Path] = []
    try:
        rows = conn.execute(
            "SELECT report_key, payload_json, report_md, report_md_path, report_json_path "
            "FROM history_sync_reports WHERE report_written = 0"
        ).fetchall()
        for report_key, payload_json, report_md, md_path_str, json_path_str in rows:
            md_path = Path(md_path_str)
            json_path = Path(json_path_str)
            _reports_dir(paths)
            atomic_write_text(json_path, payload_json)
            atomic_write_text(md_path, report_md)
            conn.execute(
                "UPDATE history_sync_reports SET report_written = 1, updated_at = ? "
                "WHERE report_key = ?",
                (_now_utc(), report_key),
            )
            recovered.append(md_path)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return recovered


def _write_noop_report(paths: GrossyPaths, listed_order_count: int) -> Path:
    """Write the timestamped sanitized no-op synchronization summary (FR-28, FR-29).

    A normal synchronization with zero unknown orders still must leave the
    required sanitized summary. Source order IDs are never written (FR-29);
    the report carries only counts, deferred task-05 notes, and diagnostics.
    """
    report_dir = _reports_dir(paths)
    stamp = _stamp()
    generated_at = _now_utc()

    payload = {
        "schema_name": SYNC_REPORT_SCHEMA_NAME,
        "schema_version": SYNC_REPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "phase": "scan-prepare",
        "status": "no-op",
        "proposal_checksum": None,
        "discovery": {
            "orders_in_scan": 0,
            "imported_orders": 0,
            "already_present_orders": 0,
            "listed_order_count": listed_order_count,
            "unknown_order_count": 0,
        },
        "import": {
            "product_presence_records": 0,
            "new_products": 0,
            "new_product_facts": [],
            "changed_products": 0,
            "changed_product_facts": [],
        },
        "diagnostics": {
            "displayed_count_discrepancies": 0,
            "warnings": [],
        },
        "category_family_recurrence": {
            "status": "deferred",
            "note": (
                "Category normalization, family inference, and recurrence are "
                "implemented in task 05."
            ),
        },
        "review": {
            "links_requiring_review": [],
            "note": "Family links and uncertain category assignments require review (task 05+).",
        },
    }
    json_text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    json_path = report_dir / f"sync-noop-{stamp}.json"
    atomic_write_text(json_path, json_text)
    md_path = report_dir / f"sync-noop-{stamp}.md"
    atomic_write_text(md_path, _render_noop_markdown(generated_at, listed_order_count))
    return md_path


def _render_noop_markdown(generated_at: str, listed_order_count: int) -> str:
    lines = [
        "# Grossy history synchronization report",
        "",
        f"- Generated at: {generated_at}",
        "- Phase: scan-prepare",
        "- Status: no-op (nothing to synchronize)",
        "",
        "## Discovery",
        f"- Listed orders (observable window): {listed_order_count}",
        "- Unknown orders at discovery: 0",
        "- Orders scanned this synchronization: 0",
        "- Imported orders: 0",
        "- Already present (idempotent skip): 0",
        "",
        "## Import",
        "- Product-presence records: 0",
        "- New products: 0",
        "- Changed products: 0",
        "",
        "## Diagnostics",
        "- Displayed-count discrepancies: 0",
        "- Warnings: none",
        "",
        "## Category / family / recurrence",
        "- Deferred to task 05 (learned products, families, recurrence).",
        "",
        "## Review",
        "- No family links or uncertain category assignments require review at this task.",
        "",
        "_Source order IDs are intentionally omitted from this report (PRD FR-29)._",
    ]
    return "\n".join(lines)


def _render_apply_markdown(
    generated_at: str, checksum: str, stats: _ApplyStats, redaction_ids: set[str]
) -> str:
    warnings = list(stats.warnings)
    lines = [
        "# Grossy history synchronization report",
        "",
        f"- Generated at: {generated_at}",
        "- Phase: sync-apply",
        f"- Proposal checksum: {checksum}",
        "",
        "## Discovery",
        f"- Listed orders (observable window): {stats.listed_order_count}",
        f"- Unknown orders at discovery: {stats.unknown_order_count}",
        f"- Orders scanned this synchronization: {stats.orders_in_scan}",
        f"- Imported orders: {stats.imported_orders}",
        f"- Already present (idempotent skip): {stats.already_present_orders}",
        "",
        "## Import",
        f"- Product-presence records: {stats.imported_product_presence}",
        f"- New products: {stats.new_products}",
    ]
    for name, cat in stats.new_product_facts:
        safe_name = _sanitize_untrusted_text(name, redaction_ids)
        safe_cat = _sanitize_untrusted_text(cat, redaction_ids)
        suffix = f" ({safe_cat})" if safe_cat else ""
        lines.append(f"  - {safe_name}{suffix}")
    lines.append(f"- Changed products: {stats.changed_products}")
    for name, cat, reason in stats.changed_product_facts:
        safe_name = _sanitize_untrusted_text(name, redaction_ids)
        safe_cat = _sanitize_untrusted_text(cat, redaction_ids)
        suffix = f" ({safe_cat})" if safe_cat else ""
        lines.append(f"  - {safe_name}{suffix}: {reason}")
    lines += [
        "",
        "## Diagnostics",
        f"- Displayed-count discrepancies: {stats.displayed_count_discrepancies}",
    ]
    if warnings:
        lines.append("- Warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")
    else:
        lines.append("- Warnings: none")
    lines += [
        "",
        "## Category / family / recurrence",
        "- Deferred to task 05 (learned products, families, recurrence).",
        "",
        "## Review",
        "- No family links or uncertain category assignments require review at this task.",
        "",
        "_Source order IDs are intentionally omitted from this report (PRD FR-29)._",
    ]
    return "\n".join(lines)
