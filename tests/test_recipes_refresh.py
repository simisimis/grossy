"""Tests for recipe refresh: diff preview, apply, and snapshot retention (task 08).

Covers PRD FR-78..FR-80: refresh diff preview before apply, immutable
revision creation on apply, and retained historical revision snapshots.
Uses deterministic sanitized fixtures; no live source access.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from grossy.cli import app
from grossy.db.connection import open_connection
from grossy.init import do_init
from grossy.paths import GrossyPaths
from grossy.recipes import (
    RecipeRefreshError,
    get_revision_snapshot,
    list_revisions,
    run_import_apply,
    run_import_validate,
    run_refresh_apply,
    run_refresh_diff_with_paths,
    validate_recipe_refresh,
)
from grossy.recipes.proposal import proposal_checksum
from grossy.recipes.refresh import REFRESH_SCHEMA_NAME, REFRESH_SCHEMA_VERSION

RUNNER = CliRunner()
GENERATED_AT = "2026-09-26T12:00:00+00:00"


# --- helpers ------------------------------------------------------------------


def _recipe_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "recipe_id": "test-recipe",
        "title": "Test Recipe",
        "reference_url": "https://example.com/recipe/test",
        "source_name": "Test Source",
        "source_tier": "approved_established",
        "cuisine": "Italian",
        "protein_class": "poultry",
        "total_time_minutes": 30,
        "servings": 2.0,
        "ingredients": [
            {"name": "chicken breast", "amount": 500.0, "unit": "g"},
            {"name": "olive oil", "amount": 2.0, "unit": "tbsp", "pantry_eligible": True},
            {"name": "garlic", "amount": 3.0, "unit": "cloves", "indivisible": True},
        ],
        "steps": [
            "Season the chicken with salt and pepper",
            "Sear in olive oil until golden, 5 minutes per side",
            "Add garlic and cook for 2 minutes",
        ],
        "appliances": [{"type": "stovetop", "capability": "searing"}],
        "variants": [],
        "base_weight": 1.0,
        "is_favorite": False,
        "is_regular": True,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _import_proposal(recipe: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": "grossy.recipe.import.v1",
        "schema_version": 1,
        "generated_at": GENERATED_AT,
        "recipe": recipe,
    }
    checksum_source = {k: v for k, v in payload.items() if k != "checksum"}
    return {**payload, "checksum": proposal_checksum(checksum_source)}


def _refresh_payload(recipe_id: str = "test-recipe", **overrides: Any) -> dict[str, Any]:
    payload = _unchanged_refresh_payload(
        recipe_id=recipe_id,
        title="Test Recipe (Updated)",
        reference_url="https://example.com/recipe/test-v2",
        total_time_minutes=35,
        servings=3.0,
        ingredients=[
            {"name": "chicken thigh", "amount": 600.0, "unit": "g"},
            {"name": "olive oil", "amount": 3.0, "unit": "tbsp", "pantry_eligible": True},
            {"name": "garlic", "amount": 4.0, "unit": "cloves", "indivisible": True},
            {"name": "lemon", "amount": 1.0, "unit": "pc", "indivisible": True},
        ],
        steps=[
            "Season the chicken thighs with salt and pepper",
            "Sear in olive oil until golden, 6 minutes per side",
            "Add garlic and lemon, cook for 3 minutes",
            "Rest for 5 minutes before serving",
        ],
        provenance="url-refresh",
    )
    payload.update(overrides)
    return payload


def _refresh_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "schema_name": REFRESH_SCHEMA_NAME,
        "schema_version": REFRESH_SCHEMA_VERSION,
        "generated_at": GENERATED_AT,
        "payload": payload,
    }
    checksum_source = {k: v for k, v in envelope.items() if k != "checksum"}
    return {**envelope, "checksum": proposal_checksum(checksum_source)}


def _unchanged_refresh_payload(**overrides: Any) -> dict[str, Any]:
    """Imported content, with maintenance-only fields removed and import provenance."""
    payload = _recipe_payload()
    for field in ("base_weight", "is_favorite", "is_regular", "enabled"):
        del payload[field]
    payload["provenance"] = "url-import"
    payload.update(overrides)
    return payload


def _write_json(directory: Path, data: Any, name: str = "proposal.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, GrossyPaths]:
    """An initialized temporary home with one imported recipe."""
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    do_init(paths)
    # Import a recipe to refresh
    import_path = _write_json(tmp_path, _import_proposal(_recipe_payload()), "import.json")
    checksum = run_import_validate(import_path).to_dict()["proposal_checksum"]
    run_import_apply(import_path, checksum, paths)
    return tmp_path, paths


def _db(paths: GrossyPaths) -> sqlite3.Connection:
    return open_connection(paths.database_file)


def _recipe_rows(database: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Exact recipe-domain and migration rows, including all revisions and children.

    Fixed table names keep this a local assertion helper, not a generic DB framework.
    SELECT * also works on schema-v6 fixtures before snapshot columns exist.
    """
    with closing(open_connection(database)) as conn:
        return {
            "recipes": conn.execute("SELECT * FROM recipes ORDER BY id").fetchall(),
            "revisions": conn.execute("SELECT * FROM recipe_revisions ORDER BY id").fetchall(),
            "ingredients": conn.execute("SELECT * FROM recipe_ingredients ORDER BY id").fetchall(),
            "appliances": conn.execute(
                "SELECT * FROM recipe_appliance_requirements ORDER BY id"
            ).fetchall(),
            "variants": conn.execute("SELECT * FROM recipe_variants ORDER BY id").fetchall(),
            "migrations": conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall(),
        }


def _current_snapshot(paths: GrossyPaths, recipe_id: str = "test-recipe") -> dict[str, Any]:
    with closing(_db(paths)) as conn:
        revision = conn.execute(
            "SELECT current_revision_id FROM recipes WHERE recipe_id = ?", (recipe_id,)
        ).fetchone()[0]
        return get_revision_snapshot(conn, revision)


def _main_backups(paths: GrossyPaths) -> set[Path]:
    return set(paths.backups_dir.glob("pre-migration-*.sqlite3"))


def _preview_path(paths: GrossyPaths) -> Path:
    return paths.state_dir / "refresh_previews" / "test-recipe.json"


def _diff(
    tmp_path: Path, paths: GrossyPaths, payload: dict[str, Any] | None = None
) -> tuple[Path, dict[str, Any]]:
    """Write a refresh proposal and run the diff preview; return (path, diff dict)."""
    path = _write_json(tmp_path, _refresh_proposal(payload or _refresh_payload()), "refresh.json")
    return path, run_refresh_diff_with_paths(path, paths).to_dict()


UNCHANGED_INGREDIENTS = _recipe_payload()["ingredients"]
UNCHANGED_STEPS = _recipe_payload()["steps"]


# --- refresh proposal validation -------------------------------------------------


def test_valid_refresh_proposal_passes() -> None:
    data = _refresh_proposal(_refresh_payload())
    proposal = validate_recipe_refresh(data)
    assert proposal.schema_name == REFRESH_SCHEMA_NAME
    assert proposal.schema_version == 1
    assert proposal.payload.recipe_id == "test-recipe"
    assert proposal.payload.servings == 3.0
    assert len(proposal.payload.ingredients) == 4


@pytest.mark.parametrize(
    "level,field,value,error",
    [
        ("envelope", "schema_name", "grossy.recipe.import.v1", "schema_name"),
        ("envelope", "unexpected_field", "hello", "unknown field"),
        ("payload", "protein_class", "invalid", "protein_class"),
        ("payload", "steps", [], "between 1 and"),
        ("payload", "ingredients", [], "between 1 and"),
    ],
    ids=["wrong-schema", "unknown-field", "invalid-class", "empty-steps", "empty-ingredients"],
)
def test_refresh_proposal_rejects_invalid_fields(
    level: str, field: str, value: Any, error: str
) -> None:
    data = _refresh_proposal(_refresh_payload())
    target = data if level == "envelope" else data["payload"]
    target[field] = value
    with pytest.raises(RecipeRefreshError, match=error):
        validate_recipe_refresh(_checksummed(data))


def test_refresh_proposal_rejects_tampered_checksum() -> None:
    data = _refresh_proposal(_refresh_payload())
    data["payload"]["title"] = "Tampered Title"
    with pytest.raises(RecipeRefreshError, match="checksum does not match"):
        validate_recipe_refresh(data)


# --- refresh diff preview (FR-78) --------------------------------------------------


def test_refresh_diff_reports_changes(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    _, diff = _diff(tmp_path, paths)

    assert diff["schema_name"] == "grossy.recipe.refresh-diff"
    assert diff["status"] == "preview"
    assert diff["recipe_id"] == "test-recipe"
    assert diff["has_changes"] is True
    assert "changes" in diff
    # Title changed
    assert diff["changes"]["recipe_fields"]["title"]["old"] == "Test Recipe"
    assert diff["changes"]["recipe_fields"]["title"]["new"] == "Test Recipe (Updated)"
    # Servings changed
    assert diff["changes"]["servings"]["old"] == 2.0
    assert diff["changes"]["servings"]["new"] == 3.0
    # Ingredients changed
    assert "ingredients" in diff["changes"]
    # New revision number
    assert diff["next_revision_number"] == 2


def _appliance_row(**fields: Any) -> dict[str, Any]:
    return {
        "type": "stovetop",
        "capability": "searing",
        "model_constraint": None,
        "capacity_constraint": None,
        "capacity_unit": None,
        **fields,
    }


GARLIC_INPUT = {"name": "garlic", "amount": 3.0, "unit": "cloves", "indivisible": True}
GARLIC_ROW = {
    "name": "garlic",
    "amount": 3.0,
    "unit": "cloves",
    "pantry_eligible": False,
    "indivisible": True,
    "package_unit": None,
    "package_size": None,
    "notes": None,
}


@pytest.mark.parametrize(
    "overrides,expected_changes",
    [
        pytest.param(
            {
                "appliances": [
                    {"type": "stovetop", "capability": "searing", "model_constraint": "induction"}
                ]
            },
            {
                "appliances": [
                    {
                        "action": "changed",
                        "type": "stovetop",
                        "capability": "searing",
                        "fields": {"model_constraint": {"old": None, "new": "induction"}},
                    }
                ]
            },
            id="model-constraint-changed",
        ),
        pytest.param(
            {
                "appliances": [
                    {
                        "type": "stovetop",
                        "capability": "searing",
                        "capacity_constraint": 5.0,
                        "capacity_unit": "liters",
                    }
                ]
            },
            {
                "appliances": [
                    {
                        "action": "changed",
                        "type": "stovetop",
                        "capability": "searing",
                        "fields": {
                            "capacity_constraint": {"old": None, "new": 5.0},
                            "capacity_unit": {"old": None, "new": "liters"},
                        },
                    }
                ]
            },
            id="capacity-constraint-changed",
        ),
        pytest.param(
            {"appliances": [{"type": "pressure_cooker", "capability": "pressure_cooking"}]},
            {
                "appliances": [
                    {
                        "action": "added",
                        "type": "pressure_cooker",
                        "capability": "pressure_cooking",
                        "new": _appliance_row(
                            type="pressure_cooker", capability="pressure_cooking"
                        ),
                    },
                    {
                        "action": "removed",
                        "type": "stovetop",
                        "capability": "searing",
                        "old": _appliance_row(),
                    },
                ]
            },
            id="appliance-added-and-removed",
        ),
        # P2 (FR-78): duplicates are compared by multiplicity, never collapsed.
        pytest.param(
            {"ingredients": [*UNCHANGED_INGREDIENTS, {**UNCHANGED_INGREDIENTS[2]}]},
            {"ingredients": [{"action": "added", "name": "garlic", "new": GARLIC_ROW}]},
            id="duplicate-ingredient-added",
        ),
        pytest.param(
            {
                "appliances": [
                    {"type": "stovetop", "capability": "searing"},
                    {"type": "stovetop", "capability": "searing"},
                ]
            },
            {
                "appliances": [
                    {
                        "action": "added",
                        "type": "stovetop",
                        "capability": "searing",
                        "new": _appliance_row(),
                    }
                ]
            },
            id="duplicate-appliance-added",
        ),
        pytest.param(
            {
                "appliances": [
                    {"type": "stovetop", "capability": "searing"},
                    {"type": "stovetop", "capability": "searing", "model_constraint": "induction"},
                ]
            },
            {
                "appliances": [
                    {
                        "action": "added",
                        "type": "stovetop",
                        "capability": "searing",
                        "new": _appliance_row(model_constraint="induction"),
                    }
                ]
            },
            id="duplicate-appliance-with-constraint-added",
        ),
        # P2 (FR-78): a provenance-only change is visible, never silently applied.
        pytest.param(
            {"provenance": "manual-import"},
            {"provenance": {"old": "url-import", "new": "manual-import"}},
            id="provenance-only",
        ),
    ],
)
def test_refresh_diff_reports_exactly_the_changed_facts(
    env: tuple[Path, GrossyPaths], overrides: dict[str, Any], expected_changes: dict[str, Any]
) -> None:
    """FR-78: starting from the imported content, the diff contains exactly the
    intended change and no spurious recipe/ingredient/step/provenance entries."""
    tmp_path, paths = env
    _, diff = _diff(tmp_path, paths, _unchanged_refresh_payload(**overrides))
    assert diff["has_changes"] is True
    assert diff["changes"] == expected_changes


@pytest.mark.parametrize(
    "imported,refreshed,expected_changes",
    [
        pytest.param(
            {
                "ingredients": [
                    UNCHANGED_INGREDIENTS[0],
                    GARLIC_INPUT,
                    {**GARLIC_INPUT, "amount": 5.0},
                ]
            },
            {"ingredients": [UNCHANGED_INGREDIENTS[0], GARLIC_INPUT]},
            {
                "ingredients": [
                    {"action": "removed", "name": "garlic", "old": {**GARLIC_ROW, "amount": 5.0}}
                ]
            },
            id="existing-duplicate-ingredient-removed",
        ),
        pytest.param(
            {
                "appliances": [
                    {"type": "stovetop", "capability": "searing"},
                    {"type": "stovetop", "capability": "searing", "model_constraint": "induction"},
                ]
            },
            {"appliances": [{"type": "stovetop", "capability": "searing"}]},
            {
                "appliances": [
                    {
                        "action": "removed",
                        "type": "stovetop",
                        "capability": "searing",
                        "old": _appliance_row(model_constraint="induction"),
                    }
                ]
            },
            id="existing-duplicate-appliance-removed",
        ),
    ],
)
def test_refresh_diff_compares_existing_duplicates_by_multiplicity(
    tmp_path: Path,
    imported: dict[str, Any],
    refreshed: dict[str, Any],
    expected_changes: dict[str, Any],
) -> None:
    """FR-78: duplicate rows stored by an earlier import are diffed row by row."""
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    do_init(paths)
    import_path = _write_json(
        tmp_path, _import_proposal(_recipe_payload(**imported)), "import.json"
    )
    checksum = run_import_validate(import_path).to_dict()["proposal_checksum"]
    run_import_apply(import_path, checksum, paths)
    _, diff = _diff(tmp_path, paths, _unchanged_refresh_payload(**refreshed))
    assert diff["has_changes"] is True
    assert diff["changes"] == expected_changes


def test_refresh_diff_unknown_recipe(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    refresh_path = _write_json(
        tmp_path,
        _refresh_proposal(_refresh_payload(recipe_id="nonexistent")),
        "refresh.json",
    )
    with pytest.raises(RecipeRefreshError, match="does not exist"):
        run_refresh_diff_with_paths(refresh_path, paths)


def test_refresh_diff_requires_database(env: tuple[Path, GrossyPaths]) -> None:
    """Diff fails when the database doesn't exist."""
    tmp_path, paths = env
    paths_no_db = GrossyPaths(
        config_dir=tmp_path / "config2" / "grossy",
        data_dir=tmp_path / "data2" / "grossy",
        state_dir=tmp_path / "state2" / "grossy",
    )
    refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    with pytest.raises(RecipeRefreshError, match="database not found"):
        run_refresh_diff_with_paths(refresh_path, paths_no_db)


# --- refresh apply (FR-79) -----------------------------------------------------------


def test_refresh_apply_creates_revision_and_retains_historical_snapshot(
    env: tuple[Path, GrossyPaths],
) -> None:
    """FR-78/79/80: apply accepts exactly the previewed checksum, appends an
    immutable revision and leaves the prior revision snapshot (including its
    identity fields) byte-identical."""
    tmp_path, paths = env
    old_snapshot = _current_snapshot(paths)
    payload = _refresh_payload(
        title="Completely Different Title", source_tier="akis_petretzikis", protein_class="meat"
    )
    refresh_path, diff = _diff(tmp_path, paths, payload)
    checksum = str(diff["proposal_checksum"])

    result = run_refresh_apply(refresh_path, checksum, paths)
    assert result.recipe_id == "test-recipe"
    assert result.revision_number == 2
    assert result.checksum == checksum

    with closing(_db(paths)) as conn:
        assert [r["revision_number"] for r in list_revisions(conn, "test-recipe")] == [1, 2]
        assert get_revision_snapshot(conn, old_snapshot["revision_id"]) == old_snapshot
        new_snapshot = get_revision_snapshot(conn, result.revision_id)
    assert (old_snapshot["title"], old_snapshot["source_tier"], old_snapshot["protein_class"]) == (
        "Test Recipe",
        "approved_established",
        "poultry",
    )
    assert (new_snapshot["title"], new_snapshot["source_tier"], new_snapshot["protein_class"]) == (
        "Completely Different Title",
        "akis_petretzikis",
        "meat",
    )
    assert new_snapshot["servings"] == 3.0
    assert new_snapshot["total_time_minutes"] == 35
    assert len(new_snapshot["ingredients"]) == 4
    assert new_snapshot["steps"] != old_snapshot["steps"]
    assert _current_snapshot(paths)["revision_id"] == result.revision_id


def test_refresh_apply_preserves_linked_variant_snapshot(
    tmp_path: Path,
) -> None:
    """Refreshing a recipe with a revision-bound variant link preserves the old snapshot.

    Imports a base recipe with a variant link, refreshes it with changed
    content (title, servings, appliances), and asserts:
    - The full old revision snapshot is byte-identical after the refresh.
    - The old revision's variant link is preserved.
    - The new revision has the expected content and variant link.
    - The current pointer advances to the new revision.
    """
    from grossy.recipes import (
        run_import_apply,
        run_import_validate,
    )

    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    do_init(paths)

    # Import the variant target first (must exist for the link).
    variant_recipe = _recipe_payload(
        recipe_id="air-fryer-variant",
        title="Air Fryer Variant",
        reference_url="https://example.com/recipe/variant",
        appliances=[{"type": "air_fryer", "capability": "air_frying"}],
    )
    variant_path = _write_json(tmp_path, _import_proposal(variant_recipe), "variant.json")
    variant_checksum = run_import_validate(variant_path).to_dict()["proposal_checksum"]
    run_import_apply(variant_path, variant_checksum, paths)

    # Import the base recipe with a variant link.
    base_recipe = _recipe_payload(
        recipe_id="linked-base",
        title="Linked Base Recipe",
        reference_url="https://example.com/recipe/base",
        variants=[{"linked_recipe_id": "air-fryer-variant", "variant_type": "air_fryer"}],
    )
    base_path = _write_json(tmp_path, _import_proposal(base_recipe), "base.json")
    base_checksum = run_import_validate(base_path).to_dict()["proposal_checksum"]
    run_import_apply(base_path, base_checksum, paths)

    # Capture the full old revision snapshot before the refresh.
    conn = _db(paths)
    try:
        old_rev_id = conn.execute(
            "SELECT current_revision_id FROM recipes WHERE recipe_id = 'linked-base'"
        ).fetchone()[0]
        old_snapshot = get_revision_snapshot(conn, old_rev_id)
    finally:
        conn.close()

    # Assert the old snapshot has the variant link.
    assert len(old_snapshot["variants"]) == 1
    assert old_snapshot["variants"][0]["linked_recipe_id"] == "air-fryer-variant"
    assert old_snapshot["variants"][0]["variant_type"] == "air_fryer"

    # Refresh with changed content: new title, servings, appliances.
    refresh_payload = _refresh_payload(
        recipe_id="linked-base",
        title="Linked Base Recipe Updated",
        servings=4.0,
        appliances=[
            {"type": "stovetop", "capability": "searing"},
            {"type": "air_fryer", "capability": "air_frying"},
        ],
        variants=[{"linked_recipe_id": "air-fryer-variant", "variant_type": "air_fryer"}],
    )
    refresh_path = _write_json(tmp_path, _refresh_proposal(refresh_payload), "refresh.json")
    diff = run_refresh_diff_with_paths(refresh_path, paths).to_dict()
    result = run_refresh_apply(refresh_path, str(diff["proposal_checksum"]), paths)

    # Assert the new revision has the expected content and variant link.
    conn = _db(paths)
    try:
        new_snapshot = get_revision_snapshot(conn, result.revision_id)
        assert new_snapshot["title"] == "Linked Base Recipe Updated"
        assert new_snapshot["servings"] == 4.0
        assert len(new_snapshot["appliances"]) == 2
        assert len(new_snapshot["variants"]) == 1
        assert new_snapshot["variants"][0]["linked_recipe_id"] == "air-fryer-variant"
        assert new_snapshot["variants"][0]["variant_type"] == "air_fryer"

        # The old snapshot must be byte-identical after the refresh.
        old_snapshot_after = get_revision_snapshot(conn, old_rev_id)
        assert old_snapshot_after == old_snapshot, (
            "historical revision snapshot was mutated by refresh with variant link"
        )

        # The current pointer must advance to the new revision.
        pointer = conn.execute(
            "SELECT current_revision_id FROM recipes WHERE recipe_id = 'linked-base'"
        ).fetchone()[0]
        assert pointer == result.revision_id

        # Revision count is exactly 2.
        rev_count = conn.execute(
            "SELECT COUNT(*) FROM recipe_revisions r "
            "JOIN recipes p ON p.id = r.recipe_id "
            "WHERE p.recipe_id = 'linked-base'"
        ).fetchone()[0]
        assert rev_count == 2
    finally:
        conn.close()


def test_refresh_apply_late_storage_failure_rolls_back(
    env: tuple[Path, GrossyPaths], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure after real revision/pointer/child writes leaves every recipe row intact."""
    import grossy.recipes.refresh as refresh_mod

    tmp_path, paths = env
    before = _recipe_rows(paths.database_file)
    original_insert = refresh_mod.insert_revision_for_existing_recipe

    def fail_after_writes(conn, **kwargs):
        original_insert(conn, **kwargs)
        raise sqlite3.OperationalError("simulated disk I/O failure")

    monkeypatch.setattr(refresh_mod, "insert_revision_for_existing_recipe", fail_after_writes)
    proposal = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    diff = run_refresh_diff_with_paths(proposal, paths).to_dict()
    with pytest.raises(sqlite3.OperationalError, match="simulated disk I/O failure"):
        run_refresh_apply(proposal, diff["proposal_checksum"], paths)
    assert _recipe_rows(paths.database_file) == before


def test_refresh_apply_requires_accepted_checksum(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    # Use a wrong checksum
    with pytest.raises(RecipeRefreshError, match="does not match the previewed checksum"):
        run_refresh_apply(refresh_path, "0" * 64, paths)


@pytest.mark.parametrize("recipe_id", ["test-recipe", "nonexistent"], ids=["known", "unknown"])
def test_refresh_apply_requires_prior_preview(
    env: tuple[Path, GrossyPaths], recipe_id: str
) -> None:
    """FR-78: apply is rejected when no refresh-diff preview was persisted, for a
    known recipe and for one that could never have been previewed."""
    tmp_path, paths = env
    refresh_path = _write_json(
        tmp_path, _refresh_proposal(_refresh_payload(recipe_id=recipe_id)), "refresh.json"
    )
    checksum = json.loads(refresh_path.read_text(encoding="utf-8"))["checksum"]
    with pytest.raises(RecipeRefreshError, match="no matching persisted preview"):
        run_refresh_apply(refresh_path, checksum, paths)


def test_multiple_refreshes_create_sequential_revisions(env: tuple[Path, GrossyPaths]) -> None:
    """Multiple refreshes create a sequence of immutable revisions."""
    tmp_path, paths = env

    # First refresh (FR-78: preview before apply)
    r1 = _write_json(
        tmp_path,
        _refresh_proposal(_refresh_payload(title="Version 2")),
        "refresh1.json",
    )
    data1 = json.loads(r1.read_text(encoding="utf-8"))
    run_refresh_diff_with_paths(r1, paths)
    run_refresh_apply(r1, data1["checksum"], paths)

    # Second refresh (FR-78: preview before apply)
    r2 = _write_json(
        tmp_path,
        _refresh_proposal(_refresh_payload(title="Version 3", servings=4.0)),
        "refresh2.json",
    )
    data2 = json.loads(r2.read_text(encoding="utf-8"))
    run_refresh_diff_with_paths(r2, paths)
    result2 = run_refresh_apply(r2, data2["checksum"], paths)

    assert result2.revision_number == 3

    conn = _db(paths)
    try:
        revisions = list_revisions(conn, "test-recipe")
        assert len(revisions) == 3
        assert revisions[0]["revision_number"] == 1
        assert revisions[1]["revision_number"] == 2
        assert revisions[2]["revision_number"] == 3
        # Verify the first revision is unchanged
        snap1 = get_revision_snapshot(conn, revisions[0]["id"])
        assert snap1["servings"] == 2.0
    finally:
        conn.close()


# --- CLI integration ---------------------------------------------------------------


@pytest.fixture()
def cli_home(
    env: tuple[Path, GrossyPaths], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, GrossyPaths]:
    """Initialized home with HOME/XDG bound to the env fixture's temp paths.

    Depends on ``env`` to avoid duplicating the init/import setup. Only adds
    the HOME/XDG environment bindings needed by CliRunner.
    """
    tmp_path, paths = env
    for var, root in (
        ("HOME", tmp_path),
        ("XDG_CONFIG_HOME", tmp_path / "config"),
        ("XDG_DATA_HOME", tmp_path / "data"),
        ("XDG_STATE_HOME", tmp_path / "state"),
    ):
        monkeypatch.setenv(var, str(root))
    return tmp_path, paths


def test_cli_refresh_diff(cli_home: tuple[Path, GrossyPaths]) -> None:
    tmp_path, _paths = cli_home
    refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    result = RUNNER.invoke(app, ["recipes", "refresh-diff", str(refresh_path)])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["status"] == "preview"


def test_cli_refresh_apply(cli_home: tuple[Path, GrossyPaths]) -> None:
    """Real CLI refresh-diff → refresh-apply integration with isolated HOME/XDG.

    Exercises the full CLI wiring: diff returns the checksum, apply uses it,
    the new revision is persisted, and the JSON output is versioned.
    Asserts schema fields and pointer-to-result identity on success.
    """
    tmp_path, paths = cli_home
    refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")

    # Step 1: diff preview via CLI.
    diff_result = RUNNER.invoke(app, ["recipes", "refresh-diff", str(refresh_path)])
    assert diff_result.exit_code == 0
    diff_data = json.loads(diff_result.stdout)
    assert diff_data["status"] == "preview"
    checksum = diff_data["proposal_checksum"]

    # Step 2: apply using the returned checksum via CLI.
    apply_result = RUNNER.invoke(
        app, ["recipes", "refresh-apply", str(refresh_path), "--checksum", checksum]
    )
    assert apply_result.exit_code == 0
    apply_data = json.loads(apply_result.stdout)
    # assert versioned JSON schema fields.
    assert apply_data["schema_name"] == "grossy.recipe.refresh-apply"
    assert apply_data["schema_version"] == 1
    assert apply_data["status"] == "applied"
    assert apply_data["recipe_id"] == "test-recipe"
    assert apply_data["revision_number"] == 2
    emitted_revision_id = apply_data["revision_id"]

    # Step 3: verify the new revision is persisted and the pointer matches.
    conn = open_connection(paths.database_file)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM recipe_revisions r "
            "JOIN recipes p ON p.id = r.recipe_id WHERE p.recipe_id = 'test-recipe'"
        ).fetchone()[0]
        assert count == 2
        # the persisted current_revision_id equals the emitted revision_id.
        pointer = conn.execute(
            "SELECT current_revision_id FROM recipes WHERE recipe_id = 'test-recipe'"
        ).fetchone()[0]
        assert pointer == emitted_revision_id, (
            f"persisted pointer {pointer} != emitted revision_id {emitted_revision_id}"
        )
    finally:
        conn.close()


def test_cli_refresh_apply_missing_checksum_is_usage_error(
    cli_home: tuple[Path, GrossyPaths],
) -> None:
    """refresh-apply without --checksum is a usage error (exit code != 0)."""
    tmp_path, _paths = cli_home
    refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    # First do the diff (required for apply).
    RUNNER.invoke(app, ["recipes", "refresh-diff", str(refresh_path)])
    # Apply without --checksum: Typer usage error.
    result = RUNNER.invoke(app, ["recipes", "refresh-apply", str(refresh_path)])
    assert result.exit_code != 0


def test_cli_refresh_apply_wrong_checksum_rejected(cli_home: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = cli_home
    proposal = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    assert RUNNER.invoke(app, ["recipes", "refresh-diff", str(proposal)]).exit_code == 0
    before = _recipe_rows(paths.database_file)
    result = RUNNER.invoke(app, ["recipes", "refresh-apply", str(proposal), "--checksum", "0" * 64])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "error"
    assert _recipe_rows(paths.database_file) == before


# --- schema-v6 refresh-diff upgrade regression (task 08 fix) -------------------


def _simulate_schema_v6(paths: GrossyPaths) -> None:
    """Simulate schema-v6, including removal of Task10's empty planning tables.

    Drops later recipe columns/tables and their migration markers. This helper
    operates only on the isolated recipe test fixture, which has no weekly plans.
    """
    conn = open_connection(paths.database_file)
    try:
        conn.execute("ALTER TABLE recipe_revisions DROP COLUMN title")
        conn.execute("ALTER TABLE recipe_revisions DROP COLUMN source_tier")
        conn.execute("ALTER TABLE recipe_revisions DROP COLUMN protein_class")
        conn.execute("DROP TABLE run_recipe_mappings")
        conn.execute("DROP TABLE run_recipe_seen")
        conn.execute("DROP TABLE run_recipe_plans")
        conn.execute("DELETE FROM schema_migrations WHERE version > 6")
        conn.commit()
    finally:
        conn.close()


def test_refresh_diff_upgrades_v6_with_backup_then_apply_retains_snapshot(
    env: tuple[Path, GrossyPaths],
    current_schema_version: int,
) -> None:
    """One production upgrade flow proves backup fidelity, backfill and retained revisions."""
    from grossy.db.migrations import get_schema_version
    from grossy.db.snapshot import verify_sqlite_snapshot

    tmp_path, paths = env
    _simulate_schema_v6(paths)
    assert get_schema_version(paths.database_file) == 6
    before = _recipe_rows(paths.database_file)
    backups_before = _main_backups(paths)
    proposal = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    diff = run_refresh_diff_with_paths(proposal, paths).to_dict()
    assert diff["status"] == "preview"
    assert diff["recipe_id"] == "test-recipe"
    assert diff["has_changes"] is True
    assert get_schema_version(paths.database_file) == current_schema_version
    new_backups = _main_backups(paths) - backups_before
    assert len(new_backups) == 1
    backup = new_backups.pop()
    verify_sqlite_snapshot(backup)
    assert _recipe_rows(backup) == before
    old_snapshot = _current_snapshot(paths)
    assert old_snapshot["title"] == "Test Recipe"
    assert old_snapshot["source_tier"] == "approved_established"
    assert old_snapshot["protein_class"] == "poultry"
    assert old_snapshot["servings"] == 2.0
    assert len(old_snapshot["ingredients"]) == 3
    result = run_refresh_apply(proposal, diff["proposal_checksum"], paths)
    assert result.revision_number == 2
    with closing(_db(paths)) as conn:
        assert get_revision_snapshot(conn, old_snapshot["revision_id"]) == old_snapshot
    new_snapshot = _current_snapshot(paths)
    assert new_snapshot["revision_id"] == result.revision_id
    assert new_snapshot["title"] == "Test Recipe (Updated)"
    assert new_snapshot["servings"] == 3.0


def test_refresh_diff_respects_writer_lock(env: tuple[Path, GrossyPaths]) -> None:
    """refresh-diff acquires the writer lock; a held lock causes failure.

    Uses a subprocess to hold the writer lock and verifies that
    ``run_refresh_diff_with_paths`` raises ``LockHeldError`` when the
    lock is already held by another process (PRD 13.6).
    """
    import multiprocessing
    import time

    from grossy.db.lock import LockHeldError

    tmp_path, paths = env

    def _hold_lock(lock_path_str: str, ready_path_str: str) -> None:
        """Child process: acquire the writer lock and signal readiness."""
        import fcntl
        import os

        lock_path = Path(lock_path_str)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, f"{os.getpid()}\n".encode())
        Path(ready_path_str).touch()
        # Hold the lock until killed (timeout safety)
        time.sleep(30)

    ready_file = paths.state_dir / "writer.lock.ready"
    ready_file.unlink(missing_ok=True)

    proc = multiprocessing.Process(
        target=_hold_lock,
        args=(str(paths.state_dir / "writer.lock"), str(ready_file)),
        daemon=True,
    )
    proc.start()
    # Wait for the child to acquire the lock
    deadline = time.monotonic() + 10
    while not ready_file.exists():
        if time.monotonic() > deadline:
            proc.terminate()
            pytest.fail("lock-holding subprocess did not start in time")
        time.sleep(0.05)

    try:
        refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
        with pytest.raises(LockHeldError):
            run_refresh_diff_with_paths(refresh_path, paths)
    finally:
        proc.terminate()
        proc.join(timeout=5)
        ready_file.unlink(missing_ok=True)


# --- migration failure diagnostics (P2 fix: PRD 18) ----------------------------


@pytest.mark.parametrize(
    "via_cli,stale_backup",
    [(False, True), (False, False), (True, True)],
    ids=["service-stale-backup", "service-no-backup", "cli-stale-backup"],
)
def test_refresh_diff_checksum_failure_never_reports_a_backup(
    cli_home: tuple[Path, GrossyPaths], via_cli: bool, stale_backup: bool
) -> None:
    """Checksum validation runs before backup creation, regardless of old directory contents."""
    tmp_path, paths = cli_home
    old_backups = _main_backups(paths)
    assert old_backups
    if not stale_backup:
        for backup in old_backups:
            backup.unlink()
    backups_before = _main_backups(paths)
    with closing(_db(paths)) as conn:
        conn.execute("UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 7")
        conn.commit()
    before = _recipe_rows(paths.database_file)
    proposal = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    if via_cli:
        result = RUNNER.invoke(app, ["recipes", "refresh-diff", str(proposal)])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["schema_name"] == "grossy.cli.status"
        assert data["status"] == "error"
        detail = data["detail"]
    else:
        with pytest.raises(RecipeRefreshError, match="migration failure") as exc:
            run_refresh_diff_with_paths(proposal, paths)
        detail = str(exc.value)
    assert "migration failure" in detail
    assert "checksum mismatch" in detail
    assert "No verified backup" in detail
    assert "backup: " not in detail
    assert all(str(backup) not in detail for backup in backups_before)
    assert _main_backups(paths) == backups_before
    assert _recipe_rows(paths.database_file) == before
    assert not _preview_path(paths).exists()


@pytest.mark.parametrize("via_cli", [False, True], ids=["service", "cli-json"])
def test_refresh_diff_migration_sql_failure_reports_verified_backup(
    cli_home: tuple[Path, GrossyPaths],
    monkeypatch: pytest.MonkeyPatch,
    via_cli: bool,
    current_schema_version: int,
) -> None:
    """Real write-then-fail migration: current verified backup, exact rollback, no preview."""
    import re

    import grossy.db.migrations as migrations_mod
    from grossy.db.snapshot import verify_sqlite_snapshot

    tmp_path, paths = cli_home
    migrations = tmp_path / "bad-migrations"
    migrations.mkdir()
    (migrations / f"{current_schema_version + 1:03}_regression_bad.sql").write_text(
        "CREATE TABLE grossy_regression_probe (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO grossy_regression_probe VALUES (42);\n"
        "SELECT * FROM grossy_regression_bad_table;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(migrations_mod, "MIGRATIONS_DIR", migrations)
    backups_before = _main_backups(paths)
    rows_before = _recipe_rows(paths.database_file)
    proposal = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    if via_cli:
        result = RUNNER.invoke(app, ["recipes", "refresh-diff", str(proposal)])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["schema_name"] == "grossy.cli.status"
        assert data["status"] == "error"
        detail = data["detail"]
    else:
        with pytest.raises(RecipeRefreshError, match="migration failure") as exc:
            run_refresh_diff_with_paths(proposal, paths)
        detail = str(exc.value)
    assert "migration failure" in detail
    assert "no such table" in detail
    assert "database is preserved" in detail
    match = re.search(r"backup: (\S+)", detail)
    assert match is not None
    backup = Path(match.group(1))
    assert _main_backups(paths) - backups_before == {backup}
    verify_sqlite_snapshot(backup)
    assert _recipe_rows(backup) == rows_before
    assert _recipe_rows(paths.database_file) == rows_before
    for database in (backup, paths.database_file):
        with closing(open_connection(database)) as conn:
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'grossy_regression_probe'"
                ).fetchone()
                is None
            )
    assert not _preview_path(paths).exists()


def test_cli_refresh_diff_failed_backup_verification_reports_no_verified_backup(
    cli_home: tuple[Path, GrossyPaths], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newly written corrupt backup is not a restore point; v6 rows remain intact."""
    import grossy.db.migrations as migrations_mod

    tmp_path, paths = cli_home
    _simulate_schema_v6(paths)
    before = _recipe_rows(paths.database_file)
    assert before["migrations"][-1][0] == 6
    backups_before = _main_backups(paths)
    corrupt_bytes = b"not a valid sqlite database (regression corruption)"

    def corrupt_backup(conn, dest: Path) -> None:
        dest.write_bytes(corrupt_bytes)

    monkeypatch.setattr(migrations_mod, "_backup_database", corrupt_backup)
    proposal = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    result = RUNNER.invoke(app, ["recipes", "refresh-diff", str(proposal)])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["schema_name"] == "grossy.cli.status"
    assert data["status"] == "error"
    assert "migration failure" in data["detail"]
    assert "No verified backup" in data["detail"]
    assert "backup: " not in data["detail"]
    new_backups = _main_backups(paths) - backups_before
    assert len(new_backups) == 1
    assert new_backups.pop().read_bytes() == corrupt_bytes
    assert _recipe_rows(paths.database_file) == before
    assert not _preview_path(paths).exists()


# --- stale preview rejection (P2 fix: FR-78) --------------------------------


@pytest.mark.parametrize("same_content", [False, True], ids=["changed-content", "same-content"])
def test_refresh_apply_stale_preview_rejected(
    env: tuple[Path, GrossyPaths], same_content: bool
) -> None:
    """Revision binding rejects replay, even when the accepted content is identical."""
    tmp_path, paths = env
    old_snapshot = _current_snapshot(paths)
    payload = _unchanged_refresh_payload() if same_content else _refresh_payload(title="Version A")
    proposal = _write_json(tmp_path, _refresh_proposal(payload), "refresh.json")
    diff = run_refresh_diff_with_paths(proposal, paths).to_dict()
    assert diff["has_changes"] is not same_content
    result = run_refresh_apply(proposal, diff["proposal_checksum"], paths)
    assert result.revision_number == 2
    assert result.revision_id != old_snapshot["revision_id"]
    if same_content:
        identity = {"revision_id", "revision_number"}
        new_snapshot = _current_snapshot(paths)
        assert {k: v for k, v in new_snapshot.items() if k not in identity} == {
            k: v for k, v in old_snapshot.items() if k not in identity
        }
    before_replay = _recipe_rows(paths.database_file)
    with pytest.raises(RecipeRefreshError, match="stale preview"):
        run_refresh_apply(proposal, diff["proposal_checksum"], paths)
    assert _recipe_rows(paths.database_file) == before_replay


# --- malformed envelope / security / permission tests ---------------------------


def _checksummed(data: dict[str, Any]) -> dict[str, Any]:
    """Recompute the checksum for a mutated proposal (for malformed-input tests)."""
    source = {k: v for k, v in data.items() if k != "checksum"}
    return {**data, "checksum": proposal_checksum(source)}


# --- stateful malformed input through the public diff path --------------------


@pytest.mark.parametrize(
    "mutation,expected_match",
    [
        (lambda d: d.update(schema_version=99), "schema_version must be 1"),
        (
            lambda d: d.update(payload={**d["payload"], "unexpected_nested": "x"}),
            "Extra inputs are not permitted",
        ),
        (
            lambda d: d.update(payload={**d["payload"], "steps": ["SELECT * FROM users"]}),
            "prohibited",
        ),
        (
            lambda d: d.update(
                payload={
                    **d["payload"],
                    "ingredients": [
                        {
                            "name": "garlic",
                            "amount": 3.0,
                            "unit": "cloves",
                            "notes": "javascript:alert(1)",
                        }
                    ],
                }
            ),
            "prohibited",
        ),
        (
            lambda d: d.update(payload={**d["payload"], "steps": ["<script>alert(1)</script>"]}),
            "prohibited",
        ),
    ],
    ids=["unsupported-version", "nested-unknown-field", "sql-step", "nested-notes", "script-step"],
)
def test_refresh_diff_malformed_input_leaves_state_unchanged(
    env: tuple[Path, GrossyPaths], mutation, expected_match: str
) -> None:
    """Rechecksummed invalid input fails at the public boundary without authorizing apply."""
    tmp_path, paths = env
    data = _refresh_proposal(_refresh_payload())
    mutation(data)
    bad_path = _write_json(tmp_path, _checksummed(data), "malformed.json")
    before = _recipe_rows(paths.database_file)
    with pytest.raises(RecipeRefreshError, match=expected_match):
        run_refresh_diff_with_paths(bad_path, paths)
    assert not _preview_path(paths).exists()
    assert _recipe_rows(paths.database_file) == before


def test_refresh_diff_malformed_json_leaves_state_unchanged(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    bad_path = tmp_path / "malformed.json"
    bad_path.write_text("not valid json at all", encoding="utf-8")
    before = _recipe_rows(paths.database_file)
    with pytest.raises(RecipeRefreshError, match="not valid JSON"):
        run_refresh_diff_with_paths(bad_path, paths)
    assert not _preview_path(paths).exists()
    assert _recipe_rows(paths.database_file) == before


def test_preview_state_corrupt_revision_binding_rejected(
    env: tuple[Path, GrossyPaths], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview state with a corrupt/missing revision binding is rejected.

    Manually writes a preview state file with no ``current_revision_id``.
    The apply must reject it rather than proceeding with an unbound preview.
    """
    tmp_path, paths = env
    refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
    data = json.loads(refresh_path.read_text(encoding="utf-8"))

    # Write a corrupt preview state: checksum matches but no revision binding.
    preview_dir = paths.state_dir / "refresh_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    (preview_dir / "test-recipe.json").write_text(
        json.dumps({"recipe_id": "test-recipe", "checksum": data["checksum"].lower()}),
        encoding="utf-8",
    )

    with pytest.raises(RecipeRefreshError, match="missing the bound revision ID"):
        run_refresh_apply(refresh_path, data["checksum"], paths)


def test_preview_state_permission_under_permissive_umask(
    env: tuple[Path, GrossyPaths],
) -> None:
    """Preview state file and directory have restrictive permissions (PRD §13).

    Under a permissive umask (0o022), the preview state directory must be 0700
    and the file must be 0600: ``_persist_preview_state`` enforces the policy
    (``ensure_private_dir`` + 0600 atomic write) so the modes do not depend on
    the ambient umask.
    """
    tmp_path, paths = env
    original_umask = os.umask(0o022)
    try:
        refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
        run_refresh_diff_with_paths(refresh_path, paths)

        preview_dir = paths.state_dir / "refresh_previews"
        preview_file = preview_dir / "test-recipe.json"
        assert preview_file.exists()

        # PRD §13: directory must be 0700 (owner rwx only).
        dir_mode = preview_dir.stat().st_mode & 0o777
        assert dir_mode == 0o700, f"preview state directory mode is {oct(dir_mode)}, expected 0700"
        # PRD §13: file must be 0600 (owner read/write only).
        file_mode = preview_file.stat().st_mode & 0o777
        assert file_mode == 0o600, f"preview state file mode is {oct(file_mode)}, expected 0600"
    finally:
        os.umask(original_umask)


def test_preview_state_replacement_reenforces_policy(
    env: tuple[Path, GrossyPaths],
) -> None:
    """Replacing an existing preview file re-enforces 0700/0600 (PRD §13).

    Simulates a legacy permissive state (directory 0755, prior file 0644).
    A new diff must replace the file and bring both back onto the policy
    under a permissive umask, with the new bound revision persisted.
    """
    tmp_path, paths = env
    preview_dir = paths.state_dir / "refresh_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.chmod(0o755)
    preview_file = preview_dir / "test-recipe.json"
    preview_file.write_text("{}", encoding="utf-8")
    preview_file.chmod(0o644)

    original_umask = os.umask(0o022)
    try:
        refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
        run_refresh_diff_with_paths(refresh_path, paths)

        assert preview_file.exists()
        assert preview_dir.stat().st_mode & 0o777 == 0o700, (
            f"replaced preview directory mode is {oct(preview_dir.stat().st_mode & 0o777)}, "
            "expected 0700"
        )
        assert preview_file.stat().st_mode & 0o777 == 0o600, (
            f"replaced preview file mode is {oct(preview_file.stat().st_mode & 0o777)}, "
            "expected 0600"
        )
        # The replacement carries the new bound preview state.
        data = json.loads(preview_file.read_text(encoding="utf-8"))
        assert data["recipe_id"] == "test-recipe"
        assert "current_revision_id" in data
    finally:
        os.umask(original_umask)


def test_preview_state_temp_file_is_private_before_publication(
    env: tuple[Path, GrossyPaths], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temporary preview file is 0600 before the rename publishes it.

    Spies on ``os.replace`` during the diff to observe the temporary file's
    mode at the moment of publication: under a permissive umask the temp file
    must already be 0600 (created with an explicit mode, not chmod-ed only
    after the rename).
    """
    tmp_path, paths = env
    preview_dir = paths.state_dir / "refresh_previews"
    observed_modes: list[int] = []
    real_replace = os.replace

    def _spy_replace(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
        src_path = Path(src)
        if src_path.parent == preview_dir:
            observed_modes.append(stat.S_IMODE(os.stat(src).st_mode))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy_replace)
    original_umask = os.umask(0o022)
    try:
        refresh_path = _write_json(tmp_path, _refresh_proposal(_refresh_payload()), "refresh.json")
        run_refresh_diff_with_paths(refresh_path, paths)
    finally:
        os.umask(original_umask)

    preview_file = preview_dir / "test-recipe.json"
    assert preview_file.exists()
    assert observed_modes, "no replace was observed for the preview state publication"
    assert all(mode == 0o600 for mode in observed_modes), (
        f"temporary preview file published with non-private modes: {observed_modes}"
    )
