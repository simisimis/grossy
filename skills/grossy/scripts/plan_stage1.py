"""Weekly Stage 1 preparation: plan-prepare, identity-bound whole-cart baseline, recurring product
facts, first preflight.

Usage: plan_stage1.py [--skip-history-sync]
Requires listed-orders.json from history_sync.py unless --skip-history-sync. Idempotent: re-running
after a stop reuses plan-prepare.json / baseline-result.json. Read-only in the browser.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decimal import Decimal

import cart_bind
import live_common as drill
from live_common import D, stop, write
from slot_gate import require_ready

try:
    require_ready(D)
except ValueError as exc:
    stop(str(exc))

# Known product identities (URL + title) from every earlier live work directory: used to bind cart
# lines and read facts.
URLS, KNOWN = {}, {}


def remember(pid, url, title=None):
    URLS[pid] = url
    if title:
        KNOWN[pid] = {"canonical_url": url, "title": title}


for f in sorted(drill.BASE.glob("live-*/matching-prepare.json")):
    for x in json.loads(f.read_text())["local_products"]:
        remember(
            x["product_id"],
            x["canonical_url"],
            " ".join(filter(None, [x.get("brand"), x.get("title")])),
        )
for f in sorted(drill.BASE.glob("live-*/candidate-facts.json")):
    for p, u in json.loads(f.read_text())["urls"].items():
        remember(p, u)
for f in sorted(drill.BASE.glob("live-*/known-urls.json")):
    for p, u in json.loads(f.read_text()).items():
        remember(p, u)
for f in sorted(drill.BASE.glob("live-*/recurring-readings.json")):
    for r in json.loads(f.read_text()):
        remember(r["product_id"], r["canonical_url"], r.get("title"))
for f in sorted(drill.BASE.glob("live-*/baseline-bound.json")) + sorted(
    drill.BASE.glob("live-*/advance-bound.json")
):
    for p, r in json.loads(f.read_text()).items():
        remember(p, r["canonical_url"], r.get("title"))
write("known-urls.json", URLS)

if not (D / "plan-prepare.json").exists():
    if "--skip-history-sync" in sys.argv:
        p = drill.grossy("weekly", "plan-prepare", "--skip-history-sync", out="plan-prepare.json")
    else:
        if not (D / "listed-orders.json").exists():
            stop("listed-orders.json missing; run history_sync.py first")
        p = drill.grossy(
            "weekly",
            "plan-prepare",
            "--listed-orders",
            str(D / "listed-orders.json"),
            out="plan-prepare.json",
        )
    print("run:", p["run_id"], p["lifecycle"])
run = json.loads((D / "plan-prepare.json").read_text())["run_id"]

if not (D / "baseline-result.json").exists():
    obs, bound, unavailable, searched = cart_bind.bind_cart(KNOWN, "baseline")
    write("baseline-cart.json", obs)
    write(
        "baseline-bound.json",
        {
            p: {k: r[k] for k in ("title", "quantity", "displayed_price_eur", "canonical_url")}
            for p, r in bound.items()
        },
    )
    print(
        "baseline cart lines:",
        len(obs["lines"]),
        "| unavailable lines excluded:",
        len(unavailable),
        "| bound via search:",
        len(searched),
    )
    for p, r in bound.items():
        print(f"  {p} x{r['quantity']} €{r['displayed_price_eur']}  {r['title'][:60]}")
    b = drill.grossy(
        "weekly", "baseline-apply", run, str(D / "baseline-cart.json"), out="baseline-result.json"
    )
    print("baseline-apply:", b["status"], b["lifecycle"], "lines", b["baseline_line_count"])
b = json.loads((D / "baseline-result.json").read_text())
bound = json.loads((D / "baseline-bound.json").read_text())
for p, r in bound.items():
    remember(p, r["canonical_url"], r["title"])
pend = [i for i in b["recurring_items"] if i["status"] == "pending"]
print("pending recurring items:", len(pend))
candidate_plan = drill.grossy(
    "weekly",
    "recurring-preflight",
    run,
    str(D / "baseline-cart.json"),
    out="recurring-candidates.json",
)
CANDIDATES = candidate_plan.get("product_candidates", [])
for candidate in CANDIDATES:
    remember(candidate["product_id"], candidate["canonical_url"])
write("known-urls.json", URLS)
start = drill.now()
readings = []


def read(pid, qty=None):
    """Known in-cart lines wait for their baseline quantity (stale 0 after navigation is a known
    display issue)."""
    try:
        r = drill.product(pid, URLS[pid], qty, force_reload=True)
    except SystemExit:
        if qty is None:
            raise
        print("read retry with reload:", pid, flush=True)
        r = drill.product(pid, URLS[pid], qty, force_reload=True)
    readings.append(r)
    return r


for i in pend:
    pid = i["product_id"]
    if pid not in URLS:
        stop("no_url_for:" + pid)
    r = read(pid, bound.get(pid, {}).get("quantity"))
    if r["availability"] != "available":
        for alt in [
            c["product_id"]
            for c in CANDIDATES
            if c["primary_product_id"] == pid and c["product_id"] != pid
        ]:
            if alt and alt in URLS and alt not in [x["product_id"] for x in readings]:
                ra = read(alt)
                print(
                    f"  primary {pid} unavailable -> read alternative {alt}: {ra['availability']}"
                )
lines = []
for p, br in bound.items():
    rr = next((x for x in readings if x["product_id"] == p), None) or read(p, br["quantity"])
    lines.append(
        {
            "product_id": p,
            "canonical_url": rr["canonical_url"],
            "quantity": rr["quantity"],
            "displayed_price_eur": rr["displayed_price_eur"],
        }
    )
facts = [
    {k: r[k] for k in ("product_id", "canonical_url", "displayed_price_eur", "availability")}
    for r in readings
]
write(
    "recurring-preflight-cart.json",
    {
        "schema_name": "grossy.cart.observation.v1",
        "schema_version": 1,
        "generated_at": start,
        "lines": lines,
        "recurring_product_facts": facts,
    },
)
write("recurring-readings.json", readings)
pf = drill.grossy(
    "weekly",
    "recurring-preflight",
    run,
    str(D / "recurring-preflight-cart.json"),
    out="recurring-preflight.json",
)
dec = {d["product_id"]: d for d in pf["decisions"]}
tot = Decimal(0)
for i in pend:
    d = dec.get(i["product_id"], {})
    st = d.get("decision") or d.get("status")
    r = next(x for x in readings if x["product_id"] == i["product_id"])
    q = i["intended_quantity"]
    if st == "passing" and r["displayed_price_eur"]:
        tot += Decimal(r["displayed_price_eur"]) * q
    print(
        f"  {i['product_id']} {str(st):32} x{q} "
        f"€{r['displayed_price_eur'] or '-':>6}  {r['title'][:55]}"
    )
print(
    "preflight:",
    pf["status"],
    "| pending",
    len(pend),
    "| passing subtotal €",
    tot,
    "| first action:",
    [(a["product_id"], a["quantity"]) for a in pf["manifest"]["additions"]],
)
