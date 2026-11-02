"""Recipe options (offline): emit a closed recipe-options proposal and run `weekly recipes-apply`, then print the
four slots and `matching-prepare` ingredient status so the coordinator can write semantic matches.

Usage: recipes_apply.py [matches-spec.json]
Without a spec the proposal has no matches (CLI picks slots; ingredients stay unresolved until matches_apply.py).
No browser use.
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
def checksum(p):
    return hashlib.sha256(json.dumps({k: v for k, v in p.items() if k != "checksum"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
if not (D/"recipes-apply.json").exists():
    matches = json.load(open(sys.argv[1]))["matches"] if len(sys.argv) > 1 else []
    p = {"schema_name": "grossy.weekly.recipe-options.v1", "schema_version": 1, "generated_at": drill.now(), "run_id": RUN, "matches": matches}
    p["checksum"] = checksum(p)
    write("recipe-options.json", p)
    r = drill.grossy("weekly", "recipes-apply", RUN, str(D/"recipe-options.json"), out="recipes-apply.json")
    print("recipes-apply:", r["status"], r["lifecycle"])
r = json.load(open(D/"recipes-apply.json"))
for s in r["recipe_slots"]:
    pv = s.get("preview") or {}
    print(f"== {s['slot']} [{s.get('protein_class')}] {pv.get('title') or '(empty: '+str(s.get('empty_reason'))+')'}  recipe_id={pv.get('recipe_id')} revision={pv.get('revision_id')} servings={pv.get('target_servings')}")
    for i in pv.get("ingredients", []):
        em = (i.get("exact_match") or {}).get("product_id", "-")
        print(f"   {i['name']:28} {str(i.get('target')):>7} {str(i.get('unit') or ''):6} {i.get('status'):10} {'pantry' if i.get('pantry_covered') else '':6} -> {em}")
mp = drill.grossy("weekly", "matching-prepare", RUN, out="matching-prepare.json")
print("matching-prepare:", mp.get("status"), "| shown recipes", len(mp.get("recipes", [])), "| local products ranked by order presence:", len(mp.get("local_products", [])), "(matching-prepare.json)")
print("Next: review culinary compatibility per unresolved ingredient, write a matches spec, run live.sh matches <spec.json>.")
