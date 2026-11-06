"""Pure recurring cart reconciliation; no browser, CLI or state-file side effects."""

import re
from decimal import Decimal

# Work-directory record of the checkout rows no product page binds: name, brand, quantity and
# price as read. Written by cart_bind.bind_cart; re-recorded by advance.py at the basket-review
# barrier, the only place owner edits are accepted.
UNBOUND_RECORD = "unbound-rows.json"
# Work-directory record of the unbound rows a planned product page proved to be that product:
# pid -> row as recorded. Every later checkout read binds the row through this record, so the
# page title is never needed again.
PAGE_BOUND_RECORD = "page-bound-rows.json"


def norm(text):
    return " ".join((text or "").split()).casefold()


def unbound_row(row):
    """Identity-free checkout row fields as read; ``price`` is the rendered line price."""
    parts = row.get("price_parts", [])
    return {
        "name": row.get("name"),
        "brand": row.get("brand"),
        "quantity": row.get("quantity"),
        "price": parts[1] + "." + parts[2] if len(parts) == 3 else None,
    }


def unbound_key(row):
    return tuple(str(row.get(k)) for k in ("name", "brand", "quantity", "price"))


def bind_page_evidence(reading, unbound):
    """Return the one recorded unbound row that a planned product page proves to be that product.

    ``reading`` is the product-page read (product_id, positive in-cart quantity,
    displayed_price_eur). The row must carry the page quantity and a line price equal
    to the unit price or unit price times quantity, as ``cart_bind.match_row`` compares;
    the page title plays no part. Raises a coded ValueError when zero or more than one
    row matches; callers stop before intent, action-check and any write.
    """
    pid = reading["product_id"]
    quantity = reading.get("quantity")
    if not isinstance(quantity, int) or quantity <= 0 or not reading.get("displayed_price_eur"):
        raise ValueError("planned_product_page_without_positive_quantity:" + pid)
    unit = Decimal(reading["displayed_price_eur"])
    hits = [
        row
        for row in unbound
        if str(row.get("quantity")) == str(quantity)
        and row.get("price") is not None
        and Decimal(row["price"]) in (unit, unit * quantity)
    ]
    if len(hits) != 1:
        outcome = "ambiguous" if hits else "absent"
        raise ValueError(
            f"planned_product_unbound_row_{outcome}:{pid}:quantity={quantity}:hits={len(hits)}"
        )
    return hits[0]


def without_unbound_row(unbound, row):
    """The recorded list minus the page-bound row; other rows stay exactly as recorded."""
    return [other for other in unbound if unbound_key(other) != unbound_key(row)]


def page_bound_ids(row, page_bound):
    """Products whose page-bound record equals this checkout row as read."""
    key = unbound_key(unbound_row(row))
    return [pid for pid, bound_row in (page_bound or {}).items() if unbound_key(bound_row) == key]


def candidates(row, readings):
    matches = [
        pid
        for pid, reading in readings.items()
        if norm(row.get("name"))
        and norm(row["name"]) in norm(reading["title"])
        and norm(row.get("brand")) in norm(reading["title"])
    ]
    # Prefer the full rendered name+brand over an incidental substring in a
    # different alternative's title; still reject duplicate exact identities.
    full = norm(" ".join(filter(None, (row.get("brand"), row.get("name")))))
    exact = [pid for pid in matches if norm(readings[pid]["title"].replace("|", " ")) == full]
    return exact or matches


def bind_unavailable_rows(cart, readings, known, read):
    """Confirm every unavailable checkout row on its identity-bound product page.

    ``readings`` are this session's fresh product reads (pid -> reading); ``known``
    holds local pid -> {title, canonical_url} evidence for products not yet read.
    ``read(pid, canonical_url)`` performs the product-page read and must store the
    result in ``readings``. Raises ValueError on ambiguity or disagreement.
    """
    for row in cart["rows"]:
        if not row.get("unavailable"):
            continue
        ids = candidates(row, readings) or candidates(row, known)
        if len(ids) != 1:
            raise ValueError("unavailable_row_identity_ambiguous:candidates=" + str(len(ids)))
        pid = ids[0]
        if readings.get(pid, {}).get("availability") != "unavailable":
            read(pid, (known.get(pid) or readings.get(pid) or {}).get("canonical_url"))
        if readings.get(pid, {}).get("availability") != "unavailable":
            raise ValueError("checkout_product_availability_disagrees:" + pid)


def reconcile(cart, expected, readings, protected=(), unbound=(), page_bound=None):
    """Return buyable quantities, excluding only freshly confirmed unavailable IDs.

    Unavailable rows have no quantity: never invent one or count them as cart
    credit. Callers must refresh product-page evidence before invoking this.
    A buyable row equal to a recorded ``unbound`` row is unbound as recorded, even when
    its name happens to occur in an expected product's title. A buyable row equal to a
    ``page_bound`` record binds to that product without a title match; the quantity and
    price checks still apply. Buyable rows that match no expected product are the other
    currently unbound rows; sorted, all of them must equal the recorded list exactly.
    """
    empty_messages = cart["empty"]
    if empty_messages and cart["rows"]:
        raise ValueError("checkout_empty_message_with_lines")
    if not cart["rows"] and empty_messages != 1:
        raise ValueError("checkout_missing_empty_evidence")
    remaining = dict(expected)
    unavailable = {pid for pid, r in readings.items() if r["availability"] == "unavailable"}
    if unavailable & set(protected):
        raise ValueError(
            "written_target_unavailable_readback_unverified:"
            + ",".join(sorted(unavailable & set(protected)))
        )
    for pid in unavailable:
        remaining.pop(pid, None)
    matched = set()
    recorded = sorted(unbound_key(row) for row in unbound)
    current_unbound = []
    for index, row in enumerate(cart["rows"], 1):
        if row.get("ambiguous"):
            raise ValueError(f"checkout_row_{index}:" + row.get("reason", "missing_row_controls"))
        ids = candidates(row, readings)
        if row.get("unavailable"):
            ids = [pid for pid in ids if pid in unavailable]
        else:
            parts = row.get("price_parts", [])
            if (
                len(parts) != 3
                or parts[0] not in ("€", "~€")
                or not re.fullmatch(r"\d+", parts[1])
                or not re.fullmatch(r"\d{2}", parts[2])
            ):
                raise ValueError(f"checkout_row_{index}:invalid_price_parts")
            if not re.fullmatch(r"\d+", str(row.get("quantity"))):
                raise ValueError(f"checkout_row_{index}:invalid_quantity")
            ids = list(dict.fromkeys([*ids, *page_bound_ids(row, page_bound)]))
            if unbound_key(unbound_row(row)) in recorded or not any(
                pid in remaining for pid in ids
            ):
                current_unbound.append(unbound_row(row))
                continue
            price = Decimal(parts[1] + "." + parts[2])
            ids = [
                pid
                for pid in ids
                if pid in remaining
                and str(remaining[pid]) == row.get("quantity")
                and price
                in (
                    Decimal(readings[pid]["displayed_price_eur"]),
                    Decimal(readings[pid]["displayed_price_eur"]) * remaining[pid],
                )
            ]
        if len(ids) != 1 or ids[0] in matched:
            raise ValueError(
                f"checkout_row_{index}:identity_quantity_or_price_mismatch:candidates={len(ids)}"
            )
        matched.add(ids[0])
    if matched - unavailable != set(remaining):
        raise ValueError(
            "checkout_missing_buyable_lines:" + ",".join(sorted(set(remaining) - matched))
        )
    current = sorted(unbound_key(row) for row in current_unbound)
    if recorded != current:
        raise ValueError(
            f"checkout_unbound_rows_changed:recorded={len(recorded)}:current={len(current)}"
        )
    total = sum(remaining.values()) + sum(int(row["quantity"]) for row in current_unbound)
    unavailable_rows = sum(bool(row.get("unavailable")) for row in cart["rows"])
    if cart["badge"] not in ([str(total)], [str(total + unavailable_rows)]) and not (
        not cart["rows"] and cart["badge"] in ([], ["0"])
    ):
        raise ValueError("checkout_header_badge_inconsistent")
    return remaining
