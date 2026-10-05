"""Grammar for the manual Pi skill invocations wired by the Justfile.

The ``weekly-add`` operational target implements the documented invocation
``just weekly-add RUN_ID=<id> RECIPES=<recipe-list|all>`` (PRD 16.2). Just
passes both ``NAME=value`` tokens as positional recipe arguments; this module
parses and validates them before the values reach the ``pi`` process, so
hostile argument strings can never be shell-evaluated or reach the skill
prompt. The bare positional form (``just weekly-add <id> <recipe-list|all>``)
is also accepted.

Execution: the Justfile invokes this module via
``PYTHONPATH="$PWD/src" python3 -m grossy.skill <RUN_ID> <RECIPES>``
inside the Nix development shell. No virtualenv, pip, or uv is used.

PRD 9.2: ``<recipe-list>`` is a comma-separated list of unique persisted
non-empty recipe slots; ``all`` means all persisted non-empty slots in stable
slot order. Slot persistence is a runtime concern of a later task; this
module validates the argument grammar only.
"""

from __future__ import annotations

import re
import sys

RECIPE_SLOTS: tuple[str, ...] = ("recipe1", "recipe2", "recipe3", "recipe4")
WEEKLY_ADD_PROMPT_TEMPLATE = "/skill:grossy weekly add {run_id} {recipes}"

# Run IDs are plain tokens (for example ``run-1``): no whitespace, shell
# metacharacters, or path separators. A leading dash is rejected so a run ID
# can never be mistaken for an option.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class SkillArgumentError(ValueError):
    """An operational argument violates the skill invocation grammar."""


def _strip_named_value(raw: str, name: str) -> str:
    """Accept both the documented ``NAME=value`` form and the bare value."""
    prefix = f"{name}="
    value = raw[len(prefix) :] if raw.startswith(prefix) else raw
    if not value:
        raise SkillArgumentError(f"{name} requires a non-empty value")
    return value


def parse_run_id(raw: str) -> str:
    """Validate a weekly run ID and return its bare value."""
    value = _strip_named_value(raw, "RUN_ID")
    if not _RUN_ID_RE.fullmatch(value):
        raise SkillArgumentError(f"invalid run ID {value!r}: expected [A-Za-z0-9][A-Za-z0-9_-]*")
    return value


def parse_recipe_list(raw: str) -> tuple[str, ...]:
    """Validate a weekly-add recipe list and return the accepted slots."""
    value = _strip_named_value(raw, "RECIPES")
    if value == "all":
        return ("all",)
    slots = value.split(",")
    for slot in slots:
        if not slot:
            raise SkillArgumentError(
                f"invalid recipe list {value!r}: empty slot entry; expected "
                f"'all' or a comma-separated list of {', '.join(RECIPE_SLOTS)}"
            )
        if slot not in RECIPE_SLOTS:
            raise SkillArgumentError(
                f"invalid recipe list {value!r}: unknown slot {slot!r}; expected "
                f"'all' or a comma-separated list of {', '.join(RECIPE_SLOTS)}"
            )
    if len(set(slots)) != len(slots):
        raise SkillArgumentError(f"invalid recipe list {value!r}: duplicate slot entries")
    return tuple(slots)


def weekly_add_prompt(run_id_raw: str, recipes_raw: str) -> str:
    """Return the ``weekly add`` skill prompt after validating both arguments."""
    run_id = parse_run_id(run_id_raw)
    recipes = parse_recipe_list(recipes_raw)
    return WEEKLY_ADD_PROMPT_TEMPLATE.format(run_id=run_id, recipes=",".join(recipes))


def main(argv: list[str]) -> int:
    """Validate a ``weekly add`` invocation and print the skill prompt.

    Used by the Justfile ``weekly-add`` target: the prompt is captured and
    passed to ``pi`` as a single argument; any grammar violation exits
    non-zero with a diagnostic on stderr before ``pi`` is ever launched.
    """
    if len(argv) != 2:
        print("usage: grossy.skill <RUN_ID> <RECIPES>", file=sys.stderr)
        return 2
    try:
        prompt = weekly_add_prompt(argv[0], argv[1])
    except SkillArgumentError as exc:
        print(f"grossy: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
