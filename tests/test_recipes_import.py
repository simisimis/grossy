"""Tests for recipe import validation, preview/apply, and immutability (task 07).

Covers PRD FR-70..FR-75, FR-79, FR-80 and sections 10.3/11: strict
``grossy.recipe.import.v1`` validation with checksum preview binding,
fixture-based dry-run/apply without any live fetch, offline revision
storage, retained historical revision snapshots, and transactional
fail-closed behavior.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from grossy.cli import app
from grossy.db.connection import open_connection
from grossy.init import do_init
from grossy.paths import GrossyPaths
from grossy.recipes import (
    RecipeImportError,
    get_revision_snapshot,
    list_revisions,
    run_import_apply,
    run_import_validate,
    run_recipe_disable,
    run_recipe_weight,
    validate_recipe_import,
)
from grossy.recipes.proposal import proposal_checksum

RUNNER = CliRunner()
GENERATED_AT = "2026-09-26T12:00:00+00:00"


# --- helpers ------------------------------------------------------------------


def _recipe_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "recipe_id": "air-fryer-salmon",
        "title": "Air-fryer ginger-and-soy salmon with broccoli",
        "reference_url": "https://www.foodnetwork.com/recipes/kitchen/ginger-soy-salmon",
        "source_name": "Food Network",
        "source_tier": "approved_established",
        "cuisine": "Japanese",
        "protein_class": "fish",
        "total_time_minutes": 25,
        "servings": 2.0,
        "ingredients": [
            {"name": "salmon fillet", "amount": 400.0, "unit": "g"},
            {"name": "broccoli", "amount": 1.0, "unit": "head", "indivisible": True},
            {"name": "olive oil", "amount": 2.0, "unit": "tbsp", "pantry_eligible": True},
            {
                "name": "teriyaki sauce",
                "amount": 3.0,
                "unit": "tbsp",
                "package_unit": "bottle",
                "package_size": 250.0,
            },
        ],
        "steps": [
            "Pat the salmon dry and coat with the ginger-soy glaze",
            "Air-fry at 200 C for 8 minutes per side",
            "Steam the broccoli in the basket for the final 4 minutes",
        ],
        "appliances": [
            {
                "type": "air_fryer",
                "capability": "air_frying",
                "capacity_constraint": 4.0,
                "capacity_unit": "l",
            }
        ],
        "variants": [],
        "base_weight": 1.0,
        "is_favorite": False,
        "is_regular": True,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _proposal(recipe: dict[str, Any], **envelope_overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": "grossy.recipe.import.v1",
        "schema_version": 1,
        "generated_at": GENERATED_AT,
        "recipe": recipe,
    }
    payload.update(envelope_overrides)
    checksum_source = {k: v for k, v in payload.items() if k != "checksum"}
    return {**payload, "checksum": proposal_checksum(checksum_source)}


def _write_json(directory: Path, data: Any, name: str = "proposal.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, GrossyPaths]:
    """An initialized temporary home (config + migrated database)."""
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    do_init(paths)
    return tmp_path, paths


def _db(paths: GrossyPaths) -> sqlite3.Connection:
    return open_connection(paths.database_file)


def _count_recipe(conn: sqlite3.Connection, recipe_id: str) -> int:
    """Count recipe rows for a specific recipe_id (isolates from seeded rows)."""
    return conn.execute(
        "SELECT COUNT(*) FROM recipes WHERE recipe_id = ?", (recipe_id,)
    ).fetchone()[0]


def _count_revisions(conn: sqlite3.Connection, recipe_id: str) -> int:
    """Count revision rows belonging to a specific recipe_id."""
    return conn.execute(
        "SELECT COUNT(*) FROM recipe_revisions r "
        "JOIN recipes p ON p.id = r.recipe_id WHERE p.recipe_id = ?",
        (recipe_id,),
    ).fetchone()[0]


def _count_variants(conn: sqlite3.Connection, recipe_id: str) -> int:
    """Count variant rows declared by a specific recipe_id."""
    return conn.execute(
        "SELECT COUNT(*) FROM recipe_variants v "
        "JOIN recipe_revisions r ON r.id = v.revision_id "
        "JOIN recipes p ON p.id = r.recipe_id WHERE p.recipe_id = ?",
        (recipe_id,),
    ).fetchone()[0]


def _apply(path: Path, paths: GrossyPaths):
    """Preview then apply with the checksum the preview returned (FR-74/10.3.6)."""
    checksum = run_import_validate(path).to_dict()["proposal_checksum"]
    return run_import_apply(path, checksum, paths)


def _accepted_checksum(path: Path) -> str:
    """The checksum an accepted preview of ``path`` returns."""
    return run_import_validate(path).to_dict()["proposal_checksum"]


# --- strict proposal validation -------------------------------------------------


def test_valid_proposal_passes_and_preview_reports_facts(tmp_path: Path) -> None:
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    preview = run_import_validate(path).to_dict()
    assert preview["schema_name"] == "grossy.recipe.import-validate"
    assert preview["status"] == "preview"
    assert preview["schema"] == "grossy.recipe.import.v1"
    assert preview["recipe_id"] == "air-fryer-salmon"
    assert preview["title"] == "Air-fryer ginger-and-soy salmon with broccoli"
    assert preview["ingredient_count"] == 4
    assert preview["step_count"] == 3
    assert preview["appliance_count"] == 1
    assert preview["variant_count"] == 0
    assert preview["servings"] == 2.0
    assert len(preview["proposal_checksum"]) == 64


def test_preview_lists_exactly_the_persisted_facts(tmp_path: Path) -> None:
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    preview = run_import_validate(path).to_dict()
    # The preview exposes the normalized facts verbatim so the user approves
    # exactly what apply will persist (FR-74).
    assert preview["ingredients"][0] == {
        "name": "salmon fillet",
        "amount": 400.0,
        "unit": "g",
        "pantry_eligible": False,
        "indivisible": False,
        "package_unit": None,
        "package_size": None,
        "notes": None,
    }
    assert preview["steps"] == _recipe_payload()["steps"]
    assert preview["appliances"][0]["type"] == "air_fryer"


def test_tampered_payload_fails_checksum() -> None:
    proposal = _proposal(_recipe_payload())
    proposal["recipe"]["title"] = "Someone else's salmon"  # modify after checksum
    with pytest.raises(RecipeImportError, match="checksum does not match"):
        validate_recipe_import(proposal)


def test_tampered_file_cannot_be_applied(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    # The preview succeeds on the exact bytes and returns an accepted checksum...
    accepted = _accepted_checksum(path)
    # ...then a single-word modification must fail closed at apply (10.3.6).
    # The file's own checksum no longer matches its content.
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("salmon fillet", "tuna fillet"), encoding="utf-8")
    with pytest.raises(RecipeImportError, match="checksum does not match"):
        run_import_apply(path, accepted, paths)
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "air-fryer-salmon") == 0
    finally:
        conn.close()


def test_apply_requires_accepted_preview_checksum(env: tuple[Path, GrossyPaths]) -> None:
    """Apply with a checksum that was not the accepted preview fails closed."""
    tmp_path, paths = env
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    # A valid file whose own checksum matches, but applied with an unrelated
    # checksum (as if from a different or stale preview) must be refused.
    with pytest.raises(RecipeImportError, match="accepted preview checksum"):
        run_import_apply(path, "0" * 64, paths)
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "air-fryer-salmon") == 0
    finally:
        conn.close()


def test_edited_and_rechecksummed_proposal_cannot_be_applied(env: tuple[Path, GrossyPaths]) -> None:
    """Editing the content AND re-hashing it still cannot bypass the preview."""
    tmp_path, paths = env
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    accepted = _accepted_checksum(path)
    # Rewrite the file with different content and a freshly computed
    # self-checksum, so the file is internally consistent but was never
    # previewed in this form.
    data = json.loads(path.read_text(encoding="utf-8"))
    data["recipe"]["title"] = "Someone else's salmon"
    data["checksum"] = proposal_checksum({k: v for k, v in data.items() if k != "checksum"})
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    # Applying with the originally accepted preview checksum must fail closed.
    with pytest.raises(RecipeImportError, match="accepted preview checksum"):
        run_import_apply(path, accepted, paths)
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "air-fryer-salmon") == 0
    finally:
        conn.close()


def test_missing_checksum_field_rejected() -> None:
    data = _proposal(_recipe_payload())
    del data["checksum"]
    with pytest.raises(RecipeImportError, match="missing field"):
        validate_recipe_import(data)


def test_unknown_top_level_field_rejected() -> None:
    data = _proposal(_recipe_payload())
    data["extra"] = 1
    with pytest.raises(RecipeImportError, match="unknown field"):
        validate_recipe_import(data)


def test_unknown_nested_field_rejected() -> None:
    recipe = _recipe_payload()
    recipe["mystery"] = True
    with pytest.raises(RecipeImportError, match="recipe-import proposal validation failed"):
        validate_recipe_import(_proposal(recipe))


# --- envelope, enums, and sanitization gates -----------------------------------


@pytest.mark.parametrize(
    "mutation, match",
    [
        (lambda d: d.update(schema_name="grossy.recipe.import.v2"), "schema_name must be"),
        (lambda d: d.update(schema_version=2), "schema_version must be 1"),
        (lambda d: d.update(generated_at="2026-09-26T12:00:00"), "timezone-aware"),
        (lambda d: d.update(generated_at="not-a-time"), "valid ISO 8601"),
    ],
)
def test_envelope_violations_rejected(mutation: Any, match: str) -> None:
    data = _proposal(_recipe_payload())
    mutation(data)
    # Envelope edits invalidate the checksum binding too; either gate
    # failing closed is the correct outcome for tampering.
    with pytest.raises(RecipeImportError) as excinfo:
        validate_recipe_import(data)
    assert match in str(excinfo.value) or "checksum does not match" in str(excinfo.value)


def test_bad_checksum_format_rejected() -> None:
    data = _proposal(_recipe_payload())
    data["checksum"] = "abc"
    with pytest.raises(RecipeImportError, match="64-character hex"):
        validate_recipe_import(data)


def test_prohibited_content_rejected_in_steps() -> None:
    recipe = _recipe_payload()
    recipe["steps"] = ["<script>alert(1)</script>", "Normal step"]
    with pytest.raises(RecipeImportError, match="prohibited content"):
        validate_recipe_import(_proposal(recipe))


def test_prohibited_content_rejected_in_ingredient_notes() -> None:
    recipe = _recipe_payload()
    recipe["ingredients"][0]["notes"] = "use the bottle; SELECT * FROM secrets"
    with pytest.raises(RecipeImportError, match="prohibited content"):
        validate_recipe_import(_proposal(recipe))


def test_invalid_protein_class_rejected() -> None:
    with pytest.raises(RecipeImportError, match="protein_class must be one of"):
        validate_recipe_import(_proposal(_recipe_payload(protein_class="insect")))


def test_invalid_source_tier_rejected() -> None:
    with pytest.raises(RecipeImportError, match="source_tier must be one of"):
        validate_recipe_import(_proposal(_recipe_payload(source_tier="random_blog")))


def test_invalid_appliance_type_rejected() -> None:
    recipe = _recipe_payload()
    recipe["appliances"] = [{"type": "toaster", "capability": "toasting"}]
    with pytest.raises(RecipeImportError, match="appliance type must be one of"):
        validate_recipe_import(_proposal(recipe))


def test_capacity_pairing_rejected() -> None:
    recipe = _recipe_payload()
    recipe["appliances"] = [
        {"type": "air_fryer", "capability": "air_frying", "capacity_constraint": 4.0}
    ]
    with pytest.raises(RecipeImportError, match="provided together"):
        validate_recipe_import(_proposal(recipe))


def test_invalid_variant_type_rejected() -> None:
    recipe = _recipe_payload()
    recipe["variants"] = [{"linked_recipe_id": "other", "variant_type": "microwave"}]
    with pytest.raises(RecipeImportError, match="variant_type must be one of"):
        validate_recipe_import(_proposal(recipe))


def test_non_positive_ingredient_amount_rejected() -> None:
    recipe = _recipe_payload()
    recipe["ingredients"][0]["amount"] = 0
    with pytest.raises(RecipeImportError, match="greater than 0"):
        validate_recipe_import(_proposal(recipe))


def test_non_finite_ingredient_amount_rejected() -> None:
    recipe = _recipe_payload()
    recipe["ingredients"][0]["amount"] = float("inf")
    with pytest.raises(RecipeImportError, match="ingredient amount"):
        validate_recipe_import(_proposal(recipe))


def test_zero_servings_rejected() -> None:
    with pytest.raises(RecipeImportError, match="servings"):
        validate_recipe_import(_proposal(_recipe_payload(servings=0)))


def test_no_ingredients_rejected() -> None:
    with pytest.raises(RecipeImportError, match="between 1 and 50 ingredients"):
        validate_recipe_import(_proposal(_recipe_payload(ingredients=[])))


def test_no_steps_rejected() -> None:
    with pytest.raises(RecipeImportError, match="between 1 and 40 steps"):
        validate_recipe_import(_proposal(_recipe_payload(steps=[])))


def test_sourced_step_must_be_single_line() -> None:
    recipe = _recipe_payload()
    recipe["steps"] = ["Mix everything.\nThen rest for ten minutes."]
    with pytest.raises(RecipeImportError, match="must be a single line"):
        validate_recipe_import(_proposal(recipe))


def test_overlong_title_rejected() -> None:
    with pytest.raises(RecipeImportError, match="title must be at most 200"):
        validate_recipe_import(_proposal(_recipe_payload(title="x" * 201)))


def test_overlong_sourced_step_rejected() -> None:
    recipe = _recipe_payload()
    recipe["steps"] = ["y" * 401]
    with pytest.raises(RecipeImportError, match="steps\\[0\\] must be at most 400"):
        validate_recipe_import(_proposal(recipe))


def test_favorite_and_regular_conflict_rejected() -> None:
    with pytest.raises(RecipeImportError, match="mutually exclusive"):
        validate_recipe_import(_proposal(_recipe_payload(is_favorite=True, is_regular=True)))


def test_reference_url_required_for_url_import() -> None:
    recipe = _recipe_payload()
    del recipe["reference_url"]
    with pytest.raises(RecipeImportError, match="recipe-import proposal validation failed"):
        validate_recipe_import(_proposal(recipe))


def test_http_reference_url_rejected() -> None:
    with pytest.raises(RecipeImportError, match="must be an https URL"):
        validate_recipe_import(
            _proposal(_recipe_payload(reference_url="http://akispetretzikis.com/en/recipe/1"))
        )


def test_reference_url_with_query_rejected() -> None:
    with pytest.raises(RecipeImportError, match="query or fragment"):
        validate_recipe_import(
            _proposal(
                _recipe_payload(reference_url="https://akispetretzikis.com/en/recipe/1?ref=agent")
            )
        )


def test_invalid_recipe_id_rejected() -> None:
    with pytest.raises(RecipeImportError, match="recipe-import proposal validation failed"):
        validate_recipe_import(_proposal(_recipe_payload(recipe_id="Air Fryer!")))


def test_package_size_without_unit_rejected() -> None:
    recipe = _recipe_payload()
    recipe["ingredients"][0]["package_size"] = 250.0
    with pytest.raises(RecipeImportError, match="provided together"):
        validate_recipe_import(_proposal(recipe))


def test_whitespace_is_normalized(tmp_path: Path) -> None:
    recipe = _recipe_payload()
    recipe["ingredients"][0]["name"] = "  salmon\t  fillet "
    path = _write_json(tmp_path, _proposal(recipe))
    preview = run_import_validate(path).to_dict()
    assert preview["ingredients"][0]["name"] == "salmon fillet"


def test_invalid_json_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "proposal.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RecipeImportError, match="not valid JSON"):
        run_import_validate(path)


# --- fixture-based dry-run/apply (no fetch, no network) -------------------------


def test_apply_persists_offline_revision(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    result = _apply(path, paths)
    assert result.to_dict()["status"] == "applied"
    assert result.recipe_id == "air-fryer-salmon"
    assert result.revision_id >= 1

    conn = _db(paths)
    try:
        recipe = conn.execute(
            "SELECT title, reference_url, source_name, source_tier, cuisine, "
            "protein_class, total_time_minutes, current_revision_id, base_weight, "
            "is_favorite, is_regular, enabled FROM recipes "
            "WHERE recipe_id = 'air-fryer-salmon'"
        ).fetchone()
        assert recipe is not None
        assert recipe[0] == "Air-fryer ginger-and-soy salmon with broccoli"
        assert str(recipe[1]).startswith("https://www.foodnetwork.com/")
        assert (recipe[2], recipe[3], recipe[4]) == (
            "Food Network",
            "approved_established",
            "Japanese",
        )
        assert (recipe[5], recipe[6]) == ("fish", 25)
        assert recipe[7] == result.revision_id
        assert (recipe[8], recipe[9], recipe[10], recipe[11]) == (1.0, 0, 1, 1)

        revision = conn.execute(
            "SELECT revision_number, servings, provenance FROM recipe_revisions r "
            "JOIN recipes p ON p.id = r.recipe_id WHERE p.recipe_id = 'air-fryer-salmon'"
        ).fetchone()
        assert revision[0] == 1
        assert revision[1] == 2.0
        assert revision[2] == "url-import"

        ingredients = conn.execute(
            "SELECT i.ingredient_name, i.amount, i.unit, i.pantry_eligible, i.indivisible "
            "FROM recipe_ingredients i "
            "JOIN recipe_revisions r ON r.id = i.revision_id "
            "JOIN recipes p ON p.id = r.recipe_id "
            "WHERE p.recipe_id = 'air-fryer-salmon' ORDER BY i.id"
        ).fetchall()
        assert ingredients[0][:3] == ("salmon fillet", 400.0, "g")
        assert ingredients[1][4] == 1  # broccoli indivisible
        assert ingredients[2][3] == 1  # olive oil pantry eligible
    finally:
        conn.close()

    # Offline usability (FR-70): the stored revision snapshot carries
    # everything the planner needs without any source access.
    snapshot = get_revision_snapshot(_db(paths), result.revision_id)
    assert snapshot["revision_number"] == 1
    assert snapshot["servings"] == 2.0
    assert snapshot["steps"] == _recipe_payload()["steps"]
    assert [i["name"] for i in snapshot["ingredients"]] == [
        "salmon fillet",
        "broccoli",
        "olive oil",
        "teriyaki sauce",
    ]
    assert snapshot["appliances"][0]["type"] == "air_fryer"


def test_apply_rejects_second_import_of_same_id(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    _apply(path, paths)
    # Re-applying the same previewed proposal is a refresh (task 08),
    # not an import; the import path fails closed.
    with pytest.raises(RecipeImportError, match="already exists"):
        _apply(path, paths)
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "air-fryer-salmon") == 1
        assert _count_revisions(conn, "air-fryer-salmon") == 1
    finally:
        conn.close()


def test_apply_variant_to_unknown_recipe_leaves_nothing(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    recipe = _recipe_payload()
    recipe["variants"] = [{"linked_recipe_id": "ghost-recipe", "variant_type": "air_fryer"}]
    path = _write_json(tmp_path, _proposal(recipe))
    with pytest.raises(RecipeImportError, match="unknown recipe"):
        _apply(path, paths)
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "air-fryer-salmon") == 0
        assert _count_revisions(conn, "air-fryer-salmon") == 0
        assert _count_variants(conn, "air-fryer-salmon") == 0
    finally:
        conn.close()


def test_apply_variant_to_known_recipe_persists(env: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = env
    first = _recipe_payload(recipe_id="pan-fried-salmon", title="Pan-fried salmon")
    _apply(_write_json(tmp_path, _proposal(first), name="a.json"), paths)

    second = _recipe_payload(recipe_id="air-fryer-salmon")
    second["variants"] = [{"linked_recipe_id": "pan-fried-salmon", "variant_type": "air_fryer"}]
    _apply(_write_json(tmp_path, _proposal(second), name="b.json"), paths)

    conn = _db(paths)
    try:
        row = conn.execute(
            "SELECT p.recipe_id, l.recipe_id, v.variant_type "
            "FROM recipe_variants v "
            "JOIN recipe_revisions r ON r.id = v.revision_id "
            "JOIN recipes p ON p.id = r.recipe_id "
            "JOIN recipes l ON l.id = v.linked_recipe_id"
        ).fetchone()
        assert row == ("air-fryer-salmon", "pan-fried-salmon", "air_fryer")
    finally:
        conn.close()


def test_historical_revision_snapshot_is_retained(env: tuple[Path, GrossyPaths]) -> None:
    """Later imports and maintenance never alter a prior revision (FR-79, FR-80)."""
    tmp_path, paths = env
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    first = _apply(path, paths)
    snapshot_before = get_revision_snapshot(_db(paths), first.revision_id)

    # Later operations: another import plus maintenance on the first recipe.
    other = _recipe_payload(recipe_id="greek-yogurt-bowl", title="Greek yogurt bowl")
    _apply(_write_json(tmp_path, _proposal(other), name="b.json"), paths)
    run_recipe_weight(paths, "air-fryer-salmon", 2.5)
    run_recipe_disable(paths, "air-fryer-salmon")

    snapshot_after = get_revision_snapshot(_db(paths), first.revision_id)
    assert snapshot_after == snapshot_before

    revisions = list_revisions(_db(paths), "air-fryer-salmon")
    assert [r["revision_number"] for r in revisions] == [1]
    assert revisions[0]["steps"] == _recipe_payload()["steps"]


def test_variant_link_is_revision_bound_and_retained_in_snapshot(
    env: tuple[Path, GrossyPaths],
) -> None:
    """A revision's variant links are revision-bound and survive later changes.

    Task 07 only creates revision 1 (a later variant change arrives as a
    refresh in task 08); the revision binding plus snapshot inclusion is what
    keeps a historical snapshot's relationships intact once that exists.
    """
    tmp_path, paths = env
    # The linked target must exist before the variant link can be imported.
    target = _recipe_payload(recipe_id="pan-fried-salmon", title="Pan-fried salmon")
    _apply(_write_json(tmp_path, _proposal(target), name="a.json"), paths)

    recipe = _recipe_payload()  # air-fryer-salmon
    recipe["variants"] = [{"linked_recipe_id": "pan-fried-salmon", "variant_type": "air_fryer"}]
    first = _apply(_write_json(tmp_path, _proposal(recipe), name="b.json"), paths)

    conn = _db(paths)
    try:
        # The link is stored against the declaring revision, not the recipe row.
        target_row_id = conn.execute(
            "SELECT id FROM recipes WHERE recipe_id = 'pan-fried-salmon'"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT revision_id, linked_recipe_id, variant_type FROM recipe_variants"
        ).fetchone()
        assert row == (first.revision_id, target_row_id, "air_fryer")
    finally:
        conn.close()

    snapshot_before = get_revision_snapshot(_db(paths), first.revision_id)
    assert snapshot_before["variants"] == [
        {"linked_recipe_id": "pan-fried-salmon", "variant_type": "air_fryer"}
    ]

    # A later, unrelated change: another import plus maintenance on the recipe.
    other = _recipe_payload(recipe_id="greek-yogurt-bowl", title="Greek yogurt bowl")
    _apply(_write_json(tmp_path, _proposal(other), name="c.json"), paths)
    run_recipe_weight(paths, "air-fryer-salmon", 2.5)
    run_recipe_disable(paths, "air-fryer-salmon")

    snapshot_after = get_revision_snapshot(_db(paths), first.revision_id)
    assert snapshot_after == snapshot_before
    assert snapshot_after["variants"] == [
        {"linked_recipe_id": "pan-fried-salmon", "variant_type": "air_fryer"}
    ]


def test_missing_database_apply_fails_closed(tmp_path: Path) -> None:
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    accepted = _accepted_checksum(path)
    with pytest.raises(RecipeImportError, match="database not found"):
        run_import_apply(path, accepted, paths)
    assert not paths.database_file.exists()
    assert not (paths.state_dir / "writer.lock").exists()


# --- CLI wiring -----------------------------------------------------------------


@pytest.fixture()
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, GrossyPaths]:
    """Initialized home with HOME/XDG pointed at temp (for CliRunner)."""
    paths = GrossyPaths(
        config_dir=tmp_path / "config" / "grossy",
        data_dir=tmp_path / "data" / "grossy",
        state_dir=tmp_path / "state" / "grossy",
    )
    do_init(paths)
    for var, root in (
        ("HOME", tmp_path),
        ("XDG_CONFIG_HOME", tmp_path / "config"),
        ("XDG_DATA_HOME", tmp_path / "data"),
        ("XDG_STATE_HOME", tmp_path / "state"),
    ):
        monkeypatch.setenv(var, str(root))
    return tmp_path, paths


def test_cli_import_validate_emits_preview(cli_home: tuple[Path, GrossyPaths]) -> None:
    tmp_path, _paths = cli_home
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    result = RUNNER.invoke(app, ["recipes", "import-validate", str(path)])
    assert result.exit_code == 0
    preview = json.loads(result.stdout)
    assert preview["schema_name"] == "grossy.recipe.import-validate"
    assert preview["status"] == "preview"
    assert preview["recipe_id"] == "air-fryer-salmon"
    assert preview["step_count"] == 3


def test_cli_import_apply_success(cli_home: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = cli_home
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    checksum = _accepted_checksum(path)
    result = RUNNER.invoke(app, ["recipes", "import-apply", str(path), "--checksum", checksum])
    assert result.exit_code == 0
    stdout = json.loads(result.stdout)
    assert stdout["schema_name"] == "grossy.recipe.import-apply"
    assert stdout["status"] == "applied"
    assert stdout["recipe_id"] == "air-fryer-salmon"
    assert stdout["revision_number"] == 1
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "air-fryer-salmon") == 1
    finally:
        conn.close()


def test_cli_import_apply_requires_checksum_option(cli_home: tuple[Path, GrossyPaths]) -> None:
    """Applying without the preview checksum is a usage error (exit 2)."""
    tmp_path, paths = cli_home
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    result = RUNNER.invoke(app, ["recipes", "import-apply", str(path)])
    assert result.exit_code == 2
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "air-fryer-salmon") == 0
    finally:
        conn.close()


def test_cli_import_apply_wrong_preview_checksum_fails_closed(
    cli_home: tuple[Path, GrossyPaths],
) -> None:
    """Applying with a checksum that was not the accepted preview fails closed."""
    tmp_path, paths = cli_home
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    result = RUNNER.invoke(app, ["recipes", "import-apply", str(path), "--checksum", "0" * 64])
    assert result.exit_code == 1
    stdout = json.loads(result.stdout)
    assert stdout["status"] == "error"
    assert "accepted preview checksum" in stdout["detail"]
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "air-fryer-salmon") == 0
    finally:
        conn.close()


def test_cli_import_apply_tampered_fails_closed(cli_home: tuple[Path, GrossyPaths]) -> None:
    tmp_path, paths = cli_home
    path = _write_json(tmp_path, _proposal(_recipe_payload()))
    checksum = _accepted_checksum(path)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("salmon fillet", "tuna fillet"), encoding="utf-8")
    result = RUNNER.invoke(app, ["recipes", "import-apply", str(path), "--checksum", checksum])
    assert result.exit_code == 1
    stdout = json.loads(result.stdout)
    assert stdout["status"] == "error"
    assert "checksum does not match" in stdout["detail"]
    conn = _db(paths)
    try:
        assert _count_recipe(conn, "air-fryer-salmon") == 0
    finally:
        conn.close()
