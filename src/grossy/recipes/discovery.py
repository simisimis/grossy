"""Recipe discovery: ordered source policy and explicit workflow (PRD FR-81..FR-85; task 08).

The discovery command reports the ordered source policy for explicit recipe
discovery. It does not fetch, generate, or substitute anything: it tells the
agent (or user) which sources to try and in what order, with the required
safety constraints (no access-control bypass, no silent AI generation).

Source preference affects import and discovery, not weekly recipe weighting
(FR-83). Preferred cuisines are a soft weighting preference, not an
eligibility restriction (FR-84, FR-85). Within the same source-quality
tier, discovery prefers sources that are accessible, complete,
appliance-native, and simple (FR-82).
"""

from __future__ import annotations

from dataclasses import dataclass

from grossy.recipes.proposal import PROTEIN_CLASSES

# Ordered source tiers (FR-81): the exact discovery order.
# Tier 1: Akis Petretzikis for Greek/European dishes.
# Tier 2: Cuisine-native sources for non-Greek specific dishes.
# Tier 3: Other approved established sources (Good Food, Food Network).
# Tier 4: User-authored recipes.
# Tier 5: AI generation only through explicit manual discovery when no sourced option works.
SOURCE_POLICY_ORDER: list[dict[str, str]] = [
    {
        "tier": "akis_petretzikis",
        "description": "Akis Petretzikis: Greek dishes, general "
        "European dishes, and equivalent variants",
        "scope": "greek, european",
    },
    {
        "tier": "cuisine_native",
        "description": "Recognized cuisine-native sources for non-Greek cuisine-specific dishes",
        "scope": "cuisine-specific",
    },
    {
        "tier": "approved_established",
        "description": "Other approved established sources (Good Food, Food Network)",
        "scope": "general",
    },
    {
        "tier": "user",
        "description": "User-authored recipes",
        "scope": "user-authored",
    },
    {
        "tier": "ai_generated",
        "description": "AI generation only through explicit manual "
        "discovery when no sourced option works",
        "scope": "last-resort",
        "constraint": "requires explicit user approval; never automatic",
    },
]

# Preferred cuisines (FR-84): soft weighting, not eligibility restriction.
PREFERRED_CUISINES: tuple[str, ...] = (
    "greek",
    "indonesian",
    "thai",
    "eastern_european",
    "german",
    "italian",
    "japanese",
    "mexican",
)


class DiscoveryError(Exception):
    """Raised when a discovery request is invalid."""


@dataclass(frozen=True)
class DiscoverResult:
    """Result of a recipe discovery policy query."""

    result: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return self.result


def run_recipes_discover(classes: str) -> DiscoverResult:
    """Report the ordered source policy for explicit recipe discovery.

    ``classes`` is a comma-separated subset of meat,fish,poultry,vegetarian
    or "all". The result lists the source tiers in order, the preferred
    cuisines, and the required safety constraints.

    This is a purely informational command: it performs no fetch, no
    generation, no state change, and no database access. The agent uses
    this output to guide its discovery behavior.
    """
    # Validate the class list
    if classes == "all":
        target_classes = list(PROTEIN_CLASSES)
    else:
        target_classes = [c.strip() for c in classes.split(",") if c.strip()]
        for c in target_classes:
            if c not in PROTEIN_CLASSES:
                raise DiscoveryError(
                    f"unknown protein class {c!r}; must be one of "
                    f"{', '.join(PROTEIN_CLASSES)} or 'all'"
                )
        if not target_classes:
            raise DiscoveryError("class list must not be empty")

    # Build the policy output
    payload: dict[str, object] = {
        "schema_name": "grossy.recipes.discover",
        "schema_version": 1,
        "status": "policy",
        "target_classes": target_classes,
        "source_policy": SOURCE_POLICY_ORDER,
        "within_tier_preferences": (
            "Within the same source-quality tier, prefer sources that are "
            "accessible, complete, appliance-native, and simple (FR-82)"
        ),
        "preferred_cuisines": list(PREFERRED_CUISINES),
        "cuisine_note": "Preferred cuisines are a soft weighting preference (FR-85), "
        "not an eligibility restriction. Non-preferred cuisines remain eligible.",
        "safety_constraints": [
            "Consent walls, authentication barriers, access controls, robots challenges, "
            "and anti-bot mechanisms must not be bypassed (FR-76)",
            "An inaccessible source must be replaced by another approved source or "
            "user-authored input (FR-77)",
            "No silent substitution or AI generation: a missing source is reported, "
            "not filled (FR-88)",
            "Source preference affects import and discovery, not weekly recipe weighting (FR-83)",
        ],
        "next_steps": [
            f"For each target class ({', '.join(target_classes)}), try sources in "
            "tier order until an eligible source is found; within a tier, prefer "
            "sources that are accessible, complete, appliance-native, and simple (FR-82)",
            "If a source is inaccessible, report it and move to the next tier",
            "If no sourced option works, report the gap; AI generation requires "
            "explicit user approval and is never automatic",
        ],
    }
    return DiscoverResult(result=payload)
