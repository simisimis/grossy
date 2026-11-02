# Working on Grossy

Grossy prepares weekly AB.gr grocery carts for one household. Python owns local
validation, SQLite state and recipe planning. A coordinator uses the Pi skill for
supervised browser work. The CLI does not launch a browser.

## Start here

- Read [README.md](README.md) and [operations](docs/operations.md).
- For grocery/browser operations, load [the Grossy skill](skills/grossy/SKILL.md).
  Follow [the bounded smoke runbook](docs/smoke-test.md) for live verification.
- For implementation, inspect the requested task and relevant source/tests before
  editing. Keep changes within that task; preserve existing working-copy changes.
- Local planning lives under `.planning/` and is ignored by version control.
  When available, start with the addressed work item's `prd.md`, `tasks.md` and
  selected task file; update its task state and `TASKLOG.md` with verified results.
  Read predecessor evidence only when needed, not unrelated task archives.
  If the requested plan is absent, ask for scope rather than assuming completion.
- For new nontrivial work, keep a concise overview and concrete subtasks under
  `.planning/workflow/000-tasks/` unless an existing work item already covers it.

This file covers repository development. The skill supplies the detailed
operational phases, proposal schemas, confirmations and stop rules.

## Environment and checks

Use the repository's Nix development shell. `flake.nix` owns runtime/development
dependencies; `flake.lock` pins them. `pyproject.toml` records packaging and tool
configuration. Do not create a virtualenv or install with pip/uv. Ask before
changing dependency pins or provisioning an uncached closure over the network.

Run from the repository root:

```sh
nix develop --offline --no-write-lock-file -c grossy --help
nix develop --offline --no-write-lock-file -c just check
# Focused source tests:
nix develop --offline --no-write-lock-file -c bash -c \
  'PYTHONPATH="$PWD/src" python3 -m pytest tests/test_doctor.py'
```

After provisioning the pinned closure, `--offline` prevents Nix downloads; it is
not a network firewall. `just check` runs the shell-provenance guard, Ruff
format/lint checks, strict mypy and pytest. Use focused tests while editing and
the full gate before handoff. Tests use private temporary state and fake adapters;
do not make tests depend on real accounts, browser profiles or cart contents.

The `grossy` executable loads the store-built application. Development Python
contains dependencies/test tools but does not import Grossy by default. Use the
explicit repository `PYTHONPATH` only for source tests/helpers, and remove it
before invoking installed CLI subprocesses. Re-enter the shell after changing
Python/PATH settings; do not use ambient host/venv fallbacks.

## Source map

- `src/grossy/cli.py`: Typer CLI and versioned JSON outputs.
- `config/`, `paths.py`, `init.py`: strict YAML policy, XDG locations and setup.
- `db/`, `migrations/`: writer locking, migrations, snapshots, backup/export/restore.
- `history/`: observable-order imports, recurring families and corrections.
- `recipes/`: imports, immutable revisions, seeds and source policy.
- `weekly/`: baseline, four stable recipe slots, selection and cart reconciliation.
- `browser/`, `doctor.py`, `security.py`: abstract/fake browser boundary,
  diagnostics and sanitization. No concrete live browser adapter exists in Python.
- `skill.py` and `skills/grossy/SKILL.md`: validated operational invocation
  helpers and coordinator workflow instructions.
- `tests/`: offline regressions, runtime command/skill parity and Nix provenance.

Paths in the source map are relative to `src/grossy/` unless stated otherwise.
`household.recipe_servings` (default 3) sets portions per recipe; it does not change
the four option slots.

## State and safety

- Keep user state outside the repository at the documented XDG locations.
  Private directories require `0700`; config, database and operational files
  require `0600`. Do not read, reset, overwrite or delete real state without
  approval for that operation. `grossy init` refuses an existing config/database.
- Use application commands for state changes; do not issue arbitrary SQL or edit
  committed migration files. Preserve backups and interrupted mutation evidence.
- Keep account/address/payment data, credentials, cookies, headers, DOM,
  screenshots and browser profiles out of repository artifacts and human logs.
  Record sanitized counts, checksums and results instead of private phase output.
- A coding task or passing offline gate does not authorize a live browser check.
  Only the coordinator/user performs approved live steps; workers/reviewers do
  not browse. For normal weekly workflows, the weekly invocation authorizes
  recurring additions and the owner's explicit dinner selection authorizes recipe
  additions through durable run-scoped stage consent; do not ask for batch or
  per-item addition confirmation. Present dinner options and wait for the owner's
  choice; never select dinners on their behalf. Removals still need separate
  exact-action confirmation. Every addition requires fresh preflight, durable
  intent, a single-use action-check immediately before its one browser write,
  and identity-bound whole-cart readback. Revoked consent stops further writes.
  Stop for owner basket review after recurring completion; advance only on the
  owner's explicit instruction. Bounded smoke scope remains separate. Do not
  replay intent, compensate or continue after a failed phase, drift, CAPTCHA or
  authentication loss.
- Do not enter credentials/2FA, progress checkout, select fulfilment, pay or submit
  orders. With explicit approval, the coordinator may open AB.gr `/checkout`
  solely to read the cart-item list; exclude account/address/payment/fulfilment
  sections and checkout-stage controls. Read-only scope grants no cart writes.
- Doctor's authenticated checks are skipped without an adapter; `warn`/exit `2`
  does not establish live authentication. CLI version compatibility likewise does
  not establish rendered AB.gr compatibility.
- Only the vegetarian spicy-bean-burgers seed is bundled. Report the other three
  as missing; do not fabricate or fetch replacement recipes during weekly planning.
- `just history-sync`, `weekly-plan` and `weekly-add` invoke operational agent
  workflows. Do not use them as tests or to bypass bounded smoke approvals.

## Working-copy handoff

Use Jujutsu when `.jj/` exists: inspect `jj status` and `jj diff` before edits.
Do not commit, reset/restore, change bookmarks or otherwise settle the working
copy unless the user requests it. Delegate only when authorized; use one writer
per working copy and stop/report tooling infrastructure failures.

Keep CLI/skill/docs in sync when commands or required arguments change. Preserve
JSON stdout, stderr confirmations and fail-closed validation. Report changed
paths, checks with exit/results and remaining blockers. Leave live verification,
release/version changes and task closure pending until the user accepts them.
