# Bounded supervised smoke runbook — awaiting user verification

**Not executed by workers/reviewers.** Only the user/coordinator may perform these
live checks after offline review, with explicit authorization of each step. Until
the owner accepts Task 16, this runbook keeps explicit exact-action approval for
every live addition (steps 5 and 7) and separate confirmation for removals. Task 16
stage consent (no per-action prompts) is pending normal-workflow behavior, not a
smoke allowance; its consent records and `weekly action-check` still apply. Do not
run unrestricted `just weekly-plan`, `weekly-add all`, or the skill's
repeat-until-complete loops here. Use the phase commands in [operations.md]
(operations.md) and the skill contract, pausing at the boundaries below. No
automatic expansion of this smoke allowance.

## Preconditions and universal stop rule

- Offline gate and fixture review accepted; user understands residual risks,
  including unestablished AB.gr permission/terms and unproven cross-day behavior.
- Matching CLI/skill available, private state initialized, policy reviewed,
  no competing writer/active run, verified private backup/export available.
- User manually authenticates headed Brave using the existing private profile.
  Do not type credentials/2FA, copy profile state, or retain login-page evidence.
- Review due requests and accepted local recipes before the live sequence. Arrange
  a safe scope with exactly **one missing recurring product action** and exactly
  **one missing selected-recipe product action**. One action may add a positive
  quantity greater than one, but only one exact product identity. If other missing
  actions exist, do not hide them, edit accepted manifests, disable families
  implicitly, or add them. Stop for separately reviewed scope/policy approval.
- Only vegetarian spicy-bean-burgers is bundled. Other seeds are controlled missing.
  An unavailable/empty slot or no safe single-action recipe is a blocker, not
  permission to source/generate recipes during smoke. Record missing evidence;
  complete a separately approved import/eligibility review before continuing.
- All phases require exit zero and the exact expected versioned JSON. Any failure,
  malformed output, stale facts, changed identity/cart/price, authentication loss,
  a blocking CAPTCHA challenge, unsafe exposure or revoked consent stops **all**
  browser activity. AB.gr shows a small persistent captcha badge on every page;
  a badge is not a challenge. Only a large or overlaying captcha element, or a
  visible login form, counts as blocking (see the skill's CAPTCHA handling).
  Preserve minimized local intent/observation state; no automatic retries, removals,
  compensation, or replay. User handles manual login/review before renewed approval.
- Never progress checkout, choose fulfilment/delivery slots, pay or submit orders.
  With explicit read-only approval, `/checkout` is permitted solely for AB.gr's
  cart-item list. Extract product identity/quantity/price only; do not inspect
  account/address/payment/fulfilment sections or activate checkout-stage controls.
  This exception does not authorize cart changes or other smoke steps.
  Do not record screenshots, DOM, headers, account/address/payment data, source
  order IDs, navigation order URLs, browser profile paths or credentials in logs.

Confirmations name operation, run, stage, product ID/canonical **product** URL,
positive delta, price, whole-cart budget, substitution/blocked decisions and expiry
of observed facts. Approval is only for that action, never the next action or a
changed manifest. Re-read immediately, re-preflight, compare full cart/facts and
exact approved action; changed facts/delta need renewed approval. Intent must be
persisted successfully before write; whole-cart readback follows each attempt.
Private schema-bound proposals may include only authorized state; human evidence
uses the sanitized template below.

## Ordered user-verification steps

### 1. Authenticated read-only doctor

**Operation:** authorize local `grossy doctor`, then separately authorize coordinator
rendered authentication, supported history-controls visibility and whole-cart-read
checks through agent-browser. For history, navigate directly to
`https://www.ab.gr/customerhub/quick-shop/orders` and verify that overview route;
do not open the homepage as an intermediate step or enter individual orders.
Expand the All orders dropdown/filter to reveal six dated options plus the
aggregate option. The owner reports date-specific products and an All orders
combined view; whether this uses filtering, fetching or navigation is unverified.
Inspect controls only, without scrolling/loading or extracting union products.
Opening this popup is read-only; selecting an option
is outside step 1. Verify the pinned tab's route before inspection. The owner
has separately approved `/checkout` for its cart list only: navigate directly
there for the whole-cart read, without checkout progression or cart writes.
Stop on another route or unexpected private-section exposure. CLI doctor has no live adapter and
reports those three checks **skipped**, not passed.

**Effect/evidence:** local check status and distinct supervised rendered check
status/counts. Record manual authentication as yes/no, not account identity.
**Stop:** local failures, missing tools, profile permissions, authentication/UI
ambiguity. **Confirmation:** explicit read-only scope approval before live inspection.

### 2. Observable-six history dry-run

**Operation:** authorize `/skill:grossy history sync --dry-run`: discover-prepare,
rendered six-control extraction, private listed proposal and sync-dry-run
`--listed-orders <listed.json>`. Expand the All orders dropdown to inspect its
six dated filter options; bind each to rendered stable numeric source identity.
Observed dated URL shape is `/customerhub/quick-shop/orders?id=<numeric-order-id>`;
the aggregate token LAST_ORDERS_PRODUCTS is excluded. Retain the numeric ID and
match it exactly to source identity, preserving leading zeros; do not strip
queries or invent paths. The CLI accepts only one raw numeric `id` on that exact
route; aggregate/empty/extra/duplicate/encoded parameters and fragments fail.
Do not select a date/aggregate option, scroll/load products or extract union contents.

**Effect/evidence:** listed count six, unknown count, dated-controls validation,
no contents read/no cart change. IDs stay in the private proposal only.
**Stop:** any count/identity/date/route ambiguity. **Confirmation:** read-only approval;
no permission to perform step 3 is implied.

### 3. Bounded history synchronization (local-state mutation)

**Operation:** present unknown count, then obtain confirmation to scan/apply exactly
that currently listed unknown set (maximum six). scan-prepare gives oldest-first
unknowns; select each exact identity-bound dated option and verify the selected
identity/route and resulting per-order view before scanning its products. Allow lazy-loaded cards to render, then
require two consecutive bottom passes revealing no new product IDs. Do not
scan the All orders union or assume separate order pages. scan-validate then sync-apply `--listed-orders <listed.json>
<scan.json>`. If there are no unknowns, record no-op summary, no invented import.

**Effect/evidence:** batch counts, product-presence count, stabilization result,
displayed-count warnings, sanitized summary checksum and idempotent dry-run count
zero. Binary presence only; never infer purchased units.
**Stop:** any missing stable product identity/failed scan; import none of the batch.
A displayed-count discrepancy warns, not discards stabilized IDs.
**Confirmation:** “Apply this bounded history batch to local SQLite?” separately
from discovery; this does not authorize any cart mutation.

### 4. Cart baseline read (no cart mutation)

**Operation:** obtain approval to create one local run using `weekly plan-prepare
--listed-orders <listed.json>` after successful sync (or explicitly approved
`--skip-history-sync`, with reason). Read the whole cart, emit private observation,
then `weekly baseline-apply <run-id> <cart.json>`.

**Effect/evidence:** run ID/lifecycle, baseline line count/quantities/subtotal,
positive recurring preview, no cart changes. Baseline/excess must be preserved.
**Stop:** competing run, stale/ambiguous identity, unexplained drift or history failure.
**Confirmation:** explicitly approve local run/baseline persistence; **no cart-write
approval** yet. Preview must fit the one-recurring-action smoke bound.

### 5. ONE recurring addition and readback (exact-action approval)

**Operation:** fresh complete cart and rendered recurring facts, waiting for the
full product purchase panel or the rendered-unavailable state as described in the
skill (title alone is not readiness); recurring-preflight.
Present the manifest details (product, URL, positive delta, price) and obtain
**explicit exact-action user approval** for this one addition before proceeding.
Immediately reread/re-preflight and compare; recurring-intent must return persisted
authorization for that exact checksum. Run `weekly action-check <run-id>
<manifest.json>` with that manifest immediately before the browser write; require
`status: authorized` (failure: no write, no intent retry). Add only its positive delta
through rendered stable product controls. Read the whole cart, submit
recurring-observe with `mutation_result: {manifest_checksum, outcome: readback}`
(or failed/incomplete, never pretend verification).

**Effect/evidence:** before/intended/observed quantity, unchanged baseline, price,
manifest checksum, verified reconciliation and user visual confirmation. Readback
identity comes from the product page's in-cart quantity stepper on the canonical
`/p/<id>` route, cross-checked against checkout line count, header badge, brand,
quantity and price; AB.gr checkout lines carry no product ID (see the skill's
Observation Protocol). The manifest checksum changes on every preflight because
it covers `generated_at`; the user approves the exact action content (product, URL,
quantity, price), and intent uses the freshly accepted manifest.
**Stop:** any discrepancy/failure; do not compensate. After this **one** action,
stop additions. Fresh preflight must show zero remaining additions to persist a
zero-addition recurring-intent (local-state only, no browser write). After
completion the run enters `awaiting-basket-review` (step 5.5). If nonempty, stop
before further additions outside this smoke; do not claim Stage1 complete.
**Confirmation:** explicit exact-action user approval for this one specific
addition (product, delta, price). The bounded smoke retains per-action approval
until owner acceptance of the autonomous stage-consent workflow; stage consent
is the pending normal-workflow mechanism, not a current smoke allowance.

### 5.5 Basket-review barrier and advance

**Operation:** After recurring completion, the run is in `awaiting-basket-review`.
The user may manually edit the cart (add/remove/adjust items). When ready, the
user provides a fresh whole-cart observation and runs
`grossy weekly advance <run-id> <cart-observation.json>`.

**Effect/evidence:** advance persists the post-edit cart as the dinner-planning
basis and transitions to `recurring-apply`. Without advance, recipes-apply fails
closed. The advance observation must be fresh (within five minutes). Manual review
edits are expected differences from the last verified observation; explicit advance
accepts them rather than treating them as drift.
**Stop:** stale, incomplete/failed or ambiguous cart evidence (any non-`readback`
`mutation_result` is rejected), or an unexpected/invalid lifecycle.
**Confirmation:** User explicitly invokes advance; no automatic transition.

### 6. Recipe preview (no ingredient addition)

**Operation:** approve local recipes-apply with closed recipe-options proposal from
accepted offline revisions/product facts only. No live recipe source fetch or
catalogue search. Review four stable slots, empty reasons, three-serving targets,
pantry/cart credits, matches, package assumptions and projected costs.

**Effect/evidence:** awaiting-recipe-selection, available slot/revision summary;
recurring evidence retained; no recipe cart change.
**Stop:** missing seed/eligible recipe, unresolved match or more than one missing
product action in the chosen recipe. Do not invent availability or package facts.
**Confirmation:** preview/persistence only, not selection/addition authorization.

### 7. ONE selected-recipe action and readback (exact-action approval)

**Operation:** explicitly approve exactly one persisted nonempty slot (not all);
selection-apply persists that selection and dinner1 mapping AND grants durable
run-scoped recipe stage consent. Fresh whole cart and rendered recipe/package
facts; recipe-preflight must classify this recipe passing and show only one
missing product action. Present identity/delta, conversion, substitution and
budget warning, then obtain **explicit exact-action user approval** for this one
addition. Immediately reread/re-preflight and compare the full cart/facts/
decisions. Persist recipe-intent, run `weekly action-check` with that manifest
immediately before the browser write (failure: no write), perform
that one positive action, read back whole cart and submit recipe-observe with
the exact checksum/outcome.

**Effect/evidence:** recipeN→dinner1, revision, target versus retail quantity,
before/intended/observed delta, reconciliation and visual confirmation. Whole-cart
baseline preserved; unrelated removed recurring items are not restored.
**Stop:** blocked recipe, changed facts, mismatch/failure. After **one** recipe action,
stop additions. If further missing deltas remain, stop for new authorization
outside this smoke; never silently loop. Zero-addition completion may persist
locally, with no further browser write, entering awaiting-user-verification.
**Confirmation:** explicit exact-action user approval for this one specific
recipe ingredient addition. The bounded smoke retains per-action approval until
owner acceptance of the autonomous stage-consent workflow; stage consent is the
pending normal-workflow mechanism, not a current smoke allowance. Consent
revocation stops all further writes; readback of a pending intent remains
possible. No additional ingredients inherit approval beyond this action.

### 8. Optional ONE bounded removal

**Operation:** user chooses whether safe; otherwise record a removal waiver with
reason and reference fixture evidence. If safe, choose one verified current-run
addition, quantity no greater than remaining attribution, **without**
`--include-preexisting`. Fresh whole-cart read, removal-preflight with
`--cart-observation`, present current-run split/before state, obtain **separate
removal confirmation**, immediately reread/re-preflight, then removal-intent's
interactive confirmation. Perform exactly one bounded decrement and submit whole
cart removal-observe with checksum/outcome.

**Effect/evidence:** authorized split, before/intended/observed quantity and preserved
baseline; no recurrence/history change. Wait until the product page, header badge
and checkout agree before readback; an early read records `incomplete` and a later
fresh readback bound to the same manifest reconciles it (see the skill). **Stop:** ambiguous attribution/drift,
partial result or failure. Do not retry or automatically re-add. Outstanding
removal needs explicit fresh removal readback reconciliation, not addition resume.
**Confirmation:** both explicit user permission and interactive CLI gate; no
pre-existing removal is included in this smoke authorization.

### 9. Controlled interruption and resume reconciliation

**Operation:** get explicit approval for a **safe no-cart-write interruption**: read fresh
cart/facts and prepare recipe preflight, then stop the coordinator/skill before
any new intent or browser write. Preserve existing run evidence, do not kill SQLite
mid-transaction. Restart only by explicit cart-read/local-state reconciliation approval: fresh
whole-cart/facts and `weekly resume-prepare <run-id> <fresh-cart.json>` using the
persisted selected Stage2 state. Never replay a previously issued manifest.

**Effect/evidence:** preserved run/selection, unchanged cart across interruption,
reconciled state, remaining positive-delta count, removed quantities not restored,
no automatic cart write. These preparation/reconciliation phases persist local
audit state; include that effect in the approval. If zero missing, accepted
zero-addition completion is local
only. If nonempty, **stop without intent/write**: the one-recurring/one-recipe
allowance is exhausted; additional live actions need a new separately reviewed
approval outside this smoke. A refusal/attention result is recorded honestly,
not overridden or described as successful recovery.
**Stop:** stale/incomplete evidence, baseline loss/unrelated changes or outstanding
removal. If interruption unexpectedly follows a durable intent, do not replay it;
use fresh readback reconciliation per operations/skill, preserve partial state,
and stop for user review. Mid-write interruption/retry safety remains fixture
proven, not claimed from this no-cart-write live interruption.
**Confirmation:** interruption scope, then separate resume cart-read/local-state reconciliation
approval; any future cart write requires a fresh distinct confirmation and intent.

## Evidence and handoff

Keep private operational artifacts at 0600 in 0700 directories. Human log per step:

```text
Step: <1..9>; UTC time: <instant>; run: <run-id if applicable>
Approved scope: <operation; exact product ID/delta only for cart mutation>
Confirmation: <explicit yes/no; no account identity or copied prompt data>
Effect: <read-only / local state / single add / single remove / no-op>
Evidence: <sanitized counts, lifecycle, before/intended/observed quantities,
           price/budget warning, manifest/report checksum, exit code>
Readback/user visual review: <pass/fail/not applicable>
Stop condition: <none or sanitized reason>; next action: <stop/review>
Removal waiver: <yes/no; safe reason if waived>
Interruption/resume: <boundary, result, remaining delta count; no replay>
```

Never attach source order IDs/order URLs, profile paths, DOM, screenshots, cookies,
headers, credentials, address/account/payment/fulfilment data or raw CLI traces
containing private phase outputs. Record only sanitized checksums/counts for
history. Check evidence before handoff; do not merely label it sanitized.

After all nine steps, present outcomes, waivers, blockers and residual risks to
the user. `weekly finish` (or `--no-recipes` in the supported lifecycle) records
review, never a purchase. Abandon preserves cart contents if completion is unsafe.
Both need explicit local-state closure approval. Acceptance, release metadata and
planning closure remain coordinator-owned; missing/live-blocked checks are pending,
not waived implicitly. No checkout/order claim is permitted.
