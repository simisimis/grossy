#!/usr/bin/env bash
# Grossy live-workflow runner. Every step is a deterministic script in this directory executed inside the
# repository Nix shell with the private work directory from <state>/grossy/live-current.txt.
#
#   live.sh start [--reuse] [--history-only]  open Brave; weekly start pauses for manual slot selection
#   live.sh slot-ready                record the owner's explicit "slot selected" report
#   live.sh history [--dry-run] [--full]  history sync (reservation-history walk newest first, dry-run, scan-prepare)
#   live.sh stage1 [--skip-history-sync]  plan-prepare, whole-cart baseline, recurring facts, preflight
#   live.sh recurring [max-actions]    recurring prefill loop (intent -> action-check -> one click -> readback)
#   live.sh advance                    basket-review advance after owner edits
#   live.sh recipes [spec.json]        recipes-apply (offline options) + matching-prepare summary
#   live.sh matches <spec.json>        matches-apply from coordinator-reviewed semantic spec
#   live.sh recipe-add <recipes|all> [max-actions]   selection-apply then recipe prefill loop
#   live.sh status                     run id / lifecycle / verified cart quantities
#   live.sh grossy <args...>           any grossy CLI command in the same shell (finish, abandon, reroll-apply ...)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
STEP="${1:-}"; [ $# -gt 0 ] && shift
run() { exec nix develop --offline --no-write-lock-file "$REPO" -c "$@"; }
case "$STEP" in
  start)      run python3 "$HERE/live_start.py" "$@" ;;
  slot-ready) run python3 "$HERE/slot_ready.py" "$@" ;;
  history)    run python3 "$HERE/history_sync.py" "$@" ;;
  stage1)     run python3 "$HERE/plan_stage1.py" "$@" ;;
  recurring)  run python3 "$HERE/recurring_add.py" "$@" ;;
  advance)    run python3 "$HERE/advance.py" "$@" ;;
  recipes)    run python3 "$HERE/recipes_apply.py" "$@" ;;
  matches)    run python3 "$HERE/matches_apply.py" "$@" ;;
  recipe-add) run python3 "$HERE/recipe_add.py" "$@" ;;
  status)     run python3 "$HERE/status.py" "$@" ;;
  grossy)     run grossy "$@" ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
