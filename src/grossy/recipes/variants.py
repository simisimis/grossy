"""Appliance capability equivalence and variant preference resolution (PRD FR-93..FR-102; task 08).

Appliance capabilities define which configured appliances satisfy a recipe's
requirements. The equivalence rules (FR-94) are:
- steam oven satisfies conventional oven
- induction cooktop satisfies stovetop
- Thermomix supplies mixing, blending, chopping, kneading, cooking, and steaming

Optional equipment metadata (FR-96) — Thermomix model, air-fryer capacity,
microwave wattage, pressure-cooker kind and capacity, Dutch-oven capacity —
is preserved from the configuration. Missing or incompatible metadata leaves
a model- or capacity-dependent requirement unmet: missing metadata permits
only model- and capacity-independent automatic choices (FR-97).

Variant preference (FR-100): within linked variants of the same dish,
selection prefers:
1. approved air-fryer variant over pan-fried variant
2. approved Thermomix-native variant over manual variant

Only linked variants that are eligible with the configured appliances
(availability plus model/capacity metadata) are considered; when none is
eligible the base recipe is used, so an unavailable variant never displaces
it (FR-101: other appliances add no preference). Resolution is a read-only
query that occurs before recipe weighting (FR-102). A conventional recipe is
never silently rewritten (FR-98): appliance adaptations must be separately
previewed, explicitly approved, and stored as linked variants or revisions
with their own data (FR-99).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from grossy.recipes.repository import get_revision_snapshot

# Capabilities a Thermomix supplies (FR-94).
THERMOMIX_CAPABILITIES: tuple[str, ...] = (
    "mixing",
    "blending",
    "chopping",
    "kneading",
    "cooking",
    "steaming",
)

# Appliance capability equivalence (FR-94): maps a required appliance type
# to the configured appliance types that can satisfy it.
CAPABILITY_EQUIVALENCE: dict[str, tuple[str, ...]] = {
    # Conventional oven can be satisfied by a steam oven (FR-94)
    "conventional_oven": ("steam_oven", "conventional_oven"),
    # Stovetop can be satisfied by an induction cooktop (FR-94)
    "stovetop": ("induction_cooktop", "stovetop"),
    # Thermomix supplies multiple capabilities (FR-94)
    **{capability: ("thermomix",) for capability in THERMOMIX_CAPABILITIES},
}

# Configured appliances (FR-93) — the canonical set.
CONFIGURED_APPLIANCES: tuple[str, ...] = (
    "thermomix",
    "dough_kneading_machine",
    "steam_oven",
    "air_fryer",
    "induction_cooktop",
    "microwave",
    "pressure_cooker",
    "dutch_oven",
)

# Assumed ordinary equipment (FR-95): never a blocking requirement.
ASSUMED_EQUIPMENT: tuple[str, ...] = (
    "knife",
    "cutting_board",
    "pot",
    "pan",
    "tray",
    "bowl",
    "utensils",
    "refrigerator",
    "freezer",
)

# Variant preference order (FR-100): lower index = higher preference.
VARIANT_PREFERENCE_ORDER: tuple[str, ...] = (
    "air_fryer",  # preferred over pan-fried
    "thermomix_native",  # preferred over manual
    "other",
)

# Capacity units the configured metadata can express (FR-96): the config
# stores capacities in litres, so only litre spellings are comparable.
SUPPORTED_CAPACITY_UNITS: tuple[str, ...] = ("l", "litre", "litres")


class VariantResolutionError(Exception):
    """Raised when variant resolution fails."""


@dataclass(frozen=True)
class ApplianceConfig:
    """One configured appliance: availability plus optional metadata.

    The metadata (FR-96) is optional: a missing value permits only model-
    and capacity-independent automatic choices (FR-97).
    """

    available: bool
    model: str | None = None
    capacity_litres: float | None = None
    wattage: float | None = None
    kind: str | None = None


def _as_float(value: Any) -> float | None:
    """Numeric config values (YAML numerals or Decimals) as float, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def _as_str(value: Any) -> str | None:
    """String config values as str, else None (fail closed on wrong types)."""
    return value if isinstance(value, str) else None


def get_appliance_config_from_yaml(
    appliances: dict[str, Any],
) -> dict[str, ApplianceConfig]:
    """Extract the per-appliance configuration from the YAML config.

    The config structure is:
    ```yaml
    appliances:
      thermomix:
        available: true
        model: TM6
      air_fryer:
        available: true
        capacity_litres: 4
    ```

    Returns ``{appliance_type: ApplianceConfig}`` preserving the optional
    model/capacity metadata (FR-96) so model- or capacity-dependent
    requirements can be checked (FR-97).
    """
    result: dict[str, ApplianceConfig] = {}
    for appliance_type in CONFIGURED_APPLIANCES:
        entry = appliances.get(appliance_type)
        if isinstance(entry, dict):
            result[appliance_type] = ApplianceConfig(
                available=bool(entry.get("available", False)),
                model=_as_str(entry.get("model")),
                capacity_litres=_as_float(entry.get("capacity_litres")),
                wattage=_as_float(entry.get("wattage")),
                kind=_as_str(entry.get("kind")),
            )
        else:
            result[appliance_type] = ApplianceConfig(available=False)
    return result


def _candidate_types(required_type: str, required_capability: str) -> tuple[str, ...]:
    """Configured appliance types that can satisfy the requirement (FR-94).

    The directly named appliance comes first, then the equivalent
    appliances (steam oven for conventional oven, induction cooktop for
    stovetop), then the Thermomix when it supplies the required capability.
    """
    candidates: list[str] = [required_type]
    for candidate in CAPABILITY_EQUIVALENCE.get(required_type, ()):
        if candidate != required_type and candidate not in candidates:
            candidates.append(candidate)
    if required_capability in THERMOMIX_CAPABILITIES and "thermomix" not in candidates:
        candidates.append("thermomix")
    return tuple(candidates)


def appliance_satisfies_requirement(
    required_type: str,
    required_capability: str,
    configured: dict[str, ApplianceConfig],
) -> bool:
    """Check whether some configured appliance is available to satisfy a
    recipe requirement (availability only, FR-94 equivalence applied).

    Model/capacity constraints are not considered here; use
    :func:`check_appliance_eligibility` for the full FR-97 check.
    """
    for candidate in _candidate_types(required_type, required_capability):
        cfg = configured.get(candidate)
        if cfg is not None and cfg.available:
            return True
    return False


def _metadata_problem(
    candidate_type: str,
    model_constraint: str | None,
    capacity_constraint: float | None,
    capacity_unit: str | None,
    cfg: ApplianceConfig,
) -> str | None:
    """None when the candidate's metadata satisfies the requirement.

    A model or capacity constraint with missing or incompatible metadata is
    a problem: missing metadata permits only model- and capacity-independent
    automatic choices (FR-97).
    """
    if model_constraint is not None:
        if cfg.model is None:
            return (
                f"{candidate_type} has no model metadata, so the model "
                f"constraint {model_constraint!r} cannot be satisfied (FR-97)"
            )
        if cfg.model.strip().lower() != model_constraint.strip().lower():
            return (
                f"{candidate_type} model {cfg.model!r} does not match the "
                f"required model {model_constraint!r}"
            )
    if capacity_constraint is not None:
        unit = (capacity_unit or "").strip().lower()
        if unit not in SUPPORTED_CAPACITY_UNITS:
            return (
                f"capacity unit {capacity_unit!r} is not supported by the "
                "configured metadata, which stores litres (FR-96)"
            )
        if cfg.capacity_litres is None:
            return (
                f"{candidate_type} has no capacity metadata, so the capacity "
                f"constraint {capacity_constraint:g} L cannot be satisfied (FR-97)"
            )
        if cfg.capacity_litres < capacity_constraint:
            return (
                f"{candidate_type} capacity {cfg.capacity_litres:g} L is below "
                f"the required {capacity_constraint:g} L"
            )
    return None


def _unmet_reason(
    required_type: str,
    required_capability: str,
    model_constraint: Any,
    capacity_constraint: Any,
    capacity_unit: Any,
    configured: dict[str, ApplianceConfig],
) -> str | None:
    """None when some available candidate satisfies the requirement fully."""
    model = _as_str(model_constraint)
    if model is not None and not model.strip():
        model = None
    if isinstance(capacity_constraint, bool) or not isinstance(
        capacity_constraint, (int, float, Decimal)
    ):
        capacity = None
    else:
        capacity = float(capacity_constraint)
    unit = _as_str(capacity_unit)

    available = [
        (candidate, cfg)
        for candidate in _candidate_types(required_type, required_capability)
        if (cfg := configured.get(candidate)) is not None and cfg.available
    ]
    if not available:
        return (
            f"no configured appliance is available for type {required_type!r} "
            f"(capability {required_capability!r})"
        )
    problems = [
        _metadata_problem(candidate, model, capacity, unit, cfg) for candidate, cfg in available
    ]
    if any(problem is None for problem in problems):
        return None
    return problems[0]


def check_appliance_eligibility(
    requirements: list[dict[str, Any]],
    configured: dict[str, ApplianceConfig],
) -> list[dict[str, str]]:
    """Check all appliance requirements against the configured appliances.

    Returns a list of unmet requirements (empty = all satisfied), each
    reported with its type, capability, and reason. A requirement is met
    when at least one available candidate appliance (FR-94 equivalence
    included) also satisfies any model/capacity constraint; missing or
    incompatible metadata leaves the requirement unmet (FR-97).
    """
    unmet: list[dict[str, str]] = []
    for req in requirements:
        req_type = str(req.get("type", ""))
        req_cap = str(req.get("capability", ""))
        reason = _unmet_reason(
            req_type,
            req_cap,
            req.get("model_constraint"),
            req.get("capacity_constraint"),
            req.get("capacity_unit"),
            configured,
        )
        if reason is not None:
            unmet.append({"type": req_type, "capability": req_cap, "reason": reason})
    return unmet


def resolve_variant_preference(
    conn: sqlite3.Connection,
    recipe_id: str,
    configured: dict[str, ApplianceConfig],
) -> dict[str, Any]:
    """Resolve the preferred linked variant for a recipe (FR-100, FR-102).

    Looks up the recipe's current revision's variant links and returns the
    preferred linked recipe according to the preference order (air-fryer
    over pan-fried, then Thermomix-native over manual) — but only among
    variants whose own appliance requirements are eligible with the
    configured appliances (availability plus model/capacity metadata). When
    no linked variant is eligible, the base recipe is used: an unavailable
    variant must not displace it.

    The resolution is a read-only query that occurs before recipe weighting
    (FR-102). A conventional recipe is never silently rewritten (FR-98): the
    variant is a separate linked recipe with its own data.
    """
    row = conn.execute("SELECT r.id FROM recipes r WHERE r.recipe_id = ?", (recipe_id,)).fetchone()
    if row is None:
        raise VariantResolutionError(f"unknown recipe {recipe_id!r}")
    recipe_row_id = int(row[0])

    # Get the current revision's variant links, in preference order.
    variants = conn.execute(
        "SELECT v.linked_recipe_id, v.variant_type, l.recipe_id as linked_id "
        "FROM recipe_variants v "
        "JOIN recipes l ON l.id = v.linked_recipe_id "
        "WHERE v.revision_id = (SELECT current_revision_id FROM recipes WHERE id = ?) "
        "ORDER BY CASE v.variant_type "
        "WHEN 'air_fryer' THEN 0 "
        "WHEN 'thermomix_native' THEN 1 "
        "ELSE 2 END",
        (recipe_row_id,),
    ).fetchall()

    if not variants:
        return {
            "recipe_id": recipe_id,
            "preferred_variant": None,
            "variant_type": None,
            "resolution": "no_variants",
            "detail": "no linked variants; the recipe itself is used",
        }

    for variant in variants:
        variant_type = str(variant[1])
        linked_id = str(variant[2])
        linked_row = conn.execute(
            "SELECT current_revision_id FROM recipes WHERE recipe_id = ?",
            (linked_id,),
        ).fetchone()
        if linked_row is None or linked_row[0] is None:
            continue
        snapshot = get_revision_snapshot(conn, int(linked_row[0]))
        if not check_appliance_eligibility(snapshot["appliances"], configured):
            return {
                "recipe_id": recipe_id,
                "preferred_variant": linked_id,
                "variant_type": variant_type,
                "resolution": "variant_preferred",
                "detail": (
                    f"preferred {variant_type} variant {linked_id} over base recipe "
                    "(eligible with the configured appliances)"
                ),
            }

    return {
        "recipe_id": recipe_id,
        "preferred_variant": None,
        "variant_type": None,
        "resolution": "base_recipe",
        "detail": (
            "no linked variant is eligible with the configured appliances; the base recipe is used"
        ),
    }
