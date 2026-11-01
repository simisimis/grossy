"""Apply coordinator-reviewed semantic ingredient matches: read catalogue facts for non-local identities (read-only
browser), compute the proposal checksum, run `weekly matches-apply`, print per-ingredient previews.

Usage: matches_apply.py <matches-spec.json>
Spec format (agent-authored after reviewing matching-prepare.json):
{
  "urls": {"<product_id>": "<canonical product url>"},        # for every non-local product id used
  "matches": [ {"recipe_id", "revision_id", "ingredient_name", "product_id", "compatible_product_ids": [...],
                "rationale", "fallback_product_ids": [...] (optional), "retail_unit_coverage": {pid: "n"} (optional)} ]
}
Evidence is always agent_semantic_review; availability/price stay unknown/null (verified later by recipe-preflight).
"""
import sys, json, os, hashlib; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_common as drill
from live_common import stop, write, D
from slot_gate import require_ready

try:
    require_ready(D)
except ValueError as exc:
    stop(str(exc))

RUN = json.load(open(D/"plan-prepare.json"))["run_id"]
spec = json.load(open(sys.argv[1]))
mp = json.load(open(D/"matching-prepare.json"))
LOCAL = {p["product_id"]: p["canonical_url"] for p in mp["local_products"]}
URL = {**LOCAL, **json.load(open(D/"known-urls.json")), **spec.get("urls", {})}
matches = []
for m in spec["matches"]:
    rec = {"recipe_id": m["recipe_id"], "revision_id": str(m["revision_id"]), "ingredient_name": m["ingredient_name"], "product_id": m["product_id"],
           "fallback_product_ids": list(m.get("fallback_product_ids", [])), "availability": "unknown", "price_eur": None,
           "evidence": "agent_semantic_review", "rationale": m["rationale"], "compatible_product_ids": list(m["compatible_product_ids"])}
    if m.get("retail_unit_coverage"): rec["retail_unit_coverage"] = m["retail_unit_coverage"]
    matches.append(rec)
new_ids = sorted({p for m in matches for p in m["compatible_product_ids"]} - set(LOCAL))
catalogue = []
for pid in new_ids:
    if pid not in URL: stop("no_url_for_catalogue_product:"+pid)
    f = drill.recipe_fact(pid, URL[pid])
    brand, _, title = (f["title"] or "").partition(" | ")
    rec = {"product_id": pid, "canonical_url": URL[pid], "title": (title or brand).strip(), "observed_at": drill.now()}
    if title and brand.strip() not in ("", "-"): rec["brand"] = brand.strip()
    catalogue.append(rec)
p = {"schema_name": "grossy.weekly.recipe-options.v1", "schema_version": 1, "generated_at": drill.now(), "run_id": RUN, "matches": matches}
if catalogue: p["catalogue_products"] = catalogue
p["checksum"] = hashlib.sha256(json.dumps({k: v for k, v in p.items() if k != "checksum"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
write("dinner-matches.json", p)
urls = json.load(open(D/"known-urls.json")); urls.update({pid: URL[pid] for pid in new_ids}); write("known-urls.json", urls)
r = drill.grossy("weekly", "matches-apply", RUN, str(D/"dinner-matches.json"), out="matches-apply.json")
print("matches-apply:", r.get("status"), "| new catalogue identities:", len(catalogue))
for s in r["recipe_slots"]:
    pv = s.get("preview")
    if not pv: continue
    print("==", s["slot"], pv["title"], "| est. incremental €", pv.get("estimated_incremental_cost_eur"), "| known €", pv.get("known_incremental_cost_eur"), "| cost complete:", pv.get("cost_complete"))
    for i in pv["ingredients"]:
        print(f'   {i["name"]:26} {i["status"]:10} target {str(i.get("target")):>6} {str(i.get("unit") or ""):5} -> {(i.get("exact_match") or {}).get("product_id","-"):8} packs {i.get("package_quantity")}  credit {i.get("cart_credit")}  €{i.get("incremental_cost_eur")}')
