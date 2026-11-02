"""Pure recurring cart reconciliation; no browser, CLI or state-file side effects."""
import re
from decimal import Decimal


def norm(text):
    return " ".join((text or "").split()).casefold()


def candidates(row, readings):
    matches = [pid for pid, reading in readings.items()
               if norm(row.get("name")) and norm(row["name"]) in norm(reading["title"])
               and norm(row.get("brand")) in norm(reading["title"])]
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


def reconcile(cart, expected, readings, protected=()):
    """Return buyable quantities, excluding only freshly confirmed unavailable IDs.

    Unavailable rows have no quantity: never invent one or count them as cart
    credit. Callers must refresh product-page evidence before invoking this.
    """
    empty_messages = cart["empty"]
    if empty_messages and cart["rows"]:
        raise ValueError("checkout_empty_message_with_lines")
    if not cart["rows"] and empty_messages != 1:
        raise ValueError("checkout_missing_empty_evidence")
    remaining = dict(expected)
    unavailable = {pid for pid, r in readings.items() if r["availability"] == "unavailable"}
    if unavailable & set(protected):
        raise ValueError("written_target_unavailable_readback_unverified:" + ",".join(sorted(unavailable & set(protected))))
    for pid in unavailable:
        remaining.pop(pid, None)
    matched = set()
    for index, row in enumerate(cart["rows"], 1):
        if row.get("ambiguous"):
            raise ValueError(f"checkout_row_{index}:" + row.get("reason", "missing_row_controls"))
        ids = candidates(row, readings)
        if row.get("unavailable"):
            ids = [pid for pid in ids if pid in unavailable]
        else:
            parts = row.get("price_parts", [])
            if len(parts) != 3 or parts[0] not in ("€", "~€") or not re.fullmatch(r"\d+", parts[1]) or not re.fullmatch(r"\d{2}", parts[2]):
                raise ValueError(f"checkout_row_{index}:invalid_price_parts")
            price = Decimal(parts[1] + "." + parts[2])
            ids = [pid for pid in ids if pid in remaining
                   and str(remaining[pid]) == row.get("quantity")
                   and price in (Decimal(readings[pid]["displayed_price_eur"]),
                                 Decimal(readings[pid]["displayed_price_eur"]) * remaining[pid])]
        if len(ids) != 1 or ids[0] in matched:
            raise ValueError(f"checkout_row_{index}:identity_quantity_or_price_mismatch:candidates={len(ids)}")
        matched.add(ids[0])
    if matched - unavailable != set(remaining):
        raise ValueError("checkout_missing_buyable_lines:" + ",".join(sorted(set(remaining) - matched)))
    total = sum(remaining.values())
    unavailable_rows = sum(bool(row.get("unavailable")) for row in cart["rows"])
    if cart["badge"] not in ([str(total)], [str(total + unavailable_rows)]) and not (not cart["rows"] and cart["badge"] in ([], ["0"])):
        raise ValueError("checkout_header_badge_inconsistent")
    return remaining
