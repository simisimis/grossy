"""History sync: discover-prepare, reservation-history enumeration newest first, listed-orders v2
proposal, sync-dry-run, scan-prepare.

Usage: history_sync.py [--dry-run] [--full]
The walk collects order-details anchors until two consecutive settled bottom passes add nothing.
By default it stops at the first already-imported order; --full skips imported orders and walks
the whole list. The stall rule (no new content and no bottom reached for 30 s) is the only safety
stop and ends the run with an explicit partial result. "Already imported" is asked through the
CLI sync-dry-run phase, never SQL. Every browser command uses the strict pinned original tab:
authentication loss, CAPTCHA, route drift or target loss stops with no retry and no tab adoption.
List identity is at least one rendered order entry with its order-details anchor; no verified
empty-list state exists yet, so a list without entries stops as missing identity (fail closed).
Outputs in the work dir: discover-prepare.json, listed-orders.json, enumeration.json, dry-run.json,
scan-prepare.json. Private: never print source IDs/URLs.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_common as drill
from live_common import D, stop, write
from slot_gate import require_ready

if set(sys.argv[1:]) - {"--dry-run", "--full"}:
    stop("usage: live.sh history [--dry-run] [--full]")
try:
    require_ready(D, history=True)
except ValueError as exc:
    stop(str(exc))

DRY = "--dry-run" in sys.argv
FULL = "--full" in sys.argv
LIST_URL = "https://www.ab.gr/en/my-account/reservation-history"
LIST_PATH = "/en/my-account/reservation-history"
ORDER_URL = "https://www.ab.gr/my-account/order?id="
ROUTE_JS = 'if(location.pathname!=="' + LIST_PATH + '")throw Error("unexpected_route");'
# Rendered entries in DOM order (newest first): the order-details anchor href and the entry's
# date text, the only date source. Order pages are never read for dates.
ENTRIES_JS = (
    ROUTE_JS + 'return [...document.querySelectorAll("li[data-testid=my-account-orders-item]")].'
    'filter(v).map(e=>{const a=[...e.querySelectorAll("a[data-testid=order-details-button]")];'
    'const d=[...e.querySelectorAll("p[data-testid=my-account-orders-item-date]")];'
    'return {hrefs:a.map(x=>x.getAttribute("href")),dates:d.map(x=>x.innerText.trim())};});'
)
HREF_RE = re.compile(r"^(?:https://www\.ab\.gr)?/my-account/order\?id=(\d+)$")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


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
stop("per_order_scan_not_available: unknown orders listed in scan-prepare.json")
