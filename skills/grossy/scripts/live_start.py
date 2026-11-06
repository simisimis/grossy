"""Create a private live work directory, point live-current.txt at it, and launch/bind the
task-owned headed Brave tab.

Usage: live_start.py [--reuse] [--history-only]
Default weekly start pauses for manual delivery-slot selection; --history-only is for standalone
history sync. --reuse preserves this work directory's readiness state and only (re)binds the
browser tab. Read-only: opens the reservation-history list route. Never adopts a tab the agent
did not open.
"""

import datetime
import json
import os
import pathlib
import subprocess
import sys

from slot_gate import initialize, load

if set(sys.argv[1:]) - {"--reuse", "--history-only"}:
    print("STOP: usage: live.sh start [--reuse] [--history-only]", flush=True)
    sys.exit(1)

os.umask(0o077)
BASE = (
    pathlib.Path(os.environ.get("XDG_STATE_HOME", str(pathlib.Path.home() / ".local/state")))
    / "grossy"
)
PTR = BASE / "live-current.txt"
PROFILE = (
    pathlib.Path(os.environ.get("XDG_DATA_HOME", str(pathlib.Path.home() / ".local/share")))
    / "grossy"
    / "agent-browser"
    / "ab-profile"
)
BRAVE = pathlib.Path.home() / ".nix-profile" / "bin" / "brave"
START_URL = "https://www.ab.gr/en/my-account/reservation-history"

prev_session = prev_target = None
if PTR.exists():
    prev = pathlib.Path(PTR.read_text().strip())
    if (prev / "session.txt").exists():
        prev_session = (prev / "session.txt").read_text().strip()
    if (prev / "task-tab-target.json").exists():
        prev_target = json.loads((prev / "task-tab-target.json").read_text()).get("target_id")
if "--reuse" in sys.argv and PTR.exists():
    D = pathlib.Path(PTR.read_text().strip())
else:
    D = BASE / ("live-weekly-" + datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ"))
    D.mkdir(mode=0o700)
    PTR.write_text(str(D) + "\n")
    PTR.chmod(0o600)
try:
    initialize(D, history_only="--history-only" in sys.argv, reuse="--reuse" in sys.argv)
except ValueError as exc:
    print("STOP:", exc, flush=True)
    sys.exit(1)
# One headed Brave per profile: a new work dir keeps the previous session name so a live tab can
# be rebound.
SESSION = (
    (D / "session.txt").read_text().strip()
    if (D / "session.txt").exists()
    else (prev_session or "grossy-live-" + D.name[-16:])
)
(D / "session.txt").write_text(SESSION + "\n")


def tabs():
    p = subprocess.run(
        ["agent-browser", "--session", SESSION, "--pin-tab", "tab", "list", "--json"],
        capture_output=True,
        text=True,
    )
    # Exit 1 / success:false means no browser for this session, not an empty tab list.
    if p.returncode:
        return None
    try:
        d = json.loads(p.stdout)
        d = d.get("data", d)
        t = d.get("tabs", d) if isinstance(d, dict) else d
        return t if isinstance(t, list) else None
    except ValueError:
        return None


t = tabs()
if t and prev_target and any(x.get("targetId") == prev_target for x in t):
    target = prev_target
    print("rebound the previously bound task tab (other owner tabs are left alone)")
elif t and any(x.get("url") != "about:blank" for x in t):
    if len(t) != 1:
        print(
            "STOP: session has several tabs and none is the agent's bound tab; "
            "close them or use a new session",
            flush=True,
        )
        sys.exit(1)
    target = t[0]["targetId"]
    print("reusing existing task tab")
else:
    if t is not None:
        subprocess.run(
            ["agent-browser", "--session", SESSION, "close"], capture_output=True, text=True
        )
    # Imported here: live_common binds to live-current.txt, which exists only from this point.
    from live_common import BRAVE_LAUNCH_ARGS

    p = subprocess.run(
        [
            "agent-browser",
            "--session",
            SESSION,
            "--headed",
            "--executable-path",
            str(BRAVE),
            "--profile",
            str(PROFILE),
            "--args",
            BRAVE_LAUNCH_ARGS,
            "--pin-tab",
            "open",
            START_URL,
        ],
        capture_output=True,
        text=True,
    )
    (D / "launch.err").write_text(p.stderr)
    t = tabs()
    if p.returncode or not t or len(t) != 1:
        print("STOP: browser launch failed (see launch.err)", flush=True)
        sys.exit(1)
    target = t[0]["targetId"]
    print("launched headed Brave with the Grossy profile")
(D / "task-tab-target.json").write_text(json.dumps({"target_id": target}))
print("work dir:", D)
print("session:", SESSION)
print("task tab bound")
if "--history-only" not in sys.argv:
    if load(D)["status"] == "awaiting-owner":
        print(
            'PAUSE: select your delivery day/time slot manually in Brave, then report "slot selecte'
            'd". '
            "No history, availability checks or prefill yet."
        )
    else:
        print(
            "Owner-reported slot readiness preserved for this work directory; "
            "stop if the slot changed."
        )
