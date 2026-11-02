"""History sync: discover-prepare, rendered six-control extraction, listed proposal, sync-dry-run,
scan-prepare, then (unless --dry-run) scan exactly the unknown orders oldest first, scan-validate
and sync-apply.

Usage: history_sync.py [--dry-run]
Outputs in the work dir: discover-prepare.json, listed-orders.json, dry-run.json, scan-prepare.json,
order-scan.json, scan-validate.json, sync-apply.json. Private: never print source IDs/URLs.
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_common as drill
from live_common import D, stop, write
from slot_gate import require_ready

try:
    require_ready(D, history=True)
except ValueError as exc:
    stop(str(exc))

DRY = "--dry-run" in sys.argv
ORDERS_URL = "https://www.ab.gr/customerhub/quick-shop/orders"


def open_dropdown(tag):
    drill.browser(["open", ORDERS_URL], tag)
    drill.wait(
        'if(location.pathname!=="/customerhub/quick-shop/orders")throw Error("unexpected_route");co'
        'nst h=[...document.querySelectorAll("[role=combobox] h2")].filter(v);return document.read'
        'yState==="complete"&&h.length===1;',
        tag=tag,
    )
    st = drill.evaluate(
        'const c=[...document.querySelectorAll("[role=combobox]")].filter(e=>v(e)&&e.querySelector('
        '"h2"));return {n:c.length,expanded:c.length===1?c[0].getAttribute("aria-expanded"):null};',
        tag,
    )
    if st["n"] != 1:
        stop("combobox_ambiguous")
    if st["expanded"] != "true":
        drill.browser(["click", "[role=combobox] h2"], tag)
    drill.wait(
        'return [...document.querySelectorAll("button[role=option]")].filter(v).length>=2;', tag=tag
    )


def collect(extract, seen):
    """Add every rendered card of the selected order to ``seen``; return the number of new ids."""
    new = 0
    for c in drill.evaluate(extract, "scan"):
        if len(c["urls"]) != 1 or not c["name"]:
            stop("card_without_stable_identity")
        pid = re.search(r"/p/(\d+)$", c["urls"][0]).group(1)
        if pid not in seen:
            seen[pid] = {
                "product_id": pid,
                "canonical_url": c["urls"][0],
                "title": c["name"],
                "brand": c["brand"],
            }
            new += 1
        elif seen[pid]["canonical_url"] != c["urls"][0]:
            stop("inconsistent_card_identity")
    return new


r = drill.grossy("history", "discover-prepare", out="discover-prepare.json")
print("discover-prepare:", r["status"])
open_dropdown("hist")
label = drill.evaluate(
    'return [...document.querySelectorAll("[role=combobox] h2")].filter(v)[0].innerText.trim();',
    "hist",
)
if not re.fullmatch(r"All orders", label, re.I):
    stop("combobox_label_unexpected")
opts = drill.evaluate(
    'return [...document.querySelectorAll("button[role=option]")].filter(v).map(b=>({id:b.id,label:'
    'b.innerText.trim().split("\\n")[0]}));',
    "hist",
)
orders = []
for o in opts:
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", o["label"])
    if re.fullmatch(r"\d+", o["id"] or "") and m:
        d, mo, y = m.groups()
        orders.append(
            {
                "source_order_id": o["id"],
                "displayed_label": f"{d}/{mo}/{y}",
                "order_date": f"{y}-{mo}-{d}",
                "navigation_url": ORDERS_URL + "?id=" + o["id"],
            }
        )
print(
    "dropdown options:",
    len(opts),
    "dated:",
    len(orders),
    "dates:",
    [o["displayed_label"] for o in orders],
)
if len(orders) != 6:
    stop("dated_option_count")
write(
    "listed-orders.json",
    {
        "schema_name": "grossy.history.listed-orders.v1",
        "schema_version": 1,
        "generated_at": drill.now(),
        "orders": orders,
    },
)
lo = str(D / "listed-orders.json")
dr = drill.grossy("history", "sync-dry-run", "--listed-orders", lo, out="dry-run.json")
print("sync-dry-run: unknown", dr["unknown_order_count"])
if DRY:
    print("dry-run requested; stopping before scan-prepare")
    sys.exit(0)
sp = drill.grossy("history", "scan-prepare", "--listed-orders", lo, out="scan-prepare.json")
print("scan-prepare:", sp["status"], "unknown", sp.get("unknown_order_count"))
if not sp.get("unknown_order_count"):
    print("history up to date (no-op summary written)")
    sys.exit(0)

scanned = []
for o in sp["unknown_orders"]:
    sid = o["source_order_id"]
    open_dropdown("scan")
    n = drill.evaluate(
        'return [...document.querySelectorAll("button[role=option]")].filter(v).filter(b=>b.id==="'
        + sid
        + '").length;',
        "scan",
    )
    if n != 1:
        stop("dated_option_not_unique")
    lbl = drill.evaluate(
        'return document.getElementById("' + sid + '").innerText.trim().split("\\n")[0];', "scan"
    )
    if not lbl.startswith(o["displayed_label"]):
        stop("dated_option_label_mismatch")
    drill.browser(["click", 'button[role=option][id="' + sid + '"]'], "scan")
    drill.wait(
        'if(location.pathname!=="/customerhub/quick-shop/orders")throw Error("unexpected_route");co'
        'nst p=new URLSearchParams(location.search);const h=[...document.querySelectorAll("[role=c'
        'ombobox] h2")].filter(v);'
        'return p.get("id")==="'
        + sid
        + '"&&[...p.keys()].length===1&&h.length===1&&h[0].innerText.trim().startsWith("'
        + o["displayed_label"]
        + '")&&document.readyState==="complete";',
        tag="scan",
    )
    drill.wait(
        'return [...document.querySelectorAll("[data-testid=product-block]")].filter(v).length>0;',
        60,
        tag="scan",
    )
    seen = {}
    EXTRACT = (
        'const p=new URLSearchParams(location.search);if(p.get("id")!=="'
        + sid
        + '")throw Error("selection_lost");'
        'return [...document.querySelectorAll("[data-testid=product-block]")].filter(v).map(c=>{con'
        "st a=[...c.querySelectorAll(\"a[href*='/p/']\")].map(x=>x.href.split(/[?#]/)[0]).filter(h"
        "=>/\\/p\\/\\d+$/.test(h));"
        'return {urls:[...new Set(a)],name:c.querySelector("[data-testid=product-name]")?.innerText'
        '.trim()||null,brand:(c.querySelector("[data-testid=product-brand]")?.innerText||"").trim('
        ")||null};});"
    )
    collect(EXTRACT, seen)
    stable = passes = 0
    while stable < 2 and passes < 40:
        passes += 1
        for _ in range(30):
            at_bottom = drill.evaluate(
                "window.scrollBy(0,600);return (window.innerHeight+window.scrollY)>=document.body.s"
                "crollHeight-5;",
                "scan",
            )
            time.sleep(0.4)
            collect(EXTRACT, seen)
            if at_bottom:
                break
        time.sleep(2.0)
        stable = stable + 1 if collect(EXTRACT, seen) == 0 else 0
    if stable < 2:
        stop("list_not_stabilized")
    drill.evaluate(
        'const p=new URLSearchParams(location.search);if(p.get("id")!=="'
        + sid
        + '")throw Error("selection_lost");return true;',
        "scan",
    )
    prods = [
        {
            **s,
            "category_heading": None,
            "displayed_price_eur": None,
            "package_amount": None,
            "package_count": None,
            "package_display": None,
            "package_unit": None,
        }
        for s in seen.values()
    ]
    scanned.append(
        {
            "source_order_id": sid,
            "displayed_label": o["displayed_label"],
            "order_date": o["order_date"],
            "navigation_url": o["navigation_url"],
            "displayed_product_count": None,
            "products": prods,
        }
    )
    print(
        f"order {o['displayed_label']}: {len(prods)} unique products after {passes} bottom passes",
        flush=True,
    )
write(
    "order-scan.json",
    {
        "schema_name": "grossy.history.order-scan.v1",
        "schema_version": 1,
        "generated_at": drill.now(),
        "orders": scanned,
    },
)
r = drill.grossy("history", "scan-validate", str(D / "order-scan.json"), out="scan-validate.json")
print("scan-validate:", r.get("status"), "warnings", r.get("warnings"))
a = drill.grossy(
    "history",
    "sync-apply",
    "--listed-orders",
    lo,
    str(D / "order-scan.json"),
    out="sync-apply.json",
)
print(
    "sync-apply:",
    a.get("status"),
    {
        k: a.get(k)
        for k in (
            "imported_orders",
            "imported_product_presence",
            "new_products",
            "changed_products",
            "displayed_count_discrepancies",
        )
    },
)
dr = drill.grossy("history", "sync-dry-run", "--listed-orders", lo, out="dry-run-after.json")
print("dry-run after apply: unknown", dr["unknown_order_count"])
