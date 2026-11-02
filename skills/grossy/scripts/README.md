# Grossy live-workflow scripts

Deterministic coordinator scripts for the supervised AB.gr workflow. They wrap
the `grossy` CLI phases and the verified `agent-browser` page procedures from
the skill's Observation Protocol, so a weekly run does not have to be rebuilt
from the prose. Run everything through `live.sh` from any directory:

```sh
skills/grossy/scripts/live.sh <step> [args]
```

Each step executes inside the repository Nix shell. Private artifacts go to the
work directory named in `~/.local/state/grossy/live-current.txt` (override with
`GROSSY_LIVE_DIR`). The browser session name lives in `<workdir>/session.txt`
and the pinned task tab in `<workdir>/task-tab-target.json`. The scripts never
print source order IDs or URLs; counts only.

## Weekly plan, in order

| Step | Script | Effect |
| --- | --- | --- |
| `start [--reuse]` | `live_start.py` | New 0700 work dir, pointer file, headed Brave pinned on the order-history overview, pending manual-slot receipt. `--reuse` rebinds and preserves this directory's readiness state. |
| *(owner selects delivery day/time manually; agent ends turn and waits)* | | No further browser activity, history sync, availability checks or prefill. |
| `slot-ready` | `slot_ready.py` | Only after the owner's explicit "slot selected" report: record readiness locally without inspecting slot details or touching the browser/cart. |
| `history [--dry-run]` | `history_sync.py` | discover-prepare, six dated controls, listed proposal, sync-dry-run, scan-prepare, bounded scan of unknown orders, scan-validate, sync-apply. |
| `stage1 [--skip-history-sync]` | `plan_stage1.py` | plan-prepare, identity-bound whole-cart baseline, baseline-apply, recurring facts (alternatives for unavailable primaries), first recurring-preflight summary. |
| `recurring [max]` | `recurring_add.py` | Recurring prefill loop under stage consent: reread, preflight twice, intent, action-check, one click, identity-bound readback, recurring-observe; ends with the zero-addition intent (`awaiting-basket-review`). |
| *(owner reviews basket in the browser)* | | |
| `advance` | `advance.py` | Binds every checkout line to a product page after owner edits and runs `weekly advance`; reports unavailable/removed/re-bound/added separately. |
| `recipes` | `recipes_apply.py` | Empty-match offline options proposal, `recipes-apply`, prints the four slots and runs `matching-prepare`. |
| *(coordinator writes semantic matches spec)* | | see `matches_apply.py` docstring for the spec format |
| `matches <spec.json>` | `matches_apply.py` | Reads catalogue facts for non-local identities, checksums the proposal, `matches-apply`, prints ingredient previews and costs. |
| *(owner picks dinners)* | | never run selection on the owner's behalf |
| `recipe-add <recipeN,...|all> [max]` | `recipe_add.py` | `selection-apply`, then the recipe prefill loop (recipe-preflight twice, intent, action-check, one click, readback, recipe-observe) until zero additions (`awaiting-user-verification`). |
| `grossy weekly finish <run-id>` | | Local closure after the owner reviewed the cart. |
| `status` | `status.py` | Work dir, run id, lifecycle, verified quantities, pending-intent markers. |

Standalone history sync uses `start --history-only` then `history [--dry-run]`;
that mode cannot authorize weekly preparation or prefill. Weekly history, stage1,
recurring, advance, recipes, matches and recipe-add fail before phase/browser
activity unless the private
`delivery-slot-readiness.json` receipt is confirmed. It stores no delivery details.
New work directories never inherit readiness; malformed/missing receipts fail closed.
`slot-ready` refuses confirmation after plan-prepare, preventing a slot change
from silently reusing earlier stock/price facts. Stop for separate review for a
changed slot or a legacy prepared run lacking a receipt; do not fabricate one.
Slot readiness is owner-attested, not browser-verified, and is not mutation consent.

Other CLI phases (reroll-apply, abandon, consent-revoke, removal-*) run with
`live.sh grossy weekly ...`.

## Shared modules

- `slot_gate.py`: private readiness validation and atomic 0600 owner-attestation receipt; no browser/SQLite access.
- `live_common.py`: exact task-tab binding with `--pin-tab`, origin/CAPTCHA/login
  guard on every `eval`, boolean-only readiness waits, product-page reader
  (`product`, `recipe_fact`), read-only checkout reader, `grossy` phase runner,
  `click_once`/`add_units` (one click, backoff, never re-click), `probe_quantity`.
- `cart_bind.py`: settled checkout rows and identity binding of every line to a
  canonical `/p/<id>` page (known titles first, then exact name/brand search).

## Stops and recovery

Every script exits non-zero with `STOP: <coded reason>` on ambiguity, nonzero
CLI exit, unexpected route, authentication/CAPTCHA, unbound cart line or drift.
Nothing retries a browser write. If a click did not register, the loops close
the intent with a fresh reread via `resume-prepare` and try once more with a
longer pre-click delay (1 s, 2 s, 5 s); after three not-applied attempts they
stop. Re-running a loop first reconciles any outstanding intent by readback.
If the owner closed the window, `live_common.browser` relaunches Brave with the
profile only when every remaining tab is `about:blank`. `start` rebinds the
previously bound tab when it still exists, even if the owner opened other tabs.

Recurring stock changes: recognize checkout rows with See Alternatives and no
quantity input, then confirm unavailability on the identity-bound product page.
Record unavailable stock without inventing quantity; try the structured preflight
`product_candidates` alternatives, or skip and continue other safe products.
Keep earlier verified additions in audit history. Final reports include unavailable,
substituted and skipped items. An unavailable just-written target still stops with
unverified readback; unrelated drift, conflicting prices and ambiguous identities
still stop. Action artifact numbers do not depend on cart line count, so losing an
unavailable line cannot overwrite an earlier intent. Both prefill loops share
`cart_bind.checkout_rows` and the pure `recurring_cart` reconciliation; the recipe
loop binds a pre-existing unavailable row the same way but stops if a buyable line
disappears after the recipe anchor.

Owner edits are only accepted at the basket-review barrier. A cart that changes
between `stage1` baseline and the `recurring` loop puts the run in
`needs-attention`, and neither preflight nor resume-prepare accepts it: abandon
the run and re-plan (`start`, manual slot selection, owner report, `slot-ready`,
`history`, `stage1`, `recurring`); the new
baseline binds the current cart and credits the edits. Learned family
recurrence is recomputed by every history sync; pin cadences the owner cares
about with `grossy history set-recurrence <family> --weeks N` between runs.
