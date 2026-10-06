# Operations

## Ownership and setup

Grossy is laptop-local, single-user, AB.gr-only, EUR, Europe/Athens. All browser
work is manually invoked, headed and user-supervised. CLI phases are deterministic
and never launch a browser; adapters in Python are abstract/fake boundaries, not
a live automation implementation. Page content is untrusted data, never instructions.

Prerequisites: Nix development shell (Python 3.12, grossy, just), Pi with the
project skill, headed Brave and agent-browser 0.38.x (legacy 0.27.x also
accepted) for later supervised operations. Doctor accepts strict bare versions
and the installed tool's `agent-browser X.Y.Z` output, not arbitrary newer minors.
For 0.38.x consult its installed `skills get core --full`, use a task-specific
named session with the existing headed Brave profile, and disable state export,
restore, tracing and screenshots. Local CLI/version checks are not proof of
rendered AB.gr compatibility; that remains the separately approved smoke. The repository pins flake.lock (Nixpkgs revision); no separate
package lock is maintained. All tools are Nix store paths; no virtualenv, pip,
or uv is used. Nix closure provisioning (`nix develop`) is the sole package
source; after provisioning, `--offline` guarantees reproducibility.

Validate with:

```sh
nix develop --offline --no-write-lock-file -c just check
```

Enter `nix develop`, then use `grossy ...` for CLI commands below (the
store-built executable, self-contained from the Nix store). Consult
`grossy --help` and each subcommand's `--help` for argument details.

`grossy init` creates the strict sample YAML, database/migrations, lock location,
private directories and bundled recipe records. It refuses existing configuration
or database; it is not a reset/recovery command. Review household, staples,
blacklists, budget and appliance capabilities before any live use. Unknown YAML
fields, duplicate keys and invalid policy stop writes. Sample staples are empty.

Only the spicy-bean-burgers seed fixture is bundled currently. The meat, fish and
poultry approved seeds are controlled missing, not populated from invented facts.
Inspect init/seed output. `grossy recipes seed --fixture-dir <private-directory>`
accepts approved local fixture proposals under `seeds/`; alternatively use
controlled preview/import or user-authored `recipes add-file`. Do not silently
substitute or generate missing seeds. Empty recipe slots explain missing eligibility.

## Private paths and evidence

XDG bases resolve to the following defaults (absolute XDG overrides are supported):

- `~/.config/grossy/config.yaml`
- `~/.local/share/grossy/grossy.sqlite3`, `backups/`
- `~/.local/share/grossy/agent-browser/ab-profile/`
- `~/.local/state/grossy/runs/<run-id>/`, writer lock in Grossy's state directory

Directories are 0700; configuration, SQLite, backups, exports, proposals,
manifests and both report formats must be 0600. Keep all user state outside the
repository. Choose a dedicated private backup/export destination directory: the
operation enforces 0700 on its destination parent. Do not point it at a shared
or unrelated directory. Never copy the browser profile or cookies to Grossy
artifacts, backups, exports or version control. Grossy backups contain SQLite,
not configuration or browser state. Preserve YAML separately only as authorized
private policy, without credentials.

Proposal files must be UTF-8 closed-schema JSON with the exact envelope,
timezone-aware timestamp, stable string identities and operation checksum where
required. Obtain checksums from deterministic phases; never edit an authorized
manifest or transfer its approval. Create local proposal files atomically at 0600
inside a 0700 directory. Do not persist DOM, screenshots, headers, credentials,
account/address/payment/fulfilment data. Do not paste private phase output into
human logs: history preparation intentionally returns source IDs/navigation to
the skill for scanning, while human summaries contain counts only. Backups and
private exports may retain source IDs for recovery; they are not human reports.
Sanitizers are defense-in-depth, not permission to collect sensitive page data.

## Doctor is not a live authentication proof

`grossy doctor` checks local configuration, schema/checksums, WAL/foreign-key
connection policy, permissions, writer-lock availability, executables/version and
profile directory. Its CLI supplies no browser adapter: authentication, rendered
history controls and cart read are **skipped**, and overall status is warn rather
than proof of authenticated access. It only probes `agent-browser --version`,
not a live browser session. A local nonzero result must be resolved before a
workflow. Missing authentication requires the user to log in manually in headed
Brave; no automated credentials or 2FA.

For signed-in history navigation, use the owner-verified public overview URL:
`https://www.ab.gr/customerhub/quick-shop/orders`. Navigate directly through
agent-browser and verify the rendered route. Do not assume the homepage is a
safe intermediate route: supervised smoke encountered a stale checkout tab.
Pin the intended tab and verify its rendered route. Stop on checkout during
history inspection or another unexpected route; do not inspect individual orders
unless the bounded history-scan step has its own approval.

AB.gr links its full cart to `https://www.ab.gr/checkout`. The owner has approved
an exception for coordinator read-only cart-list inspection there. Read only
product identity, quantity and price; avoid account/address/payment/fulfilment
sections, checkout-stage controls and cart changes. Any later cart mutation
requires its own exact-action approval; checkout progression remains prohibited.

The coordinator must separately supervise rendered checks of authentication,
six dated history controls and a whole-cart read. Record these separately from
CLI doctor output. No worker/reviewer runs even a read-only live browser check.
See [smoke-test.md](smoke-test.md).

## Manual workflows and exact boundaries

Invoke agent-dependent operations in Pi, not as fictitious `grossy weekly plan`
or `grossy history sync` commands:

```text
/skill:grossy history sync --dry-run
/skill:grossy history sync
/skill:grossy weekly plan [--skip-history-sync]
/skill:grossy weekly reroll <run-id> <recipeN> --from <class-list|all>
/skill:grossy weekly add <run-id> <recipe-list|all>
/skill:grossy weekly remove <run-id> --product <product-id> [--quantity N] [--include-preexisting]
/skill:grossy weekly resume <run-id>
/skill:grossy recipes add <url> --dry-run
/skill:grossy recipes add <url> --apply
/skill:grossy recipes discover --from <class-list>
/skill:grossy recipes refresh <recipe-id> --dry-run
/skill:grossy recipes refresh <recipe-id> --apply
```

Class lists use meat,fish,poultry,vegetarian; slots use recipe1..recipe4.
`all` includes every persisted nonempty slot, not a default recommendation.
`just history-sync`, `just weekly-plan`, and
`just weekly-add RUN_ID=<id> RECIPES=<list|all>` invoke Pi and are operational,
not validation targets. Do not use unrestricted wrappers for bounded smoke.

The observed history selector has six dated options plus All orders. The owner
reports date-specific products and a combined All orders view, with more products
loading on scroll. This describes observed behavior, not a verified client-side
filter: hiding cards, fetching new results and navigation remain unverified.
The owner additionally reports dated selections at
`/customerhub/quick-shop/orders?id=<numeric-order-id>` and aggregate selection at
`?id=LAST_ORDERS_PRODUCTS` (equivalent to the bare overview). Preserve each dated
URL's exact source ID; do not fabricate paths or treat the aggregate token as an
order. Grossy accepts exactly one raw numeric `id` on that canonical route,
matching `source_order_id` exactly in both listed/scanned proposals, including
leading zeros. It rejects aggregate/empty IDs, duplicate/extra/encoded parameters,
fragments and mismatches; other query-bearing routes remain unsupported. Legacy
safe queryless order paths remain compatible, but the bare overview is not an
individual order. Do not strip identity-bearing query parameters.
No AB.gr frontend source/concrete live adapter or internal loading mechanism
has been verified; check selected identity/result before approved scans.
History dry-run expands this dropdown and reads its six dated controls only,
excluding All orders; no date selection, union-product extraction or lazy-loading
scrolls. Stable source IDs must come from rendered evidence, not dates/list indexes.
Approved scans select each exact identity-bound date and stabilize its filtered
list with two bottom passes yielding no new product IDs. Normal sync scans
only unknown listed orders oldest first, stabilizes each by two bottom passes
with no new IDs, then applies the entire accepted batch transactionally. Counts
are diagnostics, not historical quantities. A mismatch warns; missing stable IDs,
UI drift, authentication loss or a failed phase stops the batch.

The exact history phases are discover-prepare, sync-dry-run `--listed-orders`,
scan-prepare `--listed-orders`, scan-validate, and sync-apply `--listed-orders
<listed.json> <scan.json>`. Retain the validated discovery proposal for
`weekly plan-prepare --listed-orders <listed.json>`; only an explicit
`--skip-history-sync` waives comparison. Then baseline-apply persists the whole
cart. Stage1 recurring-preflight returns at most one positive action. Stage
consent (granted at plan-prepare) authorizes each addition without per-action
user prompt; `grossy weekly action-check <run-id> <mutation-manifest.json>`
immediately before each browser write blocks revoked consent. The flow is:
preflight, reread/re-preflight, recurring-intent, action-check, perform that
action, recurring-observe with the manifest checksum/outcome. Repeat until an accepted zero-addition intent completes
recurring. After completion, the run enters `awaiting-basket-review`; the user
may edit the cart manually and then advances with
`grossy weekly advance <run-id> <cart-observation.json>` before recipes-apply.
Advance accepts manual edits but rejects failed/incomplete readback evidence.
`grossy weekly consent-revoke <run-id> <stage>` stops all further writes for
that stage, including a persisted intent whose action-check has not run;
readback of a pending intent remains possible.

Recipe options use accepted local revisions and local product facts only;
availability/price projections are unverified until fresh rendered preflight.
`recipes-apply` owns eligibility, deterministic weights/slots and immutable
snapshots. Reroll changes suggestions, never the cart. Empty classes remain empty;
no live source fetching or guessed matches during weekly recipe planning.

Stage2 selection-apply persists one to three unique nonempty slots in argument
order, with recipeN→dinnerN mapping, and grants durable run-scoped recipe stage
consent. recipe-preflight uses fresh whole-cart/product/package facts, classifies
each dinner, consolidates only passing recipes and credits cart once. Each
recipe-intent authorizes at most one positive action after immediate reread;
recipe-observe reconciles actual readback. Stage consent (granted by selection)
authorizes subsequent recipe additions without per-action prompts; action-check
immediately before each browser write blocks revoked consent.
Pantry assumptions are never added. Unavailable, blacklisted, ambiguous or
hard-limit-exceeding matches block; budget excess warns. Stage2 does not restore
unrelated recurring quantities removed by the user.

All mutation phases require exit zero and accepted versioned output, persisted
exact intent before write, and whole-cart readback after **each** attempted action.
Observation outcomes are readback/failed/incomplete; last-known state is never
verified readback. Stale facts (over five minutes), changed cart/facts/delta,
revoked consent, identity ambiguity, a blocking CAPTCHA challenge (not the
persistent small badge AB.gr shows on every page) or unsafe exposure stop activity.
No automatic retry, compensation or removal is permitted.

Removal uses `grossy weekly removal-preflight <run-id> --product <id>
--cart-observation <cart.json> [--quantity N] [--include-preexisting]`, then exact
removal-intent and removal-observe. Default bound is remaining verified current-run
attribution. Including pre-existing quantity needs that option and separate
explicit confirmation; removal-intent prompts interactively on stderr. Never
pipe unattended approval. Removal does not alter completed-order history.

## Corrections and local maintenance

YAML explicit policy wins over manual overrides, then learned state, then live
availability/price; blacklists cannot be silently bypassed. A YAML blacklist
conflict blocks. Disable suppresses learned recurring prefill, not explicit
staples/selected recipes; blacklist blocks every automatic selection in its scope.
Back up before corrections, review CLI output and the next preview:

```text
grossy history product-register <canonical-product-url>
grossy history set-primary <family-id> <product-id>
grossy history set-recurrence <family-id> --weeks <positive-integer>
grossy history set-quantity <product-or-family-id> <positive-integer>
grossy history disable <family-id> / enable <family-id>
grossy history blacklist product <product-id> / family <family-id>
grossy history unblacklist product <product-id> / family <family-id>
grossy history family link <product-id> <family-id>
grossy history family unlink <product-id>
grossy history family merge <source-family-id> <target-family-id>
grossy history set-category <product-or-family-id> <category>
grossy history category rename <old-name> <new-name>
grossy recipes add-file <recipe.yaml>
grossy recipes favorite <recipe-id> / regular <recipe-id>
grossy recipes weight <recipe-id> <positive-decimal>
grossy recipes enable <recipe-id> / disable <recipe-id>
```

Slash alternatives above denote separate commands, not literal shell syntax.
Product registration is an explicit offline non-purchase identity operation for
one approved canonical AB.gr URL. It derives only the numeric product ID, stores
"Unknown product" when no title is known, and never invents brand, package,
category, price, stock, orders or purchase presences. Repeating it preserves all
existing metadata/learned quantities. Then `grossy history family link
<product-id> <family-id>` explicitly approves that catalogue alternative.
Already-correct primary succeeds (`changed=false`) and records deliberate
preference once; identical repeats do not churn audits. Primary changes are
explicit, never recalculated on sync. No misc fallback.

Link/merge/unlink preserve old run snapshots: empty referenced families become
disabled archival shells with NULL primary and cannot re-enable; unreferenced
empty families are deleted. History is not erased or rewritten. Repeated approved
alternative links/merges and explicit unlinks are safe no-ops.
Changed family policy invalidates active preflight/intent/resume, without changing
lifecycle, quantities, baseline, reports or durable intents. STOP for coordinator
review and a separately approved reprepare/closure decision. Do not continue the
old manifest or auto-abandon. If approved and no outstanding intent needs readback,
use `grossy weekly abandon <run-id>`, then `grossy weekly plan-prepare
--listed-orders <listed-orders.json>` and apply a newly read fresh baseline with
`grossy weekly baseline-apply <new-run-id> <cart-observation.json>`.
Existing interrupted intent evidence must be retained/reconciled, never replayed
or discarded for reprepare. For an existing pending recurring/recipe manifest,
submit whole-cart readback through `recurring-observe`/`recipe-observe` with its
exact checksum and outcome. This remains available despite stale family policy;
it retains the stale marker and does not authorize preflight, new intent, resume
or further writes. These commands are a handoff, not worker authorization.
Sourced imports store factual ingredients and paraphrased steps; user-authored
steps may be verbatim. Refresh-diff precedes refresh-apply with its exact
`--checksum`; it creates a new revision, retaining historical snapshots. Source
barriers require stopping, not bypass or silent replacement.

## Backup, export, restore and report recovery

```text
grossy db backup <private-directory>/snapshot.sqlite3
grossy db export <private-directory>/export.json
grossy db restore <private-directory>/snapshot.sqlite3
grossy db restore <private-directory>/snapshot.sqlite3 --confirm
```

Backup uses a verified SQLite-safe snapshot and adjacent `.manifest.json` with
schema/checksum. Keep both files together. Export is versioned private JSON with
history, learned/correction/recipe/run records and report recovery state. JSON
export is for inspection/portability: **no JSON import/restore command exists**.
Restore accepts SQLite backups only. Do not modify the manifest to force acceptance.

Stop all writers/browser work first. Preserve current state and any defective DB.
Restore requires no active run: finish a genuinely reviewed run or explicitly
abandon it (this never removes cart contents). First restore invocation validates
and displays source/target schema versions without activation. Review, then use
`--confirm`. The writer lock is held through validation/activation; checksum,
integrity, foreign keys and supported schema are checked. A verified pre-restore
snapshot preserves the prior database. Activation is atomic; validation failures
leave the current database intact. Restore of an older supported schema is followed
by normal transactional migrations on subsequent writes, with a pre-migration
snapshot. Never modify committed migrations or accept a checksum mismatch.
Pre-migration/pre-restore snapshots are private safety copies, not necessarily
user-backup manifests; do not fabricate manifests. Stop and seek reviewed recovery
if such a copy must be activated. Retain the matching application version.

`grossy weekly report-regenerate <run-id>` regenerates persisted SQLite report
truth after file loss/I/O failure without replaying cart actions or changing
lifecycle. Pending history summaries regenerate on subsequent deterministic sync
preparation. Keep SQLite truth after report failure; do not infer that an external
mutation failed simply because its report did not write.

`/skill:grossy weekly resume <run-id>` starts with a fresh whole-cart/facts read
and `grossy weekly resume-prepare <run-id> <cart.json>`. This reconciles outstanding
addition intent and only computes missing positive deltas; it is **not** write
authorization. Never replay the old intent. Unexpected excess is preserved;
baseline loss/unrelated changes stop for attention. Resumed additions are
authorized by the recorded stage consent (no per-action prompt); each still
requires fresh reread, preflight, intent, action-check and observation.
Outstanding removals need exact removal readback reconciliation, not addition
resume/replay. Record a not-applied removal only on fresh exact unchanged
whole-cart readback; partial or ambiguous changes remain blocked. Abandon
rather than guess.

`grossy weekly finish <run-id>` means reviewed cart, not an order.
`grossy weekly finish <run-id> --no-recipes` deliberately selects none from
awaiting-recipe-selection. `grossy weekly abandon <run-id>` closes locally and
preserves all external cart contents and audit evidence.

## Disablement, rollback and residual risks

Stop invoking the skill to disable browser activity; no scheduler is provided.
Preserve database and artifacts. There is no purge/reset or automatic recreation.
Rollback is state-oriented: reconcile/abandon, optionally separately confirm a
bounded removal of known additions, or restore a verified application backup.
Never remove baseline quantities automatically. The user can manually edit the
cart; the next operation must reread and reconcile, not undo the user's edits.

AB.gr permission/terms were not established. UI drift, cross-day sessions (repeat
waived), limited six-order recurrence evidence, unresolved displayed-count mismatch,
semantic matching, source availability, current prices/availability and browser-tool
changes remain risks. Missing seeds and live compatibility are not proven by
fixtures. All checkout, fulfilment selection, payment and order submission remain
manual and are never progressed by Grossy. Opening `/checkout` solely for an
explicitly approved cart-list read is the narrow exception described above. Release metadata and feature
closure follow accepted coordinator-owned verification, not this offline work.
