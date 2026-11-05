"""Tests for reservation-history synchronization (PRD FR-7..FR-31, sections 10.2, 15, 18).

Covers the closed proposal schemas, the deterministic sync phases
(discover-prepare, sync-dry-run, scan-prepare, scan-validate, sync-apply),
atomicity, idempotency, fail-closed validation, and sanitized reports that
omit source order IDs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import grossy.history.sync as sync_mod
from grossy.cli import app
from grossy.db.connection import open_connection
from grossy.history import (
    HistoryProposalError,
    HistorySyncError,
    OrderListEntry,
    OrderListError,
    RawProductCard,
    ScanStabilizationError,
    filter_dated_orders,
    run_discover_prepare,
    run_scan_prepare,
    run_scan_validate,
    run_sync_apply,
    run_sync_dry_run,
    stabilize_scan,
    validate_listed_orders,
    validate_order_scan,
)
from grossy.history.proposal import LISTED_ORDERS_SCHEMA_NAME
from grossy.init import do_init
from grossy.paths import GrossyPaths

RUNNER = CliRunner()

GENERATED_AT = "2026-09-26T12:00:00+00:00"
# The retired predecessor schema name: v2 replaced it in place with no reader.
RETIRED_LISTED_ORDERS_SCHEMA_NAME = LISTED_ORDERS_SCHEMA_NAME.replace(".v2", ".v1")


# --- proposal builders ------------------------------------------------------


def _label(iso_date: str) -> str:
    year, month, day = iso_date.split("-")
    return f"{day}/{month}/{year}"


def _nav_url(source_order_id: str) -> str:
    return f"https://www.ab.gr/my-account/order?id={source_order_id}"


def _product_url(product_id: str) -> str:
    return f"https://www.ab.gr/en/eshop/Food/item-{product_id}/p/{product_id}"


def _listed_order(source_order_id: str, order_date: str) -> dict[str, Any]:
    return {
        "source_order_id": source_order_id,
        "order_date": order_date,
        "navigation_url": _nav_url(source_order_id),
    }


def _scanned_product(
    product_id: str, *, title: str | None = None, brand: str | None = None
) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "canonical_url": _product_url(product_id),
        "title": title or f"Product {product_id}",
        "brand": brand,
        "category_heading": "Dairy",
    }


def _scanned_order(
    source_order_id: str,
    order_date: str,
    product_ids: list[str],
    *,
    displayed_count: int | None = None,
) -> dict[str, Any]:
    return {
        "source_order_id": source_order_id,
        "displayed_label": _label(order_date),
        "order_date": order_date,
        "navigation_url": _nav_url(source_order_id),
        "displayed_product_count": displayed_count,
        "products": [_scanned_product(pid) for pid in product_ids],
    }


def _listed_orders_payload(orders: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_name": "grossy.history.listed-orders.v2",
        "schema_version": 2,
        "generated_at": GENERATED_AT,
        "orders": orders,
    }


def _scan_payload(orders: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_name": "grossy.history.order-scan.v1",
        "schema_version": 1,
        "generated_at": GENERATED_AT,
        "orders": orders,
    }


def _window_listed() -> dict[str, Any]:
    """Six listed orders: 2026-09-20 .. 2026-09-25, IDs 1001 .. 1006."""
    orders = []
    for i in range(6):
        source_id = str(1001 + i)
        date = f"2026-09-{20 + i:02d}"
        orders.append(_listed_order(source_id, date))
    return _listed_orders_payload(orders)


# --- fixtures ---------------------------------------------------------------


def _paths_at(config_root: Path, data_root: Path, state_root: Path) -> GrossyPaths:
    return GrossyPaths(
        config_dir=config_root / "grossy",
        data_dir=data_root / "grossy",
        state_dir=state_root / "grossy",
    )


@pytest.fixture
def grossy_home(tmp_path: Path) -> GrossyPaths:
    return _paths_at(
        tmp_path / ".config",
        tmp_path / ".local" / "share",
        tmp_path / ".local" / "state",
    )


@pytest.fixture
def initialized_home(grossy_home: GrossyPaths) -> GrossyPaths:
    do_init(grossy_home)
    return grossy_home


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GrossyPaths:
    """Point the CLI at a fresh, initialized home via XDG environment."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    paths = _paths_at(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    do_init(paths)
    return paths


def _write(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _imported_ids(db: Path) -> set[str]:
    conn = open_connection(db)
    try:
        rows = conn.execute("SELECT source_order_id FROM source_orders").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _seed_imported(db: Path, source_order_ids: list[str]) -> None:
    """Mark window orders as already imported, bypassing the scan pipeline."""
    conn = open_connection(db)
    try:
        for oid in source_order_ids:
            conn.execute(
                "INSERT INTO source_orders (retailer, source_order_id, displayed_label, "
                "order_date, import_timestamp, created_at) VALUES ('ab', ?, '20/09/2026', "
                "'2026-09-20', '2026-09-26T00:00:00.000000Z', '2026-09-26T00:00:00.000000Z')",
                (oid,),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()


WINDOW_IDS = [str(1001 + i) for i in range(6)]


def _product_row(db: Path, product_id: str) -> tuple[Any, ...] | None:
    conn = open_connection(db)
    try:
        return conn.execute(
            "SELECT name, brand, purchase_order_count, first_observed_date, "
            "last_observed_date, category_id FROM products "
            "WHERE retailer='ab' AND retailer_product_id=?",
            (product_id,),
        ).fetchone()
    finally:
        conn.close()


# --- proposal schema tests --------------------------------------------------


def _validate_query_order(family: str, url: str) -> Any:
    source_id = "000123456"  # Synthetic identity; leading zeros must survive.
    if family == "listed":
        payload = _window_listed()
        payload["orders"][0] = _listed_order(source_id, "2026-09-20")
        payload["orders"][0]["navigation_url"] = url
        return validate_listed_orders(payload).orders[0]
    order = _scanned_order(source_id, "2026-09-20", ["11"])
    order["navigation_url"] = url
    return validate_order_scan(_scan_payload([order])).orders[0]


class TestQueryBackedOrderIdentity:
    def test_cli_query_selection_import_and_idempotent_dry_run(
        self, cli_env: GrossyPaths, tmp_path: Path
    ) -> None:
        payload = _window_listed()
        scanned = []
        for listed in payload["orders"]:
            listed["source_order_id"] = "000" + listed["source_order_id"]
            listed["navigation_url"] = _nav_url(listed["source_order_id"])
            scanned.append(_scanned_order(listed["source_order_id"], listed["order_date"], ["11"]))
        listed_path = _write(tmp_path, "listed-query.json", payload)
        scan_path = _write(tmp_path, "scan-query.json", _scan_payload(scanned))
        dry_args = ["history", "sync-dry-run", "--listed-orders", str(listed_path)]
        dry = RUNNER.invoke(app, dry_args)
        assert dry.exit_code == 0
        assert json.loads(dry.stdout)["unknown_order_count"] == 6
        assert _imported_ids(cli_env.database_file) == set()
        applied = RUNNER.invoke(
            app, ["history", "sync-apply", "--listed-orders", str(listed_path), str(scan_path)]
        )
        assert applied.exit_code == 0
        assert json.loads(applied.stdout)["imported_orders"] == 6
        assert _imported_ids(cli_env.database_file) == {
            order["source_order_id"] for order in payload["orders"]
        }
        repeat = RUNNER.invoke(app, dry_args)
        assert repeat.exit_code == 0
        assert json.loads(repeat.stdout)["unknown_order_count"] == 0

    @pytest.mark.parametrize("family", ["listed", "scan"])
    def test_accepts_bound_query_without_normalizing_id(self, family: str) -> None:
        url = _nav_url("000123456")
        order = _validate_query_order(family, url)
        assert order.source_order_id == "000123456"
        assert order.navigation_url == url

    @pytest.mark.parametrize("family", ["listed", "scan"])
    def test_rejects_query_id_mismatch(self, family: str) -> None:
        with pytest.raises(HistoryProposalError, match="id must match source_order_id"):
            _validate_query_order(family, _nav_url("123456"))

    # --- both families: only the order-details route ------------------------

    @pytest.mark.parametrize("family", ["listed", "scan"])
    @pytest.mark.parametrize(
        "url",
        [
            # Retired routes: the quick-shop history overview and queryless paths.
            "https://www.ab.gr/customerhub/quick-shop/orders?id=000123456",
            "https://www.ab.gr/customerhub/quick-shop/orders",
            "https://www.ab.gr/orders/000123456",
            "https://www.ab.gr/en/eshop/order-history/detail/000123456",
            # Order-details route with a wrong or ambiguous selection.
            "https://www.ab.gr/my-account/order",
            "https://www.ab.gr/my-account/order?",
            "https://www.ab.gr/my-account/order?id=LAST_ORDERS_PRODUCTS",
            "https://www.ab.gr/my-account/order?id=",
            "https://www.ab.gr/my-account/order?id=000123456&id=000123456",
            "https://www.ab.gr/my-account/order?id=000123456&extra=1",
            "https://www.ab.gr/my-account/order?ID=000123456",
            "https://www.ab.gr/my-account/order?id=%3000123456",
            "https://www.ab.gr/my-account/order?%69d=000123456",
            "https://www.ab.gr/my-account/order?id=000123456#",
            "https://www.ab.gr/my-account/order?id=000123456#section",
            "https://www.ab.gr/my-account/order/?id=000123456",
            "https://www.ab.gr/en/my-account/order?id=000123456",
            "https://www.ab.gr/my-account/../my-account/order?id=000123456",
            "https://www.ab.gr/my-account/%6frder?id=000123456",
            "https://www.ab.gr/my-account/order?id=000123456<script>",
            "https://www.ab.gr/other?id=000123456",
            "http://www.ab.gr/my-account/order?id=000123456",
            "https://evil.example/my-account/order?id=000123456",
        ],
    )
    def test_rejects_retired_or_ambiguous_route(self, family: str, url: str) -> None:
        with pytest.raises(HistoryProposalError):
            _validate_query_order(family, url)


class TestListedOrdersSchema:
    @pytest.mark.parametrize("count", [0, 1, 6, 60], ids=["zero", "one", "six", "many"])
    def test_accepts_any_order_count(self, count: int) -> None:
        """v2 has no fixed window: zero, one and many listed orders validate."""
        orders = [
            _listed_order(f"{1000 + i:09d}", f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}")
            for i in range(count)
        ]
        proposal = validate_listed_orders(_listed_orders_payload(orders))
        assert proposal.schema_name == "grossy.history.listed-orders.v2"
        assert proposal.schema_version == 2
        assert len(proposal.orders) == count

    def test_preserves_leading_zero_ids_as_strings(self) -> None:
        payload = _listed_orders_payload(
            [_listed_order("000123456", "2026-11-05"), _listed_order("123456", "2026-11-05")]
        )
        proposal = validate_listed_orders(payload)
        assert [o.source_order_id for o in proposal.orders] == ["000123456", "123456"]
        assert proposal.orders[0].navigation_url == _nav_url("000123456")

    def test_accepts_iso_date_with_day_at_most_twelve(self) -> None:
        """A list date 05/11/2026 arrives as ISO 2026-11-05 and is kept verbatim."""
        proposal = validate_listed_orders(
            _listed_orders_payload([_listed_order("000000001", "2026-11-05")])
        )
        assert proposal.orders[0].order_date == "2026-11-05"

    @pytest.mark.parametrize(
        "mutation,match",
        [
            pytest.param(
                lambda p: p["orders"].__setitem__(-1, dict(p["orders"][0])),
                "duplicate source order ID",
                id="duplicate-ids",
            ),
            pytest.param(
                lambda p: p["orders"][0].update(displayed_label="20/09/2026"),
                None,
                id="v1-displayed-label-field",
            ),
            pytest.param(
                lambda p: p["orders"][0].update(order_date="20/09/2026"),
                "ISO YYYY-MM-DD",
                id="ddmmyyyy-date",
            ),
            pytest.param(
                lambda p: p["orders"][0].update(order_date="20260920"),
                "ISO YYYY-MM-DD",
                id="non-iso-date",
            ),
            pytest.param(
                lambda p: p["orders"][0].update(order_date="2026-13-01"),
                "not a valid calendar date",
                id="invalid-calendar-date",
            ),
            pytest.param(
                lambda p: p["orders"][0].update(source_order_id="1001x"),
                None,
                id="non-numeric-id",
            ),
            pytest.param(
                lambda p: p["orders"][0].update(
                    navigation_url="https://www.ab.gr/customerhub/quick-shop/orders?id=1001"
                ),
                "order-details route",
                id="retired-quick-shop-route",
            ),
            pytest.param(
                lambda p: p["orders"][0].update(navigation_url="https://evil.example/orders/1001"),
                "canonical",
                id="noncanonical-url",
            ),
            pytest.param(
                lambda p: p["orders"][0].update(screenshot="https://www.ab.gr/x.png"),
                None,
                id="unknown-field",
            ),
            pytest.param(
                lambda p: p.update(schema_name=RETIRED_LISTED_ORDERS_SCHEMA_NAME),
                "schema_name",
                id="v1-schema-name",
            ),
            pytest.param(
                lambda p: p.update(schema_version=1),
                "schema_version",
                id="v1-schema-version",
            ),
            pytest.param(
                lambda p: p["orders"][0].update(
                    navigation_url="https://www.ab.gr/my-account/order?id=1001<script>"
                ),
                "prohibited content",
                id="prohibited-content",
            ),
            # Encoded/mixed dot segments, separators and controls would change
            # the route; none may pass as the order-details route.
            *[
                pytest.param(
                    lambda p, url=url: p["orders"][0].update(navigation_url=url),
                    "order-details route",
                    id=case,
                )
                for case, url in [
                    ("encoded-dotdot", "https://www.ab.gr/my-account/%2e%2e/order?id=1001"),
                    ("mixed-dot", "https://www.ab.gr/my-account/.%2e/order?id=1001"),
                    ("nested-encoding", "https://www.ab.gr/%252e%252e/my-account/order?id=1001"),
                    ("encoded-separator", "https://www.ab.gr/my-account%2forder?id=1001"),
                    ("encoded-control", "https://www.ab.gr/my-account/order%00?id=1001"),
                ]
            ],
        ],
    )
    def test_rejects_invalid_listed_orders(self, mutation: Any, match: str | None) -> None:
        payload = _window_listed()
        mutation(payload)
        with pytest.raises(HistoryProposalError, match=match):
            validate_listed_orders(payload)


class TestOrderScanSchema:
    def test_valid_scan_accepted_with_count_mismatch_diagnostic(self) -> None:
        payload = _scan_payload(
            [
                _scanned_order("1001", "2026-09-20", ["11", "12"], displayed_count=99),
                _scanned_order("1002", "2026-09-21", ["13"]),
            ]
        )
        proposal = validate_order_scan(payload)
        assert len(proposal.orders) == 2
        assert proposal.orders[0].displayed_product_count == 99

    def test_accepts_positive_package_facts(self) -> None:
        order = _scanned_order("1001", "2026-09-20", ["11"])
        order["products"][0]["package_amount"] = 2.5
        order["products"][0]["package_count"] = 3
        proposal = validate_order_scan(_scan_payload([order]))
        assert proposal.orders[0].products[0].package_amount == 2.5
        assert proposal.orders[0].products[0].package_count == 3

    def test_rejects_zero_orders(self) -> None:
        with pytest.raises(HistoryProposalError, match="at least one order"):
            validate_order_scan(_scan_payload([]))

    @pytest.mark.parametrize(
        "mutation,match",
        [
            pytest.param(lambda o: o.update(products=[]), "no product cards", id="no-products"),
            pytest.param(
                lambda o: o["products"].append(_scanned_product("11")),
                "duplicate product ID",
                id="duplicate-product",
            ),
            pytest.param(
                lambda o: o["products"][0].update(canonical_url=_product_url("999")),
                "disagrees with the ID carried",
                id="product-url-id-mismatch",
            ),
            # P1: the scan proposal cannot be directed off the listed order route.
            pytest.param(
                lambda o: o.update(navigation_url="https://www.ab.gr/my-account/%6frder?id=1001"),
                "order-details route",
                id="encoded-traversal-url",
            ),
            pytest.param(
                lambda o: o["products"][0].update(
                    product_id="abc", canonical_url="https://www.ab.gr/en/eshop/Food/x/p/abc"
                ),
                None,
                id="non-numeric-product-id",
            ),
            # P2-2: package facts must be positive and strictly typed.
            pytest.param(
                lambda o: o["products"][0].update(package_amount=-1.5),
                "package_amount must be positive",
                id="negative-package-amount",
            ),
            pytest.param(
                lambda o: o["products"][0].update(package_amount=0.0),
                "package_amount must be positive",
                id="zero-package-amount",
            ),
            pytest.param(
                lambda o: o["products"][0].update(package_amount="2.5"),
                None,
                id="string-package-amount",
            ),
            pytest.param(
                lambda o: o["products"][0].update(package_count=-2),
                "package_count must be positive",
                id="negative-package-count",
            ),
            pytest.param(
                lambda o: o["products"][0].update(package_count=0),
                "package_count must be positive",
                id="zero-package-count",
            ),
        ],
    )
    def test_rejects_invalid_scanned_order(self, mutation: Any, match: str | None) -> None:
        order = _scanned_order("1001", "2026-09-20", ["11", "12"])
        mutation(order)
        with pytest.raises(HistoryProposalError, match=match):
            validate_order_scan(_scan_payload([order]))


# --- discover-prepare -------------------------------------------------------


class TestDiscoverPrepare:
    def test_reports_ready_state(self, initialized_home: GrossyPaths) -> None:
        result = run_discover_prepare(initialized_home)
        assert result.database_exists is True
        assert result.imported_order_count == 0
        assert result.schema_version >= 1
        payload = result.to_dict()
        assert payload["status"] == "ready"
        assert "required_order_window" not in payload
        assert payload["listed_orders_schema"] == "grossy.history.listed-orders.v2"
        assert RETIRED_LISTED_ORDERS_SCHEMA_NAME not in json.dumps(payload)

    def test_reports_imported_count(self, initialized_home: GrossyPaths) -> None:
        conn = open_connection(initialized_home.database_file)
        conn.execute(
            "INSERT INTO source_orders (retailer, source_order_id, displayed_label, "
            "order_date, import_timestamp, created_at) VALUES ('ab', '555', "
            "'25/09/2026', '2026-09-25', '2026-09-26T00:00:00.000000Z', "
            "'2026-09-26T00:00:00.000000Z')"
        )
        conn.execute("COMMIT")
        conn.close()
        assert run_discover_prepare(initialized_home).imported_order_count == 1

    def test_fails_without_database(self, grossy_home: GrossyPaths) -> None:
        with pytest.raises(HistorySyncError, match="run 'grossy init' first"):
            run_discover_prepare(grossy_home)


# --- sync-dry-run -----------------------------------------------------------


class TestSyncDryRun:
    def test_counts_unknown_orders(self, initialized_home: GrossyPaths, tmp_path: Path) -> None:
        # Mark two of the six window orders as already imported.
        _seed_imported(initialized_home.database_file, ["1001", "1002"])

        path = _write(tmp_path, "listed.json", _window_listed())
        result = run_sync_dry_run(path, initialized_home)
        assert result.listed_order_count == 6
        assert result.unknown_order_count == 4
        payload = result.to_dict()
        assert payload["orders_opened"] == 0
        assert payload["product_contents_inspected"] is False

    def test_zero_unknown_when_all_imported(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        _seed_imported(initialized_home.database_file, WINDOW_IDS)
        path = _write(tmp_path, "listed.json", _window_listed())
        assert run_sync_dry_run(path, initialized_home).unknown_order_count == 0

    def test_rejects_invalid_listed_proposal(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        payload = _window_listed()
        payload["orders"][-1]["navigation_url"] = "https://www.ab.gr/orders/1006"
        path = _write(tmp_path, "listed.json", payload)
        with pytest.raises(HistorySyncError, match="listed-orders proposal rejected"):
            run_sync_dry_run(path, initialized_home)

    def test_fails_without_database(self, grossy_home: GrossyPaths, tmp_path: Path) -> None:
        path = _write(tmp_path, "listed.json", _window_listed())
        with pytest.raises(HistorySyncError, match="run 'grossy init' first"):
            run_sync_dry_run(path, grossy_home)


# --- scan-prepare -----------------------------------------------------------


class TestScanPrepare:
    def test_exposes_unknown_orders_oldest_first(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """FR-9/FR-14: the phase exposes exactly the unknown orders, oldest first."""
        _seed_imported(initialized_home.database_file, ["1001", "1002"])

        # Supplied newest-first (as rendered); the result must be oldest-first.
        payload = _window_listed()
        payload["orders"] = list(reversed(payload["orders"]))
        path = _write(tmp_path, "listed.json", payload)
        result = run_scan_prepare(path, initialized_home)
        assert result.status == "ready"
        assert result.listed_order_count == 6
        assert result.unknown_order_count == 4
        assert [o["source_order_id"] for o in result.unknown_orders] == [
            "1003",
            "1004",
            "1005",
            "1006",
        ]
        first = result.unknown_orders[0]
        assert first == {
            "source_order_id": "1003",
            "order_date": "2026-09-22",
            "navigation_url": _nav_url("1003"),
        }
        assert result.report_path is None
        output = result.to_dict()
        assert output["orders_opened"] == 0
        assert output["product_contents_inspected"] is False
        assert output["unknown_orders"] == [dict(o) for o in result.unknown_orders]

    def test_zero_unknown_writes_redacted_noop_report(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """FR-28/FR-29: a zero-unknown normal sync leaves the sanitized summary."""
        _seed_imported(initialized_home.database_file, WINDOW_IDS)

        path = _write(tmp_path, "listed.json", _window_listed())
        result = run_scan_prepare(path, initialized_home)
        assert result.status == "no-op"
        assert result.unknown_orders == ()
        assert result.report_path is not None
        assert result.report_path.exists()

        report_text = result.report_path.read_text(encoding="utf-8")
        assert "Status: no-op" in report_text
        assert "Listed orders: 6" in report_text
        assert "Unknown orders at discovery: 0" in report_text
        # FR-29: no source order IDs in the human report.
        for i in range(6):
            assert str(1001 + i) not in report_text

        # Mirrored JSON report carries the counts and omits source order IDs.
        json_report = result.report_path.with_suffix(".json")
        assert json_report.exists()
        json_text = json_report.read_text(encoding="utf-8")
        assert '"status": "no-op"' in json_text
        assert '"listed_order_count": 6' in json_text
        assert '"unknown_order_count": 0' in json_text
        for i in range(6):
            assert str(1001 + i) not in json_text

    def test_zero_listed_orders_is_noop(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """An empty walk is a valid v2 proposal and a no-op report."""
        path = _write(tmp_path, "listed.json", _listed_orders_payload([]))
        result = run_scan_prepare(path, initialized_home)
        assert result.status == "no-op"
        assert result.listed_order_count == 0
        assert result.unknown_orders == ()
        assert result.report_path is not None
        assert "Listed orders: 0" in result.report_path.read_text(encoding="utf-8")

    def test_single_unknown_order_with_leading_zeros(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        payload = _listed_orders_payload([_listed_order("000123456", "2026-11-05")])
        path = _write(tmp_path, "listed.json", payload)
        result = run_scan_prepare(path, initialized_home)
        assert result.status == "ready"
        assert result.unknown_order_count == 1
        assert result.unknown_orders[0]["source_order_id"] == "000123456"
        assert result.unknown_orders[0]["order_date"] == "2026-11-05"

    def test_many_listed_orders_returns_only_current_walk_unknowns(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """Sixty listed orders with thirty imported expose exactly the thirty unknown."""
        ids = [f"{1 + i:09d}" for i in range(60)]
        _seed_imported(initialized_home.database_file, ids[:30])
        orders = [
            _listed_order(oid, f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}")
            for i, oid in enumerate(ids)
        ]
        path = _write(tmp_path, "listed.json", _listed_orders_payload(list(reversed(orders))))
        result = run_scan_prepare(path, initialized_home)
        assert result.status == "ready"
        assert result.listed_order_count == 60
        assert result.unknown_order_count == 30
        assert [o["source_order_id"] for o in result.unknown_orders] == ids[30:]

    def test_fails_without_database(self, grossy_home: GrossyPaths, tmp_path: Path) -> None:
        path = _write(tmp_path, "listed.json", _window_listed())
        with pytest.raises(HistorySyncError, match="run 'grossy init' first"):
            run_scan_prepare(path, grossy_home)

    def test_rejects_v1_listed_proposal(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        payload = _window_listed()
        payload.update(schema_name=RETIRED_LISTED_ORDERS_SCHEMA_NAME, schema_version=1)
        path = _write(tmp_path, "listed.json", payload)
        with pytest.raises(HistorySyncError, match="listed-orders proposal rejected"):
            run_scan_prepare(path, initialized_home)


# --- scan-validate ----------------------------------------------------------


class TestScanValidate:
    def test_accepts_valid_scan(self, tmp_path: Path) -> None:
        payload = _scan_payload(
            [
                _scanned_order("1001", "2026-09-20", ["11", "12"]),
                _scanned_order("1002", "2026-09-21", ["13"]),
            ]
        )
        path = _write(tmp_path, "scan.json", payload)
        result = run_scan_validate(path)
        assert result.order_count == 2
        assert result.product_card_count == 3
        assert len(result.checksum) == 64
        assert result.warnings == ()

    def test_reports_count_mismatch_warning(self, tmp_path: Path) -> None:
        payload = _scan_payload(
            [_scanned_order("1001", "2026-09-20", ["11", "12"], displayed_count=5)]
        )
        path = _write(tmp_path, "scan.json", payload)
        result = run_scan_validate(path)
        assert len(result.warnings) == 1
        assert "displayed count 5" in result.warnings[0]

    def test_rejects_invalid_scan(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "scan.json", _scan_payload([]))
        with pytest.raises(HistorySyncError, match="order-scan proposal rejected"):
            run_scan_validate(path)


# --- sync-apply -------------------------------------------------------------


class TestSyncApply:
    def test_fresh_db_accepts_all_six_oldest_first(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """Acceptance: a fresh database accepts all six dated orders oldest first."""
        # Scan all six window orders (all unknown on a fresh DB), supplied newest first.
        orders = []
        for i in reversed(range(6)):
            source_id = str(1001 + i)
            date = f"2026-09-{20 + i:02d}"
            orders.append(_scanned_order(source_id, date, [str(40 + i)]))
        path = _write(tmp_path, "scan.json", _scan_payload(orders))
        result = run_sync_apply(path, initialized_home)
        assert result.status == "applied"
        assert result.stats.imported_orders == 6
        assert result.stats.new_products == 6
        # Insertion order (rowid) must be oldest-first.
        conn = open_connection(initialized_home.database_file)
        rows = conn.execute("SELECT order_date FROM source_orders ORDER BY id").fetchall()
        conn.close()
        assert [row[0] for row in rows] == [f"2026-09-{20 + i:02d}" for i in range(6)]

    def test_apply_imports_out_of_order_scan(self, initialized_home: GrossyPaths, tmp_path: Path):
        payload = _scan_payload(
            [
                # Deliberately out of order to exercise arbitrary proposal ordering.
                _scanned_order("1003", "2026-09-22", ["31", "32"]),
                _scanned_order("1001", "2026-09-20", ["11", "12"]),
                _scanned_order("1002", "2026-09-21", ["21"]),
            ]
        )
        path = _write(tmp_path, "scan.json", payload)
        result = run_sync_apply(path, initialized_home)
        assert result.status == "applied"
        assert result.stats.imported_orders == 3
        assert result.stats.new_products == 5
        assert _imported_ids(initialized_home.database_file) == {"1001", "1002", "1003"}

    def test_is_idempotent_on_reapply(self, initialized_home: GrossyPaths, tmp_path: Path):
        payload = _scan_payload([_scanned_order("1001", "2026-09-20", ["11", "12"])])
        path = _write(tmp_path, "scan.json", payload)
        first = run_sync_apply(path, initialized_home)
        assert first.status == "applied"
        assert first.stats.new_products == 2

        second = run_sync_apply(path, initialized_home)
        assert second.status == "no-op"
        assert second.stats.already_present_orders == 1
        assert second.stats.imported_orders == 0
        # No duplicate source orders or product presence rows.
        conn = open_connection(initialized_home.database_file)
        presence = conn.execute("SELECT COUNT(*) FROM source_order_products").fetchone()[0]
        orders = conn.execute("SELECT COUNT(*) FROM source_orders").fetchone()[0]
        conn.close()
        assert orders == 1
        assert presence == 2

    def test_updates_purchase_order_count_across_orders(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Product 71 appears in two distinct orders within one scan.
        payload = _scan_payload(
            [
                _scanned_order("1001", "2026-09-20", ["71"]),
                _scanned_order("1002", "2026-09-21", ["71", "72"]),
            ]
        )
        path = _write(tmp_path, "scan.json", payload)
        run_sync_apply(path, initialized_home)
        row = _product_row(initialized_home.database_file, "71")
        assert row is not None
        assert row[2] == 2  # purchase_order_count

    def test_new_vs_changed_product_counts(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        # Seed a pre-existing product with an older observation window.
        conn = open_connection(initialized_home.database_file)
        conn.execute(
            "INSERT INTO products (retailer, retailer_product_id, canonical_url, name, "
            "first_observed_date, last_observed_date, purchase_order_count, created_at, "
            "updated_at) VALUES ('ab', '55', "
            "'https://www.ab.gr/en/eshop/Food/item-55/p/55', 'Existing', "
            "'2026-09-01', '2026-09-05', 1, '2026-09-01T00:00:00.000000Z', "
            "'2026-09-05T00:00:00.000000Z')"
        )
        conn.execute("COMMIT")
        conn.close()

        payload = _scan_payload(
            [
                _scanned_order(
                    "1001",
                    "2026-09-20",
                    ["55", "88"],
                )
            ]
        )
        path = _write(tmp_path, "scan.json", payload)
        result = run_sync_apply(path, initialized_home)
        assert result.stats.new_products == 1  # product 88
        assert result.stats.changed_products == 1  # product 55 (last_observed extended)

    def test_rolls_back_entire_batch_on_failure(
        self, initialized_home: GrossyPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = _scan_payload(
            [
                _scanned_order("1001", "2026-09-20", ["11"]),
                _scanned_order("1002", "2026-09-21", ["22"]),
            ]
        )
        path = _write(tmp_path, "scan.json", payload)
        real_upsert = sync_mod._upsert_product

        def flaky(conn, product, order_date, key, pre_existing, now):
            if product.product_id == "22":
                raise sqlite3.IntegrityError("simulated failure mid-apply")
            return real_upsert(conn, product, order_date, key, pre_existing, now)

        monkeypatch.setattr(sync_mod, "_upsert_product", flaky)
        with pytest.raises(sqlite3.IntegrityError):
            run_sync_apply(path, initialized_home)
        # Nothing from either order persisted.
        assert _imported_ids(initialized_home.database_file) == set()
        assert _product_row(initialized_home.database_file, "11") is None
        assert _product_row(initialized_home.database_file, "22") is None

    def test_writes_sanitized_report_omitting_order_ids(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        payload = _scan_payload(
            [
                _scanned_order("1001", "2026-09-20", ["11", "12"], displayed_count=9),
                _scanned_order("1002", "2026-09-21", ["13"]),
            ]
        )
        path = _write(tmp_path, "scan.json", payload)
        result = run_sync_apply(path, initialized_home)
        assert result.report_path is not None
        assert result.report_path.exists()
        report_text = result.report_path.read_text(encoding="utf-8")
        # FR-29: source order IDs must never appear in the human report.
        assert "1001" not in report_text
        assert "1002" not in report_text
        # The count-mismatch diagnostic is present (sanitized, no ID).
        assert "displayed count 9" in report_text
        # A mirrored JSON report also omits source order IDs.
        json_report = result.report_path.with_suffix(".json")
        assert json_report.exists()
        json_text = json_report.read_text(encoding="utf-8")
        assert "1001" not in json_text
        assert "1002" not in json_text

    def test_rejects_invalid_scan(self, initialized_home: GrossyPaths, tmp_path: Path) -> None:
        path = _write(tmp_path, "scan.json", _scan_payload([]))
        with pytest.raises(HistorySyncError, match="order-scan proposal rejected"):
            run_sync_apply(path, initialized_home)

    # --- P1: discovery binding tests (FR-10.3.6, FR-23, FR-24) -------------

    def test_apply_with_listed_orders_accepts_matching_unknown(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """Apply succeeds when the scan covers exactly the unknown orders."""
        _seed_imported(initialized_home.database_file, ["1001", "1002"])

        # Scan exactly the 4 unknown orders (1003..1006).
        orders = []
        for i in range(2, 6):
            source_id = str(1001 + i)
            date = f"2026-09-{20 + i:02d}"
            orders.append(_scanned_order(source_id, date, [str(100 + i)]))
        scan_path = _write(tmp_path, "scan.json", _scan_payload(orders))
        listed_path = _write(tmp_path, "listed.json", _window_listed())

        result = run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert result.status == "applied"
        assert result.stats.imported_orders == 4
        assert result.stats.listed_order_count == 6
        assert result.stats.unknown_order_count == 4

    def test_apply_with_listed_orders_rejects_partial_scan(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """A scan missing some unknown orders is rejected (FR-26)."""
        # Scan only 2 of the 6 unknown orders on a fresh DB.
        orders = [
            _scanned_order("1001", "2026-09-20", ["11"]),
            _scanned_order("1002", "2026-09-21", ["12"]),
        ]
        scan_path = _write(tmp_path, "scan.json", _scan_payload(orders))
        listed_path = _write(tmp_path, "listed.json", _window_listed())

        with pytest.raises(HistorySyncError, match="missing 4 expected unknown order"):
            run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert _imported_ids(initialized_home.database_file) == set()

    def test_apply_with_listed_orders_rejects_unlisted_order(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """A scan containing an order not in the listed discovery is rejected (FR-10)."""
        orders = [
            _scanned_order("1001", "2026-09-20", ["11"]),
            _scanned_order("1002", "2026-09-21", ["12"]),
            _scanned_order("1003", "2026-09-22", ["13"]),
            _scanned_order("1004", "2026-09-23", ["14"]),
            _scanned_order("1005", "2026-09-24", ["15"]),
            _scanned_order("1006", "2026-09-25", ["16"]),
            # This ID is not in the listed discovery.
            _scanned_order("9999", "2026-09-10", ["99"]),
        ]
        scan_path = _write(tmp_path, "scan.json", _scan_payload(orders))
        listed_path = _write(tmp_path, "listed.json", _window_listed())

        with pytest.raises(HistorySyncError, match="not present in the listed-orders discovery"):
            run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert _imported_ids(initialized_home.database_file) == set()

    def test_apply_with_listed_orders_rejects_already_imported_order(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """P1 regression: a scan of known (already-imported) orders is rejected.

        The scan must cover exactly the unknown orders; scanning all six
        listed orders when two are already imported must fail closed and
        import nothing (FR-23, FR-26).
        """
        _seed_imported(initialized_home.database_file, ["1001", "1002"])

        # Scan all six listed orders, including the two already imported.
        orders = []
        for i in range(6):
            source_id = str(1001 + i)
            date = f"2026-09-{20 + i:02d}"
            orders.append(_scanned_order(source_id, date, [str(50 + i)]))
        scan_path = _write(tmp_path, "scan.json", _scan_payload(orders))
        listed_path = _write(tmp_path, "listed.json", _window_listed())

        with pytest.raises(HistorySyncError, match="already-imported order"):
            run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        # Only the pre-seeded rows exist; the scan imported nothing.
        assert _imported_ids(initialized_home.database_file) == {"1001", "1002"}

    def test_apply_with_listed_orders_rejects_mismatched_date(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """A scan with a date that doesn't match the listed order is rejected."""
        # Order 1001 is listed with date 2026-09-20, but scan says 2026-09-25.
        orders = [
            _scanned_order("1001", "2026-09-25", ["11"]),  # wrong date
            _scanned_order("1002", "2026-09-21", ["12"]),
            _scanned_order("1003", "2026-09-22", ["13"]),
            _scanned_order("1004", "2026-09-23", ["14"]),
            _scanned_order("1005", "2026-09-24", ["15"]),
            _scanned_order("1006", "2026-09-25", ["16"]),
        ]
        scan_path = _write(tmp_path, "scan.json", _scan_payload(orders))
        listed_path = _write(tmp_path, "listed.json", _window_listed())

        with pytest.raises(HistorySyncError, match="does not match listed-orders date"):
            run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert _imported_ids(initialized_home.database_file) == set()

    def test_apply_with_listed_orders_rejects_mismatched_url(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """A scan whose navigation URL is off the order-details route fails closed.

        Both proposals accept only ``/my-account/order?id=<source_order_id>``,
        so a URL mismatch is caught at scan validation before the sync-apply
        URL-equality binding runs; nothing is imported either way.
        """
        order = _scanned_order("1001", "2026-09-20", ["11"])
        order["navigation_url"] = "https://www.ab.gr/orders/1001"  # retired queryless route
        orders = [
            order,
            _scanned_order("1002", "2026-09-21", ["12"]),
            _scanned_order("1003", "2026-09-22", ["13"]),
            _scanned_order("1004", "2026-09-23", ["14"]),
            _scanned_order("1005", "2026-09-24", ["15"]),
            _scanned_order("1006", "2026-09-25", ["16"]),
        ]
        scan_path = _write(tmp_path, "scan.json", _scan_payload(orders))
        listed_path = _write(tmp_path, "listed.json", _window_listed())

        with pytest.raises(HistorySyncError, match="order-scan proposal rejected.*order-details"):
            run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert _imported_ids(initialized_home.database_file) == set()

    def test_apply_single_order_stores_day_first_list_date(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """A list date 05/11/2026 (day <= 12) is stored as ISO 2026-11-05.

        The listed order carries only the ISO date; the scan's DD/MM/YYYY label
        must agree with it, so a month-first mix-up cannot be imported.
        """
        listed_path = _write(
            tmp_path,
            "listed.json",
            _listed_orders_payload([_listed_order("000123456", "2026-11-05")]),
        )
        scan = _scanned_order("000123456", "2026-11-05", ["11"])
        assert scan["displayed_label"] == "05/11/2026"
        scan_path = _write(tmp_path, "scan.json", _scan_payload([scan]))

        result = run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert result.status == "applied"
        assert result.stats.imported_orders == 1
        assert result.stats.listed_order_count == 1
        assert result.stats.unknown_order_count == 1
        conn = open_connection(initialized_home.database_file)
        try:
            row = conn.execute(
                "SELECT source_order_id, displayed_label, order_date FROM source_orders"
            ).fetchone()
        finally:
            conn.close()
        assert tuple(row) == ("000123456", "05/11/2026", "2026-11-05")

    def test_apply_rejects_month_first_date_mix_up(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """A scan dated 2026-05-11 cannot bind to a list entry dated 2026-11-05."""
        listed_path = _write(
            tmp_path,
            "listed.json",
            _listed_orders_payload([_listed_order("000123456", "2026-11-05")]),
        )
        scan_path = _write(
            tmp_path,
            "scan.json",
            _scan_payload([_scanned_order("000123456", "2026-05-11", ["11"])]),
        )
        with pytest.raises(HistorySyncError, match="does not match listed-orders date"):
            run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert _imported_ids(initialized_home.database_file) == set()

    def test_apply_with_zero_listed_orders_rejects_any_scan(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        listed_path = _write(tmp_path, "listed.json", _listed_orders_payload([]))
        scan_path = _write(
            tmp_path, "scan.json", _scan_payload([_scanned_order("1001", "2026-09-20", ["11"])])
        )
        with pytest.raises(HistorySyncError, match="not present in the listed-orders discovery"):
            run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert _imported_ids(initialized_home.database_file) == set()

    # --- P2-3: report includes listed/unknown counts (FR-28) ----------------

    def test_report_includes_listed_and_unknown_counts(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """FR-28: the apply report includes listed and unknown order counts."""
        _seed_imported(initialized_home.database_file, ["1001", "1002"])

        orders = []
        for i in range(2, 6):
            source_id = str(1001 + i)
            date = f"2026-09-{20 + i:02d}"
            orders.append(_scanned_order(source_id, date, [str(100 + i)]))
        scan_path = _write(tmp_path, "scan.json", _scan_payload(orders))
        listed_path = _write(tmp_path, "listed.json", _window_listed())

        result = run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert result.report_path is not None
        report_text = result.report_path.read_text(encoding="utf-8")
        # FR-28: listed and unknown counts are present.
        assert "Listed orders: 6" in report_text
        assert "Unknown orders at discovery: 4" in report_text
        # FR-29: source order IDs still omitted.
        assert "1001" not in report_text
        assert "1003" not in report_text

        # JSON report also carries the counts.
        json_report = result.report_path.with_suffix(".json")
        json_text = json_report.read_text(encoding="utf-8")
        assert '"listed_order_count": 6' in json_text
        assert '"unknown_order_count": 4' in json_text

    # --- P2: post-commit report-write failure recovery (PRD 18) ------------

    def test_report_write_failure_after_commit_is_recoverable(
        self, initialized_home: GrossyPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PRD 18: a post-commit report-write failure retains the SQLite truth,
        marks report recovery required, and allows deterministic regeneration.
        """
        payload = _scan_payload(
            [
                _scanned_order("1001", "2026-09-20", ["11", "12"]),
                _scanned_order("1002", "2026-09-21", ["13"]),
            ]
        )
        path = _write(tmp_path, "scan.json", payload)

        real_write = sync_mod.atomic_write_text
        state = {"fail_next": True}

        def flaky_write(p, text):
            if state["fail_next"]:
                state["fail_next"] = False
                raise OSError("simulated report-write failure after commit")
            return real_write(p, text)

        monkeypatch.setattr(sync_mod, "atomic_write_text", flaky_write)

        # The import commits; the first report file write then fails (I/O).
        with pytest.raises(OSError, match="simulated report-write failure"):
            run_sync_apply(path, initialized_home)

        # SQLite truth retained: both orders and every product are imported.
        assert _imported_ids(initialized_home.database_file) == {"1001", "1002"}
        assert _product_row(initialized_home.database_file, "11") is not None
        assert _product_row(initialized_home.database_file, "13") is not None

        # Report recovery required: facts persisted with report_written = 0 and
        # the report files are absent.
        conn = open_connection(initialized_home.database_file)
        row = conn.execute(
            "SELECT report_written, report_md_path, report_json_path, payload_json "
            "FROM history_sync_reports"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 0
        md_path_str, json_path_str, persisted_json = row[1], row[2], row[3]
        assert not Path(md_path_str).exists()
        assert not Path(json_path_str).exists()

        # Deterministic regeneration: discover-prepare (always the first phase)
        # re-writes the files byte-for-byte from the persisted facts and marks
        # them written, reconstructing the successful import summary.
        run_discover_prepare(initialized_home)

        conn = open_connection(initialized_home.database_file)
        written = conn.execute("SELECT report_written FROM history_sync_reports").fetchone()[0]
        conn.close()
        assert written == 1

        md_path = Path(md_path_str)
        json_path = Path(json_path_str)
        assert md_path.exists()
        assert json_path.exists()
        # Byte-for-byte deterministic: the regenerated JSON equals the facts.
        assert json_path.read_text(encoding="utf-8") == persisted_json
        # The regenerated report is the apply summary, not a zero-unknown no-op.
        md_text = md_path.read_text(encoding="utf-8")
        assert "Phase: sync-apply" in md_text
        assert "Imported orders: 2" in md_text
        # FR-29 still holds in the regenerated report.
        assert "1001" not in md_text
        assert "1002" not in md_text

    # --- P2: FR-29 redaction of untrusted report text -----------------------

    def test_malicious_multiline_title_order_id_redacted(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """FR-29: a multiline product title embedding a source order ID is
        whitespace-collapsed and the ID redacted from both report formats.
        """
        malicious_title = "Milk\n- Source order ID: 1001\ninjected line"
        order = _scanned_order("1001", "2026-09-20", ["11"])
        order["products"][0]["title"] = malicious_title
        payload = _scan_payload(
            [
                order,
                _scanned_order("1002", "2026-09-21", ["12"]),
            ]
        )
        path = _write(tmp_path, "scan.json", payload)
        result = run_sync_apply(path, initialized_home)
        assert result.status == "applied"
        assert result.report_path is not None

        md_text = result.report_path.read_text(encoding="utf-8")
        # The newline injection is collapsed and the embedded ID redacted.
        assert "Source order ID: 1001" not in md_text
        assert "Milk - Source order ID: [redacted] injected line" in md_text
        # No standalone injected markdown line survives.
        assert "\n- Source order ID: 1001\n" not in md_text

        # The mirrored JSON report is redacted identically.
        json_text = result.report_path.with_suffix(".json").read_text(encoding="utf-8")
        assert "Source order ID: 1001" not in json_text
        assert "Milk - Source order ID: [redacted] injected line" in json_text
        assert "[redacted]" in json_text


# --- CLI integration --------------------------------------------------------


class TestCli:
    def test_discover_prepare(self, cli_env: GrossyPaths) -> None:
        result = RUNNER.invoke(app, ["history", "discover-prepare"])
        assert result.exit_code == 0
        data: dict[str, Any] = json.loads(result.stdout)
        assert data["status"] == "ready"
        assert data["imported_order_count"] == 0

    def test_sync_dry_run(self, cli_env: GrossyPaths, tmp_path: Path) -> None:
        listed = _write(tmp_path, "listed.json", _window_listed())
        result = RUNNER.invoke(app, ["history", "sync-dry-run", "--listed-orders", str(listed)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["listed_order_count"] == 6
        assert data["unknown_order_count"] == 6
        assert data["orders_opened"] == 0

    def test_scan_prepare(self, cli_env: GrossyPaths, tmp_path: Path) -> None:
        listed = _write(tmp_path, "listed.json", _window_listed())
        result = RUNNER.invoke(app, ["history", "scan-prepare", "--listed-orders", str(listed)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["schema_name"] == "grossy.history.scan-prepare"
        assert data["status"] == "ready"
        assert data["listed_order_count"] == 6
        assert data["unknown_order_count"] == 6
        assert [o["source_order_id"] for o in data["unknown_orders"]] == [
            str(1001 + i) for i in range(6)
        ]
        assert data["orders_opened"] == 0
        assert data["product_contents_inspected"] is False
        assert "report_path" not in data

    def test_scan_prepare_zero_unknown_writes_report(
        self, cli_env: GrossyPaths, tmp_path: Path
    ) -> None:
        """FR-28/FR-29: a public zero-unknown retry leaves the sanitized summary."""
        _seed_imported(cli_env.database_file, WINDOW_IDS)
        listed = _write(tmp_path, "listed.json", _window_listed())
        result = RUNNER.invoke(app, ["history", "scan-prepare", "--listed-orders", str(listed)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "no-op"
        assert data["unknown_order_count"] == 0
        report = Path(data["report_path"])
        assert report.exists()
        text = report.read_text(encoding="utf-8")
        assert "Unknown orders at discovery: 0" in text
        for i in range(6):
            assert str(1001 + i) not in text

    def test_scan_prepare_invalid_fails_closed(self, cli_env: GrossyPaths, tmp_path: Path) -> None:
        payload = _window_listed()
        payload["orders"][-1]["navigation_url"] = "https://www.ab.gr/orders/1006"
        listed = _write(tmp_path, "listed.json", payload)
        result = RUNNER.invoke(app, ["history", "scan-prepare", "--listed-orders", str(listed)])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["status"] == "error"
        assert "listed-orders proposal rejected" in data["detail"]

    def test_scan_validate(self, cli_env: GrossyPaths, tmp_path: Path) -> None:
        scan = _write(
            tmp_path, "scan.json", _scan_payload([_scanned_order("1001", "2026-09-20", ["11"])])
        )
        result = RUNNER.invoke(app, ["history", "scan-validate", str(scan)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "accepted"
        assert data["order_count"] == 1

    def test_sync_apply_end_to_end(self, cli_env: GrossyPaths, tmp_path: Path) -> None:
        listed = _write(tmp_path, "listed.json", _window_listed())
        scan = _write(
            tmp_path,
            "scan.json",
            _scan_payload(
                [
                    _scanned_order("1001", "2026-09-20", ["11"]),
                    _scanned_order("1002", "2026-09-21", ["12"]),
                    _scanned_order("1003", "2026-09-22", ["13"]),
                    _scanned_order("1004", "2026-09-23", ["14"]),
                    _scanned_order("1005", "2026-09-24", ["15"]),
                    _scanned_order("1006", "2026-09-25", ["16"]),
                ]
            ),
        )
        result = RUNNER.invoke(
            app, ["history", "sync-apply", "--listed-orders", str(listed), str(scan)]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "applied"
        assert data["imported_orders"] == 6
        assert data["report_path"]
        assert _imported_ids(cli_env.database_file) == {
            "1001",
            "1002",
            "1003",
            "1004",
            "1005",
            "1006",
        }

    def test_sync_apply_requires_listed_orders(self, cli_env: GrossyPaths, tmp_path: Path) -> None:
        """P1 regression: sync-apply requires --listed-orders for discovery binding."""
        scan = _write(
            tmp_path, "scan.json", _scan_payload([_scanned_order("1001", "2026-09-20", ["11"])])
        )
        result = RUNNER.invoke(app, ["history", "sync-apply", str(scan)])
        # Typer exits with code 2 when a required option is missing.
        assert result.exit_code == 2

    def test_sync_apply_invalid_fails_closed(self, cli_env: GrossyPaths, tmp_path: Path) -> None:
        listed = _write(tmp_path, "listed.json", _window_listed())
        bad = _write(tmp_path, "scan.json", _scan_payload([]))
        result = RUNNER.invoke(
            app, ["history", "sync-apply", "--listed-orders", str(listed), str(bad)]
        )
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["status"] == "error"
        assert "order-scan proposal rejected" in data["detail"]
        assert _imported_ids(cli_env.database_file) == set()

    def test_scan_validate_missing_file_fails(self, cli_env: GrossyPaths, tmp_path: Path) -> None:
        result = RUNNER.invoke(app, ["history", "scan-validate", str(tmp_path / "missing.json")])
        assert result.exit_code != 0


# --- scroll stabilization (FR-15, FR-16, FR-22) ------------------------------


class TestStabilizeScan:
    """Tests for the two-stable-bottom-pass scroll stabilization algorithm."""

    def _card(self, pid: str | None = "11", title: str = "Milk") -> RawProductCard:
        return RawProductCard(
            product_id=pid,
            title=title,
            canonical_url=f"https://www.ab.gr/en/eshop/Dairy/item/{pid}/p/{pid}" if pid else None,
        )

    def test_two_stable_passes_succeed(self) -> None:
        """FR-16: scan ends when two consecutive passes reveal no new IDs."""
        passes = [
            [self._card("11"), self._card("12")],
            [self._card("11"), self._card("12"), self._card("13")],
            [self._card("11"), self._card("12"), self._card("13")],
            [self._card("11"), self._card("12"), self._card("13")],
        ]
        result = stabilize_scan(passes)
        assert len(result) == 3
        assert {c.product_id for c in result} == {"11", "12", "13"}

    def test_minimum_two_passes(self) -> None:
        """Fewer than two passes is insufficient evidence of stability."""
        passes = [[self._card("11")]]
        with pytest.raises(ScanStabilizationError, match="at least 2 bottom passes"):
            stabilize_scan(passes)

    def test_unstable_last_pass_fails(self) -> None:
        """FR-16: if the last pass reveals a new ID, the scan is not stable."""
        passes = [
            [self._card("11"), self._card("12")],
            [self._card("11"), self._card("12")],
            [self._card("11"), self._card("12"), self._card("99")],
        ]
        with pytest.raises(ScanStabilizationError, match="not stable"):
            stabilize_scan(passes)

    def test_only_one_stable_pass_fails(self) -> None:
        """One stable pass is insufficient; two consecutive are required."""
        passes = [
            [self._card("11"), self._card("12")],
            [self._card("11"), self._card("12")],
        ]
        # Pass 1 discovers the IDs; pass 2 adds none. That is only one
        # stable bottom pass, so the required two consecutive passes are absent.
        with pytest.raises(ScanStabilizationError, match="not stable"):
            stabilize_scan(passes)

    def test_missing_product_id_fails_closed(self) -> None:
        """FR-22: if any card lacks a stable product ID, fail closed."""
        passes = [
            [self._card("11"), self._card(None, title="Unknown Item")],
            [self._card("11"), self._card(None, title="Unknown Item")],
        ]
        with pytest.raises(ScanStabilizationError, match="lacks a stable product ID"):
            stabilize_scan(passes)

    def test_missing_id_in_later_pass_fails(self) -> None:
        """FR-22: a missing ID in any pass (not just the first) fails the scan."""
        passes = [
            [self._card("11")],
            [self._card("11"), self._card("12")],
            [self._card("11"), self._card("12"), self._card(None)],
            [self._card("11"), self._card("12"), self._card(None)],
        ]
        with pytest.raises(ScanStabilizationError, match="lacks a stable product ID"):
            stabilize_scan(passes)

    def test_preserves_first_appearance_order(self) -> None:
        passes = [
            [self._card("33"), self._card("11")],
            [self._card("33"), self._card("11"), self._card("22")],
            [self._card("33"), self._card("11"), self._card("22")],
            [self._card("33"), self._card("11"), self._card("22")],
        ]
        result = stabilize_scan(passes)
        assert [c.product_id for c in result] == ["33", "11", "22"]

    def test_alternating_non_cumulative_passes_stabilize(self) -> None:
        """P2-1 regression: alternating passes {A},{B},{A},{B},{B} stabilize.

        With non-cumulative rendered cards, a pass may show only a subset of
        the full set. The last two passes add no globally new IDs, so the
        scan is stable despite the alternating pattern.
        """
        passes = [
            [self._card("A")],
            [self._card("B")],
            [self._card("A")],
            [self._card("B")],
            [self._card("B")],
        ]
        result = stabilize_scan(passes)
        assert {c.product_id for c in result} == {"A", "B"}


# --- order list filtering (FR-6, FR-7) ----------------------------------------


class TestFilterDatedOrders:
    """Tests for order list filtering with 'All orders' exclusion and no fixed count."""

    def _dated_entry(self, oid: str, label: str) -> OrderListEntry:
        return OrderListEntry(
            label=label,
            source_order_id=oid,
            navigation_url=_nav_url(oid),
        )

    def _all_orders_entry(self) -> OrderListEntry:
        return OrderListEntry(
            label="All orders",
            source_order_id=None,
            navigation_url=None,
            is_union_entry=True,
        )

    @pytest.mark.parametrize("count", [0, 1, 6, 60], ids=["zero", "one", "six", "many"])
    def test_excludes_all_orders_and_keeps_any_count(self, count: int) -> None:
        """FR-7: the 'All orders' union entry is excluded; any dated count remains."""
        entries = [self._all_orders_entry()] + [
            self._dated_entry(f"{1001 + i:09d}", f"{1 + i % 28:02d}/09/2026") for i in range(count)
        ]
        result = filter_dated_orders(entries)
        assert len(result) == count
        assert all(not e.is_union_entry for e in result)
        assert [e.source_order_id for e in result] == [f"{1001 + i:09d}" for i in range(count)]

    def test_rejects_missing_source_id(self) -> None:
        entries = [
            self._all_orders_entry(),
            OrderListEntry(label="20/09/2026", source_order_id=None, is_union_entry=False),
        ] + [self._dated_entry(str(1002 + i), f"{21 + i:02d}/09/2026") for i in range(5)]
        with pytest.raises(OrderListError, match="lacks a stable source order ID"):
            filter_dated_orders(entries)

    def test_no_all_orders_entry_still_works(self) -> None:
        """If the agent already excluded 'All orders', filtering still succeeds."""
        entries = [self._dated_entry(str(1001 + i), f"{20 + i:02d}/09/2026") for i in range(6)]
        result = filter_dated_orders(entries)
        assert len(result) == 6


# --- fake-browser integration (PRD 19.3) ---------------------------------------


class FakeOrderPage:
    """Simulates a lazy-loaded AB.gr order page for offline integration tests.

    Each call to ``scroll_to_bottom()`` may reveal more product cards,
    simulating lazy loading. Once all cards are loaded, repeated scrolls
    return the same set.
    """

    def __init__(self, all_products: list[RawProductCard], batch_size: int = 2) -> None:
        self._all_products = all_products
        self._batch_size = batch_size
        self._loaded = 0

    def scroll_to_bottom(self) -> list[RawProductCard]:
        self._loaded = min(self._loaded + self._batch_size, len(self._all_products))
        return self._all_products[: self._loaded]


def _scroll_fake_page_to_stability(
    page: FakeOrderPage, *, max_passes: int = 10
) -> list[list[RawProductCard]]:
    """Model a bounded driver that waits for three identical rendered sets."""
    passes: list[list[RawProductCard]] = []
    for _ in range(max_passes):
        passes.append(page.scroll_to_bottom())
        if len(passes) < 3:
            continue
        last_three = [
            {card.product_id for card in batch if card.product_id is not None}
            for batch in passes[-3:]
        ]
        if last_three[0] == last_three[1] == last_three[2]:
            return passes
    raise AssertionError(f"fake page did not stabilize in {max_passes} bottom passes")


class TestFakeBrowserIntegration:
    """Offline tests of synthetic browser observations (PRD 19.3).

    Covers six-order filtering, lazy-loaded cards, missing product IDs, and a
    test-authored driver loop that exercises preparation, validation, and apply.
    These tests do not exercise a live browser or production agent workflow.
    """

    def test_lazy_loaded_scan_reaches_stability(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """A lazy-loaded order page is scanned to stability and applied."""
        products = [
            RawProductCard(
                product_id=str(200 + i),
                title=f"Item {i}",
                canonical_url=f"https://www.ab.gr/en/eshop/Food/item-{i}/p/{200 + i}",
                category_heading="Food",
            )
            for i in range(5)
        ]
        page = FakeOrderPage(products, batch_size=2)

        # Scroll until stable (two consecutive passes with no new IDs).
        passes = _scroll_fake_page_to_stability(page)
        stabilized = stabilize_scan(passes)
        assert len(stabilized) == 5
        assert {c.product_id for c in stabilized} == {str(200 + i) for i in range(5)}

    def test_lazy_loaded_scan_proposal_validates_and_applies(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """Synthetic cards become a proposal that validates and applies."""
        products = [
            RawProductCard(
                product_id=str(300 + i),
                title=f"Product {i}",
                canonical_url=f"https://www.ab.gr/en/eshop/Food/item-{i}/p/{300 + i}",
                category_heading="Dairy",
            )
            for i in range(4)
        ]
        page = FakeOrderPage(products, batch_size=2)
        passes = _scroll_fake_page_to_stability(page)
        stabilized = stabilize_scan(passes)

        # Build the order-scan proposal from the stabilized scan.
        order_payload = {
            "source_order_id": "2001",
            "displayed_label": "25/09/2026",
            "order_date": "2026-09-25",
            "navigation_url": _nav_url("2001"),
            "displayed_product_count": 4,
            "products": [
                {
                    "product_id": c.product_id,
                    "canonical_url": c.canonical_url or "",
                    "title": c.title,
                    "brand": c.brand,
                    "category_heading": c.category_heading,
                }
                for c in stabilized
            ],
        }
        payload = _scan_payload([order_payload])
        path = _write(tmp_path, "scan.json", payload)

        # Validate and apply.
        val_result = run_scan_validate(path)
        assert val_result.order_count == 1
        assert val_result.product_card_count == 4

        apply_result = run_sync_apply(path, initialized_home)
        assert apply_result.status == "applied"
        assert apply_result.stats.imported_orders == 1
        assert apply_result.stats.new_products == 4

    def test_stabilization_rejects_missing_id_from_fake_page(self) -> None:
        """FR-22: stabilization rejects a rendered card without a stable ID."""
        products = [
            RawProductCard(
                product_id="400",
                title="Good",
                canonical_url="https://www.ab.gr/en/eshop/Food/g/p/400",
            ),
            RawProductCard(product_id=None, title="Broken Card"),
            RawProductCard(
                product_id="401",
                title="Also Good",
                canonical_url="https://www.ab.gr/en/eshop/Food/a/p/401",
            ),
        ]
        page = FakeOrderPage(products, batch_size=3)
        passes = []
        for _ in range(4):
            passes.append(page.scroll_to_bottom())

        with pytest.raises(ScanStabilizationError, match="lacks a stable product ID"):
            stabilize_scan(passes)

    def test_simulated_order_list_filters_all_orders_before_dry_run(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """Six synthetic dated entries exclude the synthetic 'All orders' entry."""
        # Simulate the rendered order list: 6 dated + 1 'All orders'.
        entries = [
            OrderListEntry(
                label=f"{20 + i:02d}/09/2026",
                source_order_id=str(5001 + i),
                navigation_url=_nav_url(str(5001 + i)),
            )
            for i in range(6)
        ]
        entries.append(
            OrderListEntry(label="All orders", is_union_entry=True, source_order_id=None)
        )

        dated = filter_dated_orders(entries)
        assert len(dated) == 6

        # Build the listed-orders proposal from the filtered entries.
        listed_payload = _listed_orders_payload(
            [
                {
                    "source_order_id": e.source_order_id or "",
                    "order_date": _iso_from_label(e.label),
                    "navigation_url": e.navigation_url or "",
                }
                for e in dated
            ]
        )
        listed_path = _write(tmp_path, "listed.json", listed_payload)

        # Dry-run should report 6 unknown orders on a fresh DB.
        result = run_sync_dry_run(listed_path, initialized_home)
        assert result.listed_order_count == 6
        assert result.unknown_order_count == 6

    def test_simulated_browser_driver_scans_prepared_orders_oldest_first(
        self, initialized_home: GrossyPaths, tmp_path: Path
    ) -> None:
        """Simulate a browser driver that follows scan-prepare's order list.

        The test supplies the driver loop and synthetic newest-first list; it
        checks the preparation and downstream scan/apply contracts, not live
        agent or browser behavior. Each simulated order requires two stable
        bottom passes.
        """
        # Pre-import the three newest window orders so the unknown set is the
        # three oldest.
        conn = open_connection(initialized_home.database_file)
        for i in range(3, 6):
            oid = str(6001 + i)
            conn.execute(
                "INSERT INTO source_orders (retailer, source_order_id, displayed_label, "
                "order_date, import_timestamp, created_at) VALUES ('ab', ?, ?, ?, ?, ?)",
                (
                    oid,
                    f"{20 + i:02d}/09/2026",
                    f"2026-09-{20 + i:02d}",
                    "2026-09-26T00:00:00.000000Z",
                    "2026-09-26T00:00:00.000000Z",
                ),
            )
        conn.execute("COMMIT")
        conn.close()

        # Rendered order list: newest first, with the 'All orders' union entry.
        entries = [
            OrderListEntry(
                label=f"{20 + i:02d}/09/2026",
                source_order_id=str(6001 + i),
                navigation_url=_nav_url(str(6001 + i)),
            )
            for i in range(5, -1, -1)
        ]
        entries.append(
            OrderListEntry(label="All orders", is_union_entry=True, source_order_id=None)
        )
        dated = filter_dated_orders(entries)

        # The listed-orders proposal keeps the rendered (newest-first) order.
        listed_payload = _listed_orders_payload(
            [
                {
                    "source_order_id": e.source_order_id or "",
                    "order_date": _iso_from_label(e.label),
                    "navigation_url": e.navigation_url or "",
                }
                for e in dated
            ]
        )
        listed_path = _write(tmp_path, "listed.json", listed_payload)

        # Dry-run stays count-only.
        dry = run_sync_dry_run(listed_path, initialized_home)
        assert dry.unknown_order_count == 3

        # Scan-prepare exposes the unknown orders oldest first.
        prepare = run_scan_prepare(listed_path, initialized_home)
        assert prepare.status == "ready"
        assert [o["source_order_id"] for o in prepare.unknown_orders] == [
            "6001",
            "6002",
            "6003",
        ]

        # Product sets per unknown order.
        product_sets = {
            "6001": ["700", "701", "702"],
            "6002": ["703", "704"],
            "6003": ["705", "706", "707", "708"],
        }

        scan_orders: list[dict[str, Any]] = []
        navigation_log: list[str] = []
        pass_counts: dict[str, int] = {}
        for unknown in prepare.unknown_orders:
            source_id = unknown["source_order_id"]
            navigation_log.append(source_id)  # simulated consumer follows prepare output
            product_ids = product_sets[source_id]
            products = [
                RawProductCard(
                    product_id=pid,
                    title=f"Product {pid}",
                    canonical_url=f"https://www.ab.gr/en/eshop/Food/item/{pid}/p/{pid}",
                    category_heading="Food",
                )
                for pid in product_ids
            ]
            page = FakeOrderPage(products, batch_size=2)
            passes = _scroll_fake_page_to_stability(page)
            # Verify the test-authored driver observed at least three equal sets.
            assert len(passes) >= 3, f"order {source_id} did not stabilize"
            pass_counts[source_id] = len(passes)
            stabilized = stabilize_scan(passes)
            assert {c.product_id for c in stabilized} == set(product_ids)

            scan_orders.append(
                {
                    "source_order_id": source_id,
                    "displayed_label": _label(unknown["order_date"]),
                    "order_date": unknown["order_date"],
                    "navigation_url": unknown["navigation_url"],
                    "displayed_product_count": len(product_ids),
                    "products": [
                        {
                            "product_id": c.product_id,
                            "canonical_url": c.canonical_url or "",
                            "title": c.title,
                            "brand": c.brand,
                            "category_heading": c.category_heading,
                        }
                        for c in stabilized
                    ],
                }
            )

        # The recorded navigation order is oldest first, not the rendered order.
        assert navigation_log == ["6001", "6002", "6003"]

        # Scan covers exactly the 3 unknown orders (6001..6003).
        scan_path = _write(tmp_path, "scan.json", _scan_payload(scan_orders))

        # Validate: the scan must be accepted.
        val = run_scan_validate(scan_path)
        assert val.order_count == 3

        # Apply with discovery binding.
        result = run_sync_apply(scan_path, initialized_home, listed_orders_path=listed_path)
        assert result.status == "applied"
        assert result.stats.imported_orders == 3
        assert result.stats.new_products == 9

        # Verify oldest-first insertion order (only the 3 new ones).
        conn = open_connection(initialized_home.database_file)
        rows = conn.execute(
            "SELECT source_order_id FROM source_orders "
            "WHERE source_order_id IN ('6001','6002','6003') ORDER BY id"
        ).fetchall()
        conn.close()
        assert [row[0] for row in rows] == ["6001", "6002", "6003"]

        # Verify each order was scanned with at least 3 passes (two stable).
        for oid in ("6001", "6002", "6003"):
            assert pass_counts[oid] >= 3, f"order {oid} had only {pass_counts[oid]} passes"


def _iso_from_label(label: str) -> str:
    """Convert a DD/MM/YYYY label to ISO YYYY-MM-DD."""
    day, month, year = label.split("/")
    return f"{year}-{month}-{day}"
