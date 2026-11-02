"""Record readiness only after the owner explicitly reports manual slot selection.

Usage: live.sh slot-ready
No browser reads/writes, slot details, or cart consent are collected here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_common import D, stop
from slot_gate import confirm

if len(sys.argv) != 1:
    stop("usage: live.sh slot-ready")
try:
    confirm(D)
except ValueError as exc:
    stop(str(exc))
print("Owner-reported delivery slot readiness recorded; no rendered slot verification performed.")
