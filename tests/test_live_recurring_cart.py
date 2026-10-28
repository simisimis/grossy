"""Offline regressions for the live recurring checkout boundary (no browser/profile)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".pi/skills/grossy/scripts"
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
    known = {"111": {"title": "AB Salad", "canonical_url": "https://www.ab.gr/p/111"}}
    reads: list[tuple[str, str]] = []

    def read(pid: str, url: str) -> None:
        reads.append((pid, url))
        readings[pid] = reading("AB Salad", "unavailable")

    cart.bind_unavailable_rows(unavailable_cart(), readings, known, read)
    cart.bind_unavailable_rows(unavailable_cart(), readings, known, read)
    assert reads == [("111", "https://www.ab.gr/p/111")]


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


def test_duplicate_semantic_identity_is_not_a_binding() -> None:
    with pytest.raises(ValueError, match="candidates=2"):
        cart.reconcile(
            {"rows": [row("Cheese")], "badge": ["2"], "empty": 0},
            {"111": 1, "222": 1},
            {"111": reading("AB Cheese"), "222": reading("AB Cheese")},
        )
