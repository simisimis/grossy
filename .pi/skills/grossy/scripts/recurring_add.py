"""Drill 2 recurring prefill, light identity-bound verification. One click per persisted+checked intent; never re-click."""
import sys, json, re, time, datetime; sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import live_common as drill, cart_bind, recurring_cart
from live_common import stop, write, D
from slot_gate import require_ready

try:
    require_ready(D)
except ValueError as exc:
    stop(str(exc))

RUN = json.load(open(D/"plan-prepare.json"))["run_id"]
INIT = {r["product_id"]: r for r in json.load(open(D/"recurring-readings.json"))}
TRACKED = list(INIT)
URLS = json.load(open(D/"known-urls.json"))
URLS.update({p: r["canonical_url"] for p, r in INIT.items()})
ALTERNATIVES = {}

def remember_candidates(result):
    for c in result.get("product_candidates", []):
        URLS[c["product_id"]] = c["canonical_url"]
        if c["product_id"] != c["primary_product_id"]:
            alternatives = ALTERNATIVES.setdefault(c["primary_product_id"], [])
            if c["product_id"] not in alternatives: alternatives.append(c["product_id"])

remember_candidates(json.load(open(D/"recurring-preflight.json")))
KNOWN = {p: {"title": r["title"], "canonical_url": r["canonical_url"]} for p, r in INIT.items()}
EXP_FILE = D/"verified-recurring-quantities.json"
MAX_ACTIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
STALE = 240  # seconds; observation freshness limit is 300
cache = {}   # pid -> reading + read_at (epoch) + iso

def norm(t): return " ".join((t or "").split()).casefold()

def read(pid, qty):
    url = URLS.get(pid) or cache[pid]["canonical_url"]
    t = time.time(); iso = drill.now()
    try:
        r = drill.product(pid, url, qty)
    except SystemExit:
        print("read retry with reload:", pid, flush=True)
        t = time.time(); iso = drill.now()
        r = drill.product(pid, url, qty, force_reload=True)
    cache[pid] = {**r, "read_at": t, "iso": iso}
    if pid not in TRACKED: TRACKED.append(pid)

def refresh(expected, skip=()):
    for pid in list(dict.fromkeys([*TRACKED, *expected])):
        if pid in skip: continue
        if pid not in cache or time.time() - cache[pid]["read_at"] > STALE:
            read(pid, expected.get(pid, 0))
    for pid in list(TRACKED):
        if cache[pid]["availability"] == "unavailable":
            for alt in ALTERNATIVES.get(pid, []):
                if alt not in URLS: stop("approved_alternative_url_missing:"+alt)
                if alt not in cache or time.time() - cache[alt]["read_at"] > STALE:
                    read(alt, expected.get(alt, 0))

def checkout_check(expected, tag, protected=()):
    """Identity-bound checkout readback. Returns the buyable quantities (unavailable lines excluded)."""
    c = cart_bind.checkout_rows(tag)
    try:
        recurring_cart.bind_unavailable_rows(c, cache, KNOWN, lambda pid, url: read(pid, None))
        return recurring_cart.reconcile(c, expected, cache, protected)
    except ValueError as exc:
        write(tag+"-checkout-diagnostic.json", {"reason": str(exc), "row_count": len(c["rows"])})
        stop(str(exc))

def observation(expected):
    for p in expected:
        if cache[p]["quantity"] != expected[p]: stop("cached_quantity_differs:"+p)
    used = [cache[p] for p in dict.fromkeys([*TRACKED, *expected])]
    oldest = min(used, key=lambda r: r["read_at"])
    if time.time() - oldest["read_at"] > 290: stop("observation_would_be_stale")
    return {"schema_name": "grossy.cart.observation.v1", "schema_version": 1, "generated_at": oldest["iso"],
            "lines": [{"product_id": p, "canonical_url": cache[p]["canonical_url"], "quantity": q, "displayed_price_eur": cache[p]["displayed_price_eur"]} for p, q in expected.items()],
            "recurring_product_facts": [{k: cache[p][k] for k in ("product_id", "canonical_url", "displayed_price_eur", "availability")} for p in TRACKED]}

def phase(cmd, name, schema, status):
    r = drill.grossy("weekly", cmd, RUN, str(D/name), out=name.replace(".json", "")+"-"+cmd+"-result.json")
    if r.get("schema_name") != schema or r.get("schema_version") != 1 or r.get("status") != status: stop(cmd+"_unexpected_result")
    return r

def readback(pre, m, a, expected):
    post = {**expected, a["product_id"]: expected.get(a["product_id"], 0) + a["quantity"]}
    read(a["product_id"], post[a["product_id"]])  # reloads (in cart)
    refresh(post, skip={a["product_id"]})    # only stale ones
    post = checkout_check(post, pre+"-readback", protected={a["product_id"]})
    obs = observation(post); obs["mutation_result"] = {"manifest_checksum": m["checksum"], "outcome": "readback"}
    write(pre+"-readback.json", obs)
    phase("recurring-observe", pre+"-readback.json", "grossy.weekly.recurring-observe.v1", "success")
    write(EXP_FILE.name, post)
    return post

def resume_not_applied(pre, expected):
    """Close a persisted intent whose single click did not register: fresh reread + resume-prepare."""
    refresh(expected)
    checkout_check(expected, pre+"-resume")
    o = observation(expected); write(pre+"-resume-cart.json", o)
    rp = drill.grossy("weekly", "resume-prepare", RUN, str(D/(pre+"-resume-cart.json")), out=pre+"-resume-prepare-result.json")
    if rp.get("status") not in ("accepted", "success"): stop("resume_prepare_unexpected")
    (D/(pre+"-not-applied")).write_text("click did not register; reconciled via resume-prepare\n")

if EXP_FILE.exists():
    expected = json.load(open(EXP_FILE))
else:
    # First pass: seed from a full identity-bound cart read (baseline lines and any owner edits since).
    KNOWN = {p: {"canonical_url": r["canonical_url"], "title": r["title"]} for p, r in INIT.items()}
    if (D/"baseline-bound.json").exists():
        KNOWN.update({p: {"canonical_url": r["canonical_url"], "title": r["title"]} for p, r in json.load(open(D/"baseline-bound.json")).items()})
    t0, iso0 = time.time(), drill.now()
    obs0, bound0, unavailable0, searched0 = cart_bind.bind_cart(KNOWN, "seed")
    for p, r in bound0.items():
        cache[p] = {**r, "read_at": t0, "iso": iso0}
        INIT.setdefault(p, r)
    expected = {p: r["quantity"] for p, r in bound0.items()}
    write(EXP_FILE.name, expected)
    print(f"seeded from cart: {len(expected)} lines, {sum(expected.values())} items; unavailable lines excluded: {len(unavailable0)}", flush=True)
pending = [p.name.removesuffix("-manifest-recurring-intent-result.json") for p in D.glob("action-*-manifest-recurring-intent-result.json")]
pending = [p for p in pending if json.load(open(D/(p+"-manifest.json")))["additions"] and not (D/(p+"-readback-recurring-observe-result.json")).exists() and not (D/(p+"-not-applied")).exists()]
if len(pending) > 1: stop("multiple_pending_intents_require_review")
pend = pending[0] if pending else None
if pend:
    m = json.load(open(D/(pend+"-manifest.json"))); a0 = m["additions"][0]
    probe = drill.product(a0["product_id"], a0["canonical_url"], None, force_reload=True)
    if probe["availability"] != "available": stop("pending_target_unavailable_requires_review:"+a0["product_id"])
    if probe["quantity"] == expected.get(a0["product_id"], 0) + a0["quantity"]:
        expected = readback(pend, m, a0, expected)
        print(f"{pend}: outstanding intent reconciled by readback; cart lines {len(expected)}", flush=True)
    elif probe["quantity"] == expected.get(a0["product_id"], 0):
        refresh(expected)
        expected = checkout_check(expected, pend+"-resume")
        o = observation(expected); write(pend+"-resume-cart.json", o)
        rp = drill.grossy("weekly", "resume-prepare", RUN, str(D/(pend+"-resume-cart.json")), out=pend+"-resume-prepare-result.json")
        if rp.get("status") not in ("accepted", "success"): stop("resume_prepare_unexpected")
        (D/(pend+"-not-applied")).write_text("click did not register; reconciled via resume-prepare\n")
        print(f"{pend}: click had not registered; outstanding intent reconciled by resume-prepare (nothing verified, no re-click)", flush=True)
    else:
        stop("unexpected_pending_quantity")

for _ in range(MAX_ACTIONS):
    numbers = [int(m.group(1)) for p in D.glob("action-*") if (m := re.match(r"action-(\d+)", p.name))]
    pre = f"action-{max(numbers, default=0)+1:02d}"
    refresh(expected)
    expected = checkout_check(expected, pre+"-pre")
    o1 = observation(expected); write(pre+"-cart-1.json", o1)
    r1 = phase("recurring-preflight", pre+"-cart-1.json", "grossy.weekly.recurring-preflight.v1", "accepted")
    remember_candidates(r1)
    adds = r1["manifest"]["additions"]
    if adds:  # immediate reread of the exact target before intent
        read(adds[0]["product_id"], expected.get(adds[0]["product_id"], 0))
    refresh(expected)
    expected = checkout_check(expected, pre+"-pre-intent")
    o2 = observation(expected); write(pre+"-cart-2.json", o2)
    r2 = phase("recurring-preflight", pre+"-cart-2.json", "grossy.weekly.recurring-preflight.v1", "accepted")
    if r1["manifest"]["additions"] != r2["manifest"]["additions"] or r1["decisions"] != r2["decisions"]:
        before = {f["product_id"]: f for f in o1["recurring_product_facts"]}
        after = {f["product_id"]: f for f in o2["recurring_product_facts"]}
        price_changed = any(p in before and before[p]["availability"] == after[p]["availability"] == "available" and before[p]["displayed_price_eur"] != after[p]["displayed_price_eur"] for p in after)
        stock_changed = any(p not in before or before[p]["availability"] != after[p]["availability"] for p in after)
        if price_changed or not stock_changed: stop("accepted_action_or_decisions_changed")
        print("Stock/approved alternative changed before intent; preparing fresh action (no click)", flush=True)
        continue
    m = r2["manifest"]; write(pre+"-manifest.json", m)
    if not m["additions"]:
        done = phase("recurring-intent", pre+"-manifest.json", "grossy.weekly.recurring-authorization.v1", "persisted")
        write(EXP_FILE.name, expected)
        print("ZERO-ADDITION intent persisted; recurring complete;", {k: done.get(k) for k in ("lifecycle",)}, flush=True)
        print("Unavailable:", [p for p in TRACKED if cache[p]["availability"] == "unavailable"], flush=True)
        for primary, alternatives in ALTERNATIVES.items():
            if primary in cache and cache[primary]["availability"] == "unavailable":
                for alt in alternatives:
                    if expected.get(alt): print("Alternative in basket:", primary, "->", alt, flush=True)
        for decision in r2["decisions"]:
            if decision["status"].startswith("skipped") or decision["status"].startswith("blocked"):
                print("Skipped:", decision, flush=True)
        break
    a = m["additions"][0]
    if m["stage"] != "recurring" or len(m["additions"]) != 1 or not 1 <= a["quantity"] <= 3 or a["product_id"] not in TRACKED \
       or a["price_eur"] != cache[a["product_id"]]["displayed_price_eur"]:
        stop("action_outside_observed_scope")
    attempt = sum(json.load(open(D/(p.name.removesuffix("-not-applied")+"-manifest.json")))["additions"][0]["product_id"] == a["product_id"] for p in D.glob("action-*-not-applied"))
    if attempt >= len(drill.BACKOFF): stop("click_not_registered_after_backoff:"+a["product_id"])
    i = phase("recurring-intent", pre+"-manifest.json", "grossy.weekly.recurring-authorization.v1", "persisted")
    if i.get("manifest_checksum") != m["checksum"] or i.get("addition") != a: stop("intent_binding_mismatch")
    ck = phase("action-check", pre+"-manifest.json", "grossy.weekly.action-check.v1", "authorized")
    if ck.get("manifest_checksum") != m["checksum"] or ck.get("addition") != a: stop("action_check_binding_mismatch")
    before_q = expected.get(a["product_id"], 0)
    # The pre-intent checkout read left the tab on /checkout; return to the exact target page and
    # confirm its quantity/price immediately before the single write.
    read(a["product_id"], before_q)
    if cache[a["product_id"]]["availability"] != "available" or cache[a["product_id"]]["displayed_price_eur"] != a["price_eur"]:
        stop("target_changed_before_click:"+a["product_id"])
    got = drill.add_units(a["product_id"], before_q, a["quantity"], drill.BACKOFF[attempt], pre)
    if got != before_q + a["quantity"]:
        if got != before_q: stop(f"partial_addition_needs_attention:{a['product_id']}:{got}")
        q = drill.probe_quantity(a["product_id"], a["canonical_url"])
        if q == before_q:
            resume_not_applied(pre, expected)
            print(f"{pre}: click not registered (waited {drill.BACKOFF[attempt]}s); intent closed via resume-prepare; retrying with longer wait", flush=True)
            continue
        if q != before_q + a["quantity"]: stop("unexpected_quantity_after_click")
    expected = readback(pre, m, a, expected)
    print(f"{pre}: added {a['product_id']} €{a['price_eur']} verified; cart lines {len(expected)}", flush=True)
else:
    stop("recurring_action_bound_reached: max actions used (writes plus stock-change re-prepares); recurring not complete, rerun to continue")
