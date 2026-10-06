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


# --- envelope, enums, and sanitization gates -----------------------------------


def _envelope(**overrides: Any) -> dict[str, Any]:
    """A proposal whose checksum is recomputed over the edited envelope.

    This isolates each envelope gate: the checksum binding stays valid, so
    only the specific diagnostic under test can reject the proposal.
    """
    return _proposal(_recipe_payload(), **overrides)


@pytest.mark.parametrize(
    "build, match",
    [
        pytest.param(lambda: _envelope(extra=1), "unknown field", id="unknown-top-level-field"),
        pytest.param(
            lambda: {k: v for k, v in _envelope().items() if k != "checksum"},
            "missing field",
            id="missing-checksum",
        ),
        pytest.param(
            lambda: _envelope(schema_name="grossy.recipe.import.v2"),
            "schema_name must be",
            id="wrong-schema-name",
        ),
        pytest.param(
            lambda: _envelope(schema_version=2), "schema_version must be 1", id="wrong-version"
        ),
        pytest.param(
            lambda: _envelope(generated_at="2026-09-26T12:00:00"),
            "timezone-aware",
            id="naive-generated_at",
        ),
        pytest.param(
            lambda: _envelope(generated_at="not-a-time"), "valid ISO 8601", id="bad-generated_at"
        ),
        pytest.param(
            lambda: {**_envelope(), "checksum": "abc"}, "64-character hex", id="bad-checksum"
        ),
    ],
)
def test_envelope_violations_rejected(build: Any, match: str) -> None:
    """Each envelope gate rejects with its own diagnostic, not the checksum fallback."""
    with pytest.raises(RecipeImportError, match=match):
        validate_recipe_import(build())


@pytest.mark.parametrize(
    "mutation, match",
    [
        pytest.param(
            lambda r: r.update(mystery=True),
            "recipe-import proposal validation failed",
            id="unknown-nested-field",
        ),
        pytest.param(
            lambda r: r.update(steps=["<script>alert(1)</script>", "Normal step"]),
            "prohibited content",
            id="script-step",
        ),
        pytest.param(
            lambda r: r["ingredients"][0].update(notes="use the bottle; SELECT * FROM secrets"),
            "prohibited content",
            id="sql-in-notes",
        ),
        pytest.param(
            lambda r: r.update(protein_class="insect"),
            "protein_class must be one of",
            id="invalid-protein-class",
        ),
        pytest.param(
            lambda r: r.update(source_tier="random_blog"),
            "source_tier must be one of",
            id="invalid-source-tier",
        ),
        pytest.param(
            lambda r: r.update(appliances=[{"type": "toaster", "capability": "toasting"}]),
            "appliance type must be one of",
            id="invalid-appliance-type",
        ),
        pytest.param(
            lambda r: r.update(
                appliances=[
                    {"type": "air_fryer", "capability": "air_frying", "capacity_constraint": 4.0}
                ]
            ),
            "provided together",
            id="capacity-without-unit",
        ),
        pytest.param(
            lambda r: r.update(
                variants=[{"linked_recipe_id": "other", "variant_type": "microwave"}]
            ),
            "variant_type must be one of",
            id="invalid-variant-type",
        ),
        pytest.param(
            lambda r: r["ingredients"][0].update(amount=0), "greater than 0", id="zero-amount"
        ),
        pytest.param(
            lambda r: r["ingredients"][0].update(amount=float("inf")),
            "ingredient amount",
            id="non-finite-amount",
        ),
        pytest.param(lambda r: r.update(servings=0), "servings", id="zero-servings"),
        pytest.param(
            lambda r: r.update(ingredients=[]),
            "between 1 and 50 ingredients",
            id="no-ingredients",
        ),
        pytest.param(lambda r: r.update(steps=[]), "between 1 and 40 steps", id="no-steps"),
        pytest.param(
            lambda r: r.update(steps=["Mix everything.\nThen rest for ten minutes."]),
            "must be a single line",
            id="multiline-step",
        ),
        pytest.param(
            lambda r: r.update(title="x" * 201), "title must be at most 200", id="overlong-title"
        ),
        pytest.param(
            lambda r: r.update(steps=["y" * 401]),
            "steps\\[0\\] must be at most 400",
            id="overlong-step",
        ),
        pytest.param(
            lambda r: r.update(is_favorite=True, is_regular=True),
            "mutually exclusive",
            id="favorite-and-regular",
        ),
        pytest.param(
            lambda r: r.pop("reference_url"),
            "recipe-import proposal validation failed",
            id="missing-reference-url",
        ),
        pytest.param(
            lambda r: r.update(reference_url="http://akispetretzikis.com/en/recipe/1"),
            "must be an https URL",
            id="http-reference-url",
        ),
        pytest.param(
            lambda r: r.update(reference_url="https://akispetretzikis.com/en/recipe/1?ref=agent"),
            "query or fragment",
            id="query-in-reference-url",
        ),
        pytest.param(
            lambda r: r.update(recipe_id="Air Fryer!"),
            "recipe-import proposal validation failed",
            id="invalid-recipe-id",
        ),
        pytest.param(
            lambda r: r["ingredients"][0].update(package_size=250.0),
            "provided together",
            id="package-size-without-unit",
        ),
    ],
)
def test_recipe_violations_rejected(mutation: Any, match: str) -> None:
    recipe = _recipe_payload()
    mutation(recipe)
    with pytest.raises(RecipeImportError, match=match):
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
