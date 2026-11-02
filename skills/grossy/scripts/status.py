"""Print the current live work directory, run id, lifecycle and verified cart quantities.

No browser, no writes.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_common as drill
from live_common import D

print("work dir:", D)
if not (D / "plan-prepare.json").exists():
    print("no run prepared yet")
    sys.exit(0)
run = json.loads((D / "plan-prepare.json").read_text())["run_id"]
rep = drill.BASE / "runs" / run / "report.json"
life = json.loads(rep.read_text())["lifecycle"] if rep.exists() else "(report missing)"
print("run:", run, "| lifecycle:", life)
for f in ("verified-cart-quantities.json", "verified-recurring-quantities.json"):
    if (D / f).exists():
        q = json.loads((D / f).read_text())
        print(f, "| lines", len(q), "| items", sum(q.values()))
        break
pend = sorted(D.glob("*-pending.json"))
if pend:
    print("PENDING intent markers:", [p.name for p in pend], "-> reconcile before any new write")
