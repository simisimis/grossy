# Justfile for the Grossy project (PRD section 16.2).
#
# `just setup` is the prerequisite for every target: it creates the
# uv-managed virtualenv from the pinned uv.lock. Setup is the only target
# allowed to touch the network (initial dependency download) and it runs
# `uv sync --locked` so a stale or drifting lock fails instead of updating.
#
# Quality targets enforce offline and locked execution in the recipe itself
# (`--offline --frozen` on every `uv` call, plus `uv lock --check --offline`
# which proves the uv.lock is consistent with pyproject.toml; `--frozen`
# alone would skip that freshness validation). They never rely on inherited
# UV_* flags, never access live AB.gr, and never mutate user state.
#
# Operational targets are manual. They invoke the project Pi skill
# (.pi/skills/grossy/SKILL.md); the deterministic Python process never
# contains an agent.

default: check

setup:
    uv sync --locked

format:
    uv run --offline --frozen ruff format src tests
    uv run --offline --frozen ruff check --fix src tests

lint:
    uv run --offline --frozen ruff check src tests

typecheck:
    uv run --offline --frozen mypy

test:
    uv run --offline --frozen pytest

check:
    uv lock --check --offline
    uv run --offline --frozen ruff format --check src tests
    uv run --offline --frozen ruff check src tests
    uv run --offline --frozen mypy
    uv run --offline --frozen pytest

# --- manual operational targets (PRD 16.2) ---

history-sync:
    pi -p "/skill:grossy history sync"

weekly-plan:
    pi -p "/skill:grossy weekly plan"

# Implements the documented invocation
#
#     just weekly-add RUN_ID=<id> RECIPES=<recipe-list|all>
#
# Just passes both `NAME=value` tokens as positional arguments to this
# recipe. User values never enter shell command text: they cross into the
# shell only through just's `quote()` (single-quoted, immune to command
# substitution and quote breakout), are validated by deterministic code
# (grossy.skill) which must succeed for the `pi` invocation to run, and `pi`
# receives the prompt as a single argument. The bare positional form
# (`just weekly-add <id> <recipe-list|all>`) is also accepted.
weekly-add RUN_ID RECIPES:
    GROSSY_WEEKLY_ADD_PROMPT="$(uv run --offline --frozen python -m grossy.skill {{ quote(RUN_ID) }} {{ quote(RECIPES) }})" && pi -p "$GROSSY_WEEKLY_ADD_PROMPT"
