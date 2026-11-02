# All targets run inside the Nix development shell (`nix develop`). The shell
# provides grossy (store-built), python3.12 with dependencies, ruff, mypy,
# pytest, and just. No target creates a virtualenv, downloads packages, or
# touches the network.
#
# `just setup` verifies shell provenance: it confirms that the required tools
# match this project's declared Nix closure and no ambient Python environment (PYTHONHOME,
# VIRTUAL_ENV, user-site) is active.
#
# Quality targets use Nix store tools directly
# (ruff, mypy, pytest) without any network access. Ruff covers the package
# source, the tests and the coordinator skill scripts. Source tests set PYTHONPATH
# explicitly to the repository src/ directory so grossy modules resolve from
# source while third-party dependencies resolve from the Nix store.
#
# Operational targets are manual. They invoke the project Pi skill
# (skills/grossy/SKILL.md); the deterministic Python process never
# contains an agent.

# pytest-xdist worker count: one per CPU by default. Override with
# GROSSY_TEST_WORKERS or `just test_workers=2 check`; 0 runs serially.
test_workers := env("GROSSY_TEST_WORKERS", "auto")

default: check

# Shared dependency: compare PATH tools to this project's declared shell closure.
# Source-test subprocesses may inherit only the explicitly bounded source path.
setup:
    @if [ "${GROSSY_SHELL_PROJECT:-}" != "grossy-python312-v1" ]; then echo "ERROR: enter the Grossy Nix development shell"; exit 1; fi
    @if [ -n "${VIRTUAL_ENV:-}" ]; then echo "ERROR: VIRTUAL_ENV is set; deactivate it"; exit 1; fi
    @if [ -n "${PYTHONHOME:-}" ]; then echo "ERROR: PYTHONHOME is set; unset it"; exit 1; fi
    @if [ -n "${__PYVENV_LAUNCHER__:-}" ]; then echo "ERROR: __PYVENV_LAUNCHER__ is set; deactivate venv"; exit 1; fi
    @if [ "${PYTHONNOUSERSITE:-}" != "1" ] && [ "${PYTHONNOUSERSITE:-}" != "true" ]; then echo "ERROR: user-site must be disabled"; exit 1; fi
    @if [ -n "${PYTHONPATH:-}" ] && [ "$PYTHONPATH" != "$PWD/src" ]; then echo "ERROR: unexpected PYTHONPATH"; exit 1; fi
    @if echo "${PATH}" | tr ':' '\n' | grep -q '\.venv/bin'; then echo "ERROR: .venv/bin found in PATH"; exit 1; fi
    @for tool in grossy python3 ruff mypy pytest just; do \
        case "$tool" in \
            grossy) expected="${GROSSY_SHELL_GROSSY:-}" ;; \
            python3|mypy|pytest) expected="${GROSSY_SHELL_PYTHON:-}/bin/$tool" ;; \
            ruff) expected="${GROSSY_SHELL_RUFF:-}" ;; \
            just) expected="${GROSSY_SHELL_JUST:-}" ;; \
        esac; \
        case "$expected" in /nix/store/*) ;; *) echo "ERROR: missing project shell declaration for $tool"; exit 1 ;; esac; \
        path=$(command -v "$tool") || exit 1; \
        if [ "$path" != "$expected" ]; then echo "ERROR: $tool resolves to '$path', not declared project shell tool '$expected' (not a Nix store path or wrong closure)"; exit 1; fi; \
    done
    @echo "Shell provenance OK: declared Grossy closure, no ambient Python environment."

format: setup
    ruff format src tests skills/grossy/scripts
    ruff check --fix src tests skills/grossy/scripts

lint: setup
    ruff check src tests skills/grossy/scripts

typecheck: setup
    mypy

# Source-tree tests: PYTHONPATH points to the repository src/ so grossy
# modules resolve from source while third-party deps come from the Nix store.
test: setup
    PYTHONPATH="$PWD/src" python3 -m pytest -n {{ quote(test_workers) }}

# Same complete suite without xdist worker processes, useful for debugging.
test-serial: setup
    PYTHONPATH="$PWD/src" python3 -m pytest -n 0

check: setup
    ruff format --check src tests skills/grossy/scripts
    ruff check src tests skills/grossy/scripts
    mypy
    PYTHONPATH="$PWD/src" python3 -m pytest -n {{ quote(test_workers) }}

# --- manual operational targets (PRD 16.2) ---

history-sync: setup
    pi -p "/skill:grossy history sync"

weekly-plan: setup
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
weekly-add RUN_ID RECIPES: setup
    GROSSY_WEEKLY_ADD_PROMPT="$(PYTHONPATH="$PWD/src" python3 -m grossy.skill {{ quote(RUN_ID) }} {{ quote(RECIPES) }})" && pi -p "$GROSSY_WEEKLY_ADD_PROMPT"
