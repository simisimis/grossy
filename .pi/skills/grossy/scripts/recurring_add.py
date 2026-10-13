"""Drill 2 recurring prefill, light identity-bound verification. One click per persisted+checked intent; never re-click."""
import sys, json, re, time, datetime; sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import live_common as drill, cart_bind
from live_common import stop, write, D

RUN = json.load(open(D/"plan-prepare.json"))["run_id"]
INIT = {r["product_id"]: r for r in json.load(open(D/"recurring-readings.json"))}
APPROVED = [pid for pid, r in INIT.items() if r["availability"] == "available"]
EXP_FILE = D/"verified-recurring-quantities.json"
MAX_ACTIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
STALE = 240  # seconds; observation freshness limit is 300
cache = {}   # pid -> reading + read_at (epoch) + iso

def norm(t): return " ".join((t or "").split()).casefold()

def read(pid, qty):
    url = INIT[pid]["canonical_url"] if pid in INIT else cache[pid]["canonical_url"]
    t = time.time(); iso = drill.now()
    try:
        r = drill.product(pid, url, qty)
    except SystemExit:
        print("read retry with reload:", pid, flush=True)
        t = time.time(); iso = drill.now()
        r = drill.product(pid, url, qty, force_reload=True)
    if r["availability"] != "available": stop("became_unavailable:"+pid)
    cache[pid] = {**r, "read_at": t, "iso": iso}

def refresh(expected, skip=()):
    for pid in dict.fromkeys([*APPROVED, *expected]):
        if pid in skip: continue
        if pid not in cache or time.time() - cache[pid]["read_at"] > STALE:
            read(pid, expected.get(pid, 0))

def checkout_check(expected, tag):
    c = drill.checkout(len(expected), tag)
    if c["line_count"] != len(expected) or c["empty_count"] != (0 if expected else 1): stop("checkout_count_conflict")
    matched = set()
    for row in c["rows"]:
        if row.get("ambiguous") or not row.get("name") or row["price_count"] != 1: stop("ambiguous_checkout_line")
        price = drill.price_of(row["price_parts"])
        ids = [p for p in expected if norm(row["name"]) in norm(cache[p]["title"]) and norm(row.get("brand") or "") in norm(cache[p]["title"])
               and str(expected[p]) == row["quantity"] and price in (cache[p]["displayed_price_eur"], "%.2f" % (float(cache[p]["displayed_price_eur"]) * expected[p]))]
        if len(ids) != 1 or ids[0] in matched: stop("checkout_line_not_bound_to_verified_product")
        matched.add(ids[0])
    if matched != set(expected): stop("checkout_unbound_line")
    if expected and (c["badge_count"] != 1 or not re.fullmatch(r"\d+", c["badge_text"][0]) or int(c["badge_text"][0]) != sum(expected.values())):
        stop("header_badge_inconsistent")

def observation(expected):
    for p in expected:
        if cache[p]["quantity"] != expected[p]: stop("cached_quantity_differs:"+p)
    used = [cache[p] for p in dict.fromkeys([*APPROVED, *expected])]
    oldest = min(used, key=lambda r: r["read_at"])
    if time.time() - oldest["read_at"] > 290: stop("observation_would_be_stale")
    return {"schema_name": "grossy.cart.observation.v1", "schema_version": 1, "generated_at": oldest["iso"],
            "lines": [{"product_id": p, "canonical_url": cache[p]["canonical_url"], "quantity": q, "displayed_price_eur": cache[p]["displayed_price_eur"]} for p, q in expected.items()],
            "recurring_product_facts": [{k: cache[p][k] for k in ("product_id", "canonical_url", "displayed_price_eur", "availability")} for p in APPROVED]}

def phase(cmd, name, schema, status):
    r = drill.grossy("weekly", cmd, RUN, str(D/name), out=name.replace(".json", "")+"-"+cmd+"-result.json")
    if r.get("schema_name") != schema or r.get("schema_version") != 1 or r.get("status") != status: stop(cmd+"_unexpected_result")
    return r

def readback(pre, m, a, expected):
    post = {**expected, a["product_id"]: expected.get(a["product_id"], 0) + a["quantity"]}
    read(a["product_id"], post[a["product_id"]])  # reloads (in cart)
    refresh(post, skip={a["product_id"]})    # only stale ones
    checkout_check(post, pre+"-readback")
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
base = f"action-{len(expected)+1:02d}"
cands = [base] + [f"{base}r{k}" for k in range(1, 10)]
pend = next((c for c in reversed(cands) if (D/(c+"-manifest-recurring-intent-result.json")).exists()), None)
if pend and json.load(open(D/(pend+"-manifest.json")))["additions"] and not (D/(pend+"-readback-recurring-observe-result.json")).exists() and not (D/(pend+"-not-applied")).exists():
    m = json.load(open(D/(pend+"-manifest.json"))); a0 = m["additions"][0]
    probe = drill.product(a0["product_id"], a0["canonical_url"], None, force_reload=True)
    if probe["quantity"] == 1:
        expected = readback(pend, m, a0, expected)
        print(f"{pend}: outstanding intent reconciled by readback; cart lines {len(expected)}", flush=True)
    elif probe["quantity"] == 0:
        refresh(expected)
        checkout_check(expected, pend+"-resume")
        o = observation(expected); write(pend+"-resume-cart.json", o)
        rp = drill.grossy("weekly", "resume-prepare", RUN, str(D/(pend+"-resume-cart.json")), out=pend+"-resume-prepare-result.json")
        if rp.get("status") not in ("accepted", "success"): stop("resume_prepare_unexpected")
        (D/(pend+"-not-applied")).write_text("click did not register; reconciled via resume-prepare\n")
        print(f"{pend}: click had not registered; outstanding intent reconciled by resume-prepare (nothing verified, no re-click)", flush=True)
    else:
        stop("unexpected_pending_quantity")

for _ in range(MAX_ACTIONS):
    pre = f"action-{len(expected)+1:02d}"
    k = 0
    while (D/(pre+"-not-applied")).exists() or (k and (D/(pre+"-manifest.json")).exists()):
        k += 1; pre = f"action-{len(expected)+1:02d}r{k}"
    refresh(expected)
    checkout_check(expected, pre+"-pre")
    o1 = observation(expected); write(pre+"-cart-1.json", o1)
    r1 = phase("recurring-preflight", pre+"-cart-1.json", "grossy.weekly.recurring-preflight.v1", "accepted")
    adds = r1["manifest"]["additions"]
    if adds:  # immediate reread of the exact target before intent
        read(adds[0]["product_id"], 0)
    o2 = observation(expected); write(pre+"-cart-2.json", o2)
    r2 = phase("recurring-preflight", pre+"-cart-2.json", "grossy.weekly.recurring-preflight.v1", "accepted")
    if r1["manifest"]["additions"] != r2["manifest"]["additions"] or r1["decisions"] != r2["decisions"]: stop("accepted_action_or_decisions_changed")
    m = r2["manifest"]; write(pre+"-manifest.json", m)
    if not m["additions"]:
        done = phase("recurring-intent", pre+"-manifest.json", "grossy.weekly.recurring-authorization.v1", "persisted")
        print("ZERO-ADDITION intent persisted; recurring complete;", {k: done.get(k) for k in ("lifecycle",)}, flush=True)
        break
    a = m["additions"][0]
    if m["stage"] != "recurring" or len(m["additions"]) != 1 or not 1 <= a["quantity"] <= 3 or a["product_id"] in expected or a["product_id"] not in APPROVED \
       or a["price_eur"] != cache[a["product_id"]]["displayed_price_eur"]:
        stop("action_outside_observed_scope")
    base = pre[:len("action-00") if pre.startswith("action") else len("recipe-00")]
    attempt = len(list(D.glob(base+"*-not-applied")))
    if attempt >= len(drill.BACKOFF): stop("click_not_registered_after_backoff:"+a["product_id"])
    i = phase("recurring-intent", pre+"-manifest.json", "grossy.weekly.recurring-authorization.v1", "persisted")
    if i.get("manifest_checksum") != m["checksum"] or i.get("addition") != a: stop("intent_binding_mismatch")
    ck = phase("action-check", pre+"-manifest.json", "grossy.weekly.action-check.v1", "authorized")
    if ck.get("manifest_checksum") != m["checksum"] or ck.get("addition") != a: stop("action_check_binding_mismatch")
    before_q = expected.get(a["product_id"], 0)
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
