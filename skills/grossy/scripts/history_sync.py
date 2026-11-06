"""History sync: discover-prepare, reservation-history enumeration newest first, listed-orders v2
proposal, sync-dry-run, scan-prepare, then one scan-validate and sync-apply per unknown order.

Usage: history_sync.py [--dry-run] [--full]
The walk collects order-details anchors until two consecutive settled bottom passes add nothing.
By default it stops at the first already-imported order; --full skips imported orders and walks
the whole list. The stall rule (no new content and no bottom reached for 30 s) is the only safety
stop and ends the run with an explicit partial result. "Already imported" is asked through the
CLI sync-dry-run phase, never SQL. Every browser command uses the strict pinned original tab:
authentication loss, CAPTCHA, route drift or target loss stops with no retry and no tab adoption.
List identity is at least one rendered order entry with its order-details anchor; no verified
empty-list state exists yet, so a list without entries stops as missing identity (fail closed).
Each unknown order, newest first, is opened on its order-details page, scrolled to two settled
bottom passes (progress measured by visible tile count only), read once, built into its own
order-scan proposal from its identifiable tiles, validated and applied as one sync-apply
transaction (with recurrence learning) before the next order is opened. Tiles without a canonical
/p/<id> link (for example substituted products with a generic URL) are skipped and counted; an
order with zero identifiable tiles stops the run. Imported IDs are the only
checkpoint: an interrupted run leaves exactly the orders applied so far, and a later run neither
resumes nor replays. Order-page dates are never read; the order date comes from the listed entry.
Outputs in the work dir: discover-prepare.json, listed-orders.json, enumeration.json, dry-run.json,
scan-prepare.json, order-scan-NNN.json, scan-validate-NNN.json, sync-apply-NNN.json (NNN is the
scan sequence number, not the order ID) and scan-apply.json. Private: never print source IDs/URLs.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_common as drill
from live_common import STORE, D, stop, write
from slot_gate import require_ready

if set(sys.argv[1:]) - {"--dry-run", "--full"}:
    stop("usage: live.sh history [--dry-run] [--full]")
try:
    require_ready(D, history=True)
except ValueError as exc:
    stop(str(exc))

DRY = "--dry-run" in sys.argv
FULL = "--full" in sys.argv
LIST_PATH = "/en/my-account/reservation-history"
LIST_URL = STORE + LIST_PATH
ORDER_PATH = "/my-account/order"
ORDER_URL = STORE + ORDER_PATH + "?id="
ROUTE_JS = 'if(location.pathname!=="' + LIST_PATH + '")throw Error("unexpected_route");'
# Rendered entries in DOM order (newest first): the order-details anchor href and the entry's
# date text, the only date source. Order pages are never read for dates.
ENTRIES_JS = (
    ROUTE_JS + 'return [...document.querySelectorAll("li[data-testid=my-account-orders-item]")].'
    'filter(v).map(e=>{const a=[...e.querySelectorAll("a[data-testid=order-details-button]")];'
    'const d=[...e.querySelectorAll("p[data-testid=my-account-orders-item-date]")];'
    'return {hrefs:a.map(x=>x.getAttribute("href")),dates:d.map(x=>x.innerText.trim())};});'
)
# Rendered hrefs are either relative (store paths) or absolute on the store origin.
HREF_RE = re.compile(r"^(?:" + re.escape(STORE) + r")?/my-account/order\?id=(\d+)$")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
# Canonical product link rendered on an order card (task 01: /en/eshop/<category-path>/p/<id>).
PRODUCT_HREF_RE = re.compile(
    r"^(?:" + re.escape(STORE) + r")?/(?:(?:en|el)/)?eshop/[^?#\s]+/p/(\d+)$"
)


def order_route_js(sid):
    """Guard: the pinned tab shows the order-details page of exactly this order."""
    return (
        'if(location.pathname!=="' + ORDER_PATH + '"||new URLSearchParams(location.search).get('
        '"id")!=="' + sid + '")throw Error("unexpected_route");'
    )


def heading_js(sid):
    """Distinct digit strings of the details headings (shape ``Order NNNNNNNNN``) rendered
    inside the order-details container; order-page identity beyond the route ID."""
    return (
        order_route_js(sid)
        + 'return [...new Set([...document.querySelectorAll("[data-testid=my-account-order-detail'
        's-container] *")].map(e=>(e.innerText||"").trim()).filter(t=>/^Order\\s+\\d+$/.test(t)).'
        'map(t=>t.replace(/\\D/g,"")))];'
    )


# Visible non-carousel product tiles on the order page.
TILES_JS = (
    '[...document.querySelectorAll("[data-testid=product-block]")].filter(e=>v(e)&&!e.closest("'
    '[data-testid=product-carousel-container]"))'
)


def tile_count_js(sid):
    """Walk progress measure: the number of visible tiles. No link or name rule applies mid-walk
    because a tile that is still loading may not carry its link yet."""
    return order_route_js(sid) + "return " + TILES_JS + ".length;"


def cards_js(sid):
    """Settled read of every tile: every /p/ link href, product-name and product-brand text.
    Dates on the order page are never read (month-first rendering)."""
    return (
        order_route_js(sid)
        + "return "
        + TILES_JS
        + '.map(c=>({hrefs:[...c.querySelectorAll("a[href*=\'/p/\']")].map(a=>a.getAttribute("hr'
        'ef").split(/[?#]/)[0]),names:[...c.querySelectorAll("[data-testid=product-name]")].map(e'
        '=>e.innerText.trim()),brands:[...c.querySelectorAll("[data-testid=product-brand]")].map('
        "e=>e.innerText.trim())}));"
    )


def listed_proposal(orders):
    return {
        "schema_name": "grossy.history.listed-orders.v2",
        "schema_version": 2,
        "generated_at": drill.now(),
        "orders": list(orders),
    }


def imported_ids(orders):
    """Which of ``orders`` are already imported, asked through the CLI (never SQL)."""

    def unknown_count(subset):
        write("imported-check.json", listed_proposal(subset))
        r = drill.grossy(
            "history", "sync-dry-run", "--listed-orders", str(D / "imported-check.json")
        )
        return r["unknown_order_count"]

    if not orders or unknown_count(orders) == len(orders):
        return set()
    return {o["source_order_id"] for o in orders if unknown_count([o]) == 0}


orders = {}  # source_order_id -> listed order, in walk (newest-first) order
known = []  # already-imported IDs in the order they were met
stop_at_known = [False]


def rendered_entries():
    entries = drill.evaluate(ENTRIES_JS, "hist", recover=False)
    if not entries:
        stop("list_identity_missing")
    return entries


def collect():
    """Add every rendered entry to ``orders``; return the number of new orders."""
    batch = []
    for e in rendered_entries():
        href = HREF_RE.match(e["hrefs"][0]) if len(e["hrefs"]) == 1 else None
        date = DATE_RE.match(e["dates"][0]) if len(e["dates"]) == 1 else None
        if not href or not date:
            stop("order_entry_malformed")
        sid = href.group(1)
        day, month, year = date.groups()
        order = {
            "source_order_id": sid,
            "order_date": f"{year}-{month}-{day}",
            "navigation_url": ORDER_URL + sid,
        }
        if sid not in orders:
            orders[sid] = order
            batch.append(order)
        elif orders[sid] != order:
            stop("inconsistent_order_entry")
    imported = imported_ids(batch)
    for i, o in enumerate(batch):
        if o["source_order_id"] in imported:
            known.append(o["source_order_id"])
            if not FULL:
                # Default walk ends at the first imported order; drop the older entries that
                # happened to render in the same pass.
                stop_at_known[0] = True
                for older in batch[i + 1 :]:
                    del orders[older["source_order_id"]]
                break
    return len(batch)


r = drill.grossy("history", "discover-prepare", out="discover-prepare.json")
print("discover-prepare:", r["status"], "imported", r.get("imported_order_count"))
drill.browser(["open", LIST_URL], "hist", recover=False)
drill.wait(ROUTE_JS + 'return document.readyState==="complete";', tag="hist", recover=False)
drill.settle_page("hist", recover=False, timeout=drill.STALL_SECONDS)
rendered_entries()  # positive list identity before the walk starts
walk = drill.walk_to_settled_bottom(collect, "hist", halt=lambda: stop_at_known[0])
drill.evaluate(ROUTE_JS + "return true;", "hist", recover=False)
if walk["ended"] == "halted":
    walk["ended"] = "stopped_at_known_order"
listed = list(orders.values())
write("listed-orders.json", listed_proposal(listed))
result = {
    "mode": "full" if FULL else "default",
    "ended": walk["ended"],
    "bottom_passes": walk["passes"],
    "listed_order_count": len(listed),
    "known_order_count": len(known),
    "last_known_order_id": known[-1] if known else None,
    # Enumeration imports nothing; the per-order apply step records the last imported ID here.
    "last_imported_order_id": None,
}
print(
    f"enumeration: {len(listed)} listed, {len(known)} already imported, "
    f"{walk['passes']} bottom passes, ended {walk['ended']}",
    flush=True,
)
if walk["ended"] == "stalled":
    write("enumeration.json", {**result, "status": "partial", "reason": "stall_rule"})
    stop("enumeration_stalled: partial result in enumeration.json")
write("enumeration.json", {**result, "status": "complete", "reason": walk["ended"]})
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

# Per-order scan and apply, newest first (walk order). Each order is its own sync-apply
# transaction; imported IDs are the only checkpoint, so a stop here leaves exactly the orders
# applied so far and names the last imported ID in scan-apply.json.
unknown_ids = {o["source_order_id"] for o in sp["unknown_orders"]}
pending = [o for o in listed if o["source_order_id"] in unknown_ids]
if len(pending) != len(unknown_ids):
    stop("scan_prepare_unknown_orders_not_listed")
imported = []  # applied order IDs in apply order (newest first)
skipped_total = [0]  # tiles without a canonical link over every order read so far


def scan_result(status, reason, seq=None, skipped=0):
    """Progress record; ``seq`` and ``skipped`` (tiles skipped on that order) describe the
    current order while one is open."""
    record = {
        "status": status,
        "reason": reason,
        "mode": result["mode"],
        "unknown_order_count": len(pending),
        "applied_order_count": len(imported),
        "last_imported_order_id": imported[-1] if imported else None,
        "skipped_tile_total": skipped_total[0],
    }
    if seq is not None:
        record.update(scan_sequence=seq, skipped_tile_count=skipped)
    write("scan-apply.json", record)


def scan_order(order, seq, skipped_now):
    """Open the order page, walk it to two settled bottom passes measured by visible tile count,
    then read every tile once. Returns (walk, products, skipped_tile_count). A tile without
    exactly one canonical /p/<id> link carries no identity and is skipped and counted; a linked
    tile without exactly one nonempty name, or duplicate IDs with differing data, stop the run
    with nothing applied for this order. ``skipped_now`` (one-element list) receives this
    order's skipped count as soon as the settled read completes, so a browser stop after the
    read still reports it."""
    sid, tag = order["source_order_id"], f"order-{seq:03d}"
    seen = [0]

    def collect():
        count = drill.evaluate(tile_count_js(sid), tag, recover=False)
        new = max(0, count - seen[0])
        seen[0] = max(seen[0], count)
        return new

    def read_tiles():
        products = {}  # product_id -> scanned product, first-seen order
        skipped = 0
        for tile in drill.evaluate(cards_js(sid), tag, recover=False):
            links = {m.group(1): m for m in map(PRODUCT_HREF_RE.match, tile["hrefs"]) if m}
            if len(links) != 1:
                skipped += 1
                continue
            if len(tile["names"]) != 1 or not tile["names"][0]:
                skipped_total[0] += skipped
                scan_result("partial", "product_card_malformed", seq, skipped)
                stop(f"product_card_malformed: order {seq} of {len(pending)}")
            pid, m = next(iter(links.items()))
            href = m.group(0)
            product = {
                "product_id": pid,
                "canonical_url": href if href.startswith("https://") else STORE + href,
                "title": tile["names"][0],
                "brand": tile["brands"][0]
                if len(tile["brands"]) == 1 and tile["brands"][0]
                else None,
            }
            if pid not in products:
                products[pid] = product
            elif products[pid] != product:
                skipped_total[0] += skipped
                scan_result("partial", "inconsistent_product_card", seq, skipped)
                stop(f"inconsistent_product_card: order {seq} of {len(pending)}")
        skipped_total[0] += skipped
        skipped_now[0] = skipped
        return list(products.values()), skipped

    drill.browser(["open", order["navigation_url"]], tag, recover=False)
    drill.wait(
        order_route_js(sid) + 'return document.readyState==="complete"&&document.querySelectorAll("'
        '[data-testid=my-account-order-details-container]").length===1;',
        tag=tag,
        recover=False,
    )
    headings = drill.evaluate(heading_js(sid), tag, recover=False)
    if not headings:
        scan_result("partial", "order_heading_missing", seq)
        stop(f"order_heading_missing: order {seq} of {len(pending)}")
    if headings != [sid]:
        scan_result("partial", "order_heading_mismatch", seq)
        stop(f"order_heading_mismatch: order {seq} of {len(pending)}")
    drill.settle_page(tag, recover=False, timeout=drill.STALL_SECONDS)
    walk = drill.walk_to_settled_bottom(collect, tag)
    if walk["ended"] == "stalled":
        return walk, [], 0  # nothing is read from a page that never settled
    products, skipped = read_tiles()
    drill.evaluate(order_route_js(sid) + "return true;", tag, recover=False)
    return walk, products, skipped


def scan_order_recording_browser_stops(order, seq):
    """Run ``scan_order``; a browser-level stop (authentication loss, CAPTCHA, route drift,
    target loss) mid-order is first recorded in scan-apply.json, then stops as before."""
    live_stop = drill.stop
    skipped_now = [0]  # this order's skipped tiles once read; 0 before the settled read

    def recording_stop(reason):
        drill.stop = live_stop
        scan_result("stopped", reason, seq, skipped_now[0])
        live_stop(reason)

    drill.stop = recording_stop
    try:
        return scan_order(order, seq, skipped_now)
    finally:
        drill.stop = live_stop


for seq, order in enumerate(pending, 1):
    walk, products, skipped = scan_order_recording_browser_stops(order, seq)
    if walk["ended"] == "stalled":
        scan_result("partial", "stall_rule", seq)
        stop(
            f"order_scan_stalled: order {seq} of {len(pending)}, partial result in scan-apply.json"
        )
    if not products:
        # Zero identifiable tiles on a settled page: page markup drift suspected, fail closed.
        scan_result("partial", "no_identifiable_products", seq, skipped)
        stop(
            f"no_identifiable_products: order {seq} of {len(pending)}, partial result in "
            "scan-apply.json"
        )
    year, month, day = order["order_date"].split("-")
    name = f"order-scan-{seq:03d}.json"
    write(
        name,
        {
            "schema_name": "grossy.history.order-scan.v1",
            "schema_version": 1,
            "generated_at": drill.now(),
            "orders": [
                {
                    "source_order_id": order["source_order_id"],
                    "displayed_label": f"{day}/{month}/{year}",
                    "order_date": order["order_date"],
                    "navigation_url": order["navigation_url"],
                    "products": products,
                }
            ],
        },
    )
    path = str(D / name)
    drill.grossy("history", "scan-validate", path, out=f"scan-validate-{seq:03d}.json")
    ap = drill.grossy(
        "history", "sync-apply", path, "--listed-orders", lo, out=f"sync-apply-{seq:03d}.json"
    )
    if ap["status"] != "applied" or ap["imported_orders"] != 1:
        scan_result("partial", "apply_not_confirmed", seq, skipped)
        stop(f"apply_not_confirmed: order {seq} of {len(pending)}")
    imported.append(order["source_order_id"])
    scan_result("in_progress", "applied", seq, skipped)
    print(
        f"order {seq}/{len(pending)}: {len(products)} products, {skipped} skipped "
        f"tile{'' if skipped == 1 else 's'}, {walk['passes']} bottom passes, "
        f"applied ({ap['new_products']} new products)",
        flush=True,
    )
scan_result("complete", "all_unknown_orders_applied")
print(f"history sync complete: {len(imported)} orders applied")
