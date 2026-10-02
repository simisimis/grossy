"""Tests for appliance capability equivalence and variant preference (task 08).

Covers PRD FR-93..FR-102: appliance capability equivalence (steam oven
satisfies conventional oven, induction satisfies stovetop, Thermomix
capabilities), model/capacity metadata semantics (FR-96, FR-97), variant
preference resolution restricted to eligible variants (FR-100, FR-102),
conventional recipe protection (FR-98), and config extraction with metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from grossy.db.connection import open_connection
from grossy.init import do_init
from grossy.paths import GrossyPaths
from grossy.recipes import (
    ASSUMED_EQUIPMENT,
    CONFIGURED_APPLIANCES,
    VARIANT_PREFERENCE_ORDER,
    ApplianceConfig,
    VariantResolutionError,
    appliance_satisfies_requirement,
    check_appliance_eligibility,
    get_appliance_config_from_yaml,
    resolve_variant_preference,
    run_import_apply,
    run_import_validate,
)
from grossy.recipes.proposal import proposal_checksum

RUNNER = CliRunner(mix_stderr=False)
GENERATED_AT = "2026-09-26T12:00:00+00:00"


# --- appliance capability equivalence (FR-94) -------------------------------------------


@pytest.mark.parametrize(
    "configured_type,available,required_type,capability,expected",
    [
        ("steam_oven", True, "conventional_oven", "baking", True),
        ("induction_cooktop", True, "stovetop", "searing", True),
        ("thermomix", True, "thermomix", "mixing", True),
        ("thermomix", False, "stovetop", "mixing", False),
        ("thermomix", True, "air_fryer", "air_frying", False),
        ("air_fryer", True, "air_fryer", "air_frying", True),
        ("microwave", True, "air_fryer", "air_frying", False),
        ("air_fryer", False, "air_fryer", "air_frying", False),
        ("steam_oven", True, "air_fryer", "air_frying", False),
        ("induction_cooktop", True, "conventional_oven", "baking", False),
    ],
    ids=[
        "steam-oven-equivalence",
        "induction-equivalence",
        "thermomix-direct",
        "thermomix-unavailable",
        "thermomix-unsupported-capability",
        "air-fryer-direct",
        "missing-air-fryer",
        "unavailable-air-fryer",
        "steam-oven-not-air-fryer",
        "induction-not-oven",
    ],
)
def test_appliance_equivalence(
    configured_type: str, available: bool, required_type: str, capability: str, expected: bool
) -> None:
    configured = {configured_type: ApplianceConfig(available=available)}
    assert appliance_satisfies_requirement(required_type, capability, configured) is expected


@pytest.mark.parametrize(
    "capability", ["mixing", "blending", "chopping", "kneading", "cooking", "steaming"]
)
def test_thermomix_fallback_supplies_capability(capability: str) -> None:
    """Non-Thermomix requirement forces the capability-equivalence fallback."""
    configured = {"thermomix": ApplianceConfig(available=True)}
    assert appliance_satisfies_requirement("stovetop", capability, configured)


def test_all_configured_appliances_defined() -> None:
    """FR-93: The exact set of configured appliances is defined."""
    assert set(CONFIGURED_APPLIANCES) == {
        "thermomix",
        "dough_kneading_machine",
        "steam_oven",
        "air_fryer",
        "induction_cooktop",
        "microwave",
        "pressure_cooker",
        "dutch_oven",
    }


def test_assumed_equipment_is_defined() -> None:
    """FR-95: Ordinary equipment is assumed and never blocking."""
    assert "knife" in ASSUMED_EQUIPMENT
    assert "cutting_board" in ASSUMED_EQUIPMENT
    assert "refrigerator" in ASSUMED_EQUIPMENT
    assert "freezer" in ASSUMED_EQUIPMENT


# --- check_appliance_eligibility ----------------------------------------------------------


def test_all_requirements_satisfied() -> None:
    """When all requirements are met, no unmet items are reported."""
    requirements = [
        {"type": "stovetop", "capability": "searing"},
        {"type": "conventional_oven", "capability": "baking"},
    ]
    configured = {
        "induction_cooktop": ApplianceConfig(available=True),
        "steam_oven": ApplianceConfig(available=True),
    }
    unmet = check_appliance_eligibility(requirements, configured)
    assert unmet == []


def test_unmet_requirement_reported_with_reason() -> None:
    """Unmet requirements are reported with type, capability, and reason."""
    requirements = [{"type": "air_fryer", "capability": "air_frying"}]
    configured = {"stovetop": ApplianceConfig(available=True)}
    unmet = check_appliance_eligibility(requirements, configured)
    assert len(unmet) == 1
    assert unmet[0]["type"] == "air_fryer"
    assert unmet[0]["capability"] == "air_frying"
    assert "no configured appliance is available" in unmet[0]["reason"]


def test_mixed_met_and_unmet() -> None:
    """Only unmet requirements appear in the result."""
    requirements = [
        {"type": "stovetop", "capability": "searing"},
        {"type": "pressure_cooker", "capability": "pressure_cooking"},
    ]
    configured = {"induction_cooktop": ApplianceConfig(available=True)}
    unmet = check_appliance_eligibility(requirements, configured)
    assert len(unmet) == 1
    assert unmet[0]["type"] == "pressure_cooker"


# --- model/capacity metadata semantics (FR-96, FR-97) -------------------------------------


@pytest.mark.parametrize(
    "capacity,required,unit,error",
    [
        (4.0, 4.0, "litres", None),
        (6.0, 4.0, "litres", None),
        (4.0, 5.0, "litres", "below the required"),
        (None, 4.0, "litres", "no capacity metadata"),
        (6.0, 2.0, "portions", "not supported"),
    ],
    ids=["equal", "above", "below", "missing-metadata", "unsupported-unit"],
)
def test_capacity_constraint(
    capacity: float | None, required: float, unit: str, error: str | None
) -> None:
    requirements = [
        {
            "type": "air_fryer",
            "capability": "air_frying",
            "capacity_constraint": required,
            "capacity_unit": unit,
        }
    ]
    configured = {"air_fryer": ApplianceConfig(available=True, capacity_litres=capacity)}
    unmet = check_appliance_eligibility(requirements, configured)
    if error is None:
        assert unmet == []
    else:
        assert len(unmet) == 1
        assert error in unmet[0]["reason"]


def test_no_constraint_remains_capacity_independent() -> None:
    """FR-97: Without a constraint, availability alone is sufficient."""
    requirements = [{"type": "air_fryer", "capability": "air_frying"}]
    configured = {"air_fryer": ApplianceConfig(available=True)}
    assert check_appliance_eligibility(requirements, configured) == []


@pytest.mark.parametrize(
    "model,error",
    [("TM6", None), ("TM5", "does not match"), (None, "no model metadata")],
    ids=["matching", "mismatched", "missing-metadata"],
)
def test_model_constraint(model: str | None, error: str | None) -> None:
    requirements = [{"type": "thermomix", "capability": "mixing", "model_constraint": "TM6"}]
    configured = {"thermomix": ApplianceConfig(available=True, model=model)}
    unmet = check_appliance_eligibility(requirements, configured)
    if error is None:
        assert unmet == []
    else:
        assert len(unmet) == 1
        assert error in unmet[0]["reason"]


def test_pressure_cooker_capacity_unit_semantics() -> None:
    """FR-96: Pressure-cooker capacity constraint with uppercase 'L' unit is met.

    Verifies that 'L' is accepted as a supported capacity unit (case-insensitive
    matching against SUPPORTED_CAPACITY_UNITS). The kind metadata is preserved
    in the config but is not independently checked by the eligibility check.
    """
    requirements = [
        {
            "type": "pressure_cooker",
            "capability": "pressure_cooking",
            "capacity_constraint": 6.0,
            "capacity_unit": "L",
        }
    ]
    met = {
        "pressure_cooker": ApplianceConfig(available=True, kind="instant_pot", capacity_litres=6.0)
    }
    assert check_appliance_eligibility(requirements, met) == []
    too_small = {
        "pressure_cooker": ApplianceConfig(available=True, kind="instant_pot", capacity_litres=4.5)
    }
    unmet = check_appliance_eligibility(requirements, too_small)
    assert len(unmet) == 1
    assert unmet[0]["type"] == "pressure_cooker"


# --- variant preference order (FR-100) -----------------------------------------------------


def test_variant_preference_order() -> None:
    """FR-100: Air-fryer is preferred over Thermomix-native, which is preferred over other."""
    assert VARIANT_PREFERENCE_ORDER == ("air_fryer", "thermomix_native", "other")


# --- variant resolution with database -------------------------------------------------------


def _make_recipe_payload(recipe_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "recipe_id": recipe_id,
        "title": f"Test {recipe_id}",
        "reference_url": f"https://example.com/recipe/{recipe_id}",
        "source_name": "Test Source",
        "source_tier": "approved_established",
        "cuisine": "Italian",
        "protein_class": "poultry",
        "total_time_minutes": 30,
        "servings": 2.0,
        "ingredients": [
            {"name": "chicken", "amount": 500.0, "unit": "g"},
        ],
        "steps": ["Cook the chicken"],
        "appliances": [{"type": "stovetop", "capability": "searing"}],
        "variants": [],
        "base_weight": 1.0,
        "is_favorite": False,
        "is_regular": True,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _import_recipe(tmp_path: Path, paths: GrossyPaths, recipe_id: str, **overrides: Any) -> None:
    """Import a recipe into the test database."""
    payload = _make_recipe_payload(recipe_id, **overrides)
    proposal: dict[str, Any] = {
        "schema_name": "grossy.recipe.import.v1",
        "schema_version": 1,
        "generated_at": GENERATED_AT,
        "recipe": payload,
    }
    checksum_source = {k: v for k, v in proposal.items() if k != "checksum"}
    proposal["checksum"] = proposal_checksum(checksum_source)

    import_path = tmp_path / f"{recipe_id}.json"
    import_path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
    checksum = run_import_validate(import_path).to_dict()["proposal_checksum"]
    run_import_apply(import_path, checksum, paths)


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, GrossyPaths]:
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    do_init(paths)
    return tmp_path, paths


def test_variant_resolution_no_variants(env: tuple[Path, GrossyPaths]) -> None:
    """A recipe with no variants resolves to itself."""
    tmp_path, paths = env
    _import_recipe(tmp_path, paths, "base-recipe")

    configured = {"stovetop": ApplianceConfig(available=True)}
    conn = open_connection(paths.database_file)
    try:
        result = resolve_variant_preference(conn, "base-recipe", configured)
        assert result["preferred_variant"] is None
        assert result["resolution"] == "no_variants"
    finally:
        conn.close()


def test_variant_resolution_prefers_air_fryer(env: tuple[Path, GrossyPaths]) -> None:
    """FR-100: An eligible air-fryer variant is preferred over the base recipe."""
    tmp_path, paths = env
    # Import the air-fryer variant first (must exist for the link)
    _import_recipe(
        tmp_path,
        paths,
        "air-fryer-variant",
        appliances=[{"type": "air_fryer", "capability": "air_frying"}],
    )
    # Import the base recipe with a variant link to the air-fryer version
    _import_recipe(
        tmp_path,
        paths,
        "base-recipe",
        variants=[{"linked_recipe_id": "air-fryer-variant", "variant_type": "air_fryer"}],
    )

    configured = {"air_fryer": ApplianceConfig(available=True)}
    conn = open_connection(paths.database_file)
    try:
        result = resolve_variant_preference(conn, "base-recipe", configured)
        assert result["preferred_variant"] == "air-fryer-variant"
        assert result["variant_type"] == "air_fryer"
        assert result["resolution"] == "variant_preferred"
    finally:
        conn.close()


def test_variant_resolution_prefers_thermomix(env: tuple[Path, GrossyPaths]) -> None:
    """FR-100: An eligible Thermomix-native variant is preferred over the base recipe."""
    tmp_path, paths = env
    _import_recipe(
        tmp_path,
        paths,
        "thermomix-variant",
        appliances=[{"type": "thermomix", "capability": "mixing"}],
    )
    _import_recipe(
        tmp_path,
        paths,
        "manual-recipe",
        variants=[{"linked_recipe_id": "thermomix-variant", "variant_type": "thermomix_native"}],
    )

    configured = {"thermomix": ApplianceConfig(available=True)}
    conn = open_connection(paths.database_file)
    try:
        result = resolve_variant_preference(conn, "manual-recipe", configured)
        assert result["preferred_variant"] == "thermomix-variant"
        assert result["variant_type"] == "thermomix_native"
    finally:
        conn.close()


def test_variant_resolution_air_fryer_over_thermomix(env: tuple[Path, GrossyPaths]) -> None:
    """FR-100: Air-fryer preference takes priority over Thermomix-native."""
    tmp_path, paths = env
    _import_recipe(
        tmp_path,
        paths,
        "air-fryer-v",
        appliances=[{"type": "air_fryer", "capability": "air_frying"}],
    )
    _import_recipe(
        tmp_path,
        paths,
        "thermo-v",
        appliances=[{"type": "thermomix", "capability": "mixing"}],
    )
    _import_recipe(
        tmp_path,
        paths,
        "base-v",
        variants=[
            {"linked_recipe_id": "thermo-v", "variant_type": "thermomix_native"},
            {"linked_recipe_id": "air-fryer-v", "variant_type": "air_fryer"},
        ],
    )

    configured = {
        "air_fryer": ApplianceConfig(available=True),
        "thermomix": ApplianceConfig(available=True),
    }
    conn = open_connection(paths.database_file)
    try:
        result = resolve_variant_preference(conn, "base-v", configured)
        assert result["preferred_variant"] == "air-fryer-v"
        assert result["variant_type"] == "air_fryer"
    finally:
        conn.close()


def test_variant_resolution_falls_back_when_variant_unavailable(
    env: tuple[Path, GrossyPaths],
) -> None:
    """An unavailable variant must not displace the base recipe.

    The air-fryer variant requires an air fryer that is not configured:
    resolution falls back to the base recipe instead of the variant.
    """
    tmp_path, paths = env
    _import_recipe(
        tmp_path,
        paths,
        "air-fryer-variant",
        appliances=[{"type": "air_fryer", "capability": "air_frying"}],
    )
    _import_recipe(
        tmp_path,
        paths,
        "base-recipe",
        variants=[{"linked_recipe_id": "air-fryer-variant", "variant_type": "air_fryer"}],
    )

    # No air fryer configured: the variant is not eligible.
    configured = {"stovetop": ApplianceConfig(available=True)}
    conn = open_connection(paths.database_file)
    try:
        result = resolve_variant_preference(conn, "base-recipe", configured)
        assert result["preferred_variant"] is None
        assert result["resolution"] == "base_recipe"
        assert "no linked variant is eligible" in result["detail"]
    finally:
        conn.close()


def test_variant_resolution_falls_back_on_missing_capacity_metadata(
    env: tuple[Path, GrossyPaths],
) -> None:
    """FR-97: A capacity-dependent variant is not eligible without capacity metadata."""
    tmp_path, paths = env
    _import_recipe(
        tmp_path,
        paths,
        "air-fryer-variant",
        appliances=[
            {
                "type": "air_fryer",
                "capability": "air_frying",
                "capacity_constraint": 4.0,
                "capacity_unit": "litres",
            }
        ],
    )
    _import_recipe(
        tmp_path,
        paths,
        "base-recipe",
        variants=[{"linked_recipe_id": "air-fryer-variant", "variant_type": "air_fryer"}],
    )

    # Air fryer available but with no capacity metadata: the variant is not
    # eligible for a capacity-dependent choice (FR-97).
    configured = {"air_fryer": ApplianceConfig(available=True, capacity_litres=None)}
    conn = open_connection(paths.database_file)
    try:
        result = resolve_variant_preference(conn, "base-recipe", configured)
        assert result["preferred_variant"] is None
        assert result["resolution"] == "base_recipe"
    finally:
        conn.close()


def test_variant_resolution_skips_ineligible_to_eligible_lower_preference(
    env: tuple[Path, GrossyPaths],
) -> None:
    """Preference applies only among eligible variants.

    The higher-preference air-fryer variant is not eligible (no air fryer),
    so the lower-preference Thermomix-native variant (eligible) is chosen.
    """
    tmp_path, paths = env
    _import_recipe(
        tmp_path,
        paths,
        "air-fryer-v",
        appliances=[{"type": "air_fryer", "capability": "air_frying"}],
    )
    _import_recipe(
        tmp_path,
        paths,
        "thermo-v",
        appliances=[{"type": "thermomix", "capability": "mixing"}],
    )
    _import_recipe(
        tmp_path,
        paths,
        "base-v",
        variants=[
            {"linked_recipe_id": "thermo-v", "variant_type": "thermomix_native"},
            {"linked_recipe_id": "air-fryer-v", "variant_type": "air_fryer"},
        ],
    )

    configured = {"thermomix": ApplianceConfig(available=True)}
    conn = open_connection(paths.database_file)
    try:
        result = resolve_variant_preference(conn, "base-v", configured)
        assert result["preferred_variant"] == "thermo-v"
        assert result["variant_type"] == "thermomix_native"
    finally:
        conn.close()


def test_variant_resolution_unknown_recipe_fails(env: tuple[Path, GrossyPaths]) -> None:
    """Resolving a variant for an unknown recipe raises an error."""
    tmp_path, paths = env
    configured = {"stovetop": ApplianceConfig(available=True)}
    conn = open_connection(paths.database_file)
    try:
        with pytest.raises(VariantResolutionError, match="unknown recipe"):
            resolve_variant_preference(conn, "nonexistent", configured)
    finally:
        conn.close()


# --- conventional recipe protection (FR-98) -------------------------------------------------


def test_conventional_recipe_never_silently_rewritten(env: tuple[Path, GrossyPaths]) -> None:
    """FR-98: Variant resolution does NOT modify the base recipe.

    Uses a recipe with a real variant link and an ELIGIBLE variant (air fryer
    is configured and available). After resolution, the variant is selected,
    but the full base recipe snapshot (including identity, steps, ingredients,
    and revision pointer) must be byte-for-byte unchanged.
    """
    tmp_path, paths = env
    # Import the variant target first (must exist for the link).
    _import_recipe(
        tmp_path,
        paths,
        "linked-variant",
        appliances=[{"type": "air_fryer", "capability": "air_frying"}],
    )
    # Import the base recipe with a variant link.
    _import_recipe(
        tmp_path,
        paths,
        "base-recipe",
        variants=[{"linked_recipe_id": "linked-variant", "variant_type": "air_fryer"}],
    )
    # configure the air fryer as available so the variant is ELIGIBLE.
    configured = {
        "stovetop": ApplianceConfig(available=True),
        "air_fryer": ApplianceConfig(available=True),
    }
    conn = open_connection(paths.database_file)
    try:
        # Record the full snapshot before resolution.
        rev_id_before = conn.execute(
            "SELECT current_revision_id FROM recipes WHERE recipe_id = ?",
            ("base-recipe",),
        ).fetchone()[0]
        from grossy.recipes.repository import get_revision_snapshot

        snapshot_before = get_revision_snapshot(conn, rev_id_before)
        # Resolve variant (read-only operation).
        result = resolve_variant_preference(conn, "base-recipe", configured)
        # the variant IS eligible (air fryer available), so it is selected.
        assert result["preferred_variant"] == "linked-variant", (
            f"expected the air_fryer variant to be selected, got {result['preferred_variant']!r}"
        )
        # Verify the full base recipe snapshot is unchanged (not just the pointer).
        rev_id_after = conn.execute(
            "SELECT current_revision_id FROM recipes WHERE recipe_id = ?",
            ("base-recipe",),
        ).fetchone()[0]
        assert rev_id_after == rev_id_before
        snapshot_after = get_revision_snapshot(conn, rev_id_after)
        assert snapshot_after == snapshot_before, (
            "conventional recipe snapshot was mutated by variant resolution"
        )
    finally:
        conn.close()


# --- get_appliance_config_from_yaml ----------------------------------------------------------


def test_config_extraction_preserves_metadata() -> None:
    """FR-96: The config extraction preserves model/capacity metadata."""
    config = {
        "thermomix": {"available": True, "model": "TM6"},
        "air_fryer": {"available": True, "capacity_litres": 4},
        "steam_oven": {"available": True},
        "induction_cooktop": {"available": True},
        "microwave": {"available": True, "wattage": 900},
        "dough_kneading_machine": {"available": True},
        "pressure_cooker": {"available": True, "kind": "instant_pot", "capacity_litres": 6},
        "dutch_oven": {"available": True, "capacity_litres": 5.5},
    }
    result = get_appliance_config_from_yaml(config)
    assert result["thermomix"] == ApplianceConfig(available=True, model="TM6")
    assert result["air_fryer"] == ApplianceConfig(available=True, capacity_litres=4.0)
    assert result["steam_oven"] == ApplianceConfig(available=True)
    assert result["induction_cooktop"] == ApplianceConfig(available=True)
    assert result["microwave"] == ApplianceConfig(available=True, wattage=900.0)
    assert result["dough_kneading_machine"] == ApplianceConfig(available=True)
    assert result["pressure_cooker"] == ApplianceConfig(
        available=True, kind="instant_pot", capacity_litres=6.0
    )
    assert result["dutch_oven"] == ApplianceConfig(available=True, capacity_litres=5.5)
    assert len(result) == 8


def test_config_extraction_partial() -> None:
    """Missing appliances default to unavailable."""
    config = {"air_fryer": {"available": True}}
    result = get_appliance_config_from_yaml(config)
    assert result["air_fryer"] == ApplianceConfig(available=True)
    assert result["thermomix"] == ApplianceConfig(available=False)
    assert result["steam_oven"] == ApplianceConfig(available=False)


def test_config_extraction_empty() -> None:
    """An empty config means no appliances are available."""
    result = get_appliance_config_from_yaml({})
    for appliance in CONFIGURED_APPLIANCES:
        assert result[appliance] == ApplianceConfig(available=False)


def test_config_extraction_ignores_unknown_appliances() -> None:
    """Unknown appliance entries are not part of the configured set (FR-93)."""
    config = {"toaster": {"available": True}}
    result = get_appliance_config_from_yaml(config)
    assert "toaster" not in result
    assert len(result) == len(CONFIGURED_APPLIANCES)
