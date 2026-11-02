"""Stage 2 recipe prefill: selection-apply (owner's explicit choice passed as argument), then the loop
fresh facts -> recipe-preflight x2 -> recipe-intent -> action-check -> ONE click -> identity-bound readback -> recipe-observe.

Usage: recipe_add.py <recipe-list|all> [max-actions]     e.g. recipe_add.py recipe1,recipe4
Never run without the owner's dinner selection. One click per persisted+checked intent; never re-clicks.
"""
import sys, json, time, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_common as drill, cart_bind, recurring_cart
from live_common import stop, write, D
from slot_gate import require_ready

try:
    require_ready(D)
except ValueError as exc:
    stop(str(exc))

RUN = json.load(open(D/"plan-prepare.json"))["run_id"]
args = [a for a in sys.argv[1:]]
SEL = args[0] if args and not args[0].isdigit() else None
MAX = int(args[-1]) if args and args[-1].isdigit() else 15
if not (D/"selection-apply.json").exists():
    if not SEL: stop("recipe selection argument required (recipe1,recipe3 or all)")
    r = drill.grossy("weekly", "selection-apply", RUN, SEL, out="selection-apply.json")
    print("selection-apply:", r.get("status"), r.get("lifecycle"), r.get("recipe_selection_mappings") or r.get("mappings") or "")
sel = json.load(open(D/"selection-apply.json"))
slots = {s["slot"]: s for s in (json.load(open(D/"matches-apply.json")) if (D/"matches-apply.json").exists() else json.load(open(D/"recipes-apply.json")))["recipe_slots"]}
chosen = [s for s in slots if SEL in (None, "all") or s in SEL.split(",")] if SEL else list(slots)
RECIPE = {}
for s in chosen:
    pv = slots[s].get("preview") or {}
    for i in pv.get("ingredients", []):
        em = i.get("exact_match") or {}
        if em.get("product_id"): RECIPE[em["product_id"]] = em.get("canonical_url")
        for fb in i.get("approved_family_fallbacks") or []:
            if fb.get("product_id"): RECIPE[fb["product_id"]] = fb.get("canonical_url")
URL = json.load(open(D/"known-urls.json")) if (D/"known-urls.json").exists() else {}
KNOWN = {}  # pid -> {title, canonical_url}: local evidence for binding unavailable checkout rows (e.g. a recurring line out of stock)
for r in json.load(open(D/"recurring-readings.json")): URL[r["product_id"]] = r["canonical_url"]; KNOWN[r["product_id"]] = r
for f in ("baseline-bound.json", "advance-bound.json"):
    if (D/f).exists():
        for p, r in json.load(open(D/f)).items(): URL[p] = r["canonical_url"]; KNOWN[p] = r
URL.update({p: u for p, u in RECIPE.items() if u})
missing_urls = [p for p in RECIPE if p not in URL]
if missing_urls: stop("missing_urls:" + ",".join(missing_urls))
print("selected slots:", chosen, "| recipe product identities to track:", len(RECIPE))
EXP_FILE = D/"verified-cart-quantities.json"
STALE = 240
cache = {}

def norm(t): return " ".join((t or "").split()).casefold()

def read(pid, qty):
    t, iso = time.time(), drill.now()
    try: r = drill.recipe_fact(pid, URL[pid], qty)
    except SystemExit:
        print("read retry with reload:", pid, flush=True); t, iso = time.time(), drill.now()
        drill.browser(["open", URL[pid]], "retry"); drill.browser(["reload"], "retry")
        r = drill.recipe_fact(pid, URL[pid], qty)
    cache[pid] = {**r, "read_at": t, "iso": iso}

def refresh(expected, skip=()):
    for pid in list(dict.fromkeys([*expected, *RECIPE])):
        if pid in skip: continue
        if pid not in cache or time.time() - cache[pid]["read_at"] > STALE: read(pid, expected.get(pid, 0))

def read_unavailable(pid, url):
    """Confirm an unavailable checkout row on its product page; such lines carry no quantity and no cart credit."""
    t, iso = time.time(), drill.now()
    r = drill.product(pid, url or URL[pid], None, force_reload=True)
    cache[pid] = {**r, "read_at": t, "iso": iso}

def checkout_check(expected, tag, protected=()):
    """Identity-bound checkout readback. Returns the buyable quantities (confirmed unavailable lines excluded)."""
    c = cart_bind.checkout_rows(tag)
    try:
        recurring_cart.bind_unavailable_rows(c, cache, KNOWN, read_unavailable)
        remaining = recurring_cart.reconcile(c, expected, cache, protected)
    except ValueError as exc:
        write(tag+"-checkout-diagnostic.json", {"reason": str(exc), "row_count": len(c["rows"])})
        stop(str(exc))
    if remaining != expected:
        # The recipe stage's cart anchor is identity/price-bound in the CLI; a line lost here is
        # unexplained drift there, so report it instead of continuing with a reduced cart.
        stop("checkout_lines_lost_since_recipe_anchor:" + ",".join(sorted(set(expected) - set(remaining))))
    return remaining

FACT_KEYS = ("product_id", "canonical_url", "displayed_price_eur", "availability", "package_amount", "package_unit", "package_count", "sale_kind")
def observation(expected):
    for p in expected:
        if cache[p]["quantity"] != expected[p]: stop("cached_quantity_differs:"+p)
    used = [cache[p] for p in dict.fromkeys([*expected, *RECIPE])]
    oldest = min(used, key=lambda r: r["read_at"])
    if time.time() - oldest["read_at"] > 290: stop("observation_would_be_stale")
    return {"schema_name": "grossy.cart.observation.v1", "schema_version": 1, "generated_at": oldest["iso"],
            "lines": [{"product_id": p, "canonical_url": cache[p]["canonical_url"], "quantity": q, "displayed_price_eur": cache[p]["displayed_price_eur"]} for p, q in expected.items()],
            "recipe_product_facts": [{k: cache[p][k] for k in FACT_KEYS} for p in RECIPE]}

def phase(cmd, name, schema, status):
    r = drill.grossy("weekly", cmd, RUN, str(D/name), out=name.replace(".json", "")+"-"+cmd+"-result.json")
    if r.get("schema_name") != schema or r.get("schema_version") != 1 or r.get("status") != status:
        stop(cmd+"_unexpected_result:"+str(r.get("status"))+":"+"; ".join(map(str, r.get("errors") or []))[:300])
    return r

def readback(pre, m, a, expected):
    post = {**expected, a["product_id"]: expected.get(a["product_id"], 0) + a["quantity"]}
    read(a["product_id"], post[a["product_id"]])
    refresh(post, skip={a["product_id"]})
    checkout_check(post, pre+"-readback", protected={a["product_id"]})
    obs = observation(post); obs["mutation_result"] = {"manifest_checksum": m["checksum"], "outcome": "readback"}
    write(pre+"-readback.json", obs)
    r = phase("recipe-observe", pre+"-readback.json", "grossy.weekly.recipe-observe.v1", "success")
    write(EXP_FILE.name, post)
    return post, r

def resume_not_applied(pre, expected):
    """Close a persisted intent whose single click did not register: fresh reread + resume-prepare."""
    refresh(expected)
    checkout_check(expected, pre+"-resume")
    o = observation(expected); write(pre+"-resume-cart.json", o)
    rp = drill.grossy("weekly", "resume-prepare", RUN, str(D/(pre+"-resume-cart.json")), out=pre+"-resume-prepare-result.json")
    if rp.get("status") not in ("accepted", "success"): stop("resume_prepare_unexpected")
    (D/(pre+"-not-applied")).write_text("click did not register; reconciled via resume-prepare\n")

expected = json.load(open(EXP_FILE)) if EXP_FILE.exists() else (json.load(open(D/"verified-recurring-quantities.json")) if (D/"verified-recurring-quantities.json").exists() else {})
if (D/"recipe-pending.json").exists(): stop("pending recipe intent marker present; reconcile manually")
for _ in range(MAX):
    pre = f"recipe-{sum(1 for p in expected if p in RECIPE)+1:02d}"
    k = 0
    while (D/(pre+"-manifest.json")).exists():
        k += 1; pre = f"recipe-{sum(1 for p in expected if p in RECIPE)+1:02d}r{k}"
    refresh(expected)
    checkout_check(expected, pre+"-pre")
    o1 = observation(expected); write(pre+"-cart-1.json", o1)
    r1 = phase("recipe-preflight", pre+"-cart-1.json", "grossy.weekly.recipe-preflight.v1", "accepted")
    if r1["manifest"]["additions"]: read(r1["manifest"]["additions"][0]["product_id"], expected.get(r1["manifest"]["additions"][0]["product_id"], 0))
    o2 = observation(expected); write(pre+"-cart-2.json", o2)
    r2 = phase("recipe-preflight", pre+"-cart-2.json", "grossy.weekly.recipe-preflight.v1", "accepted")
    if r1["manifest"]["additions"] != r2["manifest"]["additions"] or r1["decisions"] != r2["decisions"]: stop("accepted_action_or_decisions_changed")
    blocked = [d for d in r2["decisions"] if d.get("status") != "passing"]
    if blocked: write(pre+"-blocked.json", blocked); stop("recipe_blocked: "+"; ".join(r for d in blocked for r in d.get("reasons", []))[:400])
    m = r2["manifest"]; write(pre+"-manifest.json", m)
    if not m["additions"]:
        done = phase("recipe-intent", pre+"-manifest.json", "grossy.weekly.recipe-authorization.v1", "persisted")
        print("ZERO-ADDITION recipe intent persisted; recipe stage complete", flush=True); break
    a = m["additions"][0]
    if m["stage"] != "recipe" or len(m["additions"]) != 1 or a["product_id"] not in RECIPE or not 1 <= a["quantity"] <= 3 \
       or a["price_eur"] != cache[a["product_id"]]["displayed_price_eur"]:
        write(pre+"-out-of-scope.json", m); stop("action_outside_observed_scope")
    base = pre[:len("action-00") if pre.startswith("action") else len("recipe-00")]
    attempt = len(list(D.glob(base+"*-not-applied")))
    if attempt >= len(drill.BACKOFF): stop("click_not_registered_after_backoff:"+a["product_id"])
    write("recipe-pending.json", {"prefix": pre})
    i = phase("recipe-intent", pre+"-manifest.json", "grossy.weekly.recipe-authorization.v1", "persisted")
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
            (D/"recipe-pending.json").unlink()
            print(f"{pre}: click not registered (waited {drill.BACKOFF[attempt]}s); intent closed via resume-prepare; retrying with longer wait", flush=True)
            continue
        if q != before_q + a["quantity"]: stop("unexpected_quantity_after_click")
    expected, obs = readback(pre, m, a, expected)
    (D/"recipe-pending.json").unlink()
    print(f"{pre}: added {a['product_id']} €{a['price_eur']} verified; cart lines {len(expected)}; lifecycle {obs.get('lifecycle')}", flush=True)
