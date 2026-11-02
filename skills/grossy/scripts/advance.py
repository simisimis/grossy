"""Basket-review advance: identity-bind every checkout line after the owner's manual edits,
then `weekly advance`.

Usage: advance.py
Reports unavailable / removed / re-bound / added lines separately. Read-only in the browser.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cart_bind
import live_common as drill
from live_common import D, stop, write
from slot_gate import require_ready

try:
    require_ready(D)
except ValueError as exc:
    stop(str(exc))

RUN = json.loads((D / "plan-prepare.json").read_text())["run_id"]
READ = {r["product_id"]: r for r in json.loads((D / "recurring-readings.json").read_text())}
KNOWN = {p: {"canonical_url": r["canonical_url"], "title": r["title"]} for p, r in READ.items()}
for f in ("baseline-bound.json",):
    if (D / f).exists():
        for p, r in json.loads((D / f).read_text()).items():
            KNOWN[p] = {"canonical_url": r["canonical_url"], "title": r["title"]}
for f in sorted(drill.BASE.glob("live-*/matching-prepare.json")):
    for x in json.loads(f.read_text())["local_products"]:
        KNOWN.setdefault(
            x["product_id"],
            {
                "canonical_url": x["canonical_url"],
                "title": " ".join(filter(None, [x.get("brand"), x.get("title")])),
            },
        )

obs, bound, unavailable, searched = cart_bind.bind_cart(KNOWN, "advance")
write("advance-cart.json", obs)
write(
    "advance-bound.json",
    {
        p: {k: r[k] for k in ("title", "quantity", "displayed_price_eur", "canonical_url")}
        for p, r in bound.items()
    },
)
res = drill.grossy(
    "weekly", "advance", RUN, str(D / "advance-cart.json"), out="advance-result.json"
)


def is_unavailable_line(title):
    return any(
        cart_bind.norm(u["name"]) in cart_bind.norm(title)
        and cart_bind.norm(u["brand"]) in cart_bind.norm(title)
        for u in unavailable
    )


verified = (
    json.loads((D / "verified-recurring-quantities.json").read_text())
    if (D / "verified-recurring-quantities.json").exists()
    else {}
)
gone = [p for p in verified if p not in bound]
print(
    "advance:",
    res.get("status"),
    res.get("lifecycle"),
    "| lines",
    len(obs["lines"]),
    "| items",
    sum(line["quantity"] for line in obs["lines"]),
)
print(
    "unavailable in cart (excluded, not removed):",
    [READ[p]["title"] for p in gone if p in READ and is_unavailable_line(READ[p]["title"])]
    or [(u["brand"], u["name"]) for u in unavailable],
)
print(
    "removed by owner:",
    [
        READ.get(p, {}).get("title", p)
        for p in gone
        if not (p in READ and is_unavailable_line(READ[p]["title"]))
    ],
)
print(
    "recurring lines bound via search (not owner additions):",
    [a["title"] for a in searched if a["product_id"] in verified],
)
print(
    "added by owner:",
    [
        (a["title"], a["quantity"], a["displayed_price_eur"])
        for a in searched
        if a["product_id"] not in verified
    ],
)
changed = [
    (p, verified[p], bound[p]["quantity"])
    for p in verified
    if p in bound and bound[p]["quantity"] != verified[p]
]
if changed:
    print("quantity changed by owner:", changed)
write("verified-cart-quantities.json", {p: r["quantity"] for p, r in bound.items()})
