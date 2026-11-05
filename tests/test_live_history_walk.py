"""Offline fake-browser tests for the reservation-history enumeration walk and the per-order
scan/apply loop in history_sync.py.

The real live_common module runs against fake pages (evaluate/browser/settle_page seams) and
a fake clock; CLI phases run in-process against a private temporary home. No agent-browser,
no network, no real state.
"""

from __future__ import annotations

import importlib.util
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_history_sync import _imported_ids, _seed_imported

from grossy.history import (
    HistorySyncError,
    run_discover_prepare,
    run_scan_prepare,
    run_scan_validate,
    run_sync_apply,
    run_sync_dry_run,
    validate_listed_orders,
    validate_order_scan,
)
from grossy.init import do_init
from grossy.paths import GrossyPaths, resolve_paths

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/grossy/scripts"
LIST_URL = "https://www.ab.gr/en/my-account/reservation-history"
ORDER_URL = "https://www.ab.gr/my-account/order?id="


def order_id(index: int) -> str:
    return f"{900 - index:09d}"  # newest first, nine digits, leading zeros


def product_id(sid: str, index: int) -> str:
    return f"7{sid[-3:]}{index:02d}"


def card(pid: str, *, link: bool = True, brand: str = "Brand") -> dict[str, list[str]]:
    """One rendered order-page product card as the script's cards_js reports it."""
    hrefs = [f"/en/eshop/Food/item-{pid}/p/{pid}"] if link else ["/en/eshop/Food"]
    return {"hrefs": hrefs, "names": [f"Product {pid}"], "brands": [brand]}


def cards(sid: str, count: int = 3) -> list[dict[str, list[str]]]:
    return [card(product_id(sid, j)) for j in range(count)]


class Stop(Exception):
    pass


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class LazyPage:
    """A page rendering 10 items on open, then 10 more per settled bottom pass.

    ``stall``: the bottom is never reached (content keeps the page longer than the viewport)."""

    def __init__(self, items: list[Any], *, stall: bool = False) -> None:
        self.items = items
        self.rendered = min(10, len(items))
        self.stall = stall
        self.scrolls = 0
        self.at_bottom = False
        self.scrolled = False  # lazy loading is triggered by a scroll and lands on settle

    def scroll(self) -> None:
        self.scrolls += 1
        self.scrolled = True
        self.at_bottom = not self.stall

    def settle(self) -> None:
        if self.scrolled and self.rendered < len(self.items):
            self.rendered = min(len(self.items), self.rendered + 10)
            self.at_bottom = False  # the page grew below the scroll position
        self.scrolled = False

    def visible(self) -> list[Any]:
        return self.items[: self.rendered]


class FakeList:
    """Reservation-history list plus the order-details pages the script opens from it.

    ``products`` maps an order ID to its rendered cards (default: three linked cards per
    order). ``headings``: the ``Order NNNNNNNNN`` heading digit strings an order page renders
    (default: exactly its own ID). ``busy``: the network never idles, so no pass settles.
    ``order_stall``: those order pages never reach the bottom. ``fail_open``: opening that
    order's page stops as an authentication challenge. ``fail_scroll``: the pinned tab is lost
    on the first scroll of that order's page. Every seam records the ``recover`` flag it was
    called with and the settle timeouts it was given."""

    def __init__(
        self,
        dates: list[str],
        *,
        stall: bool = False,
        busy: bool = False,
        products: dict[str, list[dict[str, list[str]]]] | None = None,
        headings: dict[str, list[str]] | None = None,
        order_stall: frozenset[str] = frozenset(),
        fail_open: str | None = None,
        fail_scroll: str | None = None,
    ) -> None:
        self.entries = [(order_id(i), date) for i, date in enumerate(dates)]
        self.list = LazyPage(self.entries, stall=stall)
        self.current = self.list
        self.current_sid: str | None = None  # order ID of the open order page, if any
        self.busy = busy
        self.products = products or {}
        self.headings = headings or {}
        self.order_stall = order_stall
        self.fail_open = fail_open
        self.fail_scroll = fail_scroll
        self.opened: list[str] = []
        self.recover_flags: list[bool] = []
        self.settle_timeouts: list[float] = []
        self.cli_calls: list[tuple[str, ...]] = []  # every CLI phase call, in order

    @property
    def scrolls(self) -> int:
        return self.list.scrolls

    def browser(self, args: list[str], tag: str = "cmd", recover: bool = True) -> str:
        import live_common

        self.recover_flags.append(recover)
        if args[0] == "open":
            self.opened.append(args[1])
            if args[1] == LIST_URL:
                self.current, self.current_sid = self.list, None
            else:
                assert args[1].startswith(ORDER_URL)
                sid = args[1].removeprefix(ORDER_URL)
                if sid == self.fail_open:
                    live_common.stop("open:authentication_or_challenge")
                self.current = LazyPage(
                    self.products.get(sid, cards(sid)), stall=sid in self.order_stall
                )
                self.current_sid = sid
        return ""

    def settle_page(self, tag: str = "settle", recover: bool = True, timeout: float = 40) -> bool:
        self.recover_flags.append(recover)
        self.settle_timeouts.append(timeout)
        if self.busy:
            return False
        self.current.settle()
        return True

    def evaluate(self, body: str, tag: str = "eval", recover: bool = True) -> Any:
        import live_common

        self.recover_flags.append(recover)
        if body == live_common.SCROLL_BOTTOM_JS:
            if self.current_sid is not None and self.current_sid == self.fail_scroll:
                live_common.stop("exact_task_tab_unavailable")
            self.current.scroll()
            return True
        if body == live_common.AT_BOTTOM_JS:
            return self.current.at_bottom
        if "order-details-button" in body:
            assert self.current is self.list, "list read on an order page"
            return [
                {"hrefs": [f"/my-account/order?id={sid}"], "dates": [date]}
                for sid, date in self.current.visible()
            ]
        if "product-block" in body:
            assert self.current is not self.list, "order read on the list page"
            return self.current.visible()
        if "order-details-container] *" in body:
            assert self.current_sid is not None, "heading read on the list page"
            return self.headings.get(self.current_sid, [self.current_sid])
        if 'readyState==="complete"' in body or body.endswith("return true;"):
            return True
        pytest.fail("unexpected page read: " + body[:80])


@pytest.fixture
def home(tmp_path: Path) -> GrossyPaths:
    paths = resolve_paths(
        config_home=tmp_path / "config", data_home=tmp_path / "data", state_home=tmp_path / "state"
    )
    do_init(paths)
    return paths


def run_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths: GrossyPaths,
    page: FakeList,
    *args: str,
    work_name: str = "work",
) -> tuple[Path, Any, FakeClock]:
    """Run history_sync.py against ``page``; return (work dir, exit outcome, clock).

    Every CLI call is recorded in ``page.cli_calls``; sync-apply proposals must contain exactly
    one order (per-order transactions)."""
    work = tmp_path / work_name
    work.mkdir(mode=0o700)
    monkeypatch.setenv("GROSSY_LIVE_DIR", str(work))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    gate = runpy.run_path(str(SCRIPTS / "slot_gate.py"))
    gate["initialize"](work, history_only=True, reuse=False)
    spec = importlib.util.spec_from_file_location("live_common", SCRIPTS / "live_common.py")
    assert spec is not None and spec.loader is not None
    drill = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "live_common", drill)
    spec.loader.exec_module(drill)
    clock = FakeClock()

    def stop(reason: str) -> None:
        raise Stop(reason)

    def grossy(*args: str, out: str | None = None, **_: Any) -> dict[str, Any]:
        assert args[0] == "history"
        page.cli_calls.append(args)
        try:
            if args[1] == "discover-prepare":
                result = run_discover_prepare(paths).to_dict()
            elif args[1] == "scan-validate":
                assert len(args) == 3
                result = run_scan_validate(Path(args[2])).to_dict()
            elif args[1] == "sync-apply":
                assert args[3] == "--listed-orders" and len(args) == 5
                proposal = validate_order_scan(json.loads(Path(args[2]).read_text()))
                assert len(proposal.orders) == 1, "sync-apply must carry exactly one order"
                result = run_sync_apply(
                    Path(args[2]), paths, listed_orders_path=Path(args[4])
                ).to_dict()
            else:
                assert args[2] == "--listed-orders"
                phase = {"sync-dry-run": run_sync_dry_run, "scan-prepare": run_scan_prepare}[
                    args[1]
                ]
                result = phase(Path(args[3]), paths).to_dict()
        except HistorySyncError as exc:
            raise Stop(f"CLI {args[0]} {args[1]} exit 1: {exc}") from exc
        if out:
            drill.write(out, result)
        return result

    for key, value in {
        "time": clock,
        "stop": stop,
        "grossy": grossy,
        "browser": page.browser,
        "evaluate": page.evaluate,
        "settle_page": page.settle_page,
    }.items():
        monkeypatch.setattr(drill, key, value)
    monkeypatch.setattr(sys, "argv", ["history_sync.py", *args])
    try:
        runpy.run_path(str(SCRIPTS / "history_sync.py"))
    except (SystemExit, Stop) as exc:
        outcome: Any = exc
    else:
        outcome = None
    assert page.opened[:1] == [LIST_URL] and all(u.startswith(ORDER_URL) for u in page.opened[1:])
    # Strict pinned tab on every browser command of the history walk (no recovery path).
    assert page.recover_flags and not any(page.recover_flags)
    assert all(timeout <= drill.STALL_SECONDS for timeout in page.settle_timeouts)
    return work, outcome, clock


def listed(work: Path) -> list[dict[str, str]]:
    data = json.loads((work / "listed-orders.json").read_text())
    orders = validate_listed_orders(data).orders
    return [o.model_dump() for o in orders]


def enumeration(work: Path) -> dict[str, Any]:
    return json.loads((work / "enumeration.json").read_text())


def scan_apply(work: Path) -> dict[str, Any]:
    return json.loads((work / "scan-apply.json").read_text())


def applied_ids(page: FakeList) -> list[str]:
    """Order IDs of the single-order sync-apply calls, in call order."""
    ids = []
    for call in page.cli_calls:
        if call[1] == "sync-apply":
            proposal = validate_order_scan(json.loads(Path(call[2]).read_text()))
            ids.append(proposal.orders[0].source_order_id)
    return ids


def test_list_without_entries_stops_as_missing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    # No verified empty-list state exists: zero rendered entries is missing identity, not an
    # empty history. Nothing is walked and no proposal or enumeration result is written.
    page = FakeList([])
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page, "--dry-run")
    assert isinstance(outcome, Stop) and str(outcome) == "list_identity_missing"
    assert page.scrolls == 0
    assert (work / "discover-prepare.json").exists()
    assert not (work / "listed-orders.json").exists()
    assert not (work / "enumeration.json").exists()
    assert not (work / "dry-run.json").exists()


@pytest.mark.parametrize("count,passes", [(1, 2), (25, 4)])
def test_walk_lists_every_order_newest_first_after_two_settled_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths, count: int, passes: int
) -> None:
    page = FakeList([f"{1 + i % 28:02d}/10/2026" for i in range(count)])
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page, "--dry-run")
    assert isinstance(outcome, SystemExit) and outcome.code == 0
    orders = listed(work)
    assert [o["source_order_id"] for o in orders] == [order_id(i) for i in range(count)]
    assert enumeration(work) == {
        "status": "complete",
        "reason": "settled",
        "mode": "default",
        "ended": "settled",
        "bottom_passes": passes,
        "listed_order_count": count,
        "known_order_count": 0,
        "last_known_order_id": None,
        "last_imported_order_id": None,
    }
    assert json.loads((work / "dry-run.json").read_text())["unknown_order_count"] == count
    assert not (work / "scan-prepare.json").exists()


def test_leading_zero_ids_and_day_first_dates_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    home: GrossyPaths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    page = FakeList(["05/11/2026"])
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert outcome is None  # one unknown order scanned and applied; the script ran to its end
    assert listed(work) == [
        {
            "source_order_id": "000000900",
            "order_date": "2026-11-05",
            "navigation_url": "https://www.ab.gr/my-account/order?id=000000900",
        }
    ]
    prepared = json.loads((work / "scan-prepare.json").read_text())
    assert prepared["status"] == "ready" and prepared["unknown_order_count"] == 1
    assert prepared["unknown_orders"][0]["source_order_id"] == "000000900"
    # The order-scan proposal keeps the leading-zero ID, the listed ISO date and its DD/MM/YYYY
    # label; the order page is never read for a date.
    scan = json.loads((work / "order-scan-001.json").read_text())
    assert scan["schema_name"] == "grossy.history.order-scan.v1" and scan["schema_version"] == 1
    (order,) = scan["orders"]
    assert order["source_order_id"] == "000000900" and order["order_date"] == "2026-11-05"
    assert order["displayed_label"] == "05/11/2026"
    assert order["navigation_url"] == ORDER_URL + "000000900"
    assert [p["product_id"] for p in order["products"]] == [
        product_id("000000900", j) for j in range(3)
    ]
    assert order["products"][0] == {
        "product_id": "790000",
        "canonical_url": "https://www.ab.gr/en/eshop/Food/item-790000/p/790000",
        "title": "Product 790000",
        "brand": "Brand",
    }
    assert applied_ids(page) == ["000000900"]
    assert _imported_ids(home.database_file) == {"000000900"}
    assert scan_apply(work) == {
        "status": "complete",
        "reason": "all_unknown_orders_applied",
        "mode": "default",
        "unknown_order_count": 1,
        "applied_order_count": 1,
        "last_imported_order_id": "000000900",
    }
    # Privacy: no source order ID or URL on stdout.
    out = capsys.readouterr().out
    assert "000000900" not in out and "ab.gr" not in out and "order 1/1" in out


def test_zero_unknown_orders_apply_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    _seed_imported(home.database_file, [order_id(i) for i in range(3)])
    page = FakeList(["01/10/2026"] * 3)
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, SystemExit) and outcome.code == 0
    assert page.opened == [LIST_URL]  # no order page opened
    assert applied_ids(page) == [] and not (work / "scan-apply.json").exists()
    assert json.loads((work / "scan-prepare.json").read_text())["status"] == "no-op"
    assert _imported_ids(home.database_file) == {order_id(i) for i in range(3)}


def test_default_walk_stops_at_first_imported_order_and_applies_each_unknown_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    _seed_imported(home.database_file, [order_id(i) for i in range(12, 25)])
    # Many unknown orders; the newest renders 19 cards over two lazy loads (three bottom passes).
    page = FakeList(["01/10/2026"] * 25, products={order_id(0): cards(order_id(0), 19)})
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert outcome is None
    assert [o["source_order_id"] for o in listed(work)] == [order_id(i) for i in range(13)]
    result = enumeration(work)
    assert result["status"] == "complete"
    assert result["ended"] == result["reason"] == "stopped_at_known_order"
    assert result["bottom_passes"] == 1 and page.scrolls == 1
    assert result["known_order_count"] == 1
    assert result["last_known_order_id"] == order_id(12)
    prepared = json.loads((work / "scan-prepare.json").read_text())
    assert prepared["unknown_order_count"] == 12
    assert prepared["unknown_orders"][0]["source_order_id"] == order_id(11)  # oldest first
    # Each unknown order was opened, scanned, validated and applied newest first, exactly once
    # and as its own sync-apply call; the result names the last imported ID.
    assert page.opened[1:] == [ORDER_URL + order_id(i) for i in range(12)]
    assert applied_ids(page) == [order_id(i) for i in range(12)]
    phases = [c[1] for c in page.cli_calls if c[1] in ("scan-validate", "sync-apply")]
    assert phases == ["scan-validate", "sync-apply"] * 12
    assert _imported_ids(home.database_file) == {order_id(i) for i in range(25)}
    first = json.loads((work / "order-scan-001.json").read_text())["orders"][0]
    assert len(first["products"]) == 19
    assert json.loads((work / "sync-apply-012.json").read_text())["imported_orders"] == 1
    assert scan_apply(work) == {
        "status": "complete",
        "reason": "all_unknown_orders_applied",
        "mode": "default",
        "unknown_order_count": 12,
        "applied_order_count": 12,
        "last_imported_order_id": order_id(11),
    }


def test_interruption_leaves_exactly_the_applied_orders_and_full_rerun_imports_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    dates = ["01/10/2026"] * 5
    # Authentication loss while opening the third order page: two orders are already durable.
    page = FakeList(dates, fail_open=order_id(2))
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page, work_name="w1")
    assert isinstance(outcome, Stop) and str(outcome) == "open:authentication_or_challenge"
    assert applied_ids(page) == [order_id(0), order_id(1)]
    assert _imported_ids(home.database_file) == {order_id(0), order_id(1)}
    assert not (work / "order-scan-003.json").exists()
    # The browser-level stop reason is recorded in scan-apply.json, not only on the STOP line.
    assert scan_apply(work) == {
        "status": "stopped",
        "reason": "open:authentication_or_challenge",
        "mode": "default",
        "unknown_order_count": 5,
        "applied_order_count": 2,
        "last_imported_order_id": order_id(1),
        "scan_sequence": 3,
    }

    # A following default run stops at the newest imported ID: no resumption, no replay.
    page2 = FakeList(dates)
    work2, outcome2, _ = run_walk(tmp_path, monkeypatch, home, page2, work_name="w2")
    assert isinstance(outcome2, SystemExit) and outcome2.code == 0
    assert page2.opened == [LIST_URL] and applied_ids(page2) == []
    assert enumeration(work2)["ended"] == "stopped_at_known_order"
    assert enumeration(work2)["last_known_order_id"] == order_id(0)
    assert _imported_ids(home.database_file) == {order_id(0), order_id(1)}

    # A separately authorized --full rerun skips the two imported IDs and imports the rest.
    page3 = FakeList(dates)
    work3, outcome3, _ = run_walk(tmp_path, monkeypatch, home, page3, "--full", work_name="w3")
    assert outcome3 is None
    assert enumeration(work3)["known_order_count"] == 2
    assert page3.opened[1:] == [ORDER_URL + order_id(i) for i in (2, 3, 4)]
    assert applied_ids(page3) == [order_id(2), order_id(3), order_id(4)]
    assert _imported_ids(home.database_file) == {order_id(i) for i in range(5)}
    assert scan_apply(work3)["status"] == "complete"
    assert scan_apply(work3)["last_imported_order_id"] == order_id(4)


def test_missing_canonical_link_stops_the_order_with_nothing_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    broken = cards(order_id(1))
    broken[1] = card(product_id(order_id(1), 1), link=False)
    page = FakeList(["01/10/2026"] * 3, products={order_id(1): broken})
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome).startswith(
        "product_link_missing: order 2 of 3"
    )
    assert applied_ids(page) == [order_id(0)]
    assert _imported_ids(home.database_file) == {order_id(0)}
    assert not (work / "order-scan-002.json").exists()
    assert scan_apply(work) == {
        "status": "partial",
        "reason": "product_link_missing",
        "mode": "default",
        "unknown_order_count": 3,
        "applied_order_count": 1,
        "last_imported_order_id": order_id(0),
        "scan_sequence": 2,
    }


def test_pinned_tab_loss_during_an_order_walk_records_the_stop_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    page = FakeList(["01/10/2026"] * 3, fail_scroll=order_id(1))
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome) == "exact_task_tab_unavailable"
    assert applied_ids(page) == [order_id(0)]
    assert _imported_ids(home.database_file) == {order_id(0)}
    assert not (work / "order-scan-002.json").exists()
    assert scan_apply(work) == {
        "status": "stopped",
        "reason": "exact_task_tab_unavailable",
        "mode": "default",
        "unknown_order_count": 3,
        "applied_order_count": 1,
        "last_imported_order_id": order_id(0),
        "scan_sequence": 2,
    }


@pytest.mark.parametrize(
    "headings,reason",
    [
        pytest.param([order_id(0)], "order_heading_mismatch", id="other-order"),
        pytest.param([order_id(1), order_id(0)], "order_heading_mismatch", id="two-headings"),
        pytest.param([], "order_heading_missing", id="no-heading"),
    ],
)
def test_order_heading_must_name_the_requested_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    home: GrossyPaths,
    headings: list[str],
    reason: str,
) -> None:
    # The route ID matches, but the details heading inside the container does not name exactly
    # this order: stop before any card is read, with nothing applied for this order.
    page = FakeList(["01/10/2026"] * 3, headings={order_id(1): headings})
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome) == f"{reason}: order 2 of 3"
    assert page.current.scrolls == 0  # the mismatched page was never walked
    assert applied_ids(page) == [order_id(0)]
    assert _imported_ids(home.database_file) == {order_id(0)}
    assert not (work / "order-scan-002.json").exists()
    assert scan_apply(work) == {
        "status": "partial",
        "reason": reason,
        "mode": "default",
        "unknown_order_count": 3,
        "applied_order_count": 1,
        "last_imported_order_id": order_id(0),
        "scan_sequence": 2,
    }


def test_order_page_stall_ends_the_run_with_a_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    page = FakeList(["01/10/2026"] * 3, order_stall=frozenset({order_id(1)}))
    work, outcome, clock = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome).startswith("order_scan_stalled: order 2 of 3")
    assert applied_ids(page) == [order_id(0)]
    assert _imported_ids(home.database_file) == {order_id(0)}
    assert page.current.scrolls == 20 and clock.now >= 30  # only the 30 s stall rule ended it
    assert scan_apply(work) == {
        "status": "partial",
        "reason": "stall_rule",
        "mode": "default",
        "unknown_order_count": 3,
        "applied_order_count": 1,
        "last_imported_order_id": order_id(0),
        "scan_sequence": 2,
    }


def test_full_walk_skips_imported_orders_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    _seed_imported(home.database_file, [order_id(i) for i in (3, *range(12, 25))])
    page = FakeList(["01/10/2026"] * 25)
    work, outcome, _ = run_walk(tmp_path, monkeypatch, home, page, "--full", "--dry-run")
    assert isinstance(outcome, SystemExit) and outcome.code == 0
    assert [o["source_order_id"] for o in listed(work)] == [order_id(i) for i in range(25)]
    result = enumeration(work)
    assert result["mode"] == "full" and result["ended"] == "settled"
    assert result["bottom_passes"] == 4
    assert result["known_order_count"] == 14
    assert result["last_known_order_id"] == order_id(24)
    assert json.loads((work / "dry-run.json").read_text())["unknown_order_count"] == 11


def test_stall_rule_ends_the_run_with_a_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    page = FakeList(["01/10/2026"] * 10, stall=True)
    work, outcome, clock = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome).startswith("enumeration_stalled")
    assert 30 <= clock.now < 32
    assert page.scrolls == 20  # no scroll-count ceiling: only the 30 s stall rule ended it
    result = enumeration(work)
    assert result["status"] == "partial" and result["reason"] == "stall_rule"
    assert result["ended"] == "stalled" and result["listed_order_count"] == 10
    assert result["last_imported_order_id"] is None
    assert len(listed(work)) == 10
    assert not (work / "dry-run.json").exists()


def test_busy_network_never_counts_as_settled_and_ends_as_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    page = FakeList(["01/10/2026"] * 10, busy=True)
    work, outcome, clock = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome).startswith("enumeration_stalled")
    assert 30 <= clock.now < 32
    assert page.scrolls == 20
    result = enumeration(work)
    assert result["status"] == "partial" and result["reason"] == "stall_rule"
    assert result["listed_order_count"] == 10 and result["last_imported_order_id"] is None
    # Each network wait is bounded by the remaining stall budget, never the old fixed 40 s.
    walk_timeouts = page.settle_timeouts[1:]
    assert walk_timeouts[0] == 30 and walk_timeouts[-1] >= 1
    assert walk_timeouts == sorted(walk_timeouts, reverse=True) and len(set(walk_timeouts)) == 20


def test_malformed_entry_stops_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: GrossyPaths
) -> None:
    page = FakeList(["2026-10-01"])
    _, outcome, _ = run_walk(tmp_path, monkeypatch, home, page)
    assert isinstance(outcome, Stop) and str(outcome) == "order_entry_malformed"


def load_live_common(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("GROSSY_LIVE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("live_common", SCRIPTS / "live_common.py")
    assert spec is not None and spec.loader is not None
    drill = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "live_common", drill)
    spec.loader.exec_module(drill)

    def stop(reason: str) -> None:
        raise Stop(reason)

    monkeypatch.setattr(drill, "stop", stop)
    return drill


class FakeAgentBrowser:
    """subprocess.run stand-in: the pinned target cannot be selected; everything else succeeds
    (blank tabs only, so the default recovery path would relaunch)."""

    def __init__(self, *, network_busy: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.network_busy = network_busy

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ["agent-browser", "--session"]
        self.calls.append(args)
        self.timeouts.append(kwargs.get("timeout"))
        command = [a for a in args[3:] if not a.startswith("--")]
        if command[:2] == ["tab", "list"]:
            tabs = [{"targetId": "relaunched", "url": "about:blank"}]
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"tabs": tabs}}), "")
        if command[:1] == ["tab"]:
            return subprocess.CompletedProcess(args, 1, "", "tab_gone")
        if command[:1] == ["wait"] and self.network_busy:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, "true", "")

    def commands(self) -> list[str]:
        """The agent-browser subcommand of every call (tab, open, close, wait, eval ...)."""
        return [c[c.index("--pin-tab") + 1] if "--pin-tab" in c else c[3] for c in self.calls]


@pytest.mark.parametrize("seam", ["browser", "evaluate", "settle_page", "bottom_pass"])
def test_strict_pinned_tab_stops_on_target_loss_without_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    drill = load_live_common(tmp_path, monkeypatch)
    target = json.dumps({"target_id": "original"})
    (tmp_path / "task-tab-target.json").write_text(target)
    fake = FakeAgentBrowser()
    monkeypatch.setattr(drill.subprocess, "run", fake.run)
    monkeypatch.setattr(drill.time, "sleep", lambda seconds: None)
    call = {
        "browser": lambda: drill.browser(["open", LIST_URL], "t", recover=False),
        "evaluate": lambda: drill.evaluate("return true;", "t", recover=False),
        "settle_page": lambda: drill.settle_page("t", recover=False),
        "bottom_pass": lambda: drill.bottom_pass("t"),
    }[seam]
    with pytest.raises(Stop, match="^exact_task_tab_unavailable$"):
        call()
    # Exactly one pinned-target selection, then nothing: no tab list, close, relaunch, retry,
    # wait or eval; the bound target is unchanged.
    assert len(fake.calls) == 1 and fake.commands() == ["tab"]
    assert fake.calls[0][-1] == "original" and "--pin-tab" in fake.calls[0]
    assert (tmp_path / "task-tab-target.json").read_text() == target


def test_default_tab_selection_recovers_only_with_recover_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drill = load_live_common(tmp_path, monkeypatch)
    (tmp_path / "task-tab-target.json").write_text(json.dumps({"target_id": "original"}))
    fake = FakeAgentBrowser()
    monkeypatch.setattr(drill.subprocess, "run", fake.run)
    with pytest.raises(Stop, match="^exact_task_tab_unavailable$"):
        drill.settle_page("t")
    # The unrelated callers' path consulted the tab list and relaunched before giving up.
    assert ["tab", "list"] in [c[3:5] for c in fake.calls]
    assert "open" in fake.commands()


def test_settle_page_reports_a_busy_network_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drill = load_live_common(tmp_path, monkeypatch)
    (tmp_path / "task-tab-target.json").write_text(json.dumps({"target_id": "original"}))
    fake = FakeAgentBrowser(network_busy=True)
    monkeypatch.setattr(drill, "select_task_tab", lambda recover=True: None)
    monkeypatch.setattr(drill.subprocess, "run", fake.run)
    assert drill.settle_page("t", recover=False, timeout=7.5) is False
    assert fake.commands() == ["wait"] and fake.timeouts == [7.5]
    assert fake.calls[0][-3:] == ["wait", "--load", "networkidle"] and "--pin-tab" in fake.calls[0]


def test_tab_list_failure_means_no_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drill = load_live_common(tmp_path, monkeypatch)

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ["agent-browser", "--session"] and args[3:] == ["tab", "list", "--json"]
        body = {"success": False, "data": None, "error": "Auto-launch failed"}
        return subprocess.CompletedProcess(args, 1, json.dumps(body), "")

    monkeypatch.setattr(drill.subprocess, "run", fake_run)
    assert drill._tabs() is None


def test_start_opens_reservation_history_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "grossy").mkdir()
    opened: list[str] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[0] == "agent-browser"
        if "list" in args:
            tabs = [{"targetId": "fake-target", "url": "about:blank"}]
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"tabs": tabs}}), "")
        if "open" in args:
            opened.append(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["live_start.py", "--history-only"])
    monkeypatch.syspath_prepend(str(SCRIPTS))
    runpy.run_path(str(SCRIPTS / "live_start.py"))
    assert opened == [LIST_URL]
