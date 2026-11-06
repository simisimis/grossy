"""Offline regressions for the live recurring checkout boundary (no browser/profile)."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/grossy/scripts"
spec = importlib.util.spec_from_file_location("recurring_cart", SCRIPTS / "recurring_cart.py")
assert spec is not None and spec.loader is not None
cart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cart)


def reading(title: str, availability: str = "available") -> dict[str, Any]:
    return {"title": title, "availability": availability, "displayed_price_eur": "2.00"}


def row(name: str, quantity: str = "1") -> dict[str, Any]:
    return {"name": name, "brand": "AB", "quantity": quantity, "price_parts": ["€", "2", "00"]}


def test_unavailable_salad_row_without_quantity_does_not_block_other_products() -> None:
    readings = {"111": reading("AB Salad", "unavailable"), "222": reading("AB Cheese")}
    rows = [{"name": "Salad", "brand": "AB", "unavailable": True}, row("Cheese")]
    for badge in (["1"], ["2"]):
        assert cart.reconcile(
            {"rows": rows, "badge": badge, "empty": 0}, {"111": 1, "222": 1}, readings
        ) == {"222": 1}


def test_unavailable_row_requires_product_page_confirmation() -> None:
    with pytest.raises(ValueError, match="candidates=0"):
        cart.reconcile(
            {
                "rows": [{"name": "Salad", "brand": "AB", "unavailable": True}],
                "badge": ["1"],
                "empty": 0,
            },
            {"111": 1},
            {"111": reading("AB Salad")},
        )


def test_unavailable_rows_do_not_create_cart_credit_or_guessed_quantities() -> None:
    result = cart.reconcile(
        {
            "rows": [{"name": "Salad", "brand": "AB", "unavailable": True}],
            "badge": ["1"],
            "empty": 0,
        },
        {"111": 2},
        {"111": reading("AB Salad", "unavailable")},
    )
    assert result == {}


@pytest.mark.parametrize(
    "problem", ["quantity", "price", "unknown-row", "badge", "missing-line", "controls"]
)
def test_unavailable_does_not_hide_other_checkout_errors(problem: str) -> None:
    readings = {"111": reading("AB Salad", "unavailable"), "222": reading("AB Cheese")}
    checkout = {"rows": [row("Cheese")], "badge": ["1"], "empty": 0}
    if problem == "quantity":
        checkout["rows"][0]["quantity"] = "2"
    elif problem == "price":
        checkout["rows"][0]["price_parts"] = ["€", "3", "00"]
    elif problem == "unknown-row":
        checkout["rows"].append(row("Unknown"))
    elif problem == "badge":
        checkout["badge"] = ["8"]
    elif problem == "missing-line":
        checkout["rows"] = []
    else:
        checkout["rows"] = [
            {"ambiguous": True, "reason": "missing_quantity_without_unavailable_control"}
        ]
    with pytest.raises(ValueError, match="checkout_"):
        cart.reconcile(checkout, {"111": 1, "222": 1}, readings)


def test_cannot_verify_a_write_from_an_unavailable_target() -> None:
    with pytest.raises(ValueError, match="written_target_unavailable"):
        cart.reconcile(
            {"rows": [], "badge": ["0"], "empty": 1},
            {"111": 1},
            {"111": reading("AB Salad", "unavailable")},
            protected={"111"},
        )


def test_empty_cart_requires_exactly_one_empty_message() -> None:
    assert cart.reconcile({"rows": [], "badge": [], "empty": 1}, {}, {}) == {}
    for empty in (0, 2):
        with pytest.raises(ValueError, match="checkout_missing_empty_evidence"):
            cart.reconcile({"rows": [], "badge": [], "empty": empty}, {}, {})


def unavailable_cart() -> dict[str, Any]:
    return {
        "rows": [{"name": "Salad", "brand": "AB", "unavailable": True}],
        "badge": ["0"],
        "empty": 0,
    }


def test_bind_unavailable_rows_reads_known_product_page_once() -> None:
    readings: dict[str, Any] = {}
    known = {"111": {"title": "AB Salad", "canonical_url": "https://store.example/p/111"}}
    reads: list[tuple[str, str]] = []

    def read(pid: str, url: str) -> None:
        reads.append((pid, url))
        readings[pid] = reading("AB Salad", "unavailable")

    cart.bind_unavailable_rows(unavailable_cart(), readings, known, read)
    cart.bind_unavailable_rows(unavailable_cart(), readings, known, read)
    assert reads == [("111", "https://store.example/p/111")]


def test_bind_unavailable_rows_rejects_unknown_ambiguous_or_disagreeing_identity() -> None:
    def read(pid: str, url: str) -> None:
        raise AssertionError("no read expected")

    with pytest.raises(ValueError, match="candidates=0"):
        cart.bind_unavailable_rows(unavailable_cart(), {}, {}, read)
    both = {"111": reading("AB Salad"), "222": reading("AB Salad")}
    with pytest.raises(ValueError, match="candidates=2"):
        cart.bind_unavailable_rows(unavailable_cart(), both, {}, read)

    def read_available(pid: str, url: str) -> None:
        readings[pid] = reading("AB Salad", "available")

    readings: dict[str, Any] = {}
    with pytest.raises(ValueError, match="availability_disagrees:111"):
        cart.bind_unavailable_rows(
            unavailable_cart(), readings, {"111": {"title": "AB Salad"}}, read_available
        )


def test_all_live_scripts_compile() -> None:
    import py_compile

    for script in sorted(SCRIPTS.glob("*.py")):
        py_compile.compile(str(script), doraise=True)


def unbound(name: str, quantity: str = "1", price: str = "2.00") -> dict[str, Any]:
    return {"name": name, "brand": "AB", "quantity": quantity, "price": price}


def test_unchanged_unbound_rows_pass_without_credit() -> None:
    readings = {"222": reading("AB Cheese")}
    checkout = {
        "rows": [row("Cheese"), row("Olives", "3"), row("Bread")],
        "badge": ["5"],
        "empty": 0,
    }
    recorded = [unbound("Bread"), unbound("Olives", "3")]  # order differs from the checkout
    for rows in (checkout["rows"], list(reversed(checkout["rows"]))):
        assert cart.reconcile(
            {**checkout, "rows": rows}, {"222": 1}, readings, unbound=recorded
        ) == {"222": 1}


@pytest.mark.parametrize("drift", ["vanished", "changed", "replaced", "added"])
def test_unbound_rows_drift_stops(drift: str) -> None:
    readings = {"222": reading("AB Cheese")}
    rows = [row("Cheese"), row("Olives", "3"), row("Bread")]
    recorded = [unbound("Olives", "3"), unbound("Bread")]
    if drift == "vanished":
        rows.pop()
    elif drift == "changed":
        rows[1] = row("Olives", "2")
    elif drift == "replaced":
        rows[2] = row("Butter")
    else:
        rows.append(row("Butter"))
    with pytest.raises(ValueError, match="checkout_unbound_rows_changed"):
        cart.reconcile(
            {"rows": rows, "badge": [str(sum(int(r["quantity"]) for r in rows))], "empty": 0},
            {"222": 1},
            readings,
            unbound=recorded,
        )


def test_unchanged_unbound_row_named_within_a_bound_title_stays_unbound() -> None:
    """The baseline bound the Gouda row to 222 and recorded the plain cheese row unbound; the
    recorded row is recognised as itself before the title match that would otherwise make it
    a quantity mismatch for 222. The bound line's own checks still apply."""
    readings = {"222": reading("AB Cheese Gouda")}
    rows = [row("Cheese Gouda"), row("Cheese", "3")]
    recorded = [unbound("Cheese", "3")]
    assert cart.reconcile(
        {"rows": rows, "badge": ["4"], "empty": 0}, {"222": 1}, readings, unbound=recorded
    ) == {"222": 1}
    with pytest.raises(ValueError, match="checkout_row_1:identity_quantity_or_price_mismatch"):
        cart.reconcile(
            {"rows": [row("Cheese Gouda", "2"), rows[1]], "badge": ["5"], "empty": 0},
            {"222": 1},
            readings,
            unbound=recorded,
        )


def test_unbound_rows_are_identity_free_and_count_in_the_badge() -> None:
    assert cart.unbound_row(row("Olives", "3")) == {
        "name": "Olives",
        "brand": "AB",
        "quantity": "3",
        "price": "2.00",
    }
    checkout = {"rows": [row("Cheese"), row("Olives", "3")], "badge": ["1"], "empty": 0}
    with pytest.raises(ValueError, match="checkout_header_badge_inconsistent"):
        cart.reconcile(
            checkout, {"222": 1}, {"222": reading("AB Cheese")}, unbound=[unbound("Olives", "3")]
        )


def test_page_bound_row_binds_without_a_title_match() -> None:
    """A row bound by page evidence keeps its identity through the page-bound record even when
    the rendered name is not part of the page title; quantity and price still apply."""
    readings = {"222": reading("AB Cheese"), "333": reading("AB Butter")}
    page_bound = {"222": unbound("Olives")}
    checkout = {"rows": [row("Olives"), row("Bread")], "badge": ["2"], "empty": 0}
    assert cart.reconcile(
        checkout, {"222": 1}, readings, unbound=[unbound("Bread")], page_bound=page_bound
    ) == {"222": 1}
    with pytest.raises(ValueError, match="checkout_missing_buyable_lines:222"):
        cart.reconcile(checkout, {"222": 1}, readings, unbound=[unbound("Bread")])
    with pytest.raises(ValueError, match="checkout_row_1:identity_quantity_or_price_mismatch"):
        cart.reconcile(
            checkout, {"222": 2}, readings, unbound=[unbound("Bread")], page_bound=page_bound
        )
    # A changed quantity no longer equals the record: the row is neither bound nor recorded.
    with pytest.raises(ValueError, match="checkout_missing_buyable_lines:222"):
        cart.reconcile(
            {**checkout, "rows": [row("Olives", "2"), row("Bread")], "badge": ["3"]},
            {"222": 1},
            readings,
            unbound=[unbound("Bread")],
            page_bound=page_bound,
        )


def load_bind_cart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkout: dict[str, Any],
    title: str = "AB Cheese",
    cards: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[str]]:
    """Real cart_bind over a fake browser: ``checkout`` is the settled /checkout read; every
    product page renders ``title`` with one unit in the cart at 2.00; every exact-search page
    renders ``cards`` (none by default, so every row the known pages do not bind is unbound)."""
    stops: list[str] = []

    def evaluate(script: str, tag: str | None = None) -> Any:
        if "unexpected_cart_route" in script:
            return {"ready": "complete", **json.loads(json.dumps(checkout))}
        return "settled" if 'readyState==="complete"?' in script else cards or []

    def product(pid: str, canonical_url: str, required_quantity: Any = None, **_: Any) -> Any:
        assert required_quantity is None
        return {
            "product_id": pid,
            "canonical_url": canonical_url,
            "title": title,
            "availability": "available",
            "displayed_price_eur": "2.00",
            "quantity": 1,
        }

    def stop(reason: str) -> None:
        stops.append(reason)
        raise SystemExit(1)

    drill = ModuleType("live_common")
    for key, value in {
        "D": tmp_path,
        "CHECKOUT_URL": "https://store.example/checkout",
        "STORE": "https://store.example",
        "EMPTY_RE": "/^$/",
        "now": lambda: "2026-11-06T12:00:00+00:00",
        "browser": lambda *_: None,
        "evaluate": evaluate,
        "product": product,
        "price_of": lambda parts: parts[1] + "." + parts[2],
        "stop": stop,
        "write": lambda name, data: (tmp_path / name).write_text(json.dumps(data)),
    }.items():
        setattr(drill, key, value)
    monkeypatch.setitem(sys.modules, "live_common", drill)
    monkeypatch.setitem(sys.modules, "recurring_cart", cart)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    bind_spec = importlib.util.spec_from_file_location("cart_bind", SCRIPTS / "cart_bind.py")
    assert bind_spec is not None and bind_spec.loader is not None
    module = importlib.util.module_from_spec(bind_spec)
    bind_spec.loader.exec_module(module)
    return module, stops


@pytest.mark.parametrize("drift", [None, "vanished", "changed", "replaced"])
def test_recurring_seed_never_replaces_baseline_unbound_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str | None
) -> None:
    checkout = {"rows": [row("Cheese"), row("Olives", "3")], "badge": ["4"], "empty": 0}
    bind, stops = load_bind_cart(tmp_path, monkeypatch, checkout)
    known = {"222": {"title": "AB Cheese", "canonical_url": "https://store.example/p/222"}}
    obs, bound, unavailable, searched, unbound_rows = bind.bind_cart(known, "baseline")
    assert [line["product_id"] for line in obs["lines"]] == ["222"] and set(bound) == {"222"}
    assert (unavailable, searched, unbound_rows) == ([], [], [unbound("Olives", "3")])
    record = tmp_path / cart.UNBOUND_RECORD
    assert json.loads(record.read_text()) == unbound_rows
    if drift == "vanished":
        checkout["rows"].pop()
        checkout["badge"] = ["1"]
    elif drift == "changed":
        checkout["rows"][1] = row("Olives", "2")
        checkout["badge"] = ["3"]
    elif drift == "replaced":
        checkout["rows"][1] = row("Butter", "3")
    recorded = json.loads(record.read_text())
    if drift is None:
        assert bind.bind_cart(known, "seed", recorded)[4] == unbound_rows
        assert stops == []
    else:
        with pytest.raises(SystemExit):
            bind.bind_cart(known, "seed", recorded)
        assert stops == [
            "checkout_unbound_rows_changed:recorded=1:current=" + str(int(drift != "vanished"))
        ]
    assert json.loads(record.read_text()) == unbound_rows  # the baseline record is preserved
    assert json.loads((tmp_path / cart.PAGE_BOUND_RECORD).read_text()) == {}
    assert (tmp_path / "seed-unbound.json").exists()


@pytest.mark.parametrize("planned", [True, False])
def test_recurring_seed_binds_recorded_row_through_exact_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned: bool
) -> None:
    """The stored title of the planned cheese product went stale, so its recorded row binds
    through exact search instead of the known-title pass; the seed still promotes the row into
    the page-bound record, and a later seed read binds it through that record, title or not.
    A search binding to a product the run does not know stays a changed unbound list."""
    checkout = {"rows": [row("Cheese"), row("Olives", "3")], "badge": ["4"], "empty": 0}
    bind, stops = load_bind_cart(tmp_path, monkeypatch, checkout)  # search finds nothing
    _, bound, _, _, unbound_rows = bind.bind_cart({}, "baseline")
    assert bound == {} and len(unbound_rows) == 2
    record, page_record = tmp_path / cart.UNBOUND_RECORD, tmp_path / cart.PAGE_BOUND_RECORD
    cards = [{"url": "https://store.example/p/222", "name": "Cheese", "brand": "AB"}]
    bind, stops = load_bind_cart(tmp_path, monkeypatch, checkout, cards=cards)
    known = {"222": {"title": "AB Gouda", "canonical_url": "https://store.example/p/222"}}
    if not planned:
        with pytest.raises(SystemExit):
            bind.bind_cart({}, "seed", json.loads(record.read_text()), {})
        assert stops == ["checkout_unbound_rows_changed:recorded=2:current=1"]
        assert json.loads(record.read_text()) == unbound_rows
        return
    obs, bound, _, searched, now_unbound = bind.bind_cart(
        known, "seed", json.loads(record.read_text()), {}
    )
    assert stops == [] and set(bound) == {"222"} and [r["product_id"] for r in searched] == ["222"]
    assert [line["product_id"] for line in obs["lines"]] == ["222"]
    assert now_unbound == [unbound("Olives", "3")] == json.loads(record.read_text())
    assert json.loads(page_record.read_text()) == {"222": unbound("Cheese")}
    assert json.loads((tmp_path / "seed-page-bound-222.json").read_text()) == {
        "product_id": "222",
        "quantity": 1,
        "displayed_price_eur": "2.00",
        "row": unbound("Cheese"),
    }
    bind, stops = load_bind_cart(tmp_path, monkeypatch, checkout)  # no search hit this time
    bind.bind_cart(
        known, "seed", json.loads(record.read_text()), json.loads(page_record.read_text())
    )
    assert stops == [] and json.loads(page_record.read_text()) == {"222": unbound("Cheese")}


def test_advance_re_records_page_bound_rows_without_a_title_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the basket-review barrier a page-bound row keeps its identity through the record
    (the page title does not contain its rendered name); a row the owner changed falls out of
    the page-bound record and is re-recorded as unbound."""
    checkout = {"rows": [row("Olives")], "badge": ["1"], "empty": 0}
    bind, stops = load_bind_cart(tmp_path, monkeypatch, checkout)
    known = {"222": {"title": "AB Cheese", "canonical_url": "https://store.example/p/222"}}
    page_bound = {"222": unbound("Olives")}
    obs, bound, _, searched, unbound_rows = bind.bind_cart(known, "advance", page_bound=page_bound)
    assert stops == [] and searched == [] and unbound_rows == []
    assert [line["product_id"] for line in obs["lines"]] == ["222"] and set(bound) == {"222"}
    assert json.loads((tmp_path / cart.UNBOUND_RECORD).read_text()) == []
    assert json.loads((tmp_path / cart.PAGE_BOUND_RECORD).read_text()) == page_bound
    checkout["rows"] = [row("Olives", "2")]
    checkout["badge"] = ["2"]
    obs, bound, _, _, unbound_rows = bind.bind_cart(known, "advance", page_bound=page_bound)
    assert stops == [] and obs["lines"] == [] and bound == {}
    assert unbound_rows == [unbound("Olives", "2")]
    assert json.loads((tmp_path / cart.UNBOUND_RECORD).read_text()) == unbound_rows
    assert json.loads((tmp_path / cart.PAGE_BOUND_RECORD).read_text()) == {}


def page(pid: str = "222", quantity: int = 1, price: str = "2.00") -> dict[str, Any]:
    return {
        "product_id": pid,
        "title": "AB Cheese",
        "availability": "available",
        "displayed_price_eur": price,
        "quantity": quantity,
    }


def test_bind_page_evidence_returns_the_unique_quantity_and_price_match() -> None:
    recorded = [unbound("Olives", "3", "6.00"), unbound("Cheese", "2", "4.00")]
    assert cart.bind_page_evidence(page(quantity=2), recorded) == unbound("Cheese", "2", "4.00")
    # The line price may equal the unit price, as cart_bind.match_row accepts.
    assert cart.bind_page_evidence(page(quantity=2), [unbound("Cheese", "2", "2.00")]) == unbound(
        "Cheese", "2", "2.00"
    )
    # The page title is not a precondition: the identity is the page itself.
    assert cart.bind_page_evidence(page(), [unbound("Olives")]) == unbound("Olives")
    assert cart.without_unbound_row(recorded, unbound("Cheese", "2", "4.00")) == [
        unbound("Olives", "3", "6.00")
    ]
    assert cart.page_bound_ids(row("Olives"), {"222": unbound("Olives")}) == ["222"]
    assert cart.page_bound_ids(row("Olives", "2"), {"222": unbound("Olives")}) == []


@pytest.mark.parametrize(
    "recorded,reason",
    [
        ([], "planned_product_unbound_row_absent:222:quantity=1:hits=0"),
        ([unbound("Cheese", "2")], "planned_product_unbound_row_absent:222:quantity=1:hits=0"),
        (
            [unbound("Cheese", "1", "3.00")],
            "planned_product_unbound_row_absent:222:quantity=1:hits=0",
        ),
        (
            [unbound("Cheese"), unbound("Butter")],
            "planned_product_unbound_row_ambiguous:222:quantity=1:hits=2",
        ),
    ],
)
def test_bind_page_evidence_stops_on_absent_or_ambiguous_match(
    recorded: list[dict[str, Any]], reason: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        cart.bind_page_evidence(page(), recorded)
    assert str(excinfo.value) == reason


def test_bind_page_evidence_requires_a_positive_in_cart_quantity() -> None:
    for reading in (page(quantity=0), {**page(), "displayed_price_eur": None}):
        with pytest.raises(ValueError, match="planned_product_page_without_positive_quantity:222"):
            cart.bind_page_evidence(reading, [unbound("Cheese")])


def test_bound_line_drift_is_not_an_unbound_row() -> None:
    # A row for an expected product with the wrong quantity is a bound-line mismatch, not a new
    # unbound row.
    with pytest.raises(ValueError, match="identity_quantity_or_price_mismatch"):
        cart.reconcile(
            {"rows": [row("Cheese", "2")], "badge": ["2"], "empty": 0},
            {"222": 1},
            {"222": reading("AB Cheese")},
        )


def test_duplicate_semantic_identity_is_not_a_binding() -> None:
    with pytest.raises(ValueError, match="candidates=2"):
        cart.reconcile(
            {"rows": [row("Cheese")], "badge": ["2"], "empty": 0},
            {"111": 1, "222": 1},
            {"111": reading("AB Cheese"), "222": reading("AB Cheese")},
        )
